"""Usage-limit detection (roadmap D): the pure parser, the provider hook, and
the server's bounded/self-correcting limit gate."""

from __future__ import annotations

import subprocess
import types

from backend import providers
from backend.providers import usage_limits as ul
from backend.web import server


# --------------------------------------------------------------------------- #
# High-precision SCREEN match (drives the activity 'limit' state / red pill)
# --------------------------------------------------------------------------- #
def test_is_limit_screen_matches_real_banners():
    for s in (
        "Claude usage limit reached · resets 3am",
        "You've reached your usage limit",
        "Weekly limit reached ∙ resets Jul 24 at 10:59am (America/New_York)",
        "5-hour limit reached",
        "you've hit your limit",
    ):
        assert ul.is_limit_screen(s) is True, s


def test_is_limit_screen_ignores_stray_rate_limit_text():
    # These trip the looser detect_limit gate but must NOT drive the state — they
    # appear constantly in normal work and would mislabel a working session.
    for s in (
        "HTTPError: 429 Too Many Requests",
        "# handle rate limited responses",
        'raise RateLimitError("rate limited")',
        "the api rate limit exceeded during the load test",
        "",
    ):
        assert ul.is_limit_screen(s) is False, s
        # ...and the looser gate still flags the real-limit-ish ones (unchanged).
    assert ul.detect_limit("too many requests")["limited"] is True


# --------------------------------------------------------------------------- #
# Pure parser
# --------------------------------------------------------------------------- #
def test_no_limit_text_is_not_limited():
    assert ul.detect_limit("thinking… wrote 3 files", now=1000.0)["limited"] is False
    # A bare, unrelated mention doesn't trip it.
    assert (
        ul.detect_limit("added a token bucket to the code", now=1000.0)["limited"]
        is False
    )


def test_detects_usage_limit_phrase():
    st = ul.detect_limit("You've reached your usage limit.", now=1000.0)
    assert st["limited"] is True


def test_relative_reset_is_parsed():
    st = ul.detect_limit("usage limit reached — resets in 2h 30m", now=1000.0)
    assert st["limited"] is True
    assert st["reset_at"] == 1000.0 + 2 * 3600 + 30 * 60


def test_relative_minutes_only():
    st = ul.detect_limit("rate limit exceeded, try again in 45 minutes", now=0.0)
    assert st["reset_at"] == 45 * 60


def test_absolute_reset_rolls_to_future():
    # 00:00 local "now"; "resets at 3pm" → 15:00 the same day (in the future).
    import time

    base = time.mktime((2026, 7, 5, 0, 0, 0, 0, 0, -1))
    st = ul.detect_limit("usage limit reached. resets at 3pm", now=base)
    assert st["reset_at"] is not None
    assert st["reset_at"] - base == 15 * 3600


def test_relative_days():
    st = ul.detect_limit("usage limit reached — resets in 2 days", now=1000.0)
    assert st["reset_at"] == 1000.0 + 2 * 86400


def test_absolute_24h_clock():
    import time

    base = time.mktime((2026, 7, 5, 0, 0, 0, 0, 0, -1))
    st = ul.detect_limit("rate limit exceeded. resets 15:00", now=base)
    assert st["reset_at"] - base == 15 * 3600


def test_weekly_banner_with_date_and_zone():
    """Claude's weekly banner: dated reset in an explicit IANA zone — must be
    parsed in THAT zone, not server-local time."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    now = dt.datetime(2026, 7, 20, 12, 0, tzinfo=tz).timestamp()
    st = ul.detect_limit(
        "Weekly limit reached ∙ resets Jul 24 at 10:59am (America/New_York)",
        now=now,
    )
    assert st["limited"] is True
    assert st["reset_at"] == dt.datetime(2026, 7, 24, 10, 59, tzinfo=tz).timestamp()


def test_five_hour_banner_detected():
    st = ul.detect_limit("5-hour limit reached ∙ resets 3am", now=1000.0)
    assert st["limited"] is True
    assert st["reset_at"] is not None


def test_dated_reset_without_year_rolls_to_next_year():
    """ "resets Jan 2" read in late December means NEXT year's Jan 2."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("UTC")
    now = dt.datetime(2026, 12, 28, 9, 0, tzinfo=tz).timestamp()
    st = ul.detect_limit("usage limit reached — resets Jan 2 at 10am (UTC)", now=now)
    assert st["reset_at"] == dt.datetime(2027, 1, 2, 10, 0, tzinfo=tz).timestamp()


def test_midnight_12am_normalizes_to_zero_hour():
    import time

    base = time.mktime((2026, 7, 5, 6, 0, 0, 0, 0, -1))  # 06:00 local
    st = ul.detect_limit("usage limit reached. resets at 12am", now=base)
    # 12am -> 00:00; already past 06:00 today, so it rolls to 00:00 tomorrow.
    assert st["reset_at"] - base == 18 * 3600


def test_out_of_range_clock_time_is_unparseable():
    # "25:00" parses structurally but fails the 0<=hh<=23 guard -> no reset_at,
    # yet the limit is still detected.
    st = ul.detect_limit("usage limit reached. resets at 25:00", now=1000.0)
    assert st["limited"] is True
    assert st["reset_at"] is None


def test_impossible_calendar_date_is_unparseable():
    # "Feb 30" trips datetime.replace's ValueError guard -> no reset_at.
    st = ul.detect_limit(
        "usage limit reached — resets Feb 30 at 10am (UTC)", now=1000.0
    )
    assert st["limited"] is True
    assert st["reset_at"] is None


def test_unknown_zone_falls_back_to_local_time():
    # A syntactically-valid but nonexistent IANA zone: ZoneInfo raises, so the
    # parse falls back to local time and still yields a reset_at.
    import time

    base = time.mktime((2026, 7, 5, 0, 0, 0, 0, 0, -1))
    st = ul.detect_limit("usage limit reached. resets at 3pm (Not/ARealZone)", now=base)
    assert st["limited"] is True
    assert st["reset_at"] is not None
    assert st["reset_at"] - base == 15 * 3600  # 3pm local, zone ignored


def test_empty_patterns_never_reports_limited():
    # With no patterns to match against, even limit-looking text is not flagged.
    assert (
        ul.detect_limit("usage limit reached", patterns=(), now=0.0)["limited"] is False
    )
    assert (
        ul.detect_limit("usage limit reached", patterns=None, now=0.0)["limited"]
        is False
    )


def test_default_now_is_used_when_omitted():
    # Omitting `now` uses time.time(); a relative reset lands ~1h in the future.
    import time

    before = time.time()
    st = ul.detect_limit("usage limit reached — resets in 1h 0m")
    assert st["limited"] is True
    assert st["reset_at"] >= before + 3600 - 5


# --------------------------------------------------------------------------- #
# Provider hook
# --------------------------------------------------------------------------- #
def test_provider_usage_limit_state():
    p = providers.resolve("claude")
    assert p.usage_limit_state("all good", now=0.0)["limited"] is False
    assert p.usage_limit_state("Claude usage limit reached", now=0.0)["limited"] is True


# --------------------------------------------------------------------------- #
# Server gate: bounded + self-correcting (no permanent stall)
# --------------------------------------------------------------------------- #
def _fake_pane(monkeypatch, text):
    def fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=text.encode())

    monkeypatch.setattr(subprocess, "run", fake_run)


def _inst():
    return types.SimpleNamespace(Program="claude")


def _no_live(monkeypatch):
    """Isolate the gate from the dev machine's real Claude credentials — the
    gate now consults the provider's live usage meter when a limit shows."""
    monkeypatch.setattr(server, "_live_limit_reset", lambda p, n, i=None: None)


def test_gate_uses_parsed_reset(monkeypatch):
    server._LIMIT_STATE.pop("s1", None)
    _no_live(monkeypatch)
    _fake_pane(monkeypatch, "usage limit reached — resets in 1h 0m")
    until = server._refresh_limit_state(_inst(), "s1", "tmux-s1")
    assert until > 0
    # roughly now + 1h
    import time

    assert abs(until - (time.time() + 3600)) < 5


def test_gate_fallback_does_not_extend(monkeypatch):
    """A limit with no parseable reset uses a bounded fallback that must NOT keep
    pushing out on repeated detections (else the queue would stall forever)."""
    server._LIMIT_STATE.pop("s2", None)
    _no_live(monkeypatch)
    _fake_pane(monkeypatch, "you've reached your usage limit")
    first = server._refresh_limit_state(_inst(), "s2", "tmux-s2")
    second = server._refresh_limit_state(_inst(), "s2", "tmux-s2")
    assert first == second  # timer held steady, not extended


def test_gate_clears_when_pane_is_clean(monkeypatch):
    server._LIMIT_STATE["s3"] = 0.0  # expired/none
    _no_live(monkeypatch)
    _fake_pane(monkeypatch, "ready for your next task")
    assert server._refresh_limit_state(_inst(), "s3", "tmux-s3") == 0.0
    assert server._session_limited_until("s3") == 0.0
