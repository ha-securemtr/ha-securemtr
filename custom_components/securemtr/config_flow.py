"""Config flow for the securemtr integration."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class SecuremtrConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle securemtr configuration flows."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial user configuration step."""
        _LOGGER.info("Starting securemtr user configuration step")

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            if len(password) > 12:
                _LOGGER.error(
                    "Secure Controls password exceeds 12 character mobile app limit"
                )
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={CONF_PASSWORD: "password_too_long"},
                )

            normalized_email = email.lower()

            await self.async_set_unique_id(normalized_email)
            self._abort_if_unique_id_configured()

            hashed_password = hashlib.md5(password.encode("utf-8")).hexdigest()

            _LOGGER.info(
                "Secure Controls credentials accepted for %s", normalized_email
            )
            return self.async_create_entry(
                title=email,
                data={CONF_EMAIL: email, CONF_PASSWORD: hashed_password},
            )

        _LOGGER.info("Displaying Secure Controls credential form to user")
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )
