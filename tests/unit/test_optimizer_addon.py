"""Usage Optimizer (the ``optimizer`` extension): the pure analysis, the
sample store and the routes.

The analysis tests build sample lists by hand — one dict per minute-ish
reading — so the maths behind the score, the pace and the advice is pinned
without a server. The route tests go through the TestClient with the store
redirected to a tmp file (conftest sets ``MINDFLOCK_OPTIMIZER_FILE``).
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons.optimizer import analysis as A
from backend.web.addons.optimizer import sampler, store

client = TestClient(server.app)

NOW = 1_800_000_000.0
H5 = 5.0


def _s(t, pct=None, end=None, att=False, work=0, idle=0, lim=0, src="live", **extra):
    d = {
        "t": t,
        "p": "claude",
        "pct": pct,
        "end": end,
        "wpct": None,
        "wend": None,
        "src": src,
        "work": work,
        "idle": idle,
        "lim": lim,
        "off": 0,
        "att": att,
    }
    d.update(extra)
    return d


def _window(end, pcts, att, step=300.0, **extra):
    """Samples climbing through ``pcts`` and ending at ``end`` (the last
    reading lands ``step`` before the reset)."""
    n = len(pcts)
    return [
        _s(
            end - (n - i) * step,
            pct=p,
            end=end,
            att=att,
            work=1 if p else 0,
            idle=0 if p else 1,
            **extra,
        )
        for i, p in enumerate(pcts)
    ]


# --------------------------------------------------------------------------- #
# windows + score
# --------------------------------------------------------------------------- #


def test_group_windows_segments_by_reset_time_and_keeps_the_peak():
    samples = _window(NOW - 7200, [10, 40, 80], att=True) + _window(
        NOW + 7200, [5, 20], att=False
    )
    wins = A.group_windows(samples, H5, NOW)
    assert [w["final_pct"] for w in wins] == [80.0, 20.0]
    assert [w["live"] for w in wins] == [False, True]
    assert wins[0]["attended_share"] == 1.0 and wins[1]["attended_share"] == 0.0
    assert wins[0]["start"] == wins[0]["end"] - H5 * 3600


def test_group_windows_merges_jittered_live_reset_times():
    # A live meter re-reports resets_at with second-level jitter.
    end = NOW - 3600
    samples = [
        _s(end - 900, 30, end + 7),
        _s(end - 600, 50, end - 40),
        _s(end - 300, 70, end + 55),
    ]
    wins = A.group_windows(samples, H5, NOW)
    assert len(wins) == 1 and wins[0]["final_pct"] == 70.0


def test_capacity_score_is_used_over_the_ceiling():
    # Two completed 5h windows (80 % and 30 %) in a 20h observed stretch:
    # ceiling 4 windows, used 1.1 → 27.5.
    wins = [
        {
            "end": NOW - 10 * 3600,
            "start": NOW - 15 * 3600,
            "final_pct": 80.0,
            "has_pct": True,
            "live": False,
            "src": "live",
        },
        {
            "end": NOW - 5 * 3600,
            "start": NOW - 10 * 3600,
            "final_pct": 30.0,
            "has_pct": True,
            "live": False,
            "src": "live",
        },
    ]
    sc = A.capacity_score(wins, H5, 7 * 86400, NOW, observed_s=20 * 3600)
    assert sc["ceiling"] == 4.0 and sc["used"] == 1.1
    assert sc["score"] == 27.5 and sc["windows"] == 2 and sc["avg_fill"] == 55.0
    assert sc["basis"] == "live"


def test_capacity_score_counts_unwatched_time_as_unused_by_default():
    wins = [
        {
            "end": NOW - 3600,
            "start": NOW - 5 * 3600 - 3600,
            "final_pct": 100.0,
            "has_pct": True,
            "live": False,
            "src": "live",
        }
    ]
    full_day = A.capacity_score(wins, H5, 86400, NOW)  # 4.8 windows possible
    assert full_day["score"] == pytest.approx(100 / 4.8, abs=0.1)
    assert A.capacity_score([], H5, 86400, NOW)["score"] is None


def test_capacity_score_flags_transcript_estimates():
    wins = [
        {
            "end": NOW - 3600,
            "start": NOW - 6 * 3600,
            "final_pct": 50.0,
            "has_pct": True,
            "live": False,
            "src": "estimate",
        }
    ]
    assert (
        A.capacity_score(wins, H5, 86400, NOW, observed_s=5 * 3600)["basis"]
        == "estimate"
    )


def test_provider_report_ceiling_covers_a_sparsely_sampled_window():
    # One window seen at just three points must still count for its full
    # five hours, or a sparse sampler would score itself up.
    samples = [
        _s(NOW - 4 * 3600, 20, NOW - 3600),
        _s(NOW - 2 * 3600, 60, NOW - 3600),
        _s(NOW - 3700, 90, NOW - 3600),
    ]
    rep = A.provider_report(
        "claude", "Claude", samples, H5, 168.0, NOW, 7 * 86400, 3600
    )
    assert rep["score"]["ceiling"] >= 1.0
    assert rep["score"]["score"] <= 90.0


def test_calibration_prefers_budget_then_lockout_cost_then_best_window():
    end1, end2 = NOW - 7200, NOW + 3600
    base = [
        _s(end1 - 600, None, end1, src="estimate", cost=40.0, lim=0),
        _s(end1 - 300, None, end1, src="estimate", cost=90.0, lim=1),  # locked at $90
        _s(NOW - 60, None, end2, src="estimate", cost=30.0),
    ]
    cal = A.calibrate_capacity(base)
    assert cal == {"capacity": 90.0, "basis": "learned"}
    filled = A.fill_pct(base)
    assert [x["pct"] for x in filled] == [44.4, 100.0, 33.3]
    assert filled[0]["src"] == "learned"
    # A configured window budget wins over everything.
    with_budget = [dict(x, budget=120.0) for x in base]
    assert A.calibrate_capacity(with_budget) == {"capacity": 120.0, "basis": "budget"}
    assert A.fill_pct(with_budget)[1]["pct"] == 75.0
    # No lockout ever seen: the dearest window is the provisional yardstick.
    never = [dict(x, lim=0) for x in base]
    assert A.calibrate_capacity(never) == {"capacity": 90.0, "basis": "provisional"}
    assert A.calibrate_capacity([_s(NOW, None, None)]) == {
        "capacity": None,
        "basis": "",
    }


def test_provider_report_scores_estimate_only_windows_through_calibration():
    """Claude with the OAuth usage endpoint rate-limited: every sample is a
    transcript estimate with a cost and no percent. The report still scores,
    says what the percent rests on, and the live card gets a pace."""
    end1, end2 = NOW - 3600, NOW + 2 * 3600
    samples = [
        _s(end1 - 4 * 3600 + i * 600, None, end1, src="estimate", cost=2.0 * i, work=1)
        for i in range(24)
    ]
    samples[-1]["lim"] = 1  # locked out at ~$46
    samples += [
        _s(
            NOW - 1800 + i * 600,
            None,
            end2,
            src="estimate",
            cost=10.0 + 5.0 * i,
            work=1,
        )
        for i in range(3)
    ]
    rep = A.provider_report("claude", "Claude", samples, H5, 168.0, NOW, 86400, 900)
    assert rep["calibration"]["basis"] == "learned"
    assert rep["score"]["score"] is not None and rep["score"]["basis"] == "learned"
    assert rep["live"] is not None and rep["live"]["src"] == "learned"
    assert 0 < rep["live"]["pct"] < 100


# --------------------------------------------------------------------------- #
# pace + advice
# --------------------------------------------------------------------------- #


def test_live_pace_reads_the_recent_burn_and_projects_the_limit():
    end = NOW + 2 * 3600  # 3h into a 5h window
    samples = [_s(NOW - 1800, 40, end), _s(NOW - 900, 50, end), _s(NOW - 60, 60, end)]
    live = A.live_pace(samples, H5, NOW)
    assert live["pct"] == 60.0 and live["remaining_s"] == 7200
    assert live["elapsed_frac"] == pytest.approx(0.6, abs=0.01)
    assert live["ratio"] == pytest.approx(1.0, abs=0.02)
    # 20 points in 29 minutes → ~41 %/h → 100 % in just under an hour
    assert 35 < live["rate_pct_h"] < 45
    assert live["eta_limit"] is not None and NOW < live["eta_limit"] < end
    assert live["projected"] == 100.0


def test_live_pace_is_none_without_a_live_window():
    assert A.live_pace(_window(NOW - 3600, [10, 50], att=True), H5, NOW) is None


def test_weekly_pace_compares_used_share_to_elapsed_share():
    wend = NOW + 3 * 86400  # 4 of 7 days elapsed
    samples = [_s(NOW - 60, 10, NOW + 3600, wpct=80.0, wend=wend)]
    wk = A.weekly_pace(samples, 168.0, NOW)
    assert wk["pct"] == 80.0
    assert wk["elapsed_frac"] == pytest.approx(4 / 7, abs=0.01)
    assert wk["ratio"] == pytest.approx(1.4, abs=0.01)
    assert wk["eta_limit"] is not None and wk["eta_limit"] < wend


def test_advice_warns_when_draining_hot_while_attended():
    live = {
        "pct": 70.0,
        "end": NOW + 3600,
        "remaining_s": 3600,
        "elapsed_frac": 0.8,
        "ratio": 1.6,
        "rate_pct_h": 40.0,
        "eta_limit": NOW + 2700,
        "projected": 100.0,
        "src": "live",
    }
    empty = {"attended": {}, "unattended": {}, "unknown": 0, "total": 86400}
    tips = A.advice(
        "claude", "Claude", live, None, empty, {"score": 50.0}, attended_now=True
    )
    assert tips[0]["level"] == "warn"
    assert "1.6×" in tips[0]["title"]
    assert "/model sonnet" in tips[0]["text"]
    # Nobody at the keyboard: the same drain is only a tip.
    assert (
        A.advice(
            "claude", "Claude", live, None, empty, {"score": 50.0}, attended_now=False
        )[0]["level"]
        == "tip"
    )


def test_advice_points_at_unused_capacity_and_idle_unattended_time():
    live = {
        "pct": 20.0,
        "end": NOW + 1800,
        "remaining_s": 1800,
        "elapsed_frac": 0.9,
        "ratio": 0.22,
        "rate_pct_h": 0.0,
        "eta_limit": None,
        "projected": 20.0,
        "src": "live",
    }
    split = {
        "attended": {"working": 3600},
        "unattended": {"working": 3600, "idle": 6 * 3600, "offline": 2 * 3600},
        "unknown": 0,
        "total": 86400,
    }
    titles = [
        t["title"]
        for t in A.advice("claude", "Claude", live, None, split, {"score": 30.0}, False)
    ]
    assert any("80% of this window will go unused" in t for t in titles)
    assert any("unattended time sat idle" in t for t in titles)


def test_advice_calls_out_lockouts_during_attended_time():
    split = {
        "attended": {"limited": 3600},
        "unattended": {},
        "unknown": 0,
        "total": 86400,
    }
    tips = A.advice("codex", "Codex", None, None, split, {"score": None}, True)
    assert tips[0]["level"] == "warn" and "locked out" in tips[0]["title"]


def test_advice_warns_when_the_weekly_cap_is_used_up():
    weekly = {
        "pct": 100.0,
        "end": NOW + 86400,
        "remaining_s": 86400,
        "elapsed_frac": 0.8,
        "ratio": 1.25,
        "projected": 100.0,
        "eta_limit": None,
    }
    empty = {"attended": {}, "unattended": {}, "unknown": 0, "total": 86400}
    tips = A.advice("claude", "Claude", None, weekly, empty, {"score": 90.0}, True)
    assert tips[0]["level"] == "warn" and "weekly cap is used up" in tips[0]["title"]
    assert all(t["level"] != "ok" for t in tips)


def test_advice_is_calm_when_capacity_is_used():
    empty = {"attended": {}, "unattended": {}, "unknown": 0, "total": 86400}
    tips = A.advice("claude", "Claude", None, None, empty, {"score": 88.0}, True)
    assert [t["level"] for t in tips] == ["ok"]
    assert "nothing locked you out" in tips[0]["text"]
    # Lockouts in the last day, all unattended: still fine, but not "nothing".
    night = {
        "attended": {},
        "unattended": {"limited": 3600},
        "unknown": 0,
        "total": 86400,
    }
    tips = A.advice("claude", "Claude", None, None, night, {"score": 88.0}, True)
    assert "nobody was at the keyboard" in tips[0]["text"]


# --------------------------------------------------------------------------- #
# timeline + split
# --------------------------------------------------------------------------- #


def test_timeline_and_split_distinguish_attended_from_unattended_time():
    since = NOW - 3600
    end = NOW + 3600
    samples = [  # one reading per 10 minutes (the heartbeat keeps gaps short)
        _s(since, 10, end, att=True, work=1),  # 0–20m: working, attended
        _s(since + 600, 15, end, att=True, work=1),
        _s(since + 1200, 20, end, att=False, idle=1),  # 20–40m: idle, unattended
        _s(since + 1800, 20, end, att=False, idle=1),
        _s(since + 2400, 20, end, att=False, lim=1),  # 40–60m: locked out
        _s(since + 3000, 20, end, att=False, lim=1),
    ]
    tl = A.timeline(samples, since, NOW, 600.0)
    assert [b["state"] for b in tl] == [
        "working",
        "working",
        "idle",
        "idle",
        "limited",
        "limited",
    ]
    assert [b["att"] for b in tl] == [True, True, False, False, False, False]
    split = A.time_split(samples, since, NOW)
    assert split["attended"]["working"] == 1200
    assert split["unattended"]["idle"] == 1200
    assert split["unattended"]["limited"] == 1200
    assert split["unknown"] == 0


def test_a_long_gap_reads_as_unknown_not_as_the_last_state():
    since = NOW - 4 * 3600
    samples = [_s(since, 10, NOW + 3600, work=1)]  # then silence for 4h
    split = A.time_split(samples, since, NOW)
    assert split["unattended"]["working"] == A.MAX_GAP_S
    assert split["unknown"] == 4 * 3600 - A.MAX_GAP_S
    tl = A.timeline(samples, since, NOW, 3600.0)
    assert tl[0]["state"] == "unknown" or tl[-1]["state"] == "unknown"


def test_token_rate_is_the_delta_within_a_window_and_restarts_on_reset():
    since = NOW - 3600
    end1, end2 = NOW - 1800, NOW + 3 * 3600
    samples = [
        _s(since + 300, 10, end1, tok=1000),
        _s(since + 900, 20, end1, tok=4000),  # +3000
        _s(since + 2100, 5, end2, tok=500),  # new window: counts 500
        _s(since + 2700, 8, end2, tok=1500),  # +1000
    ]
    rate = A.token_rate(samples, since, NOW, 600.0)
    assert [b["tokens"] for b in rate] == [1000, 3000, 0, 500, 1000, 0]
    assert A.token_rate([_s(since, 10, end1)], since, NOW, 600.0) == []


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_store_appends_lines_reads_them_sorted_and_skips_torn_ones(
    tmp_path, monkeypatch
):
    p = tmp_path / "uo.jsonl"
    monkeypatch.setenv("MINDFLOCK_OPTIMIZER_FILE", str(p))
    store.append([{"t": 20.0, "p": "claude"}, {"t": 10.0, "p": "codex"}])
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"t": 30.0, "p": "cla')  # a crash mid-write
    rows = store.read()
    assert [r["t"] for r in rows] == [10.0, 20.0]
    assert [r["p"] for r in store.read(provider="codex")] == ["codex"]
    assert [r["t"] for r in store.read(since=15.0)] == [20.0]
    assert oct(os.stat(p).st_mode & 0o777) == oct(0o600)


def test_store_compaction_drops_samples_past_retention(tmp_path, monkeypatch):
    p = tmp_path / "uo.jsonl"
    monkeypatch.setenv("MINDFLOCK_OPTIMIZER_FILE", str(p))
    import time as _t

    old = _t.time() - store.RETENTION_S - 10
    store.append([{"t": old, "p": "claude"}, {"t": _t.time(), "p": "claude"}])
    assert store.compact() == 1
    assert len(store.read()) == 1


# --------------------------------------------------------------------------- #
# sampler
# --------------------------------------------------------------------------- #


def test_session_counts_group_by_provider_and_activity():
    rows = [
        {"program": "claude", "activity": "working", "status": "running"},
        {"program": "claude", "activity": "clarify", "status": "running"},
        {"program": "claude", "activity": "limit", "status": "running"},
        {
            "program": "claude",
            "activity": "idle",
            "status": "paused",
        },  # paused: not counted
        {"program": "codex", "activity": "offline", "status": "running"},
    ]
    c = sampler._session_counts(rows)
    assert c["claude"] == {"work": 1, "idle": 1, "lim": 1, "off": 0}
    assert c["codex"] == {"work": 0, "idle": 0, "lim": 0, "off": 1}


def test_attended_now_reads_the_servers_human_input_stamps(monkeypatch):
    import time as _t

    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {"a": _t.time() - 30})
    assert sampler.attended_now() is True
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {"a": _t.time() - A.ATTENDED_S - 1})
    assert sampler.attended_now() is False


def test_collect_drops_a_reading_whose_window_already_reset(monkeypatch):
    """Codex's newest rollout can be weeks old: its rate-limit snapshot names
    a window that reset long ago. That is "no reading", not a 0 % window."""
    import time as _t

    now = _t.time()

    def fake_window(prov):
        if prov.name == "codex":
            return {"percent_used": 0.0, "end": now - 25 * 86400, "source": "live"}
        if prov.name == "claude":
            return {
                "percent_used": 55.0,
                "end": now + 3600,
                "source": "live",
                "weekly": {"percent_used": 9.0, "end": now - 3600},
            }
        return None

    monkeypatch.setattr(server, "_usage_window_for", fake_window)
    rows = {r["p"]: r for r in sampler.collect(now)}
    assert rows["codex"]["pct"] is None and rows["codex"]["end"] is None
    assert rows["claude"]["pct"] == 55.0 and rows["claude"]["end"] == now + 3600
    assert rows["claude"]["wpct"] is None  # the weekly reading had expired too


def test_sample_once_writes_on_change_or_heartbeat_only(monkeypatch):
    sampler._last_written.clear()
    fake = [
        {
            "t": 100.0,
            "p": "claude",
            "pct": 10.0,
            "end": 500.0,
            "wpct": None,
            "wend": None,
            "src": "live",
            "work": 1,
            "idle": 0,
            "lim": 0,
            "off": 0,
            "att": True,
        }
    ]
    monkeypatch.setattr(sampler, "collect", lambda now=None: [dict(fake[0], t=now)])
    written = []
    monkeypatch.setattr(store, "append", lambda rows: written.extend(rows))
    monkeypatch.setattr(store, "last_by_provider", lambda: {})
    assert len(sampler.sample_once(now=100.0)) == 1  # first sighting
    assert sampler.sample_once(now=160.0) == []  # unchanged, heartbeat not due
    assert len(sampler.sample_once(now=100.0 + A.HEARTBEAT_S)) == 1  # heartbeat
    fake[0]["pct"] = 12.0
    assert len(sampler.sample_once(now=470.0)) == 1  # a change
    assert len(sampler.sample_once(now=480.0, force=True)) == 1
    assert len(written) == 4
    sampler._last_written.clear()


# --------------------------------------------------------------------------- #
# manifest + routes
# --------------------------------------------------------------------------- #


def test_manifest_declares_the_bar_dialog_and_pane():
    data = client.get("/api/addons").json()
    row = next(a for a in data["addons"] if a["id"] == "optimizer")
    ext = row["extension"]
    assert ext["module"] == "/extensions/optimizer/index.js"
    assert ext["bar_label"] == "Usage"
    assert [b["command"] for b in ext["buttons"]] == ["optimizer.open"]
    assert {c["id"] for c in ext["commands"]} == {
        "optimizer.open",
        "optimizer.window",
        "optimizer.sample",
    }
    kinds = {s["id"]: s["kind"] for s in ext["surfaces"]}
    assert kinds == {"main": "dialog", "board": "pane"}
    assert ext["stylesheet"] is True
    for path in (
        "/extensions/optimizer/index.js",
        "/extensions/optimizer/view.js",
        "/extensions/optimizer/charts.js",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/extensions/optimizer/style.css").status_code == 200


def test_summary_route_reports_every_watched_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_OPTIMIZER_FILE", str(tmp_path / "uo.jsonl"))
    r = client.get("/api/optimizer/summary?range=24h")
    assert r.status_code == 200
    body = r.json()
    assert body["range"] == "24h" and body["period_s"] == 86400
    names = [p["name"] for p in body["providers"]]
    assert "claude" in names
    claude = next(p for p in body["providers"] if p["name"] == "claude")
    assert claude["window_hours"] == 5.0
    assert claude["score"]["score"] is None  # nothing sampled yet
    assert isinstance(claude["timeline"], list) and len(claude["timeline"]) == 96
    assert client.get("/api/optimizer/summary?range=bogus").json()["range"] == "7d"


def test_sample_route_records_into_the_redirected_store(monkeypatch, tmp_path):
    p = tmp_path / "uo.jsonl"
    monkeypatch.setenv("MINDFLOCK_OPTIMIZER_FILE", str(p))
    monkeypatch.setattr(
        server,
        "_usage_window_for",
        lambda prov: (
            {
                "percent_used": 42.0,
                "end": NOW + 3600,
                "source": "live",
                "weekly": {"percent_used": 7.0, "end": NOW + 86400},
            }
            if prov.name == "claude"
            else None
        ),
    )
    sampler._last_written.clear()
    r = client.post("/api/optimizer/sample")
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    claude = next(row for row in rows if row["p"] == "claude")
    assert claude["pct"] == 42.0 and claude["wpct"] == 7.0 and claude["src"] == "live"
    assert store.read(provider="claude")[0]["pct"] == 42.0
    sampler._last_written.clear()
