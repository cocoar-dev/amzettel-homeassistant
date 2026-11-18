"""Discovery, device flow polling, token refresh, API error mapping."""

from __future__ import annotations

import time

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.amzettel.api import AmZettelApi
from custom_components.amzettel.auth import (
    DeviceCode,
    MemoryTokenStore,
    TokenManager,
    TokenSet,
    async_discover,
    async_poll_device_token,
    async_refresh,
    async_request_device_code,
    decode_jwt_payload,
)
from custom_components.amzettel.const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
)
from custom_components.amzettel.exceptions import (
    AuthFailed,
    CannotConnect,
    DeviceAccessDenied,
    DeviceExpired,
    DeviceFlowError,
    NotPermitted,
    PremiumRequired,
)
from custom_components.amzettel.feed_api import FeedApi

from .conftest import (
    DEVICE_ENDPOINT,
    ISSUER,
    SERVER,
    SUB,
    TOKEN_ENDPOINT,
    entry_data,
    make_jwt,
)
from .data import SHOPPING_ID
from .fake_server import FEED, FakeServer


def test_decode_jwt_payload() -> None:
    claims = decode_jwt_payload(make_jwt(oi_au_id="auth-7"))
    assert claims["sub"] == SUB
    assert claims["oi_au_id"] == "auth-7"
    with pytest.raises(AuthFailed):
        decode_jwt_payload("not-a-jwt")


BRANDED_AUTH = "https://amzettel.auth.example.test"


@pytest.mark.parametrize("auth_base", [None, BRANDED_AUTH])
async def test_discovery(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, auth_base) -> None:
    config = {"Issuer": ISSUER + "/"}
    if auth_base:
        config["AuthBase"] = auth_base + "/"
    base = auth_base or ISSUER
    aioclient_mock.get(f"{SERVER}/api/auth/config", json=config)
    aioclient_mock.get(
        f"{base}/.well-known/openid-configuration",
        json={
            "issuer": ISSUER + "/",
            "device_authorization_endpoint": f"{base}/connect/device",
            "token_endpoint": f"{base}/connect/token",
        },
    )
    endpoints = await async_discover(async_get_clientsession(hass), SERVER + "/")
    assert endpoints.issuer == ISSUER
    assert endpoints.auth_base == base
    assert endpoints.device_authorization_endpoint == f"{base}/connect/device"
    assert endpoints.token_endpoint == f"{base}/connect/token"
    # Device authorization, redemption and refresh all use the advertised branded endpoints.
    aioclient_mock.post(
        endpoints.device_authorization_endpoint,
        json={
            "device_code": "d",
            "verification_uri": f"{base}/verify",
            "expires_in": 600,
        },
    )
    session = async_get_clientsession(hass)
    device = await async_request_device_code(session, endpoints, ["openid"])
    assert device.verification_uri == f"{base}/verify"
    aioclient_mock.post(
        endpoints.token_endpoint,
        json={"access_token": "first", "refresh_token": "r1", "expires_in": 600},
    )
    await async_poll_device_token(session, endpoints.token_endpoint, device)
    await async_refresh(session, endpoints.token_endpoint, "r1")
    assert aioclient_mock.mock_calls[-1][2]["grant_type"] == "refresh_token"
    assert all(str(call[1]).startswith(base) for call in aioclient_mock.mock_calls[1:])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Issuer", None),
        ("Issuer", ""),
        ("Issuer", "http://auth.example.test"),
        ("AuthBase", ""),
        ("AuthBase", None),
        ("AuthBase", 42),
        ("AuthBase", "x"),
        ("AuthBase", "//auth.example.test"),
        ("AuthBase", "http://auth.example.test"),
        ("AuthBase", "https://user:pass@auth.example.test"),
        ("AuthBase", "https://auth.example.test?query=1"),
        ("AuthBase", "https://auth.example.test#fragment"),
    ],
)
async def test_invalid_auth_config_is_rejected(hass, aioclient_mock, field, value):
    aioclient_mock.get(
        f"{SERVER}/api/auth/config",
        json={"Issuer": ISSUER, "AuthBase": BRANDED_AUTH, field: value},
    )
    with pytest.raises(CannotConnect):
        await async_discover(async_get_clientsession(hass), SERVER)
    assert len(aioclient_mock.mock_calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", None),
        ("issuer", BRANDED_AUTH),
        ("device_authorization_endpoint", None),
        ("device_authorization_endpoint", {}),
        ("token_endpoint", ""),
        ("token_endpoint", "http://auth.example.test/token"),
    ],
)
async def test_invalid_oidc_is_rejected(hass, aioclient_mock, field, value):
    aioclient_mock.get(f"{SERVER}/api/auth/config", json={"Issuer": ISSUER, "AuthBase": BRANDED_AUTH})
    aioclient_mock.get(
        f"{BRANDED_AUTH}/.well-known/openid-configuration",
        json={
            "issuer": ISSUER,
            "device_authorization_endpoint": f"{BRANDED_AUTH}/device",
            "token_endpoint": f"{BRANDED_AUTH}/token",
            field: value,
        },
    )
    with pytest.raises(CannotConnect):
        await async_discover(async_get_clientsession(hass), SERVER)


async def test_device_request_sends_no_resource(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        DEVICE_ENDPOINT,
        json={
            "device_code": "d",
            "user_code": "U",
            "verification_uri": f"{ISSUER}/verify",
            "verification_uri_complete": f"{ISSUER}/verify?user_code=U",
            "expires_in": 600,
            "interval": 5,
        },
    )
    from custom_components.amzettel.auth import AuthEndpoints

    device = await async_request_device_code(
        async_get_clientsession(hass),
        AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT),
        ["openid", "offline_access", "lists.read"],
    )
    assert device.interval == 5
    form = aioclient_mock.mock_calls[-1][2]
    assert form == {
        "client_id": "amzettel-homeassistant",
        "scope": "openid offline_access lists.read",
    }
    assert "resource" not in form


@pytest.mark.parametrize("complete", [None, f"{BRANDED_AUTH}/verify?user_code=U&lang=en"])
async def test_device_verification_links_allow_https_queries(hass, aioclient_mock, complete):
    from custom_components.amzettel.auth import AuthEndpoints

    uri = f"{BRANDED_AUTH}/verify?lang=en"
    body = {"device_code": "d", "user_code": "U", "verification_uri": uri}
    if complete is not None:
        body["verification_uri_complete"] = complete
    aioclient_mock.post(DEVICE_ENDPOINT, json=body)
    device = await async_request_device_code(
        async_get_clientsession(hass), AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT), ["openid"]
    )
    assert device.verification_uri == uri
    assert device.verification_uri_complete == complete


@pytest.mark.parametrize("field", ["verification_uri", "verification_uri_complete"])
@pytest.mark.parametrize("value", [
    "", 42, {}, [], True, "verify", "//auth.example.test/verify",
    "http://auth.example.test/verify", "javascript:alert(1)", "https:///verify",
    "https://user:pass@auth.example.test/verify", "https://auth.example.test:0/verify",
    "https://auth.example.test:invalid/verify", "https://auth.example.test/verify#fragment",
    " https://auth.example.test/verify", "https://auth.example.test/veri\nfy",
])
async def test_invalid_device_verification_links_are_rejected(hass, aioclient_mock, field, value):
    from custom_components.amzettel.auth import AuthEndpoints

    aioclient_mock.post(DEVICE_ENDPOINT, json={
        "device_code": "d", "user_code": "U",
        "verification_uri": f"{ISSUER}/verify", field: value,
    })
    with pytest.raises(DeviceFlowError):
        await async_request_device_code(
            async_get_clientsession(hass), AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT), ["openid"]
        )


@pytest.mark.parametrize("value", [None, "missing"])
async def test_device_verification_uri_is_required(hass, aioclient_mock, value):
    from custom_components.amzettel.auth import AuthEndpoints

    body = {"device_code": "d", "user_code": "U"}
    if value is None:
        body["verification_uri"] = None
    aioclient_mock.post(DEVICE_ENDPOINT, json=body)
    with pytest.raises(DeviceFlowError):
        await async_request_device_code(
            async_get_clientsession(hass), AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT), ["openid"]
        )


def _device(interval: int = 5) -> DeviceCode:
    return DeviceCode("dev", "USER", "uri", None, 600, interval)


async def test_poll_pending_slow_down_success(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    answers = [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": make_jwt(), "refresh_token": "r", "expires_in": 900}),
    ]

    async def respond(method, url, data):
        status, body = answers.pop(0)
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    aioclient_mock.post(TOKEN_ENDPOINT, side_effect=respond)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    tokens = await async_poll_device_token(async_get_clientsession(hass), TOKEN_ENDPOINT, _device(), sleep)
    assert tokens.refresh_token == "r"
    assert sleeps == [5, 10]  # slow_down adds 5 s
    form = aioclient_mock.mock_calls[-1][2]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert form["device_code"] == "dev"


@pytest.mark.parametrize(
    ("error", "exc"),
    [("expired_token", DeviceExpired), ("access_denied", DeviceAccessDenied)],
)
async def test_poll_failures(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, error, exc) -> None:
    aioclient_mock.post(TOKEN_ENDPOINT, status=400, json={"error": error})

    async def sleep(_: float) -> None:
        return None

    with pytest.raises(exc):
        await async_poll_device_token(async_get_clientsession(hass), TOKEN_ENDPOINT, _device(), sleep)


async def test_refresh_before_expiry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    new_access = make_jwt()
    aioclient_mock.post(TOKEN_ENDPOINT, json={"access_token": new_access, "expires_in": 900})
    store = MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("old", "refresh-1", time.time() + 30))  # inside the margin
    manager = TokenManager(async_get_clientsession(hass), store)
    assert await manager.async_get_access_token() == new_access
    assert store.get().refresh_token == "refresh-1"  # kept when not rotated
    assert aioclient_mock.mock_calls[-1][2] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "amzettel-homeassistant",
    }
    # still valid now → no second refresh
    assert await manager.async_get_access_token() == new_access
    assert aioclient_mock.call_count == 1


async def test_valid_token_is_not_refreshed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    manager = TokenManager(
        async_get_clientsession(hass),
        MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() + 3600)),
    )
    assert await manager.async_get_access_token() == "t"
    assert aioclient_mock.call_count == 0


async def test_invalid_grant_raises_auth_failed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN_ENDPOINT, status=400, json={"error": "invalid_grant"})
    manager = TokenManager(
        async_get_clientsession(hass),
        MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() - 10)),
    )
    with pytest.raises(AuthFailed):
        await manager.async_get_access_token()


async def test_invalid_grant_on_setup_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    aioclient_mock.post(TOKEN_ENDPOINT, status=400, json={"error": "invalid_grant"})
    entry = MockConfigEntry(
        domain="amzettel",
        unique_id=SUB,
        data=entry_data(**{CONF_EXPIRES_AT: time.time() - 10}),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_refreshed_tokens_are_stored(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    new_access = make_jwt()
    aioclient_mock.post(
        TOKEN_ENDPOINT,
        json={"access_token": new_access, "refresh_token": "r2", "expires_in": 900},
    )
    FakeServer(aioclient_mock, [], [])
    entry = MockConfigEntry(
        domain="amzettel",
        unique_id=SUB,
        data=entry_data(**{CONF_EXPIRES_AT: time.time() + 5}),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data[CONF_ACCESS_TOKEN] == new_access
    assert entry.data[CONF_REFRESH_TOKEN] == "r2"
    # the bearer goes in the header, with the API version of the v1 contract
    headers = aioclient_mock.mock_calls[-1][3]
    assert str(aioclient_mock.mock_calls[-1][1]).startswith(FEED)
    assert headers["Authorization"] == f"Bearer {new_access}"
    assert headers["X-AmZettel-Api-Version"] == "1"
    await hass.config_entries.async_unload(entry.entry_id)


# ── API error mapping ──────────────────────────────────────────────────────────────────────


def _tokens(hass: HomeAssistant) -> TokenManager:
    session = async_get_clientsession(hass)
    return TokenManager(
        session,
        MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() + 3600)),
    )


def _api(hass: HomeAssistant) -> FeedApi:
    return FeedApi(async_get_clientsession(hass), SERVER, _tokens(hass))


OBJECT_URL = f"{FEED}/objects/List/{SHOPPING_ID}"


async def _read(hass: HomeAssistant) -> None:
    await _api(hass).async_object("Lists", SHOPPING_ID)


@pytest.mark.parametrize(
    ("title", "exc"),
    [
        ("integration_premium_required", PremiumRequired),
        ("integration_not_permitted", NotPermitted),
        ("integration_scope_missing", NotPermitted),
        ("integration_revoked", AuthFailed),
    ],
)
async def test_api_403_mapping(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, title, exc) -> None:
    aioclient_mock.get(OBJECT_URL, status=403, json={"title": title, "status": 403})
    with pytest.raises(exc):
        await _read(hass)


async def test_api_401_refreshes_once_then_fails(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    fresh = make_jwt()
    aioclient_mock.get(OBJECT_URL, status=401)
    aioclient_mock.post(TOKEN_ENDPOINT, json={"access_token": fresh, "expires_in": 900})
    with pytest.raises(AuthFailed):
        await _read(hass)
    methods = [call[0].lower() for call in aioclient_mock.mock_calls]
    assert methods == ["get", "post", "get"]
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {fresh}"  # retried once, refreshed


async def test_identity_api_401_refreshes_once_then_fails(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The shared identity endpoint (`/api/auth/me`) has the same refresh-once rule."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/auth/me", status=401)
    aioclient_mock.post(TOKEN_ENDPOINT, json={"access_token": make_jwt(), "expires_in": 900})
    session = async_get_clientsession(hass)
    with pytest.raises(AuthFailed):
        await AmZettelApi(session, SERVER, _tokens(hass)).async_get_me()
    assert [call[0].lower() for call in aioclient_mock.mock_calls] == [
        "get",
        "post",
        "get",
    ]


# ── review fixes ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_request"}),
        (400, {}),
        (503, {"error": "temporarily_unavailable"}),
        (500, {}),
    ],
)
async def test_refresh_other_errors_are_transient(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status, body
) -> None:
    from custom_components.amzettel.exceptions import CannotConnect

    aioclient_mock.post(TOKEN_ENDPOINT, status=status, json=body)
    manager = TokenManager(
        async_get_clientsession(hass),
        MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() - 10)),
    )
    with pytest.raises(CannotConnect):
        await manager.async_get_access_token()


@pytest.mark.parametrize("error", ["invalid_grant", "invalid_client", "unauthorized_client"])
async def test_refresh_credential_errors_need_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, error
) -> None:
    aioclient_mock.post(TOKEN_ENDPOINT, status=400, json={"error": error})
    manager = TokenManager(
        async_get_clientsession(hass),
        MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() - 10)),
    )
    with pytest.raises(AuthFailed):
        await manager.async_get_access_token()


async def test_refresh_network_error_is_transient(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    import aiohttp

    from custom_components.amzettel.exceptions import CannotConnect

    aioclient_mock.post(TOKEN_ENDPOINT, exc=aiohttp.ClientError())
    manager = TokenManager(
        async_get_clientsession(hass),
        MemoryTokenStore(TOKEN_ENDPOINT, TokenSet("t", "r", time.time() - 10)),
    )
    with pytest.raises(CannotConnect):
        await manager.async_get_access_token()


async def test_device_interval_minimum_and_default(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    from custom_components.amzettel.auth import AuthEndpoints

    endpoints = AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT)
    session = async_get_clientsession(hass)
    aioclient_mock.post(
        DEVICE_ENDPOINT, json={"device_code": "d", "user_code": "U", "verification_uri": f"{ISSUER}/verify", "interval": 0}
    )
    assert (await async_request_device_code(session, endpoints, ["openid"])).interval == 1
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        DEVICE_ENDPOINT, json={"device_code": "d", "user_code": "U", "verification_uri": f"{ISSUER}/verify"}
    )
    assert (await async_request_device_code(session, endpoints, ["openid"])).interval == 5


async def test_poll_survives_network_errors_and_5xx(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    import aiohttp

    answers: list = [
        aiohttp.ClientError(),
        (502, {}),
        (400, {"error": "authorization_pending"}),
        (200, {"access_token": make_jwt(), "expires_in": 900}),
    ]

    async def respond(method, url, data):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            return AiohttpClientMockResponse(method, url, exc=answer)
        status, body = answer
        return AiohttpClientMockResponse(method, url, status=status, json=body)

    aioclient_mock.post(TOKEN_ENDPOINT, side_effect=respond)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    tokens = await async_poll_device_token(async_get_clientsession(hass), TOKEN_ENDPOINT, _device(interval=0), sleep)
    assert tokens.access_token
    assert sleeps == [1, 1, 1]  # never faster than once a second


@pytest.mark.parametrize("auth_base", [None, BRANDED_AUTH])
async def test_remove_entry_revokes_the_refresh_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, auth_base
) -> None:
    from custom_components.amzettel import async_remove_entry

    if auth_base:
        config_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "auth_base": auth_base})
    base = auth_base or ISSUER
    aioclient_mock.get(
        f"{base}/.well-known/openid-configuration",
        json={"revocation_endpoint": f"{base}/connect/revoke"},
    )
    aioclient_mock.post(f"{base}/connect/revoke", status=200)
    await async_remove_entry(hass, config_entry)
    method, url, form, _ = aioclient_mock.mock_calls[-1]
    assert (method.lower(), str(url)) == ("post", f"{base}/connect/revoke")
    assert form == {
        "token": "refresh-1",
        "token_type_hint": "refresh_token",
        "client_id": "amzettel-homeassistant",
    }


async def test_remove_entry_revocation_failure_is_swallowed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry
) -> None:
    import aiohttp

    from custom_components.amzettel import async_remove_entry

    aioclient_mock.get(f"{ISSUER}/.well-known/openid-configuration", exc=aiohttp.ClientError())
    await async_remove_entry(hass, config_entry)  # no exception


async def test_api_unknown_403_keeps_the_server_reason(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(OBJECT_URL, status=403, json={"code": "account_deletion_pending"})
    with pytest.raises(NotPermitted) as err:
        await _read(hass)
    assert not err.value.is_gate
    assert err.value.server_message == "account_deletion_pending"


def test_the_integration_has_no_legacy_data_path() -> None:
    """REST/SSE v1 is the only data path: no SignalR hub, no legacy list or meal-plan endpoints."""
    from pathlib import Path

    import custom_components.amzettel as integration

    root = Path(integration.__file__).parent
    sources = {path.name: path.read_text(encoding="utf-8") for path in root.glob("*.py")}
    assert "realtime.py" not in sources
    for legacy in ("/hub/live", "/api/lists", "/api/meal-plans"):
        assert [name for name, text in sources.items() if legacy in text] == [], legacy
