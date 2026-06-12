"""Integration setup for securemtr water heater support."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from importlib import import_module
import logging
from types import MappingProxyType
from typing import Any, Literal, TYPE_CHECKING, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientSession, ClientWebSocketResponse
from homeassistant import config_entries as hass_config_entries
from homeassistant.components import recorder
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

try:  # pragma: no cover - HA 2025.10+ provides StatisticsTable
    from homeassistant.components.recorder.models import StatisticsTable
except ImportError:  # pragma: no cover - fallback for test environment
    from enum import StrEnum

    class StatisticsTable(StrEnum):
        """Fallback enum mirroring recorder.StatisticsTable."""

        STATISTICS = "statistics"


from homeassistant.components.recorder.statistics import StatisticMeanType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EMAIL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TIME_ZONE,
    EVENT_HOMEASSISTANT_CLOSE,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .beanbag import (
    BeanbagBackend,
    BeanbagEnergySample,
    BeanbagError,
    BeanbagGateway,
    BeanbagSession,
    BeanbagStateSnapshot,
    DailyProgram,
    WeeklyProgram,
)
from .energy import EnergyAccumulator
from .runtime_helpers import async_read_zone_programs
from .schedule import canonicalize_weekly
from .statistics import (
    PreparedSamples as PreparedSamples,
    StatisticsOptions as StatisticsOptions,
    ZoneContext as ZoneContext,
    ZoneProcessingResult as ZoneProcessingResult,
    _build_zone_calibrations as _build_zone_calibrations,
    _build_zone_contexts as _build_zone_contexts,
    _build_zone_statistics_samples as _build_zone_statistics_samples,
    _process_zone_records as _process_zone_records,
    _resolve_anchor as _resolve_anchor,
    _submit_statistics_samples as _submit_statistics_samples,
)
from .utils import (
    EnergyCalibration as EnergyCalibration,
    assign_report_day,
    safe_anchor_datetime as safe_anchor_datetime,
    split_runtime_segments as split_runtime_segments,
)
from .zones import ZONE_KEYS, ZONE_METADATA

if TYPE_CHECKING:
    from .local_ble_commissioning import LocalBlePriority, LocalBleWorker

DOMAIN = "securemtr"

CONF_CONNECTION_MODE = "connection_mode"
CONF_DEVICE_TYPE = "device_type"
CONF_LOCAL_BLE_KEY = "local_ble_key"
CONF_MAC_ADDRESS = "mac_address"
CONF_SERIAL_NUMBER = "serial_number"

CONNECTION_MODE_CLOUD = "cloud"
CONNECTION_MODE_LOCAL_BLE = "local_ble"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DEFAULT_DEVICE_LABEL = "E7+ Smart Water Heater Controller"

CONF_METER_DELTA_VALUES = "delta_values"
CONF_METER_NET_CONSUMPTION = "net_consumption"
CONF_METER_OFFSET = "offset"
CONF_METER_PERIODICALLY_RESETTING = "periodically_resetting"
CONF_METER_TYPE = "cycle"
CONF_SENSOR_ALWAYS_AVAILABLE = "always_available"
CONF_SOURCE_SENSOR = "source"
CONF_TARIFFS = "tariffs"
UTILITY_METER_DOMAIN = "utility_meter"
UTILITY_METER_CYCLES: tuple[str, ...] = ("daily", "weekly")
UTILITY_METER_ZONE_LABELS: dict[str, str] = {"primary": "Primary", "boost": "Boost"}

MODEL_ALIASES: dict[str, str] = {
    "2": DEFAULT_DEVICE_LABEL,
}

_RUNTIME_UPDATE_SIGNAL = "securemtr_runtime_update"

_LOGGER = logging.getLogger(__name__)

ENERGY_STORE_VERSION = 1
SERVICE_RESET_ENERGY = "reset_energy_accumulator"
SERVICE_SET_PRIMARY_WEEKLY_SCHEDULE = "set_primary_weekly_schedule"
SERVICE_SET_BOOST_WEEKLY_SCHEDULE = "set_boost_weekly_schedule"
SERVICE_START_TIMED_BOOST = "start_timed_boost"
ATTR_ENTRY_ID = "entry_id"
ATTR_DURATION_MINUTES = "duration_minutes"
ATTR_SCHEDULE = "schedule"
ATTR_ZONE = "zone"
_RESET_SERVICE_FLAG = "_reset_service_registered"
_REGISTERED_SERVICES_FLAG = "_registered_services"
_LOGIN_RETRY_DELAY = 5.0
_MAX_IMMEDIATE_STARTUP_RETRIES = 2
_LOCAL_BLE_REFRESH_INTERVAL_SECONDS = 60
_LOCAL_BLE_SNAPSHOT_COALESCE_KEY = "local_snapshot_refresh"
_LOCAL_BLE_WEEKLY_CACHE_COALESCE_KEY = "local_weekly_cache_refresh"
_SCHEDULE_WEEKDAY_KEYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


_ResultT = TypeVar("_ResultT")


def _parse_schedule_time(value: Any, *, day: str, field: str, index: int) -> int:
    """Parse one HH:MM transition time into minutes after midnight."""

    if not isinstance(value, str):
        raise HomeAssistantError(
            f"{day} {field}[{index}] must be a string in HH:MM format"
        )

    raw = value.strip()
    parts = raw.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise HomeAssistantError(
            f"{day} {field}[{index}] must use HH:MM 24-hour format"
        )

    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HomeAssistantError(
            f"{day} {field}[{index}] is out of range; expected 00:00-23:59"
        )

    total_minutes = hour * 60 + minute
    if total_minutes % 15 != 0:
        raise HomeAssistantError(
            f"{day} {field}[{index}] must align to 15-minute intervals"
        )
    return total_minutes


def _parse_daily_schedule(day: str, payload: Any) -> DailyProgram:
    """Validate one day schedule payload and return a DailyProgram."""

    if not isinstance(payload, Mapping):
        raise HomeAssistantError(
            f"{day} schedule must be a mapping with 'on' and 'off' lists"
        )

    unknown_keys = {str(key) for key in payload if key not in {"on", "off"}}
    if unknown_keys:
        raise HomeAssistantError(
            f"{day} schedule contains unsupported keys: {', '.join(sorted(unknown_keys))}"
        )

    on_raw = payload.get("on", [])
    off_raw = payload.get("off", [])
    if not isinstance(on_raw, list) or not isinstance(off_raw, list):
        raise HomeAssistantError(f"{day} schedule 'on' and 'off' must be lists")

    if len(on_raw) > 3 or len(off_raw) > 3:
        raise HomeAssistantError(
            f"{day} schedule supports at most 3 on and 3 off transitions"
        )

    on_minutes = [
        _parse_schedule_time(value, day=day, field="on", index=index)
        for index, value in enumerate(on_raw, start=1)
    ]
    off_minutes = [
        _parse_schedule_time(value, day=day, field="off", index=index)
        for index, value in enumerate(off_raw, start=1)
    ]

    all_minutes = on_minutes + off_minutes
    if not all_minutes:
        raise HomeAssistantError(
            f"{day} schedule must include at least one transition"
        )
    if len(all_minutes) > 6:
        raise HomeAssistantError(
            f"{day} schedule supports at most 6 transitions"
        )
    if len(set(all_minutes)) != len(all_minutes):
        raise HomeAssistantError(
            f"{day} schedule cannot contain duplicate transition times"
        )

    on_sorted = sorted(on_minutes)
    off_sorted = sorted(off_minutes)

    on_triplet = tuple(on_sorted + [None] * (3 - len(on_sorted)))
    off_triplet = tuple(off_sorted + [None] * (3 - len(off_sorted)))
    return DailyProgram(on_triplet, off_triplet)


def _parse_weekly_schedule(payload: Any) -> WeeklyProgram:
    """Validate and parse a weekly schedule service payload."""

    if not isinstance(payload, Mapping):
        raise HomeAssistantError("Schedule must be a mapping of weekday names")

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise HomeAssistantError("Schedule day keys must be strings")
        day_key = key.strip().lower()
        if day_key not in _SCHEDULE_WEEKDAY_KEYS:
            raise HomeAssistantError(
                f"Unsupported weekday '{key}'; expected Monday through Sunday"
            )
        if day_key in normalized:
            raise HomeAssistantError(f"Duplicate schedule day '{key}'")
        normalized[day_key] = value

    missing_days = [day for day in _SCHEDULE_WEEKDAY_KEYS if day not in normalized]
    if missing_days:
        missing = ", ".join(day.title() for day in missing_days)
        raise HomeAssistantError(f"Schedule is missing required days: {missing}")

    parsed_days = [
        _parse_daily_schedule(day.title(), normalized[day])
        for day in _SCHEDULE_WEEKDAY_KEYS
    ]

    return (
        parsed_days[0],
        parsed_days[1],
        parsed_days[2],
        parsed_days[3],
        parsed_days[4],
        parsed_days[5],
        parsed_days[6],
    )


def _format_transition_minutes(minutes: Iterable[int | None]) -> list[str]:
    """Convert transition minute offsets into HH:MM strings."""

    rendered: list[str] = []
    for minute in minutes:
        if minute is None:
            continue
        hour, remainder = divmod(minute, 60)
        rendered.append(f"{hour:02d}:{remainder:02d}")
    return rendered


def _format_weekly_program(program: WeeklyProgram) -> dict[str, dict[str, list[str]]]:
    """Format a WeeklyProgram into day-keyed on/off HH:MM lists."""

    rendered: dict[str, dict[str, list[str]]] = {}
    for day_key, daily in zip(_SCHEDULE_WEEKDAY_KEYS, program, strict=False):
        rendered[day_key] = {
            "on": _format_transition_minutes(daily.on_minutes),
            "off": _format_transition_minutes(daily.off_minutes),
        }
    return rendered


def _cache_weekly_program(runtime: SecuremtrRuntimeData, zone: str, program: WeeklyProgram) -> None:
    """Update runtime weekly-program caches for one zone."""

    programs = dict(runtime.weekly_programs or {})
    programs[zone] = program
    runtime.weekly_programs = programs

    canonicals = dict(runtime.weekly_canonicals or {})
    canonicals[zone] = canonicalize_weekly(program)
    runtime.weekly_canonicals = canonicals


def _cache_weekly_programs(
    runtime: SecuremtrRuntimeData,
    programs: Mapping[str, WeeklyProgram | None],
    canonicals: Mapping[str, list[tuple[int, int]] | None],
) -> None:
    """Store fetched weekly programs and canonical intervals in runtime."""

    runtime.weekly_programs = dict(programs)
    runtime.weekly_canonicals = dict(canonicals)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register SecureMTR domain services once per Home Assistant instance."""

    domain_data = hass.data.setdefault(DOMAIN, {})

    registered_raw = domain_data.get(_REGISTERED_SERVICES_FLAG)
    if isinstance(registered_raw, set):
        registered_services = set(registered_raw)
    elif isinstance(registered_raw, (list, tuple)):
        registered_services = {str(item) for item in registered_raw}
    else:
        registered_services: set[str] = set()

    def _service_exists(service_name: str) -> bool:
        """Return whether a service is already registered for this domain."""

        has_service = getattr(hass.services, "has_service", None)
        if callable(has_service):
            with suppress(Exception):
                return bool(has_service(DOMAIN, service_name))

        handlers = getattr(hass.services, "handlers", None)
        if isinstance(handlers, dict):
            return (DOMAIN, service_name) in handlers

        return False

    if (
        domain_data.get(_RESET_SERVICE_FLAG)
        and _service_exists(SERVICE_RESET_ENERGY)
    ):
        registered_services.add(SERVICE_RESET_ENERGY)

    reset_schema = vol.Schema(
        {
            vol.Optional(ATTR_ENTRY_ID): str,
            vol.Optional(ATTR_ZONE, default=ZONE_KEYS[0]): vol.In(ZONE_KEYS),
        }
    )
    start_boost_schema = vol.Schema(
        {
            vol.Optional(ATTR_ENTRY_ID): str,
            vol.Required(ATTR_DURATION_MINUTES): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=24 * 60)
            ),
        }
    )
    set_schedule_schema = vol.Schema(
        {
            vol.Optional(ATTR_ENTRY_ID): str,
            vol.Required(ATTR_SCHEDULE): dict,
        }
    )

    def _resolve_loaded_entry(
        call: ServiceCall,
        *,
        require_config_entry: bool = True,
    ) -> tuple[str, Any | None, SecuremtrRuntimeData]:
        """Resolve entry_id + runtime from explicit entry_id."""

        entry_id_raw = call.data.get(ATTR_ENTRY_ID)

        entry_id: str | None = None
        if isinstance(entry_id_raw, str) and entry_id_raw.strip():
            entry_id = entry_id_raw.strip()

        if entry_id is None:
            raise HomeAssistantError("entry_id is required for SecureMTR actions")

        domain_state = hass.data.get(DOMAIN, {})
        runtime: SecuremtrRuntimeData | None = domain_state.get(entry_id)
        if runtime is None:
            raise HomeAssistantError(
                f"SecureMTR entry {entry_id} is not loaded"
            )

        entry = runtime.config_entry
        if require_config_entry and (entry is None or not hasattr(entry, "data")):
            raise HomeAssistantError(
                f"SecureMTR entry {entry_id} config metadata is unavailable"
            )
        return entry_id, entry, runtime

    async def _async_handle_reset(call: ServiceCall) -> None:
        """Reset the cumulative energy state for a config entry zone."""

        entry_id, _entry, runtime = _resolve_loaded_entry(
            call,
            require_config_entry=False,
        )
        zone: str = call.data[ATTR_ZONE]

        accumulator = runtime.energy_accumulator
        if accumulator is None:
            store = runtime.energy_store
            if store is None:
                raise HomeAssistantError(
                    f"SecureMTR entry {entry_id} has no energy storage"
                )
            accumulator = EnergyAccumulator(store=store)
            runtime.energy_accumulator = accumulator

        await accumulator.async_load()
        await accumulator.async_reset_zone(zone)

        runtime.energy_state = accumulator.as_sensor_state()
        _LOGGER.info("Reset cumulative energy state for %s zone %s", entry_id, zone)
        async_dispatch_runtime_update(hass, entry_id)

    async def _async_handle_start_timed_boost(call: ServiceCall) -> None:
        """Start a timed boost for an arbitrary duration in minutes."""

        entry_id, entry, runtime = _resolve_loaded_entry(call)
        duration_minutes: int = call.data[ATTR_DURATION_MINUTES]
        controller = runtime.controller
        if controller is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        if runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
            from .local_ble_commissioning import (  # noqa: PLC0415
                LocalBleCommissioningError,
                LocalBlePriority,
            )

            try:
                worker = await async_get_local_ble_worker(hass, entry, runtime)
                await worker.async_execute_local_command(
                    method_name="start_timed_boost",
                    operation_kwargs={ATTR_DURATION_MINUTES: duration_minutes},
                    priority=LocalBlePriority.USER_COMMAND,
                )
            except (LocalBleCommissioningError, ValueError) as error:
                _LOGGER.error("Failed to start Secure Meters timed boost: %s", error)
                raise HomeAssistantError(
                    "Failed to start Secure Meters timed boost"
                ) from error
        else:

            async def _start_boost(
                backend: BeanbagBackend,
                session: BeanbagSession,
                websocket: ClientWebSocketResponse,
                active_controller: SecuremtrController,
            ) -> None:
                """Send timed-boost start using the cloud websocket transport."""

                await backend.start_timed_boost(
                    session,
                    websocket,
                    active_controller.gateway_id,
                    duration_minutes=duration_minutes,
                )

            await async_execute_controller_command(
                runtime,
                entry,
                _start_boost,
                log_context="Failed to start Secure Meters timed boost",
                exception_types=(BeanbagError, ValueError),
            )

        now_local = dt_util.now()
        end_local = now_local + timedelta(minutes=duration_minutes)
        end_minute = end_local.hour * 60 + end_local.minute
        runtime.timed_boost_active = True
        runtime.timed_boost_end_minute = end_minute
        runtime.timed_boost_end_time = coerce_end_time(end_minute)
        async_dispatch_runtime_update(hass, entry_id)

    async def _async_handle_set_weekly_schedule(
        call: ServiceCall,
        *,
        zone: Literal["primary", "boost"],
    ) -> None:
        """Persist a weekly schedule update for one zone."""

        entry_id, entry, runtime = _resolve_loaded_entry(call)
        schedule_payload = call.data[ATTR_SCHEDULE]
        controller = runtime.controller
        if controller is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        weekly_program = _parse_weekly_schedule(schedule_payload)

        if runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
            from .local_ble_commissioning import (  # noqa: PLC0415
                LocalBleCommissioningError,
                LocalBlePriority,
            )

            try:
                worker = await async_get_local_ble_worker(hass, entry, runtime)
                await worker.async_write_local_weekly_program(
                    zone=zone,
                    program=weekly_program,
                    zone_bois=runtime.schedule_zone_bois,
                    priority=LocalBlePriority.SCHEDULE_WRITE,
                )
            except (LocalBleCommissioningError, ValueError) as error:
                _LOGGER.error(
                    "Failed to update Secure Meters %s schedule: %s",
                    zone,
                    error,
                )
                raise HomeAssistantError(
                    f"Failed to update Secure Meters {zone} schedule"
                ) from error
        else:

            async def _write_weekly_program(
                backend: BeanbagBackend,
                session: BeanbagSession,
                websocket: ClientWebSocketResponse,
                active_controller: SecuremtrController,
            ) -> None:
                """Write one zone weekly schedule through cloud websocket."""

                await backend.write_weekly_program(
                    session,
                    websocket,
                    active_controller.gateway_id,
                    weekly_program,
                    zone=zone,
                )

            await async_execute_controller_command(
                runtime,
                entry,
                _write_weekly_program,
                log_context=f"Failed to update Secure Meters {zone} schedule",
                exception_types=(BeanbagError, ValueError),
            )

        _cache_weekly_program(runtime, zone, weekly_program)
        async_dispatch_runtime_update(hass, entry_id)

    async def _async_handle_set_primary_weekly_schedule(call: ServiceCall) -> None:
        """Update the primary zone weekly schedule."""

        await _async_handle_set_weekly_schedule(call, zone="primary")

    async def _async_handle_set_boost_weekly_schedule(call: ServiceCall) -> None:
        """Update the boost zone weekly schedule."""

        await _async_handle_set_weekly_schedule(call, zone="boost")

    service_specs: tuple[tuple[str, Callable[..., Any], vol.Schema], ...] = (
        (SERVICE_RESET_ENERGY, _async_handle_reset, reset_schema),
        (SERVICE_START_TIMED_BOOST, _async_handle_start_timed_boost, start_boost_schema),
        (
            SERVICE_SET_PRIMARY_WEEKLY_SCHEDULE,
            _async_handle_set_primary_weekly_schedule,
            set_schedule_schema,
        ),
        (
            SERVICE_SET_BOOST_WEEKLY_SCHEDULE,
            _async_handle_set_boost_weekly_schedule,
            set_schedule_schema,
        ),
    )

    for service_name, handler, schema in service_specs:
        if service_name in registered_services or _service_exists(service_name):
            registered_services.add(service_name)
            continue
        hass.services.async_register(
            DOMAIN,
            service_name,
            handler,
            schema=schema,
        )
        registered_services.add(service_name)

    domain_data[_REGISTERED_SERVICES_FLAG] = registered_services
    domain_data[_RESET_SERVICE_FLAG] = SERVICE_RESET_ENERGY in registered_services


async def _async_ensure_utility_meters(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create daily and weekly utility_meter helpers for the entry if needed."""

    entry_identifier = _entry_display_name(entry)
    config_entries_helper = getattr(hass, "config_entries", None)
    if config_entries_helper is None:
        _LOGGER.debug(
            "config_entries helper unavailable; skipping utility meter helpers for %s",
            entry_identifier,
        )
        return

    required_methods = ["async_entries", "async_add", "async_remove"]
    missing = [
        name for name in required_methods if not hasattr(config_entries_helper, name)
    ]
    if missing:
        _LOGGER.debug(
            "config_entries helper missing %s; skipping utility meter helpers for %s",
            ", ".join(missing),
            entry_identifier,
        )
        return

    try:
        helper_entries = list(config_entries_helper.async_entries(UTILITY_METER_DOMAIN))
    except Exception:  # pragma: no cover - defensive guard for helper behaviour
        _LOGGER.exception(
            "Unable to inspect existing utility meter helpers for %s",
            entry_identifier,
        )
        return

    existing_entries: dict[str, ConfigEntry] = {}
    existing_entry_ids: dict[str, ConfigEntry] = {}
    helpers_by_source: dict[tuple[str, str | None], list[ConfigEntry]] = {}

    for helper_entry in helper_entries:
        unique_id = getattr(helper_entry, "unique_id", None)
        if isinstance(unique_id, str):
            existing_entries[unique_id] = helper_entry

        entry_id = getattr(helper_entry, "entry_id", None)
        if isinstance(entry_id, str):
            existing_entry_ids[entry_id] = helper_entry

        options = getattr(helper_entry, "options", None)
        if isinstance(options, Mapping):
            source = options.get(CONF_SOURCE_SENSOR)
            if isinstance(source, str):
                cycle_option = options.get(CONF_METER_TYPE)
                cycle_key = cycle_option if isinstance(cycle_option, str) else None
                helpers_by_source.setdefault((source, cycle_key), []).append(
                    helper_entry
                )

    helper_identifier = _utility_meter_identifier(hass, entry)

    domain_state = hass.data.get(DOMAIN, {})
    runtime = domain_state.get(entry.entry_id)
    controller = getattr(runtime, "controller", None)
    energy_entity_ids = _energy_sensor_entity_ids(hass, entry, controller)

    for zone_key, zone_label in UTILITY_METER_ZONE_LABELS.items():
        source_entity = energy_entity_ids.get(zone_key)
        if source_entity is None:
            continue
        legacy_source = f"sensor.securemtr_{zone_key}_energy_kwh"
        fallback_source = (
            f"sensor.{DOMAIN}_{_controller_slug(entry, None)}_{zone_key}_energy_kwh"
        )
        source_candidates = [source_entity]
        if legacy_source != source_entity:
            source_candidates.append(legacy_source)
        if fallback_source not in source_candidates:
            source_candidates.append(fallback_source)
        for cycle in UTILITY_METER_CYCLES:
            unique_id = (
                f"securemtr_{helper_identifier}_{zone_key}_{cycle}_utility_meter"
            )
            entry_id = f"securemtr_um_{helper_identifier}_{zone_key}_{cycle}"

            source_helpers = _collect_candidate_helpers(
                helpers_by_source, source_candidates, cycle
            )

            seen_entries: set[str] = set()
            for legacy_entry in source_helpers:
                if legacy_entry.entry_id in seen_entries:
                    continue
                seen_entries.add(legacy_entry.entry_id)
                legacy_unique = getattr(legacy_entry, "unique_id", None)
                if legacy_entry.entry_id == entry_id and _helper_options_match(
                    legacy_entry, source_entity, cycle
                ):
                    _prune_helper_candidates(
                        helpers_by_source, source_candidates, cycle, legacy_entry
                    )
                    continue

                try:
                    await config_entries_helper.async_remove(legacy_entry.entry_id)
                except (
                    Exception
                ):  # pragma: no cover - defensive guard for helper writes
                    _LOGGER.exception(
                        "Failed to remove legacy utility meter helper %s for %s zone %s",
                        legacy_entry.entry_id,
                        entry_identifier,
                        zone_key,
                    )
                    continue

                if isinstance(legacy_unique, str):
                    existing_entries.pop(legacy_unique, None)
                existing_entry_ids.pop(legacy_entry.entry_id, None)

                _prune_helper_candidates(
                    helpers_by_source, source_candidates, cycle, legacy_entry
                )

                _LOGGER.info(
                    "Removed legacy utility meter helper %s for %s zone %s (source=%s)",
                    legacy_entry.entry_id,
                    entry_identifier,
                    zone_key,
                    source_entity,
                )

            existing_entry = existing_entries.get(unique_id)
            if existing_entry is None:
                existing_entry = existing_entry_ids.get(entry_id)
            if existing_entry is not None:
                if _helper_options_match(existing_entry, source_entity, cycle):
                    _LOGGER.debug(
                        "Utility meter helper already present for %s zone %s cycle %s",
                        entry_identifier,
                        zone_key,
                        cycle,
                    )
                    continue
                existing_entries.pop(unique_id, None)
                existing_entry_ids.pop(existing_entry.entry_id, None)

            meter_name = f"SecureMTR {zone_label} Energy {cycle.capitalize()}"
            options: dict[str, Any] = {
                CONF_NAME: meter_name,
                CONF_SOURCE_SENSOR: source_entity,
                CONF_METER_TYPE: cycle,
                CONF_METER_OFFSET: 0,
                CONF_TARIFFS: [],
                CONF_METER_NET_CONSUMPTION: False,
                CONF_METER_DELTA_VALUES: False,
                CONF_METER_PERIODICALLY_RESETTING: False,
                CONF_SENSOR_ALWAYS_AVAILABLE: False,
            }

            helper_entry = ConfigEntry(
                data={},
                domain=UTILITY_METER_DOMAIN,
                title=meter_name,
                version=2,
                minor_version=2,
                source=hass_config_entries.SOURCE_SYSTEM,
                unique_id=unique_id,
                options=options,
                discovery_keys=MappingProxyType({}),
                entry_id=entry_id,
                subentries_data=(),
            )

            try:
                await config_entries_helper.async_add(helper_entry)
            except (
                Exception
            ) as error:  # pragma: no cover - defensive guard for helper writes
                if _handle_helper_add_error(
                    error,
                    entry_identifier=entry_identifier,
                    zone_key=zone_key,
                    cycle=cycle,
                    entry_id=entry_id,
                    unique_id=unique_id,
                    existing_entries=existing_entries,
                    existing_entry_ids=existing_entry_ids,
                    helper_entry=helper_entry,
                ):
                    continue
                _LOGGER.exception(
                    "Failed to create %s utility meter helper for %s zone %s",
                    cycle,
                    entry_identifier,
                    zone_key,
                )
                continue

            existing_entries[unique_id] = helper_entry
            existing_entry_ids[entry_id] = helper_entry
            _LOGGER.info(
                "Created %s utility meter helper for %s zone %s (source=%s)",
                cycle,
                entry_identifier,
                zone_key,
                source_entity,
            )


def _handle_helper_add_error(
    error: Exception,
    *,
    entry_identifier: str,
    zone_key: str,
    cycle: str,
    entry_id: str,
    unique_id: str,
    existing_entries: dict[str, ConfigEntry],
    existing_entry_ids: dict[str, ConfigEntry],
    helper_entry: ConfigEntry,
) -> bool:
    """Handle helper creation errors that can be safely ignored."""

    if not isinstance(error, HomeAssistantError):
        return False

    existing_entry = existing_entry_ids.get(entry_id)
    if existing_entry is not None or "already exists" in str(error):
        _LOGGER.debug(
            "Utility meter helper already exists for %s zone %s cycle %s",
            entry_identifier,
            zone_key,
            cycle,
        )
        if existing_entry is not None:
            existing_entries.setdefault(unique_id, existing_entry)
        else:
            existing_entry_ids.setdefault(entry_id, helper_entry)
            existing_entries.setdefault(unique_id, helper_entry)
        return True

    return False


def _collect_candidate_helpers(
    helpers_by_source: dict[tuple[str, str | None], list[ConfigEntry]],
    source_candidates: Iterable[str],
    cycle: str,
) -> list[ConfigEntry]:
    """Return helper entries matching any of the provided source candidates."""

    collected: list[ConfigEntry] = []
    for candidate in source_candidates:
        helpers = helpers_by_source.get((candidate, cycle))
        if helpers:
            collected.extend(list(helpers))
        legacy_helpers = helpers_by_source.get((candidate, None))
        if legacy_helpers:
            collected.extend(list(legacy_helpers))
    return collected


def _helper_options_match(
    helper_entry: ConfigEntry, source_entity: str, cycle: str
) -> bool:
    """Return whether a helper entry already targets the requested source and cycle."""

    options = helper_entry.options
    meter_type = options.get(CONF_METER_TYPE)
    periodically_resetting = options.get(CONF_METER_PERIODICALLY_RESETTING)
    return (
        options.get(CONF_SOURCE_SENSOR) == source_entity
        and (meter_type == cycle or meter_type is None)
        and periodically_resetting is False
    )


def _prune_helper_candidates(
    helpers_by_source: dict[tuple[str, str | None], list[ConfigEntry]],
    source_candidates: Iterable[str],
    cycle: str,
    helper_entry: ConfigEntry,
) -> None:
    """Remove a helper entry from cached helper-by-source lookups."""

    for candidate in source_candidates:
        for helper_key in ((candidate, cycle), (candidate, None)):
            helpers = helpers_by_source.get(helper_key)
            if helpers is None:
                continue
            with suppress(ValueError):
                helpers.remove(helper_entry)
            if not helpers:
                helpers_by_source.pop(helper_key, None)


@dataclass(slots=True)
class SecuremtrRuntimeData:
    """Track runtime Beanbag backend state for a config entry."""

    backend: BeanbagBackend
    connection_mode: str = CONNECTION_MODE_CLOUD
    config_entry: ConfigEntry | None = None
    http_session: ClientSession | None = None
    session: BeanbagSession | None = None
    websocket: ClientWebSocketResponse | None = None
    startup_task: asyncio.Task[Any] | None = None
    retry_task: asyncio.Task[Any] | None = None
    controller: SecuremtrController | None = None
    controller_ready: asyncio.Event = field(default_factory=asyncio.Event)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    consumption_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    primary_power_on: bool | None = None
    timed_boost_enabled: bool | None = None
    timed_boost_active: bool | None = None
    timed_boost_end_minute: int | None = None
    timed_boost_end_time: datetime | None = None
    zone_topology: list[dict[str, Any]] | None = None
    schedule_overview: dict[str, Any] | None = None
    device_metadata: dict[str, Any] | None = None
    device_configuration: dict[str, Any] | None = None
    state_snapshot: BeanbagStateSnapshot | None = None
    consumption_metrics_log: list[dict[str, Any]] = field(default_factory=list)
    consumption_schedule_unsub: Callable[[], None] | None = None
    consumption_refresh_callback: Callable[[], None] | None = None
    consumption_refresh_pending: bool = False
    local_refresh_unsub: Callable[[], None] | None = None
    local_refresh_callback: Callable[[], None] | None = None
    local_ble_worker: "LocalBleWorker | None" = None
    energy_store: Store[dict[str, Any]] | None = None
    energy_state: dict[str, Any] | None = None
    energy_accumulator: EnergyAccumulator | None = None
    statistics_recent: dict[str, Any] | None = None
    schedule_zone_bois: dict[str, int] | None = None
    weekly_programs: dict[str, WeeklyProgram | None] | None = None
    weekly_canonicals: dict[str, list[tuple[int, int]] | None] | None = None
    energy_entity_ids: dict[str, str] = field(default_factory=dict)


def _entry_display_name(entry: ConfigEntry) -> str:
    """Return a non-sensitive identifier for a config entry."""

    title = getattr(entry, "title", None)
    if isinstance(title, str) and title.strip():
        return title

    entry_id = getattr(entry, "entry_id", None)
    if isinstance(entry_id, str) and entry_id.strip():
        return entry_id

    return DOMAIN


def _entry_connection_mode(entry: ConfigEntry) -> str:
    """Return the configured connection mode for an entry."""

    mode = entry.data.get(CONF_CONNECTION_MODE)
    if isinstance(mode, str) and mode.strip():
        return mode.strip().lower()
    return CONNECTION_MODE_CLOUD


def _build_local_controller(entry: ConfigEntry) -> SecuremtrController:
    """Build a placeholder controller model for local BLE entries."""

    serial_raw = entry.data.get(CONF_SERIAL_NUMBER)
    serial_number = serial_raw.strip() if isinstance(serial_raw, str) else None
    if serial_number == "":
        serial_number = None

    mac_raw = entry.data.get(CONF_MAC_ADDRESS)
    mac_address = mac_raw.strip().upper() if isinstance(mac_raw, str) else ""

    identifier = serial_number or mac_address or entry.entry_id
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    model = str(device_type).strip().upper() if isinstance(device_type, str) else "E7+"

    return SecuremtrController(
        identifier=identifier,
        name=DEFAULT_DEVICE_LABEL,
        gateway_id=mac_address or identifier,
        serial_number=serial_number,
        model=model,
    )


async def async_get_local_ble_worker(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
) -> "LocalBleWorker":
    """Return a configured local BLE worker for one runtime entry."""

    if runtime.connection_mode != CONNECTION_MODE_LOCAL_BLE:
        raise HomeAssistantError("SecureMTR entry is not configured for local BLE")

    controller = runtime.controller
    if controller is None:
        raise HomeAssistantError("Secure Meters controller is not connected")

    ble_key = entry.data.get(CONF_LOCAL_BLE_KEY)
    if not isinstance(ble_key, str) or not ble_key:
        raise HomeAssistantError("Local BLE key is missing for this entry")

    from .local_ble_commissioning import LocalBleWorker  # noqa: PLC0415

    existing = runtime.local_ble_worker
    if existing is not None and existing.matches(
        mac_address=controller.gateway_id,
        serial_number=controller.serial_number,
        ble_key=ble_key,
    ):
        return existing

    if existing is not None:
        await existing.async_close()

    try:
        worker = LocalBleWorker(
            hass,
            mac_address=controller.gateway_id,
            serial_number=controller.serial_number,
            ble_key=ble_key,
        )
    except ValueError as error:
        raise HomeAssistantError(
            "Local BLE MAC address is invalid for this entry"
        ) from error
    runtime.local_ble_worker = worker
    return worker


def _utility_meter_identifier(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return a slug for utility meter helper identifiers."""

    candidate: str | None = None
    domain_state = hass.data.get(DOMAIN, {}) if hass is not None else {}
    runtime: SecuremtrRuntimeData | None = None
    if isinstance(domain_state, dict):
        runtime = domain_state.get(entry.entry_id)
    if runtime is not None:
        controller = getattr(runtime, "controller", None)
        if controller is not None:
            serial_number = getattr(controller, "serial_number", None)
            if isinstance(serial_number, str) and serial_number.strip():
                candidate = serial_number.strip()
            elif (
                isinstance(controller.identifier, str) and controller.identifier.strip()
            ):
                candidate = controller.identifier.strip()

    data = getattr(entry, "data", None)
    if candidate is None and isinstance(data, dict):
        raw_serial = data.get("serial_number")
        if isinstance(raw_serial, str) and raw_serial.strip():
            candidate = raw_serial.strip()

    if candidate is None:
        unique_id = getattr(entry, "unique_id", None)
        if isinstance(unique_id, str) and unique_id.strip():
            candidate = unique_id.strip()
    if candidate is None:
        entry_id = getattr(entry, "entry_id", None)
        if isinstance(entry_id, str) and entry_id.strip():
            candidate = entry_id.strip()
    if candidate is None:
        candidate = DOMAIN

    slug = slugify_identifier(candidate)
    return slug or DOMAIN


def _invoke_refresh_callback(
    callback: Callable[[], None], entry_identifier: str
) -> None:
    """Execute a refresh callback, logging any unexpected failures."""

    try:
        callback()
    except Exception:
        _LOGGER.exception(
            "Error while executing securemtr refresh callback for %s", entry_identifier
        )


def _controller_slug(entry: ConfigEntry, controller: SecuremtrController | None) -> str:
    """Return the identifier slug for a controller or entry metadata."""

    candidate: str | None = None
    if controller is not None:
        candidate = controller.serial_number or controller.identifier

    if not candidate:
        data = getattr(entry, "data", None)
        if isinstance(data, Mapping):
            raw_serial = data.get("serial_number")
            if isinstance(raw_serial, str) and raw_serial.strip():
                candidate = raw_serial.strip()

    if not candidate:
        unique_id = getattr(entry, "unique_id", None)
        if isinstance(unique_id, str) and unique_id.strip():
            candidate = unique_id.strip()

    if not candidate:
        entry_id = getattr(entry, "entry_id", None)
        if isinstance(entry_id, str) and entry_id.strip():
            candidate = entry_id.strip()

    if not candidate:
        candidate = DOMAIN

    return slugify_identifier(candidate)


def _energy_sensor_entity_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    controller: SecuremtrController | None,
) -> dict[str, str]:
    """Return the energy sensor entity IDs for each controller zone."""

    registry = er.async_get(hass)
    entity_ids: dict[str, str] = {}
    suffixes = {zone: f"_{zone}_energy_kwh" for zone in ZONE_KEYS}

    domain_state = hass.data.get(DOMAIN, {})
    runtime = domain_state.get(entry.entry_id)
    if runtime is not None:
        runtime_ids = getattr(runtime, "energy_entity_ids", None)
        if isinstance(runtime_ids, dict):
            entity_ids.update(
                {
                    zone: entity_id
                    for zone, entity_id in runtime_ids.items()
                    if zone in suffixes and isinstance(entity_id, str)
                }
            )

    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if getattr(reg_entry, "platform", None) != DOMAIN:
            continue
        unique_id = getattr(reg_entry, "unique_id", None)
        entity_id = getattr(reg_entry, "entity_id", None)
        if not isinstance(unique_id, str) or not isinstance(entity_id, str):
            continue
        for zone, suffix in suffixes.items():
            if unique_id.endswith(suffix):
                entity_ids[zone] = entity_id

    slug = _controller_slug(entry, controller)
    for zone, suffix in suffixes.items():
        entity_ids.setdefault(zone, f"sensor.{DOMAIN}_{slug}{suffix}")

    return entity_ids


def async_get_clientsession(hass: HomeAssistant) -> ClientSession:
    """Return a dedicated aiohttp session for SecureMTR backend calls."""

    session = ClientSession()

    bus = getattr(hass, "bus", None)
    if bus is not None and hasattr(bus, "async_listen_once"):

        async def _close_session(_event: Any) -> None:
            """Close the backend session when Home Assistant shuts down."""

            await _async_close_client_session(session)

        bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _close_session)

    return session


async def _async_close_client_session(session: Any) -> None:
    """Close an aiohttp-style session when possible."""

    closed = getattr(session, "closed", None)
    if isinstance(closed, bool) and closed:
        return

    closer = getattr(session, "close", None)
    if callable(closer):
        with suppress(Exception):  # pragma: no cover - defensive guard
            result = closer()
            if isinstance(result, Awaitable):
                await result
        return

    async_closer = getattr(session, "async_close", None)
    if callable(async_closer):
        with suppress(Exception):  # pragma: no cover - defensive guard
            result = async_closer()
            if isinstance(result, Awaitable):
                await result


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the securemtr integration."""
    _LOGGER.info("Starting securemtr integration setup")
    hass.data.setdefault(DOMAIN, {})
    _async_register_services(hass)
    _LOGGER.info("securemtr integration setup completed")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up securemtr from a config entry."""
    entry_identifier = _entry_display_name(entry)
    _LOGGER.info("Setting up config entry for securemtr: %s", entry_identifier)

    hass.data.setdefault(DOMAIN, {})
    _async_register_services(hass)

    session = async_get_clientsession(hass)
    runtime = SecuremtrRuntimeData(
        backend=BeanbagBackend(session), http_session=session
    )
    runtime.connection_mode = _entry_connection_mode(entry)
    runtime.config_entry = entry
    hass.data[DOMAIN][entry.entry_id] = runtime

    if not hasattr(entry, "options"):
        setattr(entry, "options", {})

    runtime.energy_store = Store(
        hass,
        ENERGY_STORE_VERSION,
        _energy_store_key(entry),
    )

    if runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
        runtime.controller = _build_local_controller(entry)
        runtime.controller_ready.set()

        accumulator = await _ensure_energy_accumulator(hass, entry, runtime)
        runtime.energy_state = accumulator.as_sensor_state()

        def _queue_local_refresh() -> None:
            """Schedule one asynchronous local BLE snapshot refresh."""

            async def _async_safe_local_refresh() -> None:
                """Run one local BLE refresh and absorb unexpected task errors."""

                try:
                    await _async_refresh_local_ble_runtime(hass, entry)
                except Exception:
                    _LOGGER.exception(
                        "Unexpected error during local BLE refresh for %s",
                        entry_identifier,
                    )

            def _schedule() -> None:
                hass.async_create_task(_async_safe_local_refresh())

            loop = getattr(hass, "loop", None)
            if loop is None:
                hass.async_create_task(_async_refresh_local_ble_runtime(hass, entry))
                return

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is loop:
                _schedule()
            else:
                loop.call_soon_threadsafe(_schedule)

        def _scheduled_local_refresh(_now: datetime) -> None:
            """Trigger the interval-based local BLE snapshot refresh."""

            _queue_local_refresh()

        runtime.local_refresh_unsub = async_track_time_interval(
            hass,
            _scheduled_local_refresh,
            timedelta(seconds=_LOCAL_BLE_REFRESH_INTERVAL_SECONDS),
        )
        runtime.local_refresh_callback = _queue_local_refresh
        hass.async_create_task(_async_refresh_weekly_program_cache(hass, entry))

        _LOGGER.info(
            "Configured local BLE mode for %s (serial=%s)",
            entry_identifier,
            runtime.controller.serial_number,
        )
    else:
        statistics_options = _load_statistics_options(entry)
        schedule_timezone = statistics_options.timezone
        schedule_timezone_name = statistics_options.timezone_name

        runtime.startup_task = hass.async_create_task(
            _async_start_backend(hass, entry, runtime)
        )

        def _queue_consumption_refresh() -> None:
            """Schedule the asynchronous consumption metrics task safely."""

            def _schedule() -> None:
                hass.async_create_task(consumption_metrics(hass, entry))

            loop = getattr(hass, "loop", None)
            if loop is None:
                hass.async_create_task(consumption_metrics(hass, entry))
                return

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is loop:
                _schedule()
            else:
                loop.call_soon_threadsafe(_schedule)

        def _next_consumption_refresh(reference: datetime | None = None) -> datetime:
            """Return the next 01:00 UTC instant for the configured timezone."""

            base = reference or dt_util.utcnow()
            if base.tzinfo is None:
                base = base.replace(tzinfo=dt_util.UTC)
            base_utc = dt_util.as_utc(base)
            local_now = base_utc.astimezone(schedule_timezone)
            target_local = local_now.replace(hour=1, minute=0, second=0, microsecond=0)
            if local_now >= target_local:
                next_day = local_now.date() + timedelta(days=1)
                target_local = datetime.combine(
                    next_day,
                    time(1, 0, tzinfo=schedule_timezone),
                )
            return target_local.astimezone(dt_util.UTC)

        schedule_unsub: Callable[[], None] | None = None

        def _schedule_next_refresh(reference: datetime | None = None) -> None:
            """Install a one-shot timer for the next consumption refresh."""

            nonlocal schedule_unsub

            if schedule_unsub is not None:
                with suppress(Exception):
                    schedule_unsub()
                schedule_unsub = None

            next_refresh = _next_consumption_refresh(reference)
            _LOGGER.debug(
                "Scheduling consumption metrics refresh for %s at %s (%s)",
                entry_identifier,
                next_refresh.isoformat(),
                schedule_timezone_name,
            )
            schedule_unsub = async_track_point_in_time(
                hass,
                _scheduled_consumption_refresh,
                next_refresh,
            )

        def _scheduled_consumption_refresh(now: datetime) -> None:
            """Trigger the scheduled consumption metrics task."""

            _LOGGER.debug(
                "Scheduled consumption metrics refresh triggered for %s",
                entry_identifier,
            )
            _queue_consumption_refresh()
            _schedule_next_refresh(now + timedelta(seconds=1))

        _schedule_next_refresh()

        def _unsubscribe_schedules() -> None:
            """Cancel every scheduled consumption metrics callback."""

            nonlocal schedule_unsub

            if schedule_unsub is not None:
                with suppress(Exception):
                    schedule_unsub()
                schedule_unsub = None

        runtime.consumption_schedule_unsub = _unsubscribe_schedules
        runtime.consumption_refresh_callback = _queue_consumption_refresh

        _LOGGER.debug(
            "Queuing immediate consumption metrics refresh for %s", entry_identifier
        )
        _queue_consumption_refresh()

    config_entries_helper = getattr(hass, "config_entries", None)
    if config_entries_helper is not None:
        await config_entries_helper.async_forward_entry_setups(entry, ["switch"])
        await config_entries_helper.async_forward_entry_setups(
            entry, ["button", "binary_sensor", "sensor"]
        )
    else:
        _LOGGER.debug(
            "config_entries helper unavailable; skipping platform setup for %s",
            entry_identifier,
        )

    await _async_ensure_utility_meters(hass, entry)

    if (
        runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE
        and runtime.local_refresh_callback is not None
    ):
        runtime.local_refresh_callback()

    _LOGGER.info("Config entry setup completed for securemtr: %s", entry_identifier)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a securemtr config entry."""
    entry_identifier = _entry_display_name(entry)
    _LOGGER.info("Unloading securemtr config entry: %s", entry_identifier)

    hass.data.setdefault(DOMAIN, {})
    runtime: SecuremtrRuntimeData | None = hass.data[DOMAIN].pop(entry.entry_id, None)

    config_entries_helper = getattr(hass, "config_entries", None)
    if config_entries_helper is not None:
        unload_ok = await config_entries_helper.async_unload_platforms(
            entry, ["switch", "button", "binary_sensor", "sensor"]
        )
    else:
        unload_ok = True
        _LOGGER.debug(
            "config_entries helper unavailable; skipping platform unload for %s",
            entry_identifier,
        )

    if runtime is None:
        _LOGGER.info("securemtr config entry unloaded: %s", entry_identifier)
        return unload_ok

    if runtime.consumption_schedule_unsub is not None:
        try:
            runtime.consumption_schedule_unsub()
        except Exception:
            _LOGGER.exception(
                "Error while unsubscribing scheduled consumption refresh for %s",
                entry_identifier,
            )
        finally:
            runtime.consumption_schedule_unsub = None
    runtime.consumption_refresh_callback = None

    if runtime.local_refresh_unsub is not None:
        try:
            runtime.local_refresh_unsub()
        except Exception:
            _LOGGER.exception(
                "Error while unsubscribing local BLE refresh for %s",
                entry_identifier,
            )
        finally:
            runtime.local_refresh_unsub = None
    runtime.local_refresh_callback = None

    if runtime.local_ble_worker is not None:
        try:
            await runtime.local_ble_worker.async_close()
        except Exception:
            _LOGGER.exception(
                "Error while stopping local BLE worker for %s",
                entry_identifier,
            )
        finally:
            runtime.local_ble_worker = None

    if runtime.startup_task is not None and not runtime.startup_task.done():
        runtime.startup_task.cancel()
        with suppress(asyncio.CancelledError):
            await runtime.startup_task

    retry_task = runtime.retry_task
    if retry_task is not None and not retry_task.done():
        _LOGGER.debug(
            "Cancelling pending Beanbag backend retry task for %s", entry_identifier
        )
        retry_task.cancel()
        with suppress(asyncio.CancelledError):
            await retry_task
    runtime.retry_task = None

    if runtime.websocket is not None and not runtime.websocket.closed:
        await runtime.websocket.close()

    if runtime.http_session is not None:
        await _async_close_client_session(runtime.http_session)
        runtime.http_session = None

    _LOGGER.info("securemtr config entry unloaded: %s", entry_identifier)
    return unload_ok


async def _async_start_backend(
    hass: HomeAssistant, entry: ConfigEntry, runtime: SecuremtrRuntimeData
) -> None:
    """Authenticate with Beanbag and establish the WebSocket connection."""

    runtime.controller_ready.clear()

    email: str = entry.data.get(CONF_EMAIL, "").strip()
    password_digest: str = entry.data.get(CONF_PASSWORD, "")
    entry_identifier = _entry_display_name(entry)

    if not email or not password_digest:
        _LOGGER.error("Missing credentials for securemtr entry %s", entry_identifier)
        return

    _LOGGER.info("Starting Beanbag backend for %s", entry_identifier)

    try:
        outcome = await _async_attempt_backend_startup(
            entry,
            runtime,
            email=email,
            password_digest=password_digest,
            entry_identifier=entry_identifier,
        )
        if outcome == "success":
            await _async_handle_backend_success(
                hass,
                entry,
                runtime,
                runtime.consumption_refresh_callback,
            )
            return

        if outcome == "retry" and _LOGIN_RETRY_DELAY <= 0:
            for _ in range(_MAX_IMMEDIATE_STARTUP_RETRIES):
                runtime.controller_ready.clear()
                outcome = await _async_attempt_backend_startup(
                    entry,
                    runtime,
                    email=email,
                    password_digest=password_digest,
                    entry_identifier=entry_identifier,
                )
                if outcome != "retry":
                    break

            if outcome == "success":
                await _async_handle_backend_success(
                    hass,
                    entry,
                    runtime,
                    runtime.consumption_refresh_callback,
                )
                return

            if outcome == "abort":
                _LOGGER.error(
                    "Aborting Beanbag backend startup for %s due to unexpected error",
                    entry_identifier,
                )
                return

        if outcome == "retry":
            _async_queue_backend_retry(
                hass,
                entry,
                runtime,
                entry_identifier,
                on_success=runtime.consumption_refresh_callback,
            )
            return

        _LOGGER.error(
            "Aborting Beanbag backend startup for %s due to unexpected error",
            entry_identifier,
        )
    except asyncio.CancelledError:
        _LOGGER.info("Beanbag backend startup cancelled for %s", entry_identifier)
        raise


async def _async_attempt_backend_startup(
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    *,
    email: str,
    password_digest: str,
    entry_identifier: str,
) -> Literal["success", "retry", "abort"]:
    """Attempt a single Beanbag login and controller discovery cycle."""

    try:
        session, websocket = await runtime.backend.login_and_connect(
            email, password_digest
        )
    except BeanbagError as error:
        _LOGGER.error(
            "Failed to initialize Beanbag backend for %s: %s",
            entry_identifier,
            error,
        )
        return "retry"
    except Exception:
        _LOGGER.exception(
            "Unexpected error while initializing Beanbag backend for %s",
            entry_identifier,
        )
        return "abort"

    runtime.session = session
    runtime.websocket = websocket

    try:
        controller = await _async_fetch_controller(entry, runtime)
    except BeanbagError as error:
        _LOGGER.error(
            "Unable to fetch securemtr controller details for %s: %s",
            entry_identifier,
            error,
        )
        result: Literal["retry", "abort"] = "retry"
    except Exception:
        _LOGGER.exception(
            "Unexpected error while fetching securemtr controller for %s",
            entry_identifier,
        )
        result = "abort"
    else:
        runtime.controller = controller
        _LOGGER.info(
            "Discovered securemtr controller %s (%s)",
            controller.identifier,
            controller.name,
        )
        return "success"

    if result == "retry":
        await _async_reset_connection(runtime)
        runtime.session = None
    runtime.controller = None
    return result


def _async_queue_backend_retry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    entry_identifier: str,
    *,
    on_success: Callable[[], None] | None = None,
) -> None:
    """Schedule backend retries and call a hook after successful reconnection."""

    if runtime.retry_task is not None and not runtime.retry_task.done():
        _LOGGER.debug(
            "Skipping backend retry scheduling for %s because a task is already active",
            entry_identifier,
        )
        return

    runtime.controller_ready.clear()

    async def _async_retry() -> None:
        """Retry backend initialisation until successful or cancelled."""

        try:
            while True:
                await asyncio.sleep(_LOGIN_RETRY_DELAY)

                email = entry.data.get(CONF_EMAIL, "").strip()
                password_digest = entry.data.get(CONF_PASSWORD, "")

                if not email or not password_digest:
                    _LOGGER.error(
                        "Missing credentials for securemtr entry %s during retry",
                        entry_identifier,
                    )
                    return

                outcome = await _async_attempt_backend_startup(
                    entry,
                    runtime,
                    email=email,
                    password_digest=password_digest,
                    entry_identifier=entry_identifier,
                )
                if outcome == "success":
                    await _async_handle_backend_success(
                        hass,
                        entry,
                        runtime,
                        on_success,
                        after_retry=True,
                    )
                    return
                if outcome == "abort":
                    _LOGGER.error(
                        "Stopping Beanbag backend retries for %s due to unexpected error",
                        entry_identifier,
                    )
                    return
                _LOGGER.info(
                    "Retrying Beanbag backend startup for %s in %.1f seconds",
                    entry_identifier,
                    _LOGIN_RETRY_DELAY,
                )
        except asyncio.CancelledError:
            _LOGGER.info(
                "Beanbag backend retry cancelled for %s",
                entry_identifier,
            )
            raise
        finally:
            runtime.retry_task = None

    runtime.retry_task = hass.async_create_task(_async_retry())


async def _async_handle_backend_success(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    refresh_callback: Callable[[], None] | None = None,
    *,
    after_retry: bool = False,
) -> None:
    """Complete backend startup and trigger optional refresh callbacks."""

    await _async_ensure_utility_meters(hass, entry)
    entry_identifier = _entry_display_name(entry)
    suffix = " after retry" if after_retry else ""
    _LOGGER.info("Beanbag backend connected for %s%s", entry_identifier, suffix)
    if refresh_callback is not None and runtime.consumption_refresh_pending:
        _invoke_refresh_callback(refresh_callback, entry_identifier)
    runtime.controller_ready.set()


async def _async_refresh_connection(
    entry: ConfigEntry, runtime: SecuremtrRuntimeData
) -> bool:
    """Ensure the Beanbag WebSocket connection is available."""

    session = runtime.session
    websocket = runtime.websocket

    if session is not None and websocket is not None and not websocket.closed:
        return True

    email: str = entry.data.get(CONF_EMAIL, "").strip()
    password_digest: str = entry.data.get(CONF_PASSWORD, "")
    entry_identifier = _entry_display_name(entry)

    if not email or not password_digest:
        _LOGGER.error(
            "Missing credentials for securemtr entry %s during reconnection",
            entry_identifier,
        )
        return False

    try:
        session, websocket = await runtime.backend.login_and_connect(
            email, password_digest
        )
    except BeanbagError as error:
        _LOGGER.error(
            "Failed to refresh Beanbag connection for %s: %s",
            entry_identifier,
            error,
        )
        return False

    runtime.session = session
    runtime.websocket = websocket
    _LOGGER.info("Re-established Beanbag connection for %s", entry_identifier)
    return True


async def _async_reset_connection(runtime: SecuremtrRuntimeData) -> None:
    """Close the active Beanbag WebSocket and clear it from the runtime."""

    websocket = runtime.websocket
    if websocket is not None and not websocket.closed:
        await websocket.close()

    runtime.websocket = None


async def async_run_with_reconnect(
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    operation: Callable[
        [BeanbagBackend, BeanbagSession, ClientWebSocketResponse],
        Awaitable[_ResultT],
    ],
) -> _ResultT:
    """Execute a backend operation, retrying once after reconnecting."""

    if not await _async_refresh_connection(entry, runtime):
        raise BeanbagError("Beanbag connection is unavailable")

    last_error: BeanbagError | None = None
    for attempt in range(2):
        session = runtime.session
        websocket = runtime.websocket

        if session is None or websocket is None:
            raise BeanbagError("Beanbag session or websocket is unavailable")

        try:
            return await operation(runtime.backend, session, websocket)
        except BeanbagError as error:
            last_error = error
            if attempt == 1:
                break

            _LOGGER.warning(
                "Beanbag backend operation failed; attempting reconnection: %s",
                error,
            )
            await _async_reset_connection(runtime)
            if not await _async_refresh_connection(entry, runtime):
                break

    assert last_error is not None
    raise last_error


async def async_execute_controller_command(
    runtime: SecuremtrRuntimeData,
    entry: ConfigEntry,
    operation: Callable[
        [BeanbagBackend, BeanbagSession, ClientWebSocketResponse, SecuremtrController],
        Awaitable[_ResultT],
    ],
    *,
    log_context: str,
    error_message: str | None = None,
    exception_types: tuple[type[Exception], ...] = (BeanbagError,),
) -> _ResultT:
    """Run a controller-scoped Beanbag command with common safeguards."""

    controller = runtime.controller
    if controller is None:
        raise HomeAssistantError("Secure Meters controller is not connected")

    async with runtime.command_lock:
        try:
            return await async_run_with_reconnect(
                entry,
                runtime,
                lambda backend, session, websocket: operation(
                    backend,
                    session,
                    websocket,
                    controller,
                ),
            )
        except exception_types as error:
            _LOGGER.error("%s: %s", log_context, error)
            raise HomeAssistantError(error_message or log_context) from error


async def _async_fetch_energy_samples(
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    controller: SecuremtrController,
    entry_identifier: str,
) -> list[BeanbagEnergySample] | None:
    """Fetch energy history for the active controller with reconnection."""

    gateway_id = controller.gateway_id

    async def _read_energy_history(
        backend: BeanbagBackend,
        active_session: BeanbagSession,
        active_websocket: ClientWebSocketResponse,
    ) -> list[BeanbagEnergySample]:
        """Load energy history for the requested gateway."""

        return await backend.read_energy_history(
            active_session, active_websocket, gateway_id
        )

    try:
        return await async_run_with_reconnect(entry, runtime, _read_energy_history)
    except BeanbagError as error:
        runtime.consumption_refresh_pending = True
        _LOGGER.error(
            "Failed to fetch energy history for %s: %s",
            entry_identifier,
            error,
        )
        return None


@dataclass(slots=True)
class SecuremtrController:
    """Represent the discovered Secure Meters controller."""

    identifier: str
    name: str
    gateway_id: str
    serial_number: str | None = None
    firmware_version: str | None = None
    model: str | None = None


async def _async_fetch_controller(
    entry: ConfigEntry, runtime: SecuremtrRuntimeData
) -> SecuremtrController:
    """Retrieve controller metadata via the Beanbag WebSocket."""

    session = runtime.session
    websocket = runtime.websocket
    entry_identifier = _entry_display_name(entry)

    if session is None or websocket is None:
        raise BeanbagError("Beanbag session or websocket is unavailable")

    if not session.gateways:
        raise BeanbagError(
            f"No Beanbag gateways available for entry {entry_identifier}"
        )

    gateway = session.gateways[0]
    backend = runtime.backend

    runtime.zone_topology = await backend.read_zone_topology(
        session, websocket, gateway.gateway_id
    )

    try:
        await backend.sync_gateway_clock(session, websocket, gateway.gateway_id)
    except BeanbagError:
        _LOGGER.warning(
            "Secure Meters controller clock synchronisation failed for %s",
            entry_identifier,
        )

    runtime.schedule_overview = await backend.read_schedule_overview(
        session, websocket, gateway.gateway_id
    )

    metadata = await backend.read_device_metadata(
        session, websocket, gateway.gateway_id
    )
    runtime.device_metadata = metadata

    runtime.device_configuration = await backend.read_device_configuration(
        session, websocket, gateway.gateway_id
    )

    state_snapshot = await backend.read_live_state(
        session, websocket, gateway.gateway_id
    )
    runtime.state_snapshot = state_snapshot
    runtime.primary_power_on = state_snapshot.primary_power_on
    runtime.timed_boost_enabled = state_snapshot.timed_boost_enabled
    runtime.timed_boost_active = state_snapshot.timed_boost_active
    runtime.timed_boost_end_minute = state_snapshot.timed_boost_end_minute
    runtime.timed_boost_end_time = coerce_end_time(
        state_snapshot.timed_boost_end_minute
    )

    return _build_controller(metadata, gateway)


def runtime_update_signal(entry_id: str) -> str:
    """Return the dispatcher signal name for runtime updates."""

    return f"{_RUNTIME_UPDATE_SIGNAL}_{entry_id}"


def async_dispatch_runtime_update(hass: HomeAssistant, entry_id: str) -> None:
    """Notify entities that runtime state has been updated."""

    async_dispatcher_send(hass, runtime_update_signal(entry_id))


async def async_refresh_entry_state(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh runtime state for one config entry using its active transport."""

    domain_state = hass.data.get(DOMAIN, {})
    runtime: SecuremtrRuntimeData | None = domain_state.get(entry.entry_id)
    if runtime is None:
        raise HomeAssistantError(
            f"SecureMTR runtime data is unavailable for entry {entry.entry_id}"
        )

    if runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
        from .local_ble_commissioning import LocalBlePriority  # noqa: PLC0415

        await _async_refresh_local_ble_runtime(
            hass,
            entry,
            priority=LocalBlePriority.USER_READ,
            coalesce_key=None,
        )
        await _async_refresh_weekly_program_cache(
            hass,
            entry,
            priority=LocalBlePriority.USER_READ,
            coalesce_key=None,
        )
        return

    await consumption_metrics(hass, entry)


async def _async_refresh_weekly_program_cache(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    priority: "LocalBlePriority | int | None" = None,
    coalesce_key: str | None = _LOCAL_BLE_WEEKLY_CACHE_COALESCE_KEY,
) -> None:
    """Refresh runtime weekly schedule caches for a config entry."""

    domain_state = hass.data.get(DOMAIN, {})
    runtime: SecuremtrRuntimeData | None = domain_state.get(entry.entry_id)
    if runtime is None:
        return

    if runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
        from .local_ble_commissioning import (  # noqa: PLC0415
            LocalBleCommissioningError,
            LocalBlePriority,
        )

        try:
            worker = await async_get_local_ble_worker(hass, entry, runtime)
            resolved_priority = (
                LocalBlePriority.BACKGROUND_REFRESH
                if priority is None
                else priority
            )
            programs, canonicals = await worker.async_read_local_weekly_programs(
                priority=resolved_priority,
                coalesce_key=coalesce_key,
                zone_bois=runtime.schedule_zone_bois,
            )
        except (LocalBleCommissioningError, HomeAssistantError) as error:
            _LOGGER.debug(
                "Failed to refresh local BLE weekly schedules for %s: %s",
                _entry_display_name(entry),
                error,
            )
            return

        _cache_weekly_programs(runtime, programs, canonicals)
        async_dispatch_runtime_update(hass, entry.entry_id)


def _default_energy_state() -> dict[str, dict[str, Any]]:
    """Return the default per-zone energy payload for sensor hydration."""

    return {
        zone: {
            "energy_sum": 0.0,
            "last_day": None,
            "series_start": None,
            "offset_kwh": 0.0,
        }
        for zone in ZONE_KEYS
    }


def _merge_local_snapshot_energy(
    runtime: SecuremtrRuntimeData,
    *,
    primary_energy_kwh: float | None,
    boost_energy_kwh: float | None,
) -> bool:
    """Merge local snapshot energy totals into runtime sensor state."""

    state_payload = runtime.energy_state
    if not isinstance(state_payload, dict):
        state_payload = _default_energy_state()
        runtime.energy_state = state_payload

    changed = False
    for zone_key, value in (
        ("primary", primary_energy_kwh),
        ("boost", boost_energy_kwh),
    ):
        if value is None:
            continue
        zone_state = state_payload.get(zone_key)
        if not isinstance(zone_state, dict):
            zone_state = {
                "energy_sum": 0.0,
                "last_day": None,
                "series_start": None,
                "offset_kwh": 0.0,
            }
            state_payload[zone_key] = zone_state

        previous = zone_state.get("energy_sum")
        next_value = float(value)
        if not isinstance(previous, (int, float)) or float(previous) != next_value:
            zone_state["energy_sum"] = next_value
            changed = True

    return changed


def _snapshot_has_consumption_energy(
    consumption_days: list[dict[str, Any]] | None,
) -> bool:
    """Return whether parsed consumption rows include usable energy values."""

    if not isinstance(consumption_days, list):
        return False

    for row in consumption_days:
        if not isinstance(row, dict):
            continue
        for field_name in ("primary_energy_kwh", "boost_energy_kwh"):
            value = row.get(field_name)
            if isinstance(value, (int, float)) and value >= 0:
                return True
    return False


async def _merge_local_consumption_energy(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    *,
    consumption_days: list[dict[str, Any]] | None,
) -> bool:
    """Merge parsed local consumption day rows into the persisted accumulator."""

    if not isinstance(consumption_days, list):
        return False

    accumulator = await _ensure_energy_accumulator(hass, entry, runtime)
    changed = False

    for row in consumption_days:
        if not isinstance(row, dict):
            continue

        report_day = row.get("report_day")
        if not isinstance(report_day, str):
            continue
        try:
            parsed_day = date.fromisoformat(report_day)
        except ValueError:
            continue

        for zone_key, field_name in (
            ("primary", "primary_energy_kwh"),
            ("boost", "boost_energy_kwh"),
        ):
            energy_value = row.get(field_name)
            if not isinstance(energy_value, (int, float)):
                continue
            if energy_value < 0:
                continue
            if await accumulator.async_add_day(
                zone_key, parsed_day, float(energy_value)
            ):
                changed = True

    sensor_state = accumulator.as_sensor_state()
    if runtime.energy_state != sensor_state:
        runtime.energy_state = sensor_state
        changed = True

    return changed


def _merge_local_snapshot_statistics(
    runtime: SecuremtrRuntimeData,
    *,
    statistics_recent: dict[str, dict[str, Any]] | None,
) -> bool:
    """Merge local snapshot duration summaries into runtime statistics state."""

    if not isinstance(statistics_recent, dict):
        return False

    merged_statistics: dict[str, dict[str, Any]] = {}
    if isinstance(runtime.statistics_recent, dict):
        for zone_key, zone_payload in runtime.statistics_recent.items():
            if isinstance(zone_payload, dict):
                merged_statistics[zone_key] = dict(zone_payload)

    energy_state = (
        runtime.energy_state if isinstance(runtime.energy_state, dict) else {}
    )

    changed = False
    for zone_key in ZONE_KEYS:
        zone_update = statistics_recent.get(zone_key)
        if not isinstance(zone_update, dict):
            continue

        zone_state = merged_statistics.get(zone_key, {})
        previous_state = dict(zone_state)

        report_day = zone_update.get("report_day")
        if isinstance(report_day, str) and report_day:
            zone_state["report_day"] = report_day

        for field_key in ("runtime_hours", "scheduled_hours"):
            metric_value = zone_update.get(field_key)
            if isinstance(metric_value, (int, float)):
                zone_state[field_key] = float(metric_value)

        zone_energy = energy_state.get(zone_key)
        if isinstance(zone_energy, dict):
            energy_sum = zone_energy.get("energy_sum")
            if isinstance(energy_sum, (int, float)):
                zone_state["energy_sum"] = float(energy_sum)

        if zone_state != previous_state:
            changed = True

        if zone_state:
            merged_statistics[zone_key] = zone_state

    if changed:
        runtime.statistics_recent = merged_statistics
    return changed


async def _async_refresh_local_ble_runtime(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    priority: "LocalBlePriority | int | None" = None,
    coalesce_key: str | None = _LOCAL_BLE_SNAPSHOT_COALESCE_KEY,
) -> None:
    """Refresh runtime state from a local BLE GetAllServiceValues snapshot."""

    domain_state = hass.data.get(DOMAIN, {})
    runtime: SecuremtrRuntimeData | None = domain_state.get(entry.entry_id)
    if runtime is None or runtime.connection_mode != CONNECTION_MODE_LOCAL_BLE:
        return

    from .local_ble_commissioning import (  # noqa: PLC0415
        BLE_BACKGROUND_REFRESH_RETRY_LIMIT,
        BLE_COMMAND_RETRY_LIMIT,
        LocalBleCommissioningError,
        LocalBlePriority,
    )

    entry_identifier = _entry_display_name(entry)
    try:
        worker = await async_get_local_ble_worker(hass, entry, runtime)
        resolved_priority = (
            LocalBlePriority.BACKGROUND_REFRESH if priority is None else priority
        )
        # Cap the periodic refresh's retries so it cannot block a queued user
        # command for long; explicit refreshes keep the full budget.
        resolved_retry_limit = (
            BLE_BACKGROUND_REFRESH_RETRY_LIMIT
            if resolved_priority == LocalBlePriority.BACKGROUND_REFRESH
            else BLE_COMMAND_RETRY_LIMIT
        )
        snapshot = await worker.async_read_local_snapshot(
            priority=resolved_priority,
            coalesce_key=coalesce_key,
            retry_limit=resolved_retry_limit,
        )
    except HomeAssistantError as error:
        _LOGGER.debug(
            "Skipping local BLE refresh for %s: %s",
            entry_identifier,
            error,
        )
        return
    except LocalBleCommissioningError as error:
        _LOGGER.warning(
            "Failed to refresh local BLE snapshot for %s: %s",
            entry_identifier,
            error,
        )
        return

    _LOGGER.debug(
        "Local BLE snapshot for %s: primary_power_on=%s timed_boost_enabled=%s timed_boost_active=%s boost_duration=%s primary_energy_kwh=%s boost_energy_kwh=%s has_consumption_days=%s has_statistics_recent=%s",
        entry_identifier,
        snapshot.primary_power_on,
        snapshot.timed_boost_enabled,
        snapshot.timed_boost_active,
        snapshot.timed_boost_duration_minutes,
        snapshot.primary_energy_kwh,
        snapshot.boost_energy_kwh,
        isinstance(snapshot.consumption_days, list),
        isinstance(snapshot.statistics_recent, dict),
    )

    changed = False

    if (
        snapshot.primary_power_on is not None
        and runtime.primary_power_on != snapshot.primary_power_on
    ):
        runtime.primary_power_on = snapshot.primary_power_on
        changed = True

    if (
        snapshot.timed_boost_enabled is not None
        and runtime.timed_boost_enabled != snapshot.timed_boost_enabled
    ):
        runtime.timed_boost_enabled = snapshot.timed_boost_enabled
        changed = True

    was_timed_boost_active = runtime.timed_boost_active is True

    if (
        snapshot.timed_boost_active is not None
        and runtime.timed_boost_active != snapshot.timed_boost_active
    ):
        runtime.timed_boost_active = snapshot.timed_boost_active
        changed = True

    if (
        snapshot.timed_boost_active is True
        and not was_timed_boost_active
        and snapshot.timed_boost_duration_minutes
    ):
        now_local = dt_util.now()
        end_local = now_local + timedelta(minutes=snapshot.timed_boost_duration_minutes)
        end_minute = end_local.hour * 60 + end_local.minute
        if runtime.timed_boost_end_minute != end_minute:
            runtime.timed_boost_end_minute = end_minute
            changed = True
        end_time = coerce_end_time(end_minute)
        if runtime.timed_boost_end_time != end_time:
            runtime.timed_boost_end_time = end_time
            changed = True
    elif snapshot.timed_boost_active is False:
        if runtime.timed_boost_end_minute is not None:
            runtime.timed_boost_end_minute = None
            changed = True
        if runtime.timed_boost_end_time is not None:
            runtime.timed_boost_end_time = None
            changed = True

    if _snapshot_has_consumption_energy(snapshot.consumption_days):
        if await _merge_local_consumption_energy(
            hass,
            entry,
            runtime,
            consumption_days=snapshot.consumption_days,
        ):
            changed = True
    elif _merge_local_snapshot_energy(
        runtime,
        primary_energy_kwh=snapshot.primary_energy_kwh,
        boost_energy_kwh=snapshot.boost_energy_kwh,
    ):
        changed = True

    if _merge_local_snapshot_statistics(
        runtime,
        statistics_recent=snapshot.statistics_recent,
    ):
        changed = True

    if (
        isinstance(snapshot.schedule_zone_bois, dict)
        and runtime.schedule_zone_bois != snapshot.schedule_zone_bois
    ):
        runtime.schedule_zone_bois = dict(snapshot.schedule_zone_bois)
        changed = True

    if changed:
        async_dispatch_runtime_update(hass, entry.entry_id)
        _LOGGER.debug(
            "Updated local BLE runtime snapshot for %s",
            entry_identifier,
        )
    else:
        _LOGGER.debug(
            "Local BLE snapshot for %s produced no runtime state changes",
            entry_identifier,
        )


def coerce_end_time(end_minute: int | None) -> datetime | None:
    """Convert an end-minute payload into an aware datetime."""

    if end_minute is None:
        return None

    if not isinstance(end_minute, int) or end_minute < 0:
        return None

    now_local = dt_util.now()
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = midnight + timedelta(minutes=end_minute)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return dt_util.as_utc(candidate)


def _build_controller(
    metadata: dict[str, Any], gateway: BeanbagGateway
) -> SecuremtrController:
    """Translate metadata and gateway context into a controller object."""

    identifier_candidates = (
        _normalize_identifier(metadata.get("BOI")),
        _normalize_identifier(metadata.get("SN")),
        _normalize_identifier(gateway.gateway_id),
    )

    identifier = next(
        (candidate for candidate in identifier_candidates if candidate), DOMAIN
    )

    serial_value = metadata.get("SN")
    serial_number = (
        str(serial_value).strip()
        if isinstance(serial_value, (str, int, float))
        else None
    )
    if serial_number == "":
        serial_number = None

    firmware_value = metadata.get("FV")
    firmware_version = (
        str(firmware_value).strip()
        if isinstance(firmware_value, (str, int, float)) and str(firmware_value).strip()
        else None
    )

    model_value = metadata.get("MD")
    model = (
        str(model_value).strip()
        if isinstance(model_value, (str, int, float)) and str(model_value).strip()
        else None
    )
    if model:
        model = MODEL_ALIASES.get(model, model)

    raw_name = metadata.get("N")
    if isinstance(raw_name, (str, int, float)):
        candidate_name = str(raw_name).strip()
    else:
        candidate_name = ""

    default_name = DEFAULT_DEVICE_LABEL

    name = (
        candidate_name
        if candidate_name and not candidate_name.isdigit()
        else default_name
    )

    return SecuremtrController(
        identifier=identifier,
        name=name,
        gateway_id=gateway.gateway_id,
        serial_number=serial_number,
        firmware_version=firmware_version,
        model=model,
    )


async def consumption_metrics(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh and persist seven-day consumption metrics for the controller."""

    entry_identifier = _entry_display_name(entry)
    domain_state = hass.data.get(DOMAIN, {})
    runtime = domain_state.get(entry.entry_id)

    if runtime is None:
        _LOGGER.error(
            "Runtime data unavailable while requesting consumption metrics for %s",
            entry_identifier,
        )
        return

    async with runtime.consumption_lock:
        async with runtime.command_lock:
            validation = await _validate_consumption_connection(
                hass, entry, entry_identifier, runtime
            )
            if validation is None:
                return

            runtime, controller, session, websocket = validation

            prepared = await _prepare_consumption_samples(
                entry, runtime, controller, entry_identifier
            )
            if prepared is None:
                return

            zone_result = await _process_zone_samples(
                hass,
                entry,
                runtime,
                session,
                websocket,
                controller,
                prepared,
                entry_identifier,
            )

        _submit_statistics(hass, entry, controller, zone_result.statistics_samples)

        _update_runtime_state(
            hass,
            entry,
            runtime,
            zone_result,
            entry_identifier,
            len(prepared.rows),
        )


async def _validate_consumption_connection(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entry_identifier: str,
    runtime: SecuremtrRuntimeData | None = None,
) -> (
    tuple[
        SecuremtrRuntimeData,
        SecuremtrController,
        BeanbagSession,
        ClientWebSocketResponse,
    ]
    | None
):
    """Ensure the runtime, controller, and transport objects are available."""

    if runtime is None:
        domain_state = hass.data.get(DOMAIN, {})
        runtime = domain_state.get(entry.entry_id)

    if runtime is None:
        _LOGGER.error(
            "Runtime data unavailable while requesting consumption metrics for %s",
            entry_identifier,
        )
        return None

    controller = runtime.controller
    if controller is None and runtime.session is not None:
        runtime.consumption_refresh_pending = True
        _LOGGER.debug(
            "Skipping consumption metrics refresh for %s; controller unavailable",
            entry_identifier,
        )
        return None

    if not await _async_refresh_connection(entry, runtime):
        runtime.consumption_refresh_pending = True
        return None

    session = runtime.session
    websocket = runtime.websocket
    controller = runtime.controller

    if session is None or websocket is None or controller is None:
        runtime.consumption_refresh_pending = True
        _LOGGER.error(
            "Secure Meters connection unavailable for energy history request: %s",
            entry_identifier,
        )
        return None

    return runtime, controller, session, websocket


async def _prepare_consumption_samples(
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    controller: SecuremtrController,
    entry_identifier: str,
) -> PreparedSamples | None:
    """Fetch, trim, and normalise recent consumption samples."""

    samples = await _async_fetch_energy_samples(
        entry, runtime, controller, entry_identifier
    )
    if samples is None:
        return None

    if not samples:
        runtime.consumption_metrics_log = []
        runtime.consumption_refresh_pending = False
        _LOGGER.debug("No consumption samples returned for %s", entry_identifier)
        return None

    samples = sorted(samples, key=lambda sample: sample.timestamp)
    if len(samples) > 7:
        samples = samples[-7:]

    options = _load_statistics_options(entry)

    processed_rows: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []
    for sample in samples:
        sample_dt = dt_util.utc_from_timestamp(sample.timestamp)
        report_day = assign_report_day(sample_dt, options.timezone)
        iso_timestamp = sample_dt.isoformat()
        row: dict[str, Any] = {
            "timestamp": iso_timestamp,
            "epoch_seconds": sample.timestamp,
            "report_day": report_day,
        }
        for metadata in ZONE_METADATA.values():
            row[metadata.energy_field] = getattr(sample, metadata.energy_field)
            row[metadata.runtime_field] = getattr(sample, metadata.runtime_field)
            row[metadata.scheduled_field] = getattr(sample, metadata.scheduled_field)
        processed_rows.append(row)
        log_rows.append({**row, "report_day": report_day.isoformat()})
        _LOGGER.info(
            "Consumption sample %s assigned to report day %s (%s)",
            iso_timestamp,
            report_day.isoformat(),
            options.timezone_name,
        )

    runtime.consumption_metrics_log = log_rows

    return PreparedSamples(rows=processed_rows, options=options)


async def _ensure_energy_accumulator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
) -> EnergyAccumulator:
    """Return a loaded energy accumulator, creating backing storage on demand."""

    store = runtime.energy_store
    if store is None:
        store = Store(hass, ENERGY_STORE_VERSION, _energy_store_key(entry))
        runtime.energy_store = store

    accumulator = runtime.energy_accumulator
    if accumulator is None:
        accumulator = EnergyAccumulator(store=store)
        runtime.energy_accumulator = accumulator

    await accumulator.async_load()
    return accumulator


async def _prepare_zone_contexts_and_calibrations(
    runtime: SecuremtrRuntimeData,
    session: BeanbagSession,
    websocket: ClientWebSocketResponse,
    controller: SecuremtrController,
    processed_rows: list[dict[str, Any]],
    options: StatisticsOptions,
    entry_identifier: str,
) -> tuple[dict[str, ZoneContext], dict[str, EnergyCalibration]]:
    """Fetch zone programs and derive contexts plus calibrations."""

    programs, canonicals = await async_read_zone_programs(
        runtime.backend,
        session,
        websocket,
        gateway_id=controller.gateway_id,
        entry_identifier=entry_identifier,
    )
    _cache_weekly_programs(runtime, programs, canonicals)

    contexts = _build_zone_contexts(options, programs, canonicals)
    calibrations = _build_zone_calibrations(
        processed_rows, contexts, options, entry_identifier
    )
    return contexts, calibrations


def _update_runtime_summaries(
    runtime: SecuremtrRuntimeData,
    accumulator: EnergyAccumulator,
    zone_summaries: Mapping[str, tuple[date, Decimal | None, Decimal | None]],
    energy_changed: bool,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    """Integrate processed zone summaries into runtime tracking structures."""

    recent_measurements: dict[str, Any] = dict(runtime.statistics_recent or {})
    sensor_state = accumulator.as_sensor_state()
    energy_state_changed = bool(energy_changed or runtime.energy_state is None)

    for zone_key, summary in zone_summaries.items():
        report_day, runtime_hours, scheduled_hours = summary
        zone_energy = sensor_state.get(zone_key, {})
        recent_measurements[zone_key] = {
            "report_day": report_day.isoformat(),
            "runtime_hours": float(runtime_hours)
            if runtime_hours is not None
            else None,
            "scheduled_hours": float(scheduled_hours)
            if scheduled_hours is not None
            else None,
            "energy_sum": float(zone_energy.get("energy_sum", 0.0)),
        }

    dispatch_needed = energy_state_changed or bool(zone_summaries)
    return recent_measurements, sensor_state, energy_state_changed, dispatch_needed


async def _process_zone_samples(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    session: BeanbagSession,
    websocket: ClientWebSocketResponse,
    controller: SecuremtrController,
    samples: PreparedSamples,
    entry_identifier: str,
) -> ZoneProcessingResult:
    """Apply per-zone calibrations, persistence, and statistic preparation."""

    contexts, calibrations = await _prepare_zone_contexts_and_calibrations(
        runtime,
        session,
        websocket,
        controller,
        samples.rows,
        samples.options,
        entry_identifier,
    )

    accumulator = await _ensure_energy_accumulator(hass, entry, runtime)

    (
        statistics_samples,
        zone_summaries,
        energy_changed,
    ) = await _process_zone_records(
        accumulator,
        samples.rows,
        contexts,
        calibrations,
        samples.options,
        entry_identifier,
    )

    (
        recent_measurements,
        sensor_state,
        energy_state_changed,
        dispatch_needed,
    ) = _update_runtime_summaries(
        runtime,
        accumulator,
        zone_summaries,
        energy_changed,
    )

    return ZoneProcessingResult(
        statistics_samples=statistics_samples,
        sensor_state=sensor_state,
        recent_measurements=recent_measurements,
        energy_state_changed=energy_state_changed,
        dispatch_needed=dispatch_needed,
    )


def _submit_statistics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    controller: SecuremtrController,
    statistics_samples: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Record prepared statistic samples for each zone."""

    energy_entity_ids = _energy_sensor_entity_ids(hass, entry, controller)

    def _entity_stat_import_writer(
        hass: HomeAssistant,
        meta_dict: dict[str, Any],
        samples: Iterable[dict[str, Any]],
    ) -> None:
        """Import entity-bound statistics using recorder.async_import_statistics."""

        instance = recorder.get_instance(hass)
        zone_key = meta_dict.get("zone_key")
        metadata = StatisticMetaData(
            source=meta_dict.get("source") or DOMAIN,
            statistic_id=meta_dict["statistic_id"],
            unit_of_measurement=meta_dict["unit_of_measurement"],
            has_sum=True,
            has_mean=False,
            mean_type=StatisticMeanType.NONE,
            unit_class="energy",
            name=None,
        )
        statistic_rows: list[StatisticData] = []
        for sample in samples:
            start: datetime = sample["start"]
            # Samples are pre-aligned in the controller's timezone; only convert.
            start_utc = dt_util.as_utc(start)
            statistic_rows.append(
                StatisticData(
                    start=start_utc,
                    state=float(sample["state"]),
                    sum=float(sample["sum"]),
                )
            )

        async def _async_import() -> None:
            try:
                await instance.async_import_statistics(
                    metadata, statistic_rows, StatisticsTable.STATISTICS
                )
                statistic_id = (
                    metadata.statistic_id
                    if hasattr(metadata, "statistic_id")
                    else metadata["statistic_id"]
                )
                if statistic_rows:
                    first_start = statistic_rows[0]["start"]
                    last_start = statistic_rows[-1]["start"]
                    _LOGGER.debug(
                        "Imported %d rows for %s (first=%s UTC, last=%s UTC)",
                        len(statistic_rows),
                        statistic_id,
                        first_start.isoformat(),
                        last_start.isoformat(),
                    )
            except HomeAssistantError:
                statistic_id = (
                    metadata.statistic_id
                    if hasattr(metadata, "statistic_id")
                    else metadata["statistic_id"]
                )
                _LOGGER.exception(
                    "Failed to add statistics for %s (statistic_id=%s)",
                    zone_key or statistic_id,
                    statistic_id,
                )

        hass.async_create_task(_async_import())

    _submit_statistics_samples(
        hass,
        statistics_samples,
        energy_entity_ids,
        statistic_writer=_entity_stat_import_writer,
        mean_type=StatisticMeanType.NONE,
    )


def _update_runtime_state(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: SecuremtrRuntimeData,
    result: ZoneProcessingResult,
    entry_identifier: str,
    processed_rows: int,
) -> None:
    """Persist runtime state derived from the latest consumption refresh."""

    runtime.statistics_recent = result.recent_measurements

    if result.energy_state_changed:
        runtime.energy_state = result.sensor_state
        primary_total = float(
            result.sensor_state.get("primary", {}).get("energy_sum", 0.0)
        )
        boost_total = float(result.sensor_state.get("boost", {}).get("energy_sum", 0.0))
        _LOGGER.info(
            "Updated cumulative energy state for %s: primary=%.3f kWh boost=%.3f kWh",
            entry_identifier,
            primary_total,
            boost_total,
        )

    if result.dispatch_needed:
        async_dispatch_runtime_update(hass, entry.entry_id)

    runtime.consumption_refresh_pending = False
    _LOGGER.debug(
        "Secure Meters consumption metrics updated (%s entries)",
        processed_rows,
    )


def _energy_store_key(entry: ConfigEntry) -> str:
    """Return the persistent storage key for energy totals."""

    return f"{DOMAIN}_{entry.entry_id}_energy"


def _load_statistics_options(entry: ConfigEntry) -> StatisticsOptions:
    """Derive statistics options from the configuration entry."""

    from .config_flow import (  # Import lazily to avoid circular dependency.  # noqa: PLC0415
        CONF_BOOST_ANCHOR,
        CONF_ELEMENT_POWER_KW,
        CONF_PREFER_DEVICE_ENERGY,
        CONF_PRIMARY_ANCHOR,
        DEFAULT_BOOST_ANCHOR,
        DEFAULT_ELEMENT_POWER_KW,
        DEFAULT_PREFER_DEVICE_ENERGY,
        DEFAULT_PRIMARY_ANCHOR,
        DEFAULT_TIMEZONE,
        _anchor_option_to_time,
    )

    options = entry.options
    hass_timezone: str | None = None
    hass = getattr(entry, "hass", None)
    if hass is not None:
        hass_timezone = getattr(hass.config, "time_zone", None)

    requested_timezone = options.get(CONF_TIME_ZONE)
    timezone_name = requested_timezone or hass_timezone or DEFAULT_TIMEZONE

    def _zoneinfo_from(name: str | None) -> ZoneInfo | None:
        if not name:
            return None
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return None

    timezone = _zoneinfo_from(timezone_name)

    if timezone is None:
        entry_name = _entry_display_name(entry)
        _LOGGER.warning(
            "Invalid timezone %s for %s; attempting fallbacks",
            timezone_name,
            entry_name,
        )
        fallback_names: list[str | None] = [
            hass_timezone if timezone_name != hass_timezone else None,
            DEFAULT_TIMEZONE if timezone_name != DEFAULT_TIMEZONE else None,
        ]
        default_zone = dt_util.get_default_time_zone()
        fallback_names.append(getattr(default_zone, "key", None))
        fallback_names.append("UTC")

        for candidate in fallback_names:
            if not candidate:
                continue
            zone = _zoneinfo_from(candidate)
            if zone is not None:
                timezone = zone
                timezone_name = candidate
                _LOGGER.info(
                    "Using fallback timezone %s for %s", timezone_name, entry_name
                )
                break

    if timezone is None:
        timezone_name = DEFAULT_TIMEZONE
        timezone = ZoneInfo(DEFAULT_TIMEZONE)

    primary_anchor = _anchor_option_to_time(
        options.get(CONF_PRIMARY_ANCHOR),
        time.fromisoformat(DEFAULT_PRIMARY_ANCHOR),
    )
    boost_anchor = _anchor_option_to_time(
        options.get(CONF_BOOST_ANCHOR), time.fromisoformat(DEFAULT_BOOST_ANCHOR)
    )

    fallback_value = options.get(CONF_ELEMENT_POWER_KW, DEFAULT_ELEMENT_POWER_KW)
    try:
        fallback_power_kw = float(fallback_value)
    except (TypeError, ValueError):
        fallback_power_kw = float(DEFAULT_ELEMENT_POWER_KW)
    if fallback_power_kw <= 0:
        fallback_power_kw = float(DEFAULT_ELEMENT_POWER_KW)

    prefer_device_energy = bool(
        options.get(CONF_PREFER_DEVICE_ENERGY, DEFAULT_PREFER_DEVICE_ENERGY)
    )

    return StatisticsOptions(
        timezone=timezone,
        timezone_name=timezone_name,
        primary_anchor=primary_anchor,
        boost_anchor=boost_anchor,
        fallback_power_kw=fallback_power_kw,
        prefer_device_energy=prefer_device_energy,
    )


def _normalize_identifier(value: Any) -> str | None:
    """Return a sanitized identifier candidate when possible."""

    if isinstance(value, bool):
        return None

    if isinstance(value, (str, int, float)):
        candidate = str(value).strip()
        if candidate and candidate.lower() != "none":
            return candidate

    return None


slugify_identifier: Callable[[str], str] = import_module(
    "custom_components.securemtr.entity"
).slugify_identifier
