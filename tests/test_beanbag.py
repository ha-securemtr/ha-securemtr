"""Tests for the Beanbag backend clients."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr.beanbag import BeanbagHttpClient, BeanbagLoginError


def _mock_response(
    *,
    body: Any | None = None,
    status: int = 200,
    json_side_effect: Exception | None = None,
) -> AsyncMock:
    """Create a mock aiohttp response for tests."""

    response = AsyncMock()
    response.__aenter__.return_value = response
    response.__aexit__.return_value = False
    response.status = status
    if json_side_effect is None:
        response.json = AsyncMock(return_value=body)
    else:
        response.json = AsyncMock(side_effect=json_side_effect)
    return response


@pytest.mark.asyncio
async def test_login_rejects_non_json_response() -> None:
    """Ensure a non-JSON response raises a BeanbagLoginError."""

    session = Mock(spec=ClientSession)
    session.post.return_value = _mock_response(json_side_effect=ValueError("boom"))

    client = BeanbagHttpClient(session)

    with pytest.raises(BeanbagLoginError, match="not valid JSON"):
        await client.login("user@example.com", "0" * 32)


@pytest.mark.asyncio
async def test_login_trims_email_and_filters_gateways() -> None:
    """Verify login trims the email and ignores malformed gateways."""

    body = {
        "RI": "1",
        "D": {
            "SI": "session-1",
            "UI": 42,
            "JT": "token",
            "JTT": 1700000000,
            "GD": [
                {"GMI": "primary", "SN": "123"},
                "skip-me",
                {"GMI": "secondary", "SN": None},
            ],
        },
    }

    session = Mock(spec=ClientSession)
    session.post.return_value = _mock_response(body=body)

    client = BeanbagHttpClient(session)
    result = await client.login(" user@example.com ", "0" * 32)

    assert [gateway.gateway_id for gateway in result.gateways] == [
        "primary",
        "secondary",
    ]

    payload = session.post.call_args.kwargs["json"]
    assert payload["ULC"]["UEI"] == "user@example.com"


@pytest.mark.asyncio
async def test_login_handles_non_iterable_gateways_field() -> None:
    """Ensure non-iterable gateway collections do not break login."""

    body = {
        "RI": "1",
        "D": {
            "SI": "session-2",
            "UI": 7,
            "JT": "token",
            "GD": 123,
        },
    }

    session = Mock(spec=ClientSession)
    session.post.return_value = _mock_response(body=body)

    client = BeanbagHttpClient(session)
    result = await client.login("user@example.com", "f" * 32)

    assert result.gateways == ()
