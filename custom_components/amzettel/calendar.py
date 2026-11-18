"""amZettel calendars (lists of type Events) and meal plans as HA calendars.

Calendars can be written (with the `calendar.write` scope; the plan may still refuse it), meal plans
are read-only. A series is changed or deleted only as a WHOLE: the HTTP API replaces an item, it has
no scope for one occurrence or "this and the following" (only the MCP tools compute that on the
server), so those requests are refused instead of rebuilt here.

Series are expanded off the event loop: after every coordinator state each calendar computes its
events for a window (today − 31 days … + 400 days) in the executor and caches them; `event` only
reads the cache, `async_get_events` serves the window from the cache and anything outside it from
another executor job. All comparisons (overlap, order, "next event") use UTC instants.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.calendar import (
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AmZettelConfigEntry
from .const import CATEGORY_LABEL, DOMAIN, LIST_TYPE_EVENTS
from .coordinator import AmZettelCoordinator
from .entity import track_entities
from .event_write import EventInputError, WantedEvent, clean_note, due_from_bounds, note_from_description, zoned_time
from .naming import display_name
from .recurrence import combine, expand, instant, recurrence_id, resolve_local
from .selection import KIND_CALENDARS, KIND_MEAL_PLANS, category_key, named_categories
from .writes import AREA_CALENDAR, async_current_list, async_item_mutation, async_write

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

CACHE_BEFORE = timedelta(days=31)
CACHE_AFTER = timedelta(days=400)

KEY_LIST = "list:"
KEY_MEALPLAN = "mealplan:"
KEY_CATEGORY = "category:"

TIMING_LABELS: dict[str, dict[str, str]] = {
    "de": {
        "Breakfast": "Frühstück",
        "Morning": "Vormittag",
        "Lunch": "Mittagessen",
        "Afternoon": "Nachmittag",
        "Dinner": "Abendessen",
    },
    "en": {
        "Breakfast": "Breakfast",
        "Morning": "Morning",
        "Lunch": "Lunch",
        "Afternoon": "Afternoon",
        "Dinner": "Dinner",
    },
}
PEOPLE_LABEL = {"de": "Personen", "en": "People"}
MEAL_DURATION = timedelta(minutes=60)
# HA's recurrence range for "this and the following occurrences".
RANGE_THIS_AND_FUTURE = "THISANDFUTURE"


def _language(language: str | None) -> str:
    return "de" if (language or "").lower().startswith("de") else "en"


def _zone(name: str | None, fallback: tzinfo) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except ValueError, KeyError, OSError:
            pass
    return fallback


def event_bounds(event: CalendarEvent, default_zone: tzinfo) -> tuple[datetime, datetime]:
    """Start and end of an event as UTC instants."""
    return instant(event.start, default_zone), instant(event.end, default_zone)


def overlaps(event: CalendarEvent, start: datetime, end: datetime, default_zone: tzinfo) -> bool:
    """Does the event intersect [start, end)? (UTC instants; a zero-length event at `start` counts)."""
    ev_start, ev_end = event_bounds(event, default_zone)
    win_start, win_end = start.astimezone(UTC), end.astimezone(UTC)
    if ev_start == ev_end:
        return win_start <= ev_start < win_end
    return ev_start < win_end and ev_end > win_start


def sort_events(events: list[CalendarEvent], default_zone: tzinfo) -> list[CalendarEvent]:
    """Order by start instant."""
    return sorted(events, key=lambda e: event_bounds(e, default_zone))


# ── events (Due semantics of due.ts) ───────────────────────────────────────────────────────


def _category_name(view: dict[str, Any], category_id: str | None) -> str | None:
    if not category_id:
        return None
    for category in view.get("EventCategories") or []:
        if str(category.get("Id")) == str(category_id):
            return category.get("Name") or None
    return None


def _event_description(view: dict[str, Any], item: dict[str, Any], language: str) -> str | None:
    parts: list[str] = []
    if note := (item.get("Note") or "").strip():
        parts.append(note)
    if category := _category_name(view, item.get("CategoryId")):
        parts.append(f"{CATEGORY_LABEL[language]}: {category}")
    return "\n".join(parts) or None


def item_events(
    view: dict[str, Any],
    item: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    default_zone: tzinfo,
    language: str = "en",
) -> list[CalendarEvent]:
    """The events (one, or the occurrences of a series) of one item inside the window."""
    due = item.get("Due")
    if not due or not due.get("Date"):
        return []
    start_day = dt_util.parse_date(due["Date"])
    if start_day is None:
        return []
    clock = dt_util.parse_time(due["Time"]) if due.get("Time") and due.get("Zone") else None
    all_day = clock is None
    zone = default_zone if all_day else _zone(due.get("Zone"), default_zone)

    # Span, expressed the server's way: whole days for all-day (EndDate inclusive), else
    # a day offset + the end's wall-clock time (same zone as the start, there is no EndZone).
    end_day = dt_util.parse_date(due["EndDate"]) if due.get("EndDate") else None
    if all_day:
        span_days = (end_day - start_day).days if end_day and end_day > start_day else 0
        end_clock = None
        end_offset = 0
    else:
        end_clock = dt_util.parse_time(due["EndTime"]) if due.get("EndTime") else None
        end_offset = (end_day - start_day).days if end_day and end_clock else 0

    description = _event_description(view, item, language)

    def build(occ: datetime, rid: str | None) -> CalendarEvent:
        if all_day:
            first = occ.date()
            return CalendarEvent(
                start=first,
                end=first + timedelta(days=span_days + 1),
                summary=item.get("Name", ""),
                description=description,
                uid=str(item["Id"]),
                recurrence_id=rid,
            )
        start = resolve_local(occ, zone)
        end = start  # a point in time: HA accepts a zero-length event
        if end_clock is not None:
            candidate = resolve_local(datetime.combine(occ.date() + timedelta(days=end_offset), end_clock), zone)
            if candidate.astimezone(UTC) > start.astimezone(UTC):
                end = candidate
        return CalendarEvent(
            start=start,
            end=end,
            summary=item.get("Name", ""),
            description=description,
            uid=str(item["Id"]),
            recurrence_id=rid,
        )

    dtstart = combine(start_day, clock)
    recurrence = item.get("Recurrence")
    if not recurrence or not recurrence.get("Rule"):
        event = build(dtstart, None)
        return [event] if overlaps(event, window_start, window_end, default_zone) else []

    # Window in the series' own local time; widen by the span (and a day for DST) on both sides.
    span = timedelta(days=span_days + 1) if all_day else timedelta(days=end_offset + 1)
    local_start = window_start.astimezone(zone).replace(tzinfo=None) - span
    local_end = window_end.astimezone(zone).replace(tzinfo=None) + timedelta(days=1)
    if all_day:
        local_start = datetime.combine(local_start.date(), time())
    series_zone = zone if isinstance(zone, ZoneInfo) else None
    events = []
    for occurrence in expand(
        recurrence["Rule"],
        recurrence.get("RDateLines"),
        recurrence.get("ExDateLines"),
        dtstart,
        all_day=all_day,
        series_zone=series_zone,
        window_start=local_start,
        window_end=local_end,
    ):
        event = build(occurrence, recurrence_id(occurrence, all_day))
        if overlaps(event, window_start, window_end, default_zone):
            events.append(event)
    return events


def list_events(
    view: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    default_zone: tzinfo,
    language: str = "en",
) -> list[CalendarEvent]:
    """All events of one calendar inside the window, sorted by start instant."""
    return [event for _, event in list_events_by_category(view, window_start, window_end, default_zone, language)]


def list_events_by_category(
    view: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    default_zone: tzinfo,
    language: str = "en",
) -> list[tuple[str | None, CalendarEvent]]:
    """All events of one calendar with the category id of their item, sorted by start instant —
    one expansion per list that the main calendar and its category calendars share."""
    pairs: list[tuple[str | None, CalendarEvent]] = []
    for item in view.get("Items") or []:
        category = str(item["CategoryId"]) if item.get("CategoryId") else None
        try:
            pairs.extend(
                (category, e) for e in item_events(view, item, window_start, window_end, default_zone, language)
            )
        except Exception:  # noqa: BLE001 — one odd item must not hide the calendar
            _LOGGER.debug("skipping an event that could not be mapped", exc_info=True)
    return sorted(pairs, key=lambda pair: event_bounds(pair[1], default_zone))


class ListExpansions:
    """Shares the expansion of one list between its main calendar and its category calendars.

    Keyed by list and window as UTC INSTANTS — local datetimes compare equal across a DST fold
    (02:00 CEST == 02:00 CET), which would hand one window's events to the other. An entry is reused
    only for the SAME view object (a new coordinator state means a new view). The executor future is
    shielded, so one entity cancelling its cache task does not cancel the computation another one
    waits for. `prune` drops lists that are no longer shown.
    """

    MAX_PER_LIST = 8

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._jobs: dict[tuple[str, datetime, datetime, str], tuple[dict[str, Any], asyncio.Future[Any]]] = {}

    async def async_get(
        self, view: dict[str, Any], start: datetime, end: datetime, zone: tzinfo, language: str
    ) -> list[tuple[str | None, CalendarEvent]]:
        list_id = str(view["Id"])
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        key = (list_id, start, end, language)
        job = self._jobs.get(key)
        if job is None or job[0] is not view:
            # Forget computations of older states of this list, keep a few windows of this one.
            for other in [k for k, (v, _) in self._jobs.items() if k[0] == list_id and v is not view]:
                del self._jobs[other]
            same = [k for k in self._jobs if k[0] == list_id]
            for oldest in same[: max(0, len(same) - self.MAX_PER_LIST + 1)]:
                del self._jobs[oldest]
            future = self._hass.async_add_executor_job(list_events_by_category, view, start, end, zone, language)
            job = (view, future)
            self._jobs[key] = job
        return await asyncio.shield(job[1])

    @callback
    def prune(self, list_ids: set[str]) -> None:
        """Forget the lists that are gone or no longer shown; their running jobs are discarded (an
        executor job cannot be stopped, it ends on its own and nobody keeps its result)."""
        for key in [k for k in self._jobs if k[0] not in list_ids]:
            del self._jobs[key]


def series_start_ids(item: dict[str, Any], default_zone: tzinfo) -> set[str]:
    """RECURRENCE-IDs that mean "the series from its start": DTSTART's, and the first occurrence's
    (a DTSTART that does not match the rule is no occurrence)."""
    due = item.get("Due") or {}
    start_day = dt_util.parse_date(due.get("Date") or "")
    recurrence = item.get("Recurrence") or {}
    if start_day is None or not recurrence.get("Rule"):
        return set()
    clock = dt_util.parse_time(due["Time"]) if due.get("Time") and due.get("Zone") else None
    all_day = clock is None
    zone = default_zone if all_day else _zone(due.get("Zone"), default_zone)
    dtstart = combine(start_day, clock)
    ids = {recurrence_id(dtstart, all_day)}
    first = expand(
        recurrence["Rule"],
        recurrence.get("RDateLines"),
        recurrence.get("ExDateLines"),
        dtstart,
        all_day=all_day,
        series_zone=zone if isinstance(zone, ZoneInfo) else None,
        window_start=dtstart - timedelta(days=366),
        window_end=dtstart + timedelta(days=3660),
    )
    if first:
        ids.add(recurrence_id(min(first), all_day))
    return ids


def same_rule(left: str | None, right: str | None) -> bool:
    """Two RRULEs as the server stores them (upper case, optional "RRULE:" prefix)."""

    def norm(rule: str | None) -> str:
        text = (rule or "").strip().upper()
        return text[len("RRULE:") :] if text.startswith("RRULE:") else text

    return norm(left) == norm(right)


# ── meal plans ─────────────────────────────────────────────────────────────────────────────


def meal_timing(entry: dict[str, Any]) -> tuple[str | None, str | None]:
    """Stored snapshot first; otherwise the legacy exact time, never today's slot or plan zone."""
    if entry.get("PlannedTime") and entry.get("PlannedZone"):
        return entry["PlannedTime"], entry["PlannedZone"]
    return entry.get("Time"), None


def meal_bounds(
    entry: dict[str, Any], default_zone: tzinfo
) -> tuple[date | datetime, date | datetime] | None:
    """Snapshot = a point in its stored zone; preserve legacy timed/all-day interpretation."""
    day = dt_util.parse_date(entry.get("Date") or "")
    if day is None:
        return None
    value, zone_name = meal_timing(entry)
    clock = dt_util.parse_time(value) if value else None
    if zone_name:
        if clock is None:
            return None
        try:
            zone = ZoneInfo(zone_name)
        except ValueError, KeyError, OSError:
            return None  # an invalid stored zone must not silently become the HA/plan zone
        start = resolve_local(datetime.combine(day, clock), zone).astimezone(default_zone)
        return start, start
    if clock is not None:
        start = resolve_local(datetime.combine(day, clock), default_zone)
        end = (start.astimezone(UTC) + MEAL_DURATION).astimezone(default_zone)
        return start, end
    return day, day + timedelta(days=1)


def meal_label(plan: dict[str, Any], entry: dict[str, Any], language: str) -> str | None:
    """The time of day: the slot's name, a built-in slot's word, else nothing."""
    slots = {str(s.get("Id")): s for s in plan.get("Slots") or []}
    slot = slots.get(str(entry.get("SlotId"))) if entry.get("SlotId") else None
    if slot is not None:
        if name := (slot.get("Name") or "").strip():
            return name
        timing = slot.get("Timing")
    else:
        timing = entry.get("Timing")
    return TIMING_LABELS[language].get(timing or "")


def meal_event(
    plan: dict[str, Any], entry: dict[str, Any], default_zone: tzinfo, language: str = "en"
) -> CalendarEvent | None:
    """One meal → one event, using its stored timing rather than the current template.

    A meal hidden from the calendar (`ShowInCalendar == false`) is no event, as in the apps.
    """
    if entry.get("ShowInCalendar") is False:
        return None
    bounds = meal_bounds(entry, default_zone)
    if bounds is None:
        return None
    dishes = " · ".join(
        (d.get("Name") or "").strip() for d in entry.get("Dishes") or [] if (d.get("Name") or "").strip()
    )
    label = meal_label(plan, entry, language)
    summary = f"{label}: {dishes}" if label and dishes else (dishes or label or "")
    parts = []
    if entry.get("People"):
        parts.append(f"{PEOPLE_LABEL[language]}: {entry['People']}")
    if note := (entry.get("Note") or "").strip():
        parts.append(note)
    description = "\n".join(parts) or None
    return CalendarEvent(
        start=bounds[0],
        end=bounds[1],
        summary=summary,
        description=description,
        uid=str(entry["Id"]),
    )


def meal_plan_events(
    plan: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    default_zone: tzinfo,
    language: str = "en",
) -> list[CalendarEvent]:
    """All meals of one plan inside the window, sorted by start instant."""
    events = []
    for entry in plan.get("Entries") or []:
        event = meal_event(plan, entry, default_zone, language)
        if event is not None and overlaps(event, window_start, window_end, default_zone):
            events.append(event)
    return sort_events(events, default_zone)


def next_event(events: list[CalendarEvent], now: datetime, default_zone: tzinfo) -> CalendarEvent | None:
    """The running event, else the next one (events sorted by start instant)."""
    now_utc = now.astimezone(UTC)
    for event in events:
        start, end = event_bounds(event, default_zone)
        if end > now_utc or start >= now_utc:
            return event
    return None


# ── platform ───────────────────────────────────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmZettelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One calendar per event list and per meal plan."""
    coordinator = entry.runtime_data.coordinator
    sub = str(entry.unique_id)
    expansions = ListExpansions(hass)

    @callback
    def _prune_expansions() -> None:
        visible = coordinator.visibility.is_visible
        expansions.prune(
            {
                list_id
                for list_id, view in coordinator.data.lists.items()
                if view.get("Type") == LIST_TYPE_EVENTS and visible(KIND_CALENDARS, list_id)
            }
        )

    entry.async_on_unload(coordinator.async_add_listener(_prune_expansions))

    def current_keys() -> list[str]:
        data = coordinator.data
        visible = coordinator.visibility.is_visible
        keys = [
            KEY_LIST + list_id
            for list_id, view in data.lists.items()
            if view.get("Type") == LIST_TYPE_EVENTS and visible(KIND_CALENDARS, list_id)
        ]
        keys.extend(KEY_MEALPLAN + plan_id for plan_id in data.meal_plans if visible(KIND_MEAL_PLANS, plan_id))
        for list_id, view in data.lists.items():
            if view.get("Type") == LIST_TYPE_EVENTS and visible(KIND_CALENDARS, list_id):
                keys.extend(
                    KEY_CATEGORY + category_key(list_id, category_id)
                    for category_id in coordinator.visibility.split_categories(view)
                )
        return keys

    def unique_id(key: str) -> str:
        if key.startswith(KEY_CATEGORY):
            list_id, category_id = key[len(KEY_CATEGORY) :].split(":", 1)
            return f"{sub}_{list_id}_cat_{category_id}"
        if key.startswith(KEY_MEALPLAN):
            return f"{sub}_mealplan_{key[len(KEY_MEALPLAN) :]}"
        return f"{sub}_{key[len(KEY_LIST) :]}"

    def factory(key: str) -> CalendarEntity:
        if key.startswith(KEY_CATEGORY):
            list_id, category_id = key[len(KEY_CATEGORY) :].split(":", 1)
            return AmZettelCategoryCalendar(coordinator, entry, list_id, category_id, expansions)
        if key.startswith(KEY_MEALPLAN):
            return AmZettelMealPlanCalendar(coordinator, entry, key[len(KEY_MEALPLAN) :])
        return AmZettelEventCalendar(coordinator, entry, key[len(KEY_LIST) :], expansions)

    track_entities(
        hass,
        entry,
        "calendar",
        current_keys,
        unique_id,
        factory,
        async_add_entities,
        coordinator.async_add_listener,
        lambda: coordinator.data.authoritative,
    )


class _AmZettelCalendar(CoordinatorEntity[AmZettelCoordinator], CalendarEntity):
    # Called by its amZettel name alone ("Wocheneinkauf", not "amZettel Wocheneinkauf"): Assist matches
    # list names against the full friendly name, and nobody says a device prefix. HA prefixes the DEVICE
    # name for both naming styles, so the entity has no device at all; without one, modern naming
    # (has_entity_name) makes the entity name the full name.
    _attr_has_entity_name = True

    def __init__(self, coordinator: AmZettelCoordinator, entry: AmZettelConfigEntry, object_id: str) -> None:
        super().__init__(coordinator)
        self._object_id = object_id
        self._cache: list[CalendarEvent] = []
        self._cache_range: tuple[datetime, datetime] | None = None
        self._cache_generation = -1  # the coordinator state the cache was computed from
        self._generation = 0
        self._cache_task: asyncio.Task[None] | None = None
        self._removed = False

    def _source(self) -> dict[str, Any] | None:
        raise NotImplementedError

    @staticmethod
    def _compute(
        source: dict[str, Any], start: datetime, end: datetime, zone: tzinfo, language: str
    ) -> list[CalendarEvent]:
        raise NotImplementedError

    async def _async_compute(
        self, source: dict[str, Any], start: datetime, end: datetime, zone: tzinfo, language: str
    ) -> list[CalendarEvent]:
        """The events of the window, computed in the executor."""
        return await self.hass.async_add_executor_job(self._compute, source, start, end, zone, language)

    @property
    def _language(self) -> str:
        return _language(self.hass.config.language if self.hass else None)

    @property
    def available(self) -> bool:
        return super().available and self._source() is not None

    @property
    def name(self) -> str | None:
        source = self._source()
        if source is None:
            return None
        return display_name(source.get("Name") or "", source, self.coordinator.user_id, self._language)

    # ── cache ──────────────────────────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_cache()

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._cancel_cache_task()
        await super().async_will_remove_from_hass()

    @callback
    def _cancel_cache_task(self) -> None:
        # The executor job itself cannot be stopped; it ends on its own (bounded expansion).
        if self._cache_task is not None and not self._cache_task.done():
            self._cache_task.cancel()
        self._cache_task = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_cache()
        super()._handle_coordinator_update()

    @callback
    def _schedule_cache(self) -> None:
        """A new coordinator state: the cache is stale from now on; recompute it (replacing a
        computation of an older state). Tracked by the config entry, so unload waits for it."""
        self._generation += 1
        self._cancel_cache_task()
        self._cache_task = self.coordinator.config_entry.async_create_task(
            self.hass, self._async_fill_cache(self._generation), "amzettel calendar cache", eager_start=False
        )

    async def _async_fill_cache(self, generation: int) -> None:
        source = self._source()
        now = dt_util.now()
        start = dt_util.start_of_local_day(now) - CACHE_BEFORE
        end = start + CACHE_BEFORE + CACHE_AFTER
        events: list[CalendarEvent] = []
        if source is not None:
            events = await self._async_compute(source, start, end, dt_util.get_default_time_zone(), self._language)
        if generation != self._generation or self._removed:
            return  # a newer state is on its way, or the entity is gone
        self._cache = events
        self._cache_range = (start, end)
        self._cache_generation = generation
        self.async_write_ha_state()

    @property
    def event(self) -> CalendarEvent | None:
        # The only place that may show the previous state until the new one is computed.
        if self._source() is None:
            return None
        return next_event(self._cache, dt_util.now(), dt_util.get_default_time_zone())

    @property
    def _cache_fresh(self) -> bool:
        return self._cache_generation == self._generation

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        zone = dt_util.get_default_time_zone()
        if (
            self._cache_fresh
            and self._cache_range
            and self._cache_range[0] <= start_date
            and end_date <= self._cache_range[1]
        ):
            return [e for e in self._cache if overlaps(e, start_date, end_date, zone)]
        source = self._source()
        if source is None:
            return []
        generation = self.coordinator.access_generation
        events = await self._async_compute(source, start_date, end_date, zone, self._language)
        return events if self.coordinator.access_generation == generation else []


class AmZettelEventCalendar(_AmZettelCalendar):
    """A list of type Events — without the events of its categories that have a calendar of their own."""

    def __init__(
        self,
        coordinator: AmZettelCoordinator,
        entry: AmZettelConfigEntry,
        list_id: str,
        expansions: ListExpansions,
    ) -> None:
        super().__init__(coordinator, entry, list_id)
        self._attr_unique_id = f"{entry.unique_id}_{list_id}"
        self._expansions = expansions

    def _source(self) -> dict[str, Any] | None:
        return self.coordinator.data.lists.get(self._object_id)

    def _keeps(self, source: dict[str, Any], category: str | None) -> bool:
        return category not in self.coordinator.visibility.split_categories(source)

    # ── writes ─────────────────────────────────────────────────────────────────────────────

    @property
    def list_id(self) -> str:
        """The amZettel calendar (list) this entity shows."""
        return self._object_id

    @property
    def own_category_id(self) -> str | None:
        """The category new events get: none on the main calendar."""
        return None

    @property
    def supported_features(self) -> CalendarEntityFeature:
        if not self.coordinator.writes_calendars:
            return CalendarEntityFeature(0)
        return (
            CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.DELETE_EVENT
        )

    def require_view(self) -> dict[str, Any]:
        view = self._source()
        if view is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="calendar_gone")
        return view

    async def async_current_view(self) -> dict[str, Any]:
        """The calendar as the server has it NOW (a failed pull raises, nothing is written): changes
        are built on the current item, never on a cached one."""
        return await async_current_list(self.hass, self.coordinator.config_entry, self.coordinator, self._object_id)

    @staticmethod
    def _require_item(view: dict[str, Any], uid: str) -> dict[str, Any]:
        for item in view.get("Items") or []:
            if str(item.get("Id")) == uid:
                return item
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="event_gone")

    async def async_write(self, call: Any) -> Any:
        """One write, with the shared refusal mapping and repairs."""
        return await async_write(self.hass, self.coordinator.config_entry, AREA_CALENDAR, call, gone_key="event_gone")

    async def _require_whole_series(self, item: dict[str, Any], rid: str | None, recurrence_range: str | None) -> None:
        """Only the whole series can be changed or deleted (see the module docstring)."""
        if not (item.get("Recurrence") or {}).get("Rule") or rid is None:
            return
        if recurrence_range != RANGE_THIS_AND_FUTURE:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="series_occurrence")
        starts = await self.hass.async_add_executor_job(series_start_ids, item, dt_util.get_default_time_zone())
        if rid not in starts:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="series_following")

    @staticmethod
    def _bounds(event: dict[str, Any]) -> tuple[Any, Any]:
        return event[EVENT_START], event.get(EVENT_END)

    def _wanted(self, event: dict[str, Any], due: dict[str, Any], note: str | None) -> WantedEvent:
        name = (event.get(EVENT_SUMMARY) or "").strip()
        if not name:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="event_summary_required")
        return WantedEvent(name=name, due=due, note=note, category_id=self.own_category_id)

    def _due(self, event: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        start, end = self._bounds(event)
        default_zone = dt_util.get_default_time_zone()
        if isinstance(start, datetime) and previous and previous.get("Time") and previous.get("Zone"):
            # HA's edit schema converts every bound to HA's zone. It is not a request to
            # change the stored zone (and therefore the DST rules) of the event or series.
            zone = _zone(previous["Zone"], default_zone)
            start = zoned_time(start, default_zone).astimezone(zone)
            if isinstance(end, datetime):
                end = zoned_time(end, default_zone).astimezone(zone)
        try:
            return due_from_bounds(start, end, default_zone, previous)
        except EventInputError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=err.key, translation_placeholders=err.placeholders
            ) from err

    async def async_create_event(self, **kwargs: Any) -> None:
        view = self.require_view()
        wanted = self._wanted(kwargs, self._due(kwargs, None), clean_note(kwargs.get(EVENT_DESCRIPTION)))
        body = wanted.add_body(str(uuid.uuid4()))
        if rule := kwargs.get(EVENT_RRULE):
            body["Recurrence"] = {"Rule": rule}
        await self.async_write(async_item_mutation(self.coordinator, view, "add", body["Id"], body))

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        view = await self.async_current_view()
        item = self._require_item(view, uid)
        await self._require_whole_series(item, recurrence_id, recurrence_range)
        due = self._due(event, item.get("Due"))
        note = note_from_description(view, item, event.get(EVENT_DESCRIPTION))
        wanted = self._wanted(event, due, note)
        changes: dict[str, Any] = {"Name": wanted.name, "Due": due, "Note": note}
        # The rule changes only when HA sends one. Our events carry no `rrule` (an occurrence read back
        # has only its recurrence_id), and HA cannot send "no rule": its event schema refuses an empty
        # or missing value as a rule (calendar/helper.py validate_rrule), so absent = unchanged; a series
        # ends in the app.
        rule = event.get(EVENT_RRULE)
        current = item.get("Recurrence")
        if rule and not (current and same_rule(current.get("Rule"), rule)):
            changes["Recurrence"] = {"Rule": rule}  # a new rule: the old additions/exclusions do not apply
        await self.async_write(async_item_mutation(self.coordinator, view, "edit", uid, changes, previous=item))

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        view = await self.async_current_view()
        item = self._require_item(view, uid)
        await self._require_whole_series(item, recurrence_id, recurrence_range)
        await self.async_write(async_item_mutation(self.coordinator, view, "remove", uid))

    async def _async_compute(
        self, source: dict[str, Any], start: datetime, end: datetime, zone: tzinfo, language: str
    ) -> list[CalendarEvent]:
        pairs = await self._expansions.async_get(source, start, end, zone, language)
        return [event for category, event in pairs if self._keeps(source, category)]


class AmZettelCategoryCalendar(AmZettelEventCalendar):
    """One category of an event list as a calendar of its own (chosen in the options)."""

    def __init__(
        self,
        coordinator: AmZettelCoordinator,
        entry: AmZettelConfigEntry,
        list_id: str,
        category_id: str,
        expansions: ListExpansions,
    ) -> None:
        super().__init__(coordinator, entry, list_id, expansions)
        self._category_id = category_id
        self._attr_unique_id = f"{entry.unique_id}_{list_id}_cat_{category_id}"

    def _source(self) -> dict[str, Any] | None:
        view = self.coordinator.data.lists.get(self._object_id)
        if view is None or self._category_id not in self.coordinator.visibility.split_categories(view):
            return None  # category deleted in amZettel or no longer split: the entity goes away
        return view

    @property
    def name(self) -> str | None:
        view = self._source()
        if view is None:
            return None
        label = f"{view.get('Name')} · {named_categories(view)[self._category_id]}"
        return display_name(label, view, self.coordinator.user_id, self._language)

    def _keeps(self, source: dict[str, Any], category: str | None) -> bool:
        return category == self._category_id

    @property
    def own_category_id(self) -> str | None:
        """Events created here belong to this category (else they would not appear here)."""
        return self._category_id


class AmZettelMealPlanCalendar(_AmZettelCalendar):
    """A meal plan."""

    _attr_icon = "mdi:silverware-fork-knife"

    def __init__(self, coordinator: AmZettelCoordinator, entry: AmZettelConfigEntry, plan_id: str) -> None:
        super().__init__(coordinator, entry, plan_id)
        self._attr_unique_id = f"{entry.unique_id}_mealplan_{plan_id}"

    def _source(self) -> dict[str, Any] | None:
        return self.coordinator.data.meal_plans.get(self._object_id)

    @property
    def language_code(self) -> str:
        """ "de" or "en", for the meal labels."""
        return self._language

    def require_plan(self) -> dict[str, Any]:
        plan = self._source()
        if plan is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="meal_plan_gone")
        return plan

    @staticmethod
    def _compute(
        source: dict[str, Any], start: datetime, end: datetime, zone: tzinfo, language: str
    ) -> list[CalendarEvent]:
        return meal_plan_events(source, start, end, zone, language)
