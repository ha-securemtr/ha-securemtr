"""Tests for the securemtr integration setup lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
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
    async_setup_entry,
    async_unload_entry,
    consumption_metrics,
)
from custom_components.securemtr.beanbag import (
    BeanbagError,
    BeanbagGateway,
    BeanbagEnergySample,
    BeanbagSession,
    BeanbagStateSnapshot,
)


@dataclass(slots=True)
class DummyConfigEntry:
    """Provide a lightweight stand-in for Home Assistant config entries."""

    entry_id: str
    data: dict[str, str]
    unique_id: str | None = None
    title: str | None = None


class FakeWebSocket:
    """Represent a simple closable WebSocket stub."""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        """Record the close invocation and mark the socket as closed."""

        self.close_calls += 1
        self.closed = True


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
                {"I": 2, "SI": 16, "V": [{"I": 27, "V": 0}]},
            ]
        }
        return BeanbagStateSnapshot(
            payload=payload,
            primary_power_on=True,
            timed_boost_enabled=False,
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
    assert runtime.primary_power_on is True
    assert runtime.timed_boost_enabled is False
    assert hass.config_entries.forwarded == [("switch", "button")]
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
    assert hass.config_entries.unloaded == [("switch", "button")]


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
    expected = [
        {
            "timestamp": datetime.fromtimestamp(
                1_700_000_000 + offset * 86_400, timezone.utc
            ).isoformat(),
            "epoch_seconds": 1_700_000_000 + offset * 86_400,
            "primary_energy_kwh": 1.0 + offset,
            "boost_energy_kwh": 0.5 * offset,
            "primary_scheduled_minutes": 180 + offset * 10,
            "primary_active_minutes": 120 + offset * 10,
            "boost_scheduled_minutes": offset * 15,
            "boost_active_minutes": offset * 5,
        }
        for offset in range(1, 8)
    ]
    assert runtime.consumption_metrics_log == expected


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
    assert controller.name == "E7+ Water Heater (gateway-1)"
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
    assert controller.name == "E7+ Water Heater (SN: E0031158)"
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
