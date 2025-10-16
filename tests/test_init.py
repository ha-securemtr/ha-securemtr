"""Tests for the securemtr integration setup lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable
from types import SimpleNamespace

import pytest

from custom_components.securemtr import (
    DOMAIN,
    SecuremtrRuntimeData,
    _entry_display_name,
    _async_fetch_controller,
    _build_controller,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.securemtr.beanbag import (
    BeanbagError,
    BeanbagGateway,
    BeanbagSession,
)


@dataclass(slots=True)
class DummyConfigEntry:
    """Provide a lightweight stand-in for Home Assistant config entries."""

    entry_id: str
    data: dict[str, str]
    unique_id: str | None = None
    title: str | None = None


class FakeWebSocket:
    """Represent a simple closable WebSocket stub."""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        """Record the close invocation and mark the socket as closed."""

        self.close_calls += 1
        self.closed = True


class FakeBeanbagBackend:
    """Capture login requests and provide canned responses."""

    def __init__(self, session: object) -> None:
        self.session = session
        self.login_calls: list[tuple[str, str]] = []
        self.metadata_calls: list[str] = []
        self._session = BeanbagSession(
            user_id=1,
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
        self.websocket = FakeWebSocket()

    async def login_and_connect(
        self, email: str, password_digest: str
    ) -> tuple[BeanbagSession, FakeWebSocket]:
        """Record the credentials and return canned connection artefacts."""

        self.login_calls.append((email, password_digest))
        return self._session, self.websocket

    async def read_device_metadata(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> dict[str, str]:
        """Return canned metadata for the sole controller."""

        self.metadata_calls.append(gateway_id)
        return {
            "BOI": "controller-1",
            "N": "E7+ Controller",
            "SN": "serial-1",
            "FV": "1.0.0",
            "MD": "E7+",
        }

    async def turn_controller_on(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> None:
        """Pretend to send the power-on command."""

        self.metadata_calls.append(f"on:{gateway_id}")

    async def turn_controller_off(
        self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
    ) -> None:
        """Pretend to send the power-off command."""

        self.metadata_calls.append(f"off:{gateway_id}")


class FakeConfigEntries:
    """Mimic Home Assistant's config entries helper."""

    def __init__(self) -> None:
        self.forwarded: list[tuple[str, ...]] = []
        self.unloaded: list[tuple[str, ...]] = []

    async def async_forward_entry_setups(
        self, entry: DummyConfigEntry, platforms: list[str]
    ) -> None:
        """Record forwarded platforms."""

        self.forwarded.append(tuple(platforms))

    async def async_unload_platforms(
        self, entry: DummyConfigEntry, platforms: list[str]
    ) -> bool:
        """Record unloaded platforms and report success."""

        self.unloaded.append(tuple(platforms))
        return True


class FakeHass:
    """Emulate the subset of Home Assistant APIs used by the integration."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, SecuremtrRuntimeData]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self.config_entries = FakeConfigEntries()

    def async_create_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        """Schedule a coroutine on the running loop and keep a reference."""

        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def async_block_till_done(self) -> None:
        """Await all scheduled tasks to complete."""

        if not self._tasks:
            return
        await asyncio.gather(*self._tasks)


@pytest.mark.asyncio
async def test_async_setup_entry_starts_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that setup schedules the Beanbag login and stores runtime data."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="1",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.backend is backend
    assert runtime.session is backend._session
    assert runtime.websocket is backend.websocket
    assert runtime.controller is not None
    assert runtime.controller.identifier == "serial-1"
    assert backend.login_calls == [("user@example.com", "digest")]
    assert backend.metadata_calls[0] == "gateway-1"
    assert hass.config_entries.forwarded == [("switch",)]


@pytest.mark.asyncio
async def test_async_setup_entry_handles_missing_gateways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure controller discovery errors leave the runtime in a safe state."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="missing-gateway",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class NoGatewayBackend(FakeBeanbagBackend):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self._session = BeanbagSession(
                user_id=1,
                session_id="session-id",
                token="jwt-token",
                token_timestamp=None,
                gateways=(),
            )

    fake_session = object()
    backend = NoGatewayBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_setup_entry_logs_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Beanbag metadata errors do not crash the startup task."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="metadata-error",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class MetadataFailingBackend(FakeBeanbagBackend):
        async def read_device_metadata(
            self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
        ) -> dict[str, str]:
            raise BeanbagError("metadata failure")

    fake_session = object()
    backend = MetadataFailingBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_setup_entry_handles_unexpected_metadata_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure unexpected metadata failures are caught and logged."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="metadata-exception",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class ExplodingBackend(FakeBeanbagBackend):
        async def read_device_metadata(
            self, session: BeanbagSession, websocket: FakeWebSocket, gateway_id: str
        ) -> dict[str, str]:
            raise RuntimeError("boom")

    fake_session = object()
    backend = ExplodingBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_setup_entry_handles_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure backend failures are caught and do not populate runtime state."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="2",
        unique_id="user2@example.com",
        data={"email": "user2@example.com", "password": "digest"},
        title="SecureMTR",
    )

    class FailingBackend(FakeBeanbagBackend):
        async def login_and_connect(self, email: str, password_digest: str):
            raise BeanbagError("login failed")

    fake_session = object()
    backend = FailingBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.session is None
    assert runtime.websocket is None


@pytest.mark.asyncio
async def test_async_unload_entry_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm unload cancels tasks and closes the websocket."""

    hass = FakeHass()
    entry = DummyConfigEntry(
        entry_id="3",
        unique_id="user3@example.com",
        data={"email": "user3@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    # Insert a hanging task to exercise the cancellation path.
    runtime.startup_task = asyncio.create_task(asyncio.sleep(0.1))

    assert await async_unload_entry(hass, entry)
    assert entry.entry_id not in hass.data[DOMAIN]
    assert backend.websocket.close_calls == 1
    await asyncio.sleep(0)
    assert runtime.startup_task.cancelled()
    assert hass.config_entries.unloaded == [("switch",)]


@pytest.mark.asyncio
async def test_async_setup_entry_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure backend startup short-circuits when credentials are absent."""

    hass = FakeHass()
    entry = DummyConfigEntry(entry_id="4", unique_id="user4@example.com", data={})

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.session is None
    assert runtime.websocket is None
    assert backend.login_calls == []


@pytest.mark.asyncio
async def test_async_unload_entry_without_runtime() -> None:
    """Verify unload succeeds gracefully when runtime data is missing."""

    hass = FakeHass()
    hass.data.setdefault(DOMAIN, {})
    entry = DummyConfigEntry(entry_id="missing", unique_id=None, data={})

    assert await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_async_setup_entry_without_config_entries_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the setup path when Home Assistant lacks the helper attribute."""

    hass = FakeHass()
    hass.config_entries = None
    entry = DummyConfigEntry(
        entry_id="no-helper",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert runtime.controller is not None
    assert runtime.controller_ready.is_set()


@pytest.mark.asyncio
async def test_async_unload_entry_without_config_entries_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the unload path when Home Assistant lacks the helper attribute."""

    hass = FakeHass()
    hass.config_entries = None
    entry = DummyConfigEntry(
        entry_id="no-helper-unload",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "digest"},
        title="SecureMTR",
    )

    fake_session = object()
    backend = FakeBeanbagBackend(fake_session)

    monkeypatch.setattr(
        "custom_components.securemtr.async_get_clientsession",
        lambda hass_obj: fake_session,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.BeanbagBackend",
        lambda session: backend,
    )

    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()

    assert await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_async_fetch_controller_requires_connection() -> None:
    """Ensure controller fetching rejects missing session data."""

    runtime = SecuremtrRuntimeData(backend=FakeBeanbagBackend(object()))
    entry = DummyConfigEntry(
        entry_id="fetch-error",
        unique_id="user@example.com",
        data={},
    )

    with pytest.raises(BeanbagError):
        await _async_fetch_controller(entry, runtime)


def test_build_controller_normalises_metadata() -> None:
    """Verify metadata parsing handles blank serial numbers and names."""

    metadata = {"BOI": "", "SN": "", "N": "   ", "FV": 2, "MD": "E7+"}
    gateway = BeanbagGateway(
        gateway_id="gateway-1",
        serial_number=None,
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)
    assert controller.identifier == "gateway-1"
    assert controller.name == "SecureMTR gateway-1"
    assert controller.serial_number is None
    assert controller.firmware_version == "2"
    assert controller.model == "E7+"


def test_build_controller_prefers_serial_number() -> None:
    """Ensure the serial number is used when metadata supplies one."""

    metadata = {
        "BOI": "2",
        "SN": "serial-1",
        "N": "Controller",
        "FV": "1.0.0",
        "MD": "E7+",
    }
    gateway = BeanbagGateway(
        gateway_id="gateway-1",
        serial_number="gateway-serial",
        host_name="host",
        capabilities={},
    )

    controller = _build_controller(metadata, gateway)

    assert controller.identifier == "serial-1"
    assert controller.serial_number == "serial-1"


def test_entry_display_name_prefers_title() -> None:
    """Ensure the helper surfaces a provided title."""

    entry = SimpleNamespace(title="SecureMTR", entry_id="entry-id")
    assert _entry_display_name(entry) == "SecureMTR"


def test_entry_display_name_falls_back_to_domain() -> None:
    """Ensure the helper provides a generic fallback when metadata is absent."""

    entry = SimpleNamespace()
    assert _entry_display_name(entry) == DOMAIN
