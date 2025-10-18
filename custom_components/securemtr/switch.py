"""Switch entities for the Secure Meters water heater controller."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    async_dispatch_runtime_update,
    async_run_with_reconnect,
    runtime_update_signal,
)
from .entity import build_device_info, slugify_identifier
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

    async_add_entities(
        [
            SecuremtrPowerSwitch(runtime, controller, entry),
            SecuremtrTimedBoostSwitch(runtime, controller, entry),
        ]
    )


class _SecuremtrBaseSwitch(SwitchEntity):
    """Provide shared behaviour for Secure Meters switch entities."""

    _attr_should_poll = False

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the switch with runtime context and controller metadata."""

        self._runtime = runtime
        self._controller = controller
        self._entry = entry
        self._entry_id = entry.entry_id

    @property
    def available(self) -> bool:
        """Report whether the underlying WebSocket is connected."""

        return (
            self._runtime.websocket is not None and self._runtime.controller is not None
        )

    async def async_added_to_hass(self) -> None:
        """Register runtime callbacks when the entity is added to Home Assistant."""

        await super().async_added_to_hass()
        hass = self.hass
        if hass is None:
            return

        remove = async_dispatcher_connect(
            hass, runtime_update_signal(self._entry_id), self.async_write_ha_state
        )
        self.async_on_remove(remove)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information for the controller."""

        return build_device_info(self._controller)

    def _identifier_slug(self) -> str:
        """Return the slugified identifier for the controller."""

        controller = self._controller
        serial_identifier = controller.serial_number or controller.identifier
        return slugify_identifier(serial_identifier)


class SecuremtrPowerSwitch(_SecuremtrBaseSwitch):
    """Represent a maintained power toggle for the Secure Meters controller."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
    ) -> None:
        """Initialise the switch entity with runtime context."""

        super().__init__(runtime, controller, entry_id)
        self._attr_unique_id = f"{self._identifier_slug()}_primary_power"
        self._attr_name = "E7+ Controller"

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

        if controller is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        entry = self._entry
        async with runtime.command_lock:
            try:
                await async_run_with_reconnect(
                    entry,
                    runtime,
                    (
                        lambda backend, session, websocket: backend.turn_controller_on(
                            session,
                            websocket,
                            controller.gateway_id,
                        )
                        if turn_on
                        else backend.turn_controller_off(
                            session,
                            websocket,
                            controller.gateway_id,
                        )
                    ),
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
        async_dispatch_runtime_update(hass, self._entry_id)


class SecuremtrTimedBoostSwitch(_SecuremtrBaseSwitch):
    """Expose the timed boost feature toggle reported by Beanbag."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
    ) -> None:
        """Initialise the timed boost switch for the controller."""

        super().__init__(runtime, controller, entry_id)
        self._attr_unique_id = f"{self._identifier_slug()}_timed_boost"
        self._attr_name = "Timed Boost"

    @property
    def is_on(self) -> bool:
        """Return whether timed boost is currently enabled."""

        return self._runtime.timed_boost_enabled is True

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable the timed boost feature in the backend."""

        await self._async_set_timed_boost(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable the timed boost feature in the backend."""

        await self._async_set_timed_boost(False)

    async def _async_set_timed_boost(self, enabled: bool) -> None:
        """Drive the backend to the requested timed boost state."""

        runtime = self._runtime
        controller = runtime.controller

        if controller is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        entry = self._entry
        async with runtime.command_lock:
            try:
                await async_run_with_reconnect(
                    entry,
                    runtime,
                    lambda backend, session, websocket: backend.set_timed_boost_enabled(
                        session,
                        websocket,
                        controller.gateway_id,
                        enabled=enabled,
                    ),
                )
            except BeanbagError as error:
                _LOGGER.error(
                    "Failed to toggle Secure Meters timed boost feature: %s", error
                )
                raise HomeAssistantError(
                    "Failed to toggle Secure Meters timed boost feature"
                ) from error

            runtime.timed_boost_enabled = enabled

        hass = self.hass
        if hass is None:
            return

        self.async_write_ha_state()
        async_dispatch_runtime_update(hass, self._entry_id)
