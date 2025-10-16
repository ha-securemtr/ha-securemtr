"""Tests for the SecureMTR integration config flow and setup."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from pytest import TempPathFactory
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr import (
    DOMAIN,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.securemtr.config_flow import SecuremtrConfigFlow


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


@pytest.mark.asyncio
async def test_async_setup_initializes_domain_storage(
    hass_fixture: HomeAssistant,
) -> None:
    """Ensure async_setup prepares storage for the integration."""
    assert await async_setup(hass_fixture, {})
    assert hass_fixture.data[DOMAIN] == {}


@pytest.mark.asyncio
async def test_async_setup_entry_stores_entry_data(hass_fixture: HomeAssistant) -> None:
    """Ensure async_setup_entry keeps the provided credential data."""
    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secure"},
    )

    assert await async_setup_entry(hass_fixture, entry)
    assert hass_fixture.data[DOMAIN][entry.entry_id]["data"] == entry.data


@pytest.mark.asyncio
async def test_async_unload_entry_removes_entry_data(
    hass_fixture: HomeAssistant,
) -> None:
    """Ensure async_unload_entry clears stored data."""
    hass_fixture.data.setdefault(DOMAIN, {})["entry-2"] = {"data": {}}
    entry = SimpleNamespace(entry_id="entry-2", unique_id="user@example.com", data={})

    assert await async_unload_entry(hass_fixture, entry)
    assert "entry-2" not in hass_fixture.data[DOMAIN]


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

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "User@Example.com"
    assert result["data"] == {
        CONF_EMAIL: "User@Example.com",
        CONF_PASSWORD: "secret",
    }
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")
    flow._abort_if_unique_id_configured.assert_called_once()
