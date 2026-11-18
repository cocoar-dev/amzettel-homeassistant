"""Actions of the integration, registered once in `async_setup` as platform entity services: the
target is an amZettel calendar, meal plan or to-do entity (several targets = one answer each).

Writing: `add_event` (idempotent with `external_id`, decision 3), `sync_events` (a source's events in
a period, store-backed ownership), `add_item`. Reading (`supports_response=ONLY`): `get_events`
(series expanded), `get_meals`, `get_items` — plain data, not the calendar's display text.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LIST_TYPE_SHOPPING
from .event_write import (
    EventInputError,
    WantedEvent,
    clean_note,
    due_from_bounds,
    event_item_id,
    parse_bound,
    resolve_category,
)
from .recurrence import instant, resolve_local
from .sources import sync_sources
from .writes import AREA_LISTS, async_item_mutation, async_write

if TYPE_CHECKING:
    from .calendar import AmZettelEventCalendar

SERVICE_ADD_EVENT = "add_event"
SERVICE_SYNC_EVENTS = "sync_events"
SERVICE_ADD_ITEM = "add_item"
SERVICE_GET_EVENTS = "get_events"
SERVICE_GET_MEALS = "get_meals"
SERVICE_GET_ITEMS = "get_items"

ATTR_SUMMARY = "summary"
ATTR_DESCRIPTION = "description"
ATTR_CATEGORY = "category"
ATTR_EXTERNAL_ID = "external_id"
ATTR_START_DATE_TIME = "start_date_time"
ATTR_END_DATE_TIME = "end_date_time"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_SOURCE = "source"
ATTR_EVENTS = "events"
ATTR_START = "start"
ATTR_END = "end"
ATTR_ALLOW_EMPTY = "allow_empty"
ATTR_NAME = "name"
ATTR_QUANTITY = "quantity"
ATTR_UNIT = "unit"
ATTR_NOTE = "note"
ATTR_INCLUDE_COMPLETED = "include_completed"
ATTR_TIME_ZONE = "time_zone"

DEFAULT_QUERY_DAYS = 7
MAX_SYNC_EVENTS = 1000

ADD_EVENT_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_SUMMARY): vol.All(cv.string, vol.Length(min=1, max=200)),
    vol.Exclusive(ATTR_START_DATE_TIME, "start"): cv.datetime,
    vol.Exclusive(ATTR_START_DATE, "start"): cv.date,
    vol.Optional(ATTR_END_DATE_TIME): cv.datetime,
    vol.Optional(ATTR_END_DATE): cv.date,
    vol.Optional(ATTR_CATEGORY): cv.string,
    vol.Optional(ATTR_DESCRIPTION): cv.string,
    vol.Optional(ATTR_EXTERNAL_ID): vol.All(cv.string, vol.Length(min=1, max=200)),
    vol.Optional(ATTR_TIME_ZONE): cv.time_zone,
}

SYNC_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_EXTERNAL_ID): vol.All(cv.string, vol.Length(min=1, max=200)),
        vol.Required(ATTR_SUMMARY): vol.All(cv.string, vol.Length(min=1, max=200)),
        vol.Required(ATTR_START): vol.Any(cv.string, cv.date, cv.datetime),
        vol.Optional(ATTR_END): vol.Any(cv.string, cv.date, cv.datetime),
        vol.Optional(ATTR_DESCRIPTION): vol.Any(None, cv.string),
        vol.Optional(ATTR_CATEGORY): vol.Any(None, cv.string),
    },
    extra=vol.REMOVE_EXTRA,  # e.g. `location` or `uid` of a calendar.get_events answer
)

SYNC_EVENTS_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_SOURCE): vol.All(cv.string, vol.Length(min=1, max=100)),
    vol.Required(ATTR_EVENTS): vol.All(cv.ensure_list, vol.Length(max=MAX_SYNC_EVENTS), [SYNC_EVENT_SCHEMA]),
    vol.Required(ATTR_START): vol.Any(cv.string, cv.date, cv.datetime),
    vol.Required(ATTR_END): vol.Any(cv.string, cv.date, cv.datetime),
    vol.Optional(ATTR_ALLOW_EMPTY, default=False): cv.boolean,
}

ADD_ITEM_SCHEMA: dict[Any, Any] = {
    vol.Required(ATTR_NAME): vol.All(cv.string, vol.Length(min=1, max=200)),
    vol.Optional(ATTR_QUANTITY): vol.All(vol.Coerce(float), vol.Range(min=0, min_included=False, max=100000)),
    vol.Optional(ATTR_UNIT): vol.All(cv.string, vol.Length(max=20)),
    vol.Optional(ATTR_CATEGORY): vol.All(cv.string, vol.Length(max=100)),
    vol.Optional(ATTR_NOTE): vol.All(cv.string, vol.Length(max=2000)),
}

GET_EVENTS_SCHEMA: dict[Any, Any] = {
    vol.Optional(ATTR_START): vol.Any(cv.string, cv.date, cv.datetime),
    vol.Optional(ATTR_END): vol.Any(cv.string, cv.date, cv.datetime),
    vol.Optional(ATTR_CATEGORY): cv.string,
}

GET_MEALS_SCHEMA: dict[Any, Any] = {
    vol.Optional(ATTR_START): vol.Any(cv.string, cv.date, cv.datetime),
    vol.Optional(ATTR_END): vol.Any(cv.string, cv.date, cv.datetime),
}

GET_ITEMS_SCHEMA: dict[Any, Any] = {
    vol.Optional(ATTR_INCLUDE_COMPLETED, default=False): cv.boolean,
}


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None
    )


def _from_input_error(err: EventInputError) -> ServiceValidationError:
    return _invalid(err.key, **err.placeholders)


# ── pure helpers (unit tested) ─────────────────────────────────────────────────────────────


def window_bounds(start: Any, end: Any, now: datetime, zone: tzinfo) -> tuple[datetime, datetime]:
    """A query or sync period as aware datetimes. A date starts at local midnight; an END date counts
    fully (up to the next midnight). Defaults: from now, seven days."""
    try:
        first = parse_bound(start) if start is not None else now
        last = parse_bound(end) if end is not None else None
    except EventInputError as err:
        raise _from_input_error(err) from err

    def as_start(value: date | datetime) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=zone)
        return resolve_local(datetime.combine(value, datetime.min.time()), zone)

    begin = as_start(first)
    if last is None:
        finish = begin + timedelta(days=DEFAULT_QUERY_DAYS)
    elif isinstance(last, datetime):
        finish = as_start(last)
    else:
        finish = as_start(last + timedelta(days=1))
    if finish.astimezone(dt_util.UTC) <= begin.astimezone(dt_util.UTC):
        raise _invalid("period_end_before_start")
    return begin, finish


def event_bounds_input(data: dict[str, Any]) -> tuple[date | datetime, date | datetime | None]:
    """`add_event`: start_date_time/end_date_time or start_date/end_date (end exclusive, as in
    `calendar.create_event`)."""
    if ATTR_START_DATE_TIME in data:
        if ATTR_END_DATE in data:
            raise _invalid("event_mixed_bounds")
        start = data[ATTR_START_DATE_TIME]
        end = data.get(ATTR_END_DATE_TIME)
        if zone_name := data.get(ATTR_TIME_ZONE):
            zone = ZoneInfo(zone_name)
            # A clock time belongs to the chosen zone; an offset-aware value already
            # identifies an instant, so convert rather than replacing its zone.
            start = start.astimezone(zone) if start.tzinfo else start.replace(tzinfo=zone)
            if end is not None:
                end = end.astimezone(zone) if end.tzinfo else end.replace(tzinfo=zone)
        return start, end
    if ATTR_START_DATE in data:
        if ATTR_END_DATE_TIME in data:
            raise _invalid("event_mixed_bounds")
        return data[ATTR_START_DATE], data.get(ATTR_END_DATE)
    raise _invalid("event_start_required")


def _iso(value: date | datetime) -> str:
    return value.isoformat()


def event_start_instant(item: dict[str, Any], zone: tzinfo) -> datetime | None:
    """When an event (a series: its first start) begins, as an aware instant."""
    from .todo import due_to_ha

    start = due_to_ha(item.get("Due"))
    return instant(start, zone) if start is not None else None


def item_data(view: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """One to-do / shopping entry as plain data."""
    from .todo import due_to_ha, list_type

    kind = list_type(view)
    due = due_to_ha(item.get("Due"))
    return {
        "id": str(item["Id"]),
        "name": item.get("Name", ""),
        "quantity": item.get("Quantity", 1),
        "unit": item.get("Unit"),
        "category": item.get("Category"),
        "store": item.get("Store"),
        "note": item.get("Note"),
        "due": _iso(due) if due is not None else None,
        "completed": kind != LIST_TYPE_SHOPPING and bool(item.get("Done")),
        "completed_at": item.get("DoneAt") if kind != LIST_TYPE_SHOPPING else None,
    }


def purchase_data(purchase: dict[str, Any]) -> dict[str, Any]:
    """A bought shopping entry (completed) as plain data."""
    return {
        "id": str(purchase.get("ItemId")),
        "name": purchase.get("Name", ""),
        "quantity": purchase.get("Quantity", 1),
        "unit": purchase.get("Unit"),
        "category": purchase.get("Category"),
        "store": purchase.get("Store"),
        "note": None,
        "due": None,
        "completed": True,
        "completed_at": purchase.get("CheckedAt"),
        "completed_by": str(purchase["ByUserId"]) if purchase.get("ByUserId") else None,
    }


def _category_names(view: dict[str, Any]) -> dict[str, str | None]:
    return {str(c.get("Id")): c.get("Name") for c in view.get("EventCategories") or []}


def _person_names(view: dict[str, Any], ids: list[Any] | None) -> list[str]:
    persons = {str(p.get("Id")): p.get("Name") for p in view.get("Persons") or []}
    return [persons[str(i)] for i in ids or [] if persons.get(str(i))]


def events_data(
    view: dict[str, Any],
    start: datetime,
    end: datetime,
    zone: tzinfo,
    categories: set[str] | None,
) -> list[dict[str, Any]]:
    """The events (series expanded) of a calendar in [start, end) as plain data; runs in the executor.
    `categories` None = all, else only those category ids (an empty string = no category)."""
    from .calendar import list_events_by_category

    items = {str(i["Id"]): i for i in view.get("Items") or []}
    names = _category_names(view)
    result = []
    for category, event in list_events_by_category(view, start, end, zone):
        if categories is not None and (category or "") not in categories:
            continue
        item = items.get(event.uid or "", {})
        result.append(
            {
                "uid": event.uid,
                "recurrence_id": event.recurrence_id,
                "summary": event.summary,
                "start": _iso(event.start),
                "end": _iso(event.end),
                "all_day": not isinstance(event.start, datetime),
                "note": item.get("Note"),
                "category_id": category,
                "category": names.get(category) if category else None,
                "persons": _person_names(view, item.get("PersonIds")),
                "recurring": bool((item.get("Recurrence") or {}).get("Rule")),
            }
        )
    return result


def meals_data(
    plan: dict[str, Any], start: datetime, end: datetime, zone: tzinfo, language: str
) -> list[dict[str, Any]]:
    """The meals of a plan in [start, end) as plain data, by day and time — also those hidden from
    the calendar (`shown_in_calendar` says which)."""
    from .calendar import meal_bounds, meal_label, meal_timing

    begin, finish = start.astimezone(dt_util.UTC), end.astimezone(dt_util.UTC)
    rows = []
    for entry in plan.get("Entries") or []:
        bounds = meal_bounds(entry, zone)
        if bounds is None:
            continue
        first, last = (instant(bound, zone) for bound in bounds)
        intersects = begin <= first < finish if first == last else first < finish and last > begin
        if not intersects:
            continue
        value, zone_name = meal_timing(entry)
        clock = dt_util.parse_time(value) if value else None
        rows.append(
            (
                first,
                {
                    "id": str(entry["Id"]),
                    "date": entry["Date"],
                    "time": clock.strftime("%H:%M") if clock else None,
                    "time_zone": zone_name,
                    "slot": meal_label(plan, entry, language),
                    "slot_id": str(entry["SlotId"]) if entry.get("SlotId") else None,
                    "dishes": [
                        {"name": d.get("Name", ""), "recipe_id": str(d["RecipeId"]) if d.get("RecipeId") else None}
                        for d in entry.get("Dishes") or []
                    ],
                    "people": entry.get("People"),
                    "note": entry.get("Note"),
                    "shown_in_calendar": entry.get("ShowInCalendar") is not False,
                },
            )
        )
    rows.sort(key=lambda row: row[0])
    return [row for _, row in rows]


# ── handlers ───────────────────────────────────────────────────────────────────────────────


def _require_event_calendar(entity: Any) -> AmZettelEventCalendar:
    from .calendar import AmZettelEventCalendar

    if not isinstance(entity, AmZettelEventCalendar):
        raise _invalid("not_an_event_calendar", entity_id=entity.entity_id)
    return entity


def _require_calendar_write(entity: AmZettelEventCalendar) -> None:
    if not entity.coordinator.writes_calendars:
        raise _invalid("calendar_write_missing")


def _wanted_event(
    entity: AmZettelEventCalendar,
    view: dict[str, Any],
    summary: str,
    start: date | datetime,
    end: date | datetime | None,
    description: str | None,
    category: str | None,
    previous_due: dict[str, Any] | None,
) -> WantedEvent:
    """A service's event. Description and category are managed only when the call names them; a
    category calendar always puts its own category (another one would move the event away)."""
    try:
        due = due_from_bounds(start, end, dt_util.get_default_time_zone(), previous_due)
        category_id = resolve_category(view, category) if category is not None else None
    except EventInputError as err:
        raise _from_input_error(err) from err
    own = entity.own_category_id
    if own is not None:
        if category_id is not None and category_id.lower() != own.lower():
            raise _invalid("category_calendar_category")
        category_id = own
    return WantedEvent(
        name=summary.strip(),
        due=due,
        note=clean_note(description),
        category_id=category_id,
        manage_note=description is not None,
        manage_category=category is not None or own is not None,
    )


def _find_item(view: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    return next((i for i in view.get("Items") or [] if str(i.get("Id")).lower() == item_id.lower()), None)


async def _async_put_wanted(
    entity: AmZettelEventCalendar, view: dict[str, Any], item_id: str, wanted: WantedEvent
) -> str:
    """Create the event, change it when it differs, else nothing: "created" / "updated" / "unchanged"."""
    coordinator = entity.coordinator
    existing = _find_item(view, item_id)
    if existing is None:
        await entity.async_write(async_item_mutation(coordinator, view, "add", item_id, wanted.add_body(item_id)))
        return "created"
    changes = wanted.changes(existing)
    if not changes:
        return "unchanged"
    await entity.async_write(async_item_mutation(coordinator, view, "edit", item_id, changes, previous=existing))
    return "updated"


async def async_handle_add_event(entity: Any, call: ServiceCall) -> ServiceResponse:
    calendar = _require_event_calendar(entity)
    _require_calendar_write(calendar)
    data = call.data
    start, end = event_bounds_input(data)
    external_id = data.get(ATTR_EXTERNAL_ID)
    # Idempotent: compare with the CURRENT state; a failed pull raises before anything is written.
    view = await calendar.async_current_view()
    item_id = event_item_id(calendar.list_id, external_id) if external_id else str(uuid.uuid4())
    existing = _find_item(view, item_id) if external_id else None
    wanted = _wanted_event(
        calendar,
        view,
        data[ATTR_SUMMARY],
        start,
        end,
        data.get(ATTR_DESCRIPTION),
        data.get(ATTR_CATEGORY),
        existing.get("Due") if existing else None,
    )
    result = await _async_put_wanted(calendar, view, item_id, wanted)
    return {"event_id": item_id, "result": result}


async def async_handle_sync_events(entity: Any, call: ServiceCall) -> ServiceResponse:
    calendar = _require_event_calendar(entity)
    _require_calendar_write(calendar)
    data = call.data
    zone = dt_util.get_default_time_zone()
    period_start, period_end = window_bounds(data[ATTR_START], data[ATTR_END], dt_util.now(), zone)
    source: str = data[ATTR_SOURCE].strip()
    events: list[dict[str, Any]] = data[ATTR_EVENTS]
    external_ids = [str(e[ATTR_EXTERNAL_ID]).strip() for e in events]
    if len(set(external_ids)) != len(external_ids):
        raise _invalid("sync_duplicate_external_id")
    if not events and not data[ATTR_ALLOW_EMPTY]:
        # A source that is briefly unavailable often delivers nothing; that must not wipe the period.
        raise _invalid("sync_empty")

    # Parse everything before the first write: a bad event fails the call without a half sync.
    parsed: list[tuple[str, dict[str, Any], date | datetime, date | datetime | None]] = []
    for external_id, event in zip(external_ids, events, strict=True):
        try:
            start = parse_bound(event[ATTR_START])
            end = parse_bound(event[ATTR_END]) if event.get(ATTR_END) is not None else None
        except EventInputError as err:
            raise _from_input_error(err) from err
        parsed.append((external_id, event, start, end))

    coordinator = calendar.coordinator
    store = sync_sources(calendar.hass)
    counts = {"created": 0, "updated": 0, "removed": 0, "unchanged": 0}
    entry_id = coordinator.config_entry.entry_id
    async with store.lock:
        await store.async_load()
        # The current state, or no sync at all: removing on a stale state would delete an own event
        # that has since moved out of the period.
        view = await calendar.async_current_view()
        wanted_events = []
        for external_id, event, start, end in parsed:
            item_id = event_item_id(calendar.list_id, external_id, source)
            existing = _find_item(view, item_id)
            wanted = _wanted_event(
                calendar,
                view,
                event[ATTR_SUMMARY],
                start,
                end,
                event.get(ATTR_DESCRIPTION),
                event.get(ATTR_CATEGORY),
                existing.get("Due") if existing else None,
            )
            wanted_events.append((external_id, item_id, wanted))

        owned = store.owned(entry_id, calendar.list_id, source)
        try:
            # Remember ownership BEFORE writing: an event created but not recorded could never be removed.
            owned.update({external_id: item_id for external_id, item_id, _ in wanted_events})
            await store.async_save()
            refresh = False
            for _external_id, item_id, wanted in wanted_events:
                if refresh:
                    view = await calendar.async_current_view()
                    refresh = False
                result = await _async_put_wanted(calendar, view, item_id, wanted)
                counts[result] += 1
                refresh = result != "unchanged"

            delivered = set(external_ids)
            for external_id, item_id in list(owned.items()):
                if external_id in delivered:
                    continue
                if item_id != event_item_id(calendar.list_id, external_id, source):
                    continue  # not provably ours: never touched
                if refresh:
                    view = await calendar.async_current_view()
                    refresh = False
                item = _find_item(view, item_id)
                if item is None:
                    del owned[external_id]  # gone already (deleted in the app)
                    continue
                begins = event_start_instant(item, zone)
                if begins is None or not (period_start <= begins < period_end):
                    continue  # outside the period: not this call's business
                await calendar.async_write(async_item_mutation(coordinator, view, "remove", item_id))
                refresh = True
                del owned[external_id]
                counts["removed"] += 1
        finally:
            store.prune(entry_id, calendar.list_id, source)
            await store.async_save()
    return counts


async def async_handle_add_item(entity: Any, call: ServiceCall) -> ServiceResponse:
    from .todo import AmZettelTodoList, list_type

    if not isinstance(entity, AmZettelTodoList):
        raise _invalid("not_a_list", entity_id=entity.entity_id)
    todo: AmZettelTodoList = entity
    if not todo.coordinator.writes_lists:
        raise _invalid("lists_write_missing")
    view = todo.require_view()
    data = call.data
    item_id = str(uuid.uuid4())
    body: dict[str, Any] = {"Id": item_id, "Name": data[ATTR_NAME].strip(), "Quantity": data.get(ATTR_QUANTITY, 1)}
    if unit := (data.get(ATTR_UNIT) or "").strip():
        body["Unit"] = unit
    if note := clean_note(data.get(ATTR_NOTE)):
        body["Note"] = note
    if category := (data.get(ATTR_CATEGORY) or "").strip():
        # The list's own spelling of a known shopping category; anything else as given.
        known = {(c.get("Name") or "").casefold(): c.get("Name") for c in view.get("ShoppingCategories") or []}
        body["Category"] = (
            known.get(category.casefold(), category) if list_type(view) == LIST_TYPE_SHOPPING else category
        )
    coordinator = todo.coordinator
    answer = await async_write(
        todo.hass, coordinator.config_entry, AREA_LISTS, async_item_mutation(coordinator, view, "add", item_id, body)
    )
    # Shopping merges a same-name open entry into one (server side): the answer names the item it became.
    merged = answer.get("id") if isinstance(answer, dict) else None
    return {"item_id": str(merged or item_id)}


async def async_handle_get_events(entity: Any, call: ServiceCall) -> ServiceResponse:
    calendar = _require_event_calendar(entity)
    zone = dt_util.get_default_time_zone()
    start, end = window_bounds(call.data.get(ATTR_START), call.data.get(ATTR_END), dt_util.now(), zone)
    view = calendar.require_view()
    categories: set[str] | None = None
    if (wanted := call.data.get(ATTR_CATEGORY)) is not None:
        try:
            categories = {resolve_category(view, wanted) or ""}
        except EventInputError as err:
            raise _from_input_error(err) from err
    if (own := calendar.own_category_id) is not None:
        categories = {own} if categories is None else categories & {own}
    generation = calendar.coordinator.access_generation
    events = await calendar.hass.async_add_executor_job(events_data, view, start, end, zone, categories)
    if calendar.coordinator.access_generation != generation:
        events = []
    return {"calendar_id": calendar.list_id, "start": start.isoformat(), "end": end.isoformat(), "events": events}


async def async_handle_get_meals(entity: Any, call: ServiceCall) -> ServiceResponse:
    from .calendar import AmZettelMealPlanCalendar

    if not isinstance(entity, AmZettelMealPlanCalendar):
        raise _invalid("not_a_meal_plan", entity_id=entity.entity_id)
    plan_entity: AmZettelMealPlanCalendar = entity
    zone = dt_util.get_default_time_zone()
    start, end = window_bounds(call.data.get(ATTR_START), call.data.get(ATTR_END), dt_util.now(), zone)
    plan = plan_entity.require_plan()
    meals = meals_data(plan, start, end, zone, plan_entity.language_code)
    return {"meal_plan_id": str(plan["Id"]), "start": start.isoformat(), "end": end.isoformat(), "meals": meals}


async def async_handle_get_items(entity: Any, call: ServiceCall) -> ServiceResponse:
    from .todo import AmZettelTodoList, list_type, recent_purchases

    if not isinstance(entity, AmZettelTodoList):
        raise _invalid("not_a_list", entity_id=entity.entity_id)
    todo: AmZettelTodoList = entity
    view = todo.require_view()
    include_completed = call.data[ATTR_INCLUDE_COMPLETED]
    items = [item_data(view, item) for item in view.get("Items") or []]
    if not include_completed:
        items = [item for item in items if not item["completed"]]
    elif list_type(view) == LIST_TYPE_SHOPPING:
        items.extend(purchase_data(p) for p in recent_purchases(view))
    return {"list_id": todo.list_id, "name": view.get("Name"), "type": list_type(view), "items": items}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once per HA instance."""
    for name, entity_domain, schema, func, response in (
        (SERVICE_ADD_EVENT, "calendar", ADD_EVENT_SCHEMA, async_handle_add_event, SupportsResponse.OPTIONAL),
        (SERVICE_SYNC_EVENTS, "calendar", SYNC_EVENTS_SCHEMA, async_handle_sync_events, SupportsResponse.OPTIONAL),
        (SERVICE_GET_EVENTS, "calendar", GET_EVENTS_SCHEMA, async_handle_get_events, SupportsResponse.ONLY),
        (SERVICE_GET_MEALS, "calendar", GET_MEALS_SCHEMA, async_handle_get_meals, SupportsResponse.ONLY),
        (SERVICE_ADD_ITEM, "todo", ADD_ITEM_SCHEMA, async_handle_add_item, SupportsResponse.OPTIONAL),
        (SERVICE_GET_ITEMS, "todo", GET_ITEMS_SCHEMA, async_handle_get_items, SupportsResponse.ONLY),
    ):
        service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=entity_domain,
            schema=schema,
            func=func,
            supports_response=response,
        )
