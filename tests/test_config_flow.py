"""Tests for the securemtr integration config flow and setup."""

from __future__ import annotations

from datetime import time
import logging
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from pytest import TempPathFactory
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrRuntimeData,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.securemtr.beanbag import BeanbagGateway, BeanbagSession
from custom_components.securemtr.config_flow import (
    CONF_ANCHOR_STRATEGY,
    CONF_BOOST_ANCHOR,
    CONF_ELEMENT_POWER_KW,
    CONF_PREFER_DEVICE_ENERGY,
    CONF_PRIMARY_ANCHOR,
    DEFAULT_ANCHOR_STRATEGY,
    DEFAULT_BOOST_ANCHOR,
    DEFAULT_ELEMENT_POWER_KW,
    DEFAULT_PREFER_DEVICE_ENERGY,
    DEFAULT_PRIMARY_ANCHOR,
    DEFAULT_TIMEZONE,
    SecuremtrConfigFlow,
    _anchor_option_to_time,
    _serialize_anchor,
)


@pytest_asyncio.fixture
async def hass_fixture(tmp_path_factory: TempPathFactory) -> HomeAssistant:
    """Provide a Home Assistant instance for tests."""
    config_dir: Path = tmp_path_factory.mktemp("securemtr")
    hass = HomeAssistant(config_dir=str(config_dir))
    hass.data.clear()
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
    hass_fixture: HomeAssistant,
) -> None:
    """Ensure async_setup prepares storage for the integration."""
    assert await async_setup(hass_fixture, {})
    assert hass_fixture.data[DOMAIN] == {}


@pytest.mark.asyncio
async def test_async_setup_entry_stores_entry_data(
    hass_fixture: HomeAssistant, backend_patch: DummyBackend
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
async def test_async_unload_entry_removes_entry_data(
    hass_fixture: HomeAssistant, backend_patch: DummyBackend
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
async def test_config_flow_shows_form(hass_fixture: HomeAssistant) -> None:
    """Verify the config flow displays the initial form."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    result = await flow.async_step_user()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_config_flow_creates_entry(hass_fixture: HomeAssistant) -> None:
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
        CONF_EMAIL: "User@Example.com",
        CONF_PASSWORD: expected_hash,
    }
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")
    flow._abort_if_unique_id_configured.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_rejects_long_password(
    hass_fixture: HomeAssistant,
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
async def test_options_flow_uses_default_values() -> None:
    """Ensure the options flow exposes documented defaults."""

    handler = SecuremtrConfigFlow.async_get_options_flow(SimpleNamespace(options={}))

    result = await handler.async_step_init()
    assert result["type"] == FlowResultType.FORM
    defaults = result["data_schema"]({})

    assert defaults[CONF_TIME_ZONE] == DEFAULT_TIMEZONE
    assert defaults[CONF_PRIMARY_ANCHOR] == time.fromisoformat(DEFAULT_PRIMARY_ANCHOR)
    assert defaults[CONF_BOOST_ANCHOR] == time.fromisoformat(DEFAULT_BOOST_ANCHOR)
    assert defaults[CONF_ANCHOR_STRATEGY] == DEFAULT_ANCHOR_STRATEGY
    assert defaults[CONF_ELEMENT_POWER_KW] == DEFAULT_ELEMENT_POWER_KW
    assert defaults[CONF_PREFER_DEVICE_ENERGY] == DEFAULT_PREFER_DEVICE_ENERGY


@pytest.mark.asyncio
async def test_options_flow_prefers_stored_values() -> None:
    """Ensure stored options are respected as defaults."""

    handler = SecuremtrConfigFlow.async_get_options_flow(
        SimpleNamespace(
            options={
                CONF_TIME_ZONE: "America/New_York",
                CONF_PRIMARY_ANCHOR: "06:15",
                CONF_BOOST_ANCHOR: "18:45:30",
                CONF_ANCHOR_STRATEGY: "strange",
                CONF_ELEMENT_POWER_KW: "3.1",
                CONF_PREFER_DEVICE_ENERGY: False,
            }
        )
    )

    result = await handler.async_step_init()
    defaults = result["data_schema"]({})

    assert defaults[CONF_TIME_ZONE] == "America/New_York"
    assert defaults[CONF_PRIMARY_ANCHOR] == time(6, 15)
    assert defaults[CONF_BOOST_ANCHOR] == time(18, 45, 30)
    assert defaults[CONF_ANCHOR_STRATEGY] == DEFAULT_ANCHOR_STRATEGY
    assert defaults[CONF_ELEMENT_POWER_KW] == pytest.approx(3.1)
    assert defaults[CONF_PREFER_DEVICE_ENERGY] is False


@pytest.mark.asyncio
async def test_options_flow_creates_entry_with_serialized_times() -> None:
    """Ensure anchor times are serialized to ISO strings when saved."""

    handler = SecuremtrConfigFlow.async_get_options_flow(SimpleNamespace(options={}))

    result = await handler.async_step_init(
        {
            CONF_TIME_ZONE: "Europe/Paris",
            CONF_PRIMARY_ANCHOR: time(4, 30),
            CONF_BOOST_ANCHOR: time(19, 0, 15),
            CONF_ANCHOR_STRATEGY: "fixed",
            CONF_ELEMENT_POWER_KW: 3.25,
            CONF_PREFER_DEVICE_ENERGY: False,
        }
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_TIME_ZONE: "Europe/Paris",
        CONF_PRIMARY_ANCHOR: "04:30",
        CONF_BOOST_ANCHOR: "19:00:15",
        CONF_ANCHOR_STRATEGY: "fixed",
        CONF_ELEMENT_POWER_KW: 3.25,
        CONF_PREFER_DEVICE_ENERGY: False,
    }


def test_anchor_option_to_time_variants(caplog: pytest.LogCaptureFixture) -> None:
    """Exercise conversions for stored anchor values."""

    fallback = time(5, 0)
    direct = time(6, 30)

    assert _anchor_option_to_time(direct, fallback) is direct
    assert _anchor_option_to_time("07:45", fallback) == time(7, 45)

    caplog.set_level(logging.DEBUG)
    assert _anchor_option_to_time("invalid", fallback) is fallback
    assert any("Invalid anchor string" in record.message for record in caplog.records)


def test_serialize_anchor_precision() -> None:
    """Ensure anchor serialization preserves precision tiers."""

    assert _serialize_anchor(time(4, 30)) == "04:30"
    assert _serialize_anchor(time(4, 30, 5)) == "04:30:05"
    assert (
        _serialize_anchor(time(4, 30, 5, 120000))
        == "04:30:05.120000"
    )
