"""Button entities for the Secure Meters water heater controller."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
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
    """Set up Secure Meters button entities for a config entry."""

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

    async_add_entities([SecuremtrPowerButton(runtime, controller)])


class SecuremtrPowerButton(ButtonEntity):
    """Represent a stateless power toggle for the Secure Meters controller."""

    _attr_should_poll = False

    def __init__(
        self, runtime: SecuremtrRuntimeData, controller: SecuremtrController
    ) -> None:
        """Initialise the button entity with runtime context."""

        self._runtime = runtime
        self._controller = controller
        identifier_slug = _slugify_identifier(controller.identifier)
        self._attr_unique_id = f"{identifier_slug}_primary_power"
        self._attr_name = f"{controller.name} {controller.identifier} power"

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
        return DeviceInfo(
            identifiers={(DOMAIN, controller.identifier)},
            manufacturer="Secure Meters",
            model=controller.model,
            name=controller.name,
            sw_version=controller.firmware_version,
            serial_number=controller.serial_number,
        )

    async def async_press(self) -> None:
        """Send a toggle command to the Secure Meters controller."""

        runtime = self._runtime
        controller = runtime.controller
        session = runtime.session
        websocket = runtime.websocket

        if controller is None or session is None or websocket is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        turn_on = runtime.primary_power_on is not True

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


def _slugify_identifier(identifier: str) -> str:
    """Convert the controller identifier into a slug suitable for unique IDs."""

    return (
        "".join(ch.lower() if ch.isalnum() else "_" for ch in identifier).strip("_")
        or DOMAIN
    )
