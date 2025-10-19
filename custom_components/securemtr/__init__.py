"""Integration setup for securemtr water heater support."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from types import MappingProxyType
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientWebSocketResponse
from homeassistant import config_entries as hass_config_entries
from homeassistant.components.utility_meter.const import (
    CONF_METER_DELTA_VALUES,
    CONF_METER_NET_CONSUMPTION,
    CONF_METER_OFFSET,
    CONF_METER_PERIODICALLY_RESETTING,
    CONF_METER_TYPE,
    CONF_SENSOR_ALWAYS_AVAILABLE,
    CONF_SOURCE_SENSOR,
    CONF_TARIFFS,
    DAILY,
    DOMAIN as UTILITY_METER_DOMAIN,
    WEEKLY,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .beanbag import (
    BeanbagBackend,
    BeanbagError,
    BeanbagGateway,
    BeanbagSession,
    BeanbagStateSnapshot,
    WeeklyProgram,
)
from .energy import EnergyAccumulator
from .schedule import canonicalize_weekly, choose_anchor, day_intervals
from .utils import (
    EnergyCalibration,
    assign_report_day,
    calibrate_energy_scale,
    energy_from_row,
    safe_anchor_datetime,
)

DOMAIN = "securemtr"

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DEFAULT_DEVICE_LABEL = "E7+ Smart Water Heater Controller"

MODEL_ALIASES: dict[str, str] = {
    "2": DEFAULT_DEVICE_LABEL,
}

_RUNTIME_UPDATE_SIGNAL = "securemtr_runtime_update"

UTILITY_METER_CYCLES: tuple[str, ...] = (DAILY, WEEKLY)
UTILITY_METER_ZONE_LABELS: dict[str, str] = {"primary": "Primary", "boost": "Boost"}

_LOGGER = logging.getLogger(__name__)

ENERGY_STORE_VERSION = 1
SERVICE_RESET_ENERGY = "reset_energy_accumulator"
ATTR_ENTRY_ID = "entry_id"
ATTR_ZONE = "zone"
ENERGY_ZONES = ("primary", "boost")
_RESET_SERVICE_FLAG = "_reset_service_registered"


LEGACY_ENERGY_ENTITY_GUIDANCE: MappingProxyType[str, tuple[str, str]] = MappingProxyType(
    {
        "sensor.primary_energy_total": ("primary", "sensor.securemtr_primary_energy_kwh"),
        "sensor.boost_energy_total": ("boost", "sensor.securemtr_boost_energy_kwh"),
    }
)


_ResultT = TypeVar("_ResultT")


def _async_register_services(hass: HomeAssistant) -> None:
    """Register SecureMTR domain services once per Home Assistant instance."""

    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_RESET_SERVICE_FLAG):
        return

    schema = vol.Schema(
        {
            vol.Required(ATTR_ENTRY_ID): str,
            vol.Optional(ATTR_ZONE, default=ENERGY_ZONES[0]): vol.In(ENERGY_ZONES),
        }
    )

    async def _async_handle_reset(call: ServiceCall) -> None:
        """Reset the cumulative energy state for a config entry zone."""

        entry_id: str = call.data[ATTR_ENTRY_ID]
        zone: str = call.data[ATTR_ZONE]
        domain_state = hass.data.get(DOMAIN, {})
        runtime: SecuremtrRuntimeData | None = domain_state.get(entry_id)
        if runtime is None:
            raise HomeAssistantError(
                f"SecureMTR entry {entry_id} is not loaded; cannot reset energy"
            )

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
        _LOGGER.info(
            "Reset cumulative energy state for %s zone %s", entry_id, zone
        )
        async_dispatch_runtime_update(hass, entry_id)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_ENERGY,
        _async_handle_reset,
        schema=schema,
    )
    domain_data[_RESET_SERVICE_FLAG] = True


def _disable_legacy_energy_entities(
    entity_registry: er.EntityRegistry, entry: ConfigEntry
) -> list[tuple[str, str]]:
    """Disable legacy total sensors for the config entry if present."""

    disabled_entities: list[tuple[str, str]] = []
    for legacy_entity_id, guidance in LEGACY_ENERGY_ENTITY_GUIDANCE.items():
        entry_record = None
        entities_data = getattr(entity_registry, "_entities_data", None)
        if isinstance(entities_data, dict):
            entry_record = entities_data.get(legacy_entity_id)
        if entry_record is None:
            entities_index = getattr(entity_registry, "entities", None)
            if entities_index is not None:
                with suppress(Exception):
                    entry_record = entities_index.get_entry(legacy_entity_id)
        if entry_record is None:
            continue
        if entry_record.config_entry_id != entry.entry_id:
            continue
        zone, replacement_entity_id = guidance
        expected_suffix = f"_{zone}_energy_total"
        unique_id = getattr(entry_record, "unique_id", "") or ""
        if expected_suffix and not unique_id.endswith(expected_suffix):
            continue
        if entry_record.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            continue
        entity_registry.async_update_entity(
            legacy_entity_id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        )
        disabled_entities.append((legacy_entity_id, replacement_entity_id))
    return disabled_entities


async def _async_retire_legacy_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Hide legacy entities so users choose the new cumulative sensors."""

    entry_identifier = _entry_display_name(entry)
    try:
        entity_registry = er.async_get(hass)
    except Exception:  # pragma: no cover - defensive guard
        _LOGGER.exception(
            "Unable to inspect entity registry for legacy SecureMTR entities: %s",
            entry_identifier,
        )
        return

    disabled_entities = _disable_legacy_energy_entities(entity_registry, entry)
    if not disabled_entities:
        return

    for legacy_entity_id, replacement_entity_id in disabled_entities:
        _LOGGER.info(
            (
                "Disabled legacy SecureMTR energy entity %s for %s; "
                "select %s in the Energy Dashboard instead"
            ),
            legacy_entity_id,
            entry_identifier,
            replacement_entity_id,
        )


async def _async_ensure_utility_meters(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Create daily and weekly utility_meter helpers for the entry if needed."""

    entry_identifier = _entry_display_name(entry)
    config_entries_helper = getattr(hass, "config_entries", None)
    if config_entries_helper is None:
        _LOGGER.debug(
            "config_entries helper unavailable; skipping utility meter helpers for %s",
            entry_identifier,
        )
        return

    required_methods = ["async_entries", "async_add"]
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
        existing_entries = {
            entry_obj.unique_id: entry_obj
            for entry_obj in config_entries_helper.async_entries(
                UTILITY_METER_DOMAIN
            )
            if entry_obj.unique_id is not None
        }
    except Exception:  # pragma: no cover - defensive guard for helper behaviour
        _LOGGER.exception(
            "Unable to inspect existing utility meter helpers for %s",
            entry_identifier,
        )
        return

    for zone_key, zone_label in UTILITY_METER_ZONE_LABELS.items():
        source_entity = f"sensor.securemtr_{zone_key}_energy_kwh"
        for cycle in UTILITY_METER_CYCLES:
            unique_id = f"securemtr_{entry.entry_id}_{zone_key}_{cycle}_utility_meter"
            if unique_id in existing_entries:
                _LOGGER.debug(
                    "Utility meter helper already present for %s zone %s cycle %s",
                    entry_identifier,
                    zone_key,
                    cycle,
                )
                continue

            meter_name = (
                f"SecureMTR {zone_label} Energy {cycle.capitalize()}"
            )
            options: dict[str, Any] = {
                CONF_NAME: meter_name,
                CONF_SOURCE_SENSOR: source_entity,
                CONF_METER_TYPE: cycle,
                CONF_METER_OFFSET: 0,
                CONF_TARIFFS: [],
                CONF_METER_NET_CONSUMPTION: False,
                CONF_METER_DELTA_VALUES: False,
                CONF_METER_PERIODICALLY_RESETTING: True,
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
                entry_id=f"securemtr_um_{entry.entry_id}_{zone_key}_{cycle}",
                subentries_data=(),
            )

            try:
                await config_entries_helper.async_add(helper_entry)
            except Exception:  # pragma: no cover - defensive guard for helper writes
                _LOGGER.exception(
                    "Failed to create %s utility meter helper for %s zone %s",
                    cycle,
                    entry_identifier,
                    zone_key,
                )
                continue

            existing_entries[unique_id] = helper_entry
            _LOGGER.info(
                "Created %s utility meter helper for %s zone %s (source=%s)",
                cycle,
                entry_identifier,
                zone_key,
                source_entity,
            )


@dataclass(slots=True)
class StatisticsOptions:
    """Represent statistics configuration derived from entry options."""

    timezone: ZoneInfo
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
    energy_store: Store[dict[str, Any]] | None = None
    energy_state: dict[str, Any] | None = None
    energy_accumulator: EnergyAccumulator | None = None
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
    runtime = SecuremtrRuntimeData(backend=BeanbagBackend(session))
    hass.data[DOMAIN][entry.entry_id] = runtime

    runtime.energy_store = Store(
        hass,
        ENERGY_STORE_VERSION,
        _energy_store_key(entry),
    )

    await _async_retire_legacy_entities(hass, entry)

    runtime.startup_task = hass.async_create_task(_async_start_backend(entry, runtime))

    def _scheduled_consumption_refresh(now: datetime) -> None:
        """Trigger the scheduled consumption metrics task."""

        _LOGGER.debug(
            "Scheduled consumption metrics refresh triggered for %s", entry_identifier
        )
        hass.async_create_task(consumption_metrics(hass, entry))

    schedule_unsubs: list[Callable[[], None]] = []
    for scheduled_hour in (1, 13):
        schedule_unsubs.append(
            async_track_time_change(
                hass,
                _scheduled_consumption_refresh,
                hour=scheduled_hour,
                minute=0,
                second=0,
            )
        )

    def _unsubscribe_schedules() -> None:
        """Cancel every scheduled consumption metrics callback."""

        for unsubscribe in schedule_unsubs:
            with suppress(Exception):
                unsubscribe()
        schedule_unsubs.clear()

    runtime.consumption_schedule_unsub = _unsubscribe_schedules

    _LOGGER.debug(
        "Queuing immediate consumption metrics refresh for %s", entry_identifier
    )
    hass.async_create_task(consumption_metrics(hass, entry))

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
        sample_dt = dt_util.utc_from_timestamp(sample.timestamp)
        report_day = assign_report_day(sample_dt, options.timezone)
        iso_timestamp = sample_dt.isoformat()
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

    store = runtime.energy_store
    if store is None:
        store = Store(hass, ENERGY_STORE_VERSION, _energy_store_key(entry))
        runtime.energy_store = store

    accumulator = runtime.energy_accumulator
    if accumulator is None:
        accumulator = EnergyAccumulator(store=store)
        runtime.energy_accumulator = accumulator

    await accumulator.async_load()
    if runtime.energy_state is None:
        runtime.energy_state = accumulator.as_sensor_state()

    recent_measurements: dict[str, Any] = dict(runtime.statistics_recent or {})
    zone_summaries: dict[str, tuple[date, float | None, float | None]] = {}
    energy_changed = False
    runtime_updated = False

    for zone_key, context in contexts.items():
        calibration = calibrations[zone_key]
        latest_runtime_hours: float | None = None
        latest_scheduled_hours: float | None = None
        latest_day: date | None = None

        for row in processed_rows:
            report_day: date = row["report_day"]

            energy_value = energy_from_row(
                row,
                context.energy_field,
                context.runtime_field,
                calibration,
                options.fallback_power_kw,
            )
            energy_value = max(0.0, energy_value)
            updated = await accumulator.async_add_day(
                zone_key, report_day, energy_value
            )
            if updated:
                energy_changed = True
                zone_total = accumulator.zone_total(zone_key)
                _LOGGER.info(
                    "SecureMTR %s energy on %s for %s: day=%.3f kWh cumulative=%.3f kWh",
                    context.label,
                    report_day.isoformat(),
                    entry_identifier,
                    energy_value,
                    zone_total,
                )

            intervals: list[tuple[datetime, datetime]] = []
            if context.program is not None and context.canonical is not None:
                intervals = day_intervals(
                    context.program,
                    day=report_day,
                    tz=options.timezone,
                    canonical=context.canonical,
                )

            anchor = _resolve_anchor(report_day, context, options, intervals)

            runtime_minutes = float(row.get(context.runtime_field, 0.0))
            runtime_hours = max(runtime_minutes, 0.0) / 60.0

            scheduled_minutes = float(row.get(context.scheduled_field, 0.0))
            scheduled_hours = max(scheduled_minutes, 0.0) / 60.0

            _LOGGER.debug(
                "%s energy sample for %s on %s (updated=%s): anchor=%s energy=%.3f runtime_h=%.2f scheduled_h=%.2f intervals=%d",
                context.label,
                entry_identifier,
                report_day.isoformat(),
                updated,
                anchor.isoformat(),
                energy_value,
                runtime_hours,
                scheduled_hours,
                len(intervals),
            )

            latest_runtime_hours = runtime_hours
            latest_scheduled_hours = scheduled_hours
            latest_day = report_day

        if latest_day is not None:
            runtime_updated = True
            zone_summaries[zone_key] = (
                latest_day,
                latest_runtime_hours,
                latest_scheduled_hours,
            )

    sensor_state = accumulator.as_sensor_state()
    if energy_changed or runtime.energy_state is None:
        runtime.energy_state = sensor_state
        runtime_updated = True
        primary_total = float(sensor_state.get("primary", {}).get("energy_sum", 0.0))
        boost_total = float(sensor_state.get("boost", {}).get("energy_sum", 0.0))
        _LOGGER.info(
            "Updated cumulative energy state for %s: primary=%.3f kWh boost=%.3f kWh",
            entry_identifier,
            primary_total,
            boost_total,
        )

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

    runtime.statistics_recent = recent_measurements

    if runtime_updated:
        async_dispatch_runtime_update(hass, entry.entry_id)

    _LOGGER.debug(
        "Secure Meters consumption metrics updated (%s entries)",
        len(processed_rows),
    )


def _energy_store_key(entry: ConfigEntry) -> str:
    """Return the persistent storage key for energy totals."""

    return f"{DOMAIN}_{entry.entry_id}_energy"


def _load_statistics_options(entry: ConfigEntry) -> StatisticsOptions:
    """Derive statistics options from the configuration entry."""

    from .config_flow import (  # Import lazily to avoid circular dependency.  # noqa: PLC0415
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


def _normalize_identifier(value: Any) -> str | None:
    """Return a sanitized identifier candidate when possible."""

    if isinstance(value, bool):
        return None

    if isinstance(value, (str, int, float)):
        candidate = str(value).strip()
        if candidate and candidate.lower() != "none":
            return candidate

    return None
