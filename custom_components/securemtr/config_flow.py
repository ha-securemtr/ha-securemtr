"""Config flow for the securemtr integration."""

from __future__ import annotations

from datetime import time
import hashlib
import logging
import string
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import selector
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import DOMAIN
from .local_ble_commissioning import (
    LocalBleCommissioningError,
    async_commission_local_ble,
)

CONF_CONNECTION_MODE = "connection_mode"
CONF_DEVICE_TYPE = "device_type"
CONF_LOCAL_BLE_KEY = "local_ble_key"
CONF_LOCAL_OWNER_TOKEN = "local_owner_token"
CONF_MAC_ADDRESS = "mac_address"
CONF_SERIAL_NUMBER = "serial_number"
CONF_START_COMMISSIONING = "start_commissioning"

CONNECTION_MODE_CLOUD = "cloud"
CONNECTION_MODE_LOCAL_BLE = "local_ble"

DEVICE_TYPE_E7_PLUS = "e7plus"

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
        vol.Required(
            CONF_CONNECTION_MODE,
            default=CONNECTION_MODE_CLOUD,
        ): vol.In((CONNECTION_MODE_CLOUD, CONNECTION_MODE_LOCAL_BLE)),
    }
)

STEP_CLOUD_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_LOCAL_BLE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SERIAL_NUMBER): str,
        vol.Required(CONF_MAC_ADDRESS): str,
        vol.Required(CONF_DEVICE_TYPE, default=DEVICE_TYPE_E7_PLUS): vol.In(
            (DEVICE_TYPE_E7_PLUS,)
        ),
    }
)


def _normalize_mac(value: str) -> str | None:
    """Normalize a MAC string to 12 uppercase hexadecimal characters."""

    compact = value.strip().replace(":", "").replace("-", "")
    if len(compact) != 12:
        return None
    if any(char not in string.hexdigits for char in compact):
        return None
    return compact.upper()


def _local_unique_id(mac_address: str) -> str:
    """Build a unique ID for local BLE entries."""

    return f"{CONNECTION_MODE_LOCAL_BLE}:{mac_address}"


class SecuremtrConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle SecureMTR configuration flows."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise transient local BLE flow state."""

        super().__init__()
        self._pending_local_ble: dict[str, str] | None = None
        self._pending_reconfigure_local_ble: dict[str, str] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial user configuration step."""
        _LOGGER.info("Starting SecureMTR user configuration step")

        if user_input is not None:
            if CONF_CONNECTION_MODE in user_input:
                connection_mode = user_input[CONF_CONNECTION_MODE]
                if connection_mode == CONNECTION_MODE_LOCAL_BLE:
                    return await self.async_step_local_ble()
                return await self.async_step_cloud()

            if CONF_EMAIL in user_input and CONF_PASSWORD in user_input:
                return await self._async_handle_cloud_credentials(user_input)

        _LOGGER.info("Displaying SecureMTR connection mode selection form")
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the cloud credential step."""

        if user_input is not None:
            return await self._async_handle_cloud_credentials(user_input)

        _LOGGER.info(
            "Displaying SecureMTR configuration form for Secure Controls credentials"
        )
        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_DATA_SCHEMA,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle SecureMTR reconfiguration flow for existing entries."""

        entry = self._get_reconfigure_entry()
        current_mode_raw = entry.data.get(CONF_CONNECTION_MODE)
        current_mode = (
            current_mode_raw.strip().lower()
            if isinstance(current_mode_raw, str)
            else CONNECTION_MODE_CLOUD
        )
        if current_mode not in (CONNECTION_MODE_CLOUD, CONNECTION_MODE_LOCAL_BLE):
            current_mode = CONNECTION_MODE_CLOUD

        selection_schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONNECTION_MODE,
                    default=current_mode,
                ): vol.In((CONNECTION_MODE_CLOUD, CONNECTION_MODE_LOCAL_BLE)),
            }
        )

        if user_input is not None:
            if user_input[CONF_CONNECTION_MODE] == CONNECTION_MODE_LOCAL_BLE:
                return await self.async_step_reconfigure_local_ble()
            return await self.async_step_reconfigure_cloud()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=selection_schema,
        )

    async def async_step_reconfigure_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle cloud credential updates for an existing entry."""

        entry = self._get_reconfigure_entry()

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]

            if not email:
                return self.async_show_form(
                    step_id="reconfigure_cloud",
                    data_schema=STEP_CLOUD_DATA_SCHEMA,
                    errors={CONF_EMAIL: "invalid_email"},
                )

            if not password:
                return self.async_show_form(
                    step_id="reconfigure_cloud",
                    data_schema=STEP_CLOUD_DATA_SCHEMA,
                    errors={CONF_PASSWORD: "password_required"},
                )

            if len(password) > 12:
                return self.async_show_form(
                    step_id="reconfigure_cloud",
                    data_schema=STEP_CLOUD_DATA_SCHEMA,
                    errors={CONF_PASSWORD: "password_too_long"},
                )

            normalized_email = email.lower()
            for existing in self._async_current_entries():
                if (
                    existing.entry_id != entry.entry_id
                    and existing.unique_id == normalized_email
                ):
                    return self.async_abort(reason="already_configured")

            hashed_password = hashlib.md5(password.encode("utf-8")).hexdigest()

            if self.hass is None:
                raise RuntimeError("Home Assistant instance is not available")

            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD,
                    CONF_EMAIL: email,
                    CONF_PASSWORD: hashed_password,
                },
                title="SecureMTR",
                unique_id=normalized_email,
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure_cloud",
            data_schema=STEP_CLOUD_DATA_SCHEMA,
        )

    async def async_step_reconfigure_local_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle local BLE metadata updates for an existing entry."""

        errors: dict[str, str] = {}

        if user_input is not None:
            serial_number = user_input[CONF_SERIAL_NUMBER].strip()
            mac_address = _normalize_mac(user_input[CONF_MAC_ADDRESS])
            device_type = user_input[CONF_DEVICE_TYPE].strip().lower()

            if len(serial_number) != 8:
                errors[CONF_SERIAL_NUMBER] = "invalid_serial"

            if mac_address is None:
                errors[CONF_MAC_ADDRESS] = "invalid_mac"

            if device_type != DEVICE_TYPE_E7_PLUS:
                errors[CONF_DEVICE_TYPE] = "invalid_device_type"

            if not errors and mac_address is not None:
                entry = self._get_reconfigure_entry()
                unique_id = _local_unique_id(mac_address)
                for existing in self._async_current_entries():
                    if (
                        existing.entry_id != entry.entry_id
                        and existing.unique_id == unique_id
                    ):
                        return self.async_abort(reason="already_configured")

                self._pending_reconfigure_local_ble = {
                    CONF_SERIAL_NUMBER: serial_number,
                    CONF_MAC_ADDRESS: mac_address,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
                }
                return await self.async_step_reconfigure_local_ble_commissioning()

        return self.async_show_form(
            step_id="reconfigure_local_ble",
            data_schema=STEP_LOCAL_BLE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure_local_ble_commissioning(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run local BLE commissioning during reconfigure before saving entry."""

        pending = self._pending_reconfigure_local_ble
        if pending is None:
            return self.async_abort(reason="reconfigure_failed")

        if user_input is not None:
            if user_input.get(CONF_START_COMMISSIONING) is not True:
                return self.async_show_form(
                    step_id="reconfigure_local_ble_commissioning",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_START_COMMISSIONING,
                                default=False,
                            ): bool
                        }
                    ),
                    errors={"base": "commissioning_not_started"},
                    description_placeholders={
                        CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                        CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    },
                )

            entry = self._get_reconfigure_entry()
            if self.hass is None:
                raise RuntimeError("Home Assistant instance is not available")

            wiped_entry_data = {
                CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
                CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                CONF_DEVICE_TYPE: pending[CONF_DEVICE_TYPE],
            }
            self.hass.config_entries.async_update_entry(
                entry,
                data=wiped_entry_data,
                title=f"SecureMTR {pending[CONF_SERIAL_NUMBER]}",
                unique_id=_local_unique_id(pending[CONF_MAC_ADDRESS]),
            )

            try:
                credentials = await self._async_commission_local_ble(
                    serial_number=pending[CONF_SERIAL_NUMBER],
                    mac_address=pending[CONF_MAC_ADDRESS],
                    device_type=pending[CONF_DEVICE_TYPE],
                )
            except LocalBleCommissioningError as error:
                _LOGGER.error(
                    "Local BLE recommissioning failed for %s: %s",
                    pending[CONF_SERIAL_NUMBER],
                    error,
                )
                return self.async_show_form(
                    step_id="reconfigure_local_ble_commissioning",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_START_COMMISSIONING,
                                default=False,
                            ): bool
                        }
                    ),
                    errors={"base": "commissioning_failed"},
                    description_placeholders={
                        CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                        CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    },
                )

            ble_key = credentials.get(CONF_LOCAL_BLE_KEY)
            owner_token = credentials.get(CONF_LOCAL_OWNER_TOKEN)
            if not isinstance(ble_key, str) or not ble_key:
                return self.async_show_form(
                    step_id="reconfigure_local_ble_commissioning",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_START_COMMISSIONING,
                                default=False,
                            ): bool
                        }
                    ),
                    errors={"base": "commissioning_invalid_result"},
                    description_placeholders={
                        CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                        CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    },
                )

            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
                    CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                    CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    CONF_DEVICE_TYPE: pending[CONF_DEVICE_TYPE],
                    CONF_LOCAL_BLE_KEY: ble_key,
                    CONF_LOCAL_OWNER_TOKEN: owner_token,
                },
                title=f"SecureMTR {pending[CONF_SERIAL_NUMBER]}",
                unique_id=_local_unique_id(pending[CONF_MAC_ADDRESS]),
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            self._pending_reconfigure_local_ble = None
            return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure_local_ble_commissioning",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_START_COMMISSIONING,
                        default=False,
                    ): bool
                }
            ),
            description_placeholders={
                CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
            },
        )

    async def _async_handle_cloud_credentials(
        self, user_input: dict[str, Any]
    ) -> FlowResult:
        """Validate cloud credentials and create a cloud config entry."""

        email = user_input[CONF_EMAIL].strip()
        password = user_input[CONF_PASSWORD]

        if not email:
            _LOGGER.error("Secure Controls email is required")
            return self.async_show_form(
                step_id="cloud",
                data_schema=STEP_CLOUD_DATA_SCHEMA,
                errors={CONF_EMAIL: "invalid_email"},
            )

        if not password:
            _LOGGER.error("Secure Controls password is required")
            return self.async_show_form(
                step_id="cloud",
                data_schema=STEP_CLOUD_DATA_SCHEMA,
                errors={CONF_PASSWORD: "password_required"},
            )

        if len(password) > 12:
            _LOGGER.error(
                "Secure Controls password exceeds 12 character mobile app limit"
            )
            return self.async_show_form(
                step_id="cloud",
                data_schema=STEP_CLOUD_DATA_SCHEMA,
                errors={CONF_PASSWORD: "password_too_long"},
            )

        normalized_email = email.lower()

        await self.async_set_unique_id(normalized_email)
        self._abort_if_unique_id_configured()

        hashed_password = hashlib.md5(password.encode("utf-8")).hexdigest()

        _LOGGER.info("Secure Controls app credentials accepted")
        return self.async_create_entry(
            title="SecureMTR",
            data={
                CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD,
                CONF_EMAIL: email,
                CONF_PASSWORD: hashed_password,
            },
        )

    async def async_step_local_ble(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle local BLE onboarding details."""

        errors: dict[str, str] = {}

        if user_input is not None:
            serial_number = user_input[CONF_SERIAL_NUMBER].strip()
            mac_address = _normalize_mac(user_input[CONF_MAC_ADDRESS])
            device_type = user_input[CONF_DEVICE_TYPE].strip().lower()

            if len(serial_number) != 8:
                errors[CONF_SERIAL_NUMBER] = "invalid_serial"

            if mac_address is None:
                errors[CONF_MAC_ADDRESS] = "invalid_mac"

            if device_type != DEVICE_TYPE_E7_PLUS:
                errors[CONF_DEVICE_TYPE] = "invalid_device_type"

            if not errors and mac_address is not None:
                await self.async_set_unique_id(_local_unique_id(mac_address))
                self._abort_if_unique_id_configured()
                self._pending_local_ble = {
                    CONF_SERIAL_NUMBER: serial_number,
                    CONF_MAC_ADDRESS: mac_address,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
                }
                return await self.async_step_local_ble_commissioning()

        _LOGGER.info("Displaying SecureMTR local BLE onboarding form")
        return self.async_show_form(
            step_id="local_ble",
            data_schema=STEP_LOCAL_BLE_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_local_ble_commissioning(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Run local BLE commissioning before creating the entry."""

        pending = self._pending_local_ble
        if pending is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            if user_input.get(CONF_START_COMMISSIONING) is not True:
                return self.async_show_form(
                    step_id="local_ble_commissioning",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_START_COMMISSIONING,
                                default=False,
                            ): bool
                        }
                    ),
                    errors={"base": "commissioning_not_started"},
                    description_placeholders={
                        CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                        CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    },
                )

            try:
                credentials = await self._async_commission_local_ble(
                    serial_number=pending[CONF_SERIAL_NUMBER],
                    mac_address=pending[CONF_MAC_ADDRESS],
                    device_type=pending[CONF_DEVICE_TYPE],
                )
            except LocalBleCommissioningError as error:
                _LOGGER.error(
                    "Local BLE commissioning failed for %s: %s",
                    pending[CONF_SERIAL_NUMBER],
                    error,
                )
                return self.async_show_form(
                    step_id="local_ble_commissioning",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_START_COMMISSIONING,
                                default=False,
                            ): bool
                        }
                    ),
                    errors={"base": "commissioning_failed"},
                    description_placeholders={
                        CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                        CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    },
                )

            ble_key = credentials.get(CONF_LOCAL_BLE_KEY)
            owner_token = credentials.get(CONF_LOCAL_OWNER_TOKEN)
            if not isinstance(ble_key, str) or not ble_key:
                return self.async_show_form(
                    step_id="local_ble_commissioning",
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_START_COMMISSIONING,
                                default=False,
                            ): bool
                        }
                    ),
                    errors={"base": "commissioning_invalid_result"},
                    description_placeholders={
                        CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                        CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    },
                )

            self._pending_local_ble = None
            return self.async_create_entry(
                title=f"SecureMTR {pending[CONF_SERIAL_NUMBER]}",
                data={
                    CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
                    CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                    CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
                    CONF_DEVICE_TYPE: pending[CONF_DEVICE_TYPE],
                    CONF_LOCAL_BLE_KEY: ble_key,
                    CONF_LOCAL_OWNER_TOKEN: owner_token,
                },
            )

        return self.async_show_form(
            step_id="local_ble_commissioning",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_START_COMMISSIONING,
                        default=False,
                    ): bool
                }
            ),
            description_placeholders={
                CONF_SERIAL_NUMBER: pending[CONF_SERIAL_NUMBER],
                CONF_MAC_ADDRESS: pending[CONF_MAC_ADDRESS],
            },
        )

    async def _async_commission_local_ble(
        self,
        *,
        serial_number: str,
        mac_address: str,
        device_type: str,
    ) -> dict[str, str | None]:
        """Run local BLE commissioning and return persisted credential values."""

        if self.hass is None:
            raise RuntimeError("Home Assistant instance is not available")

        return await async_commission_local_ble(
            self.hass,
            serial_number=serial_number,
            mac_address=mac_address,
            device_type=device_type,
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
