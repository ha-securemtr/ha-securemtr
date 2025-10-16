"""Tests for the securemtr button platform."""

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
from custom_components.securemtr.button import (
    SecuremtrPowerButton,
    _slugify_identifier,
    async_setup_entry,
)
from homeassistant.exceptions import HomeAssistantError


@dataclass(slots=True)
class DummyEntry:
    """Provide the minimal attributes required by the platform setup."""

    entry_id: str


class DummyBackend:
    """Capture power commands issued by the button entity."""

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
        identifier="serial-1",
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
async def test_button_setup_creates_entity() -> None:
    """Ensure the button platform exposes the controller toggle button."""

    runtime, backend = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SecuremtrPowerButton] = []

    def _add_entities(new_entities: list[SecuremtrPowerButton]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert len(entities) == 1
    button = entities[0]
    assert button.available
    assert button.unique_id == "serial_1_primary_power"
    assert button.device_info["identifiers"] == {(DOMAIN, "serial-1")}

    await button.async_press()
    assert backend.on_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.primary_power_on is True

    await button.async_press()
    assert backend.off_calls == [(runtime.session, runtime.websocket, "gateway-1")]
    assert runtime.primary_power_on is False


@pytest.mark.asyncio
async def test_button_setup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the platform raises when metadata is not ready in time."""

    runtime, _backend = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.button._CONTROLLER_WAIT_TIMEOUT", 0.01
    )

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_button_setup_requires_controller() -> None:
    """Ensure a missing controller raises an explicit error."""

    runtime, _backend = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_button_press_requires_connection() -> None:
    """Ensure the button raises when the runtime lacks a live connection."""

    runtime, backend = _create_runtime()
    runtime.session = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SecuremtrPowerButton] = []

    await async_setup_entry(hass, entry, entities.extend)

    button = entities[0]
    with pytest.raises(HomeAssistantError):
        await button.async_press()

    assert backend.on_calls == []


@pytest.mark.asyncio
async def test_button_press_handles_backend_error() -> None:
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
    entities: list[SecuremtrPowerButton] = []

    await async_setup_entry(hass, entry, entities.extend)

    button = entities[0]
    with pytest.raises(HomeAssistantError):
        await button.async_press()

    assert runtime.primary_power_on is False


def test_slugify_identifier_generates_stable_slug() -> None:
    """Ensure the helper normalises identifiers as expected."""

    assert _slugify_identifier(" Controller #1 ") == "controller__1"
