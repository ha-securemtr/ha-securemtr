"""Integration setup for securemtr water heater support."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
import logging
from typing import Any, TypeVar

from aiohttp import ClientWebSocketResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TIME_ZONE,
    UnitOfEnergy,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
    async_add_external_statistics,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .beanbag import (
    BeanbagBackend,
    BeanbagError,
    BeanbagGateway,
    BeanbagSession,
    BeanbagStateSnapshot,
    WeeklyProgram,
)
from .schedule import canonicalize_weekly, choose_anchor, day_intervals
from .utils import (
    EnergyCalibration,
    calibrate_energy_scale,
    cumulative_update,
    energy_from_row,
    report_day_for_sample,
    safe_anchor_datetime,
)

DOMAIN = "securemtr"

DEFAULT_DEVICE_LABEL = "E7+ Smart Water Heater Controller"

MODEL_ALIASES: dict[str, str] = {
    "2": DEFAULT_DEVICE_LABEL,
}

_RUNTIME_UPDATE_SIGNAL = "securemtr_runtime_update"

_LOGGER = logging.getLogger(__name__)

STATISTICS_STORE_VERSION = 1


_ResultT = TypeVar("_ResultT")


@dataclass(slots=True)
class StatisticsOptions:
    """Represent statistics configuration derived from entry options."""

    timezone: tzinfo
    timezone_name: str
    anchor_strategy: str
    primary_anchor: time
    boost_anchor: time
    fallback_power_kw: float
    prefer_device_energy: bool


@dataclass(slots=True)
class ZoneContext:
    """Describe mapping and schedule context for a controller zone."""

    label: str
    energy_field: str
    runtime_field: str
    scheduled_field: str
    energy_suffix: str
    runtime_suffix: str
    schedule_suffix: str
    fallback_anchor: time
    program: WeeklyProgram | None
    canonical: list[tuple[int, int]] | None


@dataclass(slots=True)
class StatisticDefinition:
    """Describe exported statistic metadata."""

    name: str
    unit: str | None
    has_sum: bool
    mean_type: StatisticMeanType


STATISTIC_DEFINITIONS: dict[str, StatisticDefinition] = {
    "primary_energy_kwh": StatisticDefinition(
        "Primary energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        True,
        StatisticMeanType.NONE,
    ),
    "boost_energy_kwh": StatisticDefinition(
        "Boost energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        True,
        StatisticMeanType.NONE,
    ),
    "primary_runtime_h": StatisticDefinition(
        "Primary runtime",
        UnitOfTime.HOURS,
        False,
        StatisticMeanType.ARITHMETIC,
    ),
    "primary_sched_h": StatisticDefinition(
        "Primary scheduled time",
        UnitOfTime.HOURS,
        False,
        StatisticMeanType.ARITHMETIC,
    ),
    "boost_runtime_h": StatisticDefinition(
        "Boost runtime",
        UnitOfTime.HOURS,
        False,
        StatisticMeanType.ARITHMETIC,
    ),
    "boost_sched_h": StatisticDefinition(
        "Boost scheduled time",
        UnitOfTime.HOURS,
        False,
        StatisticMeanType.ARITHMETIC,
    ),
}


@dataclass(slots=True)
class SecuremtrRuntimeData:
    """Track runtime Beanbag backend state for a config entry."""

    backend: BeanbagBackend
    session: BeanbagSession | None = None
    websocket: ClientWebSocketResponse | None = None
    startup_task: asyncio.Task[Any] | None = None
    controller: SecuremtrController | None = None
    controller_ready: asyncio.Event = field(default_factory=asyncio.Event)
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
    statistics_store: Store[dict[str, Any]] | None = None
    statistics_state: dict[str, Any] | None = None
    statistics_recent: dict[str, Any] | None = None


def _entry_display_name(entry: ConfigEntry) -> str:
    """Return a non-sensitive identifier for a config entry."""

    title = getattr(entry, "title", None)
    if isinstance(title, str) and title.strip():
        return title

    entry_id = getattr(entry, "entry_id", None)
    if isinstance(entry_id, str) and entry_id.strip():
        return entry_id

    return DOMAIN


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the securemtr integration."""
    _LOGGER.info("Starting securemtr integration setup")
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info("securemtr integration setup completed")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up securemtr from a config entry."""
    entry_identifier = _entry_display_name(entry)
    _LOGGER.info("Setting up config entry for securemtr: %s", entry_identifier)

    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    runtime = SecuremtrRuntimeData(backend=BeanbagBackend(session))
    hass.data[DOMAIN][entry.entry_id] = runtime

    runtime.statistics_store = Store(
        hass,
        STATISTICS_STORE_VERSION,
        _statistics_store_key(entry),
    )

    runtime.startup_task = hass.async_create_task(_async_start_backend(entry, runtime))

    def _scheduled_consumption_refresh(now: datetime) -> None:
        """Trigger the scheduled consumption metrics task."""

        _LOGGER.debug(
            "Scheduled consumption metrics refresh triggered for %s", entry_identifier
        )
        hass.async_create_task(consumption_metrics(hass, entry))

    runtime.consumption_schedule_unsub = async_track_time_change(
        hass,
        _scheduled_consumption_refresh,
        hour=1,
        minute=0,
        second=0,
    )

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
        runtime.consumption_schedule_unsub()
        runtime.consumption_schedule_unsub = None

    if runtime.startup_task is not None and not runtime.startup_task.done():
        runtime.startup_task.cancel()
        with suppress(asyncio.CancelledError):
            await runtime.startup_task

    if runtime.websocket is not None and not runtime.websocket.closed:
        await runtime.websocket.close()

    _LOGGER.info("securemtr config entry unloaded: %s", entry_identifier)
    return unload_ok


async def _async_start_backend(
    entry: ConfigEntry, runtime: SecuremtrRuntimeData
) -> None:
    """Authenticate with Beanbag and establish the WebSocket connection."""

    email: str = entry.data.get(CONF_EMAIL, "").strip()
    password_digest: str = entry.data.get(CONF_PASSWORD, "")
    entry_identifier = _entry_display_name(entry)

    if not email or not password_digest:
        _LOGGER.error("Missing credentials for securemtr entry %s", entry_identifier)
        runtime.controller_ready.set()
        return

    _LOGGER.info("Starting Beanbag backend for %s", entry_identifier)

    try:
        session, websocket = await runtime.backend.login_and_connect(
            email, password_digest
        )
    except BeanbagError as error:
        _LOGGER.error(
            "Failed to initialize Beanbag backend for %s: %s", entry_identifier, error
        )
        runtime.controller_ready.set()
        return
    except Exception:
        _LOGGER.exception(
            "Unexpected error while initializing Beanbag backend for %s",
            entry_identifier,
        )
        runtime.controller_ready.set()
        return

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
    except Exception:
        _LOGGER.exception(
            "Unexpected error while fetching securemtr controller for %s",
            entry_identifier,
        )
    else:
        runtime.controller = controller
        _LOGGER.info(
            "Discovered securemtr controller %s (%s)",
            controller.identifier,
            controller.name,
        )
    finally:
        runtime.controller_ready.set()

    _LOGGER.info("Beanbag backend connected for %s", entry_identifier)


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
    runtime.timed_boost_end_time = coerce_end_time(state_snapshot.timed_boost_end_minute)

    return _build_controller(metadata, gateway)


def runtime_update_signal(entry_id: str) -> str:
    """Return the dispatcher signal name for runtime updates."""

    return f"{_RUNTIME_UPDATE_SIGNAL}_{entry_id}"


def async_dispatch_runtime_update(hass: HomeAssistant, entry_id: str) -> None:
    """Notify entities that runtime state has been updated."""

    async_dispatcher_send(hass, runtime_update_signal(entry_id))


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
    runtime: SecuremtrRuntimeData | None = domain_state.get(entry.entry_id)

    if runtime is None:
        _LOGGER.error(
            "Runtime data unavailable while requesting consumption metrics for %s",
            entry_identifier,
        )
        return

    if not await _async_refresh_connection(entry, runtime):
        return

    session = runtime.session
    websocket = runtime.websocket
    controller = runtime.controller

    if session is None or websocket is None or controller is None:
        _LOGGER.error(
            "Secure Meters connection unavailable for energy history request: %s",
            entry_identifier,
        )
        return

    try:
        samples = await runtime.backend.read_energy_history(
            session, websocket, controller.gateway_id
        )
    except BeanbagError as error:
        _LOGGER.error(
            "Failed to fetch energy history for %s: %s",
            entry_identifier,
            error,
        )
        return

    if not samples:
        runtime.consumption_metrics_log = []
        _LOGGER.debug("No consumption samples returned for %s", entry_identifier)
        return

    samples = sorted(samples, key=lambda sample: sample.timestamp)
    if len(samples) > 7:
        samples = samples[-7:]

    options = _load_statistics_options(entry)

    processed_rows: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []
    for sample in samples:
        report_day = report_day_for_sample(sample.timestamp, options.timezone)
        iso_timestamp = dt_util.utc_from_timestamp(sample.timestamp).isoformat()
        row = {
            "timestamp": iso_timestamp,
            "epoch_seconds": sample.timestamp,
            "report_day": report_day,
            "primary_energy_kwh": sample.primary_energy_kwh,
            "boost_energy_kwh": sample.boost_energy_kwh,
            "primary_scheduled_minutes": sample.primary_scheduled_minutes,
            "primary_active_minutes": sample.primary_active_minutes,
            "boost_scheduled_minutes": sample.boost_scheduled_minutes,
            "boost_active_minutes": sample.boost_active_minutes,
        }
        processed_rows.append(row)
        log_rows.append({**row, "report_day": report_day.isoformat()})
        _LOGGER.info(
            "Consumption sample %s assigned to report day %s (%s)",
            iso_timestamp,
            report_day.isoformat(),
            options.timezone_name,
        )

    runtime.consumption_metrics_log = log_rows

    session = runtime.session
    websocket = runtime.websocket
    assert session is not None
    assert websocket is not None

    primary_program = await _read_zone_program(
        runtime,
        session,
        websocket,
        controller.gateway_id,
        "primary",
        entry_identifier,
    )
    boost_program = await _read_zone_program(
        runtime,
        session,
        websocket,
        controller.gateway_id,
        "boost",
        entry_identifier,
    )

    primary_canonical = (
        canonicalize_weekly(primary_program) if primary_program is not None else None
    )
    boost_canonical = (
        canonicalize_weekly(boost_program) if boost_program is not None else None
    )

    contexts: dict[str, ZoneContext] = {
        "primary": ZoneContext(
            label="Primary",
            energy_field="primary_energy_kwh",
            runtime_field="primary_active_minutes",
            scheduled_field="primary_scheduled_minutes",
            energy_suffix="primary_energy_kwh",
            runtime_suffix="primary_runtime_h",
            schedule_suffix="primary_sched_h",
            fallback_anchor=options.primary_anchor,
            program=primary_program,
            canonical=primary_canonical,
        ),
        "boost": ZoneContext(
            label="Boost",
            energy_field="boost_energy_kwh",
            runtime_field="boost_active_minutes",
            scheduled_field="boost_scheduled_minutes",
            energy_suffix="boost_energy_kwh",
            runtime_suffix="boost_runtime_h",
            schedule_suffix="boost_sched_h",
            fallback_anchor=options.boost_anchor,
            program=boost_program,
            canonical=boost_canonical,
        ),
    }

    from .entity import slugify_identifier  # Import lazily to avoid circular dependency.

    controller_identifier = controller.serial_number or controller.identifier
    controller_slug = slugify_identifier(controller_identifier)
    energy_statistic_ids: dict[str, str] = {}

    registry = er.async_get(hass)
    for zone_key, context in contexts.items():
        unique_id = f"{controller_slug}_{zone_key}_energy_total"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            _LOGGER.debug(
                "Energy sensor unique ID %s not found in entity registry for %s",
                unique_id,
                entry_identifier,
            )
            continue
        energy_statistic_ids[context.energy_suffix] = entity_id

    calibrations: dict[str, EnergyCalibration] = {
        "primary": calibrate_energy_scale(
            processed_rows,
            "primary_energy_kwh",
            "primary_active_minutes",
            options.fallback_power_kw,
        ),
        "boost": calibrate_energy_scale(
            processed_rows,
            "boost_energy_kwh",
            "boost_active_minutes",
            options.fallback_power_kw,
        ),
    }

    for zone_key, calibration in calibrations.items():
        context = contexts[zone_key]
        _LOGGER.info(
            "%s calibration for %s (strategy=%s): use_scale=%s scale=%.6f source=%s",
            context.label,
            entry_identifier,
            options.anchor_strategy,
            calibration.use_scale,
            calibration.scale,
            calibration.source,
        )

    if not options.prefer_device_energy:
        calibrations["primary"] = EnergyCalibration(
            False, options.fallback_power_kw, "duration_power"
        )
        calibrations["boost"] = EnergyCalibration(
            False, options.fallback_power_kw, "duration_power"
        )

    store = runtime.statistics_store
    if store is None:
        store = Store(hass, STATISTICS_STORE_VERSION, _statistics_store_key(entry))
        runtime.statistics_store = store

    statistics_state = runtime.statistics_state
    if statistics_state is None:
        loaded_state = await store.async_load()
        statistics_state = loaded_state if isinstance(loaded_state, dict) else {}
        runtime.statistics_state = statistics_state

    statistics_payload: dict[str, list[StatisticData]] = {
        suffix: [] for suffix in STATISTIC_DEFINITIONS
    }

    entry_slug = slugify_identifier(entry_identifier)
    store_dirty = False
    runtime_updated = False
    recent_measurements: dict[str, Any] = dict(runtime.statistics_recent or {})

    for zone_key, context in contexts.items():
        calibration = calibrations[zone_key]
        energy_sum, last_day = _load_zone_state(statistics_state, zone_key)
        zone_updated = False
        latest_runtime_hours: float | None = None
        latest_scheduled_hours: float | None = None
        latest_day: date | None = None

        for row in processed_rows:
            report_day: date = row["report_day"]
            if last_day is not None and report_day <= last_day:
                continue

            intervals: list[tuple[datetime, datetime]] = []
            if context.program is not None and context.canonical is not None:
                intervals = day_intervals(
                    context.program,
                    day=report_day,
                    tz=options.timezone,
                    canonical=context.canonical,
                )

            anchor = _resolve_anchor(report_day, context, options, intervals)

            energy_value = energy_from_row(
                row,
                context.energy_field,
                context.runtime_field,
                calibration,
                options.fallback_power_kw,
            )
            energy_value = max(0.0, energy_value)
            energy_sum = cumulative_update(energy_sum, energy_value)
            statistics_payload[context.energy_suffix].append(
                {"start": anchor, "state": energy_value, "sum": energy_sum}
            )

            runtime_minutes = float(row.get(context.runtime_field, 0.0))
            runtime_hours = max(runtime_minutes, 0.0) / 60.0
            statistics_payload[context.runtime_suffix].append(
                {
                    "start": anchor,
                    "mean": runtime_hours,
                    "min": runtime_hours,
                    "max": runtime_hours,
                }
            )

            scheduled_minutes = float(row.get(context.scheduled_field, 0.0))
            scheduled_hours = max(scheduled_minutes, 0.0) / 60.0
            statistics_payload[context.schedule_suffix].append(
                {
                    "start": anchor,
                    "mean": scheduled_hours,
                    "min": scheduled_hours,
                    "max": scheduled_hours,
                }
            )

            _LOGGER.info(
                "%s statistics for %s on %s: anchor=%s energy=%.3f runtime_h=%.2f scheduled_h=%.2f intervals=%d",
                context.label,
                entry_identifier,
                report_day.isoformat(),
                anchor.isoformat(),
                energy_value,
                runtime_hours,
                scheduled_hours,
                len(intervals),
            )

            last_day = report_day
            zone_updated = True
            latest_runtime_hours = runtime_hours
            latest_scheduled_hours = scheduled_hours
            latest_day = report_day

        if zone_updated:
            _store_zone_state(statistics_state, zone_key, energy_sum, last_day)
            store_dirty = True
            runtime_updated = True
            recent_measurements[zone_key] = {
                "report_day": latest_day.isoformat() if latest_day else None,
                "runtime_hours": float(latest_runtime_hours)
                if latest_runtime_hours is not None
                else None,
                "scheduled_hours": float(latest_scheduled_hours)
                if latest_scheduled_hours is not None
                else None,
                "energy_sum": float(energy_sum),
            }

    for suffix, definition in STATISTIC_DEFINITIONS.items():
        statistics = statistics_payload[suffix]
        if not statistics:
            continue
        metadata = _build_statistic_metadata(
            entry_identifier,
            entry_slug,
            suffix,
            definition,
            statistic_id_override=energy_statistic_ids.get(suffix),
        )
        _LOGGER.info(
            "Importing %d statistic rows for %s", len(statistics), metadata["statistic_id"]
        )
        async_add_external_statistics(hass, metadata, statistics)

    runtime.statistics_recent = recent_measurements

    if store_dirty:
        await store.async_save(statistics_state)

    if runtime_updated:
        async_dispatch_runtime_update(hass, entry.entry_id)

    _LOGGER.debug(
        "Secure Meters consumption metrics updated (%s entries)",
        len(processed_rows),
    )


def _statistics_store_key(entry: ConfigEntry) -> str:
    """Return the persistent storage key for statistics state."""

    return f"{DOMAIN}_{entry.entry_id}_statistics"


def _load_statistics_options(entry: ConfigEntry) -> StatisticsOptions:
    """Derive statistics options from the configuration entry."""

    from .config_flow import (  # Import lazily to avoid circular dependency.
        ANCHOR_STRATEGIES,
        CONF_ANCHOR_STRATEGY,
        CONF_BOOST_ANCHOR,
        CONF_ELEMENT_POWER_KW,
        CONF_PREFER_DEVICE_ENERGY,
        CONF_PRIMARY_ANCHOR,
        DEFAULT_ANCHOR_STRATEGY,
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

    timezone_name = hass_timezone or options.get(CONF_TIME_ZONE) or DEFAULT_TIMEZONE
    timezone = dt_util.get_time_zone(timezone_name)

    if timezone is None:
        _LOGGER.warning(
            "Invalid timezone %s for %s; falling back to Home Assistant default",
            timezone_name,
            _entry_display_name(entry),
        )
        timezone_name = hass_timezone or DEFAULT_TIMEZONE
        timezone = dt_util.get_time_zone(timezone_name)
        if timezone is None:
            timezone = dt_util.get_time_zone(DEFAULT_TIMEZONE)
            timezone_name = DEFAULT_TIMEZONE
        if timezone is None:
            timezone = dt_util.get_default_time_zone()
            timezone_name = getattr(timezone, "key", None) or timezone.tzname(
                dt_util.utcnow()
            )

    anchor_strategy = options.get(CONF_ANCHOR_STRATEGY, DEFAULT_ANCHOR_STRATEGY)
    if anchor_strategy not in ANCHOR_STRATEGIES:
        anchor_strategy = DEFAULT_ANCHOR_STRATEGY

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
        anchor_strategy=anchor_strategy,
        primary_anchor=primary_anchor,
        boost_anchor=boost_anchor,
        fallback_power_kw=fallback_power_kw,
        prefer_device_energy=prefer_device_energy,
    )


async def _read_zone_program(
    runtime: SecuremtrRuntimeData,
    session: BeanbagSession,
    websocket: ClientWebSocketResponse,
    gateway_id: str,
    zone: str,
    entry_identifier: str,
) -> WeeklyProgram | None:
    """Fetch the weekly program for a zone, returning None on failure."""

    try:
        return await runtime.backend.read_weekly_program(
            session, websocket, gateway_id, zone=zone
        )
    except BeanbagError as error:
        _LOGGER.error(
            "Failed to fetch %s weekly program for %s: %s",
            zone,
            entry_identifier,
            error,
        )
    except Exception:
        _LOGGER.exception(
            "Unexpected error while fetching %s weekly program for %s",
            zone,
            entry_identifier,
        )
    return None


def _load_zone_state(
    statistics_state: dict[str, Any], zone: str
) -> tuple[float, date | None]:
    """Return the persisted cumulative sum and last processed day."""

    stored = statistics_state.get(zone)
    energy_sum = 0.0
    last_day: date | None = None

    if isinstance(stored, dict):
        energy_raw = stored.get("energy_sum")
        if isinstance(energy_raw, (int, float)):
            energy_sum = float(energy_raw)

        last_day_raw = stored.get("last_day")
        if isinstance(last_day_raw, str):
            with suppress(ValueError):
                last_day = date.fromisoformat(last_day_raw)

    return energy_sum, last_day


def _store_zone_state(
    statistics_state: dict[str, Any],
    zone: str,
    energy_sum: float,
    last_day: date | None,
) -> None:
    """Persist the updated cumulative state for a zone."""

    statistics_state[zone] = {
        "energy_sum": max(0.0, float(energy_sum)),
        "last_day": last_day.isoformat() if last_day is not None else None,
    }


def _resolve_anchor(
    report_day: date,
    context: ZoneContext,
    options: StatisticsOptions,
    intervals: Iterable[tuple[datetime, datetime]],
) -> datetime:
    """Select an anchor for the provided day and schedule context."""

    if options.anchor_strategy == "fixed":
        anchor = safe_anchor_datetime(report_day, context.fallback_anchor, options.timezone)
        _LOGGER.debug(
            "%s fixed anchor selected for %s on %s: %s",
            context.label,
            options.timezone_name,
            report_day.isoformat(),
            anchor.isoformat(),
        )
        return anchor

    anchor = choose_anchor(list(intervals), strategy=options.anchor_strategy)
    if anchor is not None:
        _LOGGER.debug(
            "%s schedule anchor selected via %s on %s: %s",
            context.label,
            options.anchor_strategy,
            report_day.isoformat(),
            anchor.isoformat(),
        )
        return anchor

    fallback = safe_anchor_datetime(report_day, context.fallback_anchor, options.timezone)
    _LOGGER.debug(
        "%s fallback anchor used for %s on %s: %s",
        context.label,
        options.timezone_name,
        report_day.isoformat(),
        fallback.isoformat(),
    )
    return fallback


def _build_statistic_metadata(
    entry_identifier: str,
    entry_slug: str,
    suffix: str,
    definition: StatisticDefinition,
    *,
    statistic_id_override: str | None = None,
) -> StatisticMetaData:
    """Create metadata for a statistic export."""

    identifier = entry_identifier.strip() or entry_identifier
    statistic_id = statistic_id_override or f"{DOMAIN}:{entry_slug}:{suffix}"
    return {
        "source": DOMAIN,
        "name": f"{identifier} {definition.name}",
        "statistic_id": statistic_id,
        "unit_of_measurement": definition.unit,
        "has_sum": definition.has_sum,
        "mean_type": definition.mean_type,
    }


def _normalize_identifier(value: Any) -> str | None:
    """Return a sanitized identifier candidate when possible."""

    if isinstance(value, bool):
        return None

    if isinstance(value, (str, int, float)):
        candidate = str(value).strip()
        if candidate and candidate.lower() != "none":
            return candidate

    return None
