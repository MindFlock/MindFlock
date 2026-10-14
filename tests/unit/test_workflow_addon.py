"""Workflow Optimizer (addons/workflow): phase classification, the recorder's
change-only writes, the bus moments it takes (merge, delete, stage, ticket),
work items (one branch's trip to a merged PR, shared windows combined, split
at merges, downtime untracked), the bottleneck stats + advice, and the
routes/manifest. Time is injected (``now=``); nothing reads the real engine.
"""

from __future__ import annotations

import asyncio
import json

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


def test_work_items_split_at_the_merge_and_add_up():
    items = A.build_items(scenario(), T + 26000, ["s1", "s1-copy"])
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
    it = A.build_items(rows, T + 5200, ["s"])[0]
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
    it = A.build_items(rows, T + 20000 * 300, ["s"])[0]
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
    rep = A.report(scenario(), T + 26000, 7 * 86400, ["s1", "s1-copy"])
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
