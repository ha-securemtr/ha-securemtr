"""Integration setup for securemtr water heater support."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import logging
from typing import Any

from aiohttp import ClientWebSocketResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .beanbag import (
    BeanbagBackend,
    BeanbagError,
    BeanbagGateway,
    BeanbagSession,
    BeanbagStateSnapshot,
)

DOMAIN = "securemtr"

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
    zone_topology: list[dict[str, Any]] | None = None
    schedule_overview: dict[str, Any] | None = None
    device_metadata: dict[str, Any] | None = None
    device_configuration: dict[str, Any] | None = None
    state_snapshot: BeanbagStateSnapshot | None = None


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

    config_entries_helper = getattr(hass, "config_entries", None)
    if config_entries_helper is not None:
        await config_entries_helper.async_forward_entry_setups(entry, ["switch"])
    else:
        _LOGGER.debug(
            "config_entries helper unavailable; skipping switch platform setup for %s",
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
            entry, ["switch"]
        )
    else:
        unload_ok = True
        _LOGGER.debug(
            "config_entries helper unavailable; skipping switch platform unload for %s",
            entry_identifier,
        )

    if runtime is None:
        _LOGGER.info("securemtr config entry unloaded: %s", entry_identifier)
        return unload_ok

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

    return _build_controller(metadata, gateway)


def _build_controller(
    metadata: dict[str, Any], gateway: BeanbagGateway
) -> SecuremtrController:
    """Translate metadata and gateway context into a controller object."""

    identifier_candidates = (
        str(metadata.get("BOI", "")).strip(),
        str(metadata.get("SN", "")).strip(),
        str(gateway.gateway_id or "").strip(),
    )

    identifier = next(
        (candidate for candidate in identifier_candidates if candidate), DOMAIN
    )

    raw_name = metadata.get("N") or "SecureMTR Controller"
    name = str(raw_name).strip() or f"SecureMTR {identifier}"

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

    return SecuremtrController(
        identifier=identifier,
        name=name,
        gateway_id=gateway.gateway_id,
        serial_number=serial_number,
        firmware_version=firmware_version,
        model=model,
    )
