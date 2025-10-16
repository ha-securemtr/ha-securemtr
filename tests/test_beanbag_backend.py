"""Tests for the Beanbag backend helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError

# Ensure the integration package can be imported without installation.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr.beanbag import (
    BeanbagBackend,
    BeanbagHttpClient,
    BeanbagLoginError,
    BeanbagSession,
    BeanbagWebSocketClient,
    BeanbagWebSocketError,
)


class DummyResponse:
    """Provide an async context manager wrapper for HTTP responses."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: str | None = None) -> dict[str, Any]:
        """Return the stored JSON payload."""

        return self._payload

    async def __aenter__(self) -> "DummyResponse":
        """Enter the async context manager."""

        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Exit the async context manager."""

        return None


@pytest.mark.asyncio
async def test_login_success_parses_payload() -> None:
    """Verify the login flow parses the documented response structure."""

    session = Mock()
    payload = {
        "RI": "1",
        "D": {
            "UI": 77,
            "SI": 12345,
            "JT": "token-abc",
            "JTT": "not-int",
            "GD": [
                {
                    "GMI": "gateway-1",
                    "SN": 1001,
                    "HN": "host-name",
                    "CS": 4,
                    "UR": 1,
                }
            ],
        },
    }
    session.post = Mock(return_value=DummyResponse(200, payload))

    client = BeanbagHttpClient(session)
    session_data = await client.login(
        "user@example.com", "0123456789abcdef0123456789abcdef"
    )

    assert isinstance(session_data, BeanbagSession)
    assert session_data.user_id == 77
    assert session_data.session_id == "12345"
    assert session_data.token == "token-abc"
    assert session_data.token_timestamp is None
    assert len(session_data.gateways) == 1
    gateway = session_data.gateways[0]
    assert gateway.gateway_id == "gateway-1"
    assert gateway.serial_number is None
    assert gateway.host_name == "host-name"
    assert gateway.capabilities == {"CS": 4, "UR": 1}

    session.post.assert_called_once()
    _, kwargs = session.post.call_args
    assert kwargs["headers"] == {"Request-id": "1"}
    assert kwargs["json"]["ULC"]["UEI"] == "user@example.com"


@pytest.mark.asyncio
async def test_login_rejects_empty_email() -> None:
    """Ensure an empty email raises a validation error."""

    client = BeanbagHttpClient(Mock())

    with pytest.raises(ValueError):
        await client.login("", "0123456789abcdef0123456789abcdef")


@pytest.mark.asyncio
async def test_login_rejects_invalid_digest() -> None:
    """Ensure an invalid digest string is rejected."""

    client = BeanbagHttpClient(Mock())

    with pytest.raises(ValueError):
        await client.login("user@example.com", "bad-digest")


@pytest.mark.asyncio
async def test_login_handles_http_error() -> None:
    """Translate aiohttp failures into BeanbagLoginError."""

    session = Mock()
    session.post = Mock(side_effect=ClientError("boom"))
    client = BeanbagHttpClient(session)

    with pytest.raises(BeanbagLoginError):
        await client.login("user@example.com", "0123456789abcdef0123456789abcdef")


@pytest.mark.asyncio
async def test_login_rejects_unexpected_status() -> None:
    """Raise when the login response code is not HTTP 200."""

    session = Mock()
    session.post = Mock(return_value=DummyResponse(500, {"RI": "0"}))
    client = BeanbagHttpClient(session)

    with pytest.raises(BeanbagLoginError):
        await client.login("user@example.com", "0123456789abcdef0123456789abcdef")


@pytest.mark.asyncio
async def test_login_rejects_unsuccessful_indicator() -> None:
    """Raise when the login response indicates failure."""

    session = Mock()
    payload = {"RI": "0", "D": {}}
    session.post = Mock(return_value=DummyResponse(200, payload))
    client = BeanbagHttpClient(session)

    with pytest.raises(BeanbagLoginError):
        await client.login("user@example.com", "0123456789abcdef0123456789abcdef")


@pytest.mark.asyncio
async def test_login_requires_data_object() -> None:
    """Raise when the login payload lacks the data block."""

    session = Mock()
    payload = {"RI": "1", "D": None}
    session.post = Mock(return_value=DummyResponse(200, payload))
    client = BeanbagHttpClient(session)

    with pytest.raises(BeanbagLoginError):
        await client.login("user@example.com", "0123456789abcdef0123456789abcdef")


@pytest.mark.asyncio
async def test_login_requires_expected_fields() -> None:
    """Raise when the login payload omits mandatory fields."""

    session = Mock()
    payload = {"RI": "1", "D": {"UI": 2}}
    session.post = Mock(return_value=DummyResponse(200, payload))
    client = BeanbagHttpClient(session)

    with pytest.raises(BeanbagLoginError):
        await client.login("user@example.com", "0123456789abcdef0123456789abcdef")


@pytest.mark.asyncio
async def test_websocket_connect_uses_expected_headers() -> None:
    """Verify the WebSocket client sets the documented headers."""

    session = Mock()
    fake_ws = object()
    session.ws_connect = AsyncMock(return_value=fake_ws)
    client = BeanbagWebSocketClient(session)

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )

    websocket = await client.connect(session_data)

    assert websocket is fake_ws
    session.ws_connect.assert_awaited_once()
    args, kwargs = session.ws_connect.call_args
    assert args[0] == "wss://app.beanbag.online/api/TransactionRestAPI/ConnectWebSocket"
    assert kwargs["headers"] == {
        "Authorization": "Bearer jwt",
        "Session-id": "abc",
        "Request-id": "1",
    }
    assert kwargs["protocols"] == ["BB-BO-01"]


@pytest.mark.asyncio
async def test_websocket_connect_translates_errors() -> None:
    """Translate aiohttp WebSocket failures into BeanbagWebSocketError."""

    session = Mock()
    session.ws_connect = AsyncMock(side_effect=ClientError("boom"))
    client = BeanbagWebSocketClient(session)

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )

    with pytest.raises(BeanbagWebSocketError):
        await client.connect(session_data)


@pytest.mark.asyncio
async def test_backend_login_and_connect_flow() -> None:
    """Verify the combined backend performs login then WebSocket connect."""

    session = Mock()
    payload = {
        "RI": "1",
        "D": {"UI": 5, "SI": 6, "JT": "jwt-token"},
    }
    session.post = Mock(return_value=DummyResponse(200, payload))
    fake_ws = object()
    session.ws_connect = AsyncMock(return_value=fake_ws)

    backend = BeanbagBackend(session)
    session_data, websocket = await backend.login_and_connect(
        "user@example.com", "0123456789abcdef0123456789abcdef"
    )

    assert session_data.token == "jwt-token"
    assert websocket is fake_ws
    assert session.post.called
    assert session.ws_connect.called
