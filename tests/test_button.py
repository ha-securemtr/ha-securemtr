"""Tests for the Secure Meters button platform."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest

import custom_components.securemtr.entity as securemtr_entity
import custom_components.securemtr.runtime_helpers as runtime_helpers
from custom_components.securemtr import (
    CONNECTION_MODE_LOCAL_BLE,
    DEFAULT_DEVICE_LABEL,
    DOMAIN,
    consumption_metrics,
    SecuremtrController,
    SecuremtrRuntimeData,
    coerce_end_time,
)
from custom_components.securemtr.beanbag import BeanbagError, DailyProgram
from custom_components.securemtr.button import (
    SecuremtrCancelBoostButton,
    SecuremtrConsumptionMetricsButton,
    SecuremtrLogWeeklyScheduleButton,
    SecuremtrTimedBoostButton,
    async_setup_entry,
)
from custom_components.securemtr.entity import controller_display_label
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

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


def test_controller_display_label_fallbacks() -> None:
    """Verify controller labels fall back through metadata attributes."""

    controller = SecuremtrController(
        identifier=" controller-1 ",
        name="  ",
        gateway_id=" gateway-1 ",
        serial_number=None,
        firmware_version=None,
        model=None,
    )
    assert controller_display_label(controller) == "controller-1"

    controller_gateway = SecuremtrController(
        identifier=" ",
        name="",
        gateway_id=" gateway-1 ",
        serial_number="  ",
        firmware_version=None,
        model=None,
    )
    assert controller_display_label(controller_gateway) == "gateway-1"

    controller_missing = SecuremtrController(
        identifier=" ",
        name="",
        gateway_id=" ",
        serial_number="  ",
        firmware_version=None,
        model=None,
    )
    assert controller_display_label(controller_missing) == DEFAULT_DEVICE_LABEL


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

    assert {entity.unique_id for entity in entities} == {
        "serial_1_boost_30",
        "serial_1_boost_60",
        "serial_1_boost_120",
        "serial_1_boost_cancel",
        "serial_1_refresh_consumption",
        "serial_1_log_schedule",
    }

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
    assert boost_button.translation_key == "boost_60_minutes"
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

    helper_calls: list[dict[str, Any]] = []
    original_helper = securemtr_entity.SecuremtrCommandMixin._async_controller_command

    async def _wrapped_helper(
        self: Any,
        method_name: str,
        *,
        runtime_update: Any,
        **kwargs: Any,
    ) -> Any:
        helper_calls.append(
            {
                "self": self,
                "method_name": method_name,
                "runtime_update": runtime_update,
                "kwargs": dict(kwargs),
            }
        )
        return await original_helper(
            self,
            method_name,
            runtime_update=runtime_update,
            **kwargs,
        )

    monkeypatch.setattr(
        "custom_components.securemtr.entity.SecuremtrCommandMixin._async_controller_command",
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
    assert helper_calls[0]["method_name"] == "start_timed_boost"
    assert (
        helper_calls[0]["kwargs"]["log_context"]
        == "Failed to start Secure Meters timed boost"
    )
    assert helper_calls[0]["kwargs"]["exception_types"] == (BeanbagError, ValueError)
    assert helper_calls[0]["kwargs"].get("write_state", False) is False

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

    recorded_method: list[str] = []
    recorded_kwargs: dict[str, Any] = {}

    async def _failing_helper(
        self: Any,
        method_name: str,
        *,
        runtime_update: Any,
        **kwargs: Any,
    ) -> Any:
        recorded_method.append(method_name)
        recorded_kwargs.update(kwargs)
        raise HomeAssistantError("boom")

    monkeypatch.setattr(
        "custom_components.securemtr.entity.SecuremtrCommandMixin._async_controller_command",
        _failing_helper,
    )

    with pytest.raises(HomeAssistantError, match="boom"):
        await boost_button.async_press()

    assert recorded_method == ["start_timed_boost"]
    assert recorded_kwargs["log_context"] == "Failed to start Secure Meters timed boost"
    assert recorded_kwargs.get("write_state", False) is False
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
    with caplog.at_level(logging.DEBUG):
        await schedule_button.async_press()

    assert backend.read_calls == ["primary", "boost"]
    controller_label = controller_display_label(runtime.controller)
    assert any("primary zone" in record for record in caplog.messages)
    assert any(controller_label in record for record in caplog.messages)
    assert any("boost zone" in record for record in caplog.messages)
    assert any("Monday" in record and "01:00" in record for record in caplog.messages)
    assert any("Saturday" in record and "08:00" in record for record in caplog.messages)
    assert any(
        "Canonical weekly schedule" in record and "primary zone" in record
        for record in caplog.messages
    )
    assert any(
        "Canonical weekly schedule" in record and "boost zone" in record
        for record in caplog.messages
    )


@pytest.mark.asyncio
async def test_schedule_button_local_ble_uses_local_reader(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ensure local BLE schedule logging reads schedules over BLE, not cloud."""

    runtime, backend = _create_runtime()
    runtime.connection_mode = CONNECTION_MODE_LOCAL_BLE
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(
        entry_id="entry",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    entities: list[ButtonEntity] = []

    weekday = DailyProgram((60, None, None), (120, None, None))
    weekend = DailyProgram((480, 1020, None), (540, 1320, None))
    programs = {
        "primary": (weekday,) * 5 + (weekend,) * 2,
        "boost": (weekend,) * 7,
    }
    canonicals = {
        "primary": [(60, 120)],
        "boost": [(480, 540)],
    }

    fake_worker = SimpleNamespace(
        async_read_local_weekly_programs=AsyncMock(return_value=(programs, canonicals))
    )
    get_worker = AsyncMock(return_value=fake_worker)
    monkeypatch.setattr(
        "custom_components.securemtr.button.async_get_local_ble_worker",
        get_worker,
    )

    await async_setup_entry(hass, entry, entities.extend)

    schedule_button = next(
        entity for entity in entities if entity.unique_id.endswith("log_schedule")
    )
    local_hass = SimpleNamespace()
    schedule_button.hass = local_hass

    with caplog.at_level(logging.INFO):
        await schedule_button.async_press()

    get_worker.assert_awaited_once_with(local_hass, entry, runtime)
    fake_worker.async_read_local_weekly_programs.assert_awaited_once()
    read_kwargs = fake_worker.async_read_local_weekly_programs.await_args.kwargs
    assert read_kwargs["coalesce_key"] is None
    assert read_kwargs["zone_bois"] is runtime.schedule_zone_bois
    assert backend.read_calls == []


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
    assert any("missing zones" in record for record in caplog.messages)


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

    helper_calls: list[dict[str, Any]] = []
    original_helper = securemtr_entity.SecuremtrCommandMixin._async_controller_command

    async def _wrapped_helper(
        self: Any,
        method_name: str,
        *,
        runtime_update: Any,
        **kwargs: Any,
    ) -> Any:
        helper_calls.append(
            {
                "self": self,
                "method_name": method_name,
                "runtime_update": runtime_update,
                "kwargs": dict(kwargs),
            }
        )
        return await original_helper(
            self,
            method_name,
            runtime_update=runtime_update,
            **kwargs,
        )

    monkeypatch.setattr(
        "custom_components.securemtr.entity.SecuremtrCommandMixin._async_controller_command",
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
    assert helper_calls[0]["method_name"] == "stop_timed_boost"
    assert (
        helper_calls[0]["kwargs"]["log_context"]
        == "Failed to cancel Secure Meters timed boost"
    )
    assert helper_calls[0]["kwargs"].get("write_state", False) is False

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
async def test_button_setup_times_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ensure setup skips button creation when controller metadata is delayed."""

    runtime, _backend = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.entity._CONTROLLER_READY_TIMEOUT", 0.01
    )

    entities: list[ButtonEntity] = []

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConfigEntryNotReady, match="not ready"):
            await async_setup_entry(hass, entry, entities.extend)

    assert entities == []
    assert "Skipping SecureMTR button setup" in caplog.text


@pytest.mark.asyncio
async def test_button_setup_requires_controller() -> None:
    """Ensure setup skips button creation when controller metadata is missing."""

    runtime, _backend = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    entities: list[ButtonEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert entities == []


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
        "custom_components.securemtr.button.async_refresh_entry_state", _fake_refresh
    )

    await button.async_press()

    assert calls == [(hass, entry)]

    button.hass = None
    with pytest.raises(HomeAssistantError):
        await button.async_press()


@pytest.mark.asyncio
async def test_consumption_refresh_serializes_with_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure consumption refresh defers backend work until commands finish."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    dispatcher_calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        "custom_components.securemtr.runtime_helpers.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: dispatcher_calls.append((hass_obj, entry_id)),
    )

    command_button = SecuremtrTimedBoostButton(runtime, runtime.controller, entry, 30)
    command_button.hass = SimpleNamespace()

    command_started = asyncio.Event()
    release_command = asyncio.Event()

    async def _blocking_start(
        session: object,
        websocket: object,
        gateway_id: str,
        *,
        duration_minutes: int,
    ) -> None:
        command_started.set()
        await release_command.wait()
        backend.start_calls.append((session, websocket, gateway_id, duration_minutes))

    backend.start_timed_boost = _blocking_start  # type: ignore[assignment]

    prepared_called = asyncio.Event()
    process_called = asyncio.Event()

    async def _fake_validate(
        hass_obj: object,
        entry_obj: ConfigEntry,
        entry_identifier: str,
        runtime_obj: SecuremtrRuntimeData | None = None,
    ) -> tuple[SecuremtrRuntimeData, SecuremtrController, object, object]:
        return runtime, runtime.controller, runtime.session, runtime.websocket

    async def _fake_prepare(
        entry_obj: ConfigEntry,
        runtime_obj: SecuremtrRuntimeData,
        controller_obj: SecuremtrController,
        entry_identifier: str,
    ) -> SimpleNamespace | None:
        if not release_command.is_set():
            raise AssertionError("Consumption refresh ran before command completed")
        prepared_called.set()
        return SimpleNamespace(rows=[{}], options=SimpleNamespace())

    async def _fake_process(
        hass_obj: object,
        entry_obj: ConfigEntry,
        runtime_obj: SecuremtrRuntimeData,
        session: object,
        websocket: object,
        controller_obj: SecuremtrController,
        prepared: SimpleNamespace,
        entry_identifier: str,
    ) -> SimpleNamespace:
        if not release_command.is_set():
            raise AssertionError("Zone processing ran before command completed")
        process_called.set()
        return SimpleNamespace(
            statistics_samples={},
            sensor_state={},
            recent_measurements={},
            energy_state_changed=False,
            dispatch_needed=False,
        )

    submit_calls: list[tuple] = []
    update_calls: list[tuple] = []

    def _fake_submit(*args: object) -> None:
        submit_calls.append(args)

    def _fake_update(*args: object) -> None:
        update_calls.append(args)

    monkeypatch.setattr(
        "custom_components.securemtr._validate_consumption_connection",
        _fake_validate,
    )
    monkeypatch.setattr(
        "custom_components.securemtr._prepare_consumption_samples",
        _fake_prepare,
    )
    monkeypatch.setattr(
        "custom_components.securemtr._process_zone_samples",
        _fake_process,
    )
    monkeypatch.setattr(
        "custom_components.securemtr._submit_statistics",
        _fake_submit,
    )
    monkeypatch.setattr(
        "custom_components.securemtr._update_runtime_state",
        _fake_update,
    )

    command_task = asyncio.create_task(command_button.async_press())
    await asyncio.wait_for(command_started.wait(), timeout=1)

    refresh_task = asyncio.create_task(consumption_metrics(hass, entry))

    await asyncio.sleep(0)
    assert prepared_called.is_set() is False
    assert process_called.is_set() is False

    release_command.set()

    await asyncio.wait_for(command_task, timeout=1)
    await asyncio.wait_for(refresh_task, timeout=1)

    assert prepared_called.is_set()
    assert process_called.is_set()
    assert submit_calls
    assert update_calls
    assert dispatcher_calls


@pytest.mark.asyncio
async def test_local_boost_button_uses_local_ble_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure local-mode boost buttons invoke the BLE command transport."""

    runtime, _backend = _create_runtime()
    runtime.connection_mode = CONNECTION_MODE_LOCAL_BLE
    runtime.websocket = None

    entry = create_config_entry(
        entry_id="entry-local",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    button = SecuremtrTimedBoostButton(runtime, runtime.controller, entry, 30)
    button.hass = SimpleNamespace()

    fake_worker = SimpleNamespace(async_execute_local_command=AsyncMock(return_value=0))
    get_worker = AsyncMock(return_value=fake_worker)
    monkeypatch.setattr(
        "custom_components.securemtr.entity.async_get_local_ble_worker",
        get_worker,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.entity.async_dispatch_runtime_update",
        lambda hass, entry_id: None,
    )

    await button.async_press()

    get_worker.assert_awaited_once_with(button.hass, entry, runtime)
    fake_worker.async_execute_local_command.assert_awaited_once()
    kwargs = fake_worker.async_execute_local_command.await_args.kwargs
    assert kwargs["method_name"] == "start_timed_boost"
    assert kwargs["operation_kwargs"] == {"duration_minutes": 30}
    assert runtime.timed_boost_active is True


@pytest.mark.asyncio
async def test_local_boost_button_requires_stored_ble_key() -> None:
    """Ensure local-mode commands fail when BLE credentials are absent."""

    runtime, _backend = _create_runtime()
    runtime.connection_mode = CONNECTION_MODE_LOCAL_BLE
    runtime.websocket = None

    entry = create_config_entry(entry_id="entry-local", data={})
    button = SecuremtrTimedBoostButton(runtime, runtime.controller, entry, 30)
    button.hass = SimpleNamespace()

    with pytest.raises(HomeAssistantError, match="Local BLE key is missing"):
        await button.async_press()
