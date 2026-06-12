import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
)
from custom_components.securemtr.sensor import (
    SecuremtrBoostEndsSensor,
    SecuremtrDailyDurationSensor,
    SecuremtrEnergyTotalSensor,
    SecuremtrSensorEntity,
    SecuremtrWeeklyScheduleSensor,
    async_setup_entry,
)
from custom_components.securemtr.zones import ZONE_METADATA, ZoneMetadata
from custom_components.securemtr.beanbag import DailyProgram
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady


from tests.helpers import create_config_entry


SERIAL_SLUG = "serial_1"
PRIMARY_ENERGY_ENTITY_ID = f"sensor.securemtr_{SERIAL_SLUG}_primary_energy_kwh"
BOOST_ENERGY_ENTITY_ID = f"sensor.securemtr_{SERIAL_SLUG}_boost_energy_kwh"


class DummyBackend:
    """Provide backend stubs to satisfy the runtime interface."""

    async def read_device_metadata(
        self, *args: Any, **kwargs: Any
    ) -> None:  # pragma: no cover
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


def test_energy_sensor_suffix_defaults_when_missing() -> None:
    """Ensure the energy sensor falls back to default suffix strings."""

    runtime = _create_runtime()
    entry = create_config_entry(entry_id="entry")
    metadata = ZoneMetadata(
        key="fallback",
        label="Fallback",
        energy_field="fallback_energy_kwh",
        runtime_field="fallback_active_minutes",
        scheduled_field="fallback_scheduled_minutes",
        energy_suffix="fallback_energy_kwh",
        runtime_suffix="fallback_runtime_h",
        schedule_suffix="fallback_sched_h",
        translation_keys={"energy": "fallback_energy"},
        sensor_suffixes={},
    )

    sensor = SecuremtrEnergyTotalSensor(
        runtime,
        runtime.controller,
        entry,
        metadata,
    )

    assert sensor.unique_id == "serial_1_fallback_energy_kwh"
    assert sensor.entity_id == "sensor.securemtr_serial_1_fallback_energy_kwh"
    assert sensor.translation_key == "fallback_energy"


def test_duration_sensor_suffix_defaults_when_missing() -> None:
    """Ensure duration sensors fall back to default suffix strings."""

    runtime = _create_runtime()
    entry = create_config_entry(entry_id="entry")
    metadata = ZoneMetadata(
        key="fallback",
        label="Fallback",
        energy_field="fallback_energy_kwh",
        runtime_field="fallback_active_minutes",
        scheduled_field="fallback_scheduled_minutes",
        energy_suffix="fallback_energy_kwh",
        runtime_suffix="fallback_runtime_h",
        schedule_suffix="fallback_sched_h",
        translation_keys={
            "energy": "fallback_energy",
            "runtime": "fallback_runtime",
        },
        sensor_suffixes={"energy": "fallback_energy_kwh"},
    )

    sensor = SecuremtrDailyDurationSensor(
        runtime,
        runtime.controller,
        entry,
        metadata,
        "runtime",
        metadata.translation_keys["runtime"],
    )

    assert sensor.unique_id == "serial_1_fallback_runtime_daily"
    assert sensor.translation_key == "fallback_runtime"


def test_zone_payload_validates_per_zone_dicts() -> None:
    """Ensure the helper returns per-zone dict payloads when valid."""

    runtime = _create_runtime()
    runtime.energy_state = {
        "primary": {"energy_sum": 1.0},
        "boost": "invalid",
    }
    runtime.statistics_recent = {
        "primary": {"runtime_hours": 2.0},
    }
    entry = create_config_entry(entry_id="entry")
    sensor = SecuremtrEnergyTotalSensor(
        runtime,
        runtime.controller,
        entry,
        ZONE_METADATA["primary"],
    )

    assert sensor._zone_payload("energy_state", "primary") == {"energy_sum": 1.0}
    assert sensor._zone_payload("energy_state", "boost") is None
    assert sensor._zone_payload("energy_state", "missing") is None

    runtime.energy_state = None
    assert sensor._zone_payload("energy_state", "primary") is None

    assert sensor._zone_payload("statistics_recent", "primary") == {
        "runtime_hours": 2.0
    }

    runtime.statistics_recent = []  # type: ignore[assignment]
    assert sensor._zone_payload("statistics_recent", "primary") is None


@pytest.mark.asyncio
async def test_sensor_reports_end_time() -> None:
    """Ensure the sensor reports the boost end timestamp when active."""

    runtime = _create_runtime()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[SecuremtrSensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    sensor = next(
        entity for entity in entities if isinstance(entity, SecuremtrBoostEndsSensor)
    )
    assert sensor.unique_id == "serial_1_boost_ends"
    assert sensor.native_value is None
    assert sensor.device_info["name"] == "E7+ Smart Water Heater Controller"
    assert sensor.available is True
    assert sensor.device_class is SensorDeviceClass.TIMESTAMP
    assert sensor.device_class == "timestamp"
    assert sensor.has_entity_name is True

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
            "homeassistant.helpers.entity.Entity.async_added_to_hass",
            _fake_added,
        )
        mp.setattr(
            "custom_components.securemtr.entity.async_dispatcher_connect",
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
    runtime.controller = None
    assert sensor.available is False


@pytest.mark.asyncio
async def test_sensor_requires_controller() -> None:
    """Ensure setup skips entity creation when controller metadata is missing."""

    runtime = _create_runtime()
    runtime.controller = None
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    entities: list[SecuremtrSensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert entities == []


@pytest.mark.asyncio
async def test_energy_sensors_report_totals() -> None:
    """Ensure the energy sensors expose cumulative and daily values."""

    runtime = _create_runtime()
    runtime.energy_state = {
        "primary": {
            "energy_sum": 12.5,
            "last_day": "2024-03-01",
            "series_start": "2024-02-20",
            "offset_kwh": 0.0,
        },
        "boost": {
            "energy_sum": 4.75,
            "last_day": "2024-03-01",
            "series_start": "2024-02-22",
            "offset_kwh": 1.25,
        },
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
    entry = create_config_entry(entry_id="entry")
    entities: list[SecuremtrSensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert len(entities) == 3 + len(ZONE_METADATA) * 3
    sensors_by_id = {entity.unique_id: entity for entity in entities}

    primary_metadata = ZONE_METADATA["primary"]
    boost_metadata = ZONE_METADATA["boost"]

    primary_energy = sensors_by_id[
        f"{SERIAL_SLUG}_{primary_metadata.sensor_suffixes['energy']}"
    ]
    assert isinstance(primary_energy, SecuremtrEnergyTotalSensor)
    assert primary_energy.translation_key == primary_metadata.translation_keys["energy"]
    assert primary_energy.has_entity_name is True
    assert primary_energy.entity_id == PRIMARY_ENERGY_ENTITY_ID
    assert primary_energy.native_value == pytest.approx(12.5)
    assert primary_energy.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert primary_energy.device_class is SensorDeviceClass.ENERGY
    assert primary_energy.device_class == "energy"
    assert primary_energy.state_class is SensorStateClass.TOTAL_INCREASING
    assert primary_energy.extra_state_attributes == {
        "last_report_day": "2024-03-01",
        "series_start_day": "2024-02-20",
        "offset_kwh": 0.0,
    }
    device_info = primary_energy.device_info
    assert device_info["identifiers"] == {(DOMAIN, "serial-1")}
    assert device_info["manufacturer"] == "Secure Meters"

    runtime.energy_entity_ids = {}
    runtime.config_entry = create_config_entry(entry_id="entry")
    loop = asyncio.get_running_loop()
    primary_energy.hass = SimpleNamespace(
        async_create_task=lambda coro: loop.create_task(coro),
        data={},
        loop=loop,
    )

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "custom_components.securemtr._async_ensure_utility_meters",
            _noop,
        )
        await primary_energy.async_added_to_hass()
    assert runtime.energy_entity_ids["primary"] == PRIMARY_ENERGY_ENTITY_ID

    boost_energy = sensors_by_id[
        f"{SERIAL_SLUG}_{boost_metadata.sensor_suffixes['energy']}"
    ]
    assert isinstance(boost_energy, SecuremtrEnergyTotalSensor)
    assert boost_energy.translation_key == boost_metadata.translation_keys["energy"]
    assert boost_energy.has_entity_name is True
    assert boost_energy.entity_id == BOOST_ENERGY_ENTITY_ID
    assert boost_energy.native_value == pytest.approx(4.75)
    assert boost_energy.extra_state_attributes == {
        "last_report_day": "2024-03-01",
        "series_start_day": "2024-02-22",
        "offset_kwh": 1.25,
    }

    runtime.websocket = None
    assert primary_energy.available is True
    assert primary_energy.native_value == pytest.approx(12.5)
    assert boost_energy.available is True

    primary_runtime = sensors_by_id[
        f"{SERIAL_SLUG}_{primary_metadata.sensor_suffixes['runtime']}"
    ]
    assert isinstance(primary_runtime, SecuremtrDailyDurationSensor)
    assert primary_runtime.native_value == pytest.approx(3.25)
    assert (
        primary_runtime.translation_key == primary_metadata.translation_keys["runtime"]
    )
    assert primary_runtime.native_unit_of_measurement == UnitOfTime.HOURS
    assert primary_runtime.device_class is SensorDeviceClass.DURATION
    assert primary_runtime.device_class == "duration"
    assert primary_runtime.state_class is SensorStateClass.MEASUREMENT
    assert primary_runtime.state_class == "measurement"
    primary_runtime_attrs = primary_runtime.extra_state_attributes
    assert primary_runtime_attrs is not None
    assert primary_runtime_attrs["report_day"] == "2024-03-01"
    assert primary_runtime_attrs["energy_total_kwh"] == pytest.approx(12.5)

    boost_runtime = sensors_by_id[
        f"{SERIAL_SLUG}_{boost_metadata.sensor_suffixes['runtime']}"
    ]
    assert boost_runtime.native_value == pytest.approx(0.5)
    assert boost_runtime.translation_key == boost_metadata.translation_keys["runtime"]

    primary_scheduled = sensors_by_id[
        f"{SERIAL_SLUG}_{primary_metadata.sensor_suffixes['scheduled']}"
    ]
    assert primary_scheduled.native_value == pytest.approx(4.0)
    assert (
        primary_scheduled.translation_key
        == primary_metadata.translation_keys["scheduled"]
    )
    primary_scheduled_attrs = primary_scheduled.extra_state_attributes
    assert primary_scheduled_attrs is not None
    assert primary_scheduled_attrs["report_day"] == "2024-03-01"
    assert primary_scheduled_attrs["energy_total_kwh"] == pytest.approx(12.5)

    boost_scheduled = sensors_by_id[
        f"{SERIAL_SLUG}_{boost_metadata.sensor_suffixes['scheduled']}"
    ]
    assert (
        boost_scheduled.translation_key == boost_metadata.translation_keys["scheduled"]
    )

    runtime.energy_state = {"boost": {"energy_sum": "invalid", "last_day": 123}}
    assert boost_energy.native_value is None
    assert boost_energy.extra_state_attributes is None

    runtime.energy_state = None
    assert boost_energy.native_value is None
    assert boost_energy.extra_state_attributes is None

    runtime.statistics_recent = {"boost": {"runtime_hours": "invalid"}}
    assert boost_runtime.native_value is None

    runtime.statistics_recent = None
    assert boost_runtime.native_value is None
    assert boost_runtime.extra_state_attributes is None


@pytest.mark.asyncio
async def test_weekly_schedule_sensors_expose_cached_schedule() -> None:
    """Ensure weekly schedule sensors expose transition counts and payload attributes."""

    runtime = _create_runtime()
    runtime.weekly_programs = {
        "primary": (
            DailyProgram((285, None, None), (465, None, None)),
            DailyProgram((285, None, None), (465, None, None)),
            DailyProgram((285, None, None), (465, None, None)),
            DailyProgram((285, None, None), (465, None, None)),
            DailyProgram((285, None, None), (465, None, None)),
            DailyProgram((None, None, None), (None, None, None)),
            DailyProgram((None, None, None), (None, None, None)),
        ),
        "boost": (
            DailyProgram((105, None, None), (525, None, None)),
            DailyProgram((105, None, None), (525, None, None)),
            DailyProgram((105, None, None), (525, None, None)),
            DailyProgram((105, None, None), (525, None, None)),
            DailyProgram((105, None, None), (525, None, None)),
            DailyProgram((105, None, None), (525, None, None)),
            DailyProgram((105, None, None), (525, None, None)),
        ),
    }
    runtime.weekly_canonicals = {
        "primary": [(285, 465)],
        "boost": [(105, 525)],
    }

    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[SecuremtrSensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    schedule_sensors = [
        entity for entity in entities if isinstance(entity, SecuremtrWeeklyScheduleSensor)
    ]
    assert len(schedule_sensors) == 2

    primary_sensor = next(
        sensor for sensor in schedule_sensors if sensor.unique_id.endswith("primary_weekly_schedule")
    )
    assert primary_sensor.native_value == 10
    primary_attributes = primary_sensor.extra_state_attributes
    assert primary_attributes is not None
    schedule_payload = primary_attributes["schedule"]
    assert isinstance(schedule_payload, dict)
    assert schedule_payload["monday"]["on"] == ["04:45"]
    assert schedule_payload["monday"]["off"] == ["07:45"]

    boost_sensor = next(
        sensor for sensor in schedule_sensors if sensor.unique_id.endswith("boost_weekly_schedule")
    )
    assert boost_sensor.native_value == 14
    assert boost_sensor.available is True


@pytest.mark.asyncio
async def test_sensor_setup_times_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ensure setup skips sensor creation when controller metadata is delayed."""

    runtime = _create_runtime()
    runtime.controller_ready = asyncio.Event()
    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")

    monkeypatch.setattr(
        "custom_components.securemtr.entity._CONTROLLER_READY_TIMEOUT", 0.01
    )

    entities: list[SecuremtrSensorEntity] = []

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConfigEntryNotReady, match="not ready"):
            await async_setup_entry(hass, entry, entities.extend)

    assert entities == []
    assert "Skipping SecureMTR sensor setup" in caplog.text


@pytest.mark.asyncio
async def test_energy_sensor_available_with_cached_state() -> None:
    """Ensure energy totals remain available without an active websocket."""

    runtime = _create_runtime()
    runtime.websocket = None
    runtime.energy_state = {
        "primary": {
            "energy_sum": 9.5,
            "last_day": "2024-02-01",
            "series_start": "2024-01-01",
            "offset_kwh": 0.0,
        }
    }

    hass = SimpleNamespace(data={DOMAIN: {"entry": runtime}})
    entry = create_config_entry(entry_id="entry")
    entities: list[SecuremtrSensorEntity] = []

    await async_setup_entry(hass, entry, entities.extend)

    energy_sensors = [
        entity for entity in entities if isinstance(entity, SecuremtrEnergyTotalSensor)
    ]
    assert energy_sensors

    primary_energy = next(
        sensor
        for sensor in energy_sensors
        if sensor.unique_id.endswith("primary_energy_kwh")
    )
    assert primary_energy.available is True
    assert primary_energy.native_value == pytest.approx(9.5)
