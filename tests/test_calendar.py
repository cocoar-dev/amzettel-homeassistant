"""Calendars: Due semantics, series, meal plans."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.amzettel.calendar import item_events, meal_event, next_event
from custom_components.amzettel.recurrence import expand, normalize_rule, parse_values

from .conftest import SUB
from .data import EVENTS, EVENTS_ID, PLAN, PLAN_ID, lists
from .fake_server import FakeServer

VIENNA = ZoneInfo("Europe/Vienna")
UTC = ZoneInfo("UTC")
WIN_START = datetime(2026, 11, 1, tzinfo=VIENNA)
WIN_END = datetime(2027, 1, 1, tzinfo=VIENNA)


def _events(item, start=WIN_START, end=WIN_END, view=None, language="de"):
    return item_events(view or {"EventCategories": []}, {"Id": "e1", "Name": "T", **item}, start, end, VIENNA, language)


def test_all_day_single() -> None:
    (event,) = _events({"Due": {"Date": "2026-11-16"}})
    assert event.start == date(2026, 11, 16)
    assert event.end == date(2026, 11, 17)
    assert event.all_day
    assert event.recurrence_id is None


def test_multi_day_inclusive_becomes_exclusive() -> None:
    (event,) = _events({"Due": {"Date": "2026-11-12", "EndDate": "2026-11-14"}})
    assert event.start == date(2026, 11, 12)
    assert event.end == date(2026, 11, 15)


def test_timed_with_zone_and_end() -> None:
    (event,) = _events({"Due": {"Date": "2026-11-16", "Time": "10:00", "Zone": "Europe/London", "EndTime": "11:30"}})
    assert event.start == datetime(2026, 11, 16, 10, 0, tzinfo=ZoneInfo("Europe/London"))
    assert event.end == datetime(2026, 11, 16, 11, 30, tzinfo=ZoneInfo("Europe/London"))
    assert event.start.astimezone(UTC).hour == 10


def test_timed_over_midnight() -> None:
    (event,) = _events(
        {
            "Due": {
                "Date": "2026-11-16",
                "Time": "22:00",
                "Zone": "Europe/Vienna",
                "EndDate": "2026-11-17",
                "EndTime": "01:00",
            }
        }
    )
    assert event.end - event.start == timedelta(hours=3)


def test_point_in_time_has_zero_length() -> None:
    (event,) = _events({"Due": {"Date": "2026-11-16", "Time": "08:15", "Zone": "Europe/Vienna"}})
    assert event.start == event.end


def test_description_with_note_and_category() -> None:
    (event,) = item_events(EVENTS, EVENTS["Items"][0], WIN_START, datetime(2026, 11, 20, tzinfo=VIENNA), VIENNA, "de")
    assert event.summary == "Gelbe Tonne"
    assert event.description == "vor 6 Uhr\nKategorie: Müll"
    assert event.uid == EVENTS["Items"][0]["Id"]


def test_series_with_typed_exdate() -> None:
    # every second Monday from 16.11., 30.11. excluded
    events = item_events(EVENTS, EVENTS["Items"][0], WIN_START, WIN_END, VIENNA, "de")
    assert [e.start for e in events] == [date(2026, 11, 16), date(2026, 12, 14), date(2026, 12, 28)]
    assert [e.recurrence_id for e in events] == ["20261116", "20261214", "20261228"]
    assert {e.uid for e in events} == {EVENTS["Items"][0]["Id"]}


def test_timed_series_keeps_local_time_across_dst_and_tzid_exdate() -> None:
    item = {
        "Due": {"Date": "2026-10-19", "Time": "07:00", "Zone": "Europe/Vienna", "EndTime": "07:30"},
        "Recurrence": {
            "Rule": "FREQ=WEEKLY;COUNT=4",
            "ExDateLines": ["EXDATE;TZID=Europe/Vienna:20261026T070000"],
        },
    }
    events = _events(item, datetime(2026, 10, 1, tzinfo=VIENNA), datetime(2026, 12, 1, tzinfo=VIENNA))
    assert [e.start for e in events] == [
        datetime(2026, 10, 19, 7, tzinfo=VIENNA),
        datetime(2026, 11, 2, 7, tzinfo=VIENNA),  # after the DST change, still 07:00 local
        datetime(2026, 11, 9, 7, tzinfo=VIENNA),
    ]  # COUNT counts the excluded slot too
    assert events[0].end - events[0].start == timedelta(minutes=30)
    assert events[1].recurrence_id == "20261102T070000"


def test_date_only_until_is_inclusive_on_timed_series() -> None:
    item = {
        "Due": {"Date": "2026-11-16", "Time": "18:00", "Zone": "Europe/Vienna"},
        "Recurrence": {"Rule": "RRULE:FREQ=DAILY;UNTIL=20261118"},
    }
    events = _events(item)
    assert [e.start.day for e in events] == [16, 17, 18]


def test_utc_exdate_and_rdate() -> None:
    item = {
        "Due": {"Date": "2026-11-16", "Time": "09:00", "Zone": "Europe/Vienna"},
        "Recurrence": {
            "Rule": "FREQ=DAILY;COUNT=3",
            "ExDateLines": ["EXDATE:20261117T080000Z"],  # = 09:00 Vienna
            "RDateLines": ["RDATE;TZID=Europe/Vienna:20261125T120000"],
        },
    }
    starts = [e.start for e in _events(item)]
    assert starts == [
        datetime(2026, 11, 16, 9, tzinfo=VIENNA),
        datetime(2026, 11, 18, 9, tzinfo=VIENNA),
        datetime(2026, 11, 25, 12, tzinfo=VIENNA),
    ]


def test_running_occurrence_overlapping_window_start() -> None:
    item = {
        "Due": {"Date": "2026-11-02", "EndDate": "2026-11-04"},
        "Recurrence": {"Rule": "FREQ=WEEKLY"},
    }
    events = _events(item, datetime(2026, 11, 3, 12, tzinfo=VIENNA), datetime(2026, 11, 5, tzinfo=VIENNA))
    assert [(e.start, e.end) for e in events] == [(date(2026, 11, 2), date(2026, 11, 5))]


def test_recurrence_helpers() -> None:
    assert normalize_rule("RRULE:FREQ=WEEKLY;UNTIL=20261118", False, VIENNA) == "FREQ=WEEKLY;UNTIL=20261118T235959"
    assert normalize_rule("FREQ=DAILY;UNTIL=20261118T120000Z", True, None) == "FREQ=DAILY;UNTIL=20261118"
    assert normalize_rule("FREQ=DAILY;UNTIL=20261118T120000Z", False, VIENNA) == "FREQ=DAILY;UNTIL=20261118T130000"
    assert parse_values(['EXDATE;TZID="Europe/London":20261117T090000'], VIENNA) == [datetime(2026, 11, 17, 10)]
    assert expand(
        "FREQ=DAILY",
        None,
        None,
        datetime(2026, 1, 1),
        all_day=True,
        series_zone=None,
        window_start=datetime(2026, 1, 3),
        window_end=datetime(2026, 1, 4),
    ) == [datetime(2026, 1, 3), datetime(2026, 1, 4)]


def test_meal_mapping() -> None:
    lunch, jause, pizza = (meal_event(PLAN, e, VIENNA, "de") for e in PLAN["Entries"])
    assert lunch.summary == "Mittagessen: Spaghetti · Salat"
    assert lunch.description == "Personen: 3\nNudeln kaufen"
    assert lunch.start == date(2026, 11, 16) and lunch.end == date(2026, 11, 17)
    assert jause.summary == "Jause: Brote"
    assert pizza.summary == "Pizza"
    assert pizza.start == datetime(2026, 11, 17, 18, 30, tzinfo=VIENNA)
    assert pizza.end - pizza.start == timedelta(minutes=60)
    assert meal_event(PLAN, PLAN["Entries"][0], VIENNA, "en").summary == "Lunch: Spaghetti · Salat"


@pytest.mark.parametrize(
    ("day", "clock", "zone", "expected"),
    [
        ("2026-11-24", "19:30", "Europe/London", "2026-11-24T19:30:00+00:00"),
        ("2026-07-24", "19:30", "Europe/London", "2026-07-24T18:30:00+00:00"),
        ("2026-07-24", "19:30", "Europe/Vienna", "2026-07-24T17:30:00+00:00"),
        ("2026-11-24", "19:30", "Europe/Vienna", "2026-11-24T18:30:00+00:00"),
        ("2026-11-24", "00:30", "UTC", "2026-11-24T00:30:00+00:00"),
        # Due.ToInstant(): NodaTime InZoneLeniently shifts gaps, chooses the earlier fold.
        ("2027-03-28", "02:30", "Europe/Vienna", "2027-03-28T01:30:00+00:00"),
        ("2027-03-28", "01:30", "Europe/London", "2027-03-28T01:30:00+00:00"),
        ("2026-10-25", "02:30", "Europe/Vienna", "2026-10-25T00:30:00+00:00"),
        ("2026-10-25", "01:30", "Europe/London", "2026-10-25T00:30:00+00:00"),
    ],
)
def test_meal_snapshot_uses_its_own_time_and_zone(day, clock, zone, expected) -> None:
    entry = {
        **PLAN["Entries"][0],
        "Date": day,
        "Time": "06:00",  # even a legacy time cannot override a complete snapshot
        "PlannedTime": clock,
        "PlannedZone": zone,
    }
    event = meal_event(PLAN, entry, VIENNA)
    assert event.start.astimezone(UTC).isoformat() == expected
    assert event.start.tzinfo == VIENNA
    assert event.end == event.start  # no invented duration


def test_template_changes_never_retime_existing_meals() -> None:
    original = PLAN["Entries"][0]
    snapshot = {**original, "PlannedTime": "19:30", "PlannedZone": "Europe/London"}
    changed = {**PLAN, "Zone": "Europe/Vienna", "Slots": [{"Id": original["SlotId"], "Time": "08:00"}]}
    assert meal_event(PLAN, snapshot, UTC).start == meal_event(changed, snapshot, UTC).start
    # No snapshot, even when its current slot has a time and SortMinutes: still all-day.
    assert meal_event(changed, original, UTC).start == date(2026, 11, 16)
    # Snapshot pair absent/null also preserves the old exact-time interpretation in HA's zone.
    legacy = {**PLAN["Entries"][2], "PlannedTime": None, "PlannedZone": None}
    assert meal_event(changed, legacy, VIENNA).start == datetime(2026, 11, 17, 18, 30, tzinfo=VIENNA)


def test_invalid_snapshot_zone_is_not_replaced_by_ha_or_plan_zone() -> None:
    entry = {**PLAN["Entries"][0], "PlannedTime": "19:30", "PlannedZone": "Not/AZone"}
    assert meal_event({**PLAN, "Zone": "Europe/Vienna"}, entry, UTC) is None


def test_next_event() -> None:
    events = _events({"Due": {"Date": "2026-11-16"}, "Recurrence": {"Rule": "FREQ=DAILY;COUNT=3"}})
    now = datetime(2026, 11, 17, 12, tzinfo=VIENNA)
    assert next_event(events, now, VIENNA).start == date(2026, 11, 17)


# ── entities ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry, no_realtime):
    await hass.config.async_set_time_zone("Europe/Vienna")
    config_entry.server = FakeServer(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_calendar_entities(hass: HomeAssistant, setup) -> None:
    registry = er.async_get(hass)
    events_entity = registry.async_get_entity_id("calendar", "amzettel", f"{SUB}_{EVENTS_ID}")
    plan_entity = registry.async_get_entity_id("calendar", "amzettel", f"{SUB}_mealplan_{PLAN_ID}")
    assert events_entity and plan_entity
    calendars = [e for e in registry.entities.values() if e.domain == "calendar"]
    assert len(calendars) == 2  # shopping/checklist/todo lists are no calendars

    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": [events_entity, plan_entity],
            "start_date_time": "2026-11-16T00:00:00+01:00",
            "end_date_time": "2026-11-18T00:00:00+01:00",
        },
        blocking=True,
        return_response=True,
    )
    assert [e["summary"] for e in response[events_entity]["events"]] == ["Gelbe Tonne"]
    meals = sorted(e["summary"] for e in response[plan_entity]["events"])
    assert meals == ["Jause: Brote", "Lunch: Spaghetti · Salat", "Pizza"]


async def test_meal_snapshot_refreshes_via_a_change(hass, setup) -> None:
    plan = setup.server.plans[PLAN_ID]
    plan["Entries"] = plan["Entries"][:1]
    plan["Entries"][0].update(PlannedTime="19:30", PlannedZone="Europe/London")
    plan["ETag"] = '"plan-2"'
    coordinator = setup.runtime_data.coordinator
    await coordinator.async_apply_change("MealPlans", PLAN_ID, "cursor-2")  # an SSE change of the plan
    await hass.async_block_till_done()
    assert coordinator.data.meal_plans[PLAN_ID]["ETag"] == '"plan-2"' and coordinator.cursor == "cursor-2"
    target = er.async_get(hass).async_get_entity_id("calendar", "amzettel", f"{SUB}_mealplan_{PLAN_ID}")
    response = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": target, "start_date_time": "2026-11-16T20:30:00+01:00",
         "end_date_time": "2026-11-16T20:31:00+01:00"},
        blocking=True, return_response=True,
    )
    (event,) = response[target]["events"]
    assert event["start"] == event["end"] == "2026-11-16T20:30:00+01:00"


async def test_calendar_disappears_with_its_list(hass: HomeAssistant, setup) -> None:
    from custom_components.amzettel.coordinator import AmZettelData

    coordinator = setup.runtime_data.coordinator
    coordinator.async_set_updated_data(
        AmZettelData(lists={v["Id"]: v for v in lists() if v["Id"] != EVENTS_ID}, meal_plans={})
    )
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("calendar", "amzettel", f"{SUB}_{EVENTS_ID}") is None
    assert registry.async_get_entity_id("calendar", "amzettel", f"{SUB}_mealplan_{PLAN_ID}") is None


# ── review fixes ───────────────────────────────────────────────────────────────────────────


async def test_dst_gap_resolves_forward_and_compares_in_utc(hass: HomeAssistant) -> None:
    """28.03.2027 02:30 does not exist in Vienna: it becomes 03:30 (Temporal "compatible",
    NodaTime lenient). Windows given in local time and in UTC must agree."""
    await hass.config.async_set_time_zone("Europe/Vienna")
    item = {"Due": {"Date": "2027-03-28", "Time": "02:30", "Zone": "Europe/Vienna", "EndTime": "04:00"}}
    local = _events(item, datetime(2027, 3, 28, 3, 0, tzinfo=VIENNA), datetime(2027, 3, 28, 3, 15, tzinfo=VIENNA))
    utc = _events(item, datetime(2027, 3, 28, 1, 0, tzinfo=UTC), datetime(2027, 3, 28, 1, 15, tzinfo=UTC))
    assert local == utc == []  # the event starts at 03:30 local = 01:30 UTC
    local_hit = _events(item, datetime(2027, 3, 28, 3, 40, tzinfo=VIENNA), datetime(2027, 3, 28, 3, 50, tzinfo=VIENNA))
    utc_hit = _events(item, datetime(2027, 3, 28, 1, 40, tzinfo=UTC), datetime(2027, 3, 28, 1, 50, tzinfo=UTC))
    assert local_hit == utc_hit and len(local_hit) == 1
    (event,) = local_hit
    assert (event.start.hour, event.start.minute) == (3, 30)
    assert event.start.astimezone(UTC) == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert event.end.astimezone(UTC) == datetime(2027, 3, 28, 2, 0, tzinfo=UTC)


def test_meal_duration_is_real_time_across_the_dst_end() -> None:
    plan = {"Slots": []}
    entry = {"Id": "m", "Date": "2026-10-25", "Timing": "ExactTime", "Time": "02:30", "People": 2, "Dishes": []}
    event = meal_event(plan, entry, VIENNA, "de")
    assert event.end.astimezone(UTC) - event.start.astimezone(UTC) == timedelta(minutes=60)


def test_meal_hidden_from_the_calendar_is_no_event() -> None:
    entry = {**PLAN["Entries"][0], "ShowInCalendar": False}
    assert meal_event(PLAN, entry, VIENNA, "de") is None
    assert meal_event(PLAN, {**entry, "ShowInCalendar": True}, VIENNA, "de") is not None


def test_dtstart_outside_the_rule_like_the_backend() -> None:
    """Ical.Net 5.2.3 (and the web's rrule-temporal): a DTSTART that does not match BYDAY is no
    occurrence and COUNT counts matching ones (checked against the backend's library 2026-11-14)."""
    item = {"Due": {"Date": "2026-11-16"}, "Recurrence": {"Rule": "FREQ=WEEKLY;BYDAY=WE;COUNT=3"}}
    assert [e.start for e in _events(item)] == [date(2026, 11, 18), date(2026, 11, 25), date(2026, 12, 2)]


def test_unreadable_until_is_ignored_like_the_backend() -> None:
    """Ical.Net ignores an UNTIL it cannot read and expands without end — no silent single event."""
    item = {"Due": {"Date": "2026-11-16"}, "Recurrence": {"Rule": "FREQ=DAILY;UNTIL=garbage"}}
    events = _events(item, datetime(2026, 11, 16, tzinfo=VIENNA), datetime(2026, 11, 20, tzinfo=VIENNA))
    assert [e.start.day for e in events] == [16, 17, 18, 19]


def test_impossible_rule_is_bounded() -> None:
    """FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30 never matches; dateutil alone scans to the year 9999."""
    import time as _time

    item = {"Due": {"Date": "2026-01-01"}, "Recurrence": {"Rule": "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30;COUNT=5"}}
    started = _time.monotonic()
    assert _events(item, datetime(2026, 1, 1, tzinfo=VIENNA), datetime(2027, 2, 1, tzinfo=VIENNA)) == []
    assert _time.monotonic() - started < 0.5


def test_series_time_budget(monkeypatch) -> None:
    from custom_components.amzettel import recurrence

    monkeypatch.setattr(recurrence, "SERIES_TIME_BUDGET", -1)  # already over budget
    assert (
        expand(
            "FREQ=DAILY",
            None,
            None,
            datetime(2026, 1, 1),
            all_day=True,
            series_zone=None,
            window_start=datetime(2026, 1, 1),
            window_end=datetime(2026, 1, 5),
        )
        == []
    )


async def test_event_reads_only_the_cache(hass: HomeAssistant, setup) -> None:
    """`event` never expands: after the cache is filled it works with expansion disabled."""
    from custom_components.amzettel import calendar as cal

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("calendar", "amzettel", f"{SUB}_{EVENTS_ID}")
    entity = hass.data["entity_components"]["calendar"].get_entity(entity_id)
    assert entity._cache  # filled in the executor after the first state
    calls = []
    original = cal.list_events
    cal.list_events = lambda *a, **k: calls.append(a) or original(*a, **k)
    try:
        assert entity.event is not None
        assert calls == []
    finally:
        cal.list_events = original


async def test_get_events_outside_the_cache_uses_the_executor(hass: HomeAssistant, setup) -> None:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("calendar", "amzettel", f"{SUB}_{EVENTS_ID}")
    response = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": "2030-01-01T00:00:00+01:00",
            "end_date_time": "2030-01-15T00:00:00+01:00",
        },
        blocking=True,
        return_response=True,
    )
    # every second Monday from 16.11.2026: 7. and … 2030 — computed on demand, not from the cache
    assert [e["start"] for e in response[entity_id]["events"]] == ["2030-01-07"]


# ── re-review fixes ────────────────────────────────────────────────────────────────────────

DENSE_YEARLY = (
    "FREQ=YEARLY;BYMONTH=1,2,3,4,5,6,7,8,9,10,11,12;BYMONTHDAY="
    + ",".join(map(str, range(1, 32)))
    + ";BYHOUR="
    + ",".join(map(str, range(24)))
    + ";BYMINUTE="
    + ",".join(map(str, range(60)))
    + ";BYSECOND="
    + ",".join(map(str, range(60)))
)


def _timed_expand(rule: str, dtstart: datetime) -> tuple[list[datetime] | None, float]:
    """Run `expand` in a thread so a regression fails the test instead of hanging the suite."""
    import threading
    import time as _time

    result: list = []
    started = _time.monotonic()
    worker = threading.Thread(
        target=lambda: result.append(
            expand(
                rule,
                None,
                None,
                dtstart,
                all_day=False,
                series_zone=None,
                window_start=datetime(2026, 1, 1),
                window_end=datetime(2027, 1, 1),
            )
        ),
        daemon=True,
    )
    worker.start()
    worker.join(2)
    return (result[0] if result else None), _time.monotonic() - started


def test_interval_zero_shows_only_the_start_and_ends_at_once() -> None:
    events, seconds = _timed_expand("FREQ=DAILY;INTERVAL=0", datetime(2026, 3, 1, 8))
    assert events == [datetime(2026, 3, 1, 8)]
    assert seconds < 0.5
    events, _ = _timed_expand("FREQ=DAILY;INTERVAL=0", datetime(2020, 3, 1, 8))
    assert events == []  # the start lies before the window


def test_dense_yearly_rule_stays_within_the_budget() -> None:
    events, seconds = _timed_expand(DENSE_YEARLY, datetime(2020, 1, 1))
    assert events is not None
    assert seconds < 0.5


def _calendar_entity(hass: HomeAssistant, key: str):
    entity_id = er.async_get(hass).async_get_entity_id("calendar", "amzettel", key)
    return hass.data["entity_components"]["calendar"].get_entity(entity_id)


async def test_new_state_makes_the_cache_stale(hass: HomeAssistant, setup) -> None:
    """After a change `async_get_events` must not serve deleted events from the old cache."""
    from unittest.mock import patch

    from custom_components.amzettel import calendar as cal
    from custom_components.amzettel.coordinator import AmZettelData

    entity = _calendar_entity(hass, f"{SUB}_{EVENTS_ID}")
    start = datetime(2026, 11, 16, tzinfo=VIENNA)
    end = datetime(2026, 11, 17, tzinfo=VIENNA)
    entity._cache_range = (start - timedelta(days=365), end + timedelta(days=365))  # window surely inside
    assert [e.summary for e in await entity.async_get_events(hass, start, end)] == ["Gelbe Tonne"]

    halted = asyncio.Event()

    async def halted_fill(self, generation):
        await halted.wait()

    coordinator = setup.runtime_data.coordinator
    emptied = {**coordinator.data.lists[EVENTS_ID], "Items": []}
    with patch.object(cal._AmZettelCalendar, "_async_fill_cache", halted_fill):
        coordinator.async_set_updated_data(
            AmZettelData(lists={**coordinator.data.lists, EVENTS_ID: emptied}, meal_plans=coordinator.data.meal_plans)
        )
        await asyncio.sleep(0)
        assert await entity.async_get_events(hass, start, end) == []
        assert entity.event is not None  # only `event` may still show the previous state
        halted.set()


async def test_cache_task_ends_with_the_entity(hass: HomeAssistant, setup) -> None:
    from unittest.mock import patch

    from custom_components.amzettel import calendar as cal
    from custom_components.amzettel.coordinator import AmZettelData

    entity = _calendar_entity(hass, f"{SUB}_{EVENTS_ID}")
    coordinator = setup.runtime_data.coordinator

    async def halted_fill(self, generation):
        await asyncio.Event().wait()

    with patch.object(cal._AmZettelCalendar, "_async_fill_cache", halted_fill):
        coordinator.async_set_updated_data(
            AmZettelData(lists=dict(coordinator.data.lists), meal_plans=dict(coordinator.data.meal_plans))
        )
        await asyncio.sleep(0)
        task = entity._cache_task
        assert task is not None and not task.done()
        # a newer state replaces the running computation
        coordinator.async_set_updated_data(
            AmZettelData(lists=dict(coordinator.data.lists), meal_plans=dict(coordinator.data.meal_plans))
        )
        await asyncio.sleep(0)
        assert task.cancelled()
        running = entity._cache_task
        # the list disappears → the entity is removed → its computation is cancelled
        coordinator.async_set_updated_data(
            AmZettelData(lists={k: v for k, v in coordinator.data.lists.items() if k != EVENTS_ID}, meal_plans={})
        )
        await hass.async_block_till_done()
        assert running.cancelled()
        assert entity._cache_task is None


async def test_timed_event_attributes_stay_local(hass: HomeAssistant, aioclient_mock, no_realtime, freezer):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import entry_data

    freezer.move_to("2026-11-16T08:00:00+00:00")
    config_entry = MockConfigEntry(domain="amzettel", unique_id=SUB, data=entry_data(expires_at=4102444800))
    await hass.config.async_set_time_zone("Europe/Vienna")
    view = {
        **EVENTS,
        "Items": [
            {
                "Id": "e1",
                "Name": "Call",
                "Due": {"Date": "2026-11-16", "Time": "10:00", "Zone": "Europe/London", "EndTime": "11:00"},
            }
        ],
    }
    FakeServer(aioclient_mock, [view], [])
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("calendar", "amzettel", f"{SUB}_{EVENTS_ID}")
    state = hass.states.get(entity_id)
    assert state.attributes["start_time"] == "2026-11-16 11:00:00"  # 10:00 London = 11:00 Vienna
    assert state.attributes["end_time"] == "2026-11-16 12:00:00"
    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="HA limitation: CalendarEntity.state compares start/end/now as wall clock in the SAME tzinfo "
    "(fold ignored); UTC-aware events do not help because get_datetime_local converts them back.",
)
async def test_meal_in_the_repeated_hour_is_on(hass: HomeAssistant, aioclient_mock, config_entry, no_realtime, freezer):
    freezer.move_to("2026-10-25T00:45:00+00:00")  # 02:45 CEST, the first 02:45 of that night
    await hass.config.async_set_time_zone("Europe/Vienna")
    plan = {
        **PLAN,
        "Entries": [
            {
                "Id": "m",
                "Date": "2026-10-25",
                "Timing": "ExactTime",
                "Time": "02:30",
                "People": 2,
                "Dishes": [{"Id": "d", "Name": "Suppe"}],
            }
        ],
    }
    FakeServer(aioclient_mock, [], [plan])
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("calendar", "amzettel", f"{SUB}_mealplan_{PLAN_ID}")
    try:
        assert hass.states.get(entity_id).state == "on"
    finally:
        await hass.config_entries.async_unload(config_entry.entry_id)


def test_old_dense_but_valid_series_keeps_its_occurrences() -> None:
    """Historical candidates must not use up the limit: 24 per day since 2010 → still 24 today."""
    rule = "FREQ=DAILY;BYHOUR=" + ",".join(map(str, range(24)))
    events = expand(
        rule,
        None,
        None,
        datetime(2010, 1, 1),
        all_day=False,
        series_zone=VIENNA,
        window_start=datetime(2026, 11, 16),
        window_end=datetime(2026, 11, 16, 23, 59, 59),
    )
    assert len(events) == 24
    assert events[0] == datetime(2026, 11, 16, 0) and events[-1] == datetime(2026, 11, 16, 23)


def test_historically_too_dense_rule_gives_nothing_and_logs_once(monkeypatch, caplog) -> None:
    import logging
    import time as _time

    from custom_components.amzettel import recurrence

    monkeypatch.setattr(recurrence, "_logged_aborts", set())
    rule = DENSE_YEARLY + ";WKST=MO"
    caplog.set_level(logging.DEBUG, logger="custom_components.amzettel.recurrence")
    for _ in range(2):
        started = _time.monotonic()
        events, seconds = _timed_expand(rule, datetime(2000, 1, 1))
        assert events == []  # the deadline hit before the window: no partial answer
        assert seconds < 0.5
        assert _time.monotonic() - started < 1
    assert sum("took too long" in r.getMessage() for r in caplog.records) == 1
