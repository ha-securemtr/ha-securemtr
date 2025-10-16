"""Integration setup for securemtr water heater support."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

DOMAIN = "securemtr"

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the securemtr integration."""
    _LOGGER.info("Starting securemtr integration setup")
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info("securemtr integration setup completed")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up securemtr from a config entry."""
    _LOGGER.info(
        "Setting up config entry for securemtr: %s",
        entry.unique_id or entry.entry_id,
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"data": entry.data}
    _LOGGER.info(
        "Config entry setup completed for securemtr: %s",
        entry.unique_id or entry.entry_id,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a securemtr config entry."""
    _LOGGER.info(
        "Unloading securemtr config entry: %s",
        entry.unique_id or entry.entry_id,
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].pop(entry.entry_id, None)
    _LOGGER.info(
        "securemtr config entry unloaded: %s",
        entry.unique_id or entry.entry_id,
    )
    return True
