"""amZettel lists (Shopping / Checklist / Todo) as HA to-do lists.

`summary` is the item's name (so Assist finds "Milch"); the amount (when it says more than "one")
is the first line of `description`, the note follows.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AmZettelConfigEntry
from .const import (
    DOMAIN,
    LIST_TYPE_SHOPPING,
    LIST_TYPE_TODO,
    PURCHASE_UID_PREFIX,
    TODO_LIST_TYPES,
)
from .coordinator import AmZettelCoordinator
from .entity import track_entities
from .event_write import UTC_ZONE, is_ambiguous, zoned_time
from .naming import display_name
from .selection import KIND_LISTS
from .writes import AREA_LISTS, async_current_list, async_item_mutation, async_write

_LOGGER = logging.getLogger(__name__)

# HA merges the service data into the item it read BEFORE calling us; only one write at a time per
# entity keeps a second write from merging into a state the first one has already changed.
PARALLEL_UPDATES = 1

RECENT_PURCHASE_WINDOW = timedelta(hours=2)


# ── pure mapping (unit tested) ─────────────────────────────────────────────────────────────


def list_type(view: dict[str, Any]) -> str:
    """Absent `Type` = Shopping (types.ts)."""
    return view.get("Type") or LIST_TYPE_SHOPPING


def _is_german(language: str | None) -> bool:
    return (language or "").lower().startswith("de")


def format_number(quantity: Any, language: str | None = "en") -> str:
    """2.0 → "2", 0.25 → "0.25" ("0,25" in German); no exponent, at most 3 decimals."""
    try:
        value = float(quantity)
    except TypeError, ValueError:
        return str(quantity)
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text.replace(".", ",") if _is_german(language) else text


def render_amount(quantity: Any, unit: str | None, language: str | None = "en") -> str:
    """ "2 L" / "6" — or "" when the amount says nothing (one, no unit)."""
    unit = (unit or "").strip()
    try:
        is_one = float(quantity if quantity is not None else 1) == 1
    except TypeError, ValueError:
        is_one = False
    if is_one and not unit:
        return ""
    amount = format_number(quantity if quantity is not None else 1, language)
    return f"{amount} {unit}" if unit else amount


def render_description(amount: str, note: str | None) -> str | None:
    """Amount line, then the note."""
    lines = [part for part in (amount, (note or "").strip()) if part]
    return "\n".join(lines) or None


def note_from_description(description: str | None, amount: str) -> str | None:
    """The note of an edited description: a first line equal to the rendered amount is cut off."""
    if not description:
        return None
    first, _, rest = description.partition("\n")
    if amount and first.strip() == amount:
        return rest.strip() or None
    return description.strip() or None


def parse_instant(value: str | None) -> datetime | None:
    """ISO-8601 timestamp → aware datetime."""
    if not value:
        return None
    return dt_util.parse_datetime(value)


def due_to_ha(due: dict[str, Any] | None) -> date | datetime | None:
    """`{ Date }` → date; `{ Date, Time, Zone }` → aware datetime in its own zone."""
    if not due or not due.get("Date"):
        return None
    day = dt_util.parse_date(due["Date"])
    if day is None:
        return None
    clock = dt_util.parse_time(due["Time"]) if due.get("Time") and due.get("Zone") else None
    if clock is None:
        return day
    zone = dt_util.get_time_zone(due["Zone"]) or dt_util.get_default_time_zone()
    return datetime.combine(day, clock, tzinfo=zone)


def due_from_ha(value: date | datetime | None, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """HA due → the Due wire shape.

    The previous Due is copied and only `Date`/`Time`/`Zone` are replaced, so `ReminderMinutes`,
    `IncludeInSummary` … survive. An end moves by the same distance as the start; switching between
    all-day and timed drops the end (an all-day end is an inclusive date, a timed one a clock time).
    """
    if value is None:
        return None
    due: dict[str, Any] = dict(previous) if previous else {}
    if isinstance(value, datetime):
        value = zoned_time(value, dt_util.get_default_time_zone())
        # Same rule as the calendar write (event_write.due_from_bounds): a skipped wall time becomes the
        # real one, a repeated one goes to UTC — the backend would resolve it to the earlier instant.
        value = value.astimezone(UTC_ZONE).astimezone(value.tzinfo)
        if is_ambiguous(value):
            value = value.astimezone(UTC_ZONE)
        zone = value.tzinfo
        assert isinstance(zone, ZoneInfo)
        due.update({"Date": value.date().isoformat(), "Time": value.strftime("%H:%M"), "Zone": zone.key})
    else:
        due["Date"] = value.isoformat()
        due.pop("Time", None)
        due.pop("Zone", None)
    if not previous:
        return due

    was_timed = bool(previous.get("Time") and previous.get("Zone"))
    is_timed = isinstance(value, datetime)
    old_day = dt_util.parse_date(previous.get("Date") or "")
    if was_timed != is_timed or old_day is None:
        due.pop("EndDate", None)
        due.pop("EndTime", None)
        return due

    if not is_timed:
        old_end = dt_util.parse_date(previous.get("EndDate") or "")
        if old_end is not None:
            assert isinstance(value, date)
            due["EndDate"] = (old_end + (value - old_day)).isoformat()
        return due

    old_clock = dt_util.parse_time(previous["Time"])
    end_clock = dt_util.parse_time(previous.get("EndTime") or "")
    if old_clock is None or end_clock is None:
        return due
    end_day = dt_util.parse_date(previous.get("EndDate") or "") or old_day
    duration = datetime.combine(end_day, end_clock) - datetime.combine(old_day, old_clock)
    assert isinstance(value, datetime)
    new_end = value.replace(tzinfo=None) + duration
    due["EndTime"] = new_end.strftime("%H:%M")
    if new_end.date() != value.date():
        due["EndDate"] = new_end.date().isoformat()
    else:
        due.pop("EndDate", None)
    return due


def recent_purchases(view: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """The web's rule (ListWorkspace.vue `recentOf`): bought after the cart close AND today or within
    the last two hours; newest first."""
    now = now or dt_util.now()
    closed = parse_instant(view.get("CartClosedAt"))
    today = dt_util.as_local(now).date()
    result = []
    for purchase in view.get("RecentPurchases") or []:
        bought = parse_instant(purchase.get("CheckedAt"))
        if bought is None:
            continue
        if closed is not None and bought <= closed:
            continue
        if dt_util.as_local(bought).date() != today and now - bought >= RECENT_PURCHASE_WINDOW:
            continue
        result.append(purchase)
    result.sort(key=lambda p: parse_instant(p["CheckedAt"]) or now, reverse=True)
    return result


def todo_items(view: dict[str, Any], language: str | None = "en", now: datetime | None = None) -> list[TodoItem]:
    """All to-do items of one list."""
    kind = list_type(view)
    items: list[TodoItem] = []
    for item in view.get("Items") or []:
        done = kind != LIST_TYPE_SHOPPING and bool(item.get("Done"))
        items.append(
            TodoItem(
                uid=str(item["Id"]),
                summary=item.get("Name", ""),
                status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION,
                description=render_description(
                    render_amount(item.get("Quantity"), item.get("Unit"), language), item.get("Note")
                ),
                due=due_to_ha(item.get("Due")) if kind == LIST_TYPE_TODO else None,
                completed=parse_instant(item.get("DoneAt")) if done else None,
            )
        )
    if kind == LIST_TYPE_SHOPPING:
        for purchase in recent_purchases(view, now):
            items.append(
                TodoItem(
                    uid=f"{PURCHASE_UID_PREFIX}{purchase['CheckId']}",
                    summary=purchase.get("Name", ""),
                    status=TodoItemStatus.COMPLETED,
                    description=render_description(
                        render_amount(purchase.get("Quantity"), purchase.get("Unit"), language), None
                    ),
                    completed=parse_instant(purchase.get("CheckedAt")),
                )
            )
    return items


def _is_status_only(item: TodoItem, current: TodoItemStatus, name: str | None) -> bool:
    """A bare status change, as HA's intent handlers send it (HassListCompleteItem builds a new
    TodoItem with uid, summary and status only): the status differs, the title is unchanged and
    description AND due are both missing. Read as "delete note and due" it would wipe both.
    Known limit: `todo.update_item` merges explicit nulls into the same shape, so a SINGLE call that
    completes an item AND clears both its note and its due (or the only one it has) only completes it.
    The two cannot be told apart; Assist wins because it is the common case and losing data there is
    silent. Clearing first and completing in a second call works.
    """
    return (
        item.status is not None
        and item.status != current
        and (item.summary is None or item.summary.strip() == (name or ""))
        and item.description is None
        and item.due is None
    )


# ── platform ───────────────────────────────────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmZettelConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One to-do entity per Shopping/Checklist/Todo list."""
    coordinator = entry.runtime_data.coordinator
    sub = str(entry.unique_id)

    def current_ids() -> list[str]:
        return [
            list_id
            for list_id, view in coordinator.data.lists.items()
            if list_type(view) in TODO_LIST_TYPES and coordinator.visibility.is_visible(KIND_LISTS, list_id)
        ]

    track_entities(
        hass,
        entry,
        "todo",
        current_ids,
        lambda list_id: f"{sub}_{list_id}",
        lambda list_id: AmZettelTodoList(coordinator, entry, list_id),
        async_add_entities,
        coordinator.async_add_listener,
        lambda: coordinator.data.authoritative,
    )


class AmZettelTodoList(CoordinatorEntity[AmZettelCoordinator], TodoListEntity):
    """One amZettel list."""

    # Called by its amZettel name alone ("Wocheneinkauf", not "amZettel Wocheneinkauf"): Assist matches
    # list names against the full friendly name, and nobody says a device prefix. HA prefixes the DEVICE
    # name for both naming styles, so the entity has no device at all; without one, modern naming
    # (has_entity_name) makes the entity name the full name.
    _attr_has_entity_name = True

    def __init__(self, coordinator: AmZettelCoordinator, entry: AmZettelConfigEntry, list_id: str) -> None:
        super().__init__(coordinator)
        self._list_id = list_id
        self._attr_unique_id = f"{entry.unique_id}_{list_id}"

    @property
    def _view(self) -> dict[str, Any] | None:
        return self.coordinator.data.lists.get(self._list_id)

    @property
    def list_id(self) -> str:
        """The amZettel list this entity shows."""
        return self._list_id

    @property
    def _language(self) -> str | None:
        return self.hass.config.language if self.hass else None

    @property
    def available(self) -> bool:
        return super().available and self._view is not None

    @property
    def name(self) -> str | None:
        view = self._view
        if view is None:
            return None
        return display_name(view.get("Name") or "", view, self.coordinator.user_id, self._language)

    @property
    def supported_features(self) -> TodoListEntityFeature:
        # Without `lists.write` the token may not change anything: offer no write at all.
        if not self.coordinator.writes_lists:
            return TodoListEntityFeature(0)
        features = (
            TodoListEntityFeature.CREATE_TODO_ITEM
            | TodoListEntityFeature.UPDATE_TODO_ITEM
            | TodoListEntityFeature.DELETE_TODO_ITEM
            | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
        )
        view = self._view
        if view and list_type(view) == LIST_TYPE_TODO:
            features |= TodoListEntityFeature.SET_DUE_DATE_ON_ITEM | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        return features

    @property
    def todo_items(self) -> list[TodoItem] | None:
        view = self._view
        return todo_items(view, self._language) if view else None

    # ── writes ────────────────────────────────────────────────────────────────────────────

    def require_view(self) -> dict[str, Any]:
        view = self._view
        if view is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="list_gone")
        return view

    def _find_item(self, uid: str) -> dict[str, Any] | None:
        for item in self.require_view().get("Items") or []:
            if str(item.get("Id")) == uid:
                return item
        return None

    async def _call(self, coro: Any) -> Any:
        return await async_write(self.hass, self.coordinator.config_entry, AREA_LISTS, coro)

    async def async_create_todo_item(self, item: TodoItem) -> None:
        view = self.require_view()
        body: dict[str, Any] = {
            "Id": str(uuid.uuid4()),
            "Name": (item.summary or "").strip(),
            "Quantity": 1,
        }
        if item.description:
            body["Note"] = item.description
        if list_type(view) == LIST_TYPE_TODO and item.due is not None:
            body["Due"] = due_from_ha(item.due, None)
        await self._call(async_item_mutation(self.coordinator, view, "add", body["Id"], body))

    async def async_update_todo_item(self, item: TodoItem) -> None:
        view = self.require_view()
        kind = list_type(view)
        uid = item.uid or ""
        coordinator = self.coordinator
        language = self._language

        if uid.startswith(PURCHASE_UID_PREFIX):
            check_id = uid[len(PURCHASE_UID_PREFIX) :]
            purchase = next((p for p in recent_purchases(view) if str(p.get("CheckId")) == check_id), None)
            if purchase is not None:
                name = purchase.get("Name", "")
                described = render_description(
                    render_amount(purchase.get("Quantity"), purchase.get("Unit"), language), None
                )
                if item.summary not in (None, name) or (item.description or None) != described:
                    raise HomeAssistantError(translation_domain=DOMAIN, translation_key="purchase_readonly")
            if item.status == TodoItemStatus.NEEDS_ACTION:
                await self._call(async_item_mutation(coordinator, view, "unbuy", check_id))
            return

        existing = self._find_item(uid)
        if existing is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_gone")

        was_done = kind != LIST_TYPE_SHOPPING and bool(existing.get("Done"))
        current = TodoItemStatus.COMPLETED if was_done else TodoItemStatus.NEEDS_ACTION
        if not _is_status_only(item, current, existing.get("Name")):
            changes: dict[str, Any] = {}
            if item.summary is not None:
                new_name = item.summary.strip()
                if new_name and new_name != existing.get("Name"):
                    changes["Name"] = new_name
            amount = render_amount(existing.get("Quantity"), existing.get("Unit"), language)
            note = note_from_description(item.description, amount)
            if note != (existing.get("Note") or None):
                changes["Note"] = note
            if kind == LIST_TYPE_TODO and item.due != due_to_ha(existing.get("Due")):
                changes["Due"] = due_from_ha(item.due, existing.get("Due"))
            if changes:
                await self._call(async_item_mutation(coordinator, view, "edit", uid, changes, previous=existing))
                if item.status != current:
                    view = await self._current_view()
                    existing = self._find_item(uid)
                    if existing is None:
                        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="item_gone")
                    was_done = kind != LIST_TYPE_SHOPPING and bool(existing.get("Done"))

        if item.status == TodoItemStatus.COMPLETED and not was_done:
            if kind == LIST_TYPE_SHOPPING:
                await self._call(async_item_mutation(coordinator, view, "buy", uid))
            else:
                await self._call(async_item_mutation(coordinator, view, "done", uid, {"Done": True}))
        elif item.status == TodoItemStatus.NEEDS_ACTION and was_done:
            await self._call(async_item_mutation(coordinator, view, "done", uid, {"Done": False}))

    async def _current_view(self) -> dict[str, Any]:
        coordinator = self.coordinator
        return await async_current_list(self.hass, coordinator.config_entry, coordinator, self._list_id)

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        # Bought entries live in the purchase history; the API has no way to delete one (only to put it
        # back). On a shopping list they are the only completed entries, so HA's "remove completed"
        # sends exactly those: refuse the whole request before deleting anything, with an explanation.
        if any(uid.startswith(PURCHASE_UID_PREFIX) for uid in uids):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="purchase_not_deletable")
        for index, uid in enumerate(uids):
            view = await self._current_view() if index else self.require_view()
            await self._call(async_item_mutation(self.coordinator, view, "remove", uid))
