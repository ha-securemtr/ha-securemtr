"""Config flow for the securemtr integration."""

from __future__ import annotations

from datetime import time
import hashlib
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.data_entry_flow import FlowResult
import voluptuous as vol

from . import DOMAIN

CONF_PRIMARY_ANCHOR = "primary_anchor"
CONF_BOOST_ANCHOR = "boost_anchor"
CONF_ANCHOR_STRATEGY = "anchor_strategy"
CONF_ELEMENT_POWER_KW = "element_power_kw"
CONF_PREFER_DEVICE_ENERGY = "prefer_device_energy"

DEFAULT_TIMEZONE = "Europe/Dublin"
DEFAULT_PRIMARY_ANCHOR = "03:00"
DEFAULT_BOOST_ANCHOR = "17:00"
DEFAULT_ANCHOR_STRATEGY = "midpoint"
DEFAULT_ELEMENT_POWER_KW = 2.85
DEFAULT_PREFER_DEVICE_ENERGY = True

ANCHOR_STRATEGIES: tuple[str, ...] = ("midpoint", "start", "end", "fixed")
_DEFAULT_PRIMARY_TIME = time.fromisoformat(DEFAULT_PRIMARY_ANCHOR)
_DEFAULT_BOOST_TIME = time.fromisoformat(DEFAULT_BOOST_ANCHOR)

_LOGGER = logging.getLogger(__name__)


def _anchor_option_to_time(value: Any, fallback: time) -> time:
    """Return an anchor time for the provided stored option."""

    if isinstance(value, time):
        return value

    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError:
            _LOGGER.debug("Invalid anchor string %s, using fallback", value)

    return fallback


def _serialize_anchor(value: time) -> str:
    """Return an ISO-formatted anchor string for storage."""

    if value.microsecond:
        return value.isoformat(timespec="microseconds")
    if value.second:
        return value.isoformat(timespec="seconds")
    return value.isoformat(timespec="minutes")


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


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

            _LOGGER.info("Secure Controls app credentials accepted")
            return self.async_create_entry(
                title="SecureMTR",
                data={CONF_EMAIL: email, CONF_PASSWORD: hashed_password},
            )

        _LOGGER.info(
            "Displaying SecureMTR configuration form for Secure Controls credentials"
        )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler for SecureMTR."""

        return SecuremtrOptionsFlowHandler(config_entry)


class SecuremtrOptionsFlowHandler(config_entries.OptionsFlow):
    """Configure SecureMTR runtime statistics options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise the options flow with the stored config entry."""

        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle SecureMTR options for runtime statistics."""

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_TIME_ZONE: user_input[CONF_TIME_ZONE],
                    CONF_PRIMARY_ANCHOR: _serialize_anchor(
                        user_input[CONF_PRIMARY_ANCHOR]
                    ),
                    CONF_BOOST_ANCHOR: _serialize_anchor(user_input[CONF_BOOST_ANCHOR]),
                    CONF_ANCHOR_STRATEGY: user_input[CONF_ANCHOR_STRATEGY],
                    CONF_ELEMENT_POWER_KW: user_input[CONF_ELEMENT_POWER_KW],
                    CONF_PREFER_DEVICE_ENERGY: user_input[CONF_PREFER_DEVICE_ENERGY],
                },
            )

        options = self._config_entry.options
        anchor_strategy = options.get(CONF_ANCHOR_STRATEGY, DEFAULT_ANCHOR_STRATEGY)
        if anchor_strategy not in ANCHOR_STRATEGIES:
            anchor_strategy = DEFAULT_ANCHOR_STRATEGY

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TIME_ZONE,
                    default=options.get(CONF_TIME_ZONE, DEFAULT_TIMEZONE),
                ): cv.time_zone,
                vol.Required(
                    CONF_PRIMARY_ANCHOR,
                    default=_anchor_option_to_time(
                        options.get(CONF_PRIMARY_ANCHOR), _DEFAULT_PRIMARY_TIME
                    ),
                ): cv.time,
                vol.Required(
                    CONF_BOOST_ANCHOR,
                    default=_anchor_option_to_time(
                        options.get(CONF_BOOST_ANCHOR), _DEFAULT_BOOST_TIME
                    ),
                ): cv.time,
                vol.Required(
                    CONF_ANCHOR_STRATEGY, default=anchor_strategy
                ): vol.In(ANCHOR_STRATEGIES),
                vol.Required(
                    CONF_ELEMENT_POWER_KW,
                    default=float(
                        options.get(CONF_ELEMENT_POWER_KW, DEFAULT_ELEMENT_POWER_KW)
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
                vol.Required(
                    CONF_PREFER_DEVICE_ENERGY,
                    default=options.get(
                        CONF_PREFER_DEVICE_ENERGY, DEFAULT_PREFER_DEVICE_ENERGY
                    ),
                ): cv.boolean,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
