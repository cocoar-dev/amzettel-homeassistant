"""Writing events: HA start/end ↔ the Due wire shape, categories, idempotent ids (pure, unit tested).

The Due shape (Due.cs): all-day = `Date` (+ an INCLUSIVE `EndDate` for several days); timed = `Date`,
`Time` ("HH:mm") and `Zone` (IANA) (+ `EndTime`, and `EndDate` only when the end is on another day;
the end shares the start's zone). HA hands an all-day end EXCLUSIVE (the day after), as RFC 5545
does, so one day is taken off at that boundary. The server keeps minutes, not seconds, and drops an
end at or before the start — the mapping does the same, so comparing a stored event with a wanted
one never finds a difference that the server would not keep.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from .const import CATEGORY_LABEL

# Fields of a Due this integration sets; the others (ReminderMinutes, IncludeInSummary) are the
# user's and survive every change.
DUE_TIME_FIELDS = ("Date", "Time", "Zone", "EndDate", "EndTime")


class EventInputError(ValueError):
    """The wanted event cannot be expressed (mixed date/time, end before start …); `key` names why."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


def zoned_time(value: datetime, default_zone: tzinfo) -> datetime:
    """A naive time is local (HA's zone); a fixed offset becomes HA's zone — the Due needs an IANA name."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_zone)
    if not isinstance(value.tzinfo, ZoneInfo):
        # HA defaults to datetime.UTC, not ZoneInfo("UTC"). Fixed-offset zones have no
        # IANA key, so use UTC when the configured zone is not a named zone.
        value = value.astimezone(default_zone if isinstance(default_zone, ZoneInfo) else UTC_ZONE)
    return value


UTC_ZONE = ZoneInfo("UTC")


def is_ambiguous(local: datetime) -> bool:
    """A wall-clock time that occurs twice in its zone (the repeated hour when the clocks go back)."""
    naive = local.replace(tzinfo=None)
    return (
        naive.replace(tzinfo=local.tzinfo, fold=0).utcoffset() != naive.replace(tzinfo=local.tzinfo, fold=1).utcoffset()
    )


def due_from_bounds(
    start: date | datetime,
    end: date | datetime | None,
    default_zone: tzinfo,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """HA bounds → Due. `end` exclusive for all-day; None = one day / a point in time."""
    due: dict[str, Any] = {k: v for k, v in (previous or {}).items() if k not in DUE_TIME_FIELDS}
    if isinstance(start, datetime):
        if end is not None and not isinstance(end, datetime):
            raise EventInputError("event_mixed_bounds")
        begin = zoned_time(start, default_zone)
        zone = begin.tzinfo
        assert isinstance(zone, ZoneInfo)
        # The wall clock of the INSTANT: a time in the skipped hour becomes the real one (02:30 → 03:30),
        # which is also what the backend makes of it (Due.ToInstant: InZoneLeniently).
        begin = begin.astimezone(UTC_ZONE).astimezone(zone)
        finish = zoned_time(end, default_zone).astimezone(UTC_ZONE).astimezone(zone) if end is not None else None
        if finish is not None and finish.astimezone(UTC_ZONE) < begin.astimezone(UTC_ZONE):
            raise EventInputError("event_end_before_start")
        due.update({"Date": begin.date().isoformat(), "Time": begin.strftime("%H:%M"), "Zone": zone.key})
        if finish is not None:
            # The end is a wall-clock time in the START's zone (there is no EndZone).
            local_begin = begin.replace(tzinfo=None, second=0, microsecond=0)
            local_finish = finish.replace(tzinfo=None, second=0, microsecond=0)
            if local_finish > local_begin:
                due["EndTime"] = local_finish.strftime("%H:%M")
                if local_finish.date() != local_begin.date():
                    due["EndDate"] = local_finish.date().isoformat()
        if (is_ambiguous(begin) or (finish is not None and is_ambiguous(finish))) and (
            due_key(due) != due_key(previous) or begin.fold or (finish is not None and finish.fold)
        ):
            # Preserve an unchanged stored first occurrence of a repeated hour. Otherwise
            # use UTC: Due has no fold/offset, and the backend always chooses the earlier
            # instant. Rebuilding in UTC terminates here because UTC has no repeated hour.
            return due_from_bounds(
                begin.astimezone(UTC_ZONE),
                finish.astimezone(UTC_ZONE) if finish is not None else None,
                default_zone,
                previous,
            )
        return due
    if end is not None and isinstance(end, datetime):
        raise EventInputError("event_mixed_bounds")
    due["Date"] = start.isoformat()
    if end is not None:
        if end <= start:
            raise EventInputError("event_end_before_start")
        last = end - timedelta(days=1)
        if last > start:
            due["EndDate"] = last.isoformat()
    return due


def due_key(due: dict[str, Any] | None) -> tuple[Any, ...]:
    """What decides whether two Dues are the same event time (as the server normalises them)."""
    if not due:
        return ()
    timed = bool(due.get("Time") and due.get("Zone"))
    if not timed:
        end = due.get("EndDate")
        return (due.get("Date"), None, None, end if end and end > due.get("Date", "") else None, None)
    end_time = due.get("EndTime") or None
    end_date = due.get("EndDate") if end_time else None
    if end_date == due.get("Date"):
        end_date = None
    return (due.get("Date"), (due.get("Time") or "")[:5], due.get("Zone"), end_date, (end_time or "")[:5] or None)


def event_item_id(calendar_id: str, external_id: str, source: str | None = None) -> str:
    """Decision 3: UUIDv5 of the calendar id and an unambiguous JSON encoding of who names the event:
    `["add_event", external_id]` or `["sync", source, external_id]`. A plain "source:id" would give
    ("waste", "district:42") and ("waste:district", "42") the same event; the fixed first element
    keeps `add_event` and every source apart."""
    name = json.dumps(["add_event", external_id] if source is None else ["sync", source, external_id])
    return str(uuid.uuid5(uuid.UUID(calendar_id), name))


def resolve_category(view: dict[str, Any], value: str | None) -> str | None:
    """A category of the calendar by id or (case-insensitively) by name; None for no value."""
    if value is None or not str(value).strip():
        return None
    wanted = str(value).strip()
    categories = view.get("EventCategories") or []
    for category in categories:
        if str(category.get("Id")).lower() == wanted.lower():
            return str(category["Id"])
    for category in categories:
        if (category.get("Name") or "").strip().casefold() == wanted.casefold():
            return str(category["Id"])
    names = ", ".join(sorted((c.get("Name") or str(c.get("Id"))) for c in categories)) or "—"
    raise EventInputError("unknown_category", category=wanted, categories=names)


def note_from_description(view: dict[str, Any], item: dict[str, Any] | None, description: str | None) -> str | None:
    """The note of an edited HA description: the calendar shows the category as a last line
    ("Kategorie: Müll"); handed back unchanged, that line is not part of the note."""
    if not description or not description.strip():
        return None
    lines = description.strip().split("\n")
    category_id = (item or {}).get("CategoryId")
    if category_id:
        name = next(
            (c.get("Name") for c in view.get("EventCategories") or [] if str(c.get("Id")) == str(category_id)),
            None,
        )
        if name and lines and lines[-1].strip() in {f"{label}: {name}" for label in CATEGORY_LABEL.values()}:
            lines = lines[:-1]
    return "\n".join(lines).strip() or None


def clean_note(value: str | None) -> str | None:
    """The server trims notes and stores an empty one as none."""
    return value.strip() if value and value.strip() else None


@dataclass(slots=True)
class WantedEvent:
    """An event as a service or the calendar entity wants it. `note`/`category_id` None + not
    `manage_*` = leave what the event has."""

    name: str
    due: dict[str, Any]
    note: str | None = None
    category_id: str | None = None
    manage_note: bool = True
    manage_category: bool = True

    def add_body(self, item_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"Id": item_id, "Name": self.name, "Quantity": 1, "Due": self.due}
        if self.note:
            body["Note"] = self.note
        if self.category_id:
            body["CategoryId"] = self.category_id
        return body

    def changes(self, existing: dict[str, Any]) -> dict[str, Any]:
        """The fields of `existing` that differ from what is wanted (empty = nothing to do)."""
        changes: dict[str, Any] = {}
        if self.name.strip() != (existing.get("Name") or "").strip():
            changes["Name"] = self.name.strip()
        previous = existing.get("Due")
        if due_key(self.due) != due_key(previous):
            # keep the user's ReminderMinutes / IncludeInSummary
            changes["Due"] = {
                **{k: v for k, v in (previous or {}).items() if k not in DUE_TIME_FIELDS},
                **self.due,
            }
        if self.manage_note and clean_note(self.note) != clean_note(existing.get("Note")):
            changes["Note"] = clean_note(self.note)
        current_category = str(existing["CategoryId"]).lower() if existing.get("CategoryId") else None
        wanted_category = self.category_id.lower() if self.category_id else None
        if self.manage_category and wanted_category != current_category:
            changes["CategoryId"] = self.category_id
        return changes


def parse_bound(value: Any) -> date | datetime:
    """A date or a datetime from a service field (selectors hand ISO strings or objects)."""
    if isinstance(value, (date, datetime)):
        return value
    text = str(value).strip()
    if len(text) == 10:
        parsed_date = dt_util.parse_date(text)
        if parsed_date is not None:
            return parsed_date
    parsed = dt_util.parse_datetime(text)
    if parsed is None:
        raise EventInputError("invalid_time", value=text)
    return parsed
