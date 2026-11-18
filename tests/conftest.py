"""Shared fixtures."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.amzettel.const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_ISSUER,
    CONF_REFRESH_TOKEN,
    CONF_SCOPES,
    CONF_SERVER,
    CONF_TOKEN_ENDPOINT,
    CONF_USER_ID,
    DOMAIN,
)

SERVER = "https://app.amzettel.at"
ISSUER = "https://auth-test.cocoar.dev"
TOKEN_ENDPOINT = f"{ISSUER}/connect/token"
DEVICE_ENDPOINT = f"{ISSUER}/connect/device"
SUB = "11111111-2222-3333-4444-555555555555"
USER_ID = "99999999-aaaa-bbbb-cccc-000000000001"  # my amZettel user id (`/api/auth/me`)
SCOPES = ["openid", "offline_access", "lists.read", "lists.write", "calendar.read", "mealplans.read"]


@pytest.fixture(autouse=True)
def account_api(request):
    """The authenticated profile used by full coordinator refreshes."""
    if "aioclient_mock" not in request.fixturenames:
        return  # real local HTTP/WebSocket tests must keep their real transport
    aioclient_mock = request.getfixturevalue("aioclient_mock")
    aioclient_mock.get(f"{SERVER}/api/auth/me", json={"Id": USER_ID, "Email": "a@example.com", "Name": "Anna"})


def make_jwt(sub: str = SUB, exp: float | None = None, **claims: Any) -> str:
    """An unsigned JWT with the claims the integration reads."""

    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    payload = {"sub": sub, "exp": exp or time.time() + 3600, "oi_au_id": "auth-1", **claims}
    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/amzettel."""
    return


def entry_data(**overrides: Any) -> dict[str, Any]:
    data = {
        CONF_SERVER: SERVER,
        CONF_ISSUER: ISSUER,
        CONF_TOKEN_ENDPOINT: TOKEN_ENDPOINT,
        CONF_SCOPES: SCOPES,
        CONF_ACCESS_TOKEN: make_jwt(),
        CONF_REFRESH_TOKEN: "refresh-1",
        CONF_EXPIRES_AT: time.time() + 3600,
        CONF_USER_ID: USER_ID,
    }
    data.update(overrides)
    return data


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=SUB, title="amZettel", data=entry_data())


@pytest.fixture
def no_realtime():
    """Keep the SSE stream from connecting in platform tests."""
    with patch("custom_components.amzettel.feed_stream.FeedStream.run", new=AsyncMock(return_value=None)) as run:
        yield run
