"""A create that waits in the launch gate (core.session_create.create_gated)
is checked before it waits, named and queued in one step, and never lost:
it reaches create() with its task after a drain or a restart, and a failure
leaves a visible row, an event and a note to its parent.

Nothing here touches tmux or the live engine: the registry is a fresh dict,
the create's side effects are stubbed (test_webui._stub_create), and the one
git repo is made under tmp_path.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from backend.web import server
from backend.web.core import launch_gate as gate
from backend.web.core import lineage
from backend.web.core import pending
from backend.web.core import session_create as sc
from backend.web.core import ticket_start
from backend.web.core import worker_order_driver as wod
from tests.unit.test_webui import _stub_create

#: A folder that exists: the preflight refuses a repo_path with no folder.
_FOLDER = __import__("tempfile").mkdtemp(prefix="mf-create-queue-")


def _parent():
    return SimpleNamespace(
        Title="orch", Parent="", Spawned=False, PeerShare="", GetWorktreePath=lambda: ""
    )


@pytest.fixture
def env(monkeypatch):
    """A fresh registry holding the orchestrator ``orch``, a fresh pending
    registry, a gate that queues every automatic start, and every channel a
    failure is reported on captured."""
    instances = {"orch": _parent()}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(pending, "_PENDING", {})
    monkeypatch.setattr(sc, "_CLAIMING", {})
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    monkeypatch.setattr(server, "_session_branches", lambda: {})
    monkeypatch.setattr(server, "_budget_locked", lambda p: False)
    monkeypatch.setattr(server._thread, "note_seed", lambda *a, **k: None)
    monkeypatch.setattr(server._prompt_queue, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(server._prompt_queue, "set_flags", lambda *a, **k: None)
    monkeypatch.setattr(wod._order, "load", lambda: {"parents": {}})
    # Every automatic start must wait: the gate's own verdict (what admit(),
    # gate_auto() and drain_once() all ask) says memory is low.
    monkeypatch.setattr(gate, "decide", lambda *a, **k: dict(_WAIT))
    out = SimpleNamespace(instances=instances, events=[], told=[], notes=[])
    monkeypatch.setattr(
        server._events.BUS,
        "emit",
        lambda event, **kw: out.events.append(
            (event, kw.get("session"), kw.get("data"))
        ),
    )
    monkeypatch.setattr(
        server, "_note_create_failure", lambda t, e: out.notes.append(t)
    )
    monkeypatch.setattr(
        sc, "_tell_parent", lambda p, t, e: out.told.append((p, t, e)) or True
    )
    return out


#: The gate's verdicts (launch_gate.decide).
_WAIT = {"decision": "wait", "reason": "memory is low", "why": "memory"}
_GO = {"decision": "go", "reason": "", "why": ""}


def _spawn(**kw):
    body = {"program": "bash", "repo_path": _FOLDER, "parent": "orch", "spawned": True}
    body.update(kw)
    return body


def _gated(payload, kind="spawn"):
    return asyncio.run(sc.create_result(payload, auto=kind))


def _drain(monkeypatch):
    monkeypatch.setattr(gate, "decide", lambda *a, **k: dict(_GO))
    return gate.drain_once()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(git + ["add", "a.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "a"], check=True)
    sha = subprocess.run(
        git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return str(repo), sha


# --------------------------------------------------------------------------- #
# B. a detached HEAD is no branch
# --------------------------------------------------------------------------- #
def test_head_as_base_branch_reads_as_none():
    assert lineage.normalize_base_branch("abc123", "HEAD") == ""
    assert lineage.normalize_base_branch("abc123", "head") == "head"  # case-sensitive
    assert lineage.normalize_base_branch("abc123", "main") == "main"
    # Without a base_ref it stays, to be refused like any lone base_branch.
    assert lineage.normalize_base_branch("", "HEAD") == "HEAD"


def test_base_ref_error_accepts_a_detached_head(tmp_path):
    repo, sha = _repo(tmp_path)
    assert lineage.base_ref_error(repo, sha, "HEAD") is None
    assert lineage.base_ref_error(repo, "", "HEAD") == "base_branch requires base_ref"


# --------------------------------------------------------------------------- #
# A. checked BEFORE it waits
# --------------------------------------------------------------------------- #
def test_a_bad_base_ref_is_refused_inline_and_nothing_waits(env, tmp_path):
    repo, _sha = _repo(tmp_path)
    status, body = _gated(_spawn(prompt="do it", repo_path=repo, base_ref="nope"))
    assert status == 400 and "unknown base_ref: nope" in body["error"]
    assert gate.items() == [] and pending.snapshot() == {}


def test_a_detached_head_worker_waits_and_forks_with_no_base_branch(
    env, tmp_path, monkeypatch
):
    repo, sha = _repo(tmp_path)
    captured: list = []
    _stub_create(monkeypatch, captured, git_enabled=True)
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i, **k: (repo, True))
    payload = _spawn(title="w1", prompt="Fix the parser", repo_path=repo, base_ref=sha)
    payload["base_branch"] = "HEAD"
    status, body = _gated(payload)
    assert status == 202 and body["queued"] is True and body["title"] == "w1"
    assert _drain(monkeypatch) == "w1"
    (opts,) = captured
    assert opts.base_ref == sha and opts.base_branch == ""
    assert "Fix the parser" in opts.prompt
    assert "w1" in env.instances and pending.snapshot() == {}


def test_unknown_parent_and_bad_fence_are_refused_inline(env):
    status, body = _gated(_spawn(prompt="x", parent="ghost"))
    assert (status, body["error"]) == (400, "unknown parent session: ghost")
    status, body = _gated(_spawn(prompt="x", after=["x"], parent=""))
    assert status == 400 and "after needs a parent" in body["error"]
    assert gate.items() == []


def test_waiting_creates_count_against_the_child_limit(env, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "2")
    assert _gated(_spawn(title="w1", prompt="a"))[0] == 202
    assert _gated(_spawn(title="w2", prompt="b"))[0] == 202
    status, body = _gated(_spawn(title="w3", prompt="c"))
    assert status == 409
    assert "2 children (0 live, 2 waiting to start)" in body["error"]
    assert [i["title"] for i in gate.items()] == ["w1", "w2"]


def test_spawn_limit_error_counts_waiting():
    inst = {"p": SimpleNamespace(Parent="", Spawned=False)}
    waiting = [("a", "p", True), ("b", "other", True), ("p", "", False)]
    assert lineage.spawn_limit_error(inst, "p", True, waiting) is None
    import os

    os.environ["MINDFLOCK_MAX_CHILDREN"] = "1"
    try:
        err = lineage.spawn_limit_error(inst, "p", True, waiting)
    finally:
        del os.environ["MINDFLOCK_MAX_CHILDREN"]
    assert err and "1 waiting to start" in err


# --------------------------------------------------------------------------- #
# C. naming and queueing are one step
# --------------------------------------------------------------------------- #
def test_two_untitled_creates_with_one_task_get_two_names(env):
    s1, b1 = _gated(_spawn(prompt="Fix the login bug\nmore"))
    s2, b2 = _gated(_spawn(prompt="Fix the login bug\nother"))
    assert (s1, s2) == (202, 202)
    assert (b1["title"], b2["title"]) == ("fix-login-bug", "fix-login-bug-2")
    assert [i["title"] for i in gate.items()] == ["fix-login-bug", "fix-login-bug-2"]


def test_free_title_skips_names_waiting_to_be_created(env):
    pending.add("job", "spawn")
    gate.enqueue("job-2", lambda: None, kind="spawn", creates=True)
    assert sc.free_title("job") == "job-3"
    pending.fail("job-3", "boom")  # a failed row's retry is welcome
    assert sc.free_title("job") == "job-3"


def test_a_same_title_create_while_one_waits_is_409(env):
    assert _gated(_spawn(title="w1", prompt="first"))[0] == 202
    status, body = _gated(_spawn(title="w1", prompt="second"))
    assert status == 409 and "already exists" in body["error"]
    (it,) = gate.items()
    queued = gate._QUEUE[0]["args"]["payload"]
    assert it["title"] == "w1" and queued["prompt"] == "first"


def test_a_create_already_starting_still_holds_its_name_and_slot(env, monkeypatch):
    """The gate moves a drained launch to its in-flight list (out of
    items()/waiting()) while it runs: its placeholder row still holds the
    name and the parent's child slot until the session exists."""
    monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "1")
    assert _gated(_spawn(title="w1", prompt="a"))[0] == 202
    seen = []

    def _launch():
        assert gate.items() == [] and gate.waiting("w1") is None  # in flight
        monkeypatch.setattr(gate, "decide", lambda *a, **k: dict(_WAIT))
        seen.append(_gated(_spawn(title="w1", prompt="again")))
        seen.append(_gated(_spawn(title="w2", prompt="other")))
        return None

    gate._QUEUE[0]["launch"] = _launch
    assert _drain(monkeypatch) == "w1"
    (s1, b1), (s2, b2) = seen
    assert s1 == 409 and "already exists" in b1["error"]
    assert s2 == 409 and "1 waiting to start" in b2["error"]


def test_concurrent_same_title_creates_queue_once(env):
    async def both():
        return await asyncio.gather(
            sc.create_result(_spawn(title="w1", prompt="a"), auto="spawn"),
            sc.create_result(_spawn(title="w1", prompt="b"), auto="spawn"),
        )

    got = sorted(s for s, _b in asyncio.run(both()))
    assert got == [202, 409]
    assert len(gate.items()) == 1


def test_a_persons_create_of_a_waiting_name_is_409(env, monkeypatch):
    _stub_create(monkeypatch, git_enabled=True)
    assert _gated(_spawn(title="w1", prompt="a"))[0] == 202
    status, body = asyncio.run(
        sc.create_result({"title": "w1", "program": "bash", "repo_path": _FOLDER})
    )
    assert status == 409 and "already exists" in body["error"]


# --------------------------------------------------------------------------- #
# D. after= a sibling still waiting to be created
# --------------------------------------------------------------------------- #
def test_after_may_name_a_sibling_waiting_to_be_created(env):
    assert _gated(_spawn(title="a", prompt="first"))[0] == 202
    status, body = _gated(_spawn(title="b", prompt="second", after=["a"]))
    assert status == 202, body
    assert wod.check_create("orch", "b", {"after": ["a"]}) is None
    # Someone else's waiting create is no sibling.
    assert "no session of yours: zz" in wod.check_create("orch", "b", {"after": ["zz"]})


def test_after_a_failed_sibling_fails_visibly_naming_it(env, monkeypatch):
    _stub_create(monkeypatch, git_enabled=True)
    pending.fail("a", "invalid base_branch: x", parent="orch", kind="spawn")
    status, body = _gated(_spawn(title="b", prompt="second", after=["a"]))
    assert status == 400
    assert body["error"] == "after names a, whose create failed: invalid base_branch: x"
    # …and at drain time (it was queued while a still waited): the task is
    # not lost, it fails visibly with the same words.
    pending.drop("a")
    pending.add("a", "spawn", parent="orch")
    assert _gated(_spawn(title="b", prompt="second task", after=["a"]))[0] == 202
    pending.fail("a", "boom", parent="orch")
    gate.cancel("a")
    assert _drain(monkeypatch) == "b"
    rec = pending.failed("b")
    assert rec["error"] == "after names a, whose create failed: boom"
    assert rec["prompt_preview"].startswith("second task")
    assert ("orch", "b", rec["error"]) in env.told


# --------------------------------------------------------------------------- #
# E. a failure is visible
# --------------------------------------------------------------------------- #
def test_a_refused_drain_keeps_a_failed_row_and_says_so(env, monkeypatch):
    _stub_create(monkeypatch, git_enabled=True)
    assert _gated(_spawn(title="w1", prompt="Write the docs " * 30))[0] == 202
    env.instances["w1"] = object()  # the name was taken while it waited
    assert _drain(monkeypatch) == "w1"
    rec = pending.failed("w1")
    assert rec["error"] == "instance w1 already exists" and rec["kind"] == "spawn"
    assert rec["prompt_preview"].startswith("Write the docs")
    assert len(rec["prompt_preview"]) == pending.PROMPT_PREVIEW_CHARS
    (ev,) = [e for e in env.events if e[0] == "session.create_failed"]
    assert ev[2] == {
        "title": "w1",
        "parent": "orch",
        "error": "instance w1 already exists",
        "kind": "spawn",
    }
    assert env.told == [("orch", "w1", "instance w1 already exists")]
    assert env.notes == ["w1"]
    assert gate.last_outcome("w1")["error"] == "instance w1 already exists"


def test_a_create_that_raises_keeps_a_failed_row(env, monkeypatch):
    assert _gated(_spawn(title="w1", prompt="task"))[0] == 202

    async def boom(payload, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sc, "create", boom)
    assert _drain(monkeypatch) == "w1"
    assert pending.failed("w1")["error"] == "disk full"
    assert env.told == [("orch", "w1", "disk full")]


def test_failed_rows_render_expire_and_dismiss(env, monkeypatch):
    pending.add("w1", "spawn", parent="orch", spawned=True)
    assert pending.has("w1")
    pending.fail("w1", "nope", prompt="the task")
    assert not pending.has("w1") and "w1" not in pending.snapshot()
    eng = SimpleNamespace(instances={}, default_program=lambda: "claude")
    (row,) = pending.rows(eng)
    assert row["start_failed"]["error"] == "nope" and row["parent"] == "orch"
    pending.add("w2", "spawn")
    assert {r["title"]: r["start_failed"] for r in pending.rows(eng)}["w2"] is None
    assert not pending.dismiss("w2")  # in flight: not dismiss's to forget
    assert pending.dismiss("w1") and pending.failed("w1") is None
    pending.fail("old", "x", now=1.0)
    assert pending.failed("old") is None  # past its ten minutes
    pending.fail("w3", "x")
    pending.add("w3", "spawn")  # a retry starts afresh
    assert pending.failed("w3") is None and pending.has("w3")


def test_a_note_names_workers_held_after_the_failed_one(env, monkeypatch):
    monkeypatch.setattr(
        wod._order,
        "load",
        lambda: {
            "parents": {
                "orch": {
                    "workers": {
                        "b": {"state": "held", "after": ["a"]},
                        "c": {"state": "running", "after": ["a"]},
                    }
                }
            }
        },
    )
    assert wod.held_after("orch", "a") == ["b"]


# --------------------------------------------------------------------------- #
# G. a restore keeps the task, or records why not
# --------------------------------------------------------------------------- #
def test_a_restored_create_reaches_create_with_its_prompt(env, monkeypatch):
    captured: list = []
    _stub_create(monkeypatch, captured, git_enabled=True)
    payload = _spawn(title="w1", prompt="Port the parser to rust")
    launch = sc._restore_create(
        {
            "title": "w1",
            "kind": "flight",
            "parent": "orch",
            "args": {"payload": payload},
        }
    )
    assert launch is not None and pending.has("w1")
    assert launch() is None
    (opts,) = captured
    assert "Port the parser to rust" in opts.prompt and opts.parent == "orch"
    assert "w1" in env.instances and not pending.has("w1")


def test_a_restore_whose_name_was_taken_is_a_failed_row(env):
    env.instances["w1"] = object()
    payload = _spawn(title="w1", prompt="the lost task")
    rec = {
        "title": "w1",
        "kind": "spawn",
        "parent": "orch",
        "args": {"payload": payload},
    }
    assert sc._restore_create(rec) is None
    failed = pending.failed("w1")
    assert "took the name w1" in failed["error"]
    assert failed["prompt_preview"] == "the lost task"
    assert env.told and env.told[0][:2] == ("orch", "w1")
    assert [e[0] for e in env.events] == ["session.create_failed"]


# --------------------------------------------------------------------------- #
# F. a waiting ticket start survives a restart
# --------------------------------------------------------------------------- #
def test_a_waiting_ticket_start_is_restored_through_launch(env, monkeypatch):
    calls = []

    async def fake_launch(source, ticket_id, **kw):
        # launch() makes its own row again: the restore's must be gone.
        calls.append((source, ticket_id, kw, pending.has(kw["title"])))
        return {"started": True}

    monkeypatch.setattr(ticket_start, "launch", fake_launch)
    lineage_ = {"parent": "orch", "spawned": True, "report_back": True, "note": ""}
    rec = {
        "title": "sc-7",
        "kind": "ticket",
        "parent": "orch",
        "args": {
            "launch": {
                "source": "sc",
                "ticket_id": "7",
                "title": "sc-7",
                "branch": "feature/sc-7/x",
                "lineage": lineage_,
                "extra_prompt": "brief",
                "plan": "builtin:ticket",
            }
        },
    }
    assert gate._FACTORIES["ticket"] is ticket_start._restore_ticket
    launch = ticket_start._restore_ticket(rec)
    assert launch is not None and pending.has("sc-7")
    assert launch() is None
    ((source, tid, kw, had_row),) = calls
    assert (source, tid, had_row) == ("sc", "7", False)
    assert kw["admitted"] is True and kw["lineage"]["parent"] == "orch"
    assert kw["branch"] == "feature/sc-7/x" and kw["extra_prompt"] == "brief"
    # Its name taken meanwhile: a failed row, not a log line.
    env.instances["sc-7"] = object()
    assert ticket_start._restore_ticket(rec) is None
    assert "took the name sc-7" in pending.failed("sc-7")["error"]


def test_a_restored_ticket_start_that_is_refused_fails_visibly(env, monkeypatch):
    async def refused(source, ticket_id, **kw):
        raise ticket_start.LaunchError(404, {"error": "no such ticket"})

    monkeypatch.setattr(ticket_start, "launch", refused)
    rec = {
        "title": "sc-8",
        "kind": "ticket",
        "args": {"launch": {"source": "sc", "ticket_id": "8", "lineage": {}}},
    }
    launch = ticket_start._restore_ticket(rec)
    assert launch() == "no such ticket"
    assert pending.failed("sc-8")["prompt_preview"] == "ticket sc 8"


def test_a_waiting_ticket_start_saves_what_rebuilds_it(env, monkeypatch):
    seen = {}

    def fake_gate_auto(title, launch, **kw):
        seen.update(kw)
        return {"title": title, "reason": "memory", "position": 1}

    monkeypatch.setattr(gate, "gate_auto", fake_gate_auto)
    ticket_start._gate_turn("sc-9", {"parent": "orch"}, {"launch": {"source": "sc"}})
    assert seen["args"] == {"launch": {"source": "sc"}} and seen["kind"] == "ticket"


# --------------------------------------------------------------------------- #
# F. a create the gate lets through counts once
# --------------------------------------------------------------------------- #
def test_an_untitled_spawn_let_through_counts_as_one_warming_agent(env, monkeypatch):
    """A blank-titled spawn is admitted under a placeholder key (no name
    yet); create() then notes the start under the name it picks. The
    placeholder used to stay too, so for WARMUP_SECONDS the one agent read
    as two warming (twice its growth "to come", one more live against the
    cap), holding back the automatic starts behind it."""
    _stub_create(monkeypatch, git_enabled=True)
    monkeypatch.setattr(gate, "decide", lambda *a, **k: dict(_GO))
    status, body = _gated(_spawn(prompt="Fix the login bug"))
    assert status == 202 and body["title"] == "fix-login-bug"
    assert set(gate._ADMITTED) == {gate._tmux_name("fix-login-bug")}
    assert gate.assess()["warming"] == 1
    # The stagger still runs from this start.
    assert gate._LAST_AUTO["at"] > 0
