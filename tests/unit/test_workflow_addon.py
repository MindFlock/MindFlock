"""Workflow Optimizer (addons/workflow): phase classification, the recorder's
change-only writes, the bus moments it takes (merge, delete, stage, ticket),
its HEAD watching (head / commit rows, against a real scratch git repo),
units of work (first real work → the commit that finishes it, shared windows
combined, estimated commits for older rows, idle stages inside a unit are the
person's, the handoffs after it, downtime untracked), the bottleneck stats +
advice, and the routes/manifest. Time is injected (``now=``); nothing reads
the real engine.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

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


def test_the_keyboard_bit_is_kept_for_every_idle_stage():
    """Inside a unit an idle agent on a committed branch is the person's
    move: the recorder keeps whether they were around for it too."""
    r, _ = rec()
    r.observe([row(stage="committed")], now=T, att=True)
    out = r.observe([row(stage="committed")], now=T + 5, att=False)
    assert out == [{"t": T + 5, "k": "ph", "s": "s1", "ph": "to_push", "a": False}]
    assert r.observe(
        [row(stage="committed", activity="working")], now=T + 10, att=True
    )[0] == {"t": T + 10, "k": "ph", "s": "s1", "ph": "agent", "a": False}


def git(wt, *args, when=None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "@%d +0000" % when
    out = subprocess.run(
        ["git", "-C", str(wt), *args],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return out.stdout.decode().strip()


def commit(wt, name, when):
    (wt / name).write_text(name)
    git(wt, "add", name)
    git(wt, "commit", "-q", "--no-verify", "-m", name, when=when)
    return git(wt, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    git(wt, "init", "-q", "-b", "feat")
    commit(wt, "base", T - 86400)
    return wt


def test_read_head_follows_loose_packed_and_linked_worktree_refs(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    assert R.read_head(str(repo)) == head
    git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "feat").exists()
    assert R.read_head(str(repo)) == head
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "other", str(linked))
    assert R.read_head(str(linked)) == head
    nxt = commit(linked, "x", T)
    assert R.read_head(str(linked)) == nxt and R.read_head(str(repo)) == head
    git(repo, "checkout", "-q", "--detach")
    assert R.read_head(str(repo)) == head
    assert R.read_head(str(tmp_path / "nope")) == "" and R.read_head("") == ""


def test_new_commits_counts_only_work_written_just_now(repo):
    base = git(repo, "rev-parse", "HEAD")
    one = commit(repo, "a", T + 10)
    two = commit(repo, "b", T + 20)
    assert R.new_commits(str(repo), base, two, T) == (2, False)
    assert R.new_commits(str(repo), one, two, T + 15) == (1, False)
    # an amend keeps the author time: no new work
    git(repo, "commit", "-q", "--amend", "--no-verify", "-m", "b2", when=T + 30)
    amended = git(repo, "rev-parse", "HEAD")
    assert R.new_commits(str(repo), two, amended, T + 25) == (0, False)
    # pulled work (authored before the old HEAD was seen) is no new work
    git(repo, "checkout", "-q", "-b", "side", base)
    old_side = commit(repo, "side", T - 3600)
    git(repo, "checkout", "-q", "feat")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "side", when=T + 40)
    merged = git(repo, "rev-parse", "HEAD")
    assert R.new_commits(str(repo), amended, merged, T + 35) == (0, True)
    assert old_side
    # an unknown old HEAD (gc'd after a rewrite): the new tip alone
    assert R.new_commits(str(repo), "f" * 40, amended, T) == (1, False)
    assert R.new_commits(str(repo / "nope"), base, amended, T) == (0, False)


def test_observe_writes_head_then_commit_rows(repo, monkeypatch):
    r, w = rec()
    rw = lambda **kw: row(path=str(repo), branch="feat", **kw)  # noqa: E731
    out = r.observe([rw()], now=T, att=False)
    base = git(repo, "rev-parse", "HEAD")
    assert {"t": T, "k": "head", "s": "s1", "sha": base} in out
    assert not [x for x in r.observe([rw()], now=T + 5, att=False) if x["k"] != "hb"]
    sha = commit(repo, "a", T + 7)
    out = r.observe([rw()], now=T + 10, att=False)
    assert out == [
        {"t": T + 10, "k": "commit", "s": "s1", "sha": sha, "n": 1, "merge": False}
    ]
    # a branch switch is a new baseline, not a commit
    git(repo, "checkout", "-q", "-b", "feat2")
    sha2 = commit(repo, "b", T + 12)
    out = r.observe([row(path=str(repo), branch="feat2")], now=T + 15, att=False)
    assert [x["k"] for x in out] == ["meta", "head"] and out[1]["sha"] == sha2
    # no path (or no repo) → no HEAD rows at all
    out = r.observe(
        [row(path=str(repo), branch="feat2"), row("other")], now=T + 20, att=False
    )
    assert [x["k"] for x in out] == ["meta", "ph"]


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


def test_units_end_at_commits_and_add_up():
    items = A.build_items(scenario(), T + 26000, ["s1", "s1-copy"], include_me=True)
    assert [it["id"] for it in items] == [
        "app@feature/sc-12/x#2",
        "app@feature/sc-12/x#1",
        "app@feature/sc-12/x#0",
    ]
    first = items[2]
    assert (
        first["status"] == "committed"
        and first["ticket"] == "sc-12"
        and first["pr"] == "https://x/pull/1"
    )
    # Recorded before HEAD was watched: the commit is where the second hook
    # run passed (+2760), and says it's an estimate.
    assert first["commit_at"] == T + 2760 and first["estimated"] is True
    assert first["lead_to_commit"] == 2760
    assert first["to_commit"] == {
        "setup": 60.0,
        "agent": 1920.0,
        "your_turn": 600.0,
        "precommit": 120.0,
        "hook_fix": 60.0,
    }
    assert sum(first["to_commit"].values()) == first["lead_to_commit"]
    # After it: pushed, then a PR — each wait ended in the next handoff. The
    # PR then sat until the rework turn, which starts the next unit.
    assert first["after_commit"] == {"to_push": 120.0, "to_pr": 120.0}
    assert (first["hook_runs"], first["hook_failures"]) == (2, 1)
    # The rework is a unit of its own; its commit is estimated at the end of
    # the work before the merge, and the review it waited in counts.
    rework = items[1]
    assert (rework["start"], rework["commit_at"]) == (T + 5000, T + 5600)
    assert rework["to_commit"] == {"agent": 600.0}
    assert rework["after_commit"] == {"review": 3400.0}
    # The next unit starts at real work (+20000): the post-merge idle and the
    # downtime belong to no unit; the copy window's work beats the idle one.
    nxt = items[0]
    assert nxt["status"] == "in_progress" and nxt["start"] == T + 20000
    assert nxt["commit_at"] is None and nxt["lead_to_commit"] is None
    assert nxt["to_commit"] == {"agent": 2000.0, "your_turn": 4000.0}
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
    assert it["to_commit"]["away"] == A.ALIVE_S + 100
    assert it["to_commit"]["untracked"] == 5000 - 100 - A.ALIVE_S
    assert it["to_commit"]["agent"] == 200
    assert A._fmt(5400) == "1h 30m" and A._fmt(90061) == "1d 1h" and A._fmt(42) == "42s"


def test_base_branches_list_only_committed_units():
    rows = [
        {"t": T, "k": "meta", "s": "s", "repo": "r", "branch": "main"},
        {"t": T, "k": "ph", "s": "s", "ph": "agent"},
    ]
    # a conversation on main is no unit in progress …
    assert A.build_items(rows, T + 100, ["s"]) == []
    # … but work committed there is a unit like any other
    rows += [
        {"t": T + 50, "k": "head", "s": "s", "sha": "a" * 40},
        {"t": T + 90, "k": "commit", "s": "s", "sha": "b" * 40, "n": 1},
    ]
    (it,) = A.build_items(rows, T + 100, ["s"])
    assert it["status"] == "committed" and it["lead_to_commit"] == 90


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
    # the first 300 s (your turn) is before the first real work
    assert it["to_commit"]["agent"] == 10000 * 300
    assert it["to_commit"]["your_turn"] == 9999 * 300


def test_abandoned_units_and_a_finished_branchs_leftovers():
    rows = [
        {"t": T, "k": "meta", "s": "gone", "repo": "r", "branch": "b"},
        {"t": T, "k": "ph", "s": "gone", "ph": "agent"},
        {"t": T + 60, "k": "end", "s": "gone"},
    ]
    assert A.build_items(rows, T + 100, ["other"])[0]["status"] == "abandoned"
    # After a commit, the closed window's last words are no abandoned unit.
    rows = [
        {"t": T, "k": "meta", "s": "w", "repo": "r", "branch": "b"},
        {"t": T, "k": "head", "s": "w", "sha": "a" * 40},
        {"t": T, "k": "ph", "s": "w", "ph": "agent"},
        {"t": T + 50, "k": "commit", "s": "w", "sha": "b" * 40, "n": 1},
        {"t": T + 60, "k": "ph", "s": "w", "ph": "to_push"},
        {"t": T + 70, "k": "ph", "s": "w", "ph": "agent"},
        {"t": T + 80, "k": "end", "s": "w"},
    ]
    assert [it["status"] for it in A.build_items(rows, T + 100, ["other"])] == [
        "committed"
    ]
    assert len(A.build_items(rows, T + 100, ["w"])) == 2


def test_a_closed_windows_last_stage_is_not_still_waiting():
    """A worker that committed and was closed (its branch merged locally,
    never pushed) left "committed, not pushed" as its last stage: that is
    no handoff still under way."""
    rows = [
        {"t": T, "k": "meta", "s": "w", "repo": "r", "branch": "b"},
        {"t": T, "k": "head", "s": "w", "sha": "a" * 40},
        {"t": T, "k": "ph", "s": "w", "ph": "agent"},
        {"t": T + 50, "k": "commit", "s": "w", "sha": "b" * 40, "n": 1},
        {"t": T + 60, "k": "ph", "s": "w", "ph": "to_push"},
        {"t": T + 400, "k": "end", "s": "w"},
    ] + [{"t": T + x, "k": "hb"} for x in range(0, 501, 300)]
    (gone,) = A.build_items(rows, T + 500, ["other"])
    assert gone["after_commit"] == {}
    (open_,) = A.build_items(rows, T + 500, ["w"])
    assert open_["after_commit"] == {"to_push": 340.0}


def test_report_kpis_stats_and_advice():
    rep = A.report(scenario(), T + 26000, 7 * 86400, ["s1", "s1-copy"], include_me=True)
    k = rep["kpi"]
    assert (k["items"], k["committed"], k["estimated"], k["in_progress"]) == (
        3,
        2,
        2,
        1,
    )
    # leads 2760 and 600
    assert k["median_to_commit"] == 1680 and k["p90_to_commit"] == 2760
    assert k["median_active_to_commit"] == 1680  # nothing away/untracked
    assert rep["basis"] == "committed"
    shares = {r["phase"]: r["share"] for r in rep["to_commit"]}
    assert (
        rep["to_commit"][0]["phase"] == "agent"
        and round(sum(shares.values()), 3) == 1.0
    )
    # Handoffs and review come after the commit: never in time to commit.
    assert {g["group"] for g in rep["to_commit_groups"]} == {"agent", "you", "tooling"}
    assert {r["phase"] for r in rep["after_commit"]} == {"to_push", "to_pr", "review"}
    phases = [a["phase"] for a in rep["advice"]]
    assert "your_turn" in phases and "hook_fix" in phases
    # fixes first, context (info) last
    levels = [a["level"] for a in rep["advice"]]
    assert levels == sorted(levels, key=lambda lv: lv == "info")
    assert rep["trend"][-1]["items"] == 2
    assert rep["trend"][-1]["median_to_commit"] == 1680
    assert [p["id"] for p in rep["phases"]] == A.PHASE_IDS


def test_empty_report():
    rep = A.report([], T, 7 * 86400, [])
    assert rep["kpi"]["items"] == 0 and rep["kpi"]["median_to_commit"] is None
    assert rep["advice"] == [] and rep["items"] == []


def test_idle_gaps_between_turns_after_an_early_commit_are_yours():
    """The orchestrator pattern: a commit lands early (a worker merged in),
    then the agent idles between turns on a branch that reads "committed,
    not pushed". Those gaps are the person's move — not a handoff — and the
    unit's own clock only counts the work."""
    rows = [
        {"t": T, "k": "meta", "s": "o", "repo": "app", "branch": "feat/r6"},
        {"t": T, "k": "head", "s": "o", "sha": "a" * 40},
        {"t": T, "k": "ph", "s": "o", "ph": "agent", "a": False},
        {"t": T + 600, "k": "commit", "s": "o", "sha": "b" * 40, "n": 1},
        {"t": T + 610, "k": "ph", "s": "o", "ph": "to_push", "a": True},
        {"t": T + 1900, "k": "ph", "s": "o", "ph": "agent", "a": False},
        {"t": T + 2200, "k": "ph", "s": "o", "ph": "to_push", "a": False},
        {"t": T + 2500, "k": "ph", "s": "o", "ph": "agent", "a": False},
        {"t": T + 2800, "k": "commit", "s": "o", "sha": "c" * 40, "n": 2},
        {"t": T + 2810, "k": "ph", "s": "o", "ph": "to_push", "a": True},
        {"t": T + 3000, "k": "ph", "s": "o", "ph": "to_pr", "a": True},
    ] + [{"t": T + x, "k": "hb"} for x in range(0, 3301, 300)]
    rows.sort(key=lambda r: r["t"])
    second, first = A.build_items(rows, T + 3300, ["o"])
    assert (first["start"], first["commit_at"], first["estimated"]) == (
        T,
        T + 600,
        False,
    )
    # the agent's summing-up after the commit (+600..+610) is no unit
    assert first["to_commit"] == {"agent": 600.0}
    # sat "committed, not pushed" until the next turn: not a handoff
    assert first["after_commit"] == {}
    assert second["start"] == T + 1900 and second["commit_at"] == T + 2800
    assert second["to_commit"] == {"agent": 600.0}
    assert second["your_time_s"] == 300  # the idle gap mid-unit, cut out
    full = A.build_items(rows, T + 3300, ["o"], include_me=True)[0]
    assert full["to_commit"] == {"agent": 600.0, "away": 300.0}
    # this time the push happened: that wait counts, and the open PR is
    # still waiting now
    assert second["after_commit"] == {"to_push": 190.0, "to_pr": 300.0}


def test_recorded_commits_beat_estimates_and_skip_non_work():
    rows = [
        {"t": T, "k": "meta", "s": "a", "repo": "r", "branch": "b"},
        {"t": T, "k": "meta", "s": "a-copy", "repo": "r", "branch": "b"},
        {"t": T, "k": "ph", "s": "a", "ph": "agent"},
        # before HEAD was watched: hooks that passed are an estimated commit
        {"t": T + 100, "k": "ph", "s": "a", "ph": "precommit"},
        {"t": T + 160, "k": "ph", "s": "a", "ph": "to_push"},
        {"t": T + 200, "k": "head", "s": "a", "sha": "0" * 40},
        {"t": T + 300, "k": "ph", "s": "a", "ph": "agent"},
        # watched from here: hooks alone close nothing …
        {"t": T + 400, "k": "ph", "s": "a", "ph": "precommit"},
        {"t": T + 460, "k": "ph", "s": "a", "ph": "agent"},
        # … a merge commit, a pull (n 0) close nothing …
        {"t": T + 500, "k": "commit", "s": "a", "sha": "1" * 40, "n": 3, "merge": True},
        {"t": T + 520, "k": "commit", "s": "a", "sha": "2" * 40, "n": 0},
        # … a real commit does, once, though both windows saw it
        {"t": T + 600, "k": "commit", "s": "a", "sha": "3" * 40, "n": 1},
        {"t": T + 605, "k": "commit", "s": "a-copy", "sha": "3" * 40, "n": 1},
        {"t": T + 610, "k": "ph", "s": "a", "ph": "to_push"},
    ] + [{"t": T + x, "k": "hb"} for x in range(0, 701, 300)]
    rows.sort(key=lambda r: r["t"])
    items = A.build_items(rows, T + 700, ["a", "a-copy"])
    assert [(it["commit_at"], it["estimated"]) for it in items] == [
        (T + 600, False),
        (T + 160, True),
    ]
    assert items[0]["hook_runs"] == 1 and items[0]["lead_to_commit"] == 300


# --------------------------------------------------------------------------- #
# Your own time: left out by default, counted on request
# --------------------------------------------------------------------------- #
GOLDEN = os.path.join(
    os.path.dirname(__file__), "data", "workflow_report_include_me.golden.json"
)
PERSON_LIVE = ["a", "b", "c", "d"]


def test_include_me_is_the_full_clock_byte_for_byte():
    """The full clock, pinned: the golden file holds the analysis's answer for
    both scenarios with the person's time counted. Regenerate it on purpose
    (and say why in the commit) when the analysis is meant to change."""
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


def test_leaving_me_out_compresses_each_units_clock():
    mine = {
        it["id"]: it
        for it in A.build_items(
            person_scenario(), T + 9000, PERSON_LIVE, include_me=True
        )
    }
    items = {
        it["id"]: it for it in A.build_items(person_scenario(), T + 9000, PERSON_LIVE)
    }
    # Same units either way: ids, status and the real epochs don't move.
    assert set(items) == set(mine)
    for k, it in items.items():
        for f in (
            "status",
            "start",
            "commit_at",
            "estimated",
            "end",
            "hook_runs",
            "sessions",
            "after_commit",
        ):
            assert it[f] == mine[k][f], (k, f)
        assert not set(it["to_commit"]) & A.PERSON_PHASES
        assert not set(it["after_commit"]) & A.PERSON_PHASES
        assert not {sg["ph"] for sg in it["timeline"]} & A.PERSON_PHASES
    a = items["app@feature/a#0"]
    # 4660 s wall to the commit (hooks passed), 300 + 600 + 1900 of it
    # waiting on the person.
    assert a["commit_at"] == T + 4660 and a["estimated"] is True
    assert a["your_time_s"] == 2800
    assert a["lead_to_commit"] == 4660 - 2800 == sum(a["to_commit"].values())
    assert a["to_commit"] == {"setup": 60.0, "agent": 1740.0, "precommit": 60.0}
    assert a["active_to_commit"] == 1860
    # pushed, a PR, then review until the merge (less the downtime)
    assert a["after_commit"] == {"to_push": 60.0, "to_pr": 120.0, "review": 2440.0}
    # The strip closes the holes: the two agent stints around the question
    # and the turn the person sat on are one stretch.
    tl = a["timeline"]
    assert [sg["ph"] for sg in tl] == ["setup", "agent", "precommit"]
    assert all(x["end"] == y["start"] for x, y in zip(tl, tl[1:]))
    assert tl[1] == {"ph": "agent", "start": T + 60, "end": T + 1800, "at": T + 60}
    assert tl[-1]["end"] - tl[0]["start"] == a["lead_to_commit"]
    assert tl[-1]["at"] == T + 4600
    # b: the PR was opened from an idle branch — that's its commit
    b = items["app@feature/b#0"]
    assert (b["commit_at"], b["lead_to_commit"]) == (T + 5000, 600)
    assert b["after_commit"] == {"review": 4000.0 - 720}  # still open now
    # Nothing but the person before its commit: zero, not a crash.
    c = items["app@feature/c#0"]
    assert (
        c["lead_to_commit"],
        c["active_to_commit"],
        c["to_commit"],
        c["your_time_s"],
    ) == (0.0, 0, {}, 1000)
    d = items["app@feature/d#0"]
    assert d["status"] == "in_progress" and d["lead_to_commit"] is None
    assert d["elapsed"] == 6000 - 4780 and d["to_commit"] == {
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
    assert (k["items"], k["committed"], k["in_progress"]) == (4, 3, 1)
    # leads 1860 / 600 / 0 (was 4660 / 4000 / 1000)
    assert (k["median_to_commit"], k["p90_to_commit"]) == (600, 1860)
    assert k["median_active_to_commit"] == 600
    stats = {r["phase"]: r for r in rep["to_commit"]}
    assert set(stats) == {"agent", "setup", "precommit"}
    assert round(sum(r["share"] for r in rep["to_commit"]), 3) == 1.0
    assert stats["agent"]["total_s"] == 2340 and stats["agent"]["share"] == round(
        2340 / 2460, 4
    )
    assert (
        stats["agent"]["median_s"],
        stats["agent"]["p90_s"],
        stats["agent"]["items"],
    ) == (1170, 1740, 2)
    assert {g["group"] for g in rep["to_commit_groups"]} == {"agent", "tooling"}
    assert round(sum(g["share"] for g in rep["to_commit_groups"]), 3) == 1.0
    assert not {r["phase"] for r in rep["after_commit"]} & A.PERSON_PHASES
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
    assert rep["trend"][-1]["items"] == 3
    assert rep["trend"][-1]["median_to_commit"] == 600
    assert "excluded" not in full


def test_a_unit_that_was_all_me():
    rows = [
        {"t": T, "k": "meta", "s": "s", "repo": "r", "branch": "b"},
        {"t": T, "k": "ph", "s": "s", "ph": "your_turn", "a": True},
        {"t": T + 300, "k": "ph", "s": "s", "ph": "question", "a": True},
        {"t": T + 400, "k": "ph", "s": "s", "ph": "review", "a": False},
    ] + [{"t": T + x, "k": "hb"} for x in range(0, 1001, 300)]
    rep = A.report(rows, T + 1000, 7 * 86400, ["s"])
    assert rep["kpi"]["median_to_commit"] == 0 and rep["kpi"]["p90_to_commit"] == 0
    assert rep["to_commit"] == [] and rep["to_commit_groups"] == []
    assert rep["advice"] == []
    assert rep["excluded"]["total_s"] == 100
    it = rep["items"][0]
    # the question is the first real work; the PR opening is its commit
    assert (it["start"], it["commit_at"]) == (T + 300, T + 400)
    assert it["lead_to_commit"] == 0 and it["timeline"] == []
    assert it["after_commit"] == {"review": 600.0}
    # still waiting on you, nothing committed: nothing counted yet, and no
    # division by zero
    rows = rows[:3] + rows[4:]
    rep = A.report(rows, T + 1000, 7 * 86400, ["s"])
    assert rep["basis"] == "all" and rep["items"][0]["elapsed"] == 0
    assert rep["items"][0]["timeline"] == [] and rep["to_commit"] == []


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
    assert body["range"] == "30d" and body["kpi"]["committed"] == 2
    assert all("timeline" not in it for it in body["items"])
    item_id = body["items"][2]["id"]
    it = client.get("/api/workflow/item", params={"id": item_id}).json()
    assert it["status"] == "committed" and it["timeline"][0]["ph"] == "setup"
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
    off = {"include_me": False, "hidden": [], "count_after": []}
    on = {"include_me": True, "hidden": [], "count_after": []}
    assert store.settings_path() == str(sp)
    assert store.load_settings() == off and not sp.exists()
    assert client.get("/api/workflow/settings").json() == off
    assert client.post("/api/workflow/settings", json={"include_me": True}).json() == on
    assert json.loads(sp.read_text()) == on
    assert oct(sp.stat().st_mode & 0o777) == "0o600"
    # wrong types and unknown keys change nothing
    body = client.post(
        "/api/workflow/settings", json={"include_me": "no", "hidden": "x", "x": 1}
    ).json()
    assert body == on and store.load_settings() == on
    sp.write_text("{torn")
    assert store.load_settings() == off
    assert p.exists()


def test_hidden_setting_keeps_strings_only_and_merges_alone(tmp_path, monkeypatch):
    _store_person_scenario(tmp_path, monkeypatch)
    body = client.post(
        "/api/workflow/settings",
        json={"hidden": ["precommit", 3, "", "precommit", "bogus"]},
    ).json()
    # strings kept (deduplicated); the analysis drops ids it doesn't know
    assert body == {
        "include_me": False,
        "hidden": ["precommit", "bogus"],
        "count_after": [],
    }
    assert client.post("/api/workflow/settings", json={"include_me": True}).json() == {
        "include_me": True,
        "hidden": ["precommit", "bogus"],
        "count_after": [],
    }
    assert client.get("/api/workflow/summary").json()["hide"] == ["precommit"]
    assert client.post("/api/workflow/settings", json={"hidden": []}).json() == {
        "include_me": True,
        "hidden": [],
        "count_after": [],
    }


def test_summary_and_item_follow_the_setting_and_the_override(tmp_path, monkeypatch):
    _store_person_scenario(tmp_path, monkeypatch)

    def phases(body):
        return {r["phase"] for r in body["to_commit"]} | {
            r["phase"] for r in body["after_commit"]
        }

    body = client.get("/api/workflow/summary?range=7d").json()
    assert body["include_me"] is False and not phases(body) & A.PERSON_PHASES
    assert (
        body["excluded"]["total_s"] == 7200 and body["kpi"]["median_to_commit"] == 600
    )
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


def test_a_hidden_phase_is_cut_out_like_your_time(tmp_path, monkeypatch):
    """Hiding a phase takes it out of every number (not just the legend), keeps
    it in ``phases`` so the legend can offer it back, and says how much went."""
    _store_person_scenario(tmp_path, monkeypatch)
    base = client.get("/api/workflow/summary?range=7d&hide=").json()
    assert base["hide"] == [] and "hidden" not in base
    row = max(base["to_commit"], key=lambda r: r["total_s"])
    ph = row["phase"]
    assert ph not in A.PERSON_PHASES
    body = client.get(
        "/api/workflow/summary", params={"range": "7d", "hide": ph}
    ).json()
    assert body["hide"] == [ph]
    assert ph not in {r["phase"] for r in body["to_commit"]}
    assert ph in {p["id"] for p in body["phases"]}
    assert body["hidden"] == {"phases": [ph], "total_s": row["total_s"]}
    assert body["kpi"]["median_to_commit"] < base["kpi"]["median_to_commit"]
    assert body["kpi"]["committed"] == base["kpi"]["committed"]
    assert body["advice"] == [a for a in body["advice"] if a["phase"] != ph]
    it = next(x for x in body["items"] if x["to_commit"] or x.get("hidden_s"))
    assert ph not in it["to_commit"]
    tl = client.get("/api/workflow/item", params={"id": it["id"], "hide": ph}).json()
    assert ph not in {sg["ph"] for sg in tl["timeline"]} and "at" in tl["timeline"][0]
    # the saved setting is the default; an explicit empty override shows all
    store.save_settings({"hidden": [ph]})
    assert client.get("/api/workflow/summary?range=7d").json()["hide"] == [ph]
    assert client.get("/api/workflow/summary?range=7d&hide=").json()["hide"] == []


def test_hiding_nothing_is_the_report_unchanged():
    for mine in (False, True):
        a = A.report(
            scenario(), T + 26000, 7 * 86400, ["s1", "s1-copy"], include_me=mine
        )
        b = A.report(
            scenario(),
            T + 26000,
            7 * 86400,
            ["s1", "s1-copy"],
            include_me=mine,
            hidden=["not-a-phase"],
        )
        assert a == b


def test_hiding_a_person_phase_is_left_to_include_me():
    """Your own phases have their own switch: hiding one while it is already
    cut changes nothing, and with your time counted it is cut like any other."""
    rows, now = person_scenario(), T + 9000
    off = A.report(rows, now, 7 * 86400, PERSON_LIVE)
    assert A.report(rows, now, 7 * 86400, PERSON_LIVE, hidden=["your_turn"]) == off
    on = A.report(
        rows, now, 7 * 86400, PERSON_LIVE, include_me=True, hidden=["your_turn"]
    )
    assert on["hidden"]["phases"] == ["your_turn"]
    assert "your_turn" not in {r["phase"] for r in on["to_commit"]}


def test_after_commit_handoffs_count_only_when_asked(tmp_path, monkeypatch):
    """Pushing, the PR and review stay out of the headline by default; each
    one counted adds its waits to the committed units' clocks (and their
    timelines, right after the commit), while ``after_commit`` keeps every
    handoff either way."""
    _store_person_scenario(tmp_path, monkeypatch)
    base = client.get("/api/workflow/summary?range=7d").json()
    assert base["count_after"] == [] and "count_after" not in A.report([], T)
    after = {r["phase"]: r["total_s"] for r in base["after_commit"]}
    assert after, "the scenario needs a handoff after a commit"
    ph = max(after, key=after.get)
    assert ph not in {r["phase"] for r in base["to_commit"]}
    body = client.get(
        "/api/workflow/summary", params={"range": "7d", "count_after": ph + ",agent"}
    ).json()
    assert body["count_after"] == [ph]
    assert {r["phase"]: r["total_s"] for r in body["to_commit"]}[ph] == after[ph]
    assert body["after_commit"] == base["after_commit"]
    lead = {it["id"]: it["lead_to_commit"] for it in base["items"]}
    grew = [
        it
        for it in body["items"]
        if it["after_commit"].get(ph)
        and it["lead_to_commit"] == lead[it["id"]] + it["after_commit"][ph]
    ]
    assert grew
    tl = client.get(
        "/api/workflow/item", params={"id": grew[0]["id"], "count_after": ph}
    ).json()["timeline"]
    assert tl[-1]["ph"] == ph and tl[-1]["at"] >= grew[0]["commit_at"]
    # saved, it is the default; hiding a phase wins over counting it
    store.save_settings({"count_after": [ph]})
    assert client.get("/api/workflow/summary").json()["count_after"] == [ph]
    both = client.get("/api/workflow/summary", params={"hide": ph}).json()
    assert both["count_after"] == [ph] and ph not in {
        r["phase"] for r in both["to_commit"]
    }
