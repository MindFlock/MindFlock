"""Closing a session and undoing it brings its chain back (docs/flights-reopen.md).

The close cancels the session's flight at once (a namesake must never pick up
its cursor), but the Recently-closed entry keeps a snapshot of it, and a
reopen of THAT entry restores the chain where it was."""

import time
from types import SimpleNamespace

import pytest

from backend.web.core import events as _events
from backend.web.core import flight_driver as D
from backend.web.core import flights as F


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
        published=[],
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    yield


@pytest.fixture
def emitted():
    got = []
    unsub = _events.BUS.subscribe(
        lambda env: (
            got.append(env) if str(env.get("event", "")).startswith("flight.") else None
        )
    )
    yield got
    unsub()


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


YOU = {"kind": "you", "config": {"text": "look"}}
WAIT = {"kind": "wait", "config": {"seconds": 60}}


def close(title="s"):
    """What a close does, in order: the entry's snapshot first, then the
    removal hook that cancels the chain."""
    snap = F.removal_snapshot(title)
    D.on_session_removed(title)
    return snap


def test_an_in_air_chain_comes_back_at_the_same_stop(emitted):
    f = fly(WAIT, YOU, WAIT)
    F.mutate(f["id"], lambda x: x.update(cursor=1))
    before = F.get(f["id"])
    snap = close()
    assert F.get(f["id"])["state"] == "cancelled"
    got = D.restore_on_reopen(snap, "s")
    assert got is not None
    after = F.get(f["id"])
    assert after["state"] == before["state"] == "running"
    assert after["cursor"] == 1
    assert after["step_states"] == before["step_states"]
    assert after["finished"] == 0.0 and after["session"] == "s"
    assert after["history"][-1]["event"] == "resumed"
    assert after["history"][-1]["note"] == "restored when the session was reopened"
    assert "flight.changed" in [e["event"] for e in emitted]


def test_a_ready_chain_comes_back_ready():
    f = fly(WAIT, start_on="run")
    assert F.get(f["id"])["state"] == "ready"
    snap = close()
    D.restore_on_reopen(snap, "s")
    assert F.get(f["id"])["state"] == "ready"


def test_a_paused_chain_comes_back_paused_with_its_reason():
    f = fly(WAIT, YOU)
    F.mutate(f["id"], lambda x: F.op_pause(x))
    snap = close()
    D.restore_on_reopen(snap, "s")
    got = F.get(f["id"])
    assert got["state"] == "paused"


def test_the_reopened_title_carries_the_chain():
    f = fly(WAIT)
    snap = close("s")
    D.restore_on_reopen(snap, "s-2")
    assert F.get(f["id"])["session"] == "s-2"
    assert F.for_session("s-2")["id"] == f["id"]


def test_a_landed_chain_has_nothing_to_restore():
    f = fly(WAIT)
    F.mutate(f["id"], lambda x: F._finish(x, "done", "", time.time()))
    assert F.removal_snapshot("s") is None
    D.on_session_removed("s")
    assert F.get(f["id"])["state"] == "done"


def test_a_namesake_session_gets_no_chain():
    """A new session under the old title never restores anything: only the
    closed entry's snapshot does, and nothing else calls restore."""
    f = fly(WAIT)
    close()
    assert F.for_session("s") is None
    assert F.get(f["id"])["state"] == "cancelled"


def test_a_chain_live_on_the_title_is_not_clobbered():
    old = fly(WAIT)
    snap = close()
    new = fly(YOU)
    assert D.restore_on_reopen(snap, "s") is None
    assert F.get(old["id"])["state"] == "cancelled"
    assert F.for_session("s")["id"] == new["id"]
    assert "another chain is live" in F.get(old["id"])["history"][-1]["note"]


def test_a_chain_you_cancelled_stays_cancelled():
    f = fly(WAIT)
    snap = F.removal_snapshot("s")
    F.mutate(f["id"], lambda x: F.op_cancel(x, "cancelled by you"))
    assert D.restore_on_reopen(snap, "s") is None
    assert F.get(f["id"])["state"] == "cancelled"


def test_no_snapshot_restores_nothing():
    assert D.restore_on_reopen(None, "s") is None
    assert D.restore_on_reopen({"id": "f_missing", "state": "running"}, "s") is None


def test_a_later_finish_is_announced_again(emitted):
    f = fly(WAIT)
    snap = close()
    assert any(str(k).startswith("finished:") for k in F.get(f["id"])["announced"])
    D.restore_on_reopen(snap, "s")
    assert not any(str(k).startswith("finished:") for k in F.get(f["id"])["announced"])


def test_the_close_record_keeps_the_snapshot(monkeypatch, tmp_path):
    """recently_closed._record_closed stores the chain with the entry."""
    from backend.web.core import recently_closed as RC

    f = fly(WAIT)
    saved = []
    ns = SimpleNamespace(
        _load_recently_closed=lambda: [],
        _save_recently_closed=lambda items: saved.append(items),
    )
    monkeypatch.setattr(RC, "_server", lambda: ns)
    monkeypatch.setattr(RC, "_hidden_by_of", lambda t: "")
    monkeypatch.setattr(RC, "_shared_branch_of", lambda t: {})
    monkeypatch.setattr(RC, "_person_of", lambda t: "")
    inst = SimpleNamespace(
        Title="s",
        Branch="feat",
        Path=str(tmp_path),
        InPlace=False,
        Provisioned=False,
        ToInstanceData=lambda: SimpleNamespace(to_dict=lambda: {"title": "s"}),
        GetWorktreePath=lambda: str(tmp_path),
    )
    RC._record_closed(inst)
    entry = saved[-1][0]
    assert entry["flight"] == {"id": f["id"], "state": "running", "reason": ""}
