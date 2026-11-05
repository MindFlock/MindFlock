"""The window refresh skips a plan with no 5-hour window (weekly-only)."""

from backend.providers import claude_usage_api
from backend.web.core import window_refresh as wr


def _cfg():
    return {
        "enabled": True,
        "providers": ["claude", "codex"],
        "interval_hours": 5.0,
        "anchor_time": None,
        "last_fired": {},
    }


def test_weekly_only_plan_is_never_pinged(monkeypatch):
    monkeypatch.setattr(
        claude_usage_api, "plan_shape", lambda: {"session": False, "weekly_end": None}
    )
    assert wr.due_providers(10**9, _cfg()) == ["codex"]


def test_a_plan_with_a_session_window_still_is(monkeypatch):
    monkeypatch.setattr(
        claude_usage_api, "plan_shape", lambda: {"session": True, "weekly_end": None}
    )
    assert wr.due_providers(10**9, _cfg()) == ["claude", "codex"]


def test_no_reading_yet_keeps_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(claude_usage_api, "plan_shape", lambda: None)
    assert wr.due_providers(10**9, _cfg()) == ["claude", "codex"]


def test_daily_anchor_mode_skips_too(monkeypatch):
    monkeypatch.setattr(
        claude_usage_api, "plan_shape", lambda: {"session": False, "weekly_end": None}
    )
    cfg = _cfg() | {"anchor_time": "00:00"}
    assert wr.due_providers(10**9 + 86400 * 2, cfg) == ["codex"]
