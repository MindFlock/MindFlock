"""Regression for the usage-limit hold (_refresh_limit_state): a queued prompt
must actually send once the limit window reopens, and a lingering "limit
reached" banner must not renew the hold forever."""

from __future__ import annotations

import types

import pytest

from backend.web import server


@pytest.fixture()
def h(monkeypatch):
    clock = {"t": 1000.0}
    # ``live`` is what the provider's own usage meter (usage_live) returns —
    # None means "no live source" (the pre-meter behavior).
    state = {"v": {"limited": False, "reset_at": None}, "live": None}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=b"usage limit reached"
        ),
    )

    class _Prov:
        def usage_limit_state(self, text, now=None):
            return state["v"]

        def usage_live(self):
            return state["live"]

    monkeypatch.setattr(server.providers, "resolve", lambda prog: _Prov())
    server._LIMIT_STATE.pop("t", None)
    inst = types.SimpleNamespace(Program="claude")

    def refresh():
        return server._refresh_limit_state(inst, "t", "sess")

    return clock, state, refresh


def test_relative_reset_hold_is_stable_not_recomputed(h):
    clock, state, refresh = h
    # Provider reports a RELATIVE reset ("resets in 2h") — recomputed each pass.
    state["v"] = {"limited": True, "reset_at": clock["t"] + 7200}
    first = refresh()
    assert first == pytest.approx(1000.0 + 7200)
    # 5s later the banner is still up; provider re-reports now+7200 (a moving
    # target). The hold must stay anchored to the first deadline, not slide.
    clock["t"] += 5
    state["v"] = {"limited": True, "reset_at": clock["t"] + 7200}
    assert refresh() == pytest.approx(first)  # stable — did NOT jump to 1005+7200


def test_expired_hold_with_lingering_banner_reopens(h):
    clock, state, refresh = h
    server._LIMIT_STATE["t"] = clock["t"] - 1  # a hold we set that just expired
    state["v"] = {"limited": True, "reset_at": None}  # banner still on the pane
    # Window is treated as reopened -> 0 (queue may send now), not a new hold.
    assert refresh() == 0.0
    assert "t" not in server._LIMIT_STATE


def test_fresh_limit_without_reset_uses_bounded_fallback(h):
    clock, state, refresh = h
    state["v"] = {"limited": True, "reset_at": None}
    assert refresh() == pytest.approx(1000.0 + server._LIMIT_FALLBACK)


def test_fresh_limit_with_past_reset_is_open(h):
    clock, state, refresh = h
    state["v"] = {
        "limited": True,
        "reset_at": clock["t"] - 60,
    }  # stale banner, reset passed
    assert refresh() == 0.0


# --------------------------------------------------------------------------- #
# The live usage meter (provider.usage_live) is authoritative when present
# --------------------------------------------------------------------------- #
def test_fresh_limit_prefers_live_meter_end(h):
    """Meter says the 5-hour window is spent until T — the hold lands on T even
    when the pane text parses to something earlier (early send = eaten prompt)."""
    clock, state, refresh = h
    state["v"] = {"limited": True, "reset_at": clock["t"] + 600}  # pane guesses 10m
    state["live"] = {"percent_used": 100.0, "end": clock["t"] + 5400}
    assert refresh() == pytest.approx(1000.0 + 5400)


def test_fresh_limit_holds_to_weekly_end_when_weekly_spent(h):
    """Both windows spent: the hold must outlast the LAST one (a 5-hour reset
    alone won't let a send land when the weekly cap is what's binding)."""
    clock, state, refresh = h
    state["v"] = {"limited": True, "reset_at": None}
    state["live"] = {
        "percent_used": 100.0,
        "end": clock["t"] + 3600,
        "weekly": {"percent_used": 100.0, "end": clock["t"] + 86400},
    }
    assert refresh() == pytest.approx(1000.0 + 86400)


def test_fresh_limit_ignores_unspent_weekly(h):
    """Weekly at 40% must not stretch the hold to the weekly reset."""
    clock, state, refresh = h
    state["v"] = {"limited": True, "reset_at": None}
    state["live"] = {
        "percent_used": 100.0,
        "end": clock["t"] + 3600,
        "weekly": {"percent_used": 40.0, "end": clock["t"] + 86400},
    }
    assert refresh() == pytest.approx(1000.0 + 3600)


def test_fresh_limit_meter_open_still_arms_bounded_hold(h):
    """A just-hit limit can outrun the ~60s-cached meter reading — an 'open'
    meter must not skip arming (that's the send-early dud), just bound it."""
    clock, state, refresh = h
    state["v"] = {"limited": True, "reset_at": None}
    state["live"] = {"percent_used": 42.0, "end": clock["t"] + 3600}
    assert refresh() == pytest.approx(1000.0 + server._LIMIT_FALLBACK)


def test_active_hold_released_early_when_meter_reopens(h):
    """Our hold overshot (bad parse/estimate): the moment the meter shows
    headroom again the queue must resume, not sit out the rest of the hold."""
    clock, state, refresh = h
    server._LIMIT_STATE["t"] = clock["t"] + 7200  # holding another 2h
    state["v"] = {"limited": True, "reset_at": None}  # banner still up
    state["live"] = {"percent_used": 12.0, "end": clock["t"] + 18000}
    assert refresh() == 0.0
    assert "t" not in server._LIMIT_STATE


def test_expired_hold_with_banner_rearms_from_meter(h):
    """Hold expired but the meter says the window is STILL spent: re-arm to the
    meter's reset instead of burning a queued prompt on the limit screen."""
    clock, state, refresh = h
    server._LIMIT_STATE["t"] = clock["t"] - 1
    state["v"] = {"limited": True, "reset_at": None}
    state["live"] = {"percent_used": 100.0, "end": clock["t"] + 900}
    assert refresh() == pytest.approx(1000.0 + 900)
    assert server._LIMIT_STATE["t"] == pytest.approx(1000.0 + 900)


def test_banner_scrolled_off_meter_reopens_early(h):
    """No banner on screen + unexpired hold: meter headroom releases it early."""
    clock, state, refresh = h
    server._LIMIT_STATE["t"] = clock["t"] + 7200
    state["v"] = {"limited": False, "reset_at": None}
    state["live"] = {"percent_used": 12.0, "end": clock["t"] + 18000}
    assert refresh() == 0.0


def test_meter_spent_but_no_reset_time_still_holds(h):
    """The escape bug: a window reads spent (>=99%) but the meter carries no
    usable reset (resets_at null at exhaustion / between-windows payload). It
    must NOT read as 'window open' (0.0) — that sends the prompt into the wall.
    Hold on the bounded fallback instead."""
    clock, state, refresh = h
    state["v"] = {"limited": False, "reset_at": None}  # no banner on the pane
    state["live"] = {"percent_used": 100.0}  # spent, but NO 'end'
    assert refresh() == pytest.approx(1000.0 + server._LIMIT_FALLBACK)
    assert server._LIMIT_STATE["t"] == pytest.approx(1000.0 + server._LIMIT_FALLBACK)


def test_meter_spent_past_reset_still_holds(h):
    """Same as above but the reset time is in the PAST (stale grace-served
    reading of a re-exhausted window): still a hold, not an open window."""
    clock, state, refresh = h
    state["v"] = {"limited": False, "reset_at": None}
    state["live"] = {"percent_used": 100.0, "end": clock["t"] - 30}
    assert refresh() == pytest.approx(1000.0 + server._LIMIT_FALLBACK)


def test_no_banner_but_meter_spent_arms_hold(h):
    """The reported bug: session is out of tokens but no limit banner is on the
    pane (ran out mid-turn, rebooted to a fresh idle prompt, or the banner only
    reprints after a submit). With no prior hold the pane says "not limited" —
    yet the meter shows the window spent, so the queue MUST hold, not send into
    the wall. Independent of the pane text, so it fixes Claude and Codex alike."""
    clock, state, refresh = h
    state["v"] = {"limited": False, "reset_at": None}  # nothing on the pane
    state["live"] = {"percent_used": 100.0, "end": clock["t"] + 4200}
    assert refresh() == pytest.approx(1000.0 + 4200)
    assert server._LIMIT_STATE["t"] == pytest.approx(1000.0 + 4200)


def test_no_banner_meter_open_still_sends(h):
    """Counterpart: no banner and the meter has headroom -> no hold, queue is
    free to send (must not over-hold a healthy session)."""
    clock, state, refresh = h
    state["v"] = {"limited": False, "reset_at": None}
    state["live"] = {"percent_used": 40.0, "end": clock["t"] + 4200}
    assert refresh() == 0.0
    assert "t" not in server._LIMIT_STATE


def test_no_banner_meter_unavailable_still_sends(h):
    """No banner, no prior hold, and the meter can't be read at all (usage_live
    returns None): there is nothing to hold on, so the queue stays free — a
    session with no readable meter must not be over-held."""
    clock, state, refresh = h
    state["v"] = {"limited": False, "reset_at": None}
    state["live"] = None
    assert refresh() == 0.0
    assert "t" not in server._LIMIT_STATE


def test_no_banner_weekly_spent_without_reset_holds_fallback(h):
    """No banner; the 5-hour window still has headroom but the WEEKLY cap is
    spent with no usable reset time -> hold on the bounded fallback rather than
    fire a queued prompt into the wall."""
    clock, state, refresh = h
    state["v"] = {"limited": False, "reset_at": None}
    state["live"] = {
        "percent_used": 40.0,
        "end": clock["t"] + 3600,
        "weekly": {"percent_used": 100.0},  # spent, but NO 'end'
    }
    assert refresh() == pytest.approx(1000.0 + server._LIMIT_FALLBACK)
    assert server._LIMIT_STATE["t"] == pytest.approx(1000.0 + server._LIMIT_FALLBACK)


def test_active_hold_kept_when_meter_still_spent(h):
    """An active hold is preserved (NOT slid forward) while the meter still
    reads the window spent — only genuine meter headroom releases it early, so a
    fresh spent-with-earlier-reset reading must not shorten our standing hold."""
    clock, state, refresh = h
    server._LIMIT_STATE["t"] = clock["t"] + 7200  # holding another 2h
    state["v"] = {"limited": False, "reset_at": None}  # no banner
    state["live"] = {"percent_used": 100.0, "end": clock["t"] + 900}  # still spent
    assert refresh() == pytest.approx(1000.0 + 7200)  # original deadline kept
    assert server._LIMIT_STATE["t"] == pytest.approx(1000.0 + 7200)


def test_no_limit_clears(h):
    clock, state, refresh = h
    server._LIMIT_STATE["t"] = clock["t"] + 100
    state["v"] = {"limited": False, "reset_at": None}
    # An unexpired timer is honoured even with no banner (message scrolled off)...
    assert refresh() == pytest.approx(1000.0 + 100)
    # ...but once it expires with no banner, it clears.
    clock["t"] += 200
    assert refresh() == 0.0


# --------------------------------------------------------------------------- #
# _live_limit_reset in isolation — the meter-reading primitive both the banner
# and no-banner branches of _refresh_limit_state lean on. Verified directly (not
# only via refresh) so the exhausted-without-reset guard can't regress silently.
# --------------------------------------------------------------------------- #
_NOW = 1000.0


def _meter(live):
    """A minimal provider exposing just ``usage_live()`` -> ``live``."""
    return types.SimpleNamespace(usage_live=lambda: live)


def test_live_reset_spent_without_end_is_bounded_fallback():
    # Window spent (>= exhausted pct) but no 'end' -> now + fallback, NOT 0.0
    # (0.0 would read as "window confirmed open" and send into the wall).
    prov = _meter({"percent_used": 100.0})
    assert server._live_limit_reset(prov, _NOW) == pytest.approx(
        _NOW + server._LIMIT_FALLBACK
    )


def test_live_reset_spent_with_past_end_is_bounded_fallback():
    # A spent window whose reset is already in the past is still a hold, not open.
    prov = _meter({"percent_used": 100.0, "end": _NOW - 30})
    assert server._live_limit_reset(prov, _NOW) == pytest.approx(
        _NOW + server._LIMIT_FALLBACK
    )


def test_live_reset_spent_with_future_end_returns_that_end():
    prov = _meter({"percent_used": 100.0, "end": _NOW + 5400})
    assert server._live_limit_reset(prov, _NOW) == pytest.approx(_NOW + 5400)


def test_live_reset_multiple_windows_returns_max_end():
    # Both windows spent with future resets: the LAST reset binds.
    prov = _meter(
        {
            "percent_used": 100.0,
            "end": _NOW + 3600,
            "weekly": {"percent_used": 100.0, "end": _NOW + 86400},
        }
    )
    assert server._live_limit_reset(prov, _NOW) == pytest.approx(_NOW + 86400)


def test_live_reset_readings_but_none_exhausted_is_open():
    # A genuine reading with headroom -> 0.0 (queue free to send).
    prov = _meter({"percent_used": 40.0, "end": _NOW + 3600})
    assert server._live_limit_reset(prov, _NOW) == 0.0


def test_live_reset_no_reading_returns_none():
    # No usable percent_used, an empty payload, or no live source at all -> None
    # (caller falls back to the pane text; unchanged pre-meter behaviour).
    assert server._live_limit_reset(_meter({"foo": "bar"}), _NOW) is None
    assert server._live_limit_reset(_meter({}), _NOW) is None
    assert server._live_limit_reset(_meter(None), _NOW) is None
