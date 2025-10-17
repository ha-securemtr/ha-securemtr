import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.securemtr import DOMAIN, SecuremtrController, SecuremtrRuntimeData
from custom_components.securemtr.sensor import SecuremtrBoostEndsSensor, async_setup_entry
from homeassistant.components.sensor import SensorEntity
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
        name="E7+ Controller",
        gateway_id="gateway-1",
        serial_number="serial-1",
        firmware_version="1.0.0",
        model="E7+",
    )
    runtime.controller_ready.set()
    runtime.timed_boost_active = False
    runtime.timed_boost_end_time = None
    return runtime


@pytest.mark.asyncio
async def test_sensor_reports_end_time() -> None:
    """Ensure the sensor reports the boost end timestamp when active."""

    runtime = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert len(entities) == 1
    sensor = entities[0]
    assert isinstance(sensor, SecuremtrBoostEndsSensor)
    assert sensor.unique_id == "serial_1_boost_ends"
    assert sensor.native_value is None
    assert sensor.device_info["name"] == "E7+ Water Heater (SN: serial-1)"
    assert sensor.available is True

    sensor.hass = SimpleNamespace()

    async def _fake_added(self: SecuremtrBoostEndsSensor) -> None:
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
            "custom_components.securemtr.sensor.SensorEntity.async_added_to_hass",
            _fake_added,
        )
        mp.setattr(
            "custom_components.securemtr.sensor.async_dispatcher_connect",
            _connect,
        )
        await sensor.async_added_to_hass()

    assert connections[0][0] is sensor.hass
    assert removals

    runtime.timed_boost_active = True
    expected = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    runtime.timed_boost_end_time = expected
    assert sensor.native_value == expected

    sensor.hass = None
    await sensor.async_added_to_hass()

    runtime.websocket = None
    assert sensor.available is False


@pytest.mark.asyncio
async def test_sensor_requires_controller() -> None:
    """Ensure setup raises when controller metadata is missing."""

    runtime = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)


@pytest.mark.asyncio
async def test_sensor_setup_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure setup raises when controller metadata is delayed."""

    runtime = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.sensor._CONTROLLER_WAIT_TIMEOUT", 0.01
    )

    with pytest.raises(HomeAssistantError):
        await async_setup_entry(hass, entry, lambda entities: None)
