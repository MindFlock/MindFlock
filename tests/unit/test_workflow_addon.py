"""Workflow Optimizer (addons/workflow): phase classification, the recorder's
change-only writes, the bus moments it takes (merge, delete, stage, ticket),
work items (one branch's trip to a merged PR, shared windows combined, split
at merges, downtime untracked), the bottleneck stats + advice, and the
routes/manifest. Time is injected (``now=``); nothing reads the real engine.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import workflow
from backend.web.addons.workflow import analysis as A
from backend.web.addons.workflow import recorder as R
from backend.web.addons.workflow import store

client = TestClient(server.app)

T = 1_800_000_000.0


def row(title="s1", **kw):
    base = {
        "title": title,
        "status": "running",
        "stage": "agent",
        "activity": "idle",
        "setup": None,
        "repo": "app",
        "branch": "feature/sc-12/fix-login",
        "pr_url": "",
        "name": title,
        "created_at": T,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# phase_of / meta_of
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kw, phase",
    [
        ({"status": "paused", "activity": "working"}, "paused"),
        ({"status": "loading"}, "setup"),
        ({"stage": "provisioning"}, "setup"),
        ({"setup": {"state": "running"}, "activity": "working"}, "setup"),
        ({"stage": "precommit", "activity": "working"}, "precommit"),
        ({"activity": "clarify", "stage": "pr"}, "question"),
        ({"activity": "limit"}, "limit"),
        ({"stage": "interrupt", "activity": "working"}, "hook_fix"),
        ({"stage": "interrupt", "activity": "idle"}, "hook_fix"),
        ({"activity": "working"}, "agent"),
        ({"activity": "working", "stage": "committed"}, "agent"),
        ({"activity": "working", "stage": "pr"}, "rework"),
        ({"activity": "offline"}, "offline"),
        ({"stage": "pr"}, "review"),
        ({"stage": "pushed"}, "to_pr"),
        ({"stage": "committed"}, "to_push"),
        ({}, "your_turn"),
        ({"activity": "unknown"}, "your_turn"),
    ],
)
def test_phase_of(kw, phase):
    assert R.phase_of(row(**kw)) == phase


def test_meta_finds_the_ticket():
    assert R.meta_of(row())["ticket"] == "sc-12"
    assert R.meta_of(row(branch="eng-42-login", title="x"))["ticket"] == "eng-42"
    assert R.meta_of(row(branch="main", title="issue-17-crash"))["ticket"] == "issue-17"
    assert R.meta_of(row(branch="main", title="refactor"))["ticket"] == ""
    # the intake's own record wins (and names it)
    m = R.meta_of(
        row(autopilot={"source": "tix", "item": "sc-99", "message": "Fix the login"})
    )
    assert (m["ticket"], m["name"]) == ("sc-99", "Fix the login")
    assert R.meta_of(row(), ticket="SC-7")["ticket"] == "SC-7"


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
def rec():
    written = []
    return R.Recorder(write=lambda rows: written.extend(rows)), written


def test_observe_writes_only_changes_and_a_heartbeat():
    r, w = rec()
    out = r.observe([row()], now=T, att=True)
    assert [x["k"] for x in out] == ["meta", "ph", "hb"]
    assert out[1] == {"t": T, "k": "ph", "s": "s1", "ph": "your_turn", "a": True}
    assert r.observe([row()], now=T + 5, att=True) == []
    # the keyboard bit matters only on your turn
    assert (
        r.observe([row(activity="working")], now=T + 10, att=True)[0]["ph"] == "agent"
    )
    assert r.observe([row(activity="working")], now=T + 15, att=False) == []
    out = r.observe([row()], now=T + 20, att=False)
    assert out == [{"t": T + 20, "k": "ph", "s": "s1", "ph": "your_turn", "a": False}]
    # a PR url is new meta
    assert r.observe([row(pr_url="u")], now=T + 25, att=False)[0]["k"] == "meta"
    # heartbeat once per HEARTBEAT_S even when nothing changes
    assert r.observe([row(pr_url="u")], now=T + 25 + R.HEARTBEAT_S, att=False) == [
        {"t": T + 25 + R.HEARTBEAT_S, "k": "hb"}
    ]
    assert w[-1]["k"] == "hb"


def test_observe_ends_a_vanished_session_and_skips_remote_rows():
    r, _ = rec()
    r.observe([row("a"), row("b"), row("mac::c")], now=T, att=False)
    out = r.observe([row("a")], now=T + 5, att=False)
    assert out == [{"t": T + 5, "k": "end", "s": "b"}]
    # an empty list (nothing published yet) ends nothing
    assert r.observe([], now=T + 10, att=False) == []


def test_events_mark_merge_delete_stage_and_ticket(monkeypatch):
    monkeypatch.setattr(R, "attended", lambda now=None: False)
    r, w = rec()
    r.on_event(
        {
            "event": "session.created",
            "session": "s1",
            "ts": T,
            "data": {"ticket": "4242"},
        }
    )
    r.observe([row(branch="feat/x")], now=T, att=False)
    assert w[0]["k"] == "meta" and w[0]["ticket"] == "4242"
    out = r.on_event(
        {
            "event": "session.stage_changed",
            "session": "s1",
            "ts": T + 1,
            "new": "precommit",
        }
    )
    assert out == [{"t": T + 1, "k": "ph", "s": "s1", "ph": "precommit", "a": False}]
    out = r.on_event(
        {
            "event": "session.pr_state_changed",
            "session": "s1",
            "ts": T + 2,
            "new": "MERGED",
            "data": {"url": "u"},
        }
    )
    assert out == [{"t": T + 2, "k": "merge", "s": "s1", "pr": "u"}]
    assert (
        r.on_event(
            {"event": "session.pr_state_changed", "session": "s1", "new": "CLOSED"}
        )
        == []
    )
    out = r.on_event({"event": "session.deleted", "session": "s1", "ts": T + 3})
    assert out == [{"t": T + 3, "k": "end", "s": "s1"}]
    assert r.on_event({"event": "session.deleted", "session": "s1", "ts": T + 4}) == []
    assert (
        r.on_event(
            {"event": "session.stage_changed", "session": "dev::s2", "new": "pr"}
        )
        == []
    )
    assert r.on_event({"bogus": 1}) == []


def test_store_round_trips_and_skips_torn_lines(tmp_path, monkeypatch):
    p = tmp_path / "wf.jsonl"
    monkeypatch.setenv("MINDFLOCK_WORKFLOW_FILE", str(p))
    store.append(
        [{"t": T, "k": "hb"}, {"t": T - 5, "k": "ph", "s": "a", "ph": "agent"}]
    )
    with open(p, "a") as f:
        f.write('{"t": 1, "k": "ph"\n{"t": 2, "k": "nope"}\n')
    rows = store.read()
    assert [r["t"] for r in rows] == [T - 5, T]
    assert oct(p.stat().st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def scenario():
    """sc-12: setup 60s, agent, your turn (here), agent, two commits (the first
    fails its hooks), push, PR at +3000, review with a rework stint, merged at
    +9000. Then the session idles, the server is down 12000→19000, and a new
    item starts at +20000 — where a second window on the same branch works
    while the first idles."""
    rows = [
        {
            "t": T,
            "k": "meta",
            "s": "s1",
            "repo": "app",
            "branch": "feature/sc-12/x",
            "ticket": "sc-12",
            "name": "Fix login",
        }
    ]

    def ph(dt, p, a=False, s="s1"):
        rows.append({"t": T + dt, "k": "ph", "s": s, "ph": p, "a": a})

    for dt, p, a in [
        (0, "setup", False),
        (60, "agent", False),
        (1260, "your_turn", True),
        (1860, "agent", False),
        (2460, "precommit", False),
        (2520, "hook_fix", False),
        (2580, "agent", False),
        (2700, "precommit", False),
        (2760, "to_push", False),
        (2880, "to_pr", False),
        (3000, "review", False),
        (5000, "rework", False),
        (5600, "review", False),
    ]:
        ph(dt, p, a)
    rows.append({"t": T + 9000, "k": "merge", "s": "s1", "pr": "https://x/pull/1"})
    ph(9100, "your_turn", False)
    rows += [{"t": T + x, "k": "hb"} for x in range(0, 12000, 300)]
    rows += [{"t": T + x, "k": "hb"} for x in range(19000, 26000, 300)]
    ph(20000, "agent")
    ph(21000, "your_turn", True)
    rows.append(
        {
            "t": T + 21000,
            "k": "meta",
            "s": "s1-copy",
            "repo": "app",
            "branch": "feature/sc-12/x",
        }
    )
    ph(21000, "agent", s="s1-copy")
    ph(22000, "your_turn", True, s="s1-copy")
    rows.sort(key=lambda r: r["t"])
    return rows


def person_scenario():
    """Four branches where the person's own time (questions, your turn, idle
    while away) is a big part of the clock — now is ``T + 9000``.

    * ``feature/a`` (sc-1): setup 60, agent 540, a question 300, agent 600,
      your turn 600, away 1900, agent 600, hooks 60, to_push 60, to_pr 120,
      PR at +4840, merged at +8000.
    * ``feature/b``: agent 600, your turn 3400, PR at +5000 (open).
    * ``feature/c``: ONLY the person before its PR — a question 600, your
      turn 400, PR at +3000.
    * ``feature/d`` (in progress): agent 500, then away to now.

    The server is down 5880→6600 (720 s untracked in every item)."""
    rows = []

    def meta(s, branch, **kw):
        rows.append(
            {"t": T, "k": "meta", "s": s, "repo": "app", "branch": branch, **kw}
        )

    def ph(dt, s, p, a=False):
        rows.append({"t": T + dt, "k": "ph", "s": s, "ph": p, "a": a})

    meta("a", "feature/a", ticket="sc-1", name="Fix a")
    meta("b", "feature/b")
    meta("c", "feature/c")
    meta("d", "feature/d")
    for dt, p, a in [
        (0, "setup", False),
        (60, "agent", False),
        (600, "question", True),
        (900, "agent", False),
        (1500, "your_turn", True),
        (2100, "your_turn", False),
        (4000, "agent", False),
        (4600, "precommit", False),
        (4660, "to_push", False),
        (4720, "to_pr", False),
        (4840, "review", False),
    ]:
        ph(dt, "a", p, a)
    rows.append({"t": T + 8000, "k": "merge", "s": "a", "pr": "https://x/pull/7"})
    for dt, p, a in [
        (1000, "agent", False),
        (1600, "your_turn", True),
        (5000, "review", False),
    ]:
        ph(dt, "b", p, a)
    for dt, p, a in [
        (2000, "question", True),
        (2600, "your_turn", True),
        (3000, "review", False),
    ]:
        ph(dt, "c", p, a)
    for dt, p, a in [(3000, "agent", False), (3500, "your_turn", False)]:
        ph(dt, "d", p, a)
    rows += [{"t": T + x, "k": "hb"} for x in range(0, 5401, 300)]
    rows += [{"t": T + x, "k": "hb"} for x in range(6600, 9001, 300)]
    rows.sort(key=lambda r: r["t"])
    return rows


def test_work_items_split_at_the_merge_and_add_up():
    items = A.build_items(scenario(), T + 26000, ["s1", "s1-copy"], include_me=True)
    assert [it["id"] for it in items] == [
        "app@feature/sc-12/x#1",
        "app@feature/sc-12/x#0",
    ]
    done = items[1]
    assert (
        done["status"] == "merged"
        and done["ticket"] == "sc-12"
        and done["pr"] == "https://x/pull/1"
    )
    assert done["lead_to_pr"] == 3000 and done["lead_to_merge"] == 9000
    assert done["to_pr"] == {
        "setup": 60.0,
        "agent": 1920.0,
        "your_turn": 600.0,
        "precommit": 120.0,
        "hook_fix": 60.0,
        "to_push": 120.0,
        "to_pr": 120.0,
    }
    assert sum(done["to_pr"].values()) == done["lead_to_pr"]
    assert done["after_pr"] == {"review": 5400.0, "rework": 600.0}
    assert (done["commits"], done["hook_failures"]) == (2, 1)
    # The next item starts at real work (+20000): the post-merge idle and the
    # downtime belong to no item; the copy window's work beats the idle one.
    nxt = items[0]
    assert nxt["status"] == "in_progress" and nxt["start"] == T + 20000
    assert nxt["to_pr"] == {"agent": 2000.0, "your_turn": 4000.0}
    assert nxt["sessions"] == ["s1", "s1-copy"]


def test_downtime_is_untracked_and_away_is_split_from_your_turn():
    rows = [
        {"t": T, "k": "meta", "s": "s", "repo": "r", "branch": "b"},
        {"t": T, "k": "ph", "s": "s", "ph": "agent", "a": False},
        {"t": T + 100, "k": "ph", "s": "s", "ph": "your_turn", "a": False},
        {"t": T + 5000, "k": "hb"},
        {"t": T + 5100, "k": "ph", "s": "s", "ph": "agent", "a": False},
    ]
    it = A.build_items(rows, T + 5200, ["s"], include_me=True)[0]
    # alive until +100+ALIVE_S, dark until the +5000 heartbeat, then idle
    # again for the 100 s before the agent restarts
    assert it["to_pr"]["away"] == A.ALIVE_S + 100
    assert it["to_pr"]["untracked"] == 5000 - 100 - A.ALIVE_S
    assert it["to_pr"]["agent"] == 200
    assert A._fmt(5400) == "1h 30m" and A._fmt(90061) == "1d 1h" and A._fmt(42) == "42s"


def test_base_branches_are_not_work_items():
    rows = [
        {"t": T, "k": "meta", "s": "s", "repo": "r", "branch": "main"},
        {"t": T, "k": "ph", "s": "s", "ph": "to_pr"},
    ]
    assert A.build_items(rows, T + 100, ["s"]) == []


def test_combine_scales_to_a_long_lived_session():
    import time as _time

    rows = [{"t": T, "k": "meta", "s": "s", "repo": "r", "branch": "b"}]
    for i in range(20000):  # ~70 days of agent/your-turn flips
        rows.append(
            {
                "t": T + i * 300,
                "k": "ph",
                "s": "s",
                "ph": "agent" if i % 2 else "your_turn",
                "a": True,
            }
        )
    t0 = _time.monotonic()
    it = A.build_items(rows, T + 20000 * 300, ["s"], include_me=True)[0]
    assert _time.monotonic() - t0 < 5
    assert it["to_pr"]["agent"] == it["to_pr"]["your_turn"] == 10000 * 300


def test_abandoned_and_closed_items():
    rows = [
        {"t": T, "k": "meta", "s": "gone", "repo": "r", "branch": "b"},
        {"t": T, "k": "ph", "s": "gone", "ph": "agent"},
        {"t": T + 60, "k": "end", "s": "gone"},
    ]
    assert A.build_items(rows, T + 100, ["other"])[0]["status"] == "abandoned"


def test_report_kpis_stats_and_advice():
    rep = A.report(scenario(), T + 26000, 7 * 86400, ["s1", "s1-copy"], include_me=True)
    k = rep["kpi"]
    assert (k["items"], k["prs_opened"], k["merged"], k["in_progress"]) == (2, 1, 1, 1)
    assert k["median_to_pr"] == 3000 and k["median_to_merge"] == 9000
    assert k["median_active_to_pr"] == 3000  # nothing away/untracked before the PR
    assert rep["basis"] == "shipped"
    shares = {r["phase"]: r["share"] for r in rep["to_pr"]}
    assert rep["to_pr"][0]["phase"] == "agent" and round(sum(shares.values()), 3) == 1.0
    assert {g["group"] for g in rep["to_pr_groups"]} >= {
        "agent",
        "you",
        "tooling",
        "handoff",
    }
    phases = [a["phase"] for a in rep["advice"]]
    assert "your_turn" in phases and "hook_fix" in phases
    # fixes first, context (info) last
    levels = [a["level"] for a in rep["advice"]]
    assert levels == sorted(levels, key=lambda lv: lv == "info")
    assert rep["trend"][-1]["items"] == 1
    assert [p["id"] for p in rep["phases"]] == A.PHASE_IDS


def test_empty_report():
    rep = A.report([], T, 7 * 86400, [])
    assert rep["kpi"]["items"] == 0 and rep["kpi"]["median_to_pr"] is None
    assert rep["advice"] == [] and rep["items"] == []


# --------------------------------------------------------------------------- #
# Your own time: left out by default, counted on request
# --------------------------------------------------------------------------- #
GOLDEN = os.path.join(
    os.path.dirname(__file__), "data", "workflow_report_include_me.golden.json"
)
PERSON_LIVE = ["a", "b", "c", "d"]


def test_include_me_is_the_full_clock_byte_for_byte():
    """The golden file was written by the analysis as it was before the
    person's time could be left out: counting it must change nothing."""
    got = {
        "report": {
            "scenario": A.report(
                scenario(), T + 26000, 7 * 86400, ["s1", "s1-copy"], include_me=True
            ),
            "person_scenario": A.report(
                person_scenario(), T + 9000, 7 * 86400, PERSON_LIVE, include_me=True
            ),
        },
        "items": {
            "scenario": A.build_items(
                scenario(), T + 26000, ["s1", "s1-copy"], include_me=True
            ),
            "person_scenario": A.build_items(
                person_scenario(), T + 9000, PERSON_LIVE, include_me=True
            ),
        },
    }
    with open(GOLDEN, encoding="utf-8") as f:
        assert json.dumps(got, indent=1) + "\n" == f.read()


def test_person_phases_are_the_you_and_idle_groups():
    assert A.PERSON_PHASES == {"question", "your_turn", "away"}
    assert {A.GROUP_OF[p] for p in A.PERSON_PHASES} == {"you", "idle"}
    assert "review" not in A.PERSON_PHASES and "untracked" not in A.PERSON_PHASES


def test_leaving_me_out_compresses_each_items_clock():
    mine = {
        it["id"]: it
        for it in A.build_items(
            person_scenario(), T + 9000, PERSON_LIVE, include_me=True
        )
    }
    items = {
        it["id"]: it for it in A.build_items(person_scenario(), T + 9000, PERSON_LIVE)
    }
    # Same items either way: ids, status and the real epochs don't move.
    assert set(items) == set(mine)
    for k, it in items.items():
        for f in (
            "status",
            "start",
            "pr_at",
            "merged_at",
            "end",
            "commits",
            "sessions",
        ):
            assert it[f] == mine[k][f], (k, f)
        assert not set(it["to_pr"]) & A.PERSON_PHASES
        assert not set(it["after_pr"]) & A.PERSON_PHASES
        assert not {sg["ph"] for sg in it["timeline"]} & A.PERSON_PHASES
    a = items["app@feature/a#0"]
    # 4840 s wall to the PR, 300 + 600 + 1900 of it waiting on the person.
    assert a["your_time_s"] == 2800
    assert a["lead_to_pr"] == 4840 - 2800 == sum(a["to_pr"].values())
    assert a["to_pr"] == {
        "setup": 60.0,
        "agent": 1740.0,
        "precommit": 60.0,
        "to_push": 60.0,
        "to_pr": 120.0,
    }
    assert a["active_to_pr"] == 2040
    assert a["lead_to_merge"] == a["elapsed"] == 8000 - 2800
    assert (
        a["after_pr"]
        == mine["app@feature/a#0"]["after_pr"]
        == {"review": 2440.0, "untracked": 720.0}
    )
    # The strip closes the holes: the two agent stints around the question
    # and the turn the person sat on are one stretch, the PR opens at +2040.
    tl = a["timeline"]
    assert [sg["ph"] for sg in tl] == [
        "setup",
        "agent",
        "precommit",
        "to_push",
        "to_pr",
        "review",
        "untracked",
        "review",
    ]
    assert all(x["end"] == y["start"] for x, y in zip(tl, tl[1:]))
    assert tl[1] == {"ph": "agent", "start": T + 60, "end": T + 1800, "at": T + 60}
    assert (
        tl[5]["start"] - tl[0]["start"] == a["lead_to_pr"] and tl[5]["at"] == T + 4840
    )
    assert tl[-1]["end"] - tl[0]["start"] == a["lead_to_merge"]
    assert items["app@feature/b#0"]["lead_to_pr"] == 600
    # Nothing but the person before its PR: zero to PR, not a crash.
    c = items["app@feature/c#0"]
    assert (c["lead_to_pr"], c["active_to_pr"], c["to_pr"], c["your_time_s"]) == (
        0.0,
        0,
        {},
        1000,
    )
    d = items["app@feature/d#0"]
    assert d["elapsed"] == 6000 - 4780 and d["to_pr"] == {
        "agent": 500.0,
        "untracked": 720.0,
    }


def test_leaving_me_out_ranks_only_the_process():
    full = A.report(
        person_scenario(), T + 9000, 7 * 86400, PERSON_LIVE, include_me=True
    )
    rep = A.report(person_scenario(), T + 9000, 7 * 86400, PERSON_LIVE)
    assert {a["phase"] for a in full["advice"]} >= {"your_turn", "question", "away"}
    k = rep["kpi"]
    assert (k["items"], k["prs_opened"], k["merged"], k["in_progress"]) == (4, 3, 1, 1)
    # leads 2040 / 600 / 0 (was 4840 / 4000 / 1000)
    assert (k["median_to_pr"], k["p90_to_pr"]) == (600, 2040)
    assert k["median_active_to_pr"] == 600 and k["median_to_merge"] == 5200
    stats = {r["phase"]: r for r in rep["to_pr"]}
    assert set(stats) == {"agent", "setup", "precommit", "to_push", "to_pr"}
    assert round(sum(r["share"] for r in rep["to_pr"]), 3) == 1.0
    assert stats["agent"]["total_s"] == 2340 and stats["agent"]["share"] == round(
        2340 / 2640, 4
    )
    assert (
        stats["agent"]["median_s"],
        stats["agent"]["p90_s"],
        stats["agent"]["items"],
    ) == (1170, 1740, 2)
    assert {g["group"] for g in rep["to_pr_groups"]} == {"agent", "tooling", "handoff"}
    assert round(sum(g["share"] for g in rep["to_pr_groups"]), 3) == 1.0
    assert not {r["phase"] for r in rep["after_pr"]} & A.PERSON_PHASES
    assert not {a["phase"] for a in rep["advice"]} & A.PERSON_PHASES
    assert not {p["id"] for p in rep["phases"]} & A.PERSON_PHASES
    assert {g["id"] for g in rep["groups"]} == {g for g, _ in A.GROUPS} - {
        "you",
        "idle",
    }
    assert rep["excluded"] == {
        "phases": ["question", "your_turn", "away"],
        "total_s": 2800 + 3400 + 1000,
    }
    assert rep["trend"][-1]["items"] == 3 and rep["trend"][-1]["median_to_pr"] == 600
    assert "excluded" not in full


def test_an_item_that_was_all_me():
    rows = [
        {"t": T, "k": "meta", "s": "s", "repo": "r", "branch": "b"},
        {"t": T, "k": "ph", "s": "s", "ph": "your_turn", "a": True},
        {"t": T + 300, "k": "ph", "s": "s", "ph": "question", "a": True},
        {"t": T + 400, "k": "ph", "s": "s", "ph": "review", "a": False},
    ] + [{"t": T + x, "k": "hb"} for x in range(0, 1001, 300)]
    rep = A.report(rows, T + 1000, 7 * 86400, ["s"])
    assert rep["kpi"]["median_to_pr"] == 0 and rep["kpi"]["p90_to_pr"] == 0
    assert rep["to_pr"] == [] and rep["to_pr_groups"] == [] and rep["advice"] == []
    assert rep["excluded"]["total_s"] == 400
    it = rep["items"][0]
    assert it["lead_to_pr"] == 0 and it["elapsed"] == 600
    assert [sg["ph"] for sg in it["timeline"]] == ["review"]
    # still waiting on you, no PR: nothing counted yet, and no division by zero
    rows = rows[:2] + rows[4:]
    rep = A.report(rows, T + 1000, 7 * 86400, ["s"])
    assert rep["basis"] == "all" and rep["items"][0]["elapsed"] == 0
    assert rep["items"][0]["timeline"] == [] and rep["to_pr"] == []


# --------------------------------------------------------------------------- #
# Routes + manifest
# --------------------------------------------------------------------------- #
def test_summary_and_item_routes(tmp_path, monkeypatch):
    p = tmp_path / "wf.jsonl"
    monkeypatch.setenv("MINDFLOCK_WORKFLOW_FILE", str(p))
    monkeypatch.setattr(workflow, "_live_titles", lambda: ["s1", "s1-copy"])
    import time as _time

    shift = _time.time() - (T + 26000)
    p.write_text(
        "".join(json.dumps({**r, "t": r["t"] + shift}) + "\n" for r in scenario())
    )
    body = client.get("/api/workflow/summary?range=30d").json()
    assert body["range"] == "30d" and body["kpi"]["merged"] == 1
    assert all("timeline" not in it for it in body["items"])
    item_id = body["items"][1]["id"]
    it = client.get("/api/workflow/item", params={"id": item_id}).json()
    assert it["status"] == "merged" and it["timeline"][0]["ph"] == "setup"
    assert client.get("/api/workflow/item", params={"id": "nope"}).status_code == 404
    assert client.get("/api/workflow/summary?range=bogus").json()["range"] == "7d"


def test_manifest_declares_the_bar_dialog_and_pane():
    data = client.get("/api/addons").json()
    ext = next(a for a in data["addons"] if a["id"] == "workflow")["extension"]
    assert ext["module"] == "/extensions/workflow/index.js"
    assert ext["bar_label"] == "Workflow"
    assert [b["command"] for b in ext["buttons"]] == ["workflow.open"]
    assert {s["id"]: s["kind"] for s in ext["surfaces"]} == {
        "main": "dialog",
        "board": "pane",
    }
    for path in (
        "/extensions/workflow/index.js",
        "/extensions/workflow/view.js",
        "/extensions/workflow/style.css",
    ):
        assert client.get(path).status_code == 200, path


def test_recording_is_off_under_pytest():
    started, subs = [], []

    class Ctx:
        def register_task(self, coro):
            started.append(coro)

        def subscribe(self, name, cb):
            subs.append(name)
            return lambda: None

    asyncio.run(workflow.WorkflowAddon().on_startup(Ctx()))
    assert started == [] and subs == []


def _store_person_scenario(tmp_path, monkeypatch):
    p = tmp_path / "wf.jsonl"
    monkeypatch.setenv("MINDFLOCK_WORKFLOW_FILE", str(p))
    monkeypatch.setattr(workflow, "_live_titles", lambda: PERSON_LIVE)
    import time as _time

    shift = _time.time() - (T + 9000)
    p.write_text(
        "".join(
            json.dumps({**r, "t": r["t"] + shift}) + "\n" for r in person_scenario()
        )
    )
    return p


def test_include_me_setting_defaults_off_and_persists(tmp_path, monkeypatch):
    p = _store_person_scenario(tmp_path, monkeypatch)
    sp = tmp_path / "workflow_optimizer.settings.json"
    assert store.settings_path() == str(sp)
    assert store.load_settings() == {"include_me": False} and not sp.exists()
    assert client.get("/api/workflow/settings").json() == {"include_me": False}
    assert client.post("/api/workflow/settings", json={"include_me": True}).json() == {
        "include_me": True
    }
    assert json.loads(sp.read_text()) == {"include_me": True}
    assert oct(sp.stat().st_mode & 0o777) == "0o600"
    # wrong types and unknown keys change nothing
    body = client.post(
        "/api/workflow/settings", json={"include_me": "no", "x": 1}
    ).json()
    assert body == {"include_me": True} and store.load_settings() == {
        "include_me": True
    }
    sp.write_text("{torn")
    assert store.load_settings() == {"include_me": False}
    assert p.exists()


def test_summary_and_item_follow_the_setting_and_the_override(tmp_path, monkeypatch):
    _store_person_scenario(tmp_path, monkeypatch)

    def phases(body):
        return {r["phase"] for r in body["to_pr"]} | {
            r["phase"] for r in body["after_pr"]
        }

    body = client.get("/api/workflow/summary?range=7d").json()
    assert body["include_me"] is False and not phases(body) & A.PERSON_PHASES
    assert body["excluded"]["total_s"] == 7200 and body["kpi"]["median_to_pr"] == 600
    a = next(it for it in body["items"] if it["branch"] == "feature/a")
    tl = client.get("/api/workflow/item", params={"id": a["id"]}).json()["timeline"]
    assert not {sg["ph"] for sg in tl} & A.PERSON_PHASES and "at" in tl[0]

    store.save_settings({"include_me": True})
    body = client.get("/api/workflow/summary?range=7d").json()
    assert (
        body["include_me"] is True
        and "your_turn" in phases(body)
        and "excluded" not in body
    )
    tl = client.get("/api/workflow/item", params={"id": a["id"]}).json()["timeline"]
    assert "question" in {sg["ph"] for sg in tl}
    # an explicit override wins over the setting, both ways
    body = client.get("/api/workflow/summary?range=7d&include_me=0").json()
    assert body["include_me"] is False and not phases(body) & A.PERSON_PHASES
    tl = client.get(
        "/api/workflow/item", params={"id": a["id"], "include_me": "0"}
    ).json()["timeline"]
    assert not {sg["ph"] for sg in tl} & A.PERSON_PHASES
    store.save_settings({"include_me": False})
    assert client.get("/api/workflow/summary?include_me=1").json()["include_me"] is True
    assert (
        client.get("/api/workflow/summary?include_me=bogus").json()["include_me"]
        is False
    )
