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
    _slugify_identifier,
    async_setup_entry,
)
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
        name="E7+ Controller",
        gateway_id="gateway-1",
        serial_number="serial-1",
        firmware_version="1.0.0",
        model="E7+",
    )
    runtime.primary_power_on = False
    runtime.controller_ready.set()
    return runtime, backend


@pytest.mark.asyncio
async def test_switch_setup_creates_entity() -> None:
    """Ensure the switch platform exposes the controller power switch."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SecuremtrPowerSwitch] = []

    def _add_entities(new_entities: list[SecuremtrPowerSwitch]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert len(entities) == 1
    switch = entities[0]
    assert switch.available
    assert switch.unique_id == "controller_1_primary_power"
    assert switch.device_info["identifiers"] == {(DOMAIN, "controller-1")}
    assert (
        switch.name
        == "E7+ Controller controller-1 Water Heater"
    )
    assert switch.is_on is False

    switch.hass = SimpleNamespace()
    switch.entity_id = "switch.securemtr_controller"
    state_writes: list[str] = []

    def _record_state_write() -> None:
        state_writes.append("write")

    switch.async_write_ha_state = _record_state_write  # type: ignore[assignment]

    await switch.async_turn_on()
    assert backend.on_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.primary_power_on is True
    assert switch.is_on is True
    assert state_writes == ["write"]

    switch.hass = None
    state_writes.clear()

    await switch.async_turn_off()
    assert backend.off_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.primary_power_on is False
    assert switch.is_on is False
    assert state_writes == []


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
    entities: list[SecuremtrPowerSwitch] = []

    await async_setup_entry(hass, entry, entities.extend)

    switch = entities[0]
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()

    assert backend.on_calls == []


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
