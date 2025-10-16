"""Config flow for the SecureMTR integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


class SecuremtrConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle SecureMTR configuration flows."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial user configuration step."""
        _LOGGER.info("Starting SecureMTR user configuration step")

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            normalized_email = email.lower()

            await self.async_set_unique_id(normalized_email)
            self._abort_if_unique_id_configured()

            _LOGGER.info(
                "SecureMTR credentials accepted for %s", normalized_email
            )
            return self.async_create_entry(
                title=email,
                data={CONF_EMAIL: email, CONF_PASSWORD: password},
            )

        _LOGGER.info("Displaying SecureMTR credential form to user")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
        )
