"""Sign-in at modgud: discovery, OAuth device flow and token refresh.

The user only names the amZettel server. `GET {server}/api/auth/config` tells which modgud
issuer that server trusts and its branded AuthBase. AuthBase's OpenID configuration names the
device and token endpoints. The device flow deliberately sends NO `resource=` — modgud answers `invalid_target`
for this client when one is given (checked 2026-11-13).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp

from .const import CLIENT_ID, TOKEN_REFRESH_MARGIN
from .exceptions import (
    AuthFailed,
    CannotConnect,
    DeviceAccessDenied,
    DeviceExpired,
    DeviceFlowError,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_POLL_INTERVAL = 5
MIN_POLL_INTERVAL = 1
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


@dataclass(frozen=True, slots=True)
class AuthEndpoints:
    """Where to sign in."""

    issuer: str
    device_authorization_endpoint: str
    token_endpoint: str
    auth_base: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """The answer of the device authorization endpoint."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class TokenSet:
    """Access + refresh token."""

    access_token: str
    refresh_token: str | None
    expires_at: float


def normalize_server(url: str) -> str:
    """Strip whitespace and a trailing slash; default to https."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"https://{url}"
    return url


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Read the payload of a JWT WITHOUT verifying it (only for `sub`, `oi_au_id`, `exp`).

    The server verifies the token on every request; the client only needs the claims to name the
    account and to know when to refresh.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as err:
        raise AuthFailed("access token is not a JWT") from err
    if not isinstance(data, dict):
        raise AuthFailed("access token payload is not an object")
    return data


def _token_set(body: dict[str, Any], previous_refresh: str | None = None) -> TokenSet:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise DeviceFlowError("token response without access_token")
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = time.time() + float(expires_in)
    else:
        exp = decode_jwt_payload(access).get("exp")
        expires_at = float(exp) if isinstance(exp, (int, float)) else time.time() + 300
    return TokenSet(
        access_token=access,
        refresh_token=body.get("refresh_token") or previous_refresh,
        expires_at=expires_at,
    )


async def _get_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise CannotConnect(f"{url} answered {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise CannotConnect(str(err)) from err
    if not isinstance(data, dict):
        raise CannotConnect(f"{url} did not answer an object")
    return data


def _auth_url(value: Any, name: str, *, allow_query: bool = False) -> str:
    """Accept only absolute HTTPS URLs from the trusted server's auth metadata."""
    if (
        not isinstance(value, str) or not value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise CannotConnect(f"auth metadata without valid {name}")
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and url.hostname
            and url.port != 0
            and not url.username
            and not url.password
            and (allow_query or not url.query)
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise CannotConnect(f"auth metadata without valid {name}")
    return value


async def async_discover(session: aiohttp.ClientSession, server: str) -> AuthEndpoints:
    """Discover branded endpoints without changing the canonical token issuer."""
    config = await _get_json(session, f"{normalize_server(server)}/api/auth/config")
    issuer = _auth_url(config.get("Issuer"), "Issuer").rstrip("/")
    auth_base = _auth_url(config.get("AuthBase", issuer), "AuthBase").rstrip("/")
    oidc = await _get_json(session, f"{auth_base}/.well-known/openid-configuration")
    if _auth_url(oidc.get("issuer"), "issuer").rstrip("/") != issuer:
        raise CannotConnect("OpenID issuer differs from auth/config Issuer")
    device = _auth_url(oidc.get("device_authorization_endpoint"), "device_authorization_endpoint")
    token = _auth_url(oidc.get("token_endpoint"), "token_endpoint")
    return AuthEndpoints(
        issuer=issuer,
        device_authorization_endpoint=device,
        token_endpoint=token,
        auth_base=auth_base,
    )


async def _post_form(session: aiohttp.ClientSession, url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    try:
        async with session.post(url, data=form, timeout=REQUEST_TIMEOUT) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = {}
            return resp.status, body if isinstance(body, dict) else {}
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CannotConnect(str(err)) from err


async def async_request_device_code(
    session: aiohttp.ClientSession, endpoints: AuthEndpoints, scopes: list[str]
) -> DeviceCode:
    """Start the device flow (no `resource=`)."""
    status, body = await _post_form(
        session,
        endpoints.device_authorization_endpoint,
        {"client_id": CLIENT_ID, "scope": " ".join(scopes)},
    )
    if status != 200 or "device_code" not in body:
        raise DeviceFlowError(str(body.get("error") or status))
    try:
        verification_uri = _auth_url(body.get("verification_uri"), "verification_uri", allow_query=True)
        complete = body.get("verification_uri_complete")
        if complete is not None:
            complete = _auth_url(complete, "verification_uri_complete", allow_query=True)
    except CannotConnect as err:
        raise DeviceFlowError("invalid device verification URL") from err
    return DeviceCode(
        device_code=body["device_code"],
        user_code=body.get("user_code", ""),
        verification_uri=verification_uri,
        verification_uri_complete=complete,
        expires_in=int(body.get("expires_in") or 600),
        interval=_interval(body.get("interval")),
    )


def _interval(value: Any) -> int:
    """RFC 8628: missing → 5 s; never poll faster than once a second."""
    try:
        seconds = int(value) if value is not None else DEFAULT_POLL_INTERVAL
    except TypeError, ValueError:
        seconds = DEFAULT_POLL_INTERVAL
    return max(seconds, MIN_POLL_INTERVAL)


async def async_poll_device_token(
    session: aiohttp.ClientSession,
    token_endpoint: str,
    device: DeviceCode,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> TokenSet:
    """Poll until the user confirmed (or declined, or the code expired).

    A network error or a 5xx is not the user's answer: keep polling until the code expires.
    """
    interval = max(device.interval, MIN_POLL_INTERVAL)
    deadline = time.monotonic() + device.expires_in
    while True:
        try:
            status, body = await _post_form(
                session,
                token_endpoint,
                {
                    "grant_type": DEVICE_GRANT,
                    "device_code": device.device_code,
                    "client_id": CLIENT_ID,
                },
            )
        except CannotConnect:
            status, body = 0, {}
        if status == 200 and "access_token" in body:
            return _token_set(body)
        error = body.get("error")
        if status == 0 or status >= 500 or error == "authorization_pending":
            pass
        elif error == "slow_down":
            interval += 5
        elif error == "expired_token":
            raise DeviceExpired
        elif error == "access_denied":
            raise DeviceAccessDenied
        else:
            raise DeviceFlowError(str(error or status))
        if time.monotonic() >= deadline:
            raise DeviceExpired
        await sleep(interval)


async def async_refresh(session: aiohttp.ClientSession, token_endpoint: str, refresh_token: str) -> TokenSet:
    """Exchange the refresh token.

    Only `invalid_grant` / `invalid_client` / `unauthorized_client` mean the credentials are gone
    (→ AuthFailed, reauth). Anything else — another 400, a 5xx, a network error — is transient.
    """
    status, body = await _post_form(
        session,
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )
    if status == 200 and "access_token" in body:
        return _token_set(body, previous_refresh=refresh_token)
    error = body.get("error")
    if error in ("invalid_grant", "invalid_client", "unauthorized_client"):
        raise AuthFailed(str(error))
    raise CannotConnect(f"token refresh answered {status} {error or ''}".strip())


async def async_revoke(session: aiohttp.ClientSession, auth_base: str, refresh_token: str) -> None:
    """Revoke at AuthBase's advertised revocation endpoint (RFC 7009), best effort.

    Every failure is only logged on debug: removing the entry must never fail because of it.
    """
    try:
        oidc = await _get_json(session, f"{auth_base.rstrip('/')}/.well-known/openid-configuration")
        endpoint = oidc.get("revocation_endpoint")
        if not endpoint:
            _LOGGER.debug("auth metadata publishes no revocation endpoint")
            return
        endpoint = _auth_url(endpoint, "revocation_endpoint")
        status, _ = await _post_form(
            session,
            endpoint,
            {
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": CLIENT_ID,
            },
        )
        if status != 200:
            _LOGGER.debug("token revocation answered %s", status)
    except Exception as err:  # noqa: BLE001 — best effort
        _LOGGER.debug("token revocation failed: %s", type(err).__name__)


class TokenStore(Protocol):
    """Where the tokens of one account live — the ONE source the manager reads and writes."""

    @property
    def token_endpoint(self) -> str: ...

    def get(self) -> TokenSet: ...

    def compare_and_set(self, started_with: str | None, tokens: TokenSet) -> bool:
        """Store `tokens` only while the stored refresh token is still `started_with`."""
        ...


class MemoryTokenStore:
    """Tokens held in memory (the config flow, before an entry exists)."""

    def __init__(self, token_endpoint: str, tokens: TokenSet) -> None:
        self._token_endpoint = token_endpoint
        self._tokens = tokens

    @property
    def token_endpoint(self) -> str:
        return self._token_endpoint

    def get(self) -> TokenSet:
        return self._tokens

    def compare_and_set(self, started_with: str | None, tokens: TokenSet) -> bool:
        if self._tokens.refresh_token != started_with:
            return False
        self._tokens = tokens
        return True


class TokenManager:
    """Refreshes the tokens of one account 60 s before they expire; keeps no copy of them.

    Every read goes to the store, so credentials a reauth wrote are used at once, and a cached access
    token of the replaced authorization does not exist. One refresh at a time: under the lock the
    manager re-reads the store and redeems only when nobody else has renewed meanwhile. The exchange
    runs as its own task, so a caller cancelled mid-request (an entry unloading) does not lose a
    refresh token the server already redeemed; the next caller waits for that task instead of
    redeeming the same token again. The answer is stored compare-and-set against the refresh token the
    exchange started with: a reauth that replaced the credentials meanwhile wins.

    Callers wait with `asyncio.wait`, not `asyncio.shield`: a shield whose caller was cancelled logs
    "exception in shielded future" when the exchange fails afterwards. A done callback always reads
    the exchange's exception. `async_wait_idle` lets shutdown and removal wait for a running exchange.
    """

    def __init__(self, session: aiohttp.ClientSession, store: TokenStore) -> None:
        self._session = session
        self._store = store
        self._lock = asyncio.Lock()
        self._exchange: asyncio.Task[None] | None = None

    @property
    def tokens(self) -> TokenSet:
        """The current token set (from the store)."""
        return self._store.get()

    @staticmethod
    def _valid(tokens: TokenSet) -> bool:
        return tokens.expires_at - TOKEN_REFRESH_MARGIN > time.time()

    async def async_get_access_token(self, force_refresh: bool = False) -> str:
        """A valid access token; refreshes when it is about to expire (or when forced)."""
        current = self._store.get()
        if not force_refresh and self._valid(current):
            return current.access_token
        seen = current.access_token
        async with self._lock:
            if self._exchange is not None and not self._exchange.done():
                # Left running by a cancelled caller: its answer is the one to use.
                await asyncio.wait({self._exchange})
            current = self._store.get()
            # Another caller (or a reauth) renewed while we waited.
            if current.access_token != seen or (not force_refresh and self._valid(current)):
                return current.access_token
            if not current.refresh_token:
                raise AuthFailed("no refresh token")
            exchange = asyncio.get_running_loop().create_task(self._async_exchange(current.refresh_token))
            exchange.add_done_callback(_retrieve)
            self._exchange = exchange
            await asyncio.wait({exchange})  # a cancelled caller leaves the exchange running
            exchange.result()
            return self._store.get().access_token

    async def async_wait_idle(self, timeout: float) -> None:
        """Wait (at most `timeout` s) for a running exchange, so its rotated refresh token is stored."""
        exchange = self._exchange
        if exchange is not None and not exchange.done():
            await asyncio.wait({exchange}, timeout=timeout)

    async def _async_exchange(self, refresh_token: str) -> None:
        try:
            tokens = await async_refresh(self._session, self._store.token_endpoint, refresh_token)
        except AuthFailed:
            if self._store.get().refresh_token != refresh_token:
                return  # a reauth replaced (and revoked) this authorization meanwhile: not a failure
            raise
        if self._store.compare_and_set(refresh_token, tokens):
            _LOGGER.debug("access token refreshed")
        else:
            _LOGGER.debug("discarding a token refresh: the credentials were replaced meanwhile")


def _retrieve(task: asyncio.Task[None]) -> None:
    """An exchange whose caller was cancelled may still fail; nobody else reads its exception."""
    if not task.cancelled():
        task.exception()
