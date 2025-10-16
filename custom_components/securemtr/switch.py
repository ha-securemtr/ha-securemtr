"""Switch entities for the Secure Meters water heater controller."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, SecuremtrController, SecuremtrRuntimeData
from .beanbag import BeanbagError

_LOGGER = logging.getLogger(__name__)

_CONTROLLER_WAIT_TIMEOUT = 15.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Secure Meters switch entities for a config entry."""

    runtime: SecuremtrRuntimeData = hass.data[DOMAIN][entry.entry_id]

    try:
        await asyncio.wait_for(
            runtime.controller_ready.wait(), _CONTROLLER_WAIT_TIMEOUT
        )
    except TimeoutError as error:
        raise HomeAssistantError(
            "Timed out waiting for Secure Meters controller metadata"
        ) from error

    controller = runtime.controller
    if controller is None:
        raise HomeAssistantError("Secure Meters controller metadata was not available")

    async_add_entities([SecuremtrPowerSwitch(runtime, controller)])


class SecuremtrPowerSwitch(SwitchEntity):
    """Represent a maintained power toggle for the Secure Meters controller."""

    _attr_should_poll = False

    def __init__(
        self, runtime: SecuremtrRuntimeData, controller: SecuremtrController
    ) -> None:
        """Initialise the switch entity with runtime context."""

        self._runtime = runtime
        self._controller = controller
        serial_identifier = controller.serial_number or controller.identifier
        identifier_slug = _slugify_identifier(serial_identifier)
        serial_display = serial_identifier or DOMAIN
        self._attr_unique_id = f"{identifier_slug}_primary_power"
        self._attr_name = f"Secure Meters E7+ {serial_display} Water Heater"

    @property
    def available(self) -> bool:
        """Report whether the underlying WebSocket is connected."""

        return (
            self._runtime.websocket is not None and self._runtime.controller is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information for the controller."""

        controller = self._controller
        serial_identifier = controller.serial_number or controller.identifier
        return DeviceInfo(
            identifiers={(DOMAIN, serial_identifier)},
            manufacturer="Secure Meters",
            model=controller.model or "E7+",
            name="E7+",
            sw_version=controller.firmware_version,
            serial_number=controller.serial_number,
        )

    @property
    def is_on(self) -> bool:
        """Return whether the controller reports the primary power as on."""

        return self._runtime.primary_power_on is True

    async def async_turn_on(self, **kwargs: object) -> None:
        """Send an on command to the Secure Meters controller."""

        await self._async_set_power_state(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Send an off command to the Secure Meters controller."""

        await self._async_set_power_state(False)

    async def _async_set_power_state(self, turn_on: bool) -> None:
        """Drive the backend to the requested primary power state."""

        runtime = self._runtime
        controller = runtime.controller
        session = runtime.session
        websocket = runtime.websocket

        if controller is None or session is None or websocket is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        async with runtime.command_lock:
            try:
                if turn_on:
                    await runtime.backend.turn_controller_on(
                        session,
                        websocket,
                        controller.gateway_id,
                    )
                else:
                    await runtime.backend.turn_controller_off(
                        session,
                        websocket,
                        controller.gateway_id,
                    )
            except BeanbagError as error:
                _LOGGER.error("Failed to toggle Secure Meters controller: %s", error)
                raise HomeAssistantError(
                    "Failed to toggle Secure Meters controller"
                ) from error

            runtime.primary_power_on = turn_on

        hass = self.hass
        if hass is None:
            return

        self.async_write_ha_state()


def _slugify_identifier(identifier: str) -> str:
    """Convert the controller identifier into a slug suitable for unique IDs."""

    return (
        "".join(ch.lower() if ch.isalnum() else "_" for ch in identifier).strip("_")
        or DOMAIN
    )
