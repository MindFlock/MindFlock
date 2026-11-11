"""Queued starts as an orchestrating agent sees them (backend.mcp.waiting):

* a detached-HEAD parent never sends ``base_branch: "HEAD"``, and a 400 about
  the base drops ``base_branch`` before ``base_ref``;
* a queued result is worded from the launch gate's ``why`` with stable
  machine fields (spawn_session and spawn_ticket_session alike);
* list_sessions rows carry ``waiting`` / ``start_failed``;
* wait_for_session never reads a queued child as finished, names queued
  children on timeout, and returns ``failed_to_start`` for a failed create.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import client
from backend.mcp import tools as mcp_tools
from backend.mcp import waiting
from backend.mcp.protocol import ToolError
from tests.unit._mcp_fakes import FakeClock, FakeCtx, make_box, row
from tests.unit.test_mcp_ship_tools import _tickets
from tests.unit.test_mcp_tools import _commit, _git, _git_repo, _tree, _worktree


def _ctx(clock, **kw):
    return FakeCtx(clock, **kw)


def _posts(api):
    return [c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")]


# --------------------------------------------------------------------------- #
# A. the fork base
# --------------------------------------------------------------------------- #
@pytest.fixture
def detached_flock(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    orch = _worktree(repo, tmp_path / "wt-orch", "mindflock/orch")
    _commit(orch, "orch.txt")
    _git(orch, "checkout", "-q", "--detach")
    rows = [row("orch", folder=str(orch), path=str(repo), branch="HEAD")]
    return repo, orch, rows


class TestForkBase:
    def test_detached_head_sends_base_ref_alone(self, detached_flock):
        _, orch, rows = detached_flock
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        (payload,) = _posts(api)
        sha = _git(orch, "rev-parse", "HEAD")
        assert payload["base_ref"] == sha and "base_branch" not in payload
        assert out["base_sha"] == sha and "warnings" not in out

    def test_empty_branch_sends_no_base_branch(self, detached_flock):
        _, _, rows = detached_flock
        rows[0]["branch"] = ""
        box, api, clock = make_box(rows)
        api.created_status = "running"
        box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert "base_branch" not in _posts(api)[0]

    def test_refused_base_branch_retries_with_base_ref(self, detached_flock):
        _, orch, rows = detached_flock
        rows[0]["branch"] = "weird..branch"
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create

        def picky(payload):
            if "base_branch" in payload:
                raise client.ApiError(400, "invalid base_branch: weird..branch")
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        sha = _git(orch, "rev-parse", "HEAD")
        assert out["base_sha"] == sha
        assert len(out["warnings"]) == 1
        assert "still forks from your HEAD commit" in out["warnings"][0]
        assert "weird..branch" in out["warnings"][0]

    def test_both_refused_falls_back_to_repo_head(self, detached_flock):
        _, _, rows = detached_flock
        rows[0]["branch"] = "mindflock/orch"
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create
        seen = []

        def picky(payload):
            seen.append(sorted(k for k in payload if k.startswith("base")))
            if "base_ref" in payload:
                raise client.ApiError(400, "unknown base_ref: abc")
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert seen == [["base_branch", "base_ref"], ["base_ref"], []]
        assert out["base_sha"] is None
        # Only the warning that is true in the end: no fork from our HEAD.
        assert len(out["warnings"]) == 1
        assert "could not fork from your HEAD" in out["warnings"][0]

    def test_other_400_is_not_retried(self, detached_flock):
        _, _, rows = detached_flock
        box, api, clock = make_box(rows)
        api.errors[("POST", "/api/instances")] = client.ApiError(400, "bad prompt")
        with pytest.raises(Exception, match="bad prompt"):
            box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert len(_posts(api)) == 1

    def test_branch_whitespace_is_stripped(self, detached_flock):
        _, _, rows = detached_flock
        rows[0]["branch"] = "  mindflock/orch \n"
        box, api, clock = make_box(rows)
        api.created_status = "running"
        box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert _posts(api)[0]["base_branch"] == "mindflock/orch"

    def test_blank_branch_sends_no_base_branch(self, detached_flock):
        _, _, rows = detached_flock
        rows[0]["branch"] = "   "
        box, api, clock = make_box(rows)
        api.created_status = "running"
        box.spawn_session({"prompt": "p"}, _ctx(clock))
        (payload,) = _posts(api)
        assert "base_branch" not in payload and "base_ref" in payload

    def test_detached_head_that_cannot_be_read_sends_neither_key(self):
        rows = _tree()  # folders that do not exist: no HEAD to read
        rows[1]["branch"] = "HEAD"
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        (payload,) = _posts(api)
        assert "base_ref" not in payload and "base_branch" not in payload
        assert out["base_sha"] is None

    def test_detached_head_refused_goes_straight_to_the_repo_head(self, detached_flock):
        _, _, rows = detached_flock
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create
        seen = []

        def picky(payload):
            seen.append(sorted(k for k in payload if k.startswith("base")))
            if "base_ref" in payload:
                raise client.ApiError(400, "unknown base_ref: abc")
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert seen == [["base_ref"], []]
        assert out["base_sha"] is None
        assert len(out["warnings"]) == 1
        assert "could not fork from your HEAD" in out["warnings"][0]
        assert "unknown base_ref: abc" in out["warnings"][0]
        assert "base branch" not in out["warnings"][0]

    def test_a_base_400_with_no_base_keys_is_raised(self):
        box, api, clock = make_box(_tree())  # no readable HEAD: no base keys
        api.errors[("POST", "/api/instances")] = client.ApiError(
            400, "the base of this repository is unreadable"
        )
        with pytest.raises(ToolError, match="base of this repository"):
            box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert len(_posts(api)) == 1

    def test_a_title_race_then_a_refused_base_branch_still_resolves(
        self, detached_flock
    ):
        _, orch, rows = detached_flock
        rows[0]["branch"] = "mindflock/orch"
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create
        seen = []

        def picky(payload):
            seen.append(
                (payload["title"], sorted(k for k in payload if k.startswith("base")))
            )
            if payload["title"] == "orch-w1":
                raise client.ApiError(409, "instance orch-w1 already exists")
            if "base_branch" in payload:
                raise client.ApiError(400, "invalid base_branch: mindflock/orch")
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert seen == [
            ("orch-w1", ["base_branch", "base_ref"]),
            ("orch-w2", ["base_branch", "base_ref"]),
            ("orch-w2", ["base_ref"]),
        ]
        assert out["title"] == "orch-w2"
        assert out["base_sha"] == _git(orch, "rev-parse", "HEAD")
        assert len(out["warnings"]) == 1
        assert "still forks from your HEAD commit" in out["warnings"][0]

    def test_a_dropped_base_branch_is_not_resent_for_the_next_title(
        self, detached_flock
    ):
        _, orch, rows = detached_flock
        rows[0]["branch"] = "mindflock/orch"
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create
        seen = []

        def picky(payload):
            seen.append(
                (payload["title"], sorted(k for k in payload if k.startswith("base")))
            )
            if "base_branch" in payload:
                raise client.ApiError(400, "invalid base_branch: mindflock/orch")
            if payload["title"] == "orch-w1":
                raise client.ApiError(409, "instance orch-w1 already exists")
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert seen == [
            ("orch-w1", ["base_branch", "base_ref"]),
            ("orch-w1", ["base_ref"]),
            ("orch-w2", ["base_ref"]),
        ]
        assert out["title"] == "orch-w2"
        assert out["base_sha"] == _git(orch, "rev-parse", "HEAD")


# --------------------------------------------------------------------------- #
# B. the queued result
# --------------------------------------------------------------------------- #
def _queue_box(why, position=1, reason="r"):
    box, api, clock = make_box(_tree())

    def queued(payload, q):
        rec = {
            "kind": "spawn",
            "since": 1.0,
            "reason": reason,
            "position": position,
            "creates": True,
        }
        if why is not None:
            rec["why"] = why
        return {
            "title": payload["title"],
            "queued": True,
            "status": "queued: waiting for memory",
            "reason": reason,
            "position": position,
            "waiting_for_memory": rec,
        }

    api.top_routes[("POST", "/api/instances")] = queued
    return box, api, clock


@pytest.mark.parametrize(
    "why,position,status",
    [
        ("memory", 1, "queued: waiting for memory"),
        ("warming", 1, "queued: waiting for memory"),
        ("spacing", 1, "queued: starting shortly (automatic starts are staggered)"),
        ("cap", 1, "queued: waiting for a free agent slot"),
        ("queue", 3, "queued: 2 ahead of it"),
        ("queue", 1, "queued: next in line"),
        ("restart", 1, "queued: waiting to restart"),
        ("something-new", 1, "queued: waiting for memory"),
        (None, 1, "queued: waiting for memory"),
    ],
)
def test_queued_spawn_is_worded_from_why(why, position, status):
    box, api, clock = _queue_box(why, position)
    out = box.spawn_session({"prompt": "p"}, _ctx(clock))
    assert out["status"] == status
    assert out["queued"] is True and out["ready"] is False
    assert out["why"] == why and out["position"] == position
    assert out["reason"] == "r"


_CAPACITY = {"room": 0, "session_mb": 950, "short_mb": 400, "hint": "no room"}


@pytest.mark.parametrize(
    "why,blames_memory",
    [
        (None, True),  # an older server: no why, the memory wording
        ("memory", True),
        ("warming", True),
        ("spacing", False),
        ("cap", False),
        ("queue", False),
        ("restart", False),
    ],
)
def test_only_a_memory_queue_adds_the_memory_hint(why, blames_memory):
    box, api, clock = _queue_box(why)
    api.top_routes[("GET", "/api/memory/capacity")] = dict(_CAPACITY)
    out = box.spawn_session({"prompt": "p"}, _ctx(clock))
    assert out["hint"].startswith(waiting.QUEUED_HINT)
    assert ("400 MB more would have to be free" in out["hint"]) is blames_memory
    assert ("One more session needs ~950 MB" in out["hint"]) is blames_memory
    assert out["capacity"]["short_mb"] == 400  # the numbers stay either way


def test_a_queued_spawn_keeps_its_other_keys_and_does_not_wait():
    box, api, clock = _queue_box("cap")
    ctx = _ctx(clock)
    out = box.spawn_session({"prompt": "p"}, ctx)
    assert out["title"] == "orch-w1" and out["ready"] is False
    assert out["base_sha"] is None and out["report_back"] is True
    assert "wait_for_session" in out["hint"]
    assert ctx.slept == [] and not ctx.progress_total  # no 45 s ready wait
    assert "orch-w1" in box.policy.spawned_by_me


def test_queued_ticket_spawn_has_the_same_keys():
    box, api, clock = make_box(_tree())
    api.top_routes[("GET", "/api/tickets")] = _tickets()

    def start(payload, q):
        return {
            "started": True,
            "title": "sc-23588",
            "queued": True,
            "status": "queued: waiting for memory",
            "memory_reason": "the live-agent cap (6) is reached",
            "position": 2,
            "waiting_for_memory": {
                "kind": "ticket",
                "since": 1.0,
                "position": 2,
                "creates": True,
                "reason": "the live-agent cap (6) is reached",
                "why": "cap",
            },
            "reason": "this CLI does not attach the MindFlock tools",
            "parent": payload.get("parent", ""),
            "spawned": True,
            "report_back": False,
        }

    api.top_routes[("POST", "/api/tickets/start")] = start
    out = box.spawn_ticket_session({"ticket": "SC-23588"}, _ctx(clock))
    assert out["status"] == "queued: waiting for a free agent slot"
    assert out["queued"] is True and out["why"] == "cap" and out["position"] == 2
    assert out["reason"] == "the live-agent cap (6) is reached"
    assert out["memory_reason"] == out["reason"]  # the old key, kept
    assert out["reason_no_report"].startswith("this CLI")
    assert "`why` says what it waits for" in out["hint"]


def _ticket_box(body):
    box, api, clock = make_box(_tree())
    api.top_routes[("GET", "/api/tickets")] = _tickets()
    api.top_routes[("POST", "/api/tickets/start")] = lambda payload, q: dict(
        {"started": True, "title": "sc-23588", "spawned": True}, **body
    )
    return box, api, clock


def test_queued_ticket_spawn_without_a_report_back_note():
    box, api, clock = _ticket_box(
        {
            "queued": True,
            "memory_reason": "spaced",
            "position": 1,
            "waiting_for_memory": {"reason": "spaced", "position": 1, "why": "spacing"},
            "report_back": True,
        }
    )
    ctx = _ctx(clock)
    out = box.spawn_ticket_session({"ticket": "SC-23588"}, ctx)
    assert out["status"] == "queued: starting shortly (automatic starts are staggered)"
    assert out["reason"] == out["memory_reason"] == "spaced"
    assert "reason_no_report" not in out
    assert out["hint"] == (
        waiting.QUEUED_HINT + " wait_for_session waits through the queue"
    )
    assert out["ready"] is False and ctx.slept == []


def test_queued_ticket_spawn_from_an_older_server():
    box, api, clock = _ticket_box(
        {
            "queued": True,
            "status": "queued: waiting for memory",
            "memory_reason": "memory is low",
            "position": 2,
        }
    )
    out = box.spawn_ticket_session({"ticket": "SC-23588"}, _ctx(clock))
    assert out["status"] == "queued: waiting for memory" and out["queued"] is True
    assert out["why"] is None and out["position"] == 2
    assert out["reason"] == out["memory_reason"] == "memory is low"
    assert "reason_no_report" not in out
    # No report_back in the body: the upgrade warning is still given.
    assert any("ignores parent/report-back" in w for w in out["warnings"])


def test_queued_ticket_spawn_never_reads_the_report_back_note_as_the_gates():
    box, api, clock = _ticket_box(
        {
            "queued": True,
            "memory_reason": "",
            "reason": "this CLI does not attach the MindFlock tools",
            "report_back": False,
        }
    )
    out = box.spawn_ticket_session({"ticket": "SC-23588"}, _ctx(clock))
    assert out["reason"] == out["memory_reason"] == ""
    assert out["reason_no_report"].startswith("this CLI")
    assert not any("ignores parent/report-back" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# C. list_sessions rows
# --------------------------------------------------------------------------- #
def test_compact_rows_carry_waiting_and_start_failed():
    rows = _tree() + [
        row(
            "q1",
            parent="orch",
            spawned=True,
            pending=True,
            status="loading",
            waiting_for_memory={
                "kind": "spawn",
                "since": 1.0,
                "reason": "spaced",
                "position": 2,
                "creates": True,
                "why": "spacing",
            },
        ),
        row(
            "f1",
            parent="orch",
            spawned=True,
            pending=True,
            status="loading",
            waiting_for_memory=None,
            start_failed={"error": "boom", "at": 5.0, "prompt_preview": "do x"},
        ),
    ]
    box, api, clock = make_box(rows)
    by = {
        r["title"]: r
        for r in box.list_sessions({"filter": "all"}, _ctx(clock))["sessions"]
    }
    assert by["q1"]["waiting"] == {"why": "spacing", "position": 2, "reason": "spaced"}
    assert "start_failed" not in by["q1"]
    assert by["f1"]["start_failed"]["error"] == "boom"
    assert "waiting" not in by["f1"]
    assert "waiting" not in by["w1"] and "start_failed" not in by["w1"]


def _listed(rows):
    box, api, clock = make_box(rows)
    out = box.list_sessions({"filter": "all"}, _ctx(clock))
    return {r["title"]: r for r in out["sessions"]}


def test_compact_row_passes_the_whole_start_failed_record():
    # The row carries the server's record as it is, not only its error.
    rec = {"error": "boom", "at": 5.0, "prompt_preview": "do x"}
    by = _listed(_tree() + [row("f1", parent="orch", start_failed=dict(rec))])
    assert by["f1"]["start_failed"] == rec
    by = _listed(_tree() + [row("f1", parent="orch", start_failed="boom")])
    assert by["f1"]["start_failed"] == {"error": "boom"}


@pytest.mark.parametrize("empty", [None, {}, "", "junk"])
def test_compact_row_without_a_gate_record_has_no_waiting(empty):
    # start_failed stays None: any dict or truthy string there would count.
    rows = _tree() + [
        row("q1", parent="orch", waiting_for_memory=empty, start_failed=None)
    ]
    by = _listed(rows)
    assert "waiting" not in by["q1"] and "start_failed" not in by["q1"]


def test_compact_row_of_a_queued_restart():
    rows = _tree()
    rows[2]["waiting_for_memory"] = {
        "kind": "restart",
        "since": 1.0,
        "reason": "memory is low",
        "position": 1,
        "creates": False,
        "why": "restart",
    }
    by = _listed(rows)
    assert by["w1"]["waiting"] == {
        "why": "restart",
        "position": 1,
        "reason": "memory is low",
    }


def test_compact_row_of_an_older_servers_record_has_no_why():
    rows = _tree()
    rows[2]["waiting_for_memory"] = {"reason": "low", "position": 2}
    assert _listed(rows)["w1"]["waiting"] == {"position": 2, "reason": "low"}


# --------------------------------------------------------------------------- #
# D. wait_for_session
# --------------------------------------------------------------------------- #
def _wait_box(rows):
    clock = FakeClock()
    box, api, _ = make_box(rows, clock=clock)
    box.wait_poll_s = 3.0
    return box, api, clock


def test_a_child_queued_to_restart_is_never_offline():
    rows = _tree()
    box, api, clock = _wait_box(rows)
    w1 = api.row("w1")
    w1.update(
        activity="offline",
        status="ready",
        activity_since=1.0,
        waiting_for_memory={
            "kind": "restart",
            "since": 1.0,
            "reason": "low",
            "position": 1,
            "creates": False,
            "why": "restart",
        },
    )
    out = box.wait_for_session({"titles": ["w1"], "timeout_s": 90}, _ctx(clock))
    assert out["timed_out"] is True and out["still_running"] == ["w1"]
    assert out["queued"]["w1"]["why"] == "restart"
    assert out["queued"]["w1"]["status"] == "queued: waiting to restart"
    assert out["hint"].startswith("call wait_for_session again")
    assert "those in `queued` have not started yet" in out["hint"]


def test_a_queued_create_on_timeout_is_named_with_why():
    rows = _tree() + [
        row(
            "q1",
            parent="orch",
            spawned=True,
            pending=True,
            status="loading",
            activity="offline",
            activity_since=0,
            waiting_for_memory={"reason": "cap", "position": 3, "why": "cap"},
        ),
    ]
    box, api, clock = _wait_box(rows)
    api.row("w1")["activity"] = "working"
    out = box.wait_for_session({"titles": ["q1", "w1"], "timeout_s": 9}, _ctx(clock))
    assert out["still_running"] == ["q1", "w1"]  # still a list of titles
    assert set(out["queued"]) == {"q1"}
    assert out["queued"]["q1"] == {
        "why": "cap",
        "position": 3,
        "reason": "cap",
        "status": "queued: waiting for a free agent slot",
    }


def test_no_queued_map_when_nothing_is_queued():
    box, api, clock = _wait_box(_tree())
    api.row("w1")["activity"] = "working"
    out = box.wait_for_session({"titles": ["w1"], "timeout_s": 6}, _ctx(clock))
    assert "queued" not in out
    assert out["hint"] == "call wait_for_session again with the titles in still_running"


def test_a_failed_start_returns_at_once_with_the_error():
    rows = _tree() + [
        row(
            "f1",
            parent="orch",
            spawned=True,
            pending=True,
            status="loading",
            activity="offline",
            activity_since=0,
            waiting_for_memory=None,
            start_failed={
                "error": "worktree add failed",
                "at": 5.0,
                "prompt_preview": "do x",
            },
        ),
    ]
    box, api, clock = _wait_box(rows)
    api.row("w1")["activity"] = "working"
    ctx = _ctx(clock)
    out = box.wait_for_session({"titles": ["f1", "w1"], "mode": "any"}, ctx)
    res = out["sessions"]["f1"]
    assert res["reason"] == "failed_to_start"
    assert res["error"] == "worktree add failed"
    assert res["prompt_preview"] == "do x"
    assert "spawn_session" in res["hint"] and "last_reply" not in res
    assert ctx.slept == []


def test_an_expired_failed_create_reads_failed_to_start_not_unknown():
    box, api, clock = _wait_box(_tree())
    api.create_failures["gone-w9"] = "out of disk"
    out = box.wait_for_session({"titles": ["gone-w9"]}, _ctx(clock))
    assert out["sessions"]["gone-w9"]["reason"] == "failed_to_start"
    assert out["sessions"]["gone-w9"]["error"] == "out of disk"
    # A title with no recorded failure is still an unknown title.
    with pytest.raises(Exception, match="no session named"):
        box.wait_for_session({"titles": ["never"]}, _ctx(clock))


def test_gone_carries_a_recorded_create_failure():
    box, api, clock = _wait_box(_tree())
    api.row("w1")["activity"] = "working"

    def vanish(now):
        api.rows[:] = [r for r in api.rows if r["title"] != "w1"]
        api.create_failures["w1"] = "tmux died"

    out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock, on_sleep=vanish))
    assert out["sessions"]["w1"]["reason"] == "gone"
    assert out["sessions"]["w1"]["error"] == "tmux died"


_QUEUED = {"kind": "spawn", "since": 1.0, "reason": "cap", "position": 2, "why": "cap"}


def _queued_row(title="q1", **kw):
    base = dict(
        parent="orch",
        spawned=True,
        pending=True,
        status="loading",
        activity="offline",
        activity_since=0,
        waiting_for_memory=dict(_QUEUED),
    )
    base.update(kw)
    return row(title, **base)


def _last_reply_gets(api, title):
    return [p for p in api.paths("GET") if title in p and "view=last_reply" in p]


@pytest.mark.parametrize(
    "reading",
    [
        {"status": "paused"},
        {"status": "ready", "activity": "offline"},
        {"status": "running", "activity": "idle", "activity_since": 1.0},
    ],
)
def test_a_queued_row_is_never_read_as_finished(reading):
    box, api, clock = _wait_box(_tree() + [_queued_row(**reading)])
    start = clock.now
    out = box.wait_for_session({"titles": ["q1"], "timeout_s": 120}, _ctx(clock))
    assert out["timed_out"] is True and out["sessions"] == {}
    assert out["still_running"] == ["q1"] and set(out["queued"]) == {"q1"}
    assert clock.now - start >= 120 > mcp_tools.OFFLINE_DONE_S


def test_the_offline_timer_starts_fresh_once_the_gate_lets_go():
    box, api, clock = _wait_box(_tree())
    w1 = api.row("w1")
    w1.update(activity="offline", status="ready")
    start = clock.now
    cleared = []

    def gate(now):
        if now - start < 9:
            return  # offline, not queued yet: the offline timer is running
        if now - start < 60:
            w1["waiting_for_memory"] = dict(_QUEUED, why="restart")
        elif not cleared:
            w1["waiting_for_memory"] = None
            cleared.append(now)

    out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock, on_sleep=gate))
    assert out["sessions"]["w1"]["reason"] == "offline"
    # Not at once on the stale timer: offline for 30 s after the queue.
    assert clock.now - cleared[0] >= mcp_tools.OFFLINE_DONE_S
    assert "queued" not in out


def test_a_failed_start_wins_over_a_gate_record():
    rows = _tree() + [_queued_row("f1", start_failed={"error": "boom", "at": 5.0})]
    box, api, clock = _wait_box(rows)
    ctx = _ctx(clock)
    out = box.wait_for_session({"titles": ["f1"]}, ctx)
    assert out["sessions"]["f1"]["reason"] == "failed_to_start"
    assert out["sessions"]["f1"]["error"] == "boom"
    assert out["still_running"] == [] and "queued" not in out
    assert ctx.slept == []


def test_failed_to_start_error_falls_back_to_the_recorded_create_failure():
    rows = _tree() + [_queued_row("f1", waiting_for_memory=None, start_failed={})]
    box, api, clock = _wait_box(rows)
    api.create_failures["f1"] = "worktree add failed"
    res = box.wait_for_session({"titles": ["f1"]}, _ctx(clock))["sessions"]["f1"]
    assert res["reason"] == "failed_to_start"
    assert res["error"] == "worktree add failed"
    assert "prompt_preview" not in res  # only when the record has one
    assert res["hint"] == mcp_tools._FAILED_TO_START_HINT
    assert _last_reply_gets(api, "f1") == []
    # Nothing recorded anywhere: still failed, with an empty error.
    del api.create_failures["f1"]
    res = box.wait_for_session({"titles": ["f1"]}, _ctx(clock))["sessions"]["f1"]
    assert res["reason"] == "failed_to_start" and res["error"] == ""


def test_mode_any_returns_a_failed_start_while_another_is_queued():
    rows = _tree() + [
        _queued_row("f1", waiting_for_memory=None, start_failed={"error": "boom"}),
        _queued_row("q1"),
    ]
    box, api, clock = _wait_box(rows)
    ctx = _ctx(clock)
    out = box.wait_for_session({"titles": ["q1", "f1"], "mode": "any"}, ctx)
    assert list(out["sessions"]) == ["f1"] and out["still_running"] == ["q1"]
    assert out["queued"] == {
        "q1": {
            "why": "cap",
            "position": 2,
            "reason": "cap",
            "status": "queued: waiting for a free agent slot",
        }
    }
    assert "hint" not in out and "timed_out" not in out
    assert ctx.slept == []


def test_mode_any_names_the_queued_ones_beside_a_finished_one():
    box, api, clock = _wait_box(_tree() + [_queued_row("q1")])
    out = box.wait_for_session({"titles": ["w1", "q1"], "mode": "any"}, _ctx(clock))
    assert out["sessions"]["w1"]["reason"] == "idle"
    assert out["still_running"] == ["q1"] and set(out["queued"]) == {"q1"}
    assert "hint" not in out


def test_a_message_return_names_the_queued_ones():
    box, api, clock = _wait_box(_tree() + [_queued_row("q1")])
    api.row("w1")["activity"] = "working"

    def ask(now):
        if not api.mail.get("orch"):
            api.deliver("orch", "w1", "question?", state="pending")

    out = box.wait_for_session({"titles": ["q1", "w1"]}, _ctx(clock, on_sleep=ask))
    assert out["reason"] == "message"
    assert out["still_running"] == ["q1", "w1"]
    assert set(out["queued"]) == {"q1"}
    assert out["queued"]["q1"]["status"] == "queued: waiting for a free agent slot"
    assert "hint" not in out


def test_a_message_return_has_no_queued_map_when_nothing_is_queued():
    box, api, clock = _wait_box(_tree())
    api.row("w1")["activity"] = "working"

    def ask(now):
        if not api.mail.get("orch"):
            api.deliver("orch", "w1", "question?", state="pending")

    out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock, on_sleep=ask))
    assert out["reason"] == "message" and "queued" not in out


def test_no_queued_map_once_nothing_is_still_running():
    box, api, clock = _wait_box(_tree())
    out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock))
    assert out["still_running"] == [] and "queued" not in out


def test_an_expired_failed_create_beside_an_unknown_title_names_only_the_unknown():
    box, api, clock = _wait_box(_tree())
    api.create_failures["gone-w9"] = "out of disk"
    with pytest.raises(ToolError) as err:
        box.wait_for_session({"titles": ["gone-w9", "never", "w1"]}, _ctx(clock))
    assert "no session named 'never'" in err.value.message
    assert "gone-w9" not in err.value.message


def test_only_expired_failed_creates_return_at_once():
    box, api, clock = _wait_box(_tree())
    api.create_failures.update({"gone-w8": "tmux died", "gone-w9": "out of disk"})
    ctx = _ctx(clock)
    out = box.wait_for_session({"titles": ["gone-w8", "gone-w9"]}, ctx)
    assert out["still_running"] == [] and "queued" not in out
    assert out["sessions"]["gone-w8"] == {
        "reason": "failed_to_start",
        "report": None,
        "error": "tmux died",
        "hint": mcp_tools._FAILED_TO_START_HINT,
    }
    assert out["sessions"]["gone-w9"]["error"] == "out of disk"
    assert ctx.slept == []
    # No mailbox or output read for a session that is not there: one
    # create_failures lookup each, nothing else under their titles.
    theirs = [p for p in api.paths() if "gone-w" in p]
    assert theirs == [
        "/api/create_failures?title=gone-w8",
        "/api/create_failures?title=gone-w9",
    ]


def test_listed_titles_cost_no_create_failures_lookup():
    box, api, clock = _wait_box(_tree())
    box.wait_for_session({"titles": ["w1", "w2"]}, _ctx(clock))
    assert not [p for p in api.paths() if p.startswith("/api/create_failures")]


def test_gone_has_no_error_without_a_recorded_create_failure():
    box, api, clock = _wait_box(_tree())
    api.row("w1")["activity"] = "working"

    def vanish(now):
        api.rows[:] = [r for r in api.rows if r["title"] != "w1"]

    out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock, on_sleep=vanish))
    res = out["sessions"]["w1"]
    assert res["reason"] == "gone" and "error" not in res
    assert "last_reply" not in res and _last_reply_gets(api, "w1") == []
    assert api.paths().count("/api/create_failures?title=w1") == 1


def test_gone_survives_a_server_without_create_failures():
    box, api, clock = _wait_box(_tree())
    api.row("w1")["activity"] = "working"
    api.errors[("GET", "/api/create_failures")] = client.ApiError(404, "Not Found")

    def vanish(now):
        api.rows[:] = [r for r in api.rows if r["title"] != "w1"]

    out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock, on_sleep=vanish))
    assert out["sessions"]["w1"]["reason"] == "gone"
    assert "error" not in out["sessions"]["w1"]
    # ...and an unknown title there is still an unknown title.
    with pytest.raises(ToolError, match="no session named 'never'"):
        box.wait_for_session({"titles": ["never"]}, _ctx(clock))


# --------------------------------------------------------------------------- #
# the helpers
# --------------------------------------------------------------------------- #
def test_gate_record_is_defensive():
    assert waiting.gate_record(None) is None
    assert waiting.gate_record({"waiting_for_memory": None}) is None
    assert waiting.gate_record({"waiting_for_memory": "junk"}) is None
    # A ticket body's "reason" is its report-back note, never the gate's.
    assert (
        waiting.gate_record(
            {"queued": True, "memory_reason": "", "reason": "no footer"}
        )
        == {}
    )
    assert waiting.queued_status(None) == waiting.QUEUED_DEFAULT
    assert waiting.start_failure({"start_failed": "boom"}) == {"error": "boom"}
    assert waiting.start_failure({"start_failed": None}) is None


def test_gate_record_reads_an_older_servers_top_level_keys():
    body = {"queued": True, "why": "cap", "position": 0, "reason": "cap reached"}
    assert waiting.gate_record(body) == {
        "why": "cap",
        "position": 0,  # kept: 0 is a position, not a missing one
        "reason": "cap reached",
    }
    assert waiting.gate_record({"queued": True}) == {}
    assert waiting.gate_record({"queued": True, "waiting_for_memory": {}}) == {}


def test_gate_record_prefers_the_gates_own_record():
    body = {
        "queued": True,
        "why": "memory",
        "position": 4,
        "reason": "top",
        "waiting_for_memory": {"why": "queue", "position": 0, "reason": "rec"},
    }
    assert waiting.gate_record(body) == {"why": "queue", "position": 0, "reason": "rec"}


def test_gate_record_never_takes_a_ticket_bodys_report_back_reason():
    both = {"queued": True, "memory_reason": "low", "reason": "no footer"}
    assert waiting.gate_record(both) == {"reason": "low"}
    blank = {"queued": True, "memory_reason": "", "reason": "no footer"}
    assert "reason" not in waiting.gate_record(blank)
    # A spawn body has no memory_reason key: its reason is the gate's.
    assert waiting.gate_record({"queued": True, "reason": "low"}) == {"reason": "low"}


def test_gate_record_of_a_row_needs_no_queued_key():
    r = {"title": "q1", "waiting_for_memory": {"why": "spacing", "since": 1.0}}
    assert waiting.gate_record(r) == {"why": "spacing"}
    # ...and a row's own "reason" is not the gate's.
    r = {"reason": "row note", "waiting_for_memory": {"why": "cap"}}
    assert waiting.gate_record(r) == {"why": "cap"}


@pytest.mark.parametrize("src", [None, "queued", 3, ["queued"], {}, {"queued": False}])
def test_gate_record_of_something_not_waiting_is_none(src):
    assert waiting.gate_record(src) is None


@pytest.mark.parametrize("rec", [None, "junk", 7, [], {}])
def test_gate_record_ignores_a_record_that_is_not_a_filled_dict(rec):
    assert waiting.gate_record({"waiting_for_memory": rec}) is None
    assert waiting.gate_record({"waiting_for_memory": rec, "queued": True}) == {}


@pytest.mark.parametrize(
    "rec,status",
    [
        ({"why": "memory"}, "queued: waiting for memory"),
        ({"why": "warming"}, "queued: waiting for memory"),
        (
            {"why": "spacing"},
            "queued: starting shortly (automatic starts are staggered)",
        ),
        ({"why": "cap"}, "queued: waiting for a free agent slot"),
        ({"why": "restart"}, "queued: waiting to restart"),
        ({"why": "restart", "position": 5}, "queued: waiting to restart"),
        ({"why": "queue", "position": 1}, "queued: next in line"),
        ({"why": "queue", "position": 2}, "queued: 1 ahead of it"),
        ({"why": "queue", "position": 5}, "queued: 4 ahead of it"),
        ({"why": "queue", "position": 0}, "queued: next in line"),
        ({"why": "queue", "position": None}, "queued: next in line"),
        ({"why": "queue"}, "queued: next in line"),
        ({"why": "queue", "position": "3"}, "queued: next in line"),  # not an int
        ({"why": "something-new"}, waiting.QUEUED_DEFAULT),
        ({"why": ""}, waiting.QUEUED_DEFAULT),
        ({"why": None}, waiting.QUEUED_DEFAULT),
        ({}, waiting.QUEUED_DEFAULT),
        (None, waiting.QUEUED_DEFAULT),
    ],
)
def test_queued_status(rec, status):
    assert waiting.queued_status(rec) == status


def test_the_queued_hint_names_every_why_the_status_table_words():
    # restart is a session's own restart, never a queued create's why.
    for why in set(waiting.QUEUED_STATUS) - {"restart"}:
        assert why in waiting.QUEUED_HINT, why
    assert "queue (behind" in waiting.QUEUED_HINT


@pytest.mark.parametrize("body", [None, {}, "junk", {"queued": True}])
def test_queued_fields_of_an_empty_body(body):
    assert waiting.queued_fields(body) == {
        "status": waiting.QUEUED_DEFAULT,
        "queued": True,
        "why": None,
        "position": None,
        "reason": "",
    }


def test_queued_fields_has_exactly_the_stable_keys():
    body = {
        "title": "t",
        "queued": True,
        "waiting_for_memory": {
            "kind": "spawn",
            "since": 1.0,
            "creates": True,
            "why": "queue",
            "position": 3,
            "reason": "behind 2",
        },
    }
    assert waiting.queued_fields(body) == {
        "status": "queued: 2 ahead of it",
        "queued": True,
        "why": "queue",
        "position": 3,
        "reason": "behind 2",
    }


def test_start_failure():
    rec = {"error": "boom", "at": 5.0, "prompt_preview": "do x"}
    assert waiting.start_failure({"start_failed": rec}) is rec
    assert waiting.start_failure({"start_failed": "boom"}) == {"error": "boom"}
    assert waiting.start_failure({"start_failed": True}) == {"error": "True"}
    # A record without an error still says the start failed.
    assert waiting.start_failure({"start_failed": {}}) == {}
    for missing in ({}, {"start_failed": None}, {"start_failed": ""}):
        assert waiting.start_failure(missing) is None
    for not_a_row in (None, "start_failed", ["start_failed"]):
        assert waiting.start_failure(not_a_row) is None


# --------------------------------------------------------------------------- #
# the texts an agent reads
# --------------------------------------------------------------------------- #
def test_tool_descriptions_name_the_new_keys():
    assert "failed_to_start" in mcp_tools._D_WAIT
    assert "queued {title: {why, position}}" in mcp_tools._D_WAIT
    assert "waiting {why, position, reason}" in mcp_tools._D_LIST
    assert "start_failed" in mcp_tools._D_LIST
    assert "queued: true, why" in mcp_tools._D_SPAWN
    assert '"queued: waiting for memory"' not in mcp_tools._D_SPAWN


def test_the_doc_names_the_new_keys():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "mcp.md").read_text()
    for key in (
        "why",
        "waiting",
        "start_failed",
        "failed_to_start",
        "reason_no_report",
    ):
        assert "`%s" % key in doc or "%s`" % key in doc, key
