"""amZettel account identity and connection registration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .auth import TokenManager
from .exceptions import (
    ApiError,
    AuthFailed,
    CannotConnect,
    NotFound,
    NotPermitted,
    PremiumRequired,
)

__all__ = [
    "AmZettelApi",
    "ApiError",
    "AuthFailed",
    "CannotConnect",
    "NotFound",
    "NotPermitted",
    "PremiumRequired",
]

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# 403 titles of the integration gate (IntegrationGate / IntegrationContext.Refusal).
TITLE_REVOKED = "integration_revoked"
TITLE_PREMIUM = "integration_premium_required"


class AmZettelApi:
    """One account at one amZettel server."""

    def __init__(self, session: aiohttp.ClientSession, server: str, tokens: TokenManager) -> None:
        self._session = session
        self.server = server.rstrip("/")
        self._tokens = tokens

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        token = await self._tokens.async_get_access_token()
        status, body = await self._send(method, path, token, json)
        if status == 401:
            # A token can expire between our check and the server's; refresh once and retry.
            token = await self._tokens.async_get_access_token(force_refresh=True)
            status, body = await self._send(method, path, token, json)
        return self._result(status, body)

    async def _send(self, method: str, path: str, token: str, json: Any) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with self._session.request(
                method, f"{self.server}{path}", json=json, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status == 204:
                    return resp.status, None
                text = await resp.text()
                body: Any = None
                if text:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = None
                return resp.status, body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err

    @staticmethod
    def _result(status: int, body: Any) -> Any:
        if 200 <= status < 300:
            return body
        title = body.get("title") if isinstance(body, dict) else None
        detail = body.get("detail") if isinstance(body, dict) else None
        code = body.get("code") if isinstance(body, dict) else None
        if status == 401:
            raise AuthFailed(str(code or title or status))
        if status == 403:
            if title == TITLE_REVOKED:
                raise AuthFailed(title)
            if title == TITLE_PREMIUM:
                raise PremiumRequired(status, title, detail, code)
            # Any other 403 (a gate refusal, or the endpoint's own rule such as Share.OwnerOnly)
            # carries the server's title/detail/code; `is_gate` tells them apart.
            raise NotPermitted(status, title, detail, code)
        if status == 404:
            raise NotFound(status, title, detail, code)
        raise ApiError(status, title, detail, code)

    # ── identity / self ────────────────────────────────────────────────────────────────────
    async def async_get_me(self) -> dict[str, Any]:
        """`GET /api/auth/me` → `{ Id, Email, Name, … }`."""
        return await self._request("GET", "/api/auth/me")

    async def async_register_self(self, name: str, kind: str, instance_id: str) -> dict[str, Any]:
        """`POST /api/integrations/self` — name, kind and instance of this system."""
        return await self._request(
            "POST",
            "/api/integrations/self",
            {"Name": name, "Kind": kind, "InstanceId": instance_id},
        )
