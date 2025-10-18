"""Tests for the securemtr integration setup lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from itertools import accumulate
from typing import Any, Awaitable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    _entry_display_name,
    _async_fetch_controller,
    _build_controller,
    _load_statistics_options,
    async_dispatch_runtime_update,
    async_run_with_reconnect,
    coerce_end_time,
    async_setup_entry,
    async_unload_entry,
    runtime_update_signal,
    consumption_metrics,
)
from custom_components.securemtr.beanbag import (
    BeanbagError,
    BeanbagGateway,
    BeanbagEnergySample,
    DailyProgram,
    BeanbagSession,
    BeanbagStateSnapshot,
    WeeklyProgram,
)
from custom_components.securemtr.config_flow import (
    CONF_ANCHOR_STRATEGY,
    CONF_BOOST_ANCHOR,
    CONF_ELEMENT_POWER_KW,
    CONF_PRIMARY_ANCHOR,
    CONF_TIME_ZONE,
    DEFAULT_BOOST_ANCHOR,
    DEFAULT_ELEMENT_POWER_KW,
    DEFAULT_PRIMARY_ANCHOR,
    DEFAULT_TIMEZONE,
)
from custom_components.securemtr.entity import slugify_identifier
from custom_components.securemtr.utils import report_day_for_sample, safe_anchor_datetime
from homeassistant.components.recorder.statistics import StatisticMeanType
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.util import dt as dt_util


@dataclass(slots=True)
class DummyConfigEntry:
    """Provide a lightweight stand-in for Home Assistant config entries."""

    entry_id: str
    data: dict[str, str]
    unique_id: str | None = None
    title: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    hass: Any | None = None


class FakeWebSocket:
    """Represent a simple closable WebSocket stub."""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        """Record the close invocation and mark the socket as closed."""

        self.close_calls += 1
        self.closed = True


@pytest.fixture(autouse=True)
def store_instances(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the Home Assistant Store with an in-memory implementation."""

    instances: list[Any] = []

    class FakeStore:
        def __init__(self, hass, version, key, *_args, **_kwargs) -> None:
            self.hass = hass
            self.version = version
            self.key = key
            self.data: dict[str, Any] | None = None
            self.saved: list[dict[str, Any]] = []

        async def async_load(self) -> dict[str, Any] | None:
            """Return previously saved data."""

            return self.data

        async def async_save(self, data: dict[str, Any]) -> None:
            """Store the provided payload."""

            self.data = data
            self.saved.append(data)

    def factory(hass, version, key, *_args, **_kwargs):
        store = FakeStore(hass, version, key, *_args, **_kwargs)
        instances.append(store)
        return store

    monkeypatch.setattr("custom_components.securemtr.Store", factory)
    return instances


@pytest.fixture
def capture_statistics(monkeypatch: pytest.MonkeyPatch):
    """Capture statistics imports for assertions."""

    captured: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}

    def _capture_statistics(_hass, metadata, statistics):
        captured[metadata["statistic_id"]] = (metadata, list(statistics))

    monkeypatch.setattr(
        "custom_components.securemtr.async_add_external_statistics",
        _capture_statistics,
    )
    return captured


class FakeBeanbagBackend:
    """Capture login requests and provide canned responses."""

    def __init__(self, session: object) -> None:
        self.session = session
        self.login_calls: list[tuple[str, str]] = []
        self.zone_calls: list[str] = []
        self.clock_calls: list[tuple[str, int]] = []
        self.schedule_calls: list[str] = []
        self.metadata_calls: list[str] = []
        self.configuration_calls: list[str] = []
        self.state_calls: list[str] = []
        self.energy_history_calls: list[tuple[str, int]] = []
        self.program_calls: list[tuple[str, str]] = []
        self._session = BeanbagSession(
            user_id=1,
            session_id="session-id",
            token="jwt-token",
            token_timestamp=None,
            gateways=(
                BeanbagGateway(
                    gateway_id="gateway-1",
                    serial_number="serial-1",
                    host_name="host-name",
                    capabilities={},
                ),
            ),
        )
        self.websocket = FakeWebSocket()
        self._primary_program: WeeklyProgram = tuple(
            DailyProgram((120, None, None), (240, None, None)) for _ in range(7)
        )
        self._boost_program: WeeklyProgram = tuple(
            DailyProgram((1020, None, None), (1080, None, None)) for _ in range(7)
        )

    async def login_and_connect(
        self, email: str, password_digest: str
    ) -> tuple[BeanbagSession, FakeWebSocket]:
        """Record the credentials and return canned connection artefacts."""

        self.login_calls.append((email, password_digest))
        return self._session, self.websocket

    async def read_device_metadata(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> dict[str, str]:
        """Return canned metadata for the sole controller."""

        self.metadata_calls.append(gateway_id)
        return {
            "BOI": "controller-1",
            "N": "E7+ Controller",
            "SN": "serial-1",
            "FV": "1.0.0",
            "MD": "E7+",
        }

    async def read_zone_topology(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> list[dict[str, str]]:
        """Return a single synthetic zone entry."""

        self.zone_calls.append(gateway_id)
        return [{"ZN": 1, "ZNM": "Primary"}]

    async def sync_gateway_clock(
        self,
        session: BeanbagSession,
        websocket: FakeWebSocket,
        gateway_id: str,
        *,
        timestamp: int | None = None,
    ) -> None:
        """Record the timestamp used for controller clock alignment."""

        self.clock_calls.append((gateway_id, int(timestamp or 0)))

    async def read_schedule_overview(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> dict[str, list[object]]:
        """Return a canned schedule overview payload."""

        self.schedule_calls.append(gateway_id)
        return {"V": []}

    async def read_device_configuration(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> dict[str, list[object]]:
        """Return canned configuration data."""

        self.configuration_calls.append(gateway_id)
        return {"V": []}

    async def read_live_state(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> BeanbagStateSnapshot:
        """Return a state snapshot with the primary power enabled."""

        self.state_calls.append(gateway_id)
        payload = {
            "V": [
                {"I": 1, "SI": 33, "V": [{"I": 6, "V": 2}]},
                {
                    "I": 2,
                    "SI": 16,
                    "V": [
                        {"I": 4, "V": 0},
                        {"I": 9, "V": 0},
                        {"I": 27, "V": 0},
                    ],
                },
            ]
        }
        return BeanbagStateSnapshot(
            payload=payload,
            primary_power_on=True,
            timed_boost_enabled=False,
            timed_boost_active=False,
            timed_boost_end_minute=None,
        )

    async def read_energy_history(
        self,
        session: BeanbagSession,
        websocket: FakeWebSocket,
        gateway_id: str,
        *,
        window_index: int = 1,
    ) -> list[BeanbagEnergySample]:
        """Return a canned set of energy samples."""

        self.energy_history_calls.append((gateway_id, window_index))
        samples: list[BeanbagEnergySample] = []
        base_timestamp = 1_700_000_000
        for offset in range(8):
            samples.append(
                BeanbagEnergySample(
                    timestamp=base_timestamp + offset * 86_400,
                    primary_energy_kwh=1.0 + offset,
                    boost_energy_kwh=0.5 * offset,
                    primary_scheduled_minutes=180 + offset * 10,
                    primary_active_minutes=120 + offset * 10,
                    boost_scheduled_minutes=offset * 15,
                    boost_active_minutes=offset * 5,
                )
            )
        return samples

    async def read_weekly_program(
        self,
        session: BeanbagSession,
        websocket: FakeWebSocket,
        gateway_id: str,
        *,
        zone: str,
    ) -> WeeklyProgram:
        """Return a weekly program for the requested zone."""

        self.program_calls.append((zone, gateway_id))
        if zone == "primary":
            return self._primary_program
        if zone == "boost":
            return self._boost_program
        raise BeanbagError(f"Unknown zone {zone}")

    async def turn_controller_on(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> None:
        """Pretend to send the power-on command."""

        self.state_calls.append(f"on:{gateway_id}")

    async def turn_controller_off(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> None:
        """Pretend to send the power-off command."""

        self.state_calls.append(f"off:{gateway_id}")


@pytest.mark.asyncio
async def test_async_run_with_reconnect_retries_operation() -> None:
    """Ensure the reconnect helper retries once after a Beanbag error."""

    class ReconnectingBackend(FakeBeanbagBackend):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self.websocket = FakeWebSocket()

        async def login_and_connect(
            self, email: str, password_digest: str
        ) -> tuple[BeanbagSession, FakeWebSocket]:
            self.login_calls.append((email, password_digest))
            self.websocket = FakeWebSocket()
            return self._session, self.websocket

    backend = ReconnectingBackend(object())
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = backend._session
    runtime.websocket = backend.websocket

    entry = DummyConfigEntry(
        entry_id="reconnect",
        data={"email": "user@example.com", "password": "digest"},
    )

    first_socket = runtime.websocket
    attempts = 0

    async def _operation(
        backend_obj: FakeBeanbagBackend,
        session: BeanbagSession,
        websocket: FakeWebSocket,
    ) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BeanbagError("send failure")
        assert session is backend._session
        assert websocket is backend.websocket
        return "ok"

    result = await async_run_with_reconnect(entry, runtime, _operation)

    assert result == "ok"
    assert attempts == 2
    assert backend.login_calls == [("user@example.com", "digest")]
    assert first_socket.close_calls == 1
    assert runtime.websocket is backend.websocket
    assert runtime.websocket.closed is False


@pytest.mark.asyncio
async def test_async_run_with_reconnect_propagates_when_refresh_fails() -> None:
    """Ensure the helper raises the original error if reconnection fails."""

    class FailingBackend(FakeBeanbagBackend):
        async def login_and_connect(
            self, email: str, password_digest: str
        ) -> tuple[BeanbagSession, FakeWebSocket]:
            self.login_calls.append((email, password_digest))
            raise BeanbagError("login failed")

    backend = FailingBackend(object())
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = backend._session
    runtime.websocket = backend.websocket

    entry = DummyConfigEntry(
        entry_id="reconnect-fail",
        data={"email": "user@example.com", "password": "digest"},
    )

    async def _operation(
        backend_obj: FakeBeanbagBackend,
        session: BeanbagSession,
        websocket: FakeWebSocket,
    ) -> None:
        raise BeanbagError("initial failure")

    with pytest.raises(BeanbagError) as excinfo:
        await async_run_with_reconnect(entry, runtime, _operation)

    assert str(excinfo.value) == "initial failure"
    assert backend.login_calls == [("user@example.com", "digest")]
    assert backend.websocket.closed is True
    assert runtime.websocket is None


class FakeConfigEntries:
    """Mimic Home Assistant's config entries helper."""

    def __init__(self) -> None:
        self.forwarded: list[tuple[str, ...]] = []
        self.unloaded: list[tuple[str, ...]] = []

    async def async_forward_entry_setups(
        self, entry: DummyConfigEntry, platforms: list[str]
    ) -> None:
        """Record forwarded platforms."""

        self.forwarded.append(tuple(platforms))

    async def async_unload_platforms(
        self, entry: DummyConfigEntry, platforms: list[str]
    ) -> bool:
        """Record unloaded platforms and report success."""

        self.unloaded.append(tuple(platforms))
        return True


class FakeHass:
    """Emulate the subset of Home Assistant APIs used by the integration."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, SecuremtrRuntimeData]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self.config_entries = FakeConfigEntries()
        self.config = SimpleNamespace(time_zone=DEFAULT_TIMEZONE)

    def async_create_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """Schedule a coroutine on the running loop and keep a reference."""

        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def async_block_till_done(self) -> None:
        """Await all scheduled tasks to complete."""

        if not self._tasks:
            return
        await asyncio.gather(*self._tasks)

    def verify_event_loop_thread(self, _caller: str) -> None:
        """Stub verification hook for dispatcher calls."""


@pytest.fixture
def track_time_spy(monkeypatch: pytest.MonkeyPatch):
    """Provide a helper to stub async_track_time_change and capture callbacks."""

    def installer(hass: FakeHass) -> list[tuple]:
        callbacks: list[tuple] = []

        def fake_track_time_change(
            hass_obj: FakeHass,
            action,
            *,
            hour: int | None = None,
            minute: int | None = None,
            second: int | None = None,
        ):
            assert hass_obj is hass
            callbacks.append((action, hour, minute, second))
            return lambda: None

        monkeypatch.setattr(
            "custom_components.securemtr.async_track_time_change",
            fake_track_time_change,
        )
        return callbacks

    return installer


@pytest.mark.asyncio
async def test_async_setup_entry_starts_backend(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
) -> None:
    """Verify that setup schedules the Beanbag login and stores runtime data."""

    fake_metrics = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr.consumption_metrics", fake_metrics
    )

    hass = FakeHass()
    callbacks = track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="1",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.backend is backend
    assert runtime.session is backend._session
    assert runtime.websocket is backend.websocket
    assert runtime.controller is not None
    assert runtime.controller.identifier == "controller-1"
    assert backend.login_calls == [("user@example.com", "digest")]
    assert backend.zone_calls == ["gateway-1"]
    assert backend.schedule_calls == ["gateway-1"]
    assert backend.metadata_calls == ["gateway-1"]
    assert backend.configuration_calls == ["gateway-1"]
    assert backend.state_calls[0] == "gateway-1"
    assert backend.clock_calls == [("gateway-1", 0)]
    assert runtime.zone_topology == [{"ZN": 1, "ZNM": "Primary"}]
    assert runtime.schedule_overview == {"V": []}
    assert runtime.device_metadata == {
        "BOI": "controller-1",
        "N": "E7+ Controller",
        "SN": "serial-1",
        "FV": "1.0.0",
        "MD": "E7+",
    }
    assert runtime.device_configuration == {"V": []}
    assert runtime.state_snapshot is not None
    assert runtime.state_snapshot.primary_power_on is True
    assert runtime.state_snapshot.timed_boost_enabled is False
    assert runtime.state_snapshot.timed_boost_active is False
    assert runtime.state_snapshot.timed_boost_end_minute is None
    assert runtime.primary_power_on is True
    assert runtime.timed_boost_enabled is False
    assert runtime.timed_boost_active is False
    assert runtime.timed_boost_end_minute is None
    assert runtime.timed_boost_end_time is None
    assert store_instances
    assert runtime.statistics_store is store_instances[0]
    assert hass.config_entries.forwarded == [
        ("switch",),
        ("button", "binary_sensor", "sensor"),
    ]
    assert callbacks and callbacks[0][1:] == (1, 0, 0)
    callback = callbacks[0][0]
    callback(datetime.now(timezone.utc))
    await hass.async_block_till_done()
    fake_metrics.assert_called_once_with(hass, entry)


@pytest.mark.asyncio
async def test_async_setup_entry_handles_missing_gateways(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure controller discovery errors leave the runtime in a safe state."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="missing-gateway",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class NoGatewayBackend(FakeBeanbagBackend):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self._session = BeanbagSession(
                user_id=1,
                session_id="session-id",
                token="jwt-token",
                token_timestamp=None,
                gateways=(),
            )

    fake_session = object()
    backend = NoGatewayBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_setup_entry_logs_clock_failure(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure clock sync errors do not abort controller discovery."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="clock-failure",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
    )

    class ClockErrorBackend(FakeBeanbagBackend):
        async def sync_gateway_clock(
            self,
            session: BeanbagSession,
            websocket: FakeWebSocket,
            gateway_id: str,
            *,
            timestamp: int | None = None,
        ) -> None:
            raise BeanbagError("clock-failed")

    fake_session = object()
    backend = ClockErrorBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is not None
    assert runtime.zone_topology == [{"ZN": 1, "ZNM": "Primary"}]


@pytest.mark.asyncio
async def test_async_setup_entry_logs_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Verify Beanbag metadata errors do not crash the startup task."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="metadata-error",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class MetadataFailingBackend(FakeBeanbagBackend):
        async def read_device_metadata(
            self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
        ) -> dict[str, str]:
            raise BeanbagError("metadata failure")

    fake_session = object()
    backend = MetadataFailingBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_setup_entry_handles_unexpected_metadata_error(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure unexpected metadata failures are caught and logged."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="metadata-exception",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class ExplodingBackend(FakeBeanbagBackend):
        async def read_device_metadata(
            self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
        ) -> dict[str, str]:
            raise RuntimeError("boom")

    fake_session = object()
    backend = ExplodingBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_setup_entry_handles_backend_error(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure backend failures are caught and do not populate runtime state."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="2",
        unique_id="user2@example.com",
        data={"email": "user2@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class FailingBackend(FakeBeanbagBackend):
        async def login_and_connect(self, email: str, password_digest: str):
            raise BeanbagError("login failed")

    fake_session = object()
    backend = FailingBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.session is None
    assert runtime.websocket is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_unload_entry_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Confirm unload cancels tasks and closes the websocket."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="3",
        unique_id="user3@example.com",
        data={"email": "user3@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    # Insert a hanging task to exercise the cancellation path.
    runtime.startup_task = asyncio.create_task(asyncio.sleep(0.1))

    assert await async_unload_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN]
    assert backend.websocket.close_calls == 1
    await asyncio.sleep(0)
    assert runtime.startup_task.cancelled()
    assert hass.config_entries.unloaded == [
        ("switch", "button", "binary_sensor", "sensor")
    ]


@pytest.mark.asyncio
async def test_async_setup_entry_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure backend startup short-circuits when credentials are absent."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(entry_id="4", unique_id="user4@example.com", data={})

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.session is None
    assert runtime.websocket is None
    assert backend.login_calls == []
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_unload_entry_without_runtime() -> None:
    """Verify unload succeeds gracefully when runtime data is missing."""

    hass = FakeHass()
    hass.data.setdefault(DOMAIN, {})
    entry = DummyConfigEntry(entry_id="missing", unique_id=None, data={})

    assert await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_async_setup_entry_without_config_entries_helper(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Exercise the setup path when Home Assistant lacks the helper attribute."""

    hass = FakeHass()
    track_time_spy(hass)
    hass.config_entries = None
    entry = DummyConfigEntry(
        entry_id="no-helper",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is not None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_unload_entry_without_config_entries_helper(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Exercise the unload path when Home Assistant lacks the helper attribute."""

    hass = FakeHass()
    track_time_spy(hass)
    hass.config_entries = None
    entry = DummyConfigEntry(
        entry_id="no-helper-unload",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    assert await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_consumption_metrics_refreshes_history(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    capture_statistics,
    store_instances,
) -> None:
    """Ensure consumption metrics refresh reconnects and stores samples."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="metrics",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    dispatch_calls: list[tuple[object, str]] = []

    def _capture_dispatch(hass_obj: object, entry_id: str) -> None:
        dispatch_calls.append((hass_obj, entry_id))

    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatch_runtime_update",
        _capture_dispatch,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    runtime.websocket.closed = True
    initial_logins = len(backend.login_calls)

    await consumption_metrics(hass, entry)

    assert len(backend.login_calls) == initial_logins + 1
    assert backend.energy_history_calls == [("gateway-1", 1)]
    assert backend.program_calls == [("primary", "gateway-1"), ("boost", "gateway-1")]

    tz = dt_util.get_time_zone("Europe/Dublin")
    base_timestamp = 1_700_000_000
    offsets = range(1, 8)
    expected_log = []
    for offset in offsets:
        epoch = base_timestamp + offset * 86_400
        report_day = report_day_for_sample(epoch, tz)
        expected_log.append(
            {
                "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                "epoch_seconds": epoch,
                "report_day": report_day.isoformat(),
                "primary_energy_kwh": 1.0 + offset,
                "boost_energy_kwh": 0.5 * offset,
                "primary_scheduled_minutes": 180 + offset * 10,
                "primary_active_minutes": 120 + offset * 10,
                "boost_scheduled_minutes": offset * 15,
                "boost_active_minutes": offset * 5,
            }
        )

    assert runtime.consumption_metrics_log == expected_log

    entry_slug = slugify_identifier(entry.title or entry.entry_id)
    stat_ids = {
        f"{DOMAIN}:{entry_slug}:primary_energy_kwh",
        f"{DOMAIN}:{entry_slug}:boost_energy_kwh",
        f"{DOMAIN}:{entry_slug}:primary_runtime_h",
        f"{DOMAIN}:{entry_slug}:primary_sched_h",
        f"{DOMAIN}:{entry_slug}:boost_runtime_h",
        f"{DOMAIN}:{entry_slug}:boost_sched_h",
    }
    assert set(capture_statistics) == stat_ids

    fallback_power = 2.85
    primary_energy = [
        (120 + offset * 10) / 60 * fallback_power for offset in offsets
    ]
    primary_cumulative = list(accumulate(primary_energy))
    boost_energy = [(offset * 5) / 60 * fallback_power for offset in offsets]
    boost_cumulative = list(accumulate(boost_energy))

    def _assert_energy(statistic_id: str, values: list[float], cumulative: list[float], anchor_time: time) -> None:
        metadata, stats = capture_statistics[statistic_id]
        assert metadata["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
        assert metadata["has_sum"] is True
        assert metadata["mean_type"] is StatisticMeanType.NONE
        for index, entry in enumerate(stats):
            day = report_day_for_sample(base_timestamp + (index + 1) * 86_400, tz)
            expected_anchor = safe_anchor_datetime(day, anchor_time, tz)
            assert entry["start"] == expected_anchor
            assert entry["state"] == pytest.approx(values[index])
            assert entry["sum"] == pytest.approx(cumulative[index])

    _assert_energy(
        f"{DOMAIN}:{entry_slug}:primary_energy_kwh",
        primary_energy,
        primary_cumulative,
        time(3, 0),
    )
    _assert_energy(
        f"{DOMAIN}:{entry_slug}:boost_energy_kwh",
        boost_energy,
        boost_cumulative,
        time(17, 30),
    )

    primary_runtime = [(120 + offset * 10) / 60 for offset in offsets]
    primary_scheduled = [(180 + offset * 10) / 60 for offset in offsets]
    boost_runtime = [(offset * 5) / 60 for offset in offsets]
    boost_scheduled = [(offset * 15) / 60 for offset in offsets]

    def _assert_duration(statistic_id: str, values: list[float], anchor_time: time) -> None:
        metadata, stats = capture_statistics[statistic_id]
        assert metadata["unit_of_measurement"] == UnitOfTime.HOURS
        assert metadata["has_sum"] is False
        assert metadata["mean_type"] is StatisticMeanType.ARITHMETIC
        for index, entry in enumerate(stats):
            day = report_day_for_sample(base_timestamp + (index + 1) * 86_400, tz)
            expected_anchor = safe_anchor_datetime(day, anchor_time, tz)
            assert entry["start"] == expected_anchor
            assert entry["mean"] == pytest.approx(values[index])
            assert entry["min"] == pytest.approx(values[index])
            assert entry["max"] == pytest.approx(values[index])

    _assert_duration(
        f"{DOMAIN}:{entry_slug}:primary_runtime_h", primary_runtime, time(3, 0)
    )
    _assert_duration(
        f"{DOMAIN}:{entry_slug}:primary_sched_h", primary_scheduled, time(3, 0)
    )
    _assert_duration(
        f"{DOMAIN}:{entry_slug}:boost_runtime_h", boost_runtime, time(17, 30)
    )
    _assert_duration(
        f"{DOMAIN}:{entry_slug}:boost_sched_h", boost_scheduled, time(17, 30)
    )

    assert store_instances and store_instances[0].saved
    persisted = store_instances[0].saved[-1]
    expected_last_day = report_day_for_sample(
        base_timestamp + offsets[-1] * 86_400, tz
    ).isoformat()
    assert persisted["primary"]["last_day"] == expected_last_day
    assert persisted["boost"]["last_day"] == expected_last_day
    assert persisted["primary"]["energy_sum"] == pytest.approx(primary_cumulative[-1])
    assert persisted["boost"]["energy_sum"] == pytest.approx(boost_cumulative[-1])
    assert runtime.statistics_state == persisted

    recent = runtime.statistics_recent
    assert isinstance(recent, dict)
    primary_recent = recent["primary"]
    boost_recent = recent["boost"]

    assert primary_recent["report_day"] == expected_last_day
    assert primary_recent["runtime_hours"] == pytest.approx(primary_runtime[-1])
    assert primary_recent["scheduled_hours"] == pytest.approx(primary_scheduled[-1])
    assert primary_recent["energy_sum"] == pytest.approx(primary_cumulative[-1])

    assert boost_recent["report_day"] == expected_last_day
    assert boost_recent["runtime_hours"] == pytest.approx(boost_runtime[-1])
    assert boost_recent["scheduled_hours"] == pytest.approx(boost_scheduled[-1])
    assert boost_recent["energy_sum"] == pytest.approx(boost_cumulative[-1])

    assert dispatch_calls == [(hass, entry.entry_id)]

@pytest.mark.asyncio
async def test_consumption_metrics_skips_processed_days(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    capture_statistics,
    store_instances,
) -> None:
    """Ensure repeated refreshes avoid duplicating statistics."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="metrics-idempotent",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    runtime.websocket.closed = True

    await consumption_metrics(hass, entry)
    first_save_count = len(store_instances[0].saved)
    persisted = store_instances[0].saved[-1]
    capture_statistics.clear()

    await consumption_metrics(hass, entry)

    assert not capture_statistics
    assert len(store_instances[0].saved) == first_save_count
    assert runtime.statistics_state == persisted
    assert len(backend.energy_history_calls) == 2


@pytest.mark.asyncio
async def test_consumption_metrics_imports_only_new_days(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    capture_statistics,
    store_instances,
) -> None:
    """Ensure only unprocessed days trigger external statistics imports."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="metrics-incremental",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    runtime.websocket.closed = True

    tz = dt_util.get_time_zone("Europe/Dublin")
    base_timestamp = 1_700_000_000
    processed_day = report_day_for_sample(base_timestamp + 4 * 86_400, tz)

    store_instances[0].data = {
        "primary": {"energy_sum": 10.0, "last_day": processed_day.isoformat()},
        "boost": {"energy_sum": 5.0, "last_day": processed_day.isoformat()},
    }
    runtime.statistics_state = None

    await consumption_metrics(hass, entry)

    entry_slug = slugify_identifier(entry.title or entry.entry_id)
    primary_id = f"{DOMAIN}:{entry_slug}:primary_energy_kwh"
    boost_id = f"{DOMAIN}:{entry_slug}:boost_energy_kwh"

    expected_days = [
        report_day_for_sample(base_timestamp + offset * 86_400, tz)
        for offset in range(5, 8)
    ]

    metadata, primary_stats = capture_statistics[primary_id]
    assert metadata["has_sum"] is True
    assert len(primary_stats) == len(expected_days)
    assert primary_stats[0]["start"].date() == expected_days[0]
    assert primary_stats[0]["sum"] > 10.0

    metadata, boost_stats = capture_statistics[boost_id]
    assert metadata["has_sum"] is True
    assert len(boost_stats) == len(expected_days)
    assert boost_stats[0]["start"].date() == expected_days[0]
    assert boost_stats[0]["sum"] > 5.0

    for statistic_id, (_metadata, stats) in capture_statistics.items():
        if statistic_id.endswith("_h"):
            continue
        assert len(stats) == len(expected_days)

    assert store_instances[0].saved
    saved_state = store_instances[0].saved[-1]
    assert saved_state["primary"]["last_day"] == expected_days[-1].isoformat()
    assert saved_state["boost"]["last_day"] == expected_days[-1].isoformat()


@pytest.mark.asyncio
async def test_consumption_metrics_honours_start_anchor_strategy(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    capture_statistics,
    store_instances,
) -> None:
    """Ensure the start anchor strategy uses schedule boundaries."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="metrics-anchors",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
        options={
            CONF_ANCHOR_STRATEGY: "start",
            CONF_TIME_ZONE: DEFAULT_TIMEZONE,
            CONF_PRIMARY_ANCHOR: DEFAULT_PRIMARY_ANCHOR,
            CONF_BOOST_ANCHOR: DEFAULT_BOOST_ANCHOR,
            CONF_ELEMENT_POWER_KW: DEFAULT_ELEMENT_POWER_KW,
        },
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    runtime.websocket.closed = True

    await consumption_metrics(hass, entry)

    entry_slug = slugify_identifier(entry.title or entry.entry_id)
    primary_id = f"{DOMAIN}:{entry_slug}:primary_energy_kwh"
    boost_id = f"{DOMAIN}:{entry_slug}:boost_energy_kwh"

    tz = dt_util.get_time_zone(DEFAULT_TIMEZONE)
    assert tz is not None
    base_timestamp = 1_700_000_000
    first_day = report_day_for_sample(base_timestamp + 86_400, tz)

    metadata, primary_stats = capture_statistics[primary_id]
    assert metadata["has_sum"] is True
    assert primary_stats[0]["start"] == safe_anchor_datetime(first_day, time(2, 0), tz)

    metadata, boost_stats = capture_statistics[boost_id]
    assert metadata["has_sum"] is True
    assert boost_stats[0]["start"] == safe_anchor_datetime(first_day, time(17, 0), tz)


def test_load_statistics_options_prefers_hass_timezone() -> None:
    """Ensure statistics options honour the Home Assistant timezone."""

    hass = FakeHass()
    hass.config.time_zone = "Europe/London"
    entry = DummyConfigEntry(entry_id="tz-pref", data={}, options={})
    entry.hass = hass

    options = _load_statistics_options(entry)

    assert options.timezone_name == "Europe/London"


def test_load_statistics_options_invalid_hass_timezone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ensure invalid Home Assistant timezones fall back to the default."""

    hass = FakeHass()
    hass.config.time_zone = "Mars/Olympus"
    entry = DummyConfigEntry(entry_id="tz-invalid", data={}, options={})
    entry.hass = hass

    with caplog.at_level(logging.WARNING):
        options = _load_statistics_options(entry)

    assert options.timezone_name == DEFAULT_TIMEZONE
    assert "Invalid timezone" in caplog.text


def test_load_statistics_options_missing_system_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure missing system time zone data falls back to the default time zone."""

    hass = FakeHass()
    hass.config.time_zone = "Mars/Olympus"
    entry = DummyConfigEntry(entry_id="tz-missing", data={}, options={})
    entry.hass = hass

    monkeypatch.setattr(
        "custom_components.securemtr.dt_util.get_time_zone", lambda _name: None
    )
    monkeypatch.setattr(
        "custom_components.securemtr.dt_util.get_default_time_zone",
        lambda: timezone.utc,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.dt_util.utcnow",
        lambda: datetime.now(timezone.utc),
    )

    options = _load_statistics_options(entry)

    assert options.timezone_name == "UTC"


@pytest.mark.asyncio
async def test_consumption_metrics_missing_runtime() -> None:
    """Ensure the helper exits quietly when runtime data is absent."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="missing-runtime",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
    )

    await consumption_metrics(hass, entry)


@pytest.mark.asyncio
async def test_consumption_metrics_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the helper logs an error when credentials are unavailable."""

    hass = FakeHass()
    runtime = FakeBeanbagBackend(object())
    data_runtime = SecuremtrRuntimeData(backend=runtime)
    hass.data.setdefault(DOMAIN, {})["no-creds"] = data_runtime
    entry = DummyConfigEntry(entry_id="no-creds", unique_id=None, data={})

    await consumption_metrics(hass, entry)
    assert runtime.login_calls == []


@pytest.mark.asyncio
async def test_consumption_metrics_login_failure(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure reconnection errors are logged and abort the refresh."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="login-failure",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
    )

    class FailingBackend(FakeBeanbagBackend):
        async def login_and_connect(self, email: str, password_digest: str):
            self.login_calls.append((email, password_digest))
            raise BeanbagError("boom")

    backend = FailingBackend(object())
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = None
    runtime.websocket = FakeWebSocket()
    runtime.controller = None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await consumption_metrics(hass, entry)
    assert len(backend.login_calls) == 1
    assert runtime.consumption_metrics_log == []


@pytest.mark.asyncio
async def test_consumption_metrics_energy_history_error(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
) -> None:
    """Ensure backend history errors abort the refresh."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="history-error",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
    )

    class HistoryBackend(FakeBeanbagBackend):
        async def read_energy_history(
            self,
            session: BeanbagSession,
            websocket: FakeWebSocket,
            gateway_id: str,
            *,
            window_index: int = 1,
        ) -> list[BeanbagEnergySample]:
            self.energy_history_calls.append((gateway_id, window_index))
            raise BeanbagError("history")

    backend = HistoryBackend(object())
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = backend._session
    runtime.websocket = backend.websocket
    runtime.controller = SecuremtrController(
        identifier="controller-1",
        name="E7+",
        gateway_id="gateway-1",
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await consumption_metrics(hass, entry)
    assert backend.energy_history_calls == [("gateway-1", 1)]
    assert runtime.consumption_metrics_log == []


@pytest.mark.asyncio
async def test_consumption_metrics_missing_connection_objects() -> None:
    """Ensure missing controller metadata aborts the refresh."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="missing-controller",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
    )

    backend = FakeBeanbagBackend(object())
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = SimpleNamespace()
    runtime.websocket = SimpleNamespace(closed=False)
    runtime.controller = None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await consumption_metrics(hass, entry)
    assert runtime.consumption_metrics_log == []


@pytest.mark.asyncio
async def test_async_fetch_controller_requires_connection() -> None:
    """Ensure controller fetching rejects missing session data."""

    runtime = SecuremtrRuntimeData(backend=FakeBeanbagBackend(object()))
    entry = DummyConfigEntry(
        entry_id="fetch-error",
        unique_id="user@example.com",
        data={},
    )

    with pytest.raises(BeanbagError):
        await _async_fetch_controller(entry, runtime)


def test_runtime_update_signal_helper() -> None:
    """Ensure the runtime update signal embeds the entry id."""

    assert runtime_update_signal("entry") == "securemtr_runtime_update_entry"


def test_async_dispatch_runtime_update_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure runtime updates emit the expected dispatcher signal."""

    calls: list[tuple[object, str]] = []

    def _fake_dispatch(hass_obj: object, signal: str) -> None:
        calls.append((hass_obj, signal))

    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatcher_send", _fake_dispatch
    )

    hass = SimpleNamespace()
    async_dispatch_runtime_update(hass, "entry")

    assert calls == [(hass, "securemtr_runtime_update_entry")]


def test_coerce_end_time_invalid_inputs() -> None:
    """Reject invalid end-minute payloads."""

    assert coerce_end_time(None) is None
    assert coerce_end_time(-1) is None
    assert coerce_end_time("oops") is None


def test_build_controller_normalises_metadata() -> None:
    """Verify metadata parsing handles blank serial numbers and names."""

    metadata = {"BOI": "", "SN": "", "N": None, "FV": 2, "MD": "E7+"}
    gateway = BeanbagGateway(
        gateway_id="gateway-1",
        serial_number=None,
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)
    assert controller.identifier == "gateway-1"
    assert controller.name == "E7+ Smart Water Heater Controller"
    assert controller.serial_number is None
    assert controller.firmware_version == "2"
    assert controller.model == "E7+"


def test_build_controller_ignores_numeric_name() -> None:
    """Ensure numeric-only metadata names fall back to the default label."""

    metadata = {"BOI": "", "SN": "E0031158", "N": 2, "FV": None, "MD": None}
    gateway = BeanbagGateway(
        gateway_id="gateway-1",
        serial_number=None,
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)
    assert controller.name == "E7+ Smart Water Heater Controller"
    assert controller.serial_number == "E0031158"


def test_build_controller_skips_none_identifiers() -> None:
    """Ensure metadata values of None do not become literal identifiers."""

    metadata = {"BOI": None, "SN": None, "N": "E7+"}
    gateway = BeanbagGateway(
        gateway_id="gateway-99",
        serial_number=None,
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)
    assert controller.identifier == "gateway-99"
    assert controller.name == "E7+"


def test_build_controller_skips_boolean_identifiers() -> None:
    """Ensure boolean metadata does not produce identifier strings."""

    metadata = {"BOI": True, "SN": False, "N": "Unit"}
    gateway = BeanbagGateway(
        gateway_id="gateway-flag",
        serial_number=None,
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)
    assert controller.identifier == "gateway-flag"
    assert controller.name == "Unit"


def test_entry_display_name_prefers_title() -> None:
    """Ensure the helper surfaces a provided title."""

    entry = SimpleNamespace(title="SecureMTR", entry_id="entry-id")
    assert _entry_display_name(entry) == "SecureMTR"


def test_entry_display_name_falls_back_to_domain() -> None:
    """Ensure the helper provides a generic fallback when metadata is absent."""

    entry = SimpleNamespace()
    assert _entry_display_name(entry) == DOMAIN
