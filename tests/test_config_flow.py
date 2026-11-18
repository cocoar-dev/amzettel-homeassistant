"""Config flow: device flow, already configured, reauth."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.amzettel.api import AmZettelApi
from custom_components.amzettel.auth import AuthEndpoints, DeviceCode, TokenSet
from custom_components.amzettel.config_flow import scopes_for
from custom_components.amzettel.const import (
    CONF_ACCESS_TOKEN,
    CONF_CALENDAR_READ,
    CONF_CALENDAR_WRITE,
    CONF_LISTS_READ,
    CONF_LISTS_WRITE,
    CONF_MEALPLANS_READ,
    CONF_SCOPES,
    CONF_SERVER,
    CONF_TOKEN_ENDPOINT,
    DEFAULT_SERVER,
    DOMAIN,
)
from custom_components.amzettel.exceptions import (
    CannotConnect,
    DeviceAccessDenied,
    DeviceExpired,
)

from .conftest import DEVICE_ENDPOINT, ISSUER, SERVER, SUB, TOKEN_ENDPOINT, make_jwt
from .data import CATEGORY_ID, CHECKLIST_ID, EVENTS_ID, PLAN_ID, SHOPPING_ID, TODO_ID
from .fake_server import FakeServer

ENDPOINTS = AuthEndpoints(ISSUER, DEVICE_ENDPOINT, TOKEN_ENDPOINT)
DEVICE = DeviceCode(
    device_code="dev-1",
    user_code="ABCD-EFGH",
    verification_uri=f"{ISSUER}/device",
    verification_uri_complete=f"{ISSUER}/device?user_code=ABCD-EFGH",
    expires_in=600,
    interval=0,
)

_POLL: "Poll"
# captured before any fixture patches them
REAL_GET_ME = AmZettelApi.async_get_me
REAL_REGISTER_SELF = AmZettelApi.async_register_self

USER_INPUT = {
    CONF_SERVER: SERVER + "/",
    CONF_LISTS_READ: True,
    CONF_LISTS_WRITE: True,
    CONF_CALENDAR_READ: True,
    CONF_CALENDAR_WRITE: False,
    CONF_MEALPLANS_READ: True,
}


def tokens(sub: str = SUB) -> TokenSet:
    return TokenSet(make_jwt(sub), "refresh-new", time.time() + 900)


class Poll:
    """A device-flow poll the test releases: the flow first has to show its progress step."""

    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.return_value: TokenSet = tokens()
        self.side_effect: Exception | None = None

    async def __call__(self, *args, **kwargs) -> TokenSet:
        await self.released.wait()
        if self.side_effect is not None:
            raise self.side_effect
        return self.return_value


@pytest.fixture
def flow_mocks(aioclient_mock):
    """Patch discovery, device flow and the two identity calls of the flow; the selection reads a v1
    snapshot from the fake server (its grant = the scopes of USER_INPUT unless a test changes it)."""
    poll = Poll()
    server = FakeServer(aioclient_mock, scopes=scopes_for(USER_INPUT))
    with (
        patch(
            "custom_components.amzettel.config_flow.async_discover",
            AsyncMock(return_value=ENDPOINTS),
        ) as discover,
        patch(
            "custom_components.amzettel.config_flow.async_request_device_code",
            AsyncMock(return_value=DEVICE),
        ) as request,
        patch("custom_components.amzettel.config_flow.async_poll_device_token", poll),
        patch(
            "custom_components.amzettel.api.AmZettelApi.async_get_me",
            AsyncMock(return_value={"Id": "u1", "Email": "a@example.com", "Name": "Anna"}),
        ) as me,
        patch(
            "custom_components.amzettel.api.AmZettelApi.async_register_self",
            AsyncMock(return_value={}),
        ) as register,
        patch("custom_components.amzettel.async_setup_entry", AsyncMock(return_value=True)) as setup,
    ):
        global _POLL
        _POLL = poll
        yield {
            "discover": discover,
            "request": request,
            "poll": poll,
            "me": me,
            "register": register,
            "setup": setup,
            "server": server,
        }


async def _run_to_progress(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "device"
    assert result["description_placeholders"]["user_code"] == "ABCD-EFGH"
    assert result["description_placeholders"]["url"].endswith("user_code=ABCD-EFGH")
    _POLL.released.set()
    await hass.async_block_till_done()
    # The flow manager follows the progress-done step by itself.
    return await hass.config_entries.flow.async_configure(result["flow_id"])


LIST_IDS = [SHOPPING_ID, CHECKLIST_ID, TODO_ID]
SPLIT = f"{EVENTS_ID}:{CATEGORY_ID}"
ALL_IDS = sorted([*LIST_IDS, EVENTS_ID, PLAN_ID])


async def _select(hass: HomeAssistant, result, user_input: dict | None = None):
    """The last step of the first setup: which objects appear (empty input = the defaults: all)."""
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "select"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input or {})


def test_scopes_for_defaults() -> None:
    assert scopes_for(USER_INPUT) == [
        "openid",
        "offline_access",
        "lists.read",
        "lists.write",
        "calendar.read",
        "mealplans.read",
    ]
    # write implies read
    assert scopes_for({CONF_CALENDAR_WRITE: True}) == [
        "openid",
        "offline_access",
        "calendar.read",
        "calendar.write",
    ]


async def test_success(hass: HomeAssistant, flow_mocks) -> None:
    result = await _select(hass, await _run_to_progress(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "amZettel – a@example.com"
    assert result["data"]["account_label"] == "a@example.com"
    assert "Name" not in result["data"] and "Email" not in result["data"]  # not the whole profile
    assert result["result"].unique_id == SUB
    data = result["data"]
    assert data[CONF_SERVER] == SERVER  # trailing slash stripped
    assert data[CONF_TOKEN_ENDPOINT] == TOKEN_ENDPOINT
    assert data[CONF_SCOPES] == scopes_for(USER_INPUT)
    # the device flow asked for exactly these scopes
    assert flow_mocks["request"].call_args.args[2] == scopes_for(USER_INPUT)
    # self registration: location name, kind, instance id
    name, kind, instance = flow_mocks["register"].call_args.args
    assert kind == "homeassistant"
    assert name == hass.config.location_name
    assert instance


async def test_branded_discovery_is_persisted_separately_from_issuer(hass, flow_mocks):
    base = "https://amzettel.auth.example.test"
    flow_mocks["discover"].return_value = AuthEndpoints(ISSUER, f"{base}/connect/device", f"{base}/connect/token", base)
    result = await _select(hass, await _run_to_progress(hass))
    assert result["data"]["issuer"] == ISSUER
    assert result["data"]["auth_base"] == base
    assert result["data"][CONF_TOKEN_ENDPOINT] == f"{base}/connect/token"


async def test_account_display_uses_me_not_token_profile_claims(hass, flow_mocks):
    flow_mocks["poll"].return_value = TokenSet(
        make_jwt(email="wrong@example.test", name="Device User"),
        "refresh-new",
        time.time() + 900,
    )
    result = await _select(hass, await _run_to_progress(hass))
    assert result["data"]["account_label"] == "a@example.com"
    assert "wrong@example.test" not in result["title"]
    assert "Device User" not in result["title"]


@pytest.mark.parametrize(
    ("error", "reason"),
    [(DeviceAccessDenied(), "access_denied"), (DeviceExpired(), "expired_token")],
)
async def test_device_flow_refused(hass: HomeAssistant, flow_mocks, error, reason) -> None:
    flow_mocks["poll"].side_effect = error
    result = await _run_to_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    flow_mocks["register"].assert_not_called()


async def test_already_configured(hass: HomeAssistant, flow_mocks, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await _run_to_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cannot_connect(hass: HomeAssistant, flow_mocks) -> None:
    flow_mocks["discover"].side_effect = CannotConnect("down")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_no_area(hass: HomeAssistant, flow_mocks) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SERVER: SERVER, **{k: False for k in USER_INPUT if k != CONF_SERVER}},
    )
    assert result["errors"] == {"base": "no_area"}


async def _reauth(hass: HomeAssistant, config_entry):
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    _POLL.released.set()
    await hass.async_block_till_done()
    # The flow manager follows the progress-done step by itself.
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_reauth_same_account(hass: HomeAssistant, flow_mocks, config_entry) -> None:
    config_entry.add_to_hass(hass)
    new = tokens()
    flow_mocks["poll"].return_value = new
    result = await _reauth(hass, config_entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_ACCESS_TOKEN] == new.access_token
    # reauth keeps the entry's server and scopes
    assert flow_mocks["request"].call_args.args[2] == config_entry.data[CONF_SCOPES]


@pytest.mark.parametrize(
    ("old_title", "expected"),
    [
        ("amZettel", "amZettel – new@example.test"),
        ("amZettel – old@example.test", "amZettel – new@example.test"),
        ("Mein Konto", "Mein Konto"),
    ],
)
async def test_reauth_updates_confirmed_label_and_preserves_user_title(
    hass, flow_mocks, config_entry, old_title, expected
):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        title=old_title,
        data={**config_entry.data, "account_label": "old@example.test"},
    )
    original_options = dict(config_entry.options)
    original_unique_id = config_entry.unique_id
    flow_mocks["me"].return_value = {
        "Id": "u1",
        "Email": "new@example.test",
        "Name": "Anna",
    }
    result = await _reauth(hass, config_entry)
    assert result["reason"] == "reauth_successful"
    assert config_entry.title == expected
    assert config_entry.data["account_label"] == "new@example.test"
    assert dict(config_entry.options) == original_options
    assert config_entry.unique_id == original_unique_id


async def test_reauth_wrong_account(hass: HomeAssistant, flow_mocks, config_entry) -> None:
    config_entry.add_to_hass(hass)
    old_token = config_entry.data[CONF_ACCESS_TOKEN]
    flow_mocks["poll"].return_value = tokens(sub="99999999-9999-9999-9999-999999999999")
    result = await _reauth(hass, config_entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_ACCESS_TOKEN] == old_token


# ── review fixes ───────────────────────────────────────────────────────────────────────────


def test_entry_title() -> None:
    from custom_components.amzettel.config_flow import entry_title

    assert entry_title("https://app.amzettel.at/") == "amZettel"
    assert entry_title("app-dev.amzettel.at") == "amZettel (app-dev.amzettel.at)"


@pytest.mark.parametrize("failing", ["me", "register"])
async def test_token_rejected(hass: HomeAssistant, flow_mocks, failing) -> None:
    from custom_components.amzettel.exceptions import AuthFailed

    flow_mocks[failing].side_effect = AuthFailed("401")
    result = await _run_to_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "token_rejected"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_refreshed_tokens_are_the_ones_stored(hass: HomeAssistant, flow_mocks, aioclient_mock) -> None:
    """The /me call may refresh the token; the entry gets the refreshed set, not the polled one."""
    from custom_components.amzettel.const import CONF_REFRESH_TOKEN

    aioclient_mock.clear_requests()
    _POLL.return_value = TokenSet(make_jwt(), "refresh-old", time.time() - 10)  # already expired
    refreshed = make_jwt(fresh=True)
    aioclient_mock.post(
        TOKEN_ENDPOINT,
        json={
            "access_token": refreshed,
            "refresh_token": "refresh-new",
            "expires_in": 900,
        },
    )
    aioclient_mock.get(f"{SERVER}/api/auth/me", json={"Id": "u1"})
    aioclient_mock.post(f"{SERVER}/api/integrations/self", json={})
    FakeServer(aioclient_mock, scopes=scopes_for(USER_INPUT))
    with (
        patch("custom_components.amzettel.api.AmZettelApi.async_get_me", REAL_GET_ME),
        patch(
            "custom_components.amzettel.api.AmZettelApi.async_register_self",
            REAL_REGISTER_SELF,
        ),
    ):
        result = await _select(hass, await _run_to_progress(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCESS_TOKEN] == refreshed
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-new"


# ── selection ──────────────────────────────────────────────────────────────────────────────


async def test_first_setup_selects_everything_by_default(hass: HomeAssistant, flow_mocks) -> None:
    result = await _select(hass, await _run_to_progress(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {
        "lists": LIST_IDS,
        "calendars": [EVENTS_ID],
        "meal_plans": [PLAN_ID],
        "category_calendars": [],  # no category calendar by default
        "auto_add_new": True,
        "known": ALL_IDS,
    }
    # the offer came from one snapshot of the areas the grant may read
    snapshots = [c for c in flow_mocks["server"]._mock.mock_calls if str(c[1]).endswith("/snapshots")]
    assert [c[2] for c in snapshots] == [{"Areas": ["Calendar", "Lists", "MealPlans"]}]


async def test_first_setup_stores_the_choice(hass: HomeAssistant, flow_mocks) -> None:
    progress = await _run_to_progress(hass)
    fields = {str(key): key for key in progress["data_schema"].schema}
    assert set(fields) == {
        "lists",
        "calendars",
        "meal_plans",
        "category_calendars",
        "auto_add_new",
    }
    result = await _select(
        hass,
        progress,
        {
            "lists": [CHECKLIST_ID],
            "calendars": [],
            "meal_plans": [PLAN_ID],
            "category_calendars": [SPLIT],
            "auto_add_new": False,
        },
    )
    assert result["options"] == {
        "lists": [CHECKLIST_ID],
        "calendars": [],
        "meal_plans": [PLAN_ID],
        "category_calendars": [SPLIT],
        "auto_add_new": False,
        "known": ALL_IDS,
    }


async def test_selection_offers_only_readable_kinds(hass: HomeAssistant, flow_mocks) -> None:
    flow_mocks["server"].scopes = [
        "openid",
        "offline_access",
        "lists.read",
    ]  # the grant follows the request
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERVER: SERVER,
            CONF_LISTS_READ: True,
            CONF_LISTS_WRITE: False,
            CONF_CALENDAR_READ: False,
            CONF_CALENDAR_WRITE: False,
            CONF_MEALPLANS_READ: False,
        },
    )
    _POLL.released.set()
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert {str(key) for key in result["data_schema"].schema} == {
        "lists",
        "auto_add_new",
    }
    result = await _select(hass, result)
    assert result["options"] == {
        "lists": LIST_IDS,
        "auto_add_new": True,
        "known": sorted(LIST_IDS),
    }


async def test_selection_tolerates_an_area_the_grant_refuses(hass: HomeAssistant, flow_mocks) -> None:
    """Meal plans were asked for, but the capabilities do not allow reading them: the rest is offered."""
    flow_mocks["server"].scopes = [s for s in scopes_for(USER_INPUT) if s != "mealplans.read"]
    result = await _select(hass, await _run_to_progress(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["lists"] == LIST_IDS and result["options"]["calendars"] == [EVENTS_ID]
    assert result["options"]["meal_plans"] == []


async def test_tokens_refreshed_during_the_selection_read_are_stored(
    hass: HomeAssistant, flow_mocks, aioclient_mock
) -> None:
    from custom_components.amzettel.const import CONF_REFRESH_TOKEN

    _POLL.return_value = TokenSet(make_jwt(), "refresh-old", time.time() - 10)  # /me is mocked: no refresh there
    rotated = make_jwt(rotated=True)
    aioclient_mock.post(
        TOKEN_ENDPOINT,
        json={
            "access_token": rotated,
            "refresh_token": "refresh-new",
            "expires_in": 900,
        },
    )
    result = await _select(hass, await _run_to_progress(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-new"
    assert result["data"][CONF_ACCESS_TOKEN] == rotated


async def test_selection_read_network_error_shows_retry(hass: HomeAssistant, flow_mocks) -> None:
    server = flow_mocks["server"]
    server.capabilities_error = (503, "unavailable")
    result = await _run_to_progress(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "select"
    assert result["errors"] == {"base": "cannot_connect"}
    server.capabilities_error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})  # "try again"
    assert result["type"] is FlowResultType.FORM and not result.get("errors")
    assert "lists" in {str(k) for k in result["data_schema"].schema}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["lists"] == LIST_IDS


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        ((404, "not_found"), "feed_unavailable"),
        ((429, "integration_limit"), "feed_snapshot_limit"),
    ],
)
async def test_selection_read_explains_an_unavailable_v1_api(hass: HomeAssistant, flow_mocks, error, shown) -> None:
    """No v1 on the server (404) or its snapshot limit reached: an explanation, no reauth, no fallback."""
    flow_mocks["server"].capabilities_error = error
    result = await _run_to_progress(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "select"
    assert result["errors"] == {"base": shown}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert flow_mocks["server"].snapshots == 0


async def test_selection_read_auth_failure_aborts(hass: HomeAssistant, flow_mocks) -> None:
    flow_mocks["server"].capabilities_error = (403, "integration_revoked")
    result = await _run_to_progress(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "token_rejected"


# ── the only server ────────────────────────────────────────────────────────────────────────


async def test_form_offers_production_and_no_protocol_choice(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    fields = {str(key): key for key in result["data_schema"].schema}
    assert fields[CONF_SERVER].default() == DEFAULT_SERVER
    assert "feed_v1" not in fields
    assert set(fields) == set(USER_INPUT)


@pytest.mark.parametrize("server", ["https://app-dev.amzettel.at", "https://example.test"])
async def test_other_servers_are_refused_before_sign_in(hass: HomeAssistant, flow_mocks, server) -> None:
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    retry = await hass.config_entries.flow.async_configure(first["flow_id"], {**USER_INPUT, CONF_SERVER: server})
    assert retry["type"] is FlowResultType.FORM
    assert retry["errors"] == {"base": "unsupported_server"}
    flow_mocks["discover"].assert_not_called()
    flow_mocks["request"].assert_not_called()


async def test_reauth_of_an_entry_on_another_server_explains(hass: HomeAssistant, flow_mocks) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import entry_data

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SUB,
        data=entry_data(server="https://app-dev.amzettel.at"),
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_server"
    flow_mocks["discover"].assert_not_called()
    assert entry.data[CONF_ACCESS_TOKEN]  # the entry stays as it was
