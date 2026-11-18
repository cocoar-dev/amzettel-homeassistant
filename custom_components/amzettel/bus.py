"""Events on the HA bus from comparing the previous and the new coordinator state.

* Only objects shown in HA: changes to hidden objects are skipped.
* No flood: an object yields events only when it was ALREADY shown in the previous state. The first
  load, a reload, an object that is new, newly chosen or visible again only become the baseline.
  Replayed SSE changes are compared with the last state. A full resnapshot sets a new baseline.
* Who: the API names the buyer of a shopping entry (`by_user_id`); for adding, removing and the
  check-off of checklists it names nobody.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Final

from homeassistant.core import HomeAssistant, callback

from .const import LIST_TYPE_EVENTS, LIST_TYPE_SHOPPING, TODO_LIST_TYPES
from .coordinator import AmZettelCoordinator, AmZettelData
from .selection import KIND_CALENDARS, KIND_LISTS, KIND_MEAL_PLANS, Visibility

EVENT_ITEM_ADDED: Final = "amzettel_item_added"
EVENT_ITEM_COMPLETED: Final = "amzettel_item_completed"
EVENT_ITEM_REMOVED: Final = "amzettel_item_removed"
EVENT_EVENT_CREATED: Final = "amzettel_event_created"
EVENT_EVENT_CHANGED: Final = "amzettel_event_changed"
EVENT_EVENT_REMOVED: Final = "amzettel_event_removed"
EVENT_MEAL_PLANNED: Final = "amzettel_meal_planned"

# What makes an event "changed" (photos, moves inside the stream … are not).
EVENT_FIELDS = ("Name", "Note", "Due", "Recurrence", "CategoryId", "PersonIds")

Fire = Callable[[str, dict[str, Any]], None]


def _list_type(view: dict[str, Any]) -> str:
    return view.get("Type") or LIST_TYPE_SHOPPING


def _shown(data: AmZettelData, visibility: Visibility) -> dict[str, dict[str, Any]]:
    """The lists and calendars shown in HA, by id."""
    shown = {}
    for list_id, view in data.lists.items():
        kind = KIND_CALENDARS if _list_type(view) == LIST_TYPE_EVENTS else KIND_LISTS
        if _list_type(view) in (*TODO_LIST_TYPES, LIST_TYPE_EVENTS) and visibility.is_visible(kind, list_id):
            shown[list_id] = view
    return shown


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _item_payload(view: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return {
        "list_id": str(view["Id"]),
        "list_name": view.get("Name"),
        "item_id": str(item.get("Id") or item.get("ItemId")),
        "name": item.get("Name", ""),
        "quantity": item.get("Quantity", 1),
        "unit": item.get("Unit"),
        "category": item.get("Category"),
        "note": item.get("Note"),
    }


def _event_payload(view: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    from .todo import due_to_ha

    due = item.get("Due") or {}
    start = due_to_ha(due)
    category = next(
        (c.get("Name") for c in view.get("EventCategories") or [] if str(c.get("Id")) == str(item.get("CategoryId"))),
        None,
    )
    return {
        "calendar_id": str(view["Id"]),
        "calendar_name": view.get("Name"),
        "event_id": str(item["Id"]),
        "summary": item.get("Name", ""),
        "start": _iso(start),
        "all_day": start is not None and not isinstance(start, datetime),
        "end_date": due.get("EndDate"),
        "end_time": due.get("EndTime"),
        "note": item.get("Note"),
        "category_id": str(item["CategoryId"]) if item.get("CategoryId") else None,
        "category": category,
        "recurring": bool((item.get("Recurrence") or {}).get("Rule")),
    }


def _meal_payload(plan: dict[str, Any], entry: dict[str, Any], language: str) -> dict[str, Any]:
    from .calendar import meal_label, meal_timing

    value, zone_name = meal_timing(entry)
    return {
        "meal_plan_id": str(plan["Id"]),
        "meal_plan_name": plan.get("Name"),
        "meal_id": str(entry["Id"]),
        "date": entry.get("Date"),
        "time": value,
        "time_zone": zone_name,
        "slot": meal_label(plan, entry, language),
        "dishes": [d.get("Name", "") for d in entry.get("Dishes") or []],
        "people": entry.get("People"),
        "note": entry.get("Note"),
    }


def diff_list(old: dict[str, Any], new: dict[str, Any], fire: Fire) -> None:
    """Items of one to-do list: added, completed (checked off / bought), removed."""
    kind = _list_type(new)
    before = {str(i["Id"]): i for i in old.get("Items") or []}
    after = {str(i["Id"]): i for i in new.get("Items") or []}
    bought_before = {str(p.get("CheckId")) for p in old.get("RecentPurchases") or []}
    bought_now = {
        str(p.get("ItemId")): p for p in new.get("RecentPurchases") or [] if str(p.get("CheckId")) not in bought_before
    }
    for item_id, item in after.items():
        if item_id not in before:
            fire(EVENT_ITEM_ADDED, _item_payload(new, item))
        elif kind != LIST_TYPE_SHOPPING and item.get("Done") and not before[item_id].get("Done"):
            fire(EVENT_ITEM_COMPLETED, {**_item_payload(new, item), "by_user_id": None})
    for item_id, item in before.items():
        if item_id in after:
            continue
        if kind == LIST_TYPE_SHOPPING and item_id in bought_now:
            purchase = bought_now[item_id]
            payload = {**_item_payload(new, item), "quantity": purchase.get("Quantity", item.get("Quantity", 1))}
            payload["by_user_id"] = str(purchase["ByUserId"]) if purchase.get("ByUserId") else None
            fire(EVENT_ITEM_COMPLETED, payload)
        else:
            fire(EVENT_ITEM_REMOVED, _item_payload(new, item))


def diff_calendar(old: dict[str, Any], new: dict[str, Any], fire: Fire) -> None:
    """Events of one calendar: created, changed, removed."""
    before = {str(i["Id"]): i for i in old.get("Items") or []}
    after = {str(i["Id"]): i for i in new.get("Items") or []}
    for item_id, item in after.items():
        previous = before.get(item_id)
        if previous is None:
            fire(EVENT_EVENT_CREATED, _event_payload(new, item))
        elif any(previous.get(f) != item.get(f) for f in EVENT_FIELDS):
            fire(EVENT_EVENT_CHANGED, _event_payload(new, item))
    for item_id, item in before.items():
        if item_id not in after:
            fire(EVENT_EVENT_REMOVED, _event_payload(new, item))


def diff_plan(old: dict[str, Any], new: dict[str, Any], fire: Fire, language: str) -> None:
    """Meals newly planned in one plan."""
    before = {str(e["Id"]) for e in old.get("Entries") or []}
    for entry in new.get("Entries") or []:
        if str(entry["Id"]) not in before:
            fire(EVENT_MEAL_PLANNED, _meal_payload(new, entry, language))


def diff_states(old: AmZettelData, new: AmZettelData, visibility: Visibility, fire: Fire, language: str = "en") -> None:
    """Every event between two states — only for objects shown in BOTH (no flood)."""
    old_shown, new_shown = _shown(old, visibility), _shown(new, visibility)
    for list_id, view in new_shown.items():
        previous = old_shown.get(list_id)
        if previous is None or previous is view:
            continue  # first sight (baseline) or untouched by this update
        if _list_type(view) == LIST_TYPE_EVENTS:
            if _list_type(previous) == LIST_TYPE_EVENTS:
                diff_calendar(previous, view, fire)
        elif _list_type(previous) in TODO_LIST_TYPES:
            diff_list(previous, view, fire)
    for plan_id, plan in new.meal_plans.items():
        previous = old.meal_plans.get(plan_id)
        if previous is None or previous is plan or not visibility.is_visible(KIND_MEAL_PLANS, plan_id):
            continue
        diff_plan(previous, plan, fire, language)


class BusEvents:
    """Holds the last state of one coordinator and fires the differences of every new one."""

    def __init__(self, hass: HomeAssistant, coordinator: AmZettelCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._last: AmZettelData | None = coordinator.data

    @callback
    def handle_update(self) -> None:
        new = self._coordinator.data
        old, self._last = self._last, new
        if old is None or new is None or old is new or new.baseline or not old.authoritative:
            return
        language = "de" if (self._hass.config.language or "").lower().startswith("de") else "en"
        diff_states(old, new, self._coordinator.visibility, self._fire, language)

    def _fire(self, event_type: str, data: dict[str, Any]) -> None:
        data = {**data, "config_entry_id": self._coordinator.config_entry.entry_id}
        self._hass.bus.async_fire(event_type, data)
