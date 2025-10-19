"""Tests for the securemtr integration setup lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from itertools import accumulate
from typing import Any, Awaitable, Callable, cast
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.utility_meter.const import (
    CONF_METER_DELTA_VALUES,
    CONF_METER_NET_CONSUMPTION,
    CONF_METER_OFFSET,
    CONF_METER_PERIODICALLY_RESETTING,
    CONF_METER_TYPE,
    CONF_SENSOR_ALWAYS_AVAILABLE,
    CONF_SOURCE_SENSOR,
    CONF_TARIFFS,
    DAILY,
    DOMAIN as UTILITY_METER_DOMAIN,
    WEEKLY,
)

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    ATTR_ENTRY_ID,
    ATTR_ZONE,
    _entry_display_name,
    _async_fetch_controller,
    _build_controller,
    _energy_store_key,
    _load_statistics_options,
    async_setup,
    async_dispatch_runtime_update,
    async_run_with_reconnect,
    coerce_end_time,
    async_setup_entry,
    async_unload_entry,
    ENERGY_STORE_VERSION,
    SERVICE_RESET_ENERGY,
    runtime_update_signal,
    consumption_metrics,
)
import custom_components.securemtr as securemtr_module
from custom_components.securemtr.energy import EnergyAccumulator
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
from custom_components.securemtr.sensor import (
    SecuremtrEnergyTotalSensor,
    async_setup_entry as sensor_async_setup_entry,
)
from custom_components.securemtr.utils import assign_report_day
from homeassistant.util import dt as dt_util
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.entity_registry import RegistryEntryDisabler


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
        self.added: list[ConfigEntry] = []
        self._entries: dict[str, ConfigEntry] = {}
        self._order: list[str] = []

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

    async def async_add(self, entry: ConfigEntry) -> None:
        """Record helper config entries created by the integration."""

        self.added.append(entry)
        self._entries[entry.entry_id] = entry
        if entry.entry_id not in self._order:
            self._order.append(entry.entry_id)

    def async_entries(
        self,
        domain: str | None = None,
        include_ignore: bool = True,
        include_disabled: bool = True,
    ) -> list[ConfigEntry]:
        """Return stored helper entries, optionally filtered by domain."""

        entries = [self._entries[entry_id] for entry_id in self._order]
        if domain is not None:
            entries = [entry for entry in entries if entry.domain == domain]
        return list(entries)


@dataclass(slots=True)
class FakeRegistryEntry:
    """Represent the subset of entity registry data required for tests."""

    entity_id: str
    unique_id: str
    config_entry_id: str
    disabled_by: RegistryEntryDisabler | None = None
    platform: str = "sensor"


class FakeEntityRegistry:
    """Provide a minimal entity registry stub with update tracking."""

    def __init__(self, entries: list[FakeRegistryEntry]) -> None:
        self._entries_by_id: dict[str, FakeRegistryEntry] = {
            entry.entity_id: entry for entry in entries
        }
        self._entities_data = dict(self._entries_by_id)
        self.entities = SimpleNamespace(get_entry=self._entries_by_id.get)
        self.updated: list[tuple[str, dict[str, object]]] = []

    def async_get(self, entity_id: str) -> FakeRegistryEntry | None:
        """Return an entity entry if present."""

        return self._entries_by_id.get(entity_id)

    def async_update_entity(self, entity_id: str, **changes: object) -> None:
        """Apply updates to a stored entity entry."""

        entry = self._entries_by_id[entity_id]
        for key, value in changes.items():
            setattr(entry, key, value)
        self.updated.append((entity_id, dict(changes)))

    def entry(self, entity_id: str) -> FakeRegistryEntry:
        """Return the stored entry, raising if it is missing."""

        return self._entries_by_id[entity_id]


class FakeServiceRegistry:
    """Provide a minimal async service registry for FakeHass."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], tuple[Callable[..., Any], Any]] = {}

    def async_register(
        self,
        domain: str,
        service: str,
        handler: Callable[..., Any],
        schema: Any | None = None,
    ) -> None:
        """Store service handlers for later invocation."""

        self.handlers[(domain, service)] = (handler, schema)

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> None:
        """Invoke the registered handler with the provided payload."""

        key = (domain, service)
        if key not in self.handlers:
            raise AssertionError(f"Service {domain}.{service} was not registered")

        handler, schema = self.handlers[key]
        payload = data or {}
        if schema is not None:
            payload = schema(payload)
        call = SimpleNamespace(data=payload)
        result = handler(call)
        if asyncio.iscoroutine(result):
            await result


class FakeHass:
    """Emulate the subset of Home Assistant APIs used by the integration."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, SecuremtrRuntimeData]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self.config_entries = FakeConfigEntries()
        self.config = SimpleNamespace(time_zone=DEFAULT_TIMEZONE)
        self.services = FakeServiceRegistry()

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
    assert runtime.energy_store is store_instances[0]
    assert hass.config_entries.forwarded == [
        ("switch",),
        ("button", "binary_sensor", "sensor"),
    ]
    assert callbacks
    assert [callback_info[1:] for callback_info in callbacks] == [
        (1, 0, 0),
        (13, 0, 0),
    ]
    assert fake_metrics.await_args_list == [call(hass, entry)]

    callback = callbacks[0][0]
    callback(datetime.now(timezone.utc))
    await hass.async_block_till_done()
    assert fake_metrics.await_args_list == [call(hass, entry), call(hass, entry)]

    midday_callback = callbacks[1][0]
    midday_callback(datetime.now(timezone.utc))
    await hass.async_block_till_done()
    assert fake_metrics.await_args_list == [
        call(hass, entry),
        call(hass, entry),
        call(hass, entry),
    ]

    meters = hass.config_entries.async_entries(UTILITY_METER_DOMAIN)
    assert len(meters) == 4
    assert {
        meter.unique_id for meter in meters
    } == {
        "securemtr_1_primary_daily_utility_meter",
        "securemtr_1_primary_weekly_utility_meter",
        "securemtr_1_boost_daily_utility_meter",
        "securemtr_1_boost_weekly_utility_meter",
    }

    expected_sources = {
        "sensor.securemtr_primary_energy_kwh",
        "sensor.securemtr_boost_energy_kwh",
    }

    for meter in meters:
        assert meter.options[CONF_SOURCE_SENSOR] in expected_sources
        assert meter.options[CONF_METER_TYPE] in (DAILY, WEEKLY)
        assert meter.options[CONF_METER_OFFSET] == 0
        assert meter.options[CONF_TARIFFS] == []
        assert meter.options[CONF_METER_NET_CONSUMPTION] is False
        assert meter.options[CONF_METER_DELTA_VALUES] is False
        assert meter.options[CONF_METER_PERIODICALLY_RESETTING] is True
        assert meter.options[CONF_SENSOR_ALWAYS_AVAILABLE] is False


@pytest.mark.asyncio
async def test_consumption_scheduler_fires_with_frozen_clock(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
) -> None:
    """Ensure the twice-daily scheduler and immediate refresh run deterministically."""

    hass = FakeHass()
    callbacks = track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="scheduler",
        data={"email": "user@example.com", "password": "digest"},
        unique_id="user@example.com",
    )

    fake_metrics = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr.consumption_metrics", fake_metrics
    )
    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: object(),
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: SimpleNamespace(),
    )
    start_backend = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr._async_start_backend", start_backend
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_ensure_utility_meters",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_retire_legacy_entities",
        AsyncMock(),
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    assert fake_metrics.await_args_list == [call(hass, entry)]
    assert start_backend.await_count == 1
    assert len(callbacks) == 2
    assert [callback_info[1:] for callback_info in callbacks] == [
        (1, 0, 0),
        (13, 0, 0),
    ]

    morning = datetime(2024, 8, 20, 1, 0, tzinfo=timezone.utc)
    callbacks[0][0](morning)
    await hass.async_block_till_done()

    midday = datetime(2024, 8, 20, 13, 0, tzinfo=timezone.utc)
    callbacks[1][0](midday)
    await hass.async_block_till_done()

    assert fake_metrics.await_args_list == [
        call(hass, entry),
        call(hass, entry),
        call(hass, entry),
    ]

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.consumption_schedule_unsub is not None
    runtime.consumption_schedule_unsub()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "callback_index"),
    (
        ("spring_morning", 0),
        ("autumn_first_morning", 0),
        ("autumn_second_morning", 0),
        ("spring_midday", 1),
        ("autumn_midday", 1),
    ),
)
async def test_consumption_scheduler_handles_dst_transitions(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
    scenario: str,
    callback_index: int,
) -> None:
    """Ensure the scheduler fires at local hours around DST boundaries."""

    hass = FakeHass()
    hass.config.time_zone = "Europe/London"
    london = ZoneInfo("Europe/London")
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", london, raising=False)
    callbacks = track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id=f"scheduler_dst_{scenario}",
        data={"email": "user@example.com", "password": "digest"},
        unique_id="user@example.com",
    )

    fake_metrics = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr.consumption_metrics", fake_metrics
    )
    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: object(),
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: SimpleNamespace(),
    )
    start_backend = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr._async_start_backend", start_backend
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_ensure_utility_meters",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_retire_legacy_entities",
        AsyncMock(),
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    assert len(callbacks) == 2
    callback = callbacks[callback_index]
    expected_hour = 1 if callback_index == 0 else 13
    assert callback[1:] == (expected_hour, 0, 0)

    assert fake_metrics.await_args_list == [call(hass, entry)]

    if scenario == "spring_morning":
        event = datetime(2024, 3, 31, 1, 0, tzinfo=london)
    elif scenario == "autumn_first_morning":
        event = datetime(2024, 10, 27, 1, 0, tzinfo=london, fold=0)
    elif scenario == "autumn_second_morning":
        event = datetime(2024, 10, 27, 1, 0, tzinfo=london, fold=1)
    elif scenario == "spring_midday":
        event = datetime(2024, 3, 31, 13, 0, tzinfo=london)
    else:
        event = datetime(2024, 10, 27, 13, 0, tzinfo=london)

    callback[0](event)
    await hass.async_block_till_done()

    assert fake_metrics.await_args_list == [call(hass, entry), call(hass, entry)]

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.consumption_schedule_unsub is not None
    runtime.consumption_schedule_unsub()

@pytest.mark.asyncio
async def test_async_setup_entry_disables_legacy_energy_entities(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ensure legacy totals are disabled and guidance is logged during setup."""

    caplog.set_level(logging.INFO)
    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="legacy", data={"email": "user@example.com", "password": "digest"}
    )

    registry = FakeEntityRegistry(
        [
            FakeRegistryEntry(
                entity_id="sensor.primary_energy_total",
                unique_id="serial_1_primary_energy_total",
                config_entry_id=entry.entry_id,
            ),
            FakeRegistryEntry(
                entity_id="sensor.securemtr_primary_energy_kwh",
                unique_id="serial_1_primary_energy_total",
                config_entry_id=entry.entry_id,
            ),
            FakeRegistryEntry(
                entity_id="sensor.other_primary_energy_total",
                unique_id="serial_2_primary_energy_total",
                config_entry_id="other-entry",
            ),
        ]
    )

    monkeypatch.setattr(
        "custom_components.securemtr.er.async_get", lambda hass_obj: registry
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_start_backend",
        lambda entry_obj, runtime: asyncio.sleep(0),
    )
    fake_metrics = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr.consumption_metrics", fake_metrics
    )
    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession", lambda hass_obj: object()
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend", lambda session: SimpleNamespace()
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_ensure_utility_meters",
        AsyncMock(),
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    legacy_entry = registry.entry("sensor.primary_energy_total")
    assert legacy_entry.disabled_by is RegistryEntryDisabler.INTEGRATION
    assert (
        registry.entry("sensor.securemtr_primary_energy_kwh").disabled_by is None
    )
    assert any(
        "Disabled legacy SecureMTR energy entity sensor.primary_energy_total" in message
        for message in caplog.messages
    )


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
    initial_history_count = len(backend.energy_history_calls)
    initial_program_calls = len(backend.program_calls)
    initial_dispatch_count = len(dispatch_calls)

    await consumption_metrics(hass, entry)

    assert len(backend.login_calls) == initial_logins + 1
    assert backend.energy_history_calls[initial_history_count:] == [
        ("gateway-1", 1)
    ]
    assert backend.program_calls[initial_program_calls:] == [
        ("primary", "gateway-1"),
        ("boost", "gateway-1"),
    ]
    assert len(dispatch_calls) == initial_dispatch_count + 1
    assert dispatch_calls[-1] == (hass, entry.entry_id)

    tz = ZoneInfo("Europe/London")
    base_timestamp = 1_700_000_000
    offsets = range(1, 8)
    expected_log = []
    for offset in offsets:
        epoch = base_timestamp + offset * 86_400
        sample_dt = datetime.fromtimestamp(epoch, timezone.utc)
        report_day = assign_report_day(sample_dt, tz)
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

    fallback_power = 2.85
    primary_energy = [
        (120 + offset * 10) / 60 * fallback_power for offset in offsets
    ]
    primary_cumulative = list(accumulate(primary_energy))
    boost_energy = [(offset * 5) / 60 * fallback_power for offset in offsets]
    boost_cumulative = list(accumulate(boost_energy))

    assert store_instances and store_instances[0].saved
    persisted = store_instances[0].saved[-1]
    expected_last_day = assign_report_day(
        datetime.fromtimestamp(base_timestamp + offsets[-1] * 86_400, timezone.utc),
        tz,
    ).isoformat()
    expected_first_day = assign_report_day(
        datetime.fromtimestamp(base_timestamp + offsets[0] * 86_400, timezone.utc),
        tz,
    ).isoformat()

    assert persisted["primary"]["last_processed_day"] == expected_last_day
    assert persisted["primary"]["series_start"] == expected_first_day
    assert persisted["primary"]["cumulative_kwh"] == pytest.approx(
        primary_cumulative[-1]
    )
    assert len(persisted["primary"]["ledger"]) == len(primary_energy)
    assert persisted["boost"]["last_processed_day"] == expected_last_day
    assert persisted["boost"]["series_start"] == expected_first_day
    assert persisted["boost"]["cumulative_kwh"] == pytest.approx(
        boost_cumulative[-1]
    )
    assert len(persisted["boost"]["ledger"]) == len(boost_energy)

    energy_state = runtime.energy_state
    assert energy_state is not None
    assert energy_state["primary"]["energy_sum"] == pytest.approx(
        primary_cumulative[-1]
    )
    assert energy_state["primary"]["last_day"] == expected_last_day
    assert energy_state["primary"]["series_start"] == expected_first_day
    assert energy_state["boost"]["energy_sum"] == pytest.approx(
        boost_cumulative[-1]
    )
    assert energy_state["boost"]["last_day"] == expected_last_day
    assert energy_state["boost"]["series_start"] == expected_first_day

    primary_runtime = [(120 + offset * 10) / 60 for offset in offsets]
    primary_scheduled = [(180 + offset * 10) / 60 for offset in offsets]
    boost_runtime = [(offset * 5) / 60 for offset in offsets]
    boost_scheduled = [(offset * 15) / 60 for offset in offsets]

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


@pytest.mark.asyncio
async def test_consumption_metrics_skips_processed_days(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
) -> None:
    """Ensure repeated refreshes avoid duplicating cumulative totals."""

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
    runtime.energy_accumulator = None
    runtime.energy_state = None
    backend.energy_history_calls.clear()
    store_instances[0].data = None
    store_instances[0].saved.clear()
    initial_history_count = len(backend.energy_history_calls)
    initial_save_count = len(store_instances[0].saved)

    await consumption_metrics(hass, entry)
    first_history_count = len(backend.energy_history_calls)
    assert first_history_count == initial_history_count + 1
    assert len(store_instances[0].saved) > initial_save_count
    first_save_count = len(store_instances[0].saved)
    persisted = store_instances[0].saved[-1]

    await consumption_metrics(hass, entry)

    assert len(store_instances[0].saved) == first_save_count
    assert len(backend.energy_history_calls) == first_history_count + 1
    energy_state = runtime.energy_state
    assert energy_state is not None
    assert energy_state["primary"]["energy_sum"] == pytest.approx(
        persisted["primary"]["cumulative_kwh"]
    )
    assert energy_state["boost"]["energy_sum"] == pytest.approx(
        persisted["boost"]["cumulative_kwh"]
    )


@pytest.mark.asyncio
async def test_consumption_metrics_processes_only_new_days(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ensure only unprocessed days adjust the cumulative energy totals."""

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

    forbidden_module = ModuleType("homeassistant.components.recorder.statistics")

    def _forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("External statistics writes must not be invoked")

    forbidden_module.async_add_external_statistics = _forbidden  # type: ignore[attr-defined]
    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.statistics = forbidden_module  # type: ignore[attr-defined]
    parent_components = sys.modules.get("homeassistant.components")
    if parent_components is None:
        parent_components = ModuleType("homeassistant.components")
        monkeypatch.setitem(sys.modules, "homeassistant.components", parent_components)
    monkeypatch.setattr(parent_components, "recorder", recorder_module, raising=False)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder_module)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.statistics",
        forbidden_module,
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
    runtime.energy_accumulator = None
    backend.energy_history_calls.clear()
    caplog.clear()

    tz = ZoneInfo("Europe/London")
    base_timestamp = 1_700_000_000
    initial_days = [
        assign_report_day(
            datetime.fromtimestamp(base_timestamp + offset * 86_400, timezone.utc), tz
        )
        for offset in range(1, 5)
    ]

    fallback_power = 2.85
    primary_energy = [
        (120 + offset * 10) / 60 * fallback_power for offset in range(1, 8)
    ]
    boost_energy = [(offset * 5) / 60 * fallback_power for offset in range(1, 8)]

    store_instances[0].data = {
        "primary": {
            "days": {
                day.isoformat(): primary_energy[index]
                for index, day in enumerate(initial_days)
            },
            "cumulative_kwh": sum(primary_energy[: len(initial_days)]),
            "last_day": initial_days[-1].isoformat(),
            "series_start": initial_days[0].isoformat(),
        },
        "boost": {
            "days": {
                day.isoformat(): boost_energy[index]
                for index, day in enumerate(initial_days)
            },
            "cumulative_kwh": sum(boost_energy[: len(initial_days)]),
            "last_day": initial_days[-1].isoformat(),
            "series_start": initial_days[0].isoformat(),
        },
    }
    runtime.energy_state = None

    with caplog.at_level(logging.INFO):
        await consumption_metrics(hass, entry)

    assert store_instances[0].saved
    saved_state = store_instances[0].saved[-1]
    expected_days = [
        assign_report_day(
            datetime.fromtimestamp(base_timestamp + offset * 86_400, timezone.utc), tz
        )
        for offset in range(1, 8)
    ]
    assert set(saved_state["primary"]["ledger"]) == {
        day.isoformat() for day in expected_days
    }
    assert saved_state["primary"]["last_processed_day"] == expected_days[-1].isoformat()
    assert saved_state["boost"]["last_processed_day"] == expected_days[-1].isoformat()
    assert saved_state["primary"]["cumulative_kwh"] > sum(primary_energy[:4])
    assert saved_state["boost"]["cumulative_kwh"] > sum(boost_energy[:4])

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Updated cumulative energy state" in message for message in messages
    ), messages
    assert any(
        "SecureMTR" in message and "energy on" in message for message in messages
    ), messages


@pytest.mark.asyncio
async def test_energy_dashboard_flow_validates_sensor_states(
    monkeypatch: pytest.MonkeyPatch,
    track_time_spy,
    store_instances,
) -> None:
    """Simulate the QA energy workflow and confirm Energy Dashboard readiness."""

    hass = FakeHass()
    track_time_spy(hass)
    entry = DummyConfigEntry(
        entry_id="qa-flow",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )
    entry.hass = hass

    fake_session = object()
    tz = ZoneInfo("Europe/London")
    base_timestamp = int(datetime(2024, 3, 1, 12, tzinfo=timezone.utc).timestamp())
    fallback_power_kw = 2.85

    def _build_sample(index: int) -> BeanbagEnergySample:
        day = index + 1
        return BeanbagEnergySample(
            timestamp=base_timestamp + index * 86_400,
            primary_energy_kwh=0.0,
            boost_energy_kwh=0.0,
            primary_scheduled_minutes=day * 45,
            primary_active_minutes=day * 30,
            boost_scheduled_minutes=day * 20,
            boost_active_minutes=day * 15,
        )

    all_samples = [_build_sample(index) for index in range(7)]
    batches = [all_samples[:2], all_samples[:4], all_samples]

    class BatchedBackend(FakeBeanbagBackend):
        """Return deterministic batches of energy history samples."""

        def __init__(
            self, session: object, payloads: list[list[BeanbagEnergySample]]
        ) -> None:
            super().__init__(session)
            self._payloads = payloads
            self._index = 0

        async def read_energy_history(
            self,
            session: BeanbagSession,
            websocket: FakeWebSocket,
            gateway_id: str,
            *,
            window_index: int = 1,
        ) -> list[BeanbagEnergySample]:
            """Return the next batch of energy samples for the controller."""

            payload = self._payloads[min(self._index, len(self._payloads) - 1)]
            self._index += 1
            self.energy_history_calls.append((gateway_id, window_index))
            return list(payload)

    backend = BatchedBackend(fake_session, batches)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    dispatch_calls: list[tuple[object, str]] = []

    def _capture_dispatch(hass_obj: object, signal: str) -> None:
        dispatch_calls.append((hass_obj, signal))

    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatcher_send", _capture_dispatch
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    sensors: list[Any] = []
    await sensor_async_setup_entry(hass, entry, sensors.extend)

    energy_entities = {
        entity.entity_id: entity
        for entity in sensors
        if isinstance(entity, SecuremtrEnergyTotalSensor)
    }

    assert set(energy_entities) == {
        "sensor.securemtr_primary_energy_kwh",
        "sensor.securemtr_boost_energy_kwh",
    }

    primary_sensor = energy_entities["sensor.securemtr_primary_energy_kwh"]
    boost_sensor = energy_entities["sensor.securemtr_boost_energy_kwh"]

    assert primary_sensor.device_class is SensorDeviceClass.ENERGY
    assert primary_sensor.state_class is SensorStateClass.TOTAL_INCREASING
    assert (
        primary_sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    )
    assert boost_sensor.device_class is SensorDeviceClass.ENERGY

    first_day_iso = assign_report_day(
        datetime.fromtimestamp(all_samples[0].timestamp, timezone.utc), tz
    ).isoformat()

    runtime = hass.data[DOMAIN][entry.entry_id]
    energy_state = runtime.energy_state
    assert isinstance(energy_state, dict)

    initial_batch = batches[0]
    initial_last_sample = initial_batch[-1]
    initial_last_day_iso = assign_report_day(
        datetime.fromtimestamp(initial_last_sample.timestamp, timezone.utc), tz
    ).isoformat()
    initial_primary = sum(
        (sample.primary_active_minutes / 60) * fallback_power_kw
        for sample in initial_batch
    )
    initial_boost = sum(
        (sample.boost_active_minutes / 60) * fallback_power_kw
        for sample in initial_batch
    )

    primary_state = energy_state["primary"]
    boost_state = energy_state["boost"]
    assert primary_state["energy_sum"] == pytest.approx(initial_primary)
    assert boost_state["energy_sum"] == pytest.approx(initial_boost)
    assert primary_state["last_day"] == initial_last_day_iso
    assert boost_state["last_day"] == initial_last_day_iso
    assert primary_state["series_start"] == first_day_iso
    assert boost_state["series_start"] == first_day_iso
    assert primary_sensor.native_value == pytest.approx(initial_primary)
    assert boost_sensor.native_value == pytest.approx(initial_boost)
    assert primary_sensor.extra_state_attributes == {
        "last_report_day": initial_last_day_iso,
        "series_start_day": first_day_iso,
        "offset_kwh": 0.0,
    }
    assert boost_sensor.extra_state_attributes == {
        "last_report_day": initial_last_day_iso,
        "series_start_day": first_day_iso,
        "offset_kwh": 0.0,
    }

    initial_dispatch_count = len(dispatch_calls)
    initial_history_count = len(backend.energy_history_calls)

    for step, batch in enumerate(batches[1:], start=1):
        await consumption_metrics(hass, entry)

        energy_state = runtime.energy_state
        assert isinstance(energy_state, dict)

        last_sample = batch[-1]
        last_day_iso = assign_report_day(
            datetime.fromtimestamp(last_sample.timestamp, timezone.utc), tz
        ).isoformat()

        expected_primary = sum(
            (sample.primary_active_minutes / 60) * fallback_power_kw
            for sample in batch
        )
        expected_boost = sum(
            (sample.boost_active_minutes / 60) * fallback_power_kw
            for sample in batch
        )

        primary_state = energy_state["primary"]
        boost_state = energy_state["boost"]

        assert primary_state["energy_sum"] == pytest.approx(expected_primary)
        assert boost_state["energy_sum"] == pytest.approx(expected_boost)
        assert primary_state["last_day"] == last_day_iso
        assert boost_state["last_day"] == last_day_iso
        assert primary_state["series_start"] == first_day_iso
        assert boost_state["series_start"] == first_day_iso

        assert primary_sensor.native_value == pytest.approx(expected_primary)
        assert boost_sensor.native_value == pytest.approx(expected_boost)

        primary_attrs = primary_sensor.extra_state_attributes
        boost_attrs = boost_sensor.extra_state_attributes
        assert primary_attrs == {
            "last_report_day": last_day_iso,
            "series_start_day": first_day_iso,
            "offset_kwh": 0.0,
        }
        assert boost_attrs == {
            "last_report_day": last_day_iso,
            "series_start_day": first_day_iso,
            "offset_kwh": 0.0,
        }

        persisted = store_instances[0].saved[-1]
        assert persisted["primary"]["cumulative_kwh"] == pytest.approx(
            expected_primary
        )
        assert persisted["boost"]["cumulative_kwh"] == pytest.approx(expected_boost)
        assert len(persisted["primary"]["ledger"]) == len(batch)
        assert len(persisted["boost"]["ledger"]) == len(batch)
        assert persisted["primary"]["last_processed_day"] == last_day_iso
        assert persisted["boost"]["last_processed_day"] == last_day_iso

        recent = runtime.statistics_recent
        assert isinstance(recent, dict)
        assert recent["primary"]["report_day"] == last_day_iso
        assert recent["primary"]["energy_sum"] == pytest.approx(expected_primary)
        assert recent["boost"]["report_day"] == last_day_iso
        assert recent["boost"]["energy_sum"] == pytest.approx(expected_boost)

        expected_signal = runtime_update_signal(entry.entry_id)
        assert dispatch_calls[-1] == (hass, expected_signal)
        assert len(dispatch_calls) == initial_dispatch_count + step
        assert len(backend.energy_history_calls) == initial_history_count + step

    assert backend.energy_history_calls == [
        ("gateway-1", 1),
        ("gateway-1", 1),
        ("gateway-1", 1),
    ]


@pytest.mark.asyncio
async def test_reset_service_resets_zone(store_instances) -> None:
    """Ensure the reset service clears stored state for the requested zone."""

    hass = FakeHass()
    await async_setup(hass, {})

    entry = DummyConfigEntry(
        entry_id="reset-entry",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "digest"},
    )

    runtime = SecuremtrRuntimeData(backend=FakeBeanbagBackend(object()))
    store = securemtr_module.Store(hass, ENERGY_STORE_VERSION, _energy_store_key(entry))
    runtime.energy_store = store
    accumulator = EnergyAccumulator(store=cast(Any, store))
    runtime.energy_accumulator = accumulator
    await accumulator.async_load()

    await accumulator.async_add_day("boost", date(2024, 8, 1), 3.5)
    runtime.energy_state = accumulator.as_sensor_state()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_ENERGY,
        {ATTR_ENTRY_ID: entry.entry_id, ATTR_ZONE: "boost"},
    )

    state = accumulator.as_sensor_state()["boost"]
    assert state["energy_sum"] == 0.0
    assert state["last_day"] is None
    assert runtime.energy_state["boost"]["energy_sum"] == 0.0

    persisted = store_instances[0].saved[-1]
    assert persisted["boost"]["ledger"] == {}
    assert persisted["boost"]["cumulative_kwh"] == 0.0


def test_load_statistics_options_prefers_hass_timezone() -> None:
    """Ensure statistics options honour the Home Assistant timezone."""

    hass = FakeHass()
    hass.config.time_zone = "Europe/London"
    entry = DummyConfigEntry(entry_id="tz-pref", data={}, options={})
    entry.hass = hass

    options = _load_statistics_options(entry)

    assert options.timezone_name == "Europe/London"
    assert isinstance(options.timezone, ZoneInfo)


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
    assert isinstance(options.timezone, ZoneInfo)


def test_load_statistics_options_missing_system_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure missing system time zone data falls back to the default time zone."""

    hass = FakeHass()
    hass.config.time_zone = "Mars/Olympus"
    entry = DummyConfigEntry(entry_id="tz-missing", data={}, options={})
    entry.hass = hass

    original_zoneinfo = ZoneInfo

    def _failing_zoneinfo(name: str) -> ZoneInfo:
        if name != "UTC":
            raise ZoneInfoNotFoundError(name)
        return original_zoneinfo("UTC")

    monkeypatch.setattr("custom_components.securemtr.ZoneInfo", _failing_zoneinfo)
    monkeypatch.setattr(
        "custom_components.securemtr.dt_util.get_default_time_zone",
        lambda: timezone.utc,
    )

    options = _load_statistics_options(entry)

    assert options.timezone_name == "UTC"
    assert isinstance(options.timezone, ZoneInfo)


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


def test_build_controller_maps_numeric_model() -> None:
    """Map numeric metadata model codes to friendly names."""

    metadata = {"BOI": "controller", "SN": "serial", "N": "Unit", "MD": 2}
    gateway = BeanbagGateway(
        gateway_id="gateway-1",
        serial_number=None,
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)
    assert controller.model == "E7+ Smart Water Heater Controller"


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
