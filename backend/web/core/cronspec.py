"""Cron expressions for template schedules: parse, describe, find slots.

Standard 5-field cron (``minute hour day-of-month month day-of-week``) in
LOCAL time, the shape every crontab and CI scheduler uses:

* ``*``, lists (``1,15``), ranges (``1-5``), steps (``*/30``, ``8-18/2``),
  month and weekday names (``jan``, ``mon-fri``), weekday ``7`` = Sunday;
* the macros ``@hourly @daily @midnight @weekly @monthly @yearly
  @annually``;
* Vixie day matching: when BOTH day-of-month and day-of-week are restricted,
  a day matches if EITHER does (``0 9 1 * mon`` = the 1st and every Monday).

A slot is identified by its local wall-clock minute (``"2026-10-14T21:00"``),
never an epoch, so DST can neither repeat a slot (the 01:30 that happens twice
on a fall-back night is ONE slot) nor drop a day; a slot's epoch comes from
``mktime`` with ``isdst=-1``.

Pure: no I/O, no clock — callers pass ``now``. No third-party dependency.
"""

from __future__ import annotations

import datetime as _dt
import functools
import time
from typing import FrozenSet, List, Optional, Tuple

#: Scheduled runs start a whole session; nothing fires more often than this.
MIN_INTERVAL_SECONDS = 900

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_DOWS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

# (name, lo, hi, names) per field; names map to lo-based numbers.
_FIELDS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day of month", 1, 31, None),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DOWS),
)

# How far the slot searches look. Feb 29 recurs within ~8 years (1900-style
# century gaps aside), so this always finds a valid expression's slot.
_SEARCH_DAYS = 366 * 8 + 2


class Cron:
    """A parsed expression. ``minutes``/``hours``/``doms``/``months``/``dows``
    are the allowed values (``dows`` 0-6, Sunday 0)."""

    __slots__ = (
        "expr",
        "minutes",
        "hours",
        "doms",
        "months",
        "dows",
        "dom_star",
        "dow_star",
    )

    def __init__(self, expr, minutes, hours, doms, months, dows, dom_star, dow_star):
        self.expr = expr
        self.minutes: Tuple[int, ...] = tuple(sorted(minutes))
        self.hours: Tuple[int, ...] = tuple(sorted(hours))
        self.doms: FrozenSet[int] = frozenset(doms)
        self.months: FrozenSet[int] = frozenset(months)
        self.dows: FrozenSet[int] = frozenset(dows)
        self.dom_star = dom_star
        self.dow_star = dow_star

    def day_ok(self, day: _dt.date) -> bool:
        if day.month not in self.months:
            return False
        dom = day.day in self.doms
        dow = (day.weekday() + 1) % 7 in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow
        if self.dow_star:
            return dom
        return dom or dow


def _value(tok: str, lo: int, hi: int, names, field: str) -> int:
    t = tok.strip().lower()
    if names and t[:3] in names and t.isalpha():
        return names.index(t[:3]) + (lo if names is _MONTHS else 0)
    if not t.isdigit():
        raise ValueError("%s: %r is not a number" % (field, tok))
    v = int(t)
    if v < lo or v > hi:
        raise ValueError("%s: %d is outside %d-%d" % (field, v, lo, hi))
    return v


def _field(spec: str, lo: int, hi: int, names, field: str) -> Tuple[set, bool]:
    """The values ``spec`` allows, and whether it is unrestricted (``*``)."""
    out: set = set()
    star = False
    for part in spec.split(","):
        if not part:
            raise ValueError("%s: empty list item" % field)
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError("%s: bad step %r" % (field, step_s))
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
            star = star or step == 1
        elif "-" in rng:
            a_s, _, b_s = rng.partition("-")
            a = _value(a_s, lo, hi, names, field)
            b = _value(b_s, lo, hi, names, field)
            if a > b:
                raise ValueError("%s: range %s is backwards" % (field, rng))
        else:
            a = _value(rng, lo, hi, names, field)
            # "5/15" = from 5 to the end by 15 (Vixie).
            b = hi if step_s else a
        out.update(range(a, b + 1, step))
    return out, star


def parse(expr: str) -> Cron:
    """``expr`` parsed; ``ValueError`` naming the bad field otherwise."""
    return _parse(" ".join(str(expr or "").split()))


@functools.lru_cache(maxsize=256)
def _parse(raw: str) -> Cron:
    if not raw:
        raise ValueError("the cron expression is empty")
    text = MACROS.get(raw.lower(), raw)
    if text.startswith("@"):
        raise ValueError("unknown macro %s (try @hourly, @daily, @weekly)" % raw)
    parts = text.split(" ")
    if len(parts) != 5:
        raise ValueError(
            "a cron expression has 5 fields (minute hour day month weekday), not %d"
            % len(parts)
        )
    sets = []
    stars = []
    for spec, (field, lo, hi, names) in zip(parts, _FIELDS):
        vals, star = _field(spec, lo, hi, names, field)
        sets.append(vals)
        stars.append(star)
    dows = {d % 7 for d in sets[4]}
    cron = Cron(raw, sets[0], sets[1], sets[2], sets[3], dows, stars[2], stars[4])
    if not any(cron.day_ok(d) for d in _days_from(_dt.date(2024, 1, 1), 366 * 4 + 1)):
        raise ValueError("%s never fires (no such date)" % raw)
    return cron


def _days_from(start: _dt.date, n: int, step: int = 1):
    for i in range(n):
        yield start + _dt.timedelta(days=i * step)


def slot_key(day: _dt.date, hh: int, mm: int) -> str:
    return "%s%s%02d:%02d" % (day.isoformat(), "T", hh, mm)


def slot_epoch(day: _dt.date, hh: int, mm: int) -> float:
    """Local-time epoch of a wall-clock minute (``isdst=-1``: a 02:30 that a
    spring-forward night skips lands an hour later, never on another day)."""
    return time.mktime((day.year, day.month, day.day, hh, mm, 0, 0, 0, -1))


def latest_due(
    cron: Cron, now: float, not_before: Optional[float] = None
) -> Optional[Tuple[str, float]]:
    """The most recent slot at or before ``now``: ``(slot key, epoch)``.
    ``not_before`` stops the backward search early (an older slot is no use
    to the caller), so a daily tick stays cheap."""
    today = _dt.date.fromtimestamp(now)
    stop = (
        _dt.date.fromtimestamp(not_before) - _dt.timedelta(days=1)
        if not_before
        else None
    )
    for day in _days_from(today, _SEARCH_DAYS, -1):
        if stop is not None and day < stop:
            return None
        if not cron.day_ok(day):
            continue
        for hh in reversed(cron.hours):
            for mm in reversed(cron.minutes):
                at = slot_epoch(day, hh, mm)
                if at <= now:
                    return slot_key(day, hh, mm), at
    return None


def next_due(cron: Cron, now: float) -> Optional[float]:
    """Epoch of the first slot strictly after ``now``."""
    today = _dt.date.fromtimestamp(now)
    for day in _days_from(today, _SEARCH_DAYS):
        if not cron.day_ok(day):
            continue
        for hh in cron.hours:
            for mm in cron.minutes:
                at = slot_epoch(day, hh, mm)
                if at > now:
                    return at
    return None


def upcoming(cron: Cron, now: float, n: int) -> List[float]:
    """The next ``n`` slot epochs after ``now``."""
    out: List[float] = []
    at: Optional[float] = now
    while len(out) < n and at is not None:
        at = next_due(cron, at)
        if at is not None:
            out.append(at)
    return out


def too_frequent(cron: Cron) -> bool:
    """Whether two slots ever come closer than :data:`MIN_INTERVAL_SECONDS`
    (checked on the minute/hour sets themselves, so no date can hide it)."""
    mins = cron.minutes
    if len(mins) < 2:
        return False
    if any((b - a) * 60 < MIN_INTERVAL_SECONDS for a, b in zip(mins, mins[1:])):
        return True
    # The wrap from the last minute to the next hour's first only counts when
    # that next hour runs too.
    wrap = (60 - mins[-1] + mins[0]) * 60
    hrs = set(cron.hours)
    return wrap < MIN_INTERVAL_SECONDS and any((h + 1) % 24 in hrs for h in hrs)


# --------------------------------------------------------------------------- #
# Describing an expression for a card
# --------------------------------------------------------------------------- #
_DOW_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _hm(hh: int, mm: int) -> str:
    return "%02d:%02d" % (hh, mm)


def describe(expr: str) -> str:
    """A short human line for common shapes ("Every 30 minutes", "Weekdays
    at 21:00", "Mondays at 09:00"); the expression itself otherwise."""
    try:
        c = parse(expr)
    except ValueError:
        return str(expr or "")
    all_days = c.dom_star and c.dow_star and len(c.months) == 12
    weekdays = c.dom_star and c.dows == frozenset(range(1, 6)) and len(c.months) == 12
    one_time = len(c.hours) == 1 and len(c.minutes) == 1
    if one_time:
        at = _hm(c.hours[0], c.minutes[0])
        if all_days:
            return "Every day at " + at
        if weekdays:
            return "Weekdays at " + at
        if c.dom_star and len(c.months) == 12 and c.dows:
            names = [_DOW_NAMES[d] for d in sorted(c.dows)]
            return "%s%s at %s" % (
                "Every " if len(names) == 1 else "",
                ", ".join(names),
                at,
            )
        if c.dow_star and len(c.months) == 12 and len(c.doms) == 1:
            return "Monthly on day %d at %s" % (next(iter(c.doms)), at)
        if c.dow_star and len(c.months) == 1 and len(c.doms) == 1:
            # A date once a year ("Every Feb 29 at 00:00") — the shape whose
            # next runs are years apart, so the preview names the year.
            month = _MONTHS[next(iter(c.months)) - 1].capitalize()
            return "Every %s %d at %s" % (month, next(iter(c.doms)), at)
    if all_days and len(c.hours) == 24:
        if len(c.minutes) == 1:
            return "Every hour at :%02d" % c.minutes[0]
        step = c.minutes[1] - c.minutes[0]
        if c.minutes[0] == 0 and c.minutes == tuple(range(0, 60, step)):
            return "Every %d minutes" % step
    if all_days and len(c.minutes) == 1 and c.minutes[0] == 0 and len(c.hours) > 1:
        step = c.hours[1] - c.hours[0]
        if c.hours[0] == 0 and c.hours == tuple(range(0, 24, step)):
            return "Every %d hours" % step
    return "Cron " + c.expr
