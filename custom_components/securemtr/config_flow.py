"""Config flow for the securemtr integration."""

from __future__ import annotations

from datetime import time
import hashlib
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import selector
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import (
    CONF_GATEWAY_ID,
    DOMAIN,
    _async_close_client_session,
    async_get_clientsession,
)
from .beanbag import BeanbagBackend, BeanbagError, BeanbagGateway, BeanbagSession

CONF_PRIMARY_ANCHOR = "primary_anchor"
CONF_BOOST_ANCHOR = "boost_anchor"
CONF_ELEMENT_POWER_KW = "element_power_kw"
CONF_PREFER_DEVICE_ENERGY = "prefer_device_energy"

DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_PRIMARY_ANCHOR = "03:00"
DEFAULT_BOOST_ANCHOR = "17:00"
DEFAULT_ELEMENT_POWER_KW = 2.85
DEFAULT_PREFER_DEVICE_ENERGY = True

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

    def __init__(self) -> None:
        """Initialise flow state for credential and gateway handling."""

        self._email: str | None = None
        self._password_digest: str | None = None
        self._gateways: tuple[BeanbagGateway, ...] = ()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial user configuration step."""
        _LOGGER.info("Starting SecureMTR user configuration step")

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            if not email:
                _LOGGER.error("Secure Controls email is required")
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={CONF_EMAIL: "invalid_email"},
                )

            if not password:
                _LOGGER.error("Secure Controls password is required")
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={CONF_PASSWORD: "password_required"},
                )

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

            try:
                session = await self._async_login_gateways(email, hashed_password)
            except BeanbagError:
                _LOGGER.error("Secure Controls login failed during configuration")
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={"base": "cannot_connect"},
                )
            except Exception:
                _LOGGER.exception("Unexpected error during Secure Controls login")
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={"base": "unknown"},
                )

            gateways = session.gateways
            if not gateways:
                _LOGGER.error(
                    "Secure Controls account has no associated gateways during setup"
                )
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={"base": "cannot_connect"},
                )

            self._email = email
            self._password_digest = hashed_password
            self._gateways = gateways

            _LOGGER.info("Secure Controls app credentials accepted")
            if len(gateways) == 1:
                return self._async_create_config_entry(gateways[0])

            return await self.async_step_gateway()

        _LOGGER.info(
            "Displaying SecureMTR configuration form for Secure Controls credentials"
        )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    async def async_step_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle gateway selection when multiple controllers are available."""

        if user_input is not None:
            gateway_id = user_input[CONF_GATEWAY_ID]
            gateway = next(
                (item for item in self._gateways if item.gateway_id == gateway_id),
                None,
            )

            if gateway is None:
                _LOGGER.error("Invalid gateway selection %s", gateway_id)
                return await self.async_step_gateway()

            return self._async_create_config_entry(gateway)

        options = {
            gateway.gateway_id: self._gateway_label(gateway)
            for gateway in self._gateways
        }

        schema = vol.Schema({vol.Required(CONF_GATEWAY_ID): vol.In(options)})
        return self.async_show_form(step_id="gateway", data_schema=schema)

    async def _async_login_gateways(
        self, email: str, password_digest: str
    ) -> BeanbagSession:
        """Authenticate with Beanbag to enumerate available gateways."""

        session = async_get_clientsession(self.hass)
        backend = BeanbagBackend(session)
        try:
            return await backend.login(email, password_digest)
        finally:
            await _async_close_client_session(session)

    def _async_create_config_entry(self, gateway: BeanbagGateway) -> FlowResult:
        """Create the config entry once a gateway has been selected."""

        if self._email is None or self._password_digest is None:
            raise ValueError("Credential state unavailable for entry creation")

        data: dict[str, Any] = {
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password_digest,
            CONF_GATEWAY_ID: gateway.gateway_id,
        }

        serial = gateway.serial_number
        if isinstance(serial, str) and serial.strip():
            data["serial_number"] = serial.strip()

        return self.async_create_entry(title="SecureMTR", data=data)

    @staticmethod
    def _gateway_label(gateway: BeanbagGateway) -> str:
        """Return a human-friendly label for a discovered gateway."""

        serial = gateway.serial_number
        if isinstance(serial, str) and serial.strip():
            return f"{serial.strip()} ({gateway.gateway_id})"
        return gateway.gateway_id

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

    def _resolve_install_timezone(self) -> str:
        """Return the Home Assistant installation timezone."""

        hass_timezone: str | None = None
        if self.hass is not None:
            hass_timezone = getattr(self.hass.config, "time_zone", None)

        if hass_timezone:
            timezone = dt_util.get_time_zone(hass_timezone)
            if timezone is not None:
                return hass_timezone
            _LOGGER.warning(
                "Invalid Home Assistant timezone %s; using default %s",
                hass_timezone,
                DEFAULT_TIMEZONE,
            )
        else:
            _LOGGER.warning(
                "Home Assistant timezone unavailable; using default %s",
                DEFAULT_TIMEZONE,
            )

        return DEFAULT_TIMEZONE

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle SecureMTR options for runtime statistics."""

        if user_input is not None:
            timezone_name = self._resolve_install_timezone()
            primary_anchor = _anchor_option_to_time(
                user_input.get(CONF_PRIMARY_ANCHOR), _DEFAULT_PRIMARY_TIME
            )
            boost_anchor = _anchor_option_to_time(
                user_input.get(CONF_BOOST_ANCHOR), _DEFAULT_BOOST_TIME
            )
            return self.async_create_entry(
                title="",
                data={
                    CONF_TIME_ZONE: timezone_name,
                    CONF_PRIMARY_ANCHOR: _serialize_anchor(primary_anchor),
                    CONF_BOOST_ANCHOR: _serialize_anchor(boost_anchor),
                    CONF_ELEMENT_POWER_KW: user_input[CONF_ELEMENT_POWER_KW],
                    CONF_PREFER_DEVICE_ENERGY: user_input[CONF_PREFER_DEVICE_ENERGY],
                },
            )

        options = self._config_entry.options
        primary_anchor_default = _anchor_option_to_time(
            options.get(CONF_PRIMARY_ANCHOR), _DEFAULT_PRIMARY_TIME
        )
        boost_anchor_default = _anchor_option_to_time(
            options.get(CONF_BOOST_ANCHOR), _DEFAULT_BOOST_TIME
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PRIMARY_ANCHOR,
                    default=primary_anchor_default,
                ): selector({"time": {}}),
                vol.Required(
                    CONF_BOOST_ANCHOR,
                    default=boost_anchor_default,
                ): selector({"time": {}}),
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
