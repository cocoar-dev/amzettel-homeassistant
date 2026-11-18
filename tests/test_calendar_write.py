"""Writing calendars: create / update / delete events, whole series only, Premium refusal as a repair."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.calendar import CalendarEntityFeature
from homeassistant.components.calendar.helper import as_local_timezone
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import DOMAIN

from .conftest import SCOPES, SUB, entry_data
from .data import CATEGORY_ID, EVENT_ID, EVENTS_ID, PLAN_ID, TODO_ID
from .fake_server import FakeServer

VIENNA = ZoneInfo("Europe/Vienna")
WRITE_SCOPES = [*SCOPES, "calendar.write"]


async def setup_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    *,
    scopes: list[str] | None = None,
    options: dict | None = None,
) -> tuple[MockConfigEntry, FakeServer]:
    await hass.config.async_set_time_zone("Europe/Vienna")
    scopes = scopes if scopes is not None else WRITE_SCOPES
    server = FakeServer(aioclient_mock, scopes=scopes)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SUB,
        data=entry_data(scopes=scopes),
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, server


def entity_id(hass: HomeAssistant, unique_id: str, domain: str = "calendar") -> str:
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert found, unique_id
    return found


def entity(hass: HomeAssistant, unique_id: str):
    return hass.data["entity_components"]["calendar"].get_entity(entity_id(hass, unique_id))


DENTIST_ID = "cccccccc-0000-0000-0000-0000000000de"


def _posted(server: FakeServer) -> list[dict]:
    """Bodies of the adds (`PUT …/events/{id}`; the id is in the path)."""
    return [body for operation, _, body in server.writes if operation == "add"]


async def test_write_features_only_with_the_scope(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    await setup_entry(hass, aioclient_mock, scopes=SCOPES)
    state = hass.states.get(entity_id(hass, f"{SUB}_{EVENTS_ID}"))
    assert state.attributes.get("supported_features", 0) == 0


async def test_write_features_on_event_calendars_not_on_meal_plans(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    await setup_entry(hass, aioclient_mock)
    events = hass.states.get(entity_id(hass, f"{SUB}_{EVENTS_ID}"))
    plan = hass.states.get(entity_id(hass, f"{SUB}_mealplan_{PLAN_ID}"))
    wanted = (
        CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT
    )
    assert events.attributes["supported_features"] == wanted
    assert plan.attributes.get("supported_features", 0) == 0


async def test_create_timed_event_through_ha(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime):
    _, server = await setup_entry(hass, aioclient_mock)
    await hass.services.async_call(
        "calendar",
        "create_event",
        {
            "entity_id": entity_id(hass, f"{SUB}_{EVENTS_ID}"),
            "summary": " Zahnarzt ",
            "description": "Karte mitnehmen",
            "start_date_time": "2026-11-20 23:30:00",
            "end_date_time": "2026-11-21 00:45:00",
        },
        blocking=True,
    )
    (body,) = _posted(server)
    assert "Id" not in body and "Quantity" not in body  # events have no amount on the wire
    assert body["Name"] == "Zahnarzt"
    assert body["Note"] == "Karte mitnehmen"
    assert body["Due"] == {
        "Date": "2026-11-20",
        "Time": "23:30",
        "Zone": "Europe/Vienna",
        "EndTime": "00:45",
        "EndDate": "2026-11-21",
    }
    assert "CategoryId" not in body


async def test_create_all_day_event_end_is_inclusive_on_the_wire(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    _, server = await setup_entry(hass, aioclient_mock)
    await hass.services.async_call(
        "calendar",
        "create_event",
        {
            "entity_id": entity_id(hass, f"{SUB}_{EVENTS_ID}"),
            "summary": "Urlaub",
            "start_date": "2026-12-24",
            "end_date": "2026-12-27",  # exclusive in HA: 24.–26.
        },
        blocking=True,
    )
    assert _posted(server)[0]["Due"] == {"Date": "2026-12-24", "EndDate": "2026-12-26"}


async def test_create_in_a_category_calendar_carries_the_category(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    options = {"category_calendars": [f"{EVENTS_ID}:{CATEGORY_ID}"], "auto_add_new": True, "known": []}
    _, server = await setup_entry(hass, aioclient_mock, options=options)
    await entity(hass, f"{SUB}_{EVENTS_ID}_cat_{CATEGORY_ID}").async_create_event(
        dtstart=date(2026, 11, 20), dtend=date(2026, 11, 21), summary="Restmüll"
    )
    assert _posted(server)[0]["CategoryId"] == CATEGORY_ID


async def test_create_with_rrule(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime):
    _, server = await setup_entry(hass, aioclient_mock)
    await entity(hass, f"{SUB}_{EVENTS_ID}").async_create_event(
        dtstart=date(2026, 11, 20), dtend=date(2026, 11, 21), summary="Turnen", rrule="FREQ=WEEKLY;BYDAY=FR"
    )
    assert _posted(server)[0]["Recurrence"] == {"Rule": "FREQ=WEEKLY;BYDAY=FR", "RDates": [], "ExDates": []}


async def test_update_keeps_reminder_and_strips_the_category_line(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    _, server = await setup_entry(hass, aioclient_mock)
    server.items(EVENTS_ID).append(
        {
            "Id": DENTIST_ID,
            "Name": "Zahnarzt",
            "Note": "alt",
            "CategoryId": CATEGORY_ID,
            "Due": {"Date": "2026-11-20", "Time": "09:00", "Zone": "Europe/Vienna", "ReminderMinutes": 45},
        }
    )
    server.touch(EVENTS_ID)  # changed by another client: the update reads it and writes with the new ETag
    calendar = entity(hass, f"{SUB}_{EVENTS_ID}")
    await calendar.async_update_event(
        DENTIST_ID,
        {
            "summary": "Zahnarzt Dr. B",
            "dtstart": datetime(2026, 11, 20, 10, 0, tzinfo=VIENNA),
            "dtend": datetime(2026, 11, 20, 11, 0, tzinfo=VIENNA),
            # as HA's dialog hands it back: our rendered description, category line included
            "description": "neu\nKategorie: Müll",
        },
    )
    operation, item_id, body = server.writes[-1]
    assert (operation, item_id) == ("edit", DENTIST_ID)
    # a partial update: only what changed, the Due only in its changed time fields
    assert body == {"Name": "Zahnarzt Dr. B", "Note": "neu", "Due": {"Time": "10:00", "EndTime": "11:00"}}
    # the server keeps what was not named: the reminder, the category, no rule
    assert server.item(EVENTS_ID, DENTIST_ID) == {
        "Id": DENTIST_ID,
        "Name": "Zahnarzt Dr. B",
        "Note": "neu",
        "CategoryId": CATEGORY_ID,
        "Due": {"Date": "2026-11-20", "Time": "10:00", "Zone": "Europe/Vienna", "EndTime": "11:00",
                "ReminderMinutes": 45},
    }


async def test_series_whole_only(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime):
    """The fixture series starts 2026-11-16 (all-day, every two weeks)."""
    _, server = await setup_entry(hass, aioclient_mock)
    calendar = entity(hass, f"{SUB}_{EVENTS_ID}")
    with pytest.raises(ServiceValidationError) as one:
        await calendar.async_delete_event(EVENT_ID, recurrence_id="20261214")
    assert one.value.translation_key == "series_occurrence"
    with pytest.raises(ServiceValidationError) as following:
        await calendar.async_delete_event(EVENT_ID, recurrence_id="20261214", recurrence_range="THISANDFUTURE")
    assert following.value.translation_key == "series_following"
    assert server.writes == []

    # "this and all following" from the FIRST occurrence = the whole series
    await calendar.async_update_event(
        EVENT_ID,
        {
            "summary": "Gelbe Tonne",
            "dtstart": date(2026, 11, 16),
            "dtend": date(2026, 11, 17),
            "rrule": "FREQ=WEEKLY;INTERVAL=2",
            "description": "vor 6 Uhr\nKategorie: Müll",
        },
        recurrence_id="20261116",
        recurrence_range="THISANDFUTURE",
    )
    _, _, body = server.writes[-1]
    # the same rule: not sent, so its additions/exclusions stay
    assert "Recurrence" not in body and "Due" not in body
    assert server.item(EVENTS_ID, EVENT_ID)["Recurrence"]["ExDateLines"] == ["EXDATE;VALUE=DATE:20261130"]
    assert body["Note"] == "vor 6 Uhr"
    await calendar.async_delete_event(EVENT_ID, recurrence_id="20261116", recurrence_range="THISANDFUTURE")
    assert server.writes[-1][:2] == ("remove", EVENT_ID)
    assert server.item(EVENTS_ID, EVENT_ID) is None


@pytest.mark.parametrize("move_hours", [0, 1])
async def test_edit_keeps_the_series_timezone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime, move_hours
):
    _, server = await setup_entry(hass, aioclient_mock)
    new_york = ZoneInfo("America/New_York")
    original_due = {
        "Date": "2026-11-20", "Time": "09:00", "Zone": "America/New_York",
        "EndTime": "10:00", "ReminderMinutes": 45,
    }
    server.items(EVENTS_ID).append({
        "Id": DENTIST_ID, "Name": "Original", "Due": original_due.copy(),
        "Recurrence": {"Rule": "FREQ=WEEKLY;BYDAY=FR"},
    })
    server.touch(EVENTS_ID)
    calendar = entity(hass, f"{SUB}_{EVENTS_ID}")
    # The websocket schema converts the instants into HA's zone, even for a title-only edit.
    event = as_local_timezone("dtstart", "dtend")({
        "summary": "Renamed",
        "dtstart": datetime(2026, 11, 20, 9 + move_hours, tzinfo=new_york),
        "dtend": datetime(2026, 11, 20, 10 + move_hours, tzinfo=new_york),
    })
    await calendar.async_update_event(DENTIST_ID, event)
    body = server.writes[-1][2]
    if not move_hours:
        assert "Due" not in body
    assert "Recurrence" not in body
    expected_due = {**original_due, "Time": f"{9 + move_hours:02}:00", "EndTime": f"{10 + move_hours:02}:00"}
    assert server.item(EVENTS_ID, DENTIST_ID)["Due"] == expected_due
    # US and European DST differ on this date: retaining the series zone is observable.
    events = await calendar.async_get_events(
        hass, datetime(2027, 3, 19, tzinfo=VIENNA), datetime(2027, 3, 20, tzinfo=VIENNA)
    )
    occurrence = next(event for event in events if event.uid == DENTIST_ID)
    assert occurrence.start == datetime(2027, 3, 19, 9 + move_hours, tzinfo=new_york)
    assert occurrence.start.astimezone(VIENNA).hour == 14 + move_hours


async def test_new_rule_drops_old_exclusions(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime):
    _, server = await setup_entry(hass, aioclient_mock)
    await entity(hass, f"{SUB}_{EVENTS_ID}").async_update_event(
        EVENT_ID,
        {"summary": "Gelbe Tonne", "dtstart": date(2026, 11, 16), "dtend": date(2026, 11, 17), "rrule": "FREQ=WEEKLY"},
    )
    assert server.writes[-1][2]["Recurrence"] == {"Rule": "FREQ=WEEKLY", "RDates": [], "ExDates": []}


async def test_mixed_bounds_are_refused(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime):
    await setup_entry(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError) as err:
        await entity(hass, f"{SUB}_{EVENTS_ID}").async_create_event(
            dtstart=date(2026, 11, 20), dtend=datetime(2026, 11, 21, 9, tzinfo=VIENNA), summary="x"
        )
    assert err.value.translation_key == "event_mixed_bounds"


async def test_premium_refusal_raises_a_repair_until_the_next_success(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    entry, server = await setup_entry(hass, aioclient_mock)
    issues = ir.async_get(hass)
    issue = f"{entry.entry_id}_calendar_write"
    server.refuse = "integration_premium_required"
    calendar = entity(hass, f"{SUB}_{EVENTS_ID}")
    with pytest.raises(HomeAssistantError) as err:
        await calendar.async_create_event(dtstart=date(2026, 11, 20), dtend=date(2026, 11, 21), summary="x")
    assert err.value.translation_key == "premium_required"
    found = issues.async_get_issue(DOMAIN, issue)
    assert found is not None and found.translation_key == "write_premium_required_calendar"

    # a list write does not clear the calendar's repair …
    server.refuse = None
    await hass.services.async_call(
        "todo", "add_item", {"entity_id": entity_id(hass, f"{SUB}_{TODO_ID}", "todo"), "item": "Brot"}, blocking=True
    )
    assert issues.async_get_issue(DOMAIN, issue) is not None
    # … a successful calendar write does
    await calendar.async_create_event(dtstart=date(2026, 11, 20), dtend=date(2026, 11, 21), summary="x")
    assert issues.async_get_issue(DOMAIN, issue) is None


async def test_gate_refusal_raises_the_connected_systems_repair(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    entry, server = await setup_entry(hass, aioclient_mock)
    server.refuse = "integration_not_permitted"
    with pytest.raises(HomeAssistantError) as err:
        await entity(hass, f"{SUB}_{EVENTS_ID}").async_delete_event(EVENT_ID)
    assert err.value.translation_key == "not_permitted"
    found = ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_calendar_write")
    assert found is not None and found.translation_key == "write_not_permitted_calendar"


async def test_removing_the_entry_removes_its_repairs(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, no_realtime
):
    entry, server = await setup_entry(hass, aioclient_mock)
    server.refuse = "integration_premium_required"
    with pytest.raises(HomeAssistantError):
        await entity(hass, f"{SUB}_{EVENTS_ID}").async_delete_event(EVENT_ID)
    await hass.config_entries.async_remove(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_calendar_write") is None
