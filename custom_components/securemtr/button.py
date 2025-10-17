"""Button entities for Secure Meters."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import (
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    async_dispatch_runtime_update,
    coerce_end_time,
    consumption_metrics,
    runtime_update_signal,
)
from .beanbag import BeanbagError
from .entity import build_device_info, slugify_identifier
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
        [
            SecuremtrTimedBoostButton(runtime, controller, entry.entry_id, 30),
            SecuremtrTimedBoostButton(runtime, controller, entry.entry_id, 60),
            SecuremtrTimedBoostButton(runtime, controller, entry.entry_id, 120),
            SecuremtrCancelBoostButton(runtime, controller, entry.entry_id),
            SecuremtrConsumptionMetricsButton(runtime, controller, entry.entry_id),
        ]
    )


class _SecuremtrBaseButton(ButtonEntity):
    """Provide shared behaviour for Secure Meters button entities."""
        [SecuremtrConsumptionMetricsButton(hass, entry, runtime, controller)]
    )

    
class SecuremtrConsumptionMetricsButton(ButtonEntity):
    """Trigger a manual refresh of Secure Meters consumption metrics."""

    _attr_should_poll = False

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
    ) -> None:
        """Initialise the button with runtime context and controller metadata."""

        self._runtime = runtime
        self._controller = controller
        self._entry_id = entry_id

    @property
    def available(self) -> bool:
        """Report whether the controller context is currently connected."""

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
        """Return device registry information for the associated controller."""

        return build_device_info(self._controller)

    def _identifier_slug(self) -> str:
        """Return the slugified identifier for the controller."""

        controller = self._controller
        serial_identifier = controller.serial_number or controller.identifier
        return slugify_identifier(serial_identifier)


class SecuremtrTimedBoostButton(_SecuremtrBaseButton):
    """Trigger a timed boost run for a fixed duration."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
        duration_minutes: int,
    ) -> None:
        """Initialise the timed boost button for the requested duration."""

        super().__init__(runtime, controller, entry_id)
        self._duration = duration_minutes
        self._attr_unique_id = f"{self._identifier_slug()}_boost_{duration_minutes}"
        self._attr_name = f"Boost {duration_minutes} minutes"

    async def async_press(self) -> None:
        """Send the timed boost start command for the configured duration."""

        runtime = self._runtime
        controller = runtime.controller
        session = runtime.session
        websocket = runtime.websocket

        if controller is None or session is None or websocket is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        async with runtime.command_lock:
            try:
                await runtime.backend.start_timed_boost(
                    session,
                    websocket,
                    controller.gateway_id,
                    duration_minutes=self._duration,
                )
            except (BeanbagError, ValueError) as error:
                _LOGGER.error("Failed to start Secure Meters timed boost: %s", error)
                raise HomeAssistantError(
                    "Failed to start Secure Meters timed boost"
                ) from error

            runtime.timed_boost_active = True
            now_local = dt_util.now()
            end_local = now_local + timedelta(minutes=self._duration)
            runtime.timed_boost_end_minute = end_local.hour * 60 + end_local.minute
            runtime.timed_boost_end_time = coerce_end_time(
                runtime.timed_boost_end_minute
            )

        hass = self.hass
        if hass is None:
            return

        async_dispatch_runtime_update(hass, self._entry_id)


class SecuremtrCancelBoostButton(_SecuremtrBaseButton):
    """Cancel an active timed boost run."""

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry_id: str,
    ) -> None:
        """Initialise the timed boost cancellation button."""

        super().__init__(runtime, controller, entry_id)
        self._attr_unique_id = f"{self._identifier_slug()}_boost_cancel"
        self._attr_name = "Cancel Boost"

    @property
    def available(self) -> bool:
        """Only expose the button while a timed boost is active."""

        return super().available and self._runtime.timed_boost_active is True

    async def async_press(self) -> None:
        """Send the timed boost stop command."""

        runtime = self._runtime
        controller = runtime.controller
        session = runtime.session
        websocket = runtime.websocket

        if controller is None or session is None or websocket is None:
            raise HomeAssistantError("Secure Meters controller is not connected")

        if runtime.timed_boost_active is not True:
            raise HomeAssistantError("Timed boost is not currently active")

        async with runtime.command_lock:
            try:
                await runtime.backend.stop_timed_boost(
                    session, websocket, controller.gateway_id
                )
            except BeanbagError as error:
                _LOGGER.error("Failed to cancel Secure Meters timed boost: %s", error)
                raise HomeAssistantError(
                    "Failed to cancel Secure Meters timed boost"
                ) from error

            runtime.timed_boost_active = False
            runtime.timed_boost_end_minute = None
            runtime.timed_boost_end_time = None

        hass = self.hass
        if hass is None:
            return

        async_dispatch_runtime_update(hass, self._entry_id)


__all__ = [
    "SecuremtrTimedBoostButton",
    "SecuremtrCancelBoostButton",
]