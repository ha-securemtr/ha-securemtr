"""Integration setup for securemtr water heater support."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
import logging
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientWebSocketResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .beanbag import (
    BeanbagBackend,
    BeanbagError,
    BeanbagGateway,
    BeanbagSession,
    BeanbagStateSnapshot,
)

DOMAIN = "securemtr"

DEFAULT_DEVICE_LABEL = "E7+ Smart Water Heater Controller"

_RUNTIME_UPDATE_SIGNAL = "securemtr_runtime_update"

_LOGGER = logging.getLogger(__name__)


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

    metrics: list[dict[str, Any]] = []
    for sample in samples:
        iso_timestamp = dt_util.utc_from_timestamp(sample.timestamp).isoformat()
        metrics.append(
            {
                "timestamp": iso_timestamp,
                "epoch_seconds": sample.timestamp,
                "primary_energy_kwh": sample.primary_energy_kwh,
                "boost_energy_kwh": sample.boost_energy_kwh,
                "primary_scheduled_minutes": sample.primary_scheduled_minutes,
                "primary_active_minutes": sample.primary_active_minutes,
                "boost_scheduled_minutes": sample.boost_scheduled_minutes,
                "boost_active_minutes": sample.boost_active_minutes,
            }
        )

    if len(metrics) > 7:
        metrics = metrics[-7:]

    runtime.consumption_metrics_log = metrics
    _LOGGER.debug(
        "Secure Meters consumption metrics updated (%s entries): %s",
        len(metrics),
        metrics,
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
