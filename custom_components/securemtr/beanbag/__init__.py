"""Beanbag cloud backend clients for Secure Meters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientWebSocketResponse
from yarl import URL

_LOGGER = logging.getLogger(__name__)

REST_BASE_URL = "https://app.beanbag.online"
LOGIN_PATH = "/api/UserRestAPI/LoginRequest"
WS_PATH = "/api/TransactionRestAPI/ConnectWebSocket"
REQUEST_ID = "1"
SUBPROTOCOL = "BB-BO-01"


class BeanbagError(RuntimeError):
    """Base exception for Beanbag backend issues."""


class BeanbagLoginError(BeanbagError):
    """Raised when the Beanbag login flow fails."""


class BeanbagWebSocketError(BeanbagError):
    """Raised when establishing the Beanbag WebSocket fails."""


@dataclass(slots=True)
class BeanbagGateway:
    """Represent a Beanbag gateway discovered during login."""

    gateway_id: str
    serial_number: str | None
    host_name: str | None
    capabilities: dict[str, Any]


@dataclass(slots=True)
class BeanbagSession:
    """Hold the authenticated Beanbag session context."""

    user_id: int
    session_id: str
    token: str
    token_timestamp: int | None
    gateways: tuple[BeanbagGateway, ...]


class BeanbagHttpClient:
    """Perform REST interactions with the Beanbag API."""

    def __init__(self, session: ClientSession, base_url: str = REST_BASE_URL) -> None:
        """Initialize the HTTP client wrapper."""

        self._session = session
        self._base_url = base_url.rstrip("/")

    async def login(self, email: str, password_digest: str) -> BeanbagSession:
        """Execute the documented Beanbag login flow."""

        if not email:
            raise ValueError("Email address must be provided for login")

        if len(password_digest) != 32 or not all(
            ch in "0123456789abcdefABCDEF" for ch in password_digest
        ):
            raise ValueError("Password digest must be a 32-character hexadecimal string")

        payload = {
            "ULC": {
                "OI": 1550005,
                "P": password_digest,
                "NT": "SetLogin",
                "UEI": email,
            }
        }
        url = f"{self._base_url}{LOGIN_PATH}"
        headers = {"Request-id": REQUEST_ID}

        _LOGGER.info("Starting Beanbag login request for %s", email)

        try:
            async with self._session.post(url, json=payload, headers=headers) as response:
                body = await response.json(content_type=None)
                status = response.status
        except ClientError as error:
            raise BeanbagLoginError("Beanbag login request failed") from error

        if status != 200:
            raise BeanbagLoginError(f"Unexpected HTTP status {status} during login")

        if body.get("RI") != "1":
            raise BeanbagLoginError("Beanbag login rejected by server")

        data = body.get("D")
        if not isinstance(data, dict):
            raise BeanbagLoginError("Beanbag login payload missing session data")

        try:
            session_id = str(data["SI"])
            user_id = int(data["UI"])
            token = str(data["JT"])
        except (KeyError, TypeError, ValueError) as error:
            raise BeanbagLoginError("Beanbag login response missing required fields") from error

        token_timestamp = data.get("JTT") if isinstance(data.get("JTT"), int) else None

        gateways_raw: Iterable[dict[str, Any]] = data.get("GD") or []
        gateways = tuple(self._parse_gateway(gateway) for gateway in gateways_raw)

        _LOGGER.info("Beanbag login succeeded for %s", email)

        return BeanbagSession(
            user_id=user_id,
            session_id=session_id,
            token=token,
            token_timestamp=token_timestamp,
            gateways=gateways,
        )

    @staticmethod
    def _parse_gateway(raw: dict[str, Any]) -> BeanbagGateway:
        """Translate raw gateway payload into an object."""

        gateway_id = str(raw.get("GMI", ""))
        serial_number = raw.get("SN")
        host_name = raw.get("HN")
        capabilities = {key: raw[key] for key in ("CS", "UR", "HI", "DT", "DN") if key in raw}
        return BeanbagGateway(
            gateway_id=gateway_id,
            serial_number=serial_number if isinstance(serial_number, str) else None,
            host_name=host_name if isinstance(host_name, str) else None,
            capabilities=capabilities,
        )


class BeanbagWebSocketClient:
    """Manage the Beanbag WebSocket connection."""

    def __init__(self, session: ClientSession, base_url: str = REST_BASE_URL) -> None:
        """Initialize the WebSocket client wrapper."""

        self._session = session
        self._ws_url = self._build_ws_url(base_url)

    async def connect(self, session: BeanbagSession) -> ClientWebSocketResponse:
        """Open the Beanbag WebSocket using the authenticated session."""

        headers = {
            "Authorization": f"Bearer {session.token}",
            "Session-id": session.session_id,
            "Request-id": REQUEST_ID,
        }

        _LOGGER.info("Opening Beanbag WebSocket for session %s", session.session_id)

        try:
            websocket = await self._session.ws_connect(
                self._ws_url,
                headers=headers,
                protocols=[SUBPROTOCOL],
            )
        except ClientError as error:
            raise BeanbagWebSocketError("Beanbag WebSocket connection failed") from error

        _LOGGER.info("Beanbag WebSocket connected for session %s", session.session_id)
        return websocket

    @staticmethod
    def _build_ws_url(base_url: str) -> str:
        """Construct the WebSocket URL from the REST base."""

        url = URL(base_url)
        scheme = "wss" if url.scheme == "https" else "ws"
        return str(url.with_scheme(scheme).with_path(WS_PATH))


class BeanbagBackend:
    """Coordinate HTTP and WebSocket clients for Beanbag."""

    def __init__(self, session: ClientSession, base_url: str = REST_BASE_URL) -> None:
        """Prepare the combined backend helper."""

        self._http = BeanbagHttpClient(session, base_url)
        self._ws = BeanbagWebSocketClient(session, base_url)

    async def login(self, email: str, password_digest: str) -> BeanbagSession:
        """Authenticate with the Beanbag REST API."""

        return await self._http.login(email, password_digest)

    async def connect_websocket(self, session: BeanbagSession) -> ClientWebSocketResponse:
        """Connect to the Beanbag WebSocket using the login session."""

        return await self._ws.connect(session)

    async def login_and_connect(
        self, email: str, password_digest: str
    ) -> tuple[BeanbagSession, ClientWebSocketResponse]:
        """Run the login flow and immediately open the WebSocket."""

        session = await self.login(email, password_digest)
        websocket = await self.connect_websocket(session)
        return session, websocket


__all__ = [
    "BeanbagBackend",
    "BeanbagGateway",
    "BeanbagHttpClient",
    "BeanbagLoginError",
    "BeanbagSession",
    "BeanbagWebSocketClient",
    "BeanbagWebSocketError",
]
