"""Shared entity helpers for the Secure Meters integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, SecuremtrController


def slugify_identifier(identifier: str) -> str:
    """Convert a controller identifier into a slug for unique IDs."""

    return (
        "".join(ch.lower() if ch.isalnum() else "_" for ch in identifier).strip("_")
        or DOMAIN
    )


def build_device_info(controller: SecuremtrController) -> DeviceInfo:
    """Construct device registry metadata for the provided controller."""

    serial_identifier = controller.serial_number or controller.identifier
    serial_display = controller.serial_number or serial_identifier
    device_name = (
        f"E7+ Water Heater (SN: {serial_display})"
        if controller.serial_number
        else f"E7+ Water Heater ({serial_display})"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, serial_identifier)},
        manufacturer="Secure Meters",
        model=controller.model or "E7+",
        name=device_name,
        sw_version=controller.firmware_version,
        serial_number=controller.serial_number,
    )
