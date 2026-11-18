"""Writes decide on the CURRENT server state, keep series rules, use unambiguous ids and times."""

from __future__ import annotations

from datetime import UTC as DATETIME_UTC
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.const import DOMAIN
from custom_components.amzettel.event_write import due_from_bounds, event_item_id
from custom_components.amzettel.recurrence import resolve_local

from .conftest import SUB
from .data import EVENT_ID, EVENTS_ID, SHOPPING_ID
from .test_calendar_write import VIENNA, entity, entity_id, setup_entry
from .test_services import _sync_id, _waste, call

pytestmark = pytest.mark.usefixtures("no_realtime")
UTC = ZoneInfo("UTC")
EVENTS_UID = f"{SUB}_{EVENTS_ID}"


@pytest.mark.parametrize("start", [
    datetime(2026, 11, 24, 10, 0),
    datetime(2026, 11, 24, 10, 0, tzinfo=DATETIME_UTC),
])
def test_writes_accept_home_assistants_default_utc(start, monkeypatch):
    """An untouched HA installation uses datetime.UTC rather than ZoneInfo."""
    from homeassistant.util import dt as dt_util

    from custom_components.amzettel.todo import due_from_ha

    expected = {"Date": "2026-11-24", "Time": "10:00", "Zone": "UTC"}
    assert due_from_bounds(start, None, DATETIME_UTC) == expected
    with monkeypatch.context() as patch:
        patch.setattr(dt_util, "get_default_time_zone", lambda: DATETIME_UTC)
        assert due_from_ha(start, None) == expected


# ── 1. a series keeps its rule ─────────────────────────────────────────────────────────────


async def test_renaming_a_series_keeps_its_rule_and_exclusions(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An occurrence read back carries no rrule; writing the whole series without one keeps the rule."""
    _, server = await setup_entry(hass, aioclient_mock)
    await entity(hass, EVENTS_UID).async_update_event(
        EVENT_ID,
        {"summary": "Gelber Sack", "dtstart": date(2026, 11, 16), "dtend": date(2026, 11, 17)},
        recurrence_id="20261116",
        recurrence_range="THISANDFUTURE",
    )
    operation, _, body = server.writes[-1]
    assert operation == "edit" and body["Name"] == "Gelber Sack"
    assert "Recurrence" not in body  # not named in the partial update: the server keeps it
    assert server.item(EVENTS_ID, EVENT_ID)["Recurrence"] == {
        "Rule": "RRULE:FREQ=WEEKLY;INTERVAL=2", "RDateLines": None, "ExDateLines": ["EXDATE;VALUE=DATE:20261130"],
    }


async def test_a_change_builds_on_the_current_server_item(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    """Changed in the app meanwhile (persons), HA not told yet: the write keeps the server's value."""
    _, server = await setup_entry(hass, aioclient_mock)
    server.item(EVENTS_ID, EVENT_ID)["PersonIds"] = []
    server.touch(EVENTS_ID)
    await entity(hass, EVENTS_UID).async_update_event(
        EVENT_ID, {"summary": "Gelbe Tonne", "dtstart": date(2026, 11, 16), "dtend": date(2026, 11, 17)}
    )
    assert "PersonIds" not in server.writes[-1][2]
    assert server.item(EVENTS_ID, EVENT_ID)["PersonIds"] == []
    assert server.requests[-1][2]["If-Match"] == '"server-1"'  # written on the version it read


# ── 2. no write on a stale state ───────────────────────────────────────────────────────────


async def test_sync_does_not_write_when_the_pull_fails(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    """The own event was moved out of the period in the app; HA's state is stale and the pull fails."""
    _, server = await setup_entry(hass, aioclient_mock)
    target = entity_id(hass, EVENTS_UID)
    period = {"source": "muell", "start": "2026-11-01", "end": "2026-11-30"}
    await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20", "2026-11-27")})
    server.item(EVENTS_ID, _sync_id("gelb-2026-11-27"))["Due"] = {"Date": "2026-12-04"}  # moved in the app
    server.touch(EVENTS_ID)
    server.fail_reads = True
    writes = len(server.writes)
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20")})
    assert err.value.translation_key == "refresh_failed"
    assert len(server.writes) == writes  # nothing written, nothing removed

    server.fail_reads = False  # the current state says: not in the period any more → kept
    result = await call(hass, "sync_events", target, {**period, "events": _waste("2026-11-20")})
    assert result["removed"] == 0
    assert server.item(EVENTS_ID, _sync_id("gelb-2026-11-27")) is not None


async def test_add_event_does_not_write_when_the_pull_fails(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    _, server = await setup_entry(hass, aioclient_mock)
    server.fail_reads = True
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "add_event", entity_id(hass, EVENTS_UID), {"summary": "x", "start_date": "2026-11-20"})
    assert err.value.translation_key == "refresh_failed"
    assert server.writes == []


async def test_calendar_update_does_not_write_when_the_pull_fails(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
):
    _, server = await setup_entry(hass, aioclient_mock)
    server.fail_reads = True
    with pytest.raises(HomeAssistantError):
        await entity(hass, EVENTS_UID).async_update_event(
            EVENT_ID, {"summary": "x", "dtstart": date(2026, 11, 16), "dtend": date(2026, 11, 17)}
        )
    with pytest.raises(HomeAssistantError):
        await entity(hass, EVENTS_UID).async_delete_event(EVENT_ID)
    assert server.writes == []


# ── 3. unambiguous ids ─────────────────────────────────────────────────────────────────────


def test_ids_of_sources_and_add_event_never_collide() -> None:
    assert event_item_id(EVENTS_ID, "district:42", "waste") != event_item_id(EVENTS_ID, "42", "waste:district")
    assert event_item_id(EVENTS_ID, "waste:42") != event_item_id(EVENTS_ID, "42", "waste")
    assert event_item_id(EVENTS_ID, "42") == event_item_id(EVENTS_ID, "42")  # stable


# ── 4. the repeated and the skipped hour ───────────────────────────────────────────────────


def _backend_instant(due: dict) -> datetime:
    """How the backend reads a Due (Due.ToInstant: Date.At(Time).InZoneLeniently(Zone))."""
    naive = datetime.combine(date.fromisoformat(due["Date"]), datetime.strptime(due["Time"], "%H:%M").time())
    return resolve_local(naive, ZoneInfo(due["Zone"])).astimezone(UTC)


@pytest.mark.parametrize(
    ("value", "due"),
    [
        # the repeated hour on 2027-10-31: both halves go to UTC (the Due has no offset)
        ("2027-10-31T02:30:00+02:00", {"Date": "2027-10-31", "Time": "00:30", "Zone": "UTC"}),
        ("2027-10-31T02:30:00+01:00", {"Date": "2027-10-31", "Time": "01:30", "Zone": "UTC"}),
        # the skipped hour on 2027-03-28: 02:30 does not exist, its instant is 03:30 local
        ("2027-03-28T02:30:00+01:00", {"Date": "2027-03-28", "Time": "03:30", "Zone": "Europe/Vienna"}),
        # an ordinary time stays local
        ("2027-10-31T04:00:00+01:00", {"Date": "2027-10-31", "Time": "04:00", "Zone": "Europe/Vienna"}),
    ],
)
def test_times_around_the_clock_change(value: str, due: dict) -> None:
    moment = datetime.fromisoformat(value)
    wire = due_from_bounds(moment, None, VIENNA)
    assert wire == due
    assert _backend_instant(wire) == moment.astimezone(UTC)  # the backend lands on the same instant


def test_skipped_hour_as_ha_hands_it_over() -> None:
    """HA's local wall clock in the gap (fold 0) is the instant before the gap + the gap."""
    wire = due_from_bounds(datetime(2027, 3, 28, 2, 30, tzinfo=VIENNA), None, VIENNA)
    assert (wire["Time"], wire["Zone"]) == ("03:30", "Europe/Vienna")


def test_an_end_in_the_repeated_hour_moves_the_event_to_utc() -> None:
    start = datetime.fromisoformat("2027-10-31T01:30:00+02:00")
    end = datetime.fromisoformat("2027-10-31T02:15:00+01:00")
    wire = due_from_bounds(start, end, VIENNA)
    assert wire == {"Date": "2027-10-30", "Time": "23:30", "Zone": "UTC", "EndTime": "01:15", "EndDate": "2027-10-31"}


@pytest.mark.parametrize("fold", [0, 1])
def test_unchanged_repeated_hour_keeps_its_zone_but_a_new_instant_does_not(fold):
    previous = {"Date": "2027-10-31", "Time": "02:30", "Zone": "Europe/Vienna", "ReminderMinutes": 45}
    start = datetime(2027, 10, 31, 2, 30, tzinfo=VIENNA, fold=fold)
    due = due_from_bounds(start, None, VIENNA, previous)
    if not fold:
        assert due == previous
    else:
        assert due == {
            "Date": "2027-10-31", "Time": "01:30", "Zone": "UTC", "ReminderMinutes": 45,
        }
    assert _backend_instant(due) == start.astimezone(UTC)


# ── 5. remove completed on a shopping list ─────────────────────────────────────────────────


async def test_remove_completed_on_a_shopping_list_explains(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    freezer.move_to("2026-11-13T11:00:00+00:00")  # "Brot" was bought recently: shown as completed
    _, server = await setup_entry(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "todo",
            "remove_completed_items",
            {"entity_id": entity_id(hass, f"{SUB}_{SHOPPING_ID}", "todo")},
            blocking=True,
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "purchase_not_deletable"
    assert server.writes == []
