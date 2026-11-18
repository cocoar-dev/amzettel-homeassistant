"""Owner in the name of shared objects; the shared expansion cache (DST keys, pruning)."""

from __future__ import annotations

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.calendar import ListExpansions
from custom_components.amzettel.const import CONF_USER_ID, DOMAIN
from custom_components.amzettel.coordinator import AmZettelData
from custom_components.amzettel.naming import display_name
from custom_components.amzettel.selection import choices

from .conftest import SERVER, SUB, USER_ID, entry_data
from .data import CATEGORY_ID, EVENTS, PLAN, SHOPPING
from .fake_server import FakeServer

VIENNA = ZoneInfo("Europe/Vienna")
OTHER = "99999999-aaaa-bbbb-cccc-000000000002"

# v1 says per object whether I own it (`IsOwner`) and, when not, whose it is (`OwnerName`).
MINE = {**copy.deepcopy(SHOPPING), "IsOwner": True}
SHARED_NAMED = {
    **copy.deepcopy(SHOPPING),
    "Id": "aaaaaaaa-0000-0000-0000-0000000000a1",
    "IsOwner": False,
    "OwnerName": "Anna",
}
SHARED_UNNAMED = {**copy.deepcopy(SHOPPING), "Id": "aaaaaaaa-0000-0000-0000-0000000000a2", "IsOwner": False}
SHARED_CALENDAR = {**copy.deepcopy(EVENTS), "IsOwner": False, "OwnerName": "Anna"}
FOREIGN_PLAN = {**copy.deepcopy(PLAN), "IsOwner": False}


# ── naming rule ────────────────────────────────────────────────────────────────────────────


def test_display_name_rule() -> None:
    assert display_name("Einkauf", MINE, USER_ID, "de") == "Einkauf"
    assert display_name("Einkauf", SHARED_NAMED, USER_ID, "de") == "Einkauf (von Anna)"
    assert display_name("Einkauf", SHARED_NAMED, USER_ID, "en") == "Einkauf (from Anna)"
    assert display_name("Einkauf", SHARED_UNNAMED, USER_ID, "de") == "Einkauf (geteilt)"
    assert display_name("Einkauf", SHARED_UNNAMED, USER_ID, "en") == "Einkauf (shared)"
    # IsOwner is authoritative: my own id (from /me) is not needed
    assert display_name("Einkauf", SHARED_UNNAMED, None, "de") == "Einkauf (geteilt)"
    assert display_name("Einkauf", MINE, None, "de") == "Einkauf"
    assert display_name("Essen", FOREIGN_PLAN, USER_ID, "de") == "Essen (geteilt)"
    assert display_name("Essen", PLAN, USER_ID, "de") == "Essen"
    # OwnerName names the owner without /me or member-management access.
    assert display_name("Einkauf", SHARED_NAMED, None, "de") == "Einkauf (von Anna)"
    assert display_name("Familie", {**SHARED_CALENDAR, "OwnerName": "owner@example.test"}, None, "en") == (
        "Familie (from owner@example.test)"
    )
    assert display_name("Essen", {**FOREIGN_PLAN, "OwnerName": "Anna"}, None, "de") == "Essen (von Anna)"
    assert display_name("Essen", {**FOREIGN_PLAN, "OwnerName": None}, USER_ID, "de") == "Essen (geteilt)"


def test_selection_labels_name_the_owner() -> None:
    offered = choices([MINE, SHARED_NAMED, SHARED_UNNAMED, SHARED_CALENDAR], [FOREIGN_PLAN], None, USER_ID, "de")
    assert offered["lists"] == {
        MINE["Id"]: "Einkauf",
        SHARED_NAMED["Id"]: "Einkauf (von Anna)",
        SHARED_UNNAMED["Id"]: "Einkauf (geteilt)",
    }
    assert offered["calendars"] == {SHARED_CALENDAR["Id"]: "Familie (von Anna)"}
    assert offered["category_calendars"] == {f"{SHARED_CALENDAR['Id']}:{CATEGORY_ID}": "Familie · Müll (von Anna)"}
    assert offered["meal_plans"] == {PLAN["Id"]: "Essen (geteilt)"}


async def _setup(hass, aioclient_mock, lists, plans, **data) -> MockConfigEntry:
    FakeServer(aioclient_mock, lists, plans)
    options = {
        "lists": [v["Id"] for v in lists if v.get("Type") != "Events"],
        "calendars": [v["Id"] for v in lists if v.get("Type") == "Events"],
        "meal_plans": [p["Id"] for p in plans],
        "category_calendars": [f"{v['Id']}:{CATEGORY_ID}" for v in lists if v.get("Type") == "Events"],
        "auto_add_new": True,
        "known": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, unique_id=SUB, data=entry_data(**data), options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _state_name(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    return hass.states.get(entity_id).attributes["friendly_name"]


async def test_entity_names_name_the_owner_and_ids_stay(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    entry = await _setup(hass, aioclient_mock, [MINE, SHARED_NAMED, SHARED_UNNAMED, SHARED_CALENDAR], [FOREIGN_PLAN])
    assert _state_name(hass, "todo", f"{SUB}_{MINE['Id']}") == "Einkauf"
    assert _state_name(hass, "todo", f"{SUB}_{SHARED_NAMED['Id']}") == "Einkauf (from Anna)"
    assert _state_name(hass, "todo", f"{SUB}_{SHARED_UNNAMED['Id']}") == "Einkauf (shared)"
    assert _state_name(hass, "calendar", f"{SUB}_{SHARED_CALENDAR['Id']}") == "Familie (from Anna)"
    assert (
        _state_name(hass, "calendar", f"{SUB}_{SHARED_CALENDAR['Id']}_cat_{CATEGORY_ID}")
        == "Familie · Müll (from Anna)"
    )
    assert _state_name(hass, "calendar", f"{SUB}_mealplan_{PLAN['Id']}") == "Essen (shared)"

    # the backend starts sending OwnerName: only the display name changes, not the entity id
    registry = er.async_get(hass)
    unique = f"{SUB}_{SHARED_UNNAMED['Id']}"
    entity_id = registry.async_get_entity_id("todo", DOMAIN, unique)
    coordinator = entry.runtime_data.coordinator
    named = {**coordinator.data.lists[SHARED_UNNAMED["Id"]], "OwnerName": "Ben"}
    coordinator.async_set_updated_data(
        AmZettelData(
            lists={**coordinator.data.lists, SHARED_UNNAMED["Id"]: named}, meal_plans=coordinator.data.meal_plans
        )
    )
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("todo", DOMAIN, unique) == entity_id
    assert hass.states.get(entity_id).attributes["friendly_name"] == "Einkauf (from Ben)"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_own_id_is_learned_from_me_for_older_entries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{SERVER}/api/auth/me", json={"Id": USER_ID, "Name": "Ich"})
    entry = await _setup(hass, aioclient_mock, [SHARED_UNNAMED], [], **{CONF_USER_ID: None})
    assert entry.runtime_data.coordinator.user_id == USER_ID
    assert _state_name(hass, "todo", f"{SUB}_{SHARED_UNNAMED['Id']}") == "Einkauf (shared)"
    await hass.config_entries.async_unload(entry.entry_id)


# ── shared expansion cache ─────────────────────────────────────────────────────────────────


async def test_cache_keys_are_instants_across_the_dst_fold(hass: HomeAssistant) -> None:
    """02:00–02:15 CEST and 02:00–02:15 CET are different windows, though the wall clock is equal."""
    view = {
        "Id": "cal",
        "EventCategories": [],
        "Items": [
            {
                "Id": "e",
                "Name": "Früh",
                "Due": {"Date": "2026-10-25", "Time": "02:05", "Zone": "Europe/Vienna", "EndTime": "02:10"},
            }
        ],
    }
    expansions = ListExpansions(hass)
    cest = (datetime(2026, 10, 25, 2, 0, tzinfo=VIENNA), datetime(2026, 10, 25, 2, 15, tzinfo=VIENNA))
    cet = (datetime(2026, 10, 25, 2, 0, fold=1, tzinfo=VIENNA), datetime(2026, 10, 25, 2, 15, fold=1, tzinfo=VIENNA))
    first = await expansions.async_get(view, *cest, VIENNA, "de")
    second = await expansions.async_get(view, *cet, VIENNA, "de")
    assert [e.summary for _, e in first] == ["Früh"]
    assert second == []


async def test_cache_forgets_lists_that_are_gone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
) -> None:
    gone = {**copy.deepcopy(EVENTS), "Id": "aaaaaaaa-0000-0000-0000-0000000000c1", "Name": "Weg"}
    stays = {**copy.deepcopy(EVENTS), "Id": "aaaaaaaa-0000-0000-0000-0000000000c2", "Name": "Bleibt"}
    entry = await _setup(hass, aioclient_mock, [gone, stays], [])
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("calendar", DOMAIN, f"{SUB}_{stays['Id']}")
    expansions = hass.data["entity_components"]["calendar"].get_entity(entity_id)._expansions
    assert {key[0] for key in expansions._jobs} == {gone["Id"], stays["Id"]}

    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_updated_data(
        AmZettelData(lists={stays["Id"]: coordinator.data.lists[stays["Id"]]}, meal_plans={})
    )
    await hass.async_block_till_done()
    assert {key[0] for key in expansions._jobs} == {stays["Id"]}
    await hass.config_entries.async_unload(entry.entry_id)
