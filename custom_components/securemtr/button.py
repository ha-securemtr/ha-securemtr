"""Button entities for Secure Meters auxiliary actions."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, SecuremtrController, SecuremtrRuntimeData, consumption_metrics
from .switch import _build_device_info, _slugify_identifier

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

    async_add_entities(
        [SecuremtrConsumptionMetricsButton(hass, entry, runtime, controller)]
    )


class SecuremtrConsumptionMetricsButton(ButtonEntity):
    """Trigger a manual refresh of Secure Meters consumption metrics."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
    ) -> None:
        """Initialise the button entity with runtime context."""

        self._hass = hass
        self._entry = entry
        self._runtime = runtime
        self._controller = controller
        slug = _slugify_identifier(controller.serial_number or controller.identifier)
        self._attr_unique_id = f"{slug}_consumption_metrics"
        self._attr_name = "Get Consumption Metrics"

    @property
    def available(self) -> bool:
        """Return whether the integration runtime is ready."""

        runtime = self._runtime
        return runtime.websocket is not None and runtime.controller is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry information for this controller."""

        return _build_device_info(self._controller)

    async def async_press(self) -> None:
        """Request an immediate consumption metrics refresh."""

        _LOGGER.debug("Manual consumption metrics refresh requested")
        await consumption_metrics(self._hass, self._entry)
