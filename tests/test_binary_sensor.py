import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.securemtr import DOMAIN, SecuremtrController, SecuremtrRuntimeData
from custom_components.securemtr.binary_sensor import (
    SecuremtrBoostActiveBinarySensor,
    async_setup_entry,
)
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.exceptions import HomeAssistantError


@dataclass(slots=True)
class DummyEntry:
    """Provide the minimal config entry attributes."""

    entry_id: str


class DummyBackend:
    """Provide backend stubs to satisfy the runtime interface."""

    async def read_device_metadata(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        """Unused helper for interface completeness."""


def _create_runtime() -> SecuremtrRuntimeData:
    """Return runtime data with a connected controller."""

    runtime = SecuremtrRuntimeData(backend=DummyBackend())
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
    runtime.timed_boost_active = False
    runtime.controller_ready.set()
    return runtime


@pytest.mark.asyncio
async def test_binary_sensor_setup_and_state() -> None:
    """Ensure the binary sensor exposes the boost active flag."""

    runtime = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[BinarySensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert len(entities) == 1
    sensor = entities[0]
    assert isinstance(sensor, SecuremtrBoostActiveBinarySensor)
    assert sensor.unique_id == "serial_1_boost_active"
    assert sensor.is_on is False
    assert sensor.available is True
    info = sensor.device_info
    assert info["name"] == "E7+ Smart Water Heater Controller"

    runtime.timed_boost_active = True
    assert sensor.is_on is True

    runtime.websocket = None
    assert sensor.available is False

    sensor.hass = SimpleNamespace()

    async def _fake_added(self: SecuremtrBoostActiveBinarySensor) -> None:
        return None

    connections: list[tuple[object, str, Any]] = []

    def _connect(hass_obj: object, signal: str, callback: Any) -> Any:
        connections.append((hass_obj, signal, callback))
        return lambda: None

    removals: list[Any] = []

    def _record_remove(remover: Any) -> None:
        removals.append(remover)

    sensor.async_on_remove = _record_remove  # type: ignore[assignment]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.securemtr.binary_sensor.BinarySensorEntity.async_added_to_hass",
            _fake_added,
        )
        mp.setattr(
            "custom_components.securemtr.binary_sensor.async_dispatcher_connect",
            _connect,
        )
        await sensor.async_added_to_hass()

    assert connections[0][0] is sensor.hass
    assert removals


@pytest.mark.asyncio
async def test_binary_sensor_requires_controller() -> None:
    """Ensure setup raises when controller metadata is missing."""

    runtime = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_binary_sensor_setup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure setup raises when controller metadata is delayed."""

    runtime = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.binary_sensor._CONTROLLER_WAIT_TIMEOUT", 0.01
    )

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_binary_sensor_async_added_to_hass_without_hass() -> None:
    """Ensure the dispatcher registration exits when hass is missing."""

    runtime = _create_runtime()
    sensor = SecuremtrBoostActiveBinarySensor(runtime, runtime.controller, "entry")
    sensor.hass = None
    await sensor.async_added_to_hass()
