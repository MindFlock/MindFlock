"""Queued starts whose server stopped and never came back
(core.launch_gate.adopt_orphans).

Two servers share one ``launch_queue.json`` (the service on 8765 and a dev
server on 9000 that is restarted and replaced many times a day). A start
queued on a server that then stops is adopted by the other one on its next
pass, not only at that server's next boot: it starts, or fails with a reason,
there. These tests run a second gate as a real second process over the same
tmp queue file, so "owner gone" and "owner alive" are real processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend.web.core import launch_gate as gate

_ROOT = Path(__file__).resolve().parents[2]

#: A second gate in its own process. ``own``: queue the given records, then
#: wait (alive, owning them) until a line arrives on stdin. ``adopt``: wait
#: for the ``go`` file, adopt what it can, print the titles it queued, then
#: wait like ``own``.
_PEER = r"""
import json, os, sys, time
from backend.web.core import launch_gate as gate
gate.AUTO_DRAIN = False
gate.PERSIST = True
mode, arg = sys.argv[1], json.loads(sys.argv[2])
if mode == "own":
    for rec in arg:
        gate.enqueue(
            rec["title"], lambda: None, kind=rec.get("kind", "k"),
            creates=rec.get("creates", False), args=rec.get("args", {}),
        )
    print("ready", flush=True)
else:
    gate.register_kind("k", lambda r: (lambda: None))
    print("ready", flush=True)
    while not os.path.exists(arg["go"]):
        time.sleep(0.002)
    gate.adopt_orphans()
    print(json.dumps([i["title"] for i in gate.items()]), flush=True)
sys.stdin.readline()
"""


class _Peer:
    def __init__(self, mode: str, arg) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _PEER, mode, json.dumps(arg)],
            cwd=str(_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout.readline().strip() == "ready"
        self.pid = self.proc.pid

    def line(self) -> str:
        return self.proc.stdout.readline()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()
            self.proc.wait(30)


@pytest.fixture
def path(monkeypatch):
    p = Path(os.environ["MINDFLOCK_LAUNCH_QUEUE_FILE"])
    p.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gate, "PERSIST", True)
    return p


@pytest.fixture
def peers():
    started = []

    def start(mode, arg):
        p = _Peer(mode, arg)
        started.append(p)
        return p

    yield start
    for p in started:
        p.stop()


def _disk(path):
    return json.loads(path.read_text())["items"]


def _owners(path):
    return {r["title"]: r["owner"]["pid"] for r in _disk(path)}


def _recs(*titles, **kw):
    return [dict({"title": t}, **kw) for t in titles]


def test_a_stopped_servers_queued_start_is_taken_over_on_the_next_pass(
    path, peers, monkeypatch
):
    """The owner exits and never comes back: the running server adopts its
    starts on a pass (no boot), in their order, and they start here."""
    ran = []
    monkeypatch.setitem(
        gate._FACTORIES, "k", lambda rec: (lambda: ran.append(rec["title"]))
    )
    a = peers("own", _recs("w1", "w2"))
    assert set(_owners(path)) == {"w1", "w2"}
    a.stop()  # the server stops; nothing restarts it
    assert gate.adopt_orphans() == 2
    w1, w2 = gate.items()
    assert (w1["title"], w2["title"]) == ("w1", "w2")
    assert w1["why"] == "restart"
    assert w1["reason"].startswith("taken over by this server")
    assert _owners(path) == {"w1": os.getpid(), "w2": os.getpid()}
    # They are automatic starts: the gate starts them like its own.
    assert gate.drain_once() == "w1" and gate.drain_once() == "w2"
    assert ran == ["w1", "w2"] and gate.last_outcome("w1")["started"] is True
    assert _disk(path) == []
    # Nothing is adopted twice.
    assert gate.adopt_orphans() == 0


def test_a_live_owners_queued_start_is_never_touched(path, peers, monkeypatch):
    monkeypatch.setitem(gate._FACTORIES, "k", lambda rec: (lambda: None))
    a = peers("own", _recs("x"))
    before = path.read_text()
    assert gate.adopt_orphans() == 0 and gate.restore() == 0
    assert gate.items() == [] and _owners(path) == {"x": a.pid}
    assert json.loads(path.read_text())["items"] == json.loads(before)["items"]
    # Once it is gone, it is the other server's to take.
    a.stop()
    assert gate.adopt_orphans() == 1 and _owners(path) == {"x": os.getpid()}


@pytest.mark.skipif(
    gate._proc_start(os.getpid()) is None, reason="no process start times here"
)
def test_a_pid_now_held_by_another_process_is_not_the_owner(path, peers, monkeypatch):
    """Pids are reused: an entry naming a live pid whose start time is not
    the one it was written with belongs to nobody, and is adopted."""
    monkeypatch.setitem(gate._FACTORIES, "k", lambda rec: (lambda: None))
    a = peers("own", _recs("theirs"))
    real = gate._proc_start(a.pid)
    doc = json.loads(path.read_text())
    (rec,) = doc["items"]
    assert rec["owner"]["start"] == real
    reused = dict(rec, title="reused", owner=dict(rec["owner"], start=real + 1))
    doc["items"].append(reused)
    path.write_text(json.dumps(doc))
    assert gate.adopt_orphans() == 1
    assert [i["title"] for i in gate.items()] == ["reused"]
    assert _owners(path) == {"reused": os.getpid(), "theirs": a.pid}


def test_two_live_servers_never_adopt_the_same_orphan(
    path, peers, monkeypatch, tmp_path
):
    """The claim is one read-modify-write under the queue's file lock: each
    orphan is taken by exactly one of two servers looking at once."""
    monkeypatch.setitem(gate._FACTORIES, "k", lambda rec: (lambda: None))
    titles = ["o%02d" % i for i in range(20)]
    a = peers("own", _recs(*titles))
    a.stop()
    go = tmp_path / "go"
    b = peers("adopt", {"go": str(go)})
    mine: list = []
    th = threading.Thread(target=lambda: mine.append(gate.adopt_orphans()))
    go.write_text("")
    th.start()
    theirs = json.loads(b.line())
    th.join(30)
    ours = [i["title"] for i in gate.items()]
    assert sorted(ours + theirs) == titles  # every one, once
    assert not set(ours) & set(theirs)
    owners = _owners(path)
    assert len(_disk(path)) == len(titles)
    assert {t for t, p in owners.items() if p == os.getpid()} == set(ours)
    assert {t for t, p in owners.items() if p == b.pid} == set(theirs)


def test_an_orphan_this_server_cannot_carry_out_fails_with_a_reason(
    path, peers, monkeypatch
):
    """A start only the stopped server knew how to make (no launch for its
    kind here, or its rebuild raises) neither waits forever nor vanishes:
    it ends as a failed start with a sentence a person can read."""
    failed = []
    monkeypatch.setattr(gate, "on_orphan_failed", lambda r, s: failed.append((r, s)))

    def boom(rec):
        raise RuntimeError("its saved request is unreadable")

    monkeypatch.setitem(gate._FACTORIES, "breaks", boom)
    payload = {"prompt": "write the docs", "parent": "orch", "spawned": True}
    a = peers(
        "own",
        _recs("lost", kind="mystery", creates=True, args={"payload": payload})
        + _recs("broken", kind="breaks", args={}),
    )
    a.stop()
    assert gate.adopt_orphans() == 0
    assert gate.items() == [] and _disk(path) == []
    lost = gate.last_outcome("lost")
    assert lost["started"] is False
    assert lost["error"] == (
        "it waited for memory on a server that stopped, and this server "
        "cannot start a mystery launch"
    )
    assert gate.last_outcome("broken")["error"].endswith(
        "could not be taken over: its saved request is unreadable"
    )
    assert [(r["title"], s) for r, s in failed] == [
        ("lost", lost["error"]),
        ("broken", gate.last_outcome("broken")["error"]),
    ]
    assert failed[0][0]["args"] == {"payload": payload}


def test_a_create_that_cannot_be_taken_over_keeps_a_failed_row(monkeypatch):
    """session_create's side of the hook: a create's row stays as a failed
    start (with its task to retry) and its parent is told; a relaunch of a
    session that exists needs no row."""
    from backend.web import server
    from backend.web.core import pending
    from backend.web.core import session_create as sc

    assert gate.on_orphan_failed is sc._orphan_failed
    monkeypatch.setattr(pending, "_PENDING", {})
    told = []
    monkeypatch.setattr(sc, "_tell_parent", lambda p, t, e: told.append((p, t, e)))
    monkeypatch.setattr(server, "_note_create_failure", lambda t, e: None)
    monkeypatch.setattr(server._events.BUS, "emit", lambda *a, **k: None)
    rec = {
        "title": "lost",
        "kind": "mystery",
        "creates": True,
        "args": {"payload": {"prompt": "write the docs", "parent": "orch"}},
    }
    sc._orphan_failed(rec, "it waited for memory on a server that stopped")
    row = pending.failed("lost")
    assert row["error"] == "it waited for memory on a server that stopped"
    assert row["prompt_preview"] == "write the docs" and row["kind"] == "mystery"
    assert told == [("orch", "lost", row["error"])]
    sc._orphan_failed(dict(rec, title="relaunch", creates=False), "x")
    assert pending.failed("relaunch") is None
