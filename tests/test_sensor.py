import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.securemtr import DOMAIN, SecuremtrController, SecuremtrRuntimeData
from custom_components.securemtr.sensor import (
    SecuremtrBoostEndsSensor,
    SecuremtrDailyDurationSensor,
    SecuremtrEnergyTotalSensor,
    async_setup_entry,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime
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

    sensor = next(
        entity
        for entity in entities
        if isinstance(entity, SecuremtrBoostEndsSensor)
    )
    assert sensor.unique_id == "serial_1_boost_ends"
    assert sensor.native_value is None
    assert (
        sensor.device_info["name"] == "E7+ Smart Water Heater Controller"
    )
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
async def test_statistics_sensors_report_totals() -> None:
    """Ensure the statistics sensors expose cumulative and daily values."""

    runtime = _create_runtime()
    runtime.statistics_state = {
        "primary": {"energy_sum": 12.5, "last_day": "2024-03-01"},
        "boost": {"energy_sum": 4.75, "last_day": "2024-03-01"},
    }
    runtime.statistics_recent = {
        "primary": {
            "report_day": "2024-03-01",
            "runtime_hours": 3.25,
            "scheduled_hours": 4.0,
            "energy_sum": 12.5,
        },
        "boost": {
            "report_day": "2024-03-01",
            "runtime_hours": 0.5,
            "scheduled_hours": 1.0,
            "energy_sum": 4.75,
        },
    }

    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = DummyEntry(entry_id="entry")
    entities: list[SensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert len(entities) == 7
    sensors_by_id = {entity.unique_id: entity for entity in entities}

    primary_energy = sensors_by_id["serial_1_primary_energy_total"]
    assert isinstance(primary_energy, SecuremtrEnergyTotalSensor)
    assert primary_energy.native_value == pytest.approx(12.5)
    assert (
        primary_energy.native_unit_of_measurement
        == UnitOfEnergy.KILO_WATT_HOUR
    )
    assert primary_energy.device_class is SensorDeviceClass.ENERGY
    assert primary_energy.state_class is SensorStateClass.TOTAL_INCREASING
    assert primary_energy.extra_state_attributes == {
        "last_report_day": "2024-03-01"
    }

    boost_energy = sensors_by_id["serial_1_boost_energy_total"]
    assert isinstance(boost_energy, SecuremtrEnergyTotalSensor)
    assert boost_energy.native_value == pytest.approx(4.75)
    assert boost_energy.extra_state_attributes == {
        "last_report_day": "2024-03-01"
    }

    primary_runtime = sensors_by_id["serial_1_primary_runtime_daily"]
    assert isinstance(primary_runtime, SecuremtrDailyDurationSensor)
    assert primary_runtime.native_value == pytest.approx(3.25)
    assert primary_runtime.native_unit_of_measurement == UnitOfTime.HOURS
    assert primary_runtime.device_class is SensorDeviceClass.DURATION
    assert primary_runtime.state_class is SensorStateClass.MEASUREMENT
    primary_runtime_attrs = primary_runtime.extra_state_attributes
    assert primary_runtime_attrs is not None
    assert primary_runtime_attrs["report_day"] == "2024-03-01"
    assert primary_runtime_attrs["energy_total_kwh"] == pytest.approx(12.5)

    boost_runtime = sensors_by_id["serial_1_boost_runtime_daily"]
    assert boost_runtime.native_value == pytest.approx(0.5)

    primary_scheduled = sensors_by_id["serial_1_primary_scheduled_daily"]
    assert primary_scheduled.native_value == pytest.approx(4.0)
    primary_scheduled_attrs = primary_scheduled.extra_state_attributes
    assert primary_scheduled_attrs is not None
    assert primary_scheduled_attrs["report_day"] == "2024-03-01"
    assert primary_scheduled_attrs["energy_total_kwh"] == pytest.approx(12.5)

    runtime.statistics_state = {"boost": {"energy_sum": "invalid", "last_day": 123}}
    assert boost_energy.native_value is None
    assert boost_energy.extra_state_attributes is None

    runtime.statistics_state = None
    assert boost_energy.native_value is None
    assert boost_energy.extra_state_attributes is None

    runtime.statistics_recent = {"boost": {"runtime_hours": "invalid"}}
    assert boost_runtime.native_value is None

    runtime.statistics_recent = None
    assert boost_runtime.native_value is None
    assert boost_runtime.extra_state_attributes is None


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
