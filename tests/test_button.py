"""Tests for the Secure Meters button platform."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest

import custom_components.securemtr.runtime_helpers as runtime_helpers
from custom_components.securemtr import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    coerce_end_time,
)
from custom_components.securemtr.beanbag import BeanbagError, DailyProgram
from custom_components.securemtr.button import (
    BOOST_BUTTON_METADATA,
    SecuremtrCancelBoostButton,
    SecuremtrConsumptionMetricsButton,
    SecuremtrLogWeeklyScheduleButton,
    SecuremtrTimedBoostButton,
    async_setup_entry,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from tests.helpers import create_config_entry


class DummyBackend:
    """Capture timed boost commands issued by button entities."""

    def __init__(self) -> None:
        self.start_calls: list[tuple[Any, Any, str, int]] = []
        self.stop_calls: list[tuple[Any, Any, str]] = []
        self.read_calls: list[str] = []
        self.weekly_programs: dict[str, tuple[DailyProgram, ...]] = {}
        self.read_error: Exception | None = None

    async def start_timed_boost(
        self,
        session: Any,
        websocket: Any,
        gateway_id: str,
        *,
        duration_minutes: int,
    ) -> None:
        """Record a start command."""

        self.start_calls.append((session, websocket, gateway_id, duration_minutes))

    async def stop_timed_boost(
        self, session: Any, websocket: Any, gateway_id: str
    ) -> None:
        """Record a stop command."""

        self.stop_calls.append((session, websocket, gateway_id))

    async def read_weekly_program(
        self,
        session: Any,
        websocket: Any,
        gateway_id: str,
        *,
        zone: str,
    ) -> tuple[DailyProgram, ...]:
        """Return the stored weekly program for the requested zone."""

        self.read_calls.append(zone)
        if self.read_error is not None:
            raise self.read_error

        program = self.weekly_programs.get(zone)
        if program is None:
            raise AssertionError(f"No weekly program configured for zone {zone}")
        return program


def _create_runtime() -> tuple[SecuremtrRuntimeData, DummyBackend]:
    """Construct a runtime data object with a ready controller."""

    backend = DummyBackend()
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = SimpleNamespace()
    runtime.websocket = SimpleNamespace()
    runtime.controller = SecuremtrController(
        identifier="controller-1",
        name="E7+ Smart Water Heater Controller",
        gateway_id="gateway-1",
        serial_number="serial-1",
        firmware_version="1.0.0",
        model="E7+",
    )
    runtime.timed_boost_enabled = True
    runtime.controller_ready.set()
    return runtime, backend


@pytest.fixture(autouse=True)
def patch_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[
    [ConfigEntry, SecuremtrRuntimeData, Callable[[Any, Any, Any], Awaitable[Any]]],
    Awaitable[Any],
]:
    """Stub the reconnect helper to operate on the dummy runtime."""

    async def _fake_run_with_reconnect(
        entry: ConfigEntry,
        runtime: SecuremtrRuntimeData,
        operation: Callable[[Any, Any, Any], Awaitable[Any]],
    ) -> Any:
        if runtime.session is None or runtime.websocket is None:
            raise BeanbagError("no connection")
        return await operation(runtime.backend, runtime.session, runtime.websocket)

    monkeypatch.setattr(
        "custom_components.securemtr.async_run_with_reconnect",
        _fake_run_with_reconnect,
    )
    return _fake_run_with_reconnect


@pytest.mark.asyncio
async def test_button_setup_creates_entities() -> None:
    """Ensure the button platform exposes the configured commands."""

    runtime, _backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    def _add_entities(new: list[ButtonEntity]) -> None:
        entities.extend(new)

    await async_setup_entry(hass, entry, _add_entities)

    expected_ids = {
        f"serial_1_boost_{metadata.duration_minutes}" for metadata in BOOST_BUTTON_METADATA
    }
    expected_ids.update(
        {
            "serial_1_boost_cancel",
            "serial_1_refresh_consumption",
            "serial_1_log_schedule",
        }
    )
    assert {entity.unique_id for entity in entities} == expected_ids

    cancel_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_cancel")
    )
    assert isinstance(cancel_button, SecuremtrCancelBoostButton)
    assert cancel_button.available is False

    metrics_button = next(
        entity
        for entity in entities
        if entity.unique_id.endswith("refresh_consumption")
    )
    assert isinstance(metrics_button, SecuremtrConsumptionMetricsButton)

    schedule_button = next(
        entity for entity in entities if entity.unique_id.endswith("log_schedule")
    )
    assert isinstance(schedule_button, SecuremtrLogWeeklyScheduleButton)

    boost_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_60")
    )
    assert boost_button.device_info["name"] == "E7+ Smart Water Heater Controller"
    metadata_60 = next(
        metadata for metadata in BOOST_BUTTON_METADATA if metadata.duration_minutes == 60
    )
    assert boost_button.translation_key == metadata_60.translation_key
    assert boost_button.has_entity_name is True


@pytest.mark.asyncio
async def test_boost_button_triggers_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify pressing a boost button calls the backend with the correct duration."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    boost_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_30")
    )
    assert isinstance(boost_button, SecuremtrTimedBoostButton)

    fixed_now = datetime(2024, 1, 1, 10, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("homeassistant.util.dt.now", lambda: fixed_now)
    dispatcher_calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        "custom_components.securemtr.runtime_helpers.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: dispatcher_calls.append((hass_obj, entry_id)),
    )

    helper_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original_helper = runtime_helpers.async_mutate_runtime

    async def _wrapped_helper(*args: Any, **kwargs: Any) -> Any:
        helper_calls.append((args, kwargs))
        return await original_helper(*args, **kwargs)

    monkeypatch.setattr(
        "custom_components.securemtr.entity.async_mutate_runtime",
        _wrapped_helper,
    )

    boost_button.hass = SimpleNamespace()
    hass_obj = boost_button.hass
    await boost_button.async_press()

    assert backend.start_calls == [
        (runtime.session, runtime.websocket, "gateway-1", 30)
    ]
    assert runtime.timed_boost_active is True
    assert runtime.timed_boost_end_minute == (10 * 60 + 45)
    assert runtime.timed_boost_end_time == coerce_end_time(10 * 60 + 45)
    assert dispatcher_calls == [(hass_obj, "entry")]
    assert (
        helper_calls[0][1]["log_context"] == "Failed to start Secure Meters timed boost"
    )
    assert helper_calls[0][1]["write_ha_state"] is None

    boost_button.hass = None
    await boost_button.async_press()
    assert backend.start_calls[-1] == (
        runtime.session,
        runtime.websocket,
        "gateway-1",
        30,
    )
    assert dispatcher_calls == [(hass_obj, "entry")]
    assert len(helper_calls) == 2


@pytest.mark.asyncio
async def test_boost_button_helper_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Surface helper failures as Home Assistant errors for boost buttons."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    boost_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_30")
    )

    recorded_kwargs: dict[str, Any] = {}

    async def _failing_helper(*args: Any, **kwargs: Any) -> Any:
        recorded_kwargs.update(kwargs)
        raise HomeAssistantError("boom")

    monkeypatch.setattr(
        "custom_components.securemtr.entity.async_mutate_runtime",
        _failing_helper,
    )

    with pytest.raises(HomeAssistantError, match="boom"):
        await boost_button.async_press()

    assert recorded_kwargs["log_context"] == "Failed to start Secure Meters timed boost"
    assert recorded_kwargs["write_ha_state"] is None
    assert backend.start_calls == []


@pytest.mark.asyncio
async def test_boost_button_requires_connection() -> None:
    """Ensure a missing runtime connection raises an error."""

    runtime, backend = _create_runtime()
    runtime.session = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    boost_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_30")
    )

    with pytest.raises(HomeAssistantError):
        await boost_button.async_press()

    assert backend.start_calls == []


@pytest.mark.asyncio
async def test_boost_button_requires_controller() -> None:
    """Ensure the timed boost button validates controller availability."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    boost_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_30")
    )

    runtime.controller = None

    with pytest.raises(HomeAssistantError):
        await boost_button.async_press()

    assert backend.start_calls == []


@pytest.mark.asyncio
async def test_custom_duration_boost_button_uses_placeholders() -> None:
    """Ensure custom boost durations expose the translation placeholder."""

    runtime, _backend = _create_runtime()
    entry = create_config_entry(entry_id="entry")
    button = SecuremtrTimedBoostButton(runtime, runtime.controller, entry, 45)

    assert button.translation_key == "boost_custom_minutes"
    assert button.translation_placeholders == {"duration": "45"}
    assert button.has_entity_name is True


@pytest.mark.asyncio
async def test_schedule_button_logs_program(caplog: pytest.LogCaptureFixture) -> None:
    """Log both weekly programs when the schedule button is pressed."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    weekday = DailyProgram((60, None, None), (120, None, None))
    weekend = DailyProgram((480, 1020, None), (540, 1320, None))
    backend.weekly_programs = {
        "primary": (weekday,) * 5 + (weekend,) * 2,
        "boost": (weekend,) * 7,
    }

    await async_setup_entry(hass, entry, entities.extend)

    schedule_button = next(
        entity for entity in entities if entity.unique_id.endswith("log_schedule")
    )
    assert isinstance(schedule_button, SecuremtrLogWeeklyScheduleButton)

    schedule_button.hass = SimpleNamespace()
    with caplog.at_level(logging.INFO):
        await schedule_button.async_press()

    assert backend.read_calls == ["primary", "boost"]
    assert any("primary zone" in record for record in caplog.messages)
    assert any("boost zone" in record for record in caplog.messages)
    assert any("Monday" in record and "01:00" in record for record in caplog.messages)
    assert any("Saturday" in record and "08:00" in record for record in caplog.messages)


@pytest.mark.asyncio
async def test_schedule_button_backend_error(caplog: pytest.LogCaptureFixture) -> None:
    """Convert backend read failures into Home Assistant errors."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    backend.weekly_programs = {}
    backend.read_error = BeanbagError("boom")

    await async_setup_entry(hass, entry, entities.extend)
    schedule_button = next(
        entity for entity in entities if entity.unique_id.endswith("log_schedule")
    )

    schedule_button.hass = SimpleNamespace()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HomeAssistantError):
            await schedule_button.async_press()

    assert backend.read_calls == ["primary", "boost"]
    assert any(
        "Failed to read Secure Meters weekly schedule" in record
        for record in caplog.messages
    )


@pytest.mark.asyncio
async def test_schedule_button_requires_connection() -> None:
    """Ensure the schedule button validates the runtime connection."""

    runtime, backend = _create_runtime()
    runtime.session = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    backend.weekly_programs = {
        "primary": (DailyProgram((None, None, None), (None, None, None)),) * 7,
        "boost": (DailyProgram((None, None, None), (None, None, None)),) * 7,
    }

    await async_setup_entry(hass, entry, entities.extend)

    schedule_button = next(
        entity for entity in entities if entity.unique_id.endswith("log_schedule")
    )

    with pytest.raises(HomeAssistantError):
        await schedule_button.async_press()

    assert backend.read_calls == []


@pytest.mark.asyncio
async def test_schedule_button_requires_controller() -> None:
    """Ensure the schedule button verifies controller availability."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    backend.weekly_programs = {
        "primary": (DailyProgram((None, None, None), (None, None, None)),) * 7,
        "boost": (DailyProgram((None, None, None), (None, None, None)),) * 7,
    }

    await async_setup_entry(hass, entry, entities.extend)

    schedule_button = next(
        entity for entity in entities if entity.unique_id.endswith("log_schedule")
    )

    runtime.controller = None

    with pytest.raises(HomeAssistantError):
        await schedule_button.async_press()

    assert backend.read_calls == []


@pytest.mark.asyncio
async def test_boost_button_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Convert backend failures into Home Assistant errors."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    boost_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_30")
    )

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise BeanbagError("boom")

    monkeypatch.setattr(backend, "start_timed_boost", _raise)

    with pytest.raises(HomeAssistantError):
        await boost_button.async_press()

    assert runtime.timed_boost_active is not True


@pytest.mark.asyncio
async def test_cancel_button_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the cancel button reports availability and stops boosts."""

    runtime, backend = _create_runtime()
    runtime.timed_boost_active = True
    runtime.timed_boost_end_minute = 615
    runtime.timed_boost_end_time = coerce_end_time(615)
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    cancel_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_cancel")
    )
    assert isinstance(cancel_button, SecuremtrCancelBoostButton)
    assert cancel_button.available is True

    dispatcher_calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        "custom_components.securemtr.runtime_helpers.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: dispatcher_calls.append((hass_obj, entry_id)),
    )

    helper_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original_helper = runtime_helpers.async_mutate_runtime

    async def _wrapped_helper(*args: Any, **kwargs: Any) -> Any:
        helper_calls.append((args, kwargs))
        return await original_helper(*args, **kwargs)

    monkeypatch.setattr(
        "custom_components.securemtr.entity.async_mutate_runtime",
        _wrapped_helper,
    )

    cancel_button.hass = SimpleNamespace()
    hass_obj = cancel_button.hass
    await cancel_button.async_press()

    assert backend.stop_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.timed_boost_active is False
    assert runtime.timed_boost_end_minute is None
    assert runtime.timed_boost_end_time is None
    assert dispatcher_calls == [(hass_obj, "entry")]

    with pytest.raises(HomeAssistantError):
        await cancel_button.async_press()

    runtime.timed_boost_active = True
    runtime.timed_boost_end_minute = 615
    runtime.timed_boost_end_time = coerce_end_time(615)
    cancel_button.hass = None
    await cancel_button.async_press()
    assert backend.stop_calls[-1] == (
        runtime.session,
        runtime.websocket,
        "gateway-1",
    )
    assert dispatcher_calls == [(hass_obj, "entry")]
    assert len(helper_calls) == 2


@pytest.mark.asyncio
async def test_cancel_button_requires_connection() -> None:
    """Ensure cancellation raises when the runtime is disconnected."""

    runtime, backend = _create_runtime()
    runtime.session = None
    runtime.timed_boost_active = True
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    cancel_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_cancel")
    )

    with pytest.raises(HomeAssistantError):
        await cancel_button.async_press()

    assert backend.stop_calls == []


@pytest.mark.asyncio
async def test_cancel_button_requires_controller() -> None:
    """Ensure the cancel button validates controller availability."""

    runtime, backend = _create_runtime()
    runtime.timed_boost_active = True
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    cancel_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_cancel")
    )

    runtime.controller = None

    with pytest.raises(HomeAssistantError):
        await cancel_button.async_press()

    assert backend.stop_calls == []


@pytest.mark.asyncio
async def test_cancel_button_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure backend failures propagate for cancellation."""

    runtime, backend = _create_runtime()
    runtime.timed_boost_active = True
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    cancel_button = next(
        entity for entity in entities if entity.unique_id.endswith("boost_cancel")
    )
    monkeypatch.setattr(
        "custom_components.securemtr.runtime_helpers.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: None,
    )
    cancel_button.hass = SimpleNamespace()

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise BeanbagError("boom")

    monkeypatch.setattr(backend, "stop_timed_boost", _raise)

    with pytest.raises(HomeAssistantError):
        await cancel_button.async_press()

    assert runtime.timed_boost_active is True


@pytest.mark.asyncio
async def test_button_setup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure setup raises when controller metadata is delayed."""

    runtime, _backend = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.entity._CONTROLLER_READY_TIMEOUT", 0.01
    )

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_button_setup_requires_controller() -> None:
    """Ensure setup raises when the runtime lacks controller metadata."""

    runtime, _backend = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_button_async_added_to_hass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure dispatcher callbacks are registered when the entity is added."""

    runtime, _backend = _create_runtime()
    button = SecuremtrTimedBoostButton(
        runtime, runtime.controller, create_config_entry(entry_id="entry"), 30
    )
    button.hass = SimpleNamespace()

    added_calls: list[SecuremtrTimedBoostButton] = []

    async def _fake_added(self: SecuremtrTimedBoostButton) -> None:
        added_calls.append(self)

    connections: list[tuple[object, str, Any]] = []

    def _connect(hass_obj: object, signal: str, callback: Any) -> Any:
        connections.append((hass_obj, signal, callback))
        return lambda: None

    removals: list[Any] = []

    def _record_remove(remover: Any) -> None:
        removals.append(remover)

    button.async_on_remove = _record_remove  # type: ignore[assignment]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.securemtr.button.ButtonEntity.async_added_to_hass",
            _fake_added,
        )
        mp.setattr(
            "custom_components.securemtr.entity.async_dispatcher_connect",
            _connect,
        )
        await button.async_added_to_hass()

    assert added_calls
    assert connections[0][0] is button.hass
    assert removals

    button.hass = None
    await button.async_added_to_hass()


@pytest.mark.asyncio
async def test_consumption_button_triggers_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure pressing the consumption button refreshes metrics."""

    runtime, _backend = _create_runtime()
    hass = SimpleNamespace()
    entry = create_config_entry(entry_id="entry")
    button = SecuremtrConsumptionMetricsButton(runtime, runtime.controller, entry)
    button.hass = hass

    calls: list[tuple[object, ConfigEntry]] = []

    async def _fake_refresh(hass_obj: object, entry_obj: ConfigEntry) -> None:
        calls.append((hass_obj, entry_obj))

    monkeypatch.setattr(
        "custom_components.securemtr.button.consumption_metrics", _fake_refresh
    )

    await button.async_press()

    assert calls == [(hass, entry)]

    button.hass = None
    with pytest.raises(HomeAssistantError):
        await button.async_press()
