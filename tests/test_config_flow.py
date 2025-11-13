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
from typing import Any, Awaitable, Callable, Mapping
import tempfile
import sys
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_TIME_ZONE
from homeassistant.core import CoreState
from homeassistant.data_entry_flow import FlowResultType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr import (
    CONF_GATEWAY_ID,
    DOMAIN,
    SecuremtrRuntimeData,
    _RESET_SERVICE_FLAG,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.securemtr.beanbag import (
    BeanbagError,
    BeanbagEnergySample,
    BeanbagGateway,
    BeanbagSession,
    BeanbagStateSnapshot,
    DailyProgram,
)
import custom_components.securemtr.config_flow as config_flow
from custom_components.securemtr.config_flow import (
    CONF_BOOST_ANCHOR,
    CONF_ELEMENT_POWER_KW,
    CONF_PREFER_DEVICE_ENERGY,
    CONF_PRIMARY_ANCHOR,
    DEFAULT_TIMEZONE,
    DEFAULT_BOOST_ANCHOR,
    DEFAULT_ELEMENT_POWER_KW,
    DEFAULT_PREFER_DEVICE_ENERGY,
    DEFAULT_PRIMARY_ANCHOR,
    SecuremtrConfigFlow,
    _anchor_option_to_time,
    _serialize_anchor,
)


class ConfigFlowServiceRegistry:
    """Provide minimal service registration for config flow tests."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], tuple[callable, Any | None]] = {}

    def async_register(
        self,
        domain: str,
        service: str,
        handler: Callable[[SimpleNamespace], Any] | Callable[[SimpleNamespace], Awaitable[Any]],
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

    async def async_add(self, entry: config_entries.ConfigEntry) -> config_entries.ConfigEntry:
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

    async def async_update_entry(
        self,
        entry: config_entries.ConfigEntry,
        *,
        data: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        """Record config entry updates performed by the integration."""

        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)


class ConfigFlowHass:
    """Emulate the subset of Home Assistant APIs required for config flow tests."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.services = ConfigFlowServiceRegistry()
        self.config_entries = ConfigFlowConfigEntries()
        self._config_dir = Path(tempfile.mkdtemp(prefix="securemtr-config-"))
        self.config = SimpleNamespace(
            time_zone="Europe/London",
            config_dir=str(self._config_dir),
            path=lambda *parts: str(Path(self._config_dir, *parts)),
        )
        self.bus = SimpleNamespace(
            async_listen=lambda *args, **kwargs: None,
            async_listen_once=lambda *args, **kwargs: None,
        )
        self._tasks: list[asyncio.Task[Any]] = []
        self.state = CoreState.running
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
        self.state = CoreState.stopped

    def async_create_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """Schedule a coroutine on the running loop."""

        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def async_add_executor_job(
        self, func: Callable[..., Any], *args: Any
    ) -> Any:
        """Execute a blocking job synchronously for tests."""

        try:
            return func(*args)
        except FileNotFoundError:
            return None

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
        self.zone_calls: list[str] = []
        self.clock_calls: list[str] = []
        self.schedule_calls: list[str] = []
        self.configuration_calls: list[str] = []
        self.state_calls: list[str] = []
        self.energy_history_calls: list[tuple[str, int]] = []
        self.program_calls: list[tuple[str, str]] = []
        self._primary_program = tuple(
            DailyProgram((120, None, None), (240, None, None)) for _ in range(7)
        )
        self._boost_program = tuple(
            DailyProgram((1020, None, None), (1080, None, None)) for _ in range(7)
        )

    async def login(self, email: str, password_digest: str) -> BeanbagSession:
        """Record credentials and return the prepared session."""

        self.login_calls.append((email, password_digest))
        return self.session

    async def login_and_connect(
        self, email: str, password_digest: str
    ) -> tuple[BeanbagSession, DummyWebSocket]:
        """Record credentials and return canned session details."""

        session = await self.login(email, password_digest)
        return session, self.websocket

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

    async def read_zone_topology(
        self, session: BeanbagSession, websocket: DummyWebSocket, gateway_id: str
    ) -> list[dict[str, str]]:
        """Return a single synthetic zone entry."""

        self.zone_calls.append(gateway_id)
        return [{"ZN": 1, "ZNM": "Primary"}]

    async def sync_gateway_clock(
        self,
        session: BeanbagSession,
        websocket: DummyWebSocket,
        gateway_id: str,
        *,
        timestamp: int | None = None,
    ) -> None:
        """Record clock sync invocations."""

        self.clock_calls.append(gateway_id)

    async def read_schedule_overview(
        self, session: BeanbagSession, websocket: DummyWebSocket, gateway_id: str
    ) -> dict[str, list[object]]:
        """Return a canned schedule overview payload."""

        self.schedule_calls.append(gateway_id)
        return {"V": []}

    async def read_device_configuration(
        self, session: BeanbagSession, websocket: DummyWebSocket, gateway_id: str
    ) -> dict[str, list[object]]:
        """Return canned configuration data."""

        self.configuration_calls.append(gateway_id)
        return {"V": []}

    async def read_live_state(
        self, session: BeanbagSession, websocket: DummyWebSocket, gateway_id: str
    ) -> BeanbagStateSnapshot:
        """Return a state snapshot with the primary power enabled."""

        self.state_calls.append(gateway_id)
        payload = {
            "V": [
                {"I": 1, "SI": 33, "V": [{"I": 6, "V": 2}]},
                {"I": 2, "SI": 35, "V": [{"I": 6, "V": 0}]},
            ]
        }
        return BeanbagStateSnapshot(
            payload=payload,
            primary_power_on=True,
            timed_boost_enabled=False,
            timed_boost_active=False,
            timed_boost_end_minute=None,
        )

    async def read_energy_history(
        self,
        session: BeanbagSession,
        websocket: DummyWebSocket,
        gateway_id: str,
        *,
        window_index: int = 1,
    ) -> list[BeanbagEnergySample]:
        """Return canned energy samples for testing."""

        self.energy_history_calls.append((gateway_id, window_index))
        return [
            BeanbagEnergySample(
                timestamp=0,
                primary_energy_kwh=0.0,
                boost_energy_kwh=0.0,
                primary_scheduled_minutes=0,
                primary_active_minutes=0,
                boost_scheduled_minutes=0,
                boost_active_minutes=0,
            )
        ]

    async def read_weekly_program(
        self,
        session: BeanbagSession,
        websocket: DummyWebSocket,
        gateway_id: str,
        *,
        zone: str,
    ) -> tuple[DailyProgram, ...]:
        """Return canned weekly programs for the requested zone."""

        self.program_calls.append((gateway_id, zone))
        if zone == "boost":
            return self._boost_program
        return self._primary_program


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
    monkeypatch.setattr(
        config_flow,
        "async_get_clientsession",
        lambda hass: fake_session,
    )
    monkeypatch.setattr(
        config_flow,
        "BeanbagBackend",
        lambda session: backend,
    )

    async def _noop_consumption_metrics(hass, entry):
        return None

    monkeypatch.setattr(
        "custom_components.securemtr.consumption_metrics", _noop_consumption_metrics
    )

    monkeypatch.setattr(
        "custom_components.securemtr._submit_statistics", lambda *args, **kwargs: None
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
        options={},
    )

    assert await async_setup_entry(hass_fixture, entry)
    await hass_fixture.async_block_till_done()

    runtime = hass_fixture.data[DOMAIN][entry.entry_id]
    assert isinstance(runtime, SecuremtrRuntimeData)
    assert runtime.session is backend_patch.session
    assert runtime.websocket is backend_patch.websocket
    assert backend_patch.login_calls == [("user@example.com", hashed_password)]
    assert entry.data[CONF_EMAIL] == "user@example.com"
    assert entry.data[CONF_PASSWORD] == hashed_password
    assert entry.data[CONF_GATEWAY_ID] == "gateway-1"
    assert entry.data["serial_number"] == "serial-1"


@pytest.mark.asyncio
async def test_async_unload_entry_removes_entry_data(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure async_unload_entry clears stored data."""
    entry = SimpleNamespace(
        entry_id="entry-2",
        unique_id="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "digest"},
        options={},
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
async def test_config_flow_creates_entry(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
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
        CONF_GATEWAY_ID: "gateway-1",
        "serial_number": "serial-1",
    }
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")
    flow._abort_if_unique_id_configured.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_prompts_for_gateway_selection(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure the flow prompts for gateway selection when multiple exist."""

    backend_patch.session = BeanbagSession(
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
            BeanbagGateway(
                gateway_id="gateway-2",
                serial_number="serial-2",
                host_name="other-host",
                capabilities={},
            ),
        ),
    )

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "gateway"

    selection = await flow.async_step_gateway({CONF_GATEWAY_ID: "gateway-2"})

    expected_hash = hashlib.md5("secret".encode("utf-8")).hexdigest()
    assert selection["type"] == FlowResultType.CREATE_ENTRY
    assert selection["data"] == {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: expected_hash,
        CONF_GATEWAY_ID: "gateway-2",
        "serial_number": "serial-2",
    }


@pytest.mark.asyncio
async def test_config_flow_handles_login_failure(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure login failures surface as connection errors."""

    async def failing_login(email: str, password_digest: str) -> BeanbagSession:
        raise BeanbagError("boom")

    backend_patch.login = failing_login

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    flow.async_set_unique_id.assert_awaited_once_with("user@example.com")


@pytest.mark.asyncio
async def test_config_flow_handles_login_exception(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure unexpected login exceptions surface as unknown errors."""

    async def crashing_login(email: str, password_digest: str) -> BeanbagSession:
        raise RuntimeError("kaboom")

    backend_patch.login = crashing_login

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_config_flow_handles_empty_gateway_list(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure an empty gateway list returns the user form with an error."""

    backend_patch.session = BeanbagSession(
        user_id=42,
        session_id="session-id",
        token="jwt-token",
        token_timestamp=None,
        gateways=(),
    )

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_config_flow_retries_gateway_form_on_invalid_selection(
    hass_fixture: ConfigFlowHass, backend_patch: DummyBackend
) -> None:
    """Ensure invalid selections redisplay the gateway form."""

    backend_patch.session = BeanbagSession(
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
            BeanbagGateway(
                gateway_id="gateway-2",
                serial_number="serial-2",
                host_name="other-host",
                capabilities={},
            ),
        ),
    )

    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    await flow.async_step_user({CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"})
    result = await flow.async_step_gateway({CONF_GATEWAY_ID: "missing"})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "gateway"


def test_config_flow_create_entry_requires_state() -> None:
    """Ensure creating an entry without cached credentials fails."""

    flow = SecuremtrConfigFlow()
    gateway = BeanbagGateway(
        gateway_id="gateway-1",
        serial_number="serial-1",
        host_name="host",
        capabilities={},
    )

    with pytest.raises(ValueError):
        flow._async_create_config_entry(gateway)


def test_gateway_label_falls_back_to_identifier() -> None:
    """Ensure the gateway label uses the identifier when the serial is absent."""

    flow = SecuremtrConfigFlow()
    gateway = BeanbagGateway(
        gateway_id="gateway-only",
        serial_number=None,
        host_name=None,
        capabilities={},
    )

    assert flow._gateway_label(gateway) == "gateway-only"


@pytest.mark.asyncio
async def test_config_flow_rejects_empty_email(
    hass_fixture: ConfigFlowHass,
) -> None:
    """Ensure config flow rejects empty email addresses."""
    flow = SecuremtrConfigFlow()
    flow.hass = hass_fixture

    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock()

    result = await flow.async_step_user(
        {CONF_EMAIL: "   ", CONF_PASSWORD: "secret"}
    )

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
async def test_options_flow_falls_back_to_default_timezone(caplog: pytest.LogCaptureFixture) -> None:
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


def test_serialize_anchor_precision() -> None:
    """Ensure anchor serialization preserves precision tiers."""

    assert _serialize_anchor(time(4, 30)) == "04:30"
    assert _serialize_anchor(time(4, 30, 5)) == "04:30:05"
    assert (
        _serialize_anchor(time(4, 30, 5, 120000))
        == "04:30:05.120000"
    )
