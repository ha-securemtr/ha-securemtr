"""Tests for the securemtr switch platform."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
)
from custom_components.securemtr.beanbag import BeanbagError
from custom_components.securemtr.switch import (
    SecuremtrPowerSwitch,
    SecuremtrTimedBoostSwitch,
    _slugify_identifier,
    async_setup_entry,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError


@dataclass(slots=True)
class DummyEntry:
    """Provide the minimal attributes required by the platform setup."""

    entry_id: str


class DummyBackend:
    """Capture power commands issued by the switch entity."""

    def __init__(self) -> None:
        self.on_calls: list[tuple[Any, Any, str]] = []
        self.off_calls: list[tuple[Any, Any, str]] = []
        self.timed_boost_calls: list[tuple[Any, Any, str, bool]] = []

    async def turn_controller_on(
        self, session: Any, websocket: Any, gateway_id: str
    ) -> None:
        """Record an on command."""

        self.on_calls.append((session, websocket, gateway_id))

    async def turn_controller_off(
        self, session: Any, websocket: Any, gateway_id: str
    ) -> None:
        """Record an off command."""

        self.off_calls.append((session, websocket, gateway_id))

    async def set_timed_boost_enabled(
        self,
        session: Any,
        websocket: Any,
        gateway_id: str,
        *,
        enabled: bool,
    ) -> None:
        """Record a timed boost toggle command."""

        self.timed_boost_calls.append((session, websocket, gateway_id, enabled))

    async def read_device_metadata(
        self, *args: Any, **kwargs: Any
    ) -> None:  # pragma: no cover - unused stub
        """Placeholder to satisfy the runtime interface."""


def _create_runtime() -> tuple[SecuremtrRuntimeData, DummyBackend]:
    """Construct a runtime data object with a ready controller."""

    backend = DummyBackend()
    runtime = SecuremtrRuntimeData(backend=backend)
    runtime.session = SimpleNamespace()
    runtime.websocket = SimpleNamespace()
    runtime.controller = SecuremtrController(
        identifier="controller-1",
        name="E7+ Water Heater (SN: serial-1)",
        gateway_id="gateway-1",
        serial_number="serial-1",
        firmware_version="1.0.0",
        model="E7+",
    )
    runtime.primary_power_on = False
    runtime.timed_boost_enabled = False
    runtime.controller_ready.set()
    return runtime, backend


@pytest.mark.asyncio
async def test_switch_setup_creates_entity() -> None:
    """Ensure the switch platform exposes the controller power switch."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SwitchEntity] = []

    def _add_entities(new_entities: list[SwitchEntity]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert {entity.unique_id for entity in entities} == {
        "serial_1_primary_power",
        "serial_1_timed_boost",
    }

    power_switch = next(
        entity for entity in entities if entity.unique_id.endswith("primary_power")
    )
    timed_switch = next(
        entity for entity in entities if entity.unique_id.endswith("timed_boost")
    )

    assert isinstance(power_switch, SecuremtrPowerSwitch)
    assert isinstance(timed_switch, SecuremtrTimedBoostSwitch)
    assert power_switch.available
    assert timed_switch.available
    assert power_switch.device_info["identifiers"] == {(DOMAIN, "serial-1")}
    assert timed_switch.device_info["name"] == "E7+ Water Heater (SN: serial-1)"
    assert timed_switch.device_info["model"] == "E7+"
    assert power_switch.name == "E7+ Controller"
    assert timed_switch.name == "Timed Boost"
    assert power_switch.is_on is False
    assert timed_switch.is_on is False

    power_switch.hass = SimpleNamespace()
    power_switch.entity_id = "switch.securemtr_controller"
    state_writes: list[str] = []

    def _record_state_write() -> None:
        state_writes.append("write")

    power_switch.async_write_ha_state = _record_state_write  # type: ignore[assignment]

    await power_switch.async_turn_on()
    assert backend.on_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.primary_power_on is True
    assert power_switch.is_on is True
    assert state_writes == ["write"]

    power_switch.hass = None
    state_writes.clear()

    await power_switch.async_turn_off()
    assert backend.off_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.primary_power_on is False
    assert power_switch.is_on is False
    assert state_writes == []

    timed_switch.hass = SimpleNamespace()
    timed_switch.entity_id = "switch.securemtr_timed_boost"
    timed_state_writes: list[str] = []

    def _record_timed_state_write() -> None:
        timed_state_writes.append("write")

    timed_switch.async_write_ha_state = _record_timed_state_write  # type: ignore[assignment]

    await timed_switch.async_turn_on()
    assert backend.timed_boost_calls == [
        (runtime.session, runtime.websocket, "gateway-1", True)
    ]
    assert runtime.timed_boost_enabled is True
    assert timed_switch.is_on is True
    assert timed_state_writes == ["write"]

    timed_switch.hass = None
    timed_state_writes.clear()

    await timed_switch.async_turn_off()
    assert backend.timed_boost_calls[-1] == (
        runtime.session,
        runtime.websocket,
        "gateway-1",
        False,
    )
    assert runtime.timed_boost_enabled is False
    assert timed_switch.is_on is False
    assert timed_state_writes == []


def test_switch_device_info_without_serial() -> None:
    """Ensure device registry names fall back to the identifier when no serial exists."""

    runtime, _backend = _create_runtime()
    controller = SecuremtrController(
        identifier="controller-1",
        name="E7+ Water Heater (controller-1)",
        gateway_id="gateway-1",
        serial_number=None,
        firmware_version=None,
        model=None,
    )

    switch = SecuremtrPowerSwitch(runtime, controller)
    device_info = switch.device_info
    assert device_info["name"] == "E7+ Water Heater (controller-1)"
    assert device_info["serial_number"] is None


@pytest.mark.asyncio
async def test_switch_setup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the platform raises when metadata is not ready in time."""

    runtime, _backend = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.switch._CONTROLLER_WAIT_TIMEOUT", 0.01
    )

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_switch_setup_requires_controller() -> None:
    """Ensure a missing controller raises an explicit error."""

    runtime, _backend = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_switch_turn_on_requires_connection() -> None:
    """Ensure the switch raises when the runtime lacks a live connection."""

    runtime, backend = _create_runtime()
    runtime.session = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SwitchEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    power_switch = next(
        entity for entity in entities if entity.unique_id.endswith("primary_power")
    )
    with pytest.raises(HomeAssistantError):
        await power_switch.async_turn_on()

    assert backend.on_calls == []


@pytest.mark.asyncio
async def test_timed_boost_requires_connection() -> None:
    """Ensure the timed boost switch raises when the runtime lacks a connection."""

    runtime, backend = _create_runtime()
    runtime.session = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SwitchEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    timed_switch = next(
        entity for entity in entities if entity.unique_id.endswith("timed_boost")
    )

    with pytest.raises(HomeAssistantError):
        await timed_switch.async_turn_on()

    assert backend.timed_boost_calls == []


@pytest.mark.asyncio
async def test_timed_boost_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Convert backend failures into Home Assistant errors for timed boost."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SwitchEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    timed_switch = next(
        entity for entity in entities if entity.unique_id.endswith("timed_boost")
    )

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise BeanbagError("fail")

    monkeypatch.setattr(backend, "set_timed_boost_enabled", _raise)

    with pytest.raises(HomeAssistantError):
        await timed_switch.async_turn_on()

    assert runtime.timed_boost_enabled is False


@pytest.mark.asyncio
async def test_switch_turn_on_handles_backend_error() -> None:
    """Verify Beanbag errors propagate as Home Assistant errors."""

    runtime, _backend = _create_runtime()

    class ErrorBackend(DummyBackend):
        async def turn_controller_on(
            self, session: Any, websocket: Any, gateway_id: str
        ) -> None:
            raise BeanbagError("boom")

    runtime.backend = ErrorBackend()  # type: ignore[assignment]
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SecuremtrPowerSwitch] = []

    await async_setup_entry(hass, entry, entities.extend)

    switch = entities[0]
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    assert runtime.primary_power_on is False


def test_slugify_identifier_generates_stable_slug() -> None:
    """Ensure the helper normalises identifiers as expected."""

    assert _slugify_identifier(" Controller #1 ") == "controller__1"
