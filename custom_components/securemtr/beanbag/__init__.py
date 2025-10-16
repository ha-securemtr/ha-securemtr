"""Beanbag cloud backend clients for Secure Meters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    ContentTypeError,
)
from yarl import URL

_LOGGER = logging.getLogger(__name__)

EMAIL_PLACEHOLDER = "{email}"
PASSWORD_DIGEST_PLACEHOLDER = "{password_digest}"
TOKEN_PLACEHOLDER = "{token}"
SESSION_ID_PLACEHOLDER = "{session_id}"
USER_ID_PLACEHOLDER = "{user_id}"

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


@dataclass(slots=True)
class BeanbagStateSnapshot:
    """Represent the parsed result of a live state query."""

    payload: dict[str, Any]
    primary_power_on: bool | None


class BeanbagHttpClient:
    """Perform REST interactions with the Beanbag API."""

    def __init__(self, session: ClientSession, base_url: str = REST_BASE_URL) -> None:
        """Initialize the HTTP client wrapper."""

        self._session = session
        self._base_url = base_url.rstrip("/")

    async def login(self, email: str, password_digest: str) -> BeanbagSession:
        """Execute the documented Beanbag login flow."""

        normalized_email = email.strip()

        if not normalized_email:
            raise ValueError("Email address must be provided for login")

        if len(password_digest) != 32 or not all(
            ch in "0123456789abcdefABCDEF" for ch in password_digest
        ):
            raise ValueError(
                "Password digest must be a 32-character hexadecimal string"
            )

        payload = {
            "ULC": {
                "OI": 1550005,
                "P": password_digest,
                "NT": "SetLogin",
                "UEI": normalized_email,
            }
        }
        url = f"{self._base_url}{LOGIN_PATH}"
        headers = {"Request-id": REQUEST_ID}

        sanitized_payload = {
            "ULC": {
                "OI": 1550005,
                "P": PASSWORD_DIGEST_PLACEHOLDER,
                "NT": "SetLogin",
                "UEI": EMAIL_PLACEHOLDER,
            }
        }
        _LOGGER.info("Starting Beanbag login request for %s", EMAIL_PLACEHOLDER)
        _LOGGER.debug(
            "Beanbag login POST %s with headers=%s payload=%s",
            url,
            {"Request-id": REQUEST_ID},
            sanitized_payload,
        )

        try:
            async with self._session.post(
                url, json=payload, headers=headers
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except (ContentTypeError, ValueError) as error:
                    _LOGGER.error(
                        "Beanbag login response was not valid JSON for %s",
                        EMAIL_PLACEHOLDER,
                    )
                    raise BeanbagLoginError(
                        "Beanbag login response was not valid JSON",
                    ) from error
        except ClientError as error:
            _LOGGER.error(
                "Beanbag login request failed for %s: %s", EMAIL_PLACEHOLDER, error
            )
            raise BeanbagLoginError("Beanbag login request failed") from error

        if status != 200:
            _LOGGER.error(
                "Unexpected HTTP status %s during Beanbag login for %s",
                status,
                EMAIL_PLACEHOLDER,
            )
            raise BeanbagLoginError(f"Unexpected HTTP status {status} during login")

        if body.get("RI") != "1":
            _LOGGER.error("Beanbag login rejected by server for %s", EMAIL_PLACEHOLDER)
            raise BeanbagLoginError("Beanbag login rejected by server")

        data = body.get("D")
        if not isinstance(data, dict):
            _LOGGER.error(
                "Beanbag login payload missing session data for %s", EMAIL_PLACEHOLDER
            )
            raise BeanbagLoginError("Beanbag login payload missing session data")

        try:
            session_id = str(data["SI"])
            user_id = int(data["UI"])
            token = str(data["JT"])
        except (KeyError, TypeError, ValueError) as error:
            _LOGGER.error(
                "Beanbag login response missing required fields for %s",
                EMAIL_PLACEHOLDER,
            )
            raise BeanbagLoginError(
                "Beanbag login response missing required fields"
            ) from error

        token_timestamp = data.get("JTT") if isinstance(data.get("JTT"), int) else None

        gateways_field = data.get("GD")
        gateway_payloads: list[dict[str, Any]] = []

        if gateways_field is None:
            gateways_raw: Iterable[dict[str, Any]] = ()
        elif isinstance(gateways_field, Iterable) and not isinstance(
            gateways_field, (str, bytes)
        ):
            for gateway in gateways_field:
                if isinstance(gateway, dict):
                    gateway_payloads.append(gateway)
                else:
                    _LOGGER.debug(
                        "Ignoring Beanbag gateway entry with unexpected type: %s",
                        type(gateway).__name__,
                    )
            gateways_raw = gateway_payloads
        else:
            _LOGGER.warning(
                "Beanbag login payload contained invalid gateway collection; ignoring",
            )
            gateways_raw = ()

        gateways = tuple(self._parse_gateway(gateway) for gateway in gateways_raw)

        _LOGGER.debug(
            "Beanbag login returned %s gateways for %s",
            len(gateways),
            EMAIL_PLACEHOLDER,
        )
        _LOGGER.info(
            "Beanbag login succeeded with %s and %s",
            USER_ID_PLACEHOLDER,
            TOKEN_PLACEHOLDER,
        )

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
        capabilities = {
            key: raw[key] for key in ("CS", "UR", "HI", "DT", "DN") if key in raw
        }
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

        sanitized_headers = {
            "Authorization": f"Bearer {TOKEN_PLACEHOLDER}",
            "Session-id": SESSION_ID_PLACEHOLDER,
            "Request-id": REQUEST_ID,
        }

        _LOGGER.info("Opening Beanbag WebSocket for session %s", SESSION_ID_PLACEHOLDER)
        _LOGGER.debug(
            "Beanbag WebSocket connect %s with headers=%s",
            self._ws_url,
            sanitized_headers,
        )

        try:
            websocket = await self._session.ws_connect(
                self._ws_url,
                headers=headers,
                protocols=[SUBPROTOCOL],
            )
        except ClientError as error:
            _LOGGER.error(
                "Beanbag WebSocket connection failed for %s: %s",
                SESSION_ID_PLACEHOLDER,
                error,
            )
            raise BeanbagWebSocketError(
                "Beanbag WebSocket connection failed"
            ) from error

        _LOGGER.info(
            "Beanbag WebSocket connected for session %s", SESSION_ID_PLACEHOLDER
        )
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

        _LOGGER.info("BeanbagBackend login invoked for %s", EMAIL_PLACEHOLDER)
        return await self._http.login(email, password_digest)

    async def connect_websocket(
        self, session: BeanbagSession
    ) -> ClientWebSocketResponse:
        """Connect to the Beanbag WebSocket using the login session."""

        _LOGGER.info(
            "BeanbagBackend connect_websocket invoked for session %s",
            SESSION_ID_PLACEHOLDER,
        )
        return await self._ws.connect(session)

    async def login_and_connect(
        self, email: str, password_digest: str
    ) -> tuple[BeanbagSession, ClientWebSocketResponse]:
        """Run the login flow and immediately open the WebSocket."""

        _LOGGER.info("Starting combined Beanbag login and WebSocket handshake")
        session = await self.login(email, password_digest)
        websocket = await self.connect_websocket(session)
        _LOGGER.info("Completed Beanbag login and WebSocket handshake")
        return session, websocket

    async def read_device_metadata(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> dict[str, Any]:
        """Fetch the controller metadata block via the WebSocket."""

        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=17,
            header_si=11,
        )

        if not isinstance(response, dict):
            raise BeanbagWebSocketError(
                "Beanbag metadata payload did not contain an object"
            )

        _LOGGER.debug(
            "Received Beanbag metadata payload keys: %s",
            sorted(response),
        )
        return response

    async def read_zone_topology(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> list[dict[str, Any]]:
        """Retrieve the configured immersion zones for the gateway."""

        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=49,
            header_si=11,
        )

        if not isinstance(response, list):
            raise BeanbagWebSocketError(
                "Beanbag zones payload did not contain a list"
            )

        zones: list[dict[str, Any]] = []
        for entry in response:
            if isinstance(entry, dict):
                zones.append(entry)
            else:
                _LOGGER.debug(
                    "Ignoring unexpected zone entry type %s", type(entry).__name__
                )

        _LOGGER.debug("Beanbag reported %s zones", len(zones))
        return zones

    async def sync_gateway_clock(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
        *,
        timestamp: int | None = None,
    ) -> None:
        """Align the controller clock with the current epoch timestamp."""

        epoch = int(time.time() if timestamp is None else timestamp)
        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=2,
            header_si=103,
            args=[epoch],
        )

        if response not in (0, "0", None):
            raise BeanbagWebSocketError(
                f"Unexpected Beanbag clock acknowledgement: {response}"
            )

        _LOGGER.debug("Beanbag controller clock synchronised to %s", epoch)

    async def read_schedule_overview(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> dict[str, Any]:
        """Fetch the summary of configured boost and heating schedules."""

        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=5,
            header_si=1,
        )

        if not isinstance(response, dict):
            raise BeanbagWebSocketError(
                "Beanbag schedule payload did not contain an object"
            )

        _LOGGER.debug(
            "Received Beanbag schedule overview keys: %s",
            sorted(response),
        )
        return response

    async def read_device_configuration(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> dict[str, Any]:
        """Fetch controller configuration parameters via the WebSocket."""

        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=14,
            header_si=11,
        )

        if not isinstance(response, dict):
            raise BeanbagWebSocketError(
                "Beanbag configuration payload did not contain an object"
            )

        _LOGGER.debug(
            "Received Beanbag configuration payload keys: %s",
            sorted(response),
        )
        return response

    async def read_live_state(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> BeanbagStateSnapshot:
        """Read the live state blocks and derive the primary power flag."""

        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=3,
            header_si=1,
        )

        if not isinstance(response, dict):
            raise BeanbagWebSocketError(
                "Beanbag live state payload did not contain an object"
            )

        primary_power = self._extract_primary_power(response)
        _LOGGER.debug(
            "Beanbag live state reports primary power %s",
            "on" if primary_power else "off" if primary_power is False else "unknown",
        )
        return BeanbagStateSnapshot(payload=response, primary_power_on=primary_power)

    async def turn_controller_on(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> None:
        """Send the WebSocket command to enable the primary immersion."""

        await self._set_primary_mode(session, websocket, gateway_id, value=2)

    async def turn_controller_off(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
    ) -> None:
        """Send the WebSocket command to disable the primary immersion."""

        await self._set_primary_mode(session, websocket, gateway_id, value=0)

    async def _set_primary_mode(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
        *,
        value: int,
    ) -> None:
        """Issue the documented primary mode write command."""

        response = await self._send_request(
            session,
            websocket,
            gateway_id,
            header_hi=2,
            header_si=15,
            args=[1, {"I": 6, "V": value}],
        )

        if response not in (0, "0", None):
            raise BeanbagWebSocketError(
                f"Unexpected Beanbag mode write acknowledgement: {response}"
            )

    @staticmethod
    def _extract_primary_power(state_payload: dict[str, Any]) -> bool | None:
        """Return the primary power boolean from a live state payload."""

        blocks = state_payload.get("V")
        if not isinstance(blocks, list):
            return None

        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("SI") != 33:
                continue

            items = block.get("V")
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("I") != 6:
                    continue

                value = item.get("V")
                if value == 2:
                    return True
                if value == 0:
                    return False

        return None

    async def _send_request(
        self,
        session: BeanbagSession,
        websocket: ClientWebSocketResponse,
        gateway_id: str,
        *,
        header_hi: int,
        header_si: int,
        args: list[Any] | None = None,
    ) -> Any:
        """Send a request frame and await the matching response payload."""

        correlation_id = f"{session.session_id}-{secrets.randbits(31):08x}"
        payload: dict[str, Any] = {
            "V": "1.0",
            "DTS": int(time.time()),
            "I": correlation_id,
            "M": "Request",
        }

        parameters: list[Any] = [{"GMI": gateway_id, "HI": header_hi, "SI": header_si}]
        if args is not None:
            parameters.append(args)

        payload["P"] = parameters

        sanitized_parameters = [{"GMI": gateway_id, "HI": header_hi, "SI": header_si}]
        if args is not None:
            sanitized_parameters.append(args)

        _LOGGER.debug(
            "Beanbag WebSocket send correlation=%s header=%s args=%s",
            correlation_id,
            sanitized_parameters[0],
            sanitized_parameters[1:] if len(sanitized_parameters) > 1 else (),
        )

        await websocket.send_json(payload)

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                _LOGGER.debug("Ignoring non-object WebSocket frame: %s", type(message))
                continue

            message_id = message.get("I")
            if message_id != correlation_id:
                _LOGGER.debug(
                    "Ignoring Beanbag WebSocket frame with correlation %s", message_id
                )
                continue

            if "R" in message:
                _LOGGER.debug("Beanbag WebSocket received reply for %s", correlation_id)
                return message["R"]

            if message.get("M") == "Notify":
                _LOGGER.debug(
                    "Beanbag WebSocket received notify for %s; waiting for reply",
                    correlation_id,
                )
                continue

            raise BeanbagWebSocketError(
                "Beanbag WebSocket response missing result payload"
            )


__all__ = [
    "BeanbagBackend",
    "BeanbagGateway",
    "BeanbagHttpClient",
    "BeanbagLoginError",
    "BeanbagSession",
    "BeanbagStateSnapshot",
    "BeanbagWebSocketClient",
    "BeanbagWebSocketError",
]
