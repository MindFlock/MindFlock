"""Per-provider usage-window descriptor (web.core.usage_api._usage_window_for).

Locks the live-vs-estimate window selection — in particular that a live reading
which reports a percent but NO reset time still engages (previously it was
dropped because the branch gated solely on ``end``, blanking the window pill for
codex/antigravity builds that omit the reset field).
"""

from __future__ import annotations

from backend.web.core import usage_api


class _Fake:
    """A minimal windowed, non-claude provider (so the transcript estimate is
    never used — only its own ``usage_live`` feeds the window)."""

    name = "codex"

    def __init__(self, live):
        self._live = live

    def usage_mode(self):
        return "windowed"

    def usage_window(self):
        return {"kind": "rolling", "hours": 5.0}

    def usage_live(self):
        return self._live


def test_live_window_engages_with_percent_but_no_end():
    win = usage_api._usage_window_for(_Fake({"percent_used": 33.0}))
    assert win is not None
    assert win["source"] == "live"
    assert win["percent_used"] == 33.0
    assert "end" not in win  # no reset field reported, but the window still shows


def test_live_window_engages_with_end():
    win = usage_api._usage_window_for(_Fake({"percent_used": 50.0, "end": 999.0}))
    assert win["source"] == "live"
    assert win["end"] == 999.0
    assert win["percent_used"] == 50.0


def test_live_window_engages_with_groups_only():
    win = usage_api._usage_window_for(
        _Fake({"groups": [{"label": "g", "percent_used": 5.0}]})
    )
    assert win is not None
    assert win["groups"]


def test_empty_live_reading_yields_no_window():
    # Nothing usable in the live dict, and (non-claude) no transcript estimate.
    assert usage_api._usage_window_for(_Fake({})) is None
    assert usage_api._usage_window_for(_Fake(None)) is None


def test_metered_provider_has_no_window():
    class _Metered:
        name = "aider"

        def usage_mode(self):
            return "metered"

        def usage_window(self):
            return {"kind": ""}

        def usage_live(self):
            return None

    assert usage_api._usage_window_for(_Metered()) is None


def test_live_window_carries_weekly_plan_extra_and_groups():
    live = {
        "percent_used": 12.0,
        "end": 100.0,
        "weekly": {"percent_used": 40.0},
        "groups": [{"label": "g"}],
        "extra": {"note": "x"},
        "plan": "Max",
    }
    win = usage_api._usage_window_for(_Fake(live))
    assert win["source"] == "live"
    assert win["weekly"] == {"percent_used": 40.0}
    assert win["groups"] == [{"label": "g"}]
    assert win["extra"] == {"note": "x"}
    assert win["plan"] == "Max"
    assert win["budget"] == 0.0  # windowed-live windows default budget to 0


def test_live_reading_exception_falls_through_to_no_window():
    # usage_live() blowing up must not propagate — a non-claude provider with no
    # transcript estimate then yields no window at all.
    class _Boom(_Fake):
        def usage_live(self):
            raise RuntimeError("provider telemetry offline")

    assert usage_api._usage_window_for(_Boom(None)) is None


# --------------------------------------------------------------------------- #
# _usage_window_for: the transcript-estimate branch (claude only)
# --------------------------------------------------------------------------- #
class _Claude:
    """A windowed claude provider whose own live telemetry is empty, so the
    transcript-derived estimate (measured against the configured budget) is
    what feeds the window."""

    name = "claude"

    def usage_mode(self):
        return "windowed"

    def usage_window(self):
        return {"kind": "rolling", "hours": 5.0}

    def usage_live(self):
        return None


def _patch_estimate(monkeypatch, window, budget):
    from backend.providers import usage_history

    monkeypatch.setattr(usage_history, "current_window", lambda hours: window)

    class _Srv:
        def _window_budget_usd(self):
            return budget

    monkeypatch.setattr(usage_api, "_server", lambda: _Srv())


def test_estimate_branch_computes_percent_against_budget(monkeypatch):
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 2.0, "tokens": 100},
        budget=10.0,
    )
    win = usage_api._usage_window_for(_Claude())
    assert win["source"] == "estimate"
    assert win["budget"] == 10.0
    assert win["percent_used"] == 20.0  # 100 * 2 / 10


def test_estimate_branch_percent_none_when_budget_zero(monkeypatch):
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 2.0, "tokens": 100},
        budget=0.0,
    )
    win = usage_api._usage_window_for(_Claude())
    assert win["source"] == "estimate"
    assert win["percent_used"] is None  # no budget -> no percent


def test_estimate_branch_percent_caps_at_100(monkeypatch):
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 99.0, "tokens": 1},
        budget=1.0,
    )
    win = usage_api._usage_window_for(_Claude())
    assert win["percent_used"] == 100.0  # min(100, ...) clamps overshoot


def test_claude_with_no_active_window_yields_none(monkeypatch):
    _patch_estimate(monkeypatch, None, budget=10.0)  # idle past the window
    assert usage_api._usage_window_for(_Claude()) is None


# --------------------------------------------------------------------------- #
# _usage_window_for: the daily fixed-quota branch
# --------------------------------------------------------------------------- #
def test_daily_window_is_countdown_only():
    class _Daily:
        name = "somecli"

        def usage_mode(self):
            return "windowed"

        def usage_window(self):
            return {"kind": "daily"}

        def usage_live(self):
            return None

    win = usage_api._usage_window_for(_Daily())
    # A day-long countdown with no $ / token metering.
    assert win["end"] - win["anchor"] == 86400.0
    assert win["cost"] is None
    assert win["tokens"] is None
    assert win["percent_used"] is None


def test_unknown_window_kind_yields_none():
    class _Weird:
        name = "x"

        def usage_mode(self):
            return "windowed"

        def usage_window(self):
            return {"kind": "phase-of-moon"}

        def usage_live(self):
            return None

    assert usage_api._usage_window_for(_Weird()) is None


# --------------------------------------------------------------------------- #
# _provider_label
# --------------------------------------------------------------------------- #
def test_server_lazy_import_returns_server_module():
    # The lazy accessor resolves the real server module (top-level import would
    # be circular — server imports these names at startup).
    from backend.web import server

    assert usage_api._server() is server


def test_provider_label_known_names():
    assert usage_api._provider_label("claude") == "Claude"
    assert usage_api._provider_label("codex") == "Codex"
    assert usage_api._provider_label("aider") == "Aider"


def test_provider_label_capitalizes_unknown():
    assert usage_api._provider_label("windsurf") == "Windsurf"
    # Only the first char is upper-cased; the tail is preserved verbatim.
    assert usage_api._provider_label("myCLI") == "MyCLI"


def test_provider_label_empty():
    assert usage_api._provider_label("") == ""
    assert usage_api._provider_label(None) == ""


# --------------------------------------------------------------------------- #
# _provider_usage_entry
# --------------------------------------------------------------------------- #
def _fake_server(monkeypatch, window):
    class _Srv:
        _provider_label = staticmethod(usage_api._provider_label)

        def _usage_window_for(self, p):
            return window

    monkeypatch.setattr(usage_api, "_server", lambda: _Srv())


def test_provider_usage_entry_full(monkeypatch):
    _fake_server(monkeypatch, {"source": "live"})

    class _P:
        name = "codex"

        def usage_mode(self):
            return "windowed"

        def usage_window(self):
            return {"note": "resets nightly"}

        def usage_periods(self):
            return [{"day": "2026-07-24", "cost": 1.0}]

    entry = usage_api._provider_usage_entry(_P())
    assert entry == {
        "name": "codex",
        "label": "Codex",
        "mode": "windowed",
        "window": {"source": "live"},
        "window_note": "resets nightly",
        "periods": [{"day": "2026-07-24", "cost": 1.0}],
    }


def test_provider_usage_entry_swallows_usage_errors(monkeypatch):
    _fake_server(monkeypatch, None)

    class _P:
        name = "claude"

        def usage_mode(self):
            raise RuntimeError("boom")

        def usage_window(self):
            raise RuntimeError("boom")

        def usage_periods(self):
            raise RuntimeError("boom")

    entry = usage_api._provider_usage_entry(_P())
    # Usage is enrichment only: a throwing provider still yields a well-formed
    # entry with the metered default and no window/periods.
    assert entry["name"] == "claude"
    assert entry["label"] == "Claude"
    assert entry["mode"] == "metered"
    assert entry["window"] is None
    assert entry["window_note"] == ""
    assert entry["periods"] is None


# --------------------------------------------------------------------------- #
# _usage_window_for: a live reading with a reset time but NO utilization
# --------------------------------------------------------------------------- #
# Reported from a Mac: the plan window showed its reset countdown but no
# "% used" row at all. Taking live's percent verbatim discarded a percentage the
# transcript estimate could still supply, so an end-only live reading blanked it.
class _ClaudeLive(_Claude):
    def __init__(self, live):
        self._live = live

    def usage_live(self):
        return self._live


def test_live_end_without_percent_falls_back_to_the_estimate(monkeypatch):
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 3.0, "tokens": 100},
        budget=10.0,
    )
    win = usage_api._usage_window_for(_ClaudeLive({"end": 4242.0}))
    assert win["end"] == 4242.0  # the live reset time is still the accurate one
    assert win["percent_used"] == 30.0  # 100 * 3 / 10, from the estimate
    # Marked as an estimate so the UI keeps its "(est.)" qualifier honest.
    assert win["source"] == "estimate"


def test_live_end_without_percent_stays_blank_with_no_budget(monkeypatch):
    """No budget means there is genuinely no percentage to compute — the window
    still reports its reset time, as before."""
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 3.0, "tokens": 100},
        budget=0.0,
    )
    win = usage_api._usage_window_for(_ClaudeLive({"end": 4242.0}))
    assert win["end"] == 4242.0
    assert win["percent_used"] is None
    assert win["source"] == "live"


def test_live_percent_is_never_overridden_by_the_estimate(monkeypatch):
    """The provider's own meter wins whenever it reports one."""
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 9.0, "tokens": 100},
        budget=10.0,
    )
    win = usage_api._usage_window_for(_ClaudeLive({"end": 1.0, "percent_used": 7.0}))
    assert win["percent_used"] == 7.0
    assert win["source"] == "live"


def test_group_quotas_are_left_alone(monkeypatch):
    """Per-group quotas are their own presentation — no headline percent is
    invented for them from an unrelated transcript estimate."""
    _patch_estimate(
        monkeypatch,
        {"anchor": 0.0, "end": 5.0, "cost": 9.0, "tokens": 100},
        budget=10.0,
    )
    win = usage_api._usage_window_for(
        _ClaudeLive({"end": 1.0, "groups": [{"label": "g", "percent_used": 5.0}]})
    )
    assert win["percent_used"] is None
    assert win["source"] == "live"
