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


@pytest.mark.asyncio
async def test_backend_read_device_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure metadata requests are sent with the documented headers."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"
    response_payload = {"BOI": "controller"}

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": response_payload}

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    metadata = await backend.read_device_metadata(session_data, websocket, "gateway-1")

    assert metadata == response_payload
    assert websocket.sent
    header = websocket.sent[0]["P"][0]
    assert header == {"GMI": "gateway-1", "HI": 17, "SI": 11}
    assert websocket.sent[0]["I"] == expected_correlation


@pytest.mark.asyncio
async def test_backend_read_device_metadata_validates_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise an error when the metadata payload is not an object."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": []}

    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    with pytest.raises(BeanbagWebSocketError):
        await backend.read_device_metadata(session_data, DummyWebSocket(), "gateway-1")


@pytest.mark.asyncio
async def test_backend_read_zone_topology_filters_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure non-dictionary entries are ignored from the zone list."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": [{"ZN": 1}, "ignored"]}

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    zones = await backend.read_zone_topology(session_data, websocket, "gateway-1")

    assert zones == [{"ZN": 1}]
    assert websocket.sent
    assert websocket.sent[0]["P"][0] == {"GMI": "gateway-1", "HI": 49, "SI": 11}


@pytest.mark.asyncio
async def test_backend_read_zone_topology_requires_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise when the zone payload is not a list."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": {"not": "a list"}}

    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    with pytest.raises(BeanbagWebSocketError):
        await backend.read_zone_topology(session_data, DummyWebSocket(), "gateway-1")


@pytest.mark.asyncio
async def test_backend_sync_gateway_clock_validates_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise when the controller clock reply is not the expected acknowledgement."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": 5}

    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1234)

    with pytest.raises(BeanbagWebSocketError):
        await backend.sync_gateway_clock(session_data, DummyWebSocket(), "gateway-1")


@pytest.mark.asyncio
async def test_backend_sync_gateway_clock_accepts_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept acknowledgement payloads that match vendor behaviour."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": 0}

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 2468)

    await backend.sync_gateway_clock(session_data, websocket, "gateway-1")

    assert websocket.sent[0]["P"][1] == [2468]


@pytest.mark.asyncio
async def test_backend_read_schedule_overview_requires_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise when the schedule overview payload is not an object."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": [1, 2, 3]}

    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    with pytest.raises(BeanbagWebSocketError):
        await backend.read_schedule_overview(session_data, DummyWebSocket(), "gateway-1")


@pytest.mark.asyncio
async def test_backend_read_schedule_overview_returns_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the schedule payload when the structure matches expectations."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": {"V": [1, 2, 3]}}

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    payload = await backend.read_schedule_overview(
        session_data, websocket, "gateway-1"
    )

    assert payload == {"V": [1, 2, 3]}


@pytest.mark.asyncio
async def test_backend_read_device_configuration_requires_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise when the configuration payload is not an object."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": "not-a-dict"}

    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    with pytest.raises(BeanbagWebSocketError):
        await backend.read_device_configuration(
            session_data, DummyWebSocket(), "gateway-1"
        )


@pytest.mark.asyncio
async def test_backend_read_device_configuration_returns_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return configuration payloads that match the documented format."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": {"V": []}}

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    payload = await backend.read_device_configuration(
        session_data, websocket, "gateway-1"
    )

    assert payload == {"V": []}


def test_backend_extract_primary_power_variants() -> None:
    """Cover edge cases for parsing the primary power flag."""

    backend = BeanbagBackend(Mock())

    assert backend._extract_primary_power({}) is None
    assert backend._extract_primary_power({"V": ["not-dict"]}) is None
    assert backend._extract_primary_power({"V": [{"SI": 10}]}) is None
    assert backend._extract_primary_power({"V": [{"SI": 33, "V": "bad"}]}) is None
    assert (
        backend._extract_primary_power({"V": [{"SI": 33, "V": [{}]}]}) is None
    )
    assert (
        backend._extract_primary_power({"V": [{"SI": 33, "V": [{"I": 99}]}]})
        is None
    )
    assert (
        backend._extract_primary_power(
            {"V": [{"SI": 33, "V": [{"I": 6, "V": 2}]}]}
        )
        is True
    )
    assert (
        backend._extract_primary_power(
            {"V": [{"SI": 33, "V": ["skip", {"I": 6, "V": 0}]}]}
        )
        is False
    )


@pytest.mark.asyncio
async def test_backend_read_live_state_parses_primary_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse the primary power flag from a live state payload."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            payload = {
                "I": expected_correlation,
                "R": {"V": [{"SI": 33, "V": [{"I": 6, "V": 0}]}]},
            }
            return payload

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    snapshot = await backend.read_live_state(session_data, websocket, "gateway-1")

    assert snapshot.primary_power_on is False
    assert snapshot.payload == {"V": [{"SI": 33, "V": [{"I": 6, "V": 0}]}]}
    assert websocket.sent[0]["P"][0] == {"GMI": "gateway-1", "HI": 3, "SI": 1}


@pytest.mark.asyncio
async def test_backend_read_live_state_requires_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise when the live state payload is not an object."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        async def send_json(self, payload: dict[str, Any]) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": []}

    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    with pytest.raises(BeanbagWebSocketError):
        await backend.read_live_state(session_data, DummyWebSocket(), "gateway-1")


@pytest.mark.asyncio
async def test_backend_turn_controller_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify power commands invoke the WebSocket helper with correct payloads."""

    backend = BeanbagBackend(Mock())
    send = AsyncMock(return_value=0)
    monkeypatch.setattr(backend, "_send_request", send)

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    websocket = Mock()

    await backend.turn_controller_on(session_data, websocket, "gateway-1")
    await backend.turn_controller_off(session_data, websocket, "gateway-1")

    assert send.await_args_list[0].kwargs == {
        "header_hi": 2,
        "header_si": 15,
        "args": [1, {"I": 6, "V": 2}],
    }
    assert send.await_args_list[0].args[:3] == (
        session_data,
        websocket,
        "gateway-1",
    )

    assert send.await_args_list[1].kwargs == {
        "header_hi": 2,
        "header_si": 15,
        "args": [1, {"I": 6, "V": 0}],
    }


@pytest.mark.asyncio
async def test_backend_turn_controller_mode_write_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise when the mode write acknowledgement is unexpected."""

    backend = BeanbagBackend(Mock())
    send = AsyncMock(return_value=5)
    monkeypatch.setattr(backend, "_send_request", send)

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )

    with pytest.raises(BeanbagWebSocketError):
        await backend.turn_controller_on(session_data, Mock(), "gateway-1")


@pytest.mark.asyncio
async def test_backend_send_request_handles_informational_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the request helper skips non-result frames and errors when needed."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self._responses = [
                ["not-a-dict"],
                {"I": "other", "R": 0},
                {"I": expected_correlation, "M": "Notify"},
                {"I": expected_correlation},
            ]

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> Any:
            return self._responses.pop(0)

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    with pytest.raises(BeanbagWebSocketError):
        await backend._send_request(  # type: ignore[attr-defined]
            session_data,
            websocket,  # type: ignore[arg-type]
            "gateway-1",
            header_hi=1,
            header_si=2,
        )

    assert websocket.sent


@pytest.mark.asyncio
async def test_backend_send_request_with_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure argument lists are included in the transmitted payload."""

    session_data = BeanbagSession(
        user_id=1,
        session_id="abc",
        token="jwt",
        token_timestamp=None,
        gateways=(),
    )
    expected_correlation = "abc-00000001"

    class DummyWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def receive_json(self) -> dict[str, Any]:
            return {"I": expected_correlation, "R": 0}

    websocket = DummyWebSocket()
    backend = BeanbagBackend(Mock())
    monkeypatch.setattr(
        "custom_components.securemtr.beanbag.secrets.randbits", lambda bits: 1
    )
    monkeypatch.setattr("custom_components.securemtr.beanbag.time.time", lambda: 1000)

    result = await backend._send_request(  # type: ignore[attr-defined]
        session_data,
        websocket,  # type: ignore[arg-type]
        "gateway-1",
        header_hi=2,
        header_si=15,
        args=[1, {"I": 6, "V": 2}],
    )

    assert result == 0
    assert websocket.sent[0]["P"][1] == [1, {"I": 6, "V": 2}]
