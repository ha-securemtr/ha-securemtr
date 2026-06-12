"""Shared entity helpers for the Secure Meters integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import (
    CONNECTION_MODE_LOCAL_BLE,
    DEFAULT_DEVICE_LABEL,
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    async_get_local_ble_worker,
    runtime_update_signal,
)
from .runtime_helpers import (
    MutationCallable,
    OperationCallable,
    async_dispatch_runtime_update,
    async_mutate_runtime,
    controller_gateway_operation,
)

_LOGGER = logging.getLogger(__name__)

_CONTROLLER_READY_TIMEOUT = 15.0


async def async_get_ready_controller(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[SecuremtrRuntimeData, SecuremtrController]:
    """Return runtime context and controller metadata once ready."""

    runtime: SecuremtrRuntimeData = hass.data[DOMAIN][entry.entry_id]

    try:
        await asyncio.wait_for(
            runtime.controller_ready.wait(), _CONTROLLER_READY_TIMEOUT
        )
    except TimeoutError as error:
        raise HomeAssistantError(
            "Timed out waiting for Secure Meters controller metadata"
        ) from error

    controller = runtime.controller
    if controller is None:
        raise HomeAssistantError("Secure Meters controller metadata was not available")

    return runtime, controller


def slugify_identifier(identifier: str) -> str:
    """Convert a controller identifier into a slug for unique IDs."""

    return (
        "".join(ch.lower() if ch.isalnum() else "_" for ch in identifier).strip("_")
        or DOMAIN
    )


def build_device_info(controller: SecuremtrController) -> DeviceInfo:
    """Construct device registry metadata for the provided controller."""

    serial_identifier = controller.serial_number or controller.identifier
    device_name = DEFAULT_DEVICE_LABEL
    return DeviceInfo(
        identifiers={(DOMAIN, serial_identifier)},
        manufacturer="Secure Meters",
        model=controller.model or "E7+",
        name=device_name,
        sw_version=controller.firmware_version,
        serial_number=controller.serial_number,
    )


def controller_display_label(controller: SecuremtrController) -> str:
    """Return the preferred display label for a controller."""

    def _clean(value: str | None) -> str | None:
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                return candidate
        return None

    return (
        _clean(controller.name)
        or _clean(controller.serial_number)
        or _clean(controller.identifier)
        or _clean(controller.gateway_id)
        or DEFAULT_DEVICE_LABEL
    )


class SecuremtrRuntimeEntityMixin:
    """Provide shared runtime helpers for Secure Meters entities."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        runtime: SecuremtrRuntimeData,
        controller: SecuremtrController,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the entity with runtime context and dispatcher hooks."""

        super().__init__()
        self._runtime = runtime
        self._controller = controller
        if not isinstance(entry, ConfigEntry):
            raise TypeError("SecureMTR entities require a Home Assistant ConfigEntry")
        self._entry = entry
        self._entry_id = entry.entry_id

    @property
    def available(self) -> bool:
        """Return whether the controller metadata is currently available."""

        return self._runtime_connected()

    def _runtime_connected(self) -> bool:
        """Return whether the backend runtime currently exposes controller state."""

        if self._runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
            return True

        # Cloud
        return (
            self._runtime.websocket is not None and self._runtime.controller is not None
        )

    async def async_added_to_hass(self) -> None:
        """Register dispatcher callbacks when the entity is added to Home Assistant."""

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
        """Return device registry information for the associated controller."""

        return build_device_info(self._controller)

    def _identifier_slug(self) -> str:
        """Return the slugified identifier for the controller."""

        controller = self._controller
        identifier = controller.serial_number or controller.identifier
        return slugify_identifier(identifier)

    def _set_slug_identifiers(
        self, *suffix_parts: str, entity_domain: str | None = None
    ) -> str:
        """Assign slug-based identifiers for the entity."""

        slug = self._identifier_slug()
        suffix = "_".join(part for part in suffix_parts if part)
        unique_id = f"{slug}_{suffix}" if suffix else slug
        self._attr_unique_id = unique_id
        if entity_domain:
            self.entity_id = f"{entity_domain}.securemtr_{unique_id}"
        return unique_id

    async def _async_mutate(
        self,
        *,
        operation: OperationCallable,
        mutation: MutationCallable,
        log_context: str,
        error_message: str | None = None,
        exception_types: tuple[type[Exception], ...] | type[Exception] | None = None,
        write_state: bool = False,
    ) -> Any:
        """Execute a runtime mutation using the entity's stored context."""

        if exception_types is None:
            exception_tuple: tuple[type[Exception], ...] | None = None
        elif isinstance(exception_types, tuple):
            exception_tuple = exception_types
        else:
            exception_tuple = (exception_types,)

        write_ha_state = self.async_write_ha_state if write_state else None

        return await async_mutate_runtime(
            self._runtime,
            self._entry,
            entry_id=self._entry_id,
            hass=self.hass,
            operation=operation,
            mutation=mutation,
            log_context=log_context,
            error_message=error_message,
            exception_types=exception_tuple,
            write_ha_state=write_ha_state,
        )


class SecuremtrCommandMixin(SecuremtrRuntimeEntityMixin):
    """Provide helpers for executing controller gateway commands."""

    async def _async_controller_command(
        self,
        method_name: str,
        /,
        *,
        runtime_update: MutationCallable,
        log_context: str,
        error_message: str | None = None,
        exception_types: tuple[type[Exception], ...] | type[Exception] | None = None,
        write_state: bool = False,
        **operation_kwargs: Any,
    ) -> Any:
        """Execute a controller command using the runtime mutation helper."""

        if self._runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE:
            hass = self.hass
            if hass is None:
                raise HomeAssistantError("Home Assistant instance is not available")

            from .local_ble_commissioning import (  # noqa: PLC0415
                LocalBleCommissioningError,
                LocalBlePriority,
            )

            try:
                worker = await async_get_local_ble_worker(
                    hass,
                    self._entry,
                    self._runtime,
                )
                result = await worker.async_execute_local_command(
                    method_name=method_name,
                    operation_kwargs=operation_kwargs,
                    priority=LocalBlePriority.USER_COMMAND,
                )
            except (LocalBleCommissioningError, ValueError) as error:
                _LOGGER.error("%s: %s", log_context, error)
                raise HomeAssistantError(error_message or log_context) from error

            mutation_result = runtime_update(self._runtime)
            if asyncio.iscoroutine(mutation_result):
                await mutation_result
            if write_state:
                self.async_write_ha_state()
            async_dispatch_runtime_update(hass, self._entry_id)
            return result

        operation = controller_gateway_operation(method_name, **operation_kwargs)
        return await self._async_mutate(
            operation=operation,
            mutation=runtime_update,
            log_context=log_context,
            error_message=error_message,
            exception_types=exception_types,
            write_state=write_state,
        )
