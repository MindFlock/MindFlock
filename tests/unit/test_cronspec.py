"""Cron expressions for template schedules (core.cronspec).

Parsing (fields, names, steps, macros, Vixie day matching, refusals), the
slot search in a pinned zone (America/New_York, so the DST cases are real),
the minimum interval, and the card's words.
"""

from __future__ import annotations

import time

import pytest

from backend.web.core import cronspec as cs


@pytest.fixture
def ny_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(y, mo, d, h, mi=0) -> float:
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def local(epoch: float) -> str:
    return time.strftime("%a %Y-%m-%d %H:%M", time.localtime(epoch))


def test_parse_fields_names_steps_and_macros():
    c = cs.parse("*/15 9-17/4 * jan,JUL mon-fri")
    assert c.minutes == (0, 15, 30, 45)
    assert c.hours == (9, 13, 17)
    assert c.months == {1, 7}
    assert c.dows == {1, 2, 3, 4, 5}
    assert cs.parse("0 0 * * 7").dows == {0}  # 7 is Sunday too
    assert cs.parse("5/20 * * * *").minutes == (5, 25, 45)
    assert cs.parse("  0   21 * *  1-5 ").expr == "0 21 * * 1-5"
    assert cs.parse("@daily").minutes == (0,) and cs.parse("@daily").hours == (0,)
    assert cs.parse("@WEEKLY").dows == {0}


@pytest.mark.parametrize(
    "bad, words",
    [
        ("", "empty"),
        ("* * * *", "5 fields"),
        ("60 * * * *", "outside 0-59"),
        ("0 24 * * *", "outside 0-23"),
        ("5-1 * * * *", "backwards"),
        ("*/0 * * * *", "bad step"),
        ("x * * * *", "not a number"),
        ("@sometimes", "unknown macro"),
        ("0 0 31 2 *", "never fires"),
    ],
)
def test_parse_refuses_with_a_reason(bad, words):
    with pytest.raises(ValueError, match=words):
        cs.parse(bad)


def test_vixie_day_matching(ny_tz):
    # Both day fields restricted: EITHER matches (the 1st, and every Monday).
    c = cs.parse("0 9 1 * mon")
    nxt = cs.upcoming(c, at(2026, 10, 14, 12), 4)
    assert [local(t) for t in nxt] == [
        "Mon 2026-10-19 09:00",
        "Mon 2026-10-26 09:00",
        "Sun 2026-11-01 09:00",
        "Mon 2026-11-02 09:00",
    ]
    # Only one restricted: that one alone.
    assert local(cs.next_due(cs.parse("0 9 * * mon"), at(2026, 10, 14, 12))) == (
        "Mon 2026-10-19 09:00"
    )


def test_latest_and_next(ny_tz):
    c = cs.parse("*/30 * * * *")
    now = at(2026, 10, 14, 10, 10)
    assert cs.latest_due(c, now)[0] == "2026-10-14T10:00"
    assert local(cs.next_due(c, now)) == "Wed 2026-10-14 10:30"
    # on the minute counts as due
    assert cs.latest_due(c, at(2026, 10, 14, 10, 30))[0] == "2026-10-14T10:30"
    # a leap-day job finds its slot years away
    leap = cs.parse("0 0 29 2 *")
    assert local(cs.next_due(leap, now)) == "Tue 2028-02-29 00:00"
    assert cs.latest_due(leap, now)[0] == "2024-02-29T00:00"
    # not_before stops the backward search
    assert cs.latest_due(leap, now, not_before=at(2026, 1, 1, 0)) is None


def test_dst_fall_back_is_one_slot_spring_forward_still_fires(ny_tz):
    # 2026-11-01 01:30 happens twice in New York: one slot key.
    c = cs.parse("30 1 * * *")
    first = cs.latest_due(c, at(2026, 11, 1, 1, 45))
    later = cs.latest_due(c, at(2026, 11, 1, 3, 0))
    assert first[0] == later[0] == "2026-11-01T01:30"
    # 2027-03-14 02:30 doesn't exist: the slot lands at 03:30, same day.
    key, epoch = cs.latest_due(cs.parse("30 2 * * *"), at(2027, 3, 14, 5, 0))
    assert key == "2027-03-14T02:30" and local(epoch) == "Sun 2027-03-14 03:30"


def test_too_frequent():
    assert cs.MIN_INTERVAL_SECONDS == 900
    assert cs.too_frequent(cs.parse("* * * * *"))
    assert cs.too_frequent(cs.parse("*/10 * * * *"))
    assert not cs.too_frequent(cs.parse("*/15 * * * *"))
    assert not cs.too_frequent(cs.parse("0 * * * *"))
    # :55 then the next hour's :00 — only when that next hour runs
    assert cs.too_frequent(cs.parse("0,55 9,10 * * *"))
    assert not cs.too_frequent(cs.parse("0,55 9 * * *"))
    assert not cs.too_frequent(cs.parse("0,55 9,11 * * *"))


@pytest.mark.parametrize(
    "expr, words",
    [
        ("0 21 * * *", "Every day at 21:00"),
        ("0 21 * * 1-5", "Weekdays at 21:00"),
        ("0 9 * * mon", "Every Mon at 09:00"),
        ("30 8 * * 1,3,5", "Mon, Wed, Fri at 08:30"),
        ("0 9 1 * *", "Monthly on day 1 at 09:00"),
        ("0 0 29 2 *", "Every Feb 29 at 00:00"),
        ("30 7 25 dec *", "Every Dec 25 at 07:30"),
        ("*/30 * * * *", "Every 30 minutes"),
        ("15 * * * *", "Every hour at :15"),
        ("0 */6 * * *", "Every 6 hours"),
        ("0 9 1 * mon", "Cron 0 9 1 * mon"),
        ("nonsense", "nonsense"),
    ],
)
def test_describe(expr, words):
    assert cs.describe(expr) == words
