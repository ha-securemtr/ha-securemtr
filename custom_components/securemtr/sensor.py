"""Sensors for Secure Meters runtime and statistics metadata."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, SecuremtrController, SecuremtrRuntimeData
from . import _format_weekly_program
from .entity import SecuremtrRuntimeEntityMixin, async_get_ready_controller
from .zones import ZONE_METADATA, ZoneMetadata

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Secure Meters sensors for boost and statistics."""

    entry_label = getattr(entry, "title", None) or getattr(entry, "entry_id", DOMAIN)
    _LOGGER.info("Starting SecureMTR sensor setup for %s", entry_label)

    try:
        runtime, controller = await async_get_ready_controller(hass, entry)
    except HomeAssistantError as error:
        _LOGGER.warning(
            "Skipping SecureMTR sensor setup for %s: %s",
            entry_label,
            error,
        )
        if isinstance(error.__cause__, TimeoutError):
            raise ConfigEntryNotReady(
                f"SecureMTR controller for {entry_label} is not ready"
            ) from error
        return

    _LOGGER.info(
        "Preparing SecureMTR sensor entities for %s using controller %s",
        entry_label,
        controller.identifier,
    )

    sensors: list[SecuremtrSensorEntity] = [
        SecuremtrBoostEndsSensor(runtime, controller, entry),
        SecuremtrWeeklyScheduleSensor(
            runtime,
            controller,
            entry,
            zone="primary",
            translation_key="primary_weekly_schedule",
        ),
        SecuremtrWeeklyScheduleSensor(
            runtime,
            controller,
            entry,
            zone="boost",
            translation_key="boost_weekly_schedule",
        ),
    ]
    _LOGGER.debug(
        "Prepared boost end-time sensor %s",
        sensors[0].unique_id,
    )

    for metadata in ZONE_METADATA.values():
        label = metadata.label
        energy_sensor = SecuremtrEnergyTotalSensor(
            runtime,
            controller,
            entry,
            metadata,
        )
        sensors.append(energy_sensor)
        _LOGGER.info(
            "Prepared SecureMTR %s energy sensor (entity_id=%s, unique_id=%s)",
            label,
            energy_sensor.entity_id,
            energy_sensor.unique_id,
        )

        for metric_key, translation_key in metadata.translation_keys.items():
            if metric_key == "energy":
                continue
            duration_sensor = SecuremtrDailyDurationSensor(
                runtime,
                controller,
                entry,
                metadata,
                metric_key,
                translation_key,
            )
            sensors.append(duration_sensor)
            _LOGGER.debug(
                "Prepared SecureMTR %s %s sensor (unique_id=%s)",
                label,
                metric_key,
                duration_sensor.unique_id,
            )

    async_add_entities(sensors)
    _LOGGER.info(
        "Registered %d SecureMTR sensor entities for %s",
        len(sensors),
        entry_label,
    )


class SecuremtrSensorEntity(SecuremtrRuntimeEntityMixin, SensorEntity):
    """Provide shared behaviour for Secure Meters sensors."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class: SensorDeviceClass | None = None
    _attr_state_class: SensorStateClass | None = None
    _attr_native_unit_of_measurement: str | None = None

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the sensor with runtime context and controller metadata."""

        super().__init__(runtime, controller, entry)

    @property
    def available(self) -> bool:
        """Return whether the backend is currently connected."""

        if self._runtime_connected():
            return True

        if self._runtime.controller is None:
            return False

        energy_state = self._runtime.energy_state
        if isinstance(energy_state, dict) and energy_state:
            return True

        return False

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return the assigned sensor device class."""

        return self._attr_device_class

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement for the sensor value."""

        return self._attr_native_unit_of_measurement

    @property
    def state_class(self) -> SensorStateClass | None:
        """Return the statistics state class for the sensor value."""

        return self._attr_state_class

    def _zone_payload(self, attr_name: str, zone: str) -> dict[str, object] | None:
        """Return the validated per-zone payload for the runtime attribute."""

        payload = getattr(self._runtime, attr_name, None)
        if not isinstance(payload, dict):
            return None
        zone_state = payload.get(zone)
        return zone_state if isinstance(zone_state, dict) else None


class SecuremtrBoostEndsSensor(SecuremtrSensorEntity):
    """Report the expected end time of the active boost run."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the boost end-time sensor."""

        super().__init__(runtime, controller, entry)
        self._set_slug_identifiers("boost_ends")
        self._attr_translation_key = "boost_ends"

    @property
    def native_value(self) -> datetime | None:
        """Return the boost end timestamp when active."""

        if self._runtime.timed_boost_active is not True:
            return None
        return self._runtime.timed_boost_end_time


class SecuremtrEnergyTotalSensor(SecuremtrSensorEntity):
    """Expose the cumulative energy total for a controller zone."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
        metadata: ZoneMetadata,
    ) -> None:
        """Initialise the energy total sensor for the requested zone."""

        super().__init__(runtime, controller, entry)
        self._metadata = metadata
        self._zone = metadata.key
        translation_key = metadata.translation_keys.get("energy")
        if translation_key:
            self._attr_translation_key = translation_key
        suffix = metadata.sensor_suffixes.get("energy")
        if suffix is None:
            suffix = f"{metadata.key}_energy_kwh"
        self._set_slug_identifiers(suffix, entity_domain="sensor")

    async def async_added_to_hass(self) -> None:
        """Register created energy sensors with runtime context."""

        await super().async_added_to_hass()
        runtime_ids = getattr(self._runtime, "energy_entity_ids", None)
        if isinstance(runtime_ids, dict):
            runtime_ids[self._zone] = self.entity_id

        hass = self.hass
        runtime_entry = getattr(self._runtime, "config_entry", None)
        if hass is not None and runtime_entry is not None:
            from . import _async_ensure_utility_meters  # noqa: PLC0415

            hass.async_create_task(_async_ensure_utility_meters(hass, runtime_entry))

    @property
    def native_value(self) -> float | None:
        """Return the cumulative energy total in kilowatt-hours."""

        zone_state = self._zone_payload("energy_state", self._zone)
        if not zone_state:
            return None
        energy_raw = zone_state.get("energy_sum")
        if isinstance(energy_raw, (int, float)):
            return float(energy_raw)
        return None

    # Statistics are imported directly into the recorder so the Energy dashboard
    # derives deltas from hourly sums. We still present TOTAL_INCREASING to make
    # the sensor selectable in the Energy UI.

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return metadata about the most recent statistic day."""

        zone_state = self._zone_payload("energy_state", self._zone)
        if not zone_state:
            return None
        last_day = zone_state.get("last_day")
        attributes: dict[str, object] = {}
        if isinstance(last_day, str):
            attributes["last_report_day"] = last_day
        series_start = zone_state.get("series_start")
        if isinstance(series_start, str):
            attributes["series_start_day"] = series_start
        offset = zone_state.get("offset_kwh")
        if isinstance(offset, (int, float)):
            attributes["offset_kwh"] = float(offset)
        return attributes or None


class SecuremtrWeeklyScheduleSensor(SecuremtrSensorEntity):
    """Expose a weekly schedule snapshot for one SecureMTR zone."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
        *,
        zone: str,
        translation_key: str,
    ) -> None:
        """Initialise the weekly schedule sensor for one zone."""

        super().__init__(runtime, controller, entry)
        self._zone = zone
        self._attr_translation_key = translation_key
        self._set_slug_identifiers(f"{zone}_weekly_schedule")

    @property
    def available(self) -> bool:
        """Return whether schedule data is cached or the runtime is connected."""

        if super().available:
            return True
        return self._program() is not None

    def _program(self):
        """Return the cached weekly program for the configured zone."""

        programs = self._runtime.weekly_programs
        if not isinstance(programs, dict):
            return None
        program = programs.get(self._zone)
        return program if isinstance(program, tuple) and len(program) == 7 else None

    @property
    def native_value(self) -> int | None:
        """Return the number of configured transitions for the weekly program."""

        program = self._program()
        if program is None:
            return None

        total = 0
        for day in program:
            total += len([minute for minute in day.on_minutes if minute is not None])
            total += len([minute for minute in day.off_minutes if minute is not None])
        return total

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return the cached weekly schedule and canonical interval metadata."""

        program = self._program()
        if program is None:
            return None

        attributes: dict[str, object] = {
            "schedule": _format_weekly_program(program),
        }

        canonicals = self._runtime.weekly_canonicals
        if isinstance(canonicals, dict):
            intervals = canonicals.get(self._zone)
            if isinstance(intervals, list):
                attributes["canonical_intervals"] = [
                    {"start_minute": start, "end_minute": end}
                    for start, end in intervals
                ]

        return attributes


class SecuremtrDailyDurationSensor(SecuremtrSensorEntity):
    """Expose the previous day's runtime or scheduled duration."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
        metadata: ZoneMetadata,
        metric: str,
        translation_key: str,
    ) -> None:
        """Initialise the daily duration sensor for the requested zone."""

        super().__init__(runtime, controller, entry)
        self._metadata = metadata
        self._zone = metadata.key
        self._metric = metric
        self._attr_translation_key = translation_key
        suffix = metadata.sensor_suffixes.get(metric)
        if suffix is None:
            suffix = f"{metadata.key}_{metric}_daily"
        self._set_slug_identifiers(suffix)

    @property
    def native_value(self) -> float | None:
        """Return the previous day's duration in hours."""

        zone_state = self._zone_payload("statistics_recent", self._zone)
        if not zone_state:
            return None

        key = f"{self._metric}_hours"
        value = zone_state.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return the report day and cumulative energy context."""

        zone_state = self._zone_payload("statistics_recent", self._zone)
        if not zone_state:
            return None

        attributes: dict[str, object] = {}
        report_day = zone_state.get("report_day")
        if isinstance(report_day, str):
            attributes["report_day"] = report_day
        energy_sum = zone_state.get("energy_sum")
        if isinstance(energy_sum, (int, float)):
            attributes["energy_total_kwh"] = float(energy_sum)
        return attributes or None


__all__ = [
    "SecuremtrBoostEndsSensor",
    "SecuremtrDailyDurationSensor",
    "SecuremtrEnergyTotalSensor",
    "SecuremtrWeeklyScheduleSensor",
]
