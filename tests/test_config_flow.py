"""Tests for the securemtr integration config flow and setup."""

from __future__ import annotations

import asyncio
import shutil
import hashlib
import logging
from contextlib import suppress
from datetime import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
import tempfile
import sys
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.data_entry_flow import FlowResultType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrRuntimeData,
    _RESET_SERVICE_FLAG,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.securemtr.beanbag import BeanbagGateway, BeanbagSession
import custom_components.securemtr.config_flow as config_flow
from custom_components.securemtr.config_flow import (
    CONF_CONNECTION_MODE,
    CONF_BOOST_ANCHOR,
    CONF_DEVICE_TYPE,
    CONF_ELEMENT_POWER_KW,
    CONF_LOCAL_BLE_KEY,
    CONF_LOCAL_OWNER_TOKEN,
    CONF_MAC_ADDRESS,
    CONF_PREFER_DEVICE_ENERGY,
    CONF_PRIMARY_ANCHOR,
    CONF_SERIAL_NUMBER,
    CONF_START_COMMISSIONING,
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_LOCAL_BLE,
    DEFAULT_TIMEZONE,
    DEFAULT_BOOST_ANCHOR,
    DEVICE_TYPE_E7_PLUS,
    DEFAULT_ELEMENT_POWER_KW,
    DEFAULT_PREFER_DEVICE_ENERGY,
    DEFAULT_PRIMARY_ANCHOR,
    SecuremtrConfigFlow,
    _anchor_option_to_time,
    _local_unique_id,
    _normalize_mac,
    _serialize_anchor,
)
from custom_components.securemtr.local_ble_commissioning import (
    LocalBleCommissioningError,
)


class ConfigFlowServiceRegistry:
    """Provide minimal service registration for config flow tests."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], tuple[callable, Any | None]] = {}

    def async_register(
        self,
        domain: str,
        service: str,
        handler: Callable[[SimpleNamespace], Any]
        | Callable[[SimpleNamespace], Awaitable[Any]],
        schema: Any | None = None,
    ) -> None:
        """Record registered services."""

        self._handlers[(domain, service)] = (handler, schema)

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> None:
        """Invoke a registered service handler if present."""

        handler, schema = self._handlers[(domain, service)]
        payload = data or {}
        if schema is not None:
            payload = schema(payload)
        result = handler(SimpleNamespace(data=payload))
        if asyncio.iscoroutine(result):
            await result


class ConfigFlowConfigEntries:
    """Provide minimal config entry helper behavior."""

    def __init__(self) -> None:
        self._entries: list[config_entries.ConfigEntry] = []

    def async_entries(self, domain: str) -> list[config_entries.ConfigEntry]:
        """Return stored entries for the requested domain."""

        return [entry for entry in self._entries if entry.domain == domain]

    async def async_add(
        self, entry: config_entries.ConfigEntry
    ) -> config_entries.ConfigEntry:
        """Store a created config entry."""

        self._entries.append(entry)
        return entry

    async def async_forward_entry_setups(
        self, entry: config_entries.ConfigEntry, platforms: list[str]
    ) -> None:
        """No-op platform forwarder for tests."""

    async def async_unload_platforms(
        self, entry: config_entries.ConfigEntry, platforms: list[str]
    ) -> bool:
        """Simulate successful platform unload."""

        return True


class ConfigFlowHass:
    """Emulate the subset of Home Assistant APIs required for config flow tests."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.services = ConfigFlowServiceRegistry()
        self.config_entries = ConfigFlowConfigEntries()
        self._config_dir = Path(tempfile.mkdtemp(prefix="securemtr-config-"))
        self.config = SimpleNamespace(
            time_zone="Europe/London", config_dir=str(self._config_dir)
        )
        self._tasks: list[asyncio.Task[Any]] = []
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.get_event_loop()

    async def async_start(self) -> None:
        """Start the test Home Assistant instance."""

    async def async_stop(self) -> None:
        """Stop the test Home Assistant instance, cancelling pending tasks."""

        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        shutil.rmtree(self._config_dir, ignore_errors=True)

    def async_create_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """Schedule a coroutine on the running loop."""

        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def async_block_till_done(self) -> None:
        """Await all tracked tasks to complete."""

        if not self._tasks:
            return
        await asyncio.gather(*self._tasks)


@pytest_asyncio.fixture
async def hass_fixture() -> ConfigFlowHass:
    """Provide a minimal Home Assistant stand-in for config flow tests."""

    hass = ConfigFlowHass()
    await hass.async_start()
    try:
        yield hass
    finally:
        await hass.async_stop()


class DummyWebSocket:
    """Provide a closable WebSocket stand-in."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        """Mark the WebSocket as closed."""

        self.closed = True


class DummyBackend:
    """Return canned session data for backend startup."""

    def __init__(self) -> None:
        self.login_calls: list[tuple[str, str]] = []
        self.session = BeanbagSession(
            user_id=42,
            session_id="session-id",
            token="jwt-token",
            token_timestamp=None,
            gateways=(
                BeanbagGateway(
                    gateway_id="gateway-1",
                    serial_number="serial-1",
                    host_name="host-name",
                    capabilities={},
                ),
            ),
        )
        self.websocket = DummyWebSocket()
        self.metadata_calls: list[str] = []

    async def login_and_connect(
        self, email: str, password_digest: str
    ) -> tuple[BeanbagSession, DummyWebSocket]:
        """Record credentials and return canned session details."""

        self.login_calls.append((email, password_digest))
        return self.session, self.websocket

    async def read_device_metadata(
        self, session: BeanbagSession, websocket: DummyWebSocket, gateway_id: str
    ) -> dict[str, str]:
        """Return canned metadata for the configured controller."""

        self.metadata_calls.append(gateway_id)
        return {
            "BOI": "controller-1",
            "N": "Test Controller",
            "SN": "serial-1",
            "FV": "1.0.0",
            "MD": "E7+",
        }


@pytest.fixture
def backend_patch(monkeypatch: pytest.MonkeyPatch) -> DummyBackend:
    """Stub Beanbag backend construction during tests."""

    fake_session = object()
    backend = DummyBackend()

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    return backend


@pytest.mark.asyncio
async def test_async_setup_initializes_domain_storage(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure async_setup prepares storage for the integration."""
    assert await async_setup(hass_fixture, {})
    assert hass_fixture.data[DOMAIN] == {_RESET_SERVICE_FLAG: True}


@pytest.mark.asyncio
async def test_async_setup_entry_stores_entry_data(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure async_setup_entry keeps the provided credential data."""
    hashed_password = hashlib.md5("secure".encode("utf-8")).hexdigest()
    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: hashed_password},
    )

    assert await async_setup_entry(hass_fixture, entry)
    await hass_fixture.async_block_till_done()

    runtime = hass_fixture.data[DOMAIN][entry.entry_id]
    assert isinstance(runtime, SecuremtrRuntimeData)
    assert runtime.session is backend_patch.session
    assert runtime.websocket is backend_patch.websocket
    assert backend_patch.login_calls == [("user@example.com", hashed_password)]


@pytest.mark.asyncio
async def test_async_setup_entry_local_ble_skips_cloud_login(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure local BLE entries initialise runtime without cloud startup."""

    entry = SimpleNamespace(
        entry_id="entry-local",
        unique_id="local_ble:AABBCCDDEEFF",
        data={
            CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
            CONF_SERIAL_NUMBER: "A1B2C3D4",
            CONF_MAC_ADDRESS: "AABBCCDDEEFF",
            CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
        },
    )

    assert await async_setup_entry(hass_fixture, entry)

    runtime = hass_fixture.data[DOMAIN][entry.entry_id]
    assert isinstance(runtime, SecuremtrRuntimeData)
    assert runtime.connection_mode == CONNECTION_MODE_LOCAL_BLE
    assert runtime.controller is not None
    assert runtime.controller.serial_number == "A1B2C3D4"
    assert runtime.controller.gateway_id == "AABBCCDDEEFF"
    assert runtime.controller_ready.is_set()
    assert backend_patch.login_calls == []


@pytest.mark.asyncio
async def test_async_unload_entry_removes_entry_data(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure async_unload_entry clears stored data."""
    entry = SimpleNamespace(
        entry_id="entry-2",
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "digest"},
    )

    assert await async_setup_entry(hass_fixture, entry)
    await hass_fixture.async_block_till_done()

    assert await async_unload_entry(hass_fixture, entry)
    assert "entry-2" not in hass_fixture.data[DOMAIN]
    assert backend_patch.websocket.closed


@pytest.mark.asyncio
async def test_config_flow_shows_form(hass_fixture: ConfigFlowHass) -> None:
    """Verify the config flow displays the initial form."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    result = await flow.async_step_user()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_config_flow_creates_entry(hass_fixture: ConfigFlowHass) -> None:
    """Verify a config entry is created with sanitized credentials."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: " User@Example.com ", CONF_PASSWORD: "secret"}
    )

    expected_hash = hashlib.md5("secret".encode("utf-8")).hexdigest()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "SecureMTR"
    assert result["data"] == {
        CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD,
        CONF_EMAIL: "User@Example.com",
        CONF_PASSWORD: expected_hash,
    }
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")
    flow._abort_if_unique_id_configured.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_rejects_empty_email(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure config flow rejects empty email addresses."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user({CONF_EMAIL: "   ", CONF_PASSWORD: "secret"})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_EMAIL: "invalid_email"}
    flow.async_set_unique_id.assert_not_called()
    flow._abort_if_unique_id_configured.assert_not_called()


@pytest.mark.asyncio
async def test_config_flow_rejects_blank_password(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure config flow rejects blank passwords."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "user@example.com", CONF_PASSWORD: ""}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_PASSWORD: "password_required"}
    flow.async_set_unique_id.assert_not_called()
    flow._abort_if_unique_id_configured.assert_not_called()


@pytest.mark.asyncio
async def test_config_flow_rejects_long_password(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure config flow rejects passwords longer than the mobile app allows."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "x" * 13}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_PASSWORD: "password_too_long"}
    flow.async_set_unique_id.assert_not_called()
    flow._abort_if_unique_id_configured.assert_not_called()


@pytest.mark.asyncio
async def test_config_flow_mode_selection_routes_to_cloud_step(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure selecting cloud mode advances to the cloud credential form."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    result = await flow.async_step_user({CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "cloud"


@pytest.mark.asyncio
async def test_config_flow_mode_selection_routes_to_local_ble_step(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure selecting local BLE mode advances to the local details form."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    result = await flow.async_step_user(
        {CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local_ble"


@pytest.mark.asyncio
async def test_reconfigure_mode_selection_routes_to_cloud_step(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure reconfigure mode selection can route to cloud credential step."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow._get_reconfigure_entry = Mock(
        return_value=SimpleNamespace(
            entry_id="entry-1",
            unique_id="local_ble:AABBCCDDEEFF",
            data={CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE},
        )
    )

    result = await flow.async_step_reconfigure(
        {CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_cloud"


@pytest.mark.asyncio
async def test_reconfigure_cloud_updates_entry_data(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure cloud reconfigure writes updated data and reloads the entry."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="local_ble:AABBCCDDEEFF",
        data={CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE},
    )
    flow._get_reconfigure_entry = Mock(return_value=entry)
    flow._async_current_entries = Mock(return_value=[entry])

    hass_fixture.config_entries.async_update_entry = Mock()
    hass_fixture.config_entries.async_reload = AsyncMock()

    result = await flow.async_step_reconfigure_cloud(
        {
            CONF_EMAIL: " User@Example.com ",
            CONF_PASSWORD: "secret",
        }
    )

    expected_hash = hashlib.md5("secret".encode("utf-8")).hexdigest()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    hass_fixture.config_entries.async_update_entry.assert_called_once_with(
        entry,
        data={
            CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD,
            CONF_EMAIL: "User@Example.com",
            CONF_PASSWORD: expected_hash,
        },
        title="SecureMTR",
        unique_id="user@example.com",
    )
    hass_fixture.config_entries.async_reload.assert_awaited_once_with("entry-1")


@pytest.mark.asyncio
async def test_reconfigure_local_ble_updates_entry_data(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure local BLE reconfigure writes updated data and reloads entry."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="user@example.com",
        data={CONF_CONNECTION_MODE: CONNECTION_MODE_CLOUD},
    )
    flow._get_reconfigure_entry = Mock(return_value=entry)
    flow._async_current_entries = Mock(return_value=[entry])

    hass_fixture.config_entries.async_update_entry = Mock()
    hass_fixture.config_entries.async_reload = AsyncMock()
    flow._async_commission_local_ble = AsyncMock(
        return_value={
            CONF_LOCAL_BLE_KEY: "00112233445566778899AABBCCDDEEFF",
            CONF_LOCAL_OWNER_TOKEN: "owner-token",
        }
    )

    result = await flow.async_step_reconfigure_local_ble(
        {
            CONF_SERIAL_NUMBER: "A1B2C3D4",
            CONF_MAC_ADDRESS: "aa:bb:cc:dd:ee:ff",
            CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_local_ble_commissioning"

    result = await flow.async_step_reconfigure_local_ble_commissioning(
        {CONF_START_COMMISSIONING: True}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert hass_fixture.config_entries.async_update_entry.call_count == 2
    first_call = hass_fixture.config_entries.async_update_entry.call_args_list[0]
    assert first_call.args == (entry,)
    assert first_call.kwargs == {
        "data": {
            CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
            CONF_SERIAL_NUMBER: "A1B2C3D4",
            CONF_MAC_ADDRESS: "AABBCCDDEEFF",
            CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
        },
        "title": "SecureMTR A1B2C3D4",
        "unique_id": "local_ble:AABBCCDDEEFF",
    }

    second_call = hass_fixture.config_entries.async_update_entry.call_args_list[1]
    assert second_call.args == (entry,)
    assert second_call.kwargs == {
        "data": {
            CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
            CONF_SERIAL_NUMBER: "A1B2C3D4",
            CONF_MAC_ADDRESS: "AABBCCDDEEFF",
            CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
            CONF_LOCAL_BLE_KEY: "00112233445566778899AABBCCDDEEFF",
            CONF_LOCAL_OWNER_TOKEN: "owner-token",
        },
        "title": "SecureMTR A1B2C3D4",
        "unique_id": "local_ble:AABBCCDDEEFF",
    }

    flow._async_commission_local_ble.assert_awaited_once_with(
        serial_number="A1B2C3D4",
        mac_address="AABBCCDDEEFF",
        device_type=DEVICE_TYPE_E7_PLUS,
    )
    hass_fixture.config_entries.async_reload.assert_awaited_once_with("entry-1")


@pytest.mark.asyncio
async def test_config_flow_local_ble_creates_entry(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Verify local BLE entries normalize values and include mode metadata."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()
    flow._async_commission_local_ble = AsyncMock(
        return_value={
            CONF_LOCAL_BLE_KEY: "00112233445566778899AABBCCDDEEFF",
            CONF_LOCAL_OWNER_TOKEN: "owner-token",
        }
    )

    result = await flow.async_step_local_ble(
        {
            CONF_SERIAL_NUMBER: "A1B2C3D4",
            CONF_MAC_ADDRESS: "aa:bb:cc:dd:ee:ff",
            CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local_ble_commissioning"

    result = await flow.async_step_local_ble_commissioning(
        {CONF_START_COMMISSIONING: True}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_CONNECTION_MODE: CONNECTION_MODE_LOCAL_BLE,
        CONF_SERIAL_NUMBER: "A1B2C3D4",
        CONF_MAC_ADDRESS: "AABBCCDDEEFF",
        CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
        CONF_LOCAL_BLE_KEY: "00112233445566778899AABBCCDDEEFF",
        CONF_LOCAL_OWNER_TOKEN: "owner-token",
    }
    flow.async_set_unique_id.assert_awaited_once_with(_local_unique_id("AABBCCDDEEFF"))
    flow._abort_if_unique_id_configured.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_local_ble_requires_commissioning_checkbox(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure local BLE commissioning does not start until checkbox is set."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow._pending_local_ble = {
        CONF_SERIAL_NUMBER: "A1B2C3D4",
        CONF_MAC_ADDRESS: "AABBCCDDEEFF",
        CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
    }
    flow._async_commission_local_ble = AsyncMock()

    result = await flow.async_step_local_ble_commissioning(
        {CONF_START_COMMISSIONING: False}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local_ble_commissioning"
    assert result["errors"] == {"base": "commissioning_not_started"}
    flow._async_commission_local_ble.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_flow_local_ble_handles_commissioning_failure(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure local BLE commissioning failures keep the flow on-screen."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow._pending_local_ble = {
        CONF_SERIAL_NUMBER: "A1B2C3D4",
        CONF_MAC_ADDRESS: "AABBCCDDEEFF",
        CONF_DEVICE_TYPE: DEVICE_TYPE_E7_PLUS,
    }
    flow._async_commission_local_ble = AsyncMock(
        side_effect=LocalBleCommissioningError("boom")
    )

    result = await flow.async_step_local_ble_commissioning(
        {CONF_START_COMMISSIONING: True}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "local_ble_commissioning"
    assert result["errors"] == {"base": "commissioning_failed"}


@pytest.mark.asyncio
async def test_config_flow_local_ble_rejects_invalid_manual_fields(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure local BLE manual details are validated before entry creation."""

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_local_ble(
        {
            CONF_SERIAL_NUMBER: "123",
            CONF_MAC_ADDRESS: "aa:bb:cc",
            CONF_DEVICE_TYPE: "unknown",
        }
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {
        CONF_SERIAL_NUMBER: "invalid_serial",
        CONF_MAC_ADDRESS: "invalid_mac",
        CONF_DEVICE_TYPE: "invalid_device_type",
    }
    flow.async_set_unique_id.assert_not_called()
    flow._abort_if_unique_id_configured.assert_not_called()


@pytest.mark.asyncio
async def test_options_flow_uses_default_values() -> None:
    """Ensure the options flow exposes documented defaults."""

    handler = SecuremtrConfigFlow.async_get_options_flow(SimpleNamespace(options={}))
    handler.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/London"))

    result = await handler.async_step_init()
    assert result["type"] == FlowResultType.FORM
    defaults = result["data_schema"]({})

    assert defaults[CONF_PRIMARY_ANCHOR] == time.fromisoformat(DEFAULT_PRIMARY_ANCHOR)
    assert defaults[CONF_BOOST_ANCHOR] == time.fromisoformat(DEFAULT_BOOST_ANCHOR)
    assert defaults[CONF_ELEMENT_POWER_KW] == DEFAULT_ELEMENT_POWER_KW
    assert defaults[CONF_PREFER_DEVICE_ENERGY] == DEFAULT_PREFER_DEVICE_ENERGY


@pytest.mark.asyncio
async def test_options_flow_prefers_stored_values() -> None:
    """Ensure stored options are respected as defaults."""

    handler = SecuremtrConfigFlow.async_get_options_flow(
        SimpleNamespace(
            options={
                CONF_PRIMARY_ANCHOR: "06:15",
                CONF_BOOST_ANCHOR: "18:45:30",
                CONF_ELEMENT_POWER_KW: "3.1",
                CONF_PREFER_DEVICE_ENERGY: False,
            }
        )
    )
    handler.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/London"))

    result = await handler.async_step_init()
    defaults = result["data_schema"]({})

    assert defaults[CONF_PRIMARY_ANCHOR] == time(6, 15)
    assert defaults[CONF_BOOST_ANCHOR] == time(18, 45, 30)
    assert defaults[CONF_ELEMENT_POWER_KW] == pytest.approx(3.1)
    assert defaults[CONF_PREFER_DEVICE_ENERGY] is False


@pytest.mark.asyncio
async def test_options_flow_creates_entry_with_serialized_times() -> None:
    """Ensure anchor times are serialized to ISO strings when saved."""

    handler = SecuremtrConfigFlow.async_get_options_flow(SimpleNamespace(options={}))
    handler.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Paris"))

    result = await handler.async_step_init(
        {
            CONF_PRIMARY_ANCHOR: time(4, 30),
            CONF_BOOST_ANCHOR: time(19, 0, 15),
            CONF_ELEMENT_POWER_KW: 3.25,
            CONF_PREFER_DEVICE_ENERGY: False,
        }
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_TIME_ZONE: "Europe/Paris",
        CONF_PRIMARY_ANCHOR: "04:30",
        CONF_BOOST_ANCHOR: "19:00:15",
        CONF_ELEMENT_POWER_KW: 3.25,
        CONF_PREFER_DEVICE_ENERGY: False,
    }


@pytest.mark.asyncio
async def test_options_flow_falls_back_to_default_timezone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ensure the options flow falls back when Home Assistant lacks a timezone."""

    handler = SecuremtrConfigFlow.async_get_options_flow(SimpleNamespace(options={}))
    handler.hass = SimpleNamespace(config=SimpleNamespace(time_zone=None))

    with caplog.at_level(logging.WARNING):
        result = await handler.async_step_init(
            {
                CONF_PRIMARY_ANCHOR: time(4, 30),
                CONF_BOOST_ANCHOR: time(19, 0, 15),
                CONF_ELEMENT_POWER_KW: 3.25,
                CONF_PREFER_DEVICE_ENERGY: False,
            }
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TIME_ZONE] == DEFAULT_TIMEZONE
    assert "timezone unavailable" in caplog.text


@pytest.mark.asyncio
async def test_options_flow_handles_invalid_home_assistant_timezone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ensure the options flow logs and falls back when the Home Assistant timezone is invalid."""

    handler = SecuremtrConfigFlow.async_get_options_flow(SimpleNamespace(options={}))
    handler.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Mars/Olympus"))

    with caplog.at_level(logging.WARNING):
        result = await handler.async_step_init(
            {
                CONF_PRIMARY_ANCHOR: time(4, 30),
                CONF_BOOST_ANCHOR: time(19, 0, 15),
                CONF_ELEMENT_POWER_KW: 3.25,
                CONF_PREFER_DEVICE_ENERGY: False,
            }
        )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TIME_ZONE] == DEFAULT_TIMEZONE
    assert "Invalid Home Assistant timezone" in caplog.text


def test_anchor_option_to_time_variants(caplog: pytest.LogCaptureFixture) -> None:
    """Exercise conversions for stored anchor values."""

    fallback = time(5, 0)
    direct = time(6, 30)

    assert _anchor_option_to_time(direct, fallback) is direct
    assert _anchor_option_to_time("07:45", fallback) == time(7, 45)

    caplog.set_level(logging.DEBUG)
    assert _anchor_option_to_time("invalid", fallback) is fallback
    assert any("Invalid anchor string" in record.message for record in caplog.records)


def test_normalize_mac_accepts_compact_and_colon_formats() -> None:
    """Ensure MAC normalization accepts compact and colon-separated input."""

    assert _normalize_mac("AABBCCDDEEFF") == "AABBCCDDEEFF"
    assert _normalize_mac("aa:bb:cc:dd:ee:ff") == "AABBCCDDEEFF"


def test_normalize_mac_rejects_invalid_values() -> None:
    """Ensure MAC normalization rejects malformed addresses."""

    assert _normalize_mac("AABBCC") is None
    assert _normalize_mac("AABBCCDDEEFG") is None


def test_serialize_anchor_precision() -> None:
    """Ensure anchor serialization preserves precision tiers."""

    assert _serialize_anchor(time(4, 30)) == "04:30"
    assert _serialize_anchor(time(4, 30, 5)) == "04:30:05"
    assert _serialize_anchor(time(4, 30, 5, 120000)) == "04:30:05.120000"
