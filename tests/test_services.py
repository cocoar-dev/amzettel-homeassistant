"""The actions: add_event / sync_events (idempotent, own events only), add_item, the queries, schemas."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import DOMAIN
from custom_components.amzettel.services import meals_data, window_bounds

from .conftest import SCOPES, SUB
from .data import CATEGORY_ID, EVENT_ID, EVENTS_ID, PLAN_ID, SHOPPING_ID, TODO_ID
from .test_calendar_write import VIENNA, entity_id, setup_entry

pytestmark = pytest.mark.usefixtures("no_realtime")

EVENTS_UID = f"{SUB}_{EVENTS_ID}"


def _add_id(external_id: str) -> str:
    """add_event's id: UUIDv5 of the calendar over the JSON ["add_event", external_id]."""
    return str(uuid.uuid5(uuid.UUID(EVENTS_ID), json.dumps(["add_event", external_id])))


async def call(hass: HomeAssistant, name: str, target: str, data: dict[str, Any], response: bool = True) -> Any:
    result = await hass.services.async_call(
        DOMAIN, name, {"entity_id": target, **data}, blocking=True, return_response=response
    )
    return result[target] if response else None


FOREIGN_ID = "cccccccc-0000-0000-0000-0000000000f0"  # an event HA did not create


def writes(server) -> list[tuple[str, str]]:
    return [(operation, item) for operation, item, _ in server.writes]


# ── add_event ──────────────────────────────────────────────────────────────────────────────


async def test_add_event_with_external_id_is_idempotent(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    data = {"summary": "Gelbe Tonne", "start_date": "2026-11-20", "category": "müll", "external_id": "gelb-1120"}
    first = await call(hass, "add_event", target, data)
    expected_id = _add_id("gelb-1120")
    assert first == {"event_id": expected_id, "result": "created"}
    assert server.item(EVENTS_ID, expected_id)["CategoryId"] == CATEGORY_ID
    assert server.item(EVENTS_ID, expected_id)["Due"] == {"Date": "2026-11-20"}

    second = await call(hass, "add_event", target, data)
    assert second == {"event_id": expected_id, "result": "unchanged"}
    assert writes(server) == [("add", expected_id)]  # nothing on the second call

    moved = await call(hass, "add_event", target, {**data, "start_date": "2026-11-21"})
    assert moved["result"] == "updated"
    assert writes(server)[-1] == ("edit", expected_id)
    assert server.item(EVENTS_ID, expected_id)["Due"] == {"Date": "2026-11-21"}


async def test_add_event_leaves_unnamed_fields_and_the_users_reminder(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    data = {"summary": "Zahnarzt", "start_date_time": "2026-11-20 09:00:00", "external_id": "z"}
    await call(hass, "add_event", target, {**data, "description": "Karte"})
    item = server.item(EVENTS_ID, _add_id("z"))
    # changed in the app: note, category, reminder
    item["Note"] = "Karte und Überweisung"
    item["CategoryId"] = CATEGORY_ID
    item["Due"]["ReminderMinutes"] = 60
    assert (await call(hass, "add_event", target, data))["result"] == "unchanged"
    # the time differs: only the time changes, the reminder stays
    assert (await call(hass, "add_event", target, {**data, "start_date_time": "2026-11-20 10:00:00"}))[
        "result"
    ] == "updated"
    assert item["Note"] == "Karte und Überweisung"
    assert item["CategoryId"] == CATEGORY_ID
    assert item["Due"] == {"ReminderMinutes": 60, "Date": "2026-11-20", "Time": "10:00", "Zone": "Europe/Vienna"}


async def test_add_event_timed_in_another_offset_lands_in_has_zone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    data = {"summary": "Call", "start_date_time": "2026-11-20T08:00:00+00:00", "external_id": "c"}
    await call(hass, "add_event", target, data)
    item = server.item(EVENTS_ID, _add_id("c"))
    assert item["Due"] == {"Date": "2026-11-20", "Time": "09:00", "Zone": "Europe/Vienna"}
    assert (await call(hass, "add_event", target, data))["result"] == "unchanged"


@pytest.mark.parametrize(
    ("start", "end", "zone", "due"),
    [
        ("2026-11-20 10:00:00", "2026-11-20 11:00:00", "Europe/London",
         {"Date": "2026-11-20", "Time": "10:00", "EndTime": "11:00", "Zone": "Europe/London"}),
        ("2026-11-20T10:00:00+01:00", "2026-11-20T11:00:00+01:00", "Europe/London",
         {"Date": "2026-11-20", "Time": "09:00", "EndTime": "10:00", "Zone": "Europe/London"}),
        ("2026-11-20 10:00:00", None, "UTC",
         {"Date": "2026-11-20", "Time": "10:00", "Zone": "UTC"}),
        ("2026-11-20T00:30:00+01:00", "2026-11-20T01:30:00+01:00", "UTC",
         {"Date": "2026-11-19", "Time": "23:30", "Zone": "UTC", "EndDate": "2026-11-20", "EndTime": "00:30"}),
        ("2027-10-31T02:30:00+01:00", None, "Europe/Vienna",
         {"Date": "2027-10-31", "Time": "01:30", "Zone": "UTC"}),
    ],
)
async def test_add_event_explicit_time_zone(hass, aioclient_mock, start, end, zone, due):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    data = {"summary": "Call", "start_date_time": start, "time_zone": zone, "external_id": "zoned"}
    if end is not None:
        data["end_date_time"] = end
    result = await call(hass, "add_event", target, data)
    assert result["result"] == "created"
    assert server.item(EVENTS_ID, result["event_id"])["Due"] == due
    assert (await call(hass, "add_event", target, data))["result"] == "unchanged"
    assert len(server.writes) == 1


async def test_add_event_time_zone_preserves_all_day_dates(hass, aioclient_mock):
    _, server = await setup_entry(hass, aioclient_mock)
    result = await call(hass, "add_event", entity_id(hass, EVENTS_UID), {
        "summary": "Holiday", "start_date": "2026-11-20", "end_date": "2026-11-22", "time_zone": "UTC",
    })
    assert server.item(EVENTS_ID, result["event_id"])["Due"] == {"Date": "2026-11-20", "EndDate": "2026-11-21"}


@pytest.mark.parametrize("zone", ["Mars/Olympus", "+01:00", ""])
async def test_add_event_rejects_invalid_time_zone_before_writing(hass, aioclient_mock, zone):
    _, server = await setup_entry(hass, aioclient_mock)
    with pytest.raises(vol.Invalid):
        await call(hass, "add_event", entity_id(hass, EVENTS_UID), {
            "summary": "Invalid zone", "start_date_time": "2026-11-20 10:00:00", "time_zone": zone,
        })
    assert server.writes == []


async def test_add_event_without_external_id_always_creates(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    data = {"summary": "Ping", "start_date": "2026-11-20"}
    first = await call(hass, "add_event", target, data)
    second = await call(hass, "add_event", target, data)
    assert first["event_id"] != second["event_id"]
    assert writes(server) == [("add", first["event_id"]), ("add", second["event_id"])]


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"start_date": "2026-11-20", "end_date_time": "2026-11-21 10:00:00"}, "event_mixed_bounds"),
        ({"start_date": "2026-11-20", "end_date": "2026-11-20"}, "event_end_before_start"),
        ({"start_date_time": "2026-11-20 10:00:00", "end_date_time": "2026-11-20 09:00:00"}, "event_end_before_start"),
        ({}, "event_start_required"),
        ({"start_date": "2026-11-20", "category": "Sport"}, "unknown_category"),
    ],
)
async def test_add_event_refuses_bad_input(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, data, key):
    _, server = await setup_entry(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "add_event", entity_id(hass, EVENTS_UID), {"summary": "x", **data})
    assert err.value.translation_key == key
    assert server.writes == []


async def test_add_event_start_fields_are_exclusive(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock)
    with pytest.raises(vol.Invalid):
        await call(
            hass,
            "add_event",
            entity_id(hass, EVENTS_UID),
            {"summary": "x", "start_date": "2026-11-20", "start_date_time": "2026-11-20 10:00:00"},
        )


async def test_add_event_on_a_meal_plan_or_without_scope(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "add_event",
            entity_id(hass, f"{SUB}_mealplan_{PLAN_ID}"),
            {"summary": "x", "start_date": "2026-11-20"},
        )
    assert err.value.translation_key == "not_an_event_calendar"


async def test_add_event_without_calendar_write(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock, scopes=SCOPES)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "add_event", entity_id(hass, EVENTS_UID), {"summary": "x", "start_date": "2026-11-20"})
    assert err.value.translation_key == "calendar_write_missing"
    assert server.writes == []


async def test_add_event_premium_refusal(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    from homeassistant.helpers import issue_registry as ir

    entry, server = await setup_entry(hass, aioclient_mock)
    server.refuse = "integration_premium_required"
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "add_event", entity_id(hass, EVENTS_UID), {"summary": "x", "start_date": "2026-11-20"})
    assert err.value.translation_key == "premium_required"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_calendar_write") is not None


async def test_add_event_in_a_category_calendar(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    options = {"category_calendars": [f"{EVENTS_ID}:{CATEGORY_ID}"], "auto_add_new": True, "known": []}
    _, server = await setup_entry(hass, aioclient_mock, options=options)
    target = entity_id(hass, f"{SUB}_{EVENTS_ID}_cat_{CATEGORY_ID}")
    result = await call(hass, "add_event", target, {"summary": "Rest", "start_date": "2026-11-20", "external_id": "r"})
    assert server.item(EVENTS_ID, result["event_id"])["CategoryId"] == CATEGORY_ID
    other = "eeeeeeee-0000-0000-0000-000000000002"
    server.lists[EVENTS_ID]["EventCategories"].append({"Id": other, "Name": "Sport", "Color": "t5"})
    server.touch(EVENTS_ID)
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.coordinator.async_pull_list(EVENTS_ID)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "add_event", target, {"summary": "x", "start_date": "2026-11-20", "category": "Sport"})
    assert err.value.translation_key == "category_calendar_category"


# ── sync_events ────────────────────────────────────────────────────────────────────────────


def _waste(*days: str) -> list[dict[str, Any]]:
    return [
        {"external_id": f"gelb-{day}", "summary": "Gelbe Tonne", "start": day, "end": _next(day), "location": "x"}
        for day in days
    ]


def _next(day: str) -> str:
    return datetime.fromisoformat(day).date().fromordinal(datetime.fromisoformat(day).toordinal() + 1).isoformat()


def _sync_id(external_id: str, source: str = "muell") -> str:
    return str(uuid.uuid5(uuid.UUID(EVENTS_ID), json.dumps(["sync", source, external_id])))


async def test_sync_creates_is_idempotent_and_removes_only_its_own(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    period = {"source": "muell", "start": "2026-11-01", "end": "2026-12-31"}
    # someone else's event with the same title in the period, and another source's event
    server.items(EVENTS_ID).append(
        {"Id": FOREIGN_ID, "Name": "Gelbe Tonne", "Due": {"Date": "2026-11-27"}}
    )
    await call(hass, "sync_events", target, {**period, "source": "bio", "events": _waste("2026-11-27")})

    first = await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20", "2026-12-04")})
    assert first == {"created": 2, "updated": 0, "removed": 0, "unchanged": 0}
    count = len(server.writes)
    second = await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20", "2026-12-04")})
    assert second == {"created": 0, "updated": 0, "removed": 0, "unchanged": 2}
    assert len(server.writes) == count  # twice the same = no change

    # the holiday moves 04.12 → 05.12: a new external id, the old one is removed
    third = await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20", "2026-12-05")})
    assert third == {"created": 1, "updated": 0, "removed": 1, "unchanged": 1}
    assert server.item(EVENTS_ID, _sync_id("gelb-2026-12-04")) is None
    assert server.item(EVENTS_ID, FOREIGN_ID) is not None  # never touched
    assert server.item(EVENTS_ID, _sync_id("gelb-2026-11-27", "bio")) is not None  # other source untouched
    assert server.item(EVENTS_ID, EVENT_ID) is not None
    stored = hass_storage["amzettel.sync_sources"]["data"]
    entry_id = next(iter(stored))
    assert set(stored[entry_id][EVENTS_ID]["muell"]) == {"gelb-2026-11-20", "gelb-2026-12-05"}


async def test_sync_changes_a_differing_event(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    period = {"source": "muell", "start": "2026-11-01", "end": "2026-12-31"}
    await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20")})
    changed = [{**_waste("2026-11-20")[0], "summary": "Gelber Sack", "description": "Feiertag"}]
    result = await call(hass, "sync_events", target, {**period, "events": changed})
    assert result == {"created": 0, "updated": 1, "removed": 0, "unchanged": 0}
    item = server.item(EVENTS_ID, _sync_id("gelb-2026-11-20"))
    assert (item["Name"], item["Note"]) == ("Gelber Sack", "Feiertag")


async def test_sync_removes_only_inside_the_period(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    await call(
        hass,
        "sync_events",
        target,
        {"source": "muell", "start": "2026-11-01", "end": "2027-01-31", "events": _waste("2026-11-20", "2027-01-15")},
    )
    # the next call only covers November: January's event is not this call's business
    result = await call(
        hass,
        "sync_events",
        target,
        {"source": "muell", "start": "2026-11-01", "end": "2026-11-30", "events": _waste("2026-11-21")},
    )
    assert result == {"created": 1, "updated": 0, "removed": 1, "unchanged": 0}
    assert server.item(EVENTS_ID, _sync_id("gelb-2027-01-15")) is not None
    assert server.item(EVENTS_ID, _sync_id("gelb-2026-11-20")) is None


async def test_sync_period_end_date_counts_fully_and_uses_local_midnight(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    timed = [{"external_id": "late", "summary": "Spät", "start": "2026-11-30T23:30:00+01:00"}]
    await call(
        hass, "sync_events", target, {"source": "s", "start": "2026-11-01", "end": "2026-11-30", "events": timed}
    )
    other = [{"external_id": "x", "summary": "x", "start": "2026-11-10"}]
    result = await call(
        hass, "sync_events", target, {"source": "s", "start": "2026-11-01", "end": "2026-11-30", "events": other}
    )
    assert result["removed"] == 1  # 23:30 local on the last day is inside
    assert server.item(EVENTS_ID, _sync_id("late", "s")) is None


async def test_sync_never_removes_an_event_whose_id_is_not_derived(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
):
    """A tampered store entry pointing at someone else's event proves nothing."""
    entry, server = await setup_entry(hass, aioclient_mock)
    server.items(EVENTS_ID).append({"Id": FOREIGN_ID, "Name": "Arzt", "Due": {"Date": "2026-11-27"}})
    from custom_components.amzettel.sources import sync_sources

    store = sync_sources(hass)
    await store.async_load()
    store.owned(entry.entry_id, EVENTS_ID, "muell")["arzt"] = FOREIGN_ID
    await call(
        hass,
        "sync_events",
        entity_id(hass, EVENTS_UID),
        {"source": "muell", "start": "2026-11-01", "end": "2026-12-31", "events": _waste("2026-11-20")},
    )
    assert server.item(EVENTS_ID, FOREIGN_ID) is not None


async def test_sync_refuses_an_empty_list_unless_allowed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    period = {"source": "muell", "start": "2026-11-01", "end": "2026-12-31"}
    await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20")})
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "sync_events", target, {**period, "events": []})
    assert err.value.translation_key == "sync_empty"
    result = await call(hass, "sync_events", target, {**period, "events": [], "allow_empty": True})
    assert result["removed"] == 1


async def test_sync_refuses_duplicate_ids_before_writing(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    events = _waste("2026-11-20") * 2
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "sync_events",
            entity_id(hass, EVENTS_UID),
            {"source": "m", "start": "2026-11-01", "end": "2026-12-31", "events": events},
        )
    assert err.value.translation_key == "sync_duplicate_external_id"
    assert server.writes == []


async def test_sync_bad_event_fails_before_any_write(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    events = [*_waste("2026-11-20"), {"external_id": "b", "summary": "b", "start": "nächsten Montag"}]
    with pytest.raises(ServiceValidationError) as err:
        await call(
            hass,
            "sync_events",
            entity_id(hass, EVENTS_UID),
            {"source": "m", "start": "2026-11-01", "end": "2026-12-31", "events": events},
        )
    assert err.value.translation_key == "invalid_time"
    assert server.writes == []


async def test_removing_the_entry_forgets_its_sources(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_storage
):
    entry, _ = await setup_entry(hass, aioclient_mock)
    await call(
        hass,
        "sync_events",
        entity_id(hass, EVENTS_UID),
        {"source": "m", "start": "2026-11-01", "end": "2026-12-31", "events": _waste("2026-11-20")},
    )
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass_storage["amzettel.sync_sources"]["data"]


# ── add_item ───────────────────────────────────────────────────────────────────────────────


async def test_add_item_with_amount_unit_category_note(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    server.lists[SHOPPING_ID]["ShoppingCategories"] = [{"Id": "k", "Name": "Kühlregal"}]
    server.touch(SHOPPING_ID)
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.coordinator.async_pull_list(SHOPPING_ID)
    target = entity_id(hass, f"{SUB}_{SHOPPING_ID}", "todo")
    result = await call(
        hass,
        "add_item",
        target,
        {"name": " Joghurt ", "quantity": 4, "unit": "Stk", "category": "kühlregal", "note": "natur"},
    )
    ((operation, item_id, body),) = server.writes
    assert (operation, item_id) == ("add", result["item_id"])
    assert body == {
        "Name": "Joghurt",
        "Quantity": 4.0,
        "Unit": "Stk",
        "Category": "Kühlregal",
        "Note": "natur",
    }


@pytest.mark.parametrize("data", [{"name": ""}, {"name": "x", "quantity": 0}, {"name": "x", "quantity": "viel"}])
async def test_add_item_schema(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, data):
    await setup_entry(hass, aioclient_mock)
    with pytest.raises(vol.Invalid):
        await call(hass, "add_item", entity_id(hass, f"{SUB}_{SHOPPING_ID}", "todo"), data)


async def test_add_item_without_lists_write(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock, scopes=["openid", "lists.read", "calendar.read"])
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "add_item", entity_id(hass, f"{SUB}_{SHOPPING_ID}", "todo"), {"name": "x"})
    assert err.value.translation_key == "lists_write_missing"


# ── queries ────────────────────────────────────────────────────────────────────────────────


async def test_get_events_expands_the_series(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock)
    result = await call(hass, "get_events", entity_id(hass, EVENTS_UID), {"start": "2026-11-15", "end": "2026-12-15"})
    events = result["events"]
    # every two weeks from 16.11., 30.11. excluded
    assert [e["start"] for e in events] == ["2026-11-16", "2026-12-14"]
    first = events[0]
    assert first == {
        "uid": EVENT_ID,
        "recurrence_id": "20261116",
        "summary": "Gelbe Tonne",
        "start": "2026-11-16",
        "end": "2026-11-17",
        "all_day": True,
        "note": "vor 6 Uhr",
        "category_id": CATEGORY_ID,
        "category": "Müll",
        "persons": ["Oma"],
        "recurring": True,
    }


async def test_get_events_filters_by_category_and_handles_timed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _, server = await setup_entry(hass, aioclient_mock)
    server.items(EVENTS_ID).append(
        {
            "Id": "cccccccc-0000-0000-0000-0000000000de",
            "Name": "Zahnarzt",
            "Due": {"Date": "2026-11-16", "Time": "09:00", "Zone": "Europe/Vienna", "EndTime": "09:30"},
        }
    )
    server.touch(EVENTS_ID)
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data.coordinator.async_pull_list(EVENTS_ID)
    target = entity_id(hass, EVENTS_UID)
    window = {"start": "2026-11-16", "end": "2026-11-16"}
    all_events = await call(hass, "get_events", target, window)
    assert [e["summary"] for e in all_events["events"]] == ["Gelbe Tonne", "Zahnarzt"]
    timed = all_events["events"][1]
    assert (timed["start"], timed["end"], timed["all_day"]) == (
        "2026-11-16T09:00:00+01:00",
        "2026-11-16T09:30:00+01:00",
        False,
    )
    waste = await call(hass, "get_events", target, {**window, "category": "Müll"})
    assert [e["summary"] for e in waste["events"]] == ["Gelbe Tonne"]


async def test_get_meals(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock)
    result = await call(
        hass, "get_meals", entity_id(hass, f"{SUB}_mealplan_{PLAN_ID}"), {"start": "2026-11-16", "end": "2026-11-17"}
    )
    meals = result["meals"]
    assert [m["id"] for m in meals] == ["meal-1", "meal-2", "meal-3"]
    assert meals[0]["dishes"] == [{"name": "Spaghetti", "recipe_id": None}, {"name": "Salat", "recipe_id": None}]
    assert meals[1]["slot"] == "Jause"
    assert (meals[2]["time"], meals[2]["date"]) == ("18:30", "2026-11-17")


def test_get_meals_snapshot_boundaries_and_stored_zone() -> None:
    from .data import PLAN

    entry = {**PLAN["Entries"][0], "PlannedTime": "00:30", "PlannedZone": "Europe/Vienna",
             "ShowInCalendar": False}
    plan = {**PLAN, "Zone": "Europe/London", "Entries": [entry]}
    # Its date is Nov 16 in Vienna, but its instant belongs to Nov 15 in HA's UTC zone.
    start = datetime.fromisoformat("2026-11-15T23:30:00+00:00")
    finish = datetime.fromisoformat("2026-11-15T23:31:00+00:00")
    (row,) = meals_data(plan, start, finish, ZoneInfo("UTC"), "en")
    assert (row["date"], row["time"], row["time_zone"], row["shown_in_calendar"]) == (
        "2026-11-16", "00:30", "Europe/Vienna", False,
    )
    assert meals_data(plan, start.replace(minute=29), start, ZoneInfo("UTC"), "en") == []
    assert meals_data(plan, finish, finish.replace(minute=32), ZoneInfo("UTC"), "en") == []


async def test_get_meals_on_a_calendar_is_refused(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "get_meals", entity_id(hass, EVENTS_UID), {})
    assert err.value.translation_key == "not_a_meal_plan"


async def test_get_items(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer):
    freezer.move_to("2026-11-13T11:00:00+00:00")
    await setup_entry(hass, aioclient_mock)
    shopping = entity_id(hass, f"{SUB}_{SHOPPING_ID}", "todo")
    open_items = await call(hass, "get_items", shopping, {})
    assert [i["name"] for i in open_items["items"]] == ["Milch", "Eier"]
    assert open_items["items"][0]["quantity"] == 2 and open_items["items"][0]["unit"] == "L"
    with_done = await call(hass, "get_items", shopping, {"include_completed": True})
    assert [i["name"] for i in with_done["items"]] == ["Milch", "Eier", "Brot"]  # Butter: before the cart close
    todo = await call(hass, "get_items", entity_id(hass, f"{SUB}_{TODO_ID}", "todo"), {})
    assert todo["items"][0]["due"] == "2026-11-20T10:00:00+01:00"


async def test_get_events_needs_a_response(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    await setup_entry(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "get_events", {"entity_id": entity_id(hass, EVENTS_UID)}, blocking=True)


def test_window_bounds_dates_times_and_dst() -> None:
    now = datetime(2026, 10, 24, 12, tzinfo=VIENNA)
    start, end = window_bounds("2026-10-25", "2026-10-25", now, VIENNA)
    utc = ZoneInfo("UTC")
    # the day the clocks go back has 25 hours (aware datetimes of ONE zone subtract wall clocks: use UTC)
    assert (end.astimezone(utc) - start.astimezone(utc)).total_seconds() == 25 * 3600
    start, end = window_bounds(None, None, now, VIENNA)
    assert (start, (end - start).days) == (now, 7)
    with pytest.raises(ServiceValidationError):
        window_bounds("2026-10-26", "2026-10-25", now, VIENNA)
