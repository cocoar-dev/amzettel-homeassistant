"""Which lists, calendars and meal plans appear: the rule, the options flow and its effect."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import CONF_SCOPES, DEFAULT_SERVER, DOMAIN
from custom_components.amzettel.coordinator import AmZettelCoordinator, AmZettelData
from custom_components.amzettel.feed_api import FeedApi, Snapshot
from custom_components.amzettel.selection import (
    KIND_CALENDARS,
    KIND_LISTS,
    Visibility,
    choices,
    kinds_for,
)

from .conftest import SUB, entry_data, make_jwt
from .data import CHECKLIST_ID, EVENTS_ID, PLAN_ID, SHOPPING_ID, TODO_ID, lists, plans
from .fake_server import FakeServer

NEW_LIST_ID = "aaaaaaaa-0000-0000-0000-000000000099"

ALL_IDS = sorted([SHOPPING_ID, CHECKLIST_ID, TODO_ID, EVENTS_ID, PLAN_ID])


# ── the rule ───────────────────────────────────────────────────────────────────────────────


def test_visibility_rule() -> None:
    options = {"lists": ["chosen"], "calendars": [], "auto_add_new": True, "known": ["chosen", "dropped"]}
    on = Visibility(options)
    assert on.is_visible(KIND_LISTS, "chosen")  # chosen
    assert not on.is_visible(KIND_LISTS, "dropped")  # known, not chosen
    assert on.is_visible(KIND_LISTS, "brand-new")  # new + auto on
    off = Visibility({**options, "auto_add_new": False})
    assert not off.is_visible(KIND_LISTS, "brand-new")  # new + auto off
    assert off.is_visible(KIND_LISTS, "chosen")


def test_entry_without_options_shows_everything() -> None:
    legacy = Visibility({})
    assert legacy.is_visible(KIND_LISTS, "anything")
    assert legacy.is_visible(KIND_CALENDARS, "anything")


def test_choices_and_kinds() -> None:
    available = choices(lists(), plans())
    assert set(available["lists"]) == {SHOPPING_ID, CHECKLIST_ID, TODO_ID}
    assert available["calendars"] == {EVENTS_ID: "Familie"}
    assert available["meal_plans"] == {PLAN_ID: "Essen"}
    assert kinds_for(["openid", "lists.read", "mealplans.write"]) == ["lists", "meal_plans"]
    assert kinds_for(["calendar.read"]) == ["calendars"]


# ── options flow ───────────────────────────────────────────────────────────────────────────


async def _setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry) -> None:
    FakeServer(aioclient_mock, scopes=entry.data[CONF_SCOPES])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def test_options_flow_saves_the_choice_and_removes_deselected_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    await _setup(hass, aioclient_mock, config_entry)
    assert _entity(hass, "todo", f"{SUB}_{CHECKLIST_ID}")
    assert _entity(hass, "calendar", f"{SUB}_mealplan_{PLAN_ID}")

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert {str(key) for key in result["data_schema"].schema} == {
        "lists",
        "calendars",
        "meal_plans",
        "category_calendars",
        "auto_add_new",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"lists": [SHOPPING_ID, TODO_ID], "calendars": [EVENTS_ID], "meal_plans": [], "auto_add_new": True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert config_entry.options == {
        "lists": [SHOPPING_ID, TODO_ID],
        "calendars": [EVENTS_ID],
        "meal_plans": [],
        "category_calendars": [],
        "auto_add_new": True,
        "known": ALL_IDS,
    }
    assert config_entry.state is ConfigEntryState.LOADED  # reloaded
    assert _entity(hass, "todo", f"{SUB}_{CHECKLIST_ID}") is None  # deselected → gone from the registry
    assert _entity(hass, "calendar", f"{SUB}_mealplan_{PLAN_ID}") is None
    assert _entity(hass, "todo", f"{SUB}_{SHOPPING_ID}")
    assert _entity(hass, "calendar", f"{SUB}_{EVENTS_ID}")
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_options_flow_offers_only_readable_kinds(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=SUB, data=entry_data(**{CONF_SCOPES: ["openid", "offline_access", "lists.read"]})
    )
    await _setup(hass, aioclient_mock, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert {str(key) for key in result["data_schema"].schema} == {"lists", "auto_add_new"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"lists": [SHOPPING_ID]})
    await hass.async_block_till_done()
    assert entry.options == {
        "lists": [SHOPPING_ID],
        "auto_add_new": True,
        "known": sorted([SHOPPING_ID, CHECKLIST_ID, TODO_ID]),
    }
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_defaults_are_what_is_visible(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    options = {"lists": [SHOPPING_ID], "calendars": [], "meal_plans": [], "auto_add_new": False, "known": ALL_IDS}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(), options=options)
    await _setup(hass, aioclient_mock, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults["lists"] == [SHOPPING_ID]
    assert defaults["calendars"] == []
    assert defaults["auto_add_new"] is False
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_needs_a_loaded_entry(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


@pytest.mark.parametrize(("auto_add", "appears"), [(True, True), (False, False)])
async def test_new_list_after_saving(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime, auto_add, appears
) -> None:
    options = {
        "lists": [SHOPPING_ID],
        "calendars": [EVENTS_ID],
        "meal_plans": [PLAN_ID],
        "auto_add_new": auto_add,
        "known": ALL_IDS,
    }
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(), options=options)
    await _setup(hass, aioclient_mock, entry)
    assert _entity(hass, "todo", f"{SUB}_{CHECKLIST_ID}") is None  # known, not chosen
    coordinator = entry.runtime_data.coordinator
    new = {**lists()[1], "Id": "aaaaaaaa-0000-0000-0000-000000000099", "Name": "Neu"}
    coordinator.async_set_updated_data(
        AmZettelData(lists={**coordinator.data.lists, new["Id"]: new}, meal_plans=coordinator.data.meal_plans)
    )
    await hass.async_block_till_done()
    assert (_entity(hass, "todo", f"{SUB}_{new['Id']}") is not None) is appears
    await hass.config_entries.async_unload(entry.entry_id)


async def test_token_refresh_does_not_reload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """A refresh by the integration itself (TokenManager → entry.data) must not reload."""
    from .conftest import TOKEN_ENDPOINT

    await _setup(hass, aioclient_mock, config_entry)
    coordinator = config_entry.runtime_data.coordinator
    aioclient_mock.post(TOKEN_ENDPOINT, json={"access_token": make_jwt(), "refresh_token": "r2", "expires_in": 900})
    await config_entry.runtime_data.tokens.async_get_access_token(force_refresh=True)
    await hass.async_block_till_done()
    assert config_entry.data["refresh_token"] == "r2"  # stored …
    assert config_entry.runtime_data.coordinator is coordinator  # … without a reload
    await hass.config_entries.async_unload(config_entry.entry_id)


# ── changes ────────────────────────────────────────────────────────────────────────────────


async def test_change_of_a_deselected_list_reads_nothing(hass: HomeAssistant) -> None:
    options = {"lists": [SHOPPING_ID], "calendars": [], "meal_plans": [], "auto_add_new": True, "known": ALL_IDS}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(), options=options)
    entry.add_to_hass(hass)
    views = {v["Id"]: v for v in lists() if v["Type"] != "Events"}
    api = MagicMock(spec=FeedApi, server=DEFAULT_SERVER)
    api.async_snapshot.return_value = Snapshot(("Lists",), {}, views, {}, "cursor-1")
    api.async_object.side_effect = lambda area, ident: {**views[SHOPPING_ID], "Id": ident}
    coordinator = AmZettelCoordinator(hass, entry, api, ["lists.read"])
    await coordinator.async_resnapshot()

    await coordinator.async_apply_change("Lists", CHECKLIST_ID, "cursor-2")  # deselected
    api.async_object.assert_not_awaited()
    assert coordinator.cursor == "cursor-2"  # passed over, not lost

    await coordinator.async_apply_change("Lists", SHOPPING_ID, "cursor-3")  # chosen
    await coordinator.async_apply_change("Lists", NEW_LIST_ID, "cursor-4")  # new, auto-add on
    assert [c.args for c in api.async_object.await_args_list] == [("Lists", SHOPPING_ID), ("Lists", NEW_LIST_ID)]
    assert coordinator.cursor == "cursor-4" and NEW_LIST_ID in coordinator.data.lists


# ── review round 6 ─────────────────────────────────────────────────────────────────────────

from custom_components.amzettel.selection import options_from_input  # noqa: E402

SCOPES_ALL = ["lists.read", "calendar.read", "mealplans.read"]


@pytest.mark.parametrize("auto_add", [True, False])
def test_saving_with_a_refused_area_keeps_its_choice(auto_add) -> None:
    previous = {
        "lists": ["a"],
        "calendars": ["c"],
        "meal_plans": [],
        "category_calendars": ["c:k"],
        "auto_add_new": auto_add,
        "known": ["a", "b", "c", "p"],
    }
    offered = {"lists": {}, "calendars": {"c": "Kal"}, "meal_plans": {"p": "Plan"}, "category_calendars": {}}
    saved = options_from_input(
        {"calendars": ["c"], "meal_plans": ["p"], "auto_add_new": auto_add}, offered, SCOPES_ALL, previous
    )
    assert saved["lists"] == ["a"]  # lists answered 403: the choice survives
    assert saved["category_calendars"] == ["c:k"]  # not offered this time: kept
    assert saved["known"] == ["a", "b", "c", "p"]
    visibility = Visibility(saved)
    assert visibility.is_visible(KIND_LISTS, "a") and not visibility.is_visible(KIND_LISTS, "b")


@pytest.mark.parametrize("auto_add", [True, False])
def test_delete_save_restore_keeps_the_status(auto_add) -> None:
    previous = {"lists": ["a"], "auto_add_new": auto_add, "known": ["a", "b"]}
    # "a" (chosen) and "b" (deselected) are deleted in amZettel, then the user saves
    saved = options_from_input(
        {"lists": ["n"], "auto_add_new": auto_add}, {"lists": {"n": "Neu"}}, ["lists.read"], previous
    )
    assert saved["lists"] == ["a", "n"]
    assert saved["known"] == ["a", "b", "n"]
    restored = Visibility(saved)  # both come back from the trash
    assert restored.is_visible(KIND_LISTS, "a")  # still chosen
    assert not restored.is_visible(KIND_LISTS, "b")  # still deselected, even with auto-add on


async def test_options_flow_saves_what_was_shown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    """A list that appears while the dialog is open stays new (not known), so auto-add shows it."""
    await _setup(hass, aioclient_mock, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    coordinator = config_entry.runtime_data.coordinator
    new = {**lists()[1], "Id": "aaaaaaaa-0000-0000-0000-000000000099", "Name": "Neu"}
    coordinator.async_set_updated_data(
        AmZettelData(lists={**coordinator.data.lists, new["Id"]: new}, meal_plans=coordinator.data.meal_plans)
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"lists": [SHOPPING_ID]})
    await hass.async_block_till_done()
    assert new["Id"] not in config_entry.options["known"]
    assert Visibility(config_entry.options).is_visible(KIND_LISTS, new["Id"])
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_options_flow_aborts_when_the_last_update_failed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    await _setup(hass, aioclient_mock, config_entry)
    config_entry.runtime_data.coordinator.last_update_success = False
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_options_change_and_token_update_reload_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime
) -> None:
    from unittest.mock import patch

    from .conftest import TOKEN_ENDPOINT

    await _setup(hass, aioclient_mock, config_entry)
    aioclient_mock.post(TOKEN_ENDPOINT, json={"access_token": make_jwt(), "refresh_token": "r2", "expires_in": 900})
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        hass.config_entries.async_update_entry(config_entry, options={"lists": [], "auto_add_new": True, "known": []})
        # a token refresh while the reload is pending
        await config_entry.runtime_data.tokens.async_get_access_token(force_refresh=True)
        await hass.async_block_till_done()
    assert reload.await_count == 1
    await hass.config_entries.async_unload(config_entry.entry_id)
