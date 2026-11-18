"""Bus events from comparing states: what fires, and that nothing floods (first load, new/visible objects)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.bus import (
    EVENT_EVENT_CHANGED,
    EVENT_EVENT_CREATED,
    EVENT_EVENT_REMOVED,
    EVENT_ITEM_ADDED,
    EVENT_ITEM_COMPLETED,
    EVENT_ITEM_REMOVED,
    EVENT_MEAL_PLANNED,
    diff_states,
)
from custom_components.amzettel.coordinator import AmZettelData
from custom_components.amzettel.selection import Visibility

from .data import (
    CASE_ID,
    CHECKLIST_ID,
    EVENTS_ID,
    MILK_ID,
    PASS_ID,
    PLAN_ID,
    SHOPPING_ID,
    TODO_ID,
    lists,
    plans,
)
from .test_calendar_write import setup_entry

ALL = Visibility(None)


def state() -> AmZettelData:
    return AmZettelData(lists={v["Id"]: v for v in lists()}, meal_plans={p["Id"]: p for p in plans()})


def fired(old: AmZettelData, new: AmZettelData, visibility: Visibility = ALL) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    diff_states(old, new, visibility, lambda kind, data: events.append((kind, data)), "de")
    return events


def test_item_added_completed_removed_on_a_checklist() -> None:
    old, new = state(), state()
    items = new.lists[CHECKLIST_ID]["Items"]
    items[0]["Done"] = True  # Pass
    items.remove(next(i for i in items if i["Id"] == CASE_ID))  # Koffer
    items.append({"Id": "new", "Name": "Sonnencreme", "Quantity": 2, "Unit": "Tuben"})
    kinds = [(k, d["name"]) for k, d in fired(old, new)]
    assert kinds == [(EVENT_ITEM_COMPLETED, "Pass"), (EVENT_ITEM_ADDED, "Sonnencreme"), (EVENT_ITEM_REMOVED, "Koffer")]
    added = next(d for k, d in fired(old, new) if k == EVENT_ITEM_ADDED)
    assert added == {
        "list_id": CHECKLIST_ID,
        "list_name": "Urlaub",
        "item_id": "new",
        "name": "Sonnencreme",
        "quantity": 2,
        "unit": "Tuben",
        "category": None,
        "note": None,
    }


def test_buying_is_completed_with_the_buyer_deleting_is_removed() -> None:
    old, new = state(), state()
    shop = new.lists[SHOPPING_ID]
    milk = next(i for i in shop["Items"] if i["Id"] == MILK_ID)
    shop["Items"] = [i for i in shop["Items"] if i["Id"] not in (MILK_ID, "cccccccc-0000-0000-0000-000000000009")]
    shop["RecentPurchases"].append(
        {
            "CheckId": "chk",
            "ItemId": MILK_ID,
            "Name": "Milch",
            "Quantity": 2,
            "Unit": "L",
            "ByUserId": "anna",
            "CheckedAt": "x",
        }
    )
    events = fired(old, new)
    assert [(k, d["name"]) for k, d in events] == [(EVENT_ITEM_COMPLETED, milk["Name"]), (EVENT_ITEM_REMOVED, "Eier")]
    assert events[0][1]["by_user_id"] == "anna"


def test_calendar_created_changed_removed() -> None:
    old, new = state(), state()
    items = new.lists[EVENTS_ID]["Items"]
    items[0]["Due"] = {"Date": "2026-11-17"}
    items.append(
        {
            "Id": "dentist",
            "Name": "Zahnarzt",
            "Quantity": 1,
            "Due": {"Date": "2026-11-20", "Time": "09:00", "Zone": "Europe/Vienna"},
        }
    )
    events = fired(old, new)
    assert [(k, d["summary"]) for k, d in events] == [
        (EVENT_EVENT_CHANGED, "Gelbe Tonne"),
        (EVENT_EVENT_CREATED, "Zahnarzt"),
    ]
    assert events[0][1]["category"] == "Müll" and events[0][1]["recurring"] is True
    assert events[1][1]["start"] == "2026-11-20T09:00:00+01:00" and events[1][1]["all_day"] is False

    removed = fired(new, state())
    assert [(k, d["event_id"]) for k, d in removed if k == EVENT_EVENT_REMOVED] == [
        ("amzettel_event_removed", "dentist")
    ]


def test_a_photo_alone_is_no_change() -> None:
    old, new = state(), state()
    new.lists[EVENTS_ID]["Items"][0]["PhotoId"] = "p"
    assert fired(old, new) == []


def test_meal_planned() -> None:
    old, new = state(), state()
    new.meal_plans[PLAN_ID]["Entries"].append(
        {
            "Id": "meal-9",
            "Date": "2026-11-18",
            "SlotId": "slot-lunch",
            "Timing": "Lunch",
            "People": 2,
            "Dishes": [{"Name": "Suppe"}],
        }
    )
    ((kind, data),) = fired(old, new)
    assert kind == EVENT_MEAL_PLANNED
    assert (data["date"], data["slot"], data["dishes"]) == ("2026-11-18", "Mittagessen", ["Suppe"])


def test_meal_planned_uses_stored_snapshot_not_template() -> None:
    old, new = state(), state()
    entry = {**new.meal_plans[PLAN_ID]["Entries"][0], "Id": "snapshot",
             "Time": "06:00", "PlannedTime": "19:30", "PlannedZone": "Europe/London"}
    new.meal_plans[PLAN_ID]["Entries"].append(entry)
    ((kind, data),) = fired(old, new)
    assert kind == EVENT_MEAL_PLANNED
    assert (data["time"], data["time_zone"]) == ("19:30", "Europe/London")
    changed = copy.deepcopy(new)
    changed.meal_plans[PLAN_ID]["Zone"] = "Europe/Vienna"
    changed.meal_plans[PLAN_ID]["Slots"][0]["Time"] = "08:00"
    assert fired(new, changed) == []


def test_no_flood_for_new_objects() -> None:
    old = state()
    del old.lists[TODO_ID]
    del old.meal_plans[PLAN_ID]
    assert fired(old, state()) == []  # a list / plan seen for the first time is only the baseline


def test_hidden_objects_fire_nothing() -> None:
    old, new = state(), state()
    new.lists[CHECKLIST_ID]["Items"].append({"Id": "x", "Name": "x", "Quantity": 1})
    new.lists[EVENTS_ID]["Items"].append({"Id": "y", "Name": "y", "Quantity": 1, "Due": {"Date": "2026-11-20"}})
    hidden = Visibility(
        {
            "lists": [SHOPPING_ID, TODO_ID],
            "calendars": [],
            "meal_plans": [],
            "auto_add_new": False,
            "known": [SHOPPING_ID, CHECKLIST_ID, TODO_ID, EVENTS_ID, PLAN_ID],
        }
    )
    assert fired(old, new, hidden) == []


# ── through the coordinator ────────────────────────────────────────────────────────────────


@pytest.fixture
def bus_events(hass: HomeAssistant) -> list[Event]:
    seen: list[Event] = []
    for kind in (
        EVENT_ITEM_ADDED,
        EVENT_ITEM_COMPLETED,
        EVENT_ITEM_REMOVED,
        EVENT_EVENT_CREATED,
        EVENT_EVENT_CHANGED,
        EVENT_EVENT_REMOVED,
        EVENT_MEAL_PLANNED,
    ):
        hass.bus.async_listen(kind, seen.append)
    return seen


async def test_first_load_and_a_resync_do_not_flood(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime, bus_events
) -> None:
    entry, server = await setup_entry(hass, aioclient_mock)
    await hass.async_block_till_done()
    assert bus_events == []  # first load: baseline only
    coordinator = entry.runtime_data.coordinator
    server.items(CHECKLIST_ID).append({"Id": "neu", "Name": "Hut", "Quantity": 1})
    server.touch(CHECKLIST_ID)
    await coordinator.async_resnapshot()  # a resync is a new baseline, never a flood
    await hass.async_block_till_done()
    assert bus_events == []

    # after a disconnect the stream replays the changes from the cursor: only those fire
    server.items(CHECKLIST_ID).append({"Id": "neu2", "Name": "Schal", "Quantity": 1})
    server.touch(CHECKLIST_ID)
    server.items(EVENTS_ID).append({"Id": "ev", "Name": "Arzt", "Due": {"Date": "2026-11-20"}})
    server.touch(EVENTS_ID)
    await coordinator.async_apply_change("Lists", CHECKLIST_ID, "cursor-2")
    await coordinator.async_apply_change("Calendar", EVENTS_ID, "cursor-3")
    await hass.async_block_till_done()
    assert sorted(e.event_type for e in bus_events) == [EVENT_EVENT_CREATED, EVENT_ITEM_ADDED]
    assert next(e for e in bus_events if e.event_type == EVENT_ITEM_ADDED).data["name"] == "Schal"
    assert all(e.data["config_entry_id"] == entry.entry_id for e in bus_events)


async def test_a_change_fires_and_a_new_list_does_not(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime, bus_events
) -> None:
    entry, server = await setup_entry(hass, aioclient_mock)
    coordinator = entry.runtime_data.coordinator
    pass_item = next(i for i in server.items(CHECKLIST_ID) if i["Id"] == PASS_ID)
    pass_item["Done"] = True
    server.touch(CHECKLIST_ID)
    await coordinator.async_apply_change("Lists", CHECKLIST_ID, "cursor-2")
    await hass.async_block_till_done()
    assert [(e.event_type, e.data["name"]) for e in bus_events] == [(EVENT_ITEM_COMPLETED, "Pass")]

    bus_events.clear()
    fresh = copy.deepcopy(server.lists[CHECKLIST_ID])
    fresh.update({"Id": "aaaaaaaa-0000-0000-0000-00000000000f", "Name": "Neu"})
    server.lists[fresh["Id"]] = fresh
    await coordinator.async_apply_change("Lists", fresh["Id"], "cursor-3")  # a new list: baseline, not "added"
    await hass.async_block_till_done()
    assert fresh["Id"] in coordinator.data.lists
    assert bus_events == []
