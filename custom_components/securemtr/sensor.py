"""Sensors for Secure Meters timed boost metadata."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    runtime_update_signal,
)
from .entity import build_device_info, slugify_identifier

_LOGGER = logging.getLogger(__name__)

_CONTROLLER_WAIT_TIMEOUT = 15.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Secure Meters timed boost sensors."""

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
        [SecuremtrBoostEndsSensor(runtime, controller, entry.entry_id)]
    )


class _SecuremtrBaseSensor(SensorEntity):
    """Provide shared behaviour for Secure Meters sensors."""

    _attr_should_poll = False

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
    ) -> None:
        """Initialise the sensor with runtime context and controller metadata."""

        self._runtime = runtime
        self._controller = controller
        self._entry_id = entry_id

    @property
    def available(self) -> bool:
        """Return whether the backend is currently connected."""

        return (
            self._runtime.websocket is not None and self._runtime.controller is not None
        )

    async def async_added_to_hass(self) -> None:
        """Register dispatcher callbacks when added to Home Assistant."""

        await super().async_added_to_hass()
        hass = self.hass
        if hass is None:
            return

        remove = async_dispatcher_connect(
            hass, runtime_update_signal(self._entry_id), self.async_write_ha_state
        )
        self.async_on_remove(remove)

    @property
    def device_info(self) -> dict[str, object]:
        """Return device registry information for the controller."""

        return build_device_info(self._controller)

    def _identifier_slug(self) -> str:
        """Return the slugified identifier for the controller."""

        controller = self._controller
        serial_identifier = controller.serial_number or controller.identifier
        return slugify_identifier(serial_identifier)


class SecuremtrBoostEndsSensor(_SecuremtrBaseSensor):
    """Report the expected end time of the active boost run."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
    ) -> None:
        """Initialise the boost end-time sensor."""

        super().__init__(runtime, controller, entry_id)
        self._attr_unique_id = f"{self._identifier_slug()}_boost_ends"
        self._attr_name = "Boost Ends"

    @property
    def native_value(self) -> datetime | None:
        """Return the boost end timestamp when active."""

        if self._runtime.timed_boost_active is not True:
            return None
        return self._runtime.timed_boost_end_time


__all__ = ["SecuremtrBoostEndsSensor"]
