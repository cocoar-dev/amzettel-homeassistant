"""Expansion of amZettel event series (RRULE + RDATE/EXDATE) inside a time window.

The stored format is the one of the web's `recurrence.ts` and the server's `RecurrenceSeries.cs`:
`Recurrence = { Rule, RDateLines, ExDateLines }`, `Rule` an RRULE (with or without the `RRULE:`
prefix), the lines RFC 5545 content lines such as

    EXDATE;VALUE=DATE:20261012,20261019
    EXDATE;TZID=Europe/Vienna:20261012T080000

DTSTART is the item's `Due`. Like the server, every value is compared as a LOCAL wall-clock
moment in the series' zone (all-day values as midnight): a timed series repeats at the same
local time across DST. Behaviour mirrored from the backend (Ical.Net 5.2.3, checked 2026-11-14):

* A date-only UNTIL on a timed series is inclusive (the whole day) — `RecurrenceSeries.InclusiveUntil`.
* An UNTIL that cannot be read is IGNORED (Ical.Net expands the series without an end).
* A DTSTART that does not match the rule is NOT an occurrence, and COUNT counts the matching
  occurrences only (Ical.Net 5.2.3 and the web's rrule-temporal with `includeDtstart = false`).
* A local time that does not exist (DST gap) moves forward by the gap; an ambiguous one takes the
  earlier instant (NodaTime `InZoneLeniently`, Temporal `compatible`) — see `resolve_local`.

Expansion is bounded: dateutil only checks UNTIL on produced occurrences, so a rule that never
matches (`FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30`) would scan up to the year 9999. The bounded rule
below stops at the year after the window and after a time budget per series.
"""

from __future__ import annotations

import logging
import re
import time as _time
import types
from datetime import UTC, date, datetime, time, tzinfo
from zoneinfo import ZoneInfo

from dateutil import rrule as _rr
from dateutil.rrule import rrulestr

_LOGGER = logging.getLogger(__name__)

_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DATETIME = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")

MAX_OCCURRENCES = 5000
# Candidates inside the window (from the — already widened — window start on). The historical
# phase before it is bounded by the deadline and the "strictly increasing" check alone, plus a
# generous emergency cap: an old but valid dense series must not lose its occurrences.
MAX_CANDIDATES = 100_000
MAX_HISTORICAL_CANDIDATES = 5_000_000
# Wall-clock budget for expanding one series (it runs in the executor, never on the loop).
SERIES_TIME_BUDGET = 0.25

_logged_aborts: set[str] = set()


def resolve_local(naive: datetime, zone: tzinfo) -> datetime:
    """A wall-clock moment in `zone` as an aware datetime, resolved like the backend.

    Non-existent (DST gap) → shifted forward by the gap; ambiguous → the earlier instant. Python's
    `fold=0` already picks those instants; the round trip through UTC normalises the wall clock
    (02:30 in a gap becomes 03:30).
    """
    return naive.replace(tzinfo=zone, fold=0).astimezone(UTC).astimezone(zone)


def instant(value: date | datetime, default_zone: tzinfo) -> datetime:
    """UTC instant of an event bound (an all-day date = local midnight in `default_zone`)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = resolve_local(value, default_zone)
        return value.astimezone(UTC)
    return resolve_local(datetime.combine(value, time()), default_zone).astimezone(UTC)


def _content_separator(line: str) -> int:
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            return index
    return -1


def _parameters(head: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in head.split(";")[1:]:
        key, sep, value = part.partition("=")
        if sep:
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            result[key.upper()] = value
    return result


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ValueError, KeyError, OSError:
        return None


def parse_values(lines: list[str] | None, series_zone: ZoneInfo | None) -> list[datetime]:
    """RDATE/EXDATE content lines → naive LOCAL datetimes in the series' zone.

    `VALUE=DATE` / 8-digit tokens → midnight; a `TZID` token in another zone and a UTC (`Z`)
    token are converted into the series' zone; a token without zone takes the series' zone.
    Unreadable lines are skipped (an EXDATE that matches nothing is harmless).
    """
    values: list[datetime] = []
    for raw in lines or []:
        separator = _content_separator(raw)
        if separator < 0:
            continue
        params = _parameters(raw[:separator])
        is_date = params.get("VALUE", "").upper() == "DATE"
        token_zone = _zone(params.get("TZID"))
        for raw_token in raw[separator + 1 :].split(","):
            token = raw_token.strip().split("/", 1)[0]  # PERIOD → its start
            if not token:
                continue
            if is_date or _DATE.match(token):
                if m := _DATE.match(token[:8]):
                    try:
                        values.append(datetime(int(m[1]), int(m[2]), int(m[3])))
                    except ValueError:
                        continue
                continue
            m = _DATETIME.match(token)
            if not m:
                continue
            try:
                local = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]))
            except ValueError:
                continue
            if m[7] == "Z":
                if series_zone is None:
                    continue
                values.append(local.replace(tzinfo=UTC).astimezone(series_zone).replace(tzinfo=None))
            elif token_zone is not None and series_zone is not None and token_zone != series_zone:
                values.append(local.replace(tzinfo=token_zone).astimezone(series_zone).replace(tzinfo=None))
            else:
                values.append(local)
    return values


def _rule_parts(rule: str) -> list[tuple[str, str]]:
    body = rule[6:] if rule.upper().startswith("RRULE:") else rule
    parts: list[tuple[str, str]] = []
    for part in body.split(";"):
        key, sep, value = part.partition("=")
        if sep and key:
            parts.append((key.strip().upper(), value.strip()))
    return parts


def _valid_until(value: str) -> bool:
    try:
        if _DATE.match(value):
            datetime.strptime(value, "%Y%m%d")
            return True
        m = _DATETIME.match(value)
        if m:
            datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
            return True
    except ValueError:
        return False
    return False


def normalize_rule(rule: str, all_day: bool, series_zone: ZoneInfo | None) -> str:
    """The rule body for dateutil with a NAIVE local DTSTART.

    UNTIL is made inclusive and local: a date-only UNTIL on a timed series becomes the last second
    of that day; a UTC UNTIL becomes the local moment in the series' zone; on an all-day series a
    date-time UNTIL is cut to its date. An unreadable UNTIL is dropped (Ical.Net ignores it).
    """
    out: list[str] = []
    for key, value in _rule_parts(rule):
        if key == "UNTIL":
            until = value.upper()
            if not _valid_until(until):
                continue
            if all_day:
                value = until[:8]
            elif len(until) == 8:
                value = f"{until}T235959"
            elif until.endswith("Z") and series_zone is not None:
                utc = datetime.strptime(until[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
                value = utc.astimezone(series_zone).strftime("%Y%m%dT%H%M%S")
            else:
                value = until.rstrip("Z")
        out.append(f"{key}={value}")
    return ";".join(out)


class _Abort(Exception):
    """Stop the expansion of one series."""

    def __init__(self, timed_out: bool) -> None:
        super().__init__()
        self.timed_out = timed_out


def _bounded(rule: _rr.rrule, limit_year: int, deadline: float) -> _rr.rrule:
    """A copy of `rule` that stops at `limit_year` and at `deadline`.

    dateutil re-builds its per-period info whenever the year or month changes; a subclass of that
    helper, bound into a private copy of `rrule._iter`'s globals, checks the bounds there — even
    when the rule never produces an occurrence. No global state is patched.
    """

    class _Info(_rr._iterinfo):  # type: ignore[name-defined,misc]
        # No own __slots__: dateutil's __init__ resets every name in `self.__slots__`.
        def rebuild(self, year: int, month: int) -> None:
            if year > limit_year:
                raise _Abort(timed_out=False)
            if _time.monotonic() > deadline:
                raise _Abort(timed_out=True)
            super().rebuild(year, month)

    original = _rr.rrule._iter
    scope = dict(vars(_rr))
    scope["_iterinfo"] = _Info
    bounded_iter = types.FunctionType(
        original.__code__, scope, original.__name__, original.__defaults__, original.__closure__
    )
    cls = type("_BoundedRRule", (_rr.rrule,), {"_iter": bounded_iter})
    copy = cls.__new__(cls)
    copy.__dict__.update(rule.__dict__)
    copy._cache = None  # no caching: iterate `_iter` directly
    return copy


def expand(
    rule: str,
    rdate_lines: list[str] | None,
    exdate_lines: list[str] | None,
    dtstart: datetime,
    *,
    all_day: bool,
    series_zone: ZoneInfo | None,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Occurrence STARTS (naive local datetimes) with `window_start <= start <= window_end`.

    `dtstart` and the window are naive local datetimes in the series' zone (all-day: midnight).
    The caller widens `window_start` by the event duration to catch overlapping occurrences.
    """
    try:
        parsed = rrulestr(normalize_rule(rule, all_day, series_zone), dtstart=dtstart)
    except (ValueError, TypeError) as err:
        _LOGGER.debug("unreadable recurrence rule: %s", err)
        return [dtstart] if window_start <= dtstart <= window_end else []
    if not isinstance(parsed, _rr.rrule):
        return []
    if not _interval_ok(rule):
        # INTERVAL=0 passes the backend's regex but would repeat one candidate forever.
        _log_once(rule, "recurrence rule with an unusable INTERVAL, showing only its start: %s")
        return [dtstart] if window_start <= dtstart <= window_end else []

    deadline = _time.monotonic() + SERIES_TIME_BUDGET
    bounded = _bounded(parsed, window_end.year + 1, deadline)
    found: set[datetime] = set()
    historical = 0
    in_window = 0
    previous: datetime | None = None
    try:
        for occurrence in bounded:
            # Checked at EVERY candidate: a dense rule yields millions of them without ever
            # changing the month.
            if previous is not None and occurrence <= previous:
                raise _Abort(timed_out=True)  # not strictly increasing: the rule repeats itself
            previous = occurrence
            if _time.monotonic() > deadline:
                raise _Abort(timed_out=True)
            if occurrence < window_start:
                historical += 1
                if historical > MAX_HISTORICAL_CANDIDATES:
                    raise _Abort(timed_out=True)
                continue
            in_window += 1
            if in_window > MAX_CANDIDATES or occurrence > window_end or len(found) >= MAX_OCCURRENCES:
                break
            found.add(occurrence)
    except _Abort as abort:
        if abort.timed_out:
            # An incomplete window would look like a complete one: show none rather than some.
            found.clear()
            _log_once(rule, "stopped expanding a recurrence rule that took too long: %s")

    for value in parse_values(rdate_lines, series_zone):
        if window_start <= value <= window_end:
            found.add(value)
    for value in parse_values(exdate_lines, series_zone):
        found.discard(value)
    return sorted(found)


def _interval_ok(rule: str) -> bool:
    for key, value in _rule_parts(rule):
        if key == "INTERVAL":
            try:
                return int(value) > 0
            except ValueError:
                return False
    return True


def _log_once(rule: str, message: str) -> None:
    if rule not in _logged_aborts:
        _logged_aborts.add(rule)
        _LOGGER.debug(message, rule)


def recurrence_id(start: datetime, all_day: bool) -> str:
    """RFC 5545 RECURRENCE-ID of an occurrence (local value)."""
    return start.strftime("%Y%m%d") if all_day else start.strftime("%Y%m%dT%H%M%S")


def combine(day: date, clock: time | None) -> datetime:
    """Naive local datetime."""
    return datetime.combine(day, clock or time())


__all__ = [
    "combine",
    "expand",
    "instant",
    "normalize_rule",
    "parse_values",
    "recurrence_id",
    "resolve_local",
]
