"""Two servers on one config dir (``title_store.SharedMap``, ``core.held``,
``core.sleep``): the service and a dev server share one ``~/.mindflock``.

Each store used to load its file once per process and rewrite it whole: a
sleep one server recorded was an unexplained Offline on the other, each save
dropped the other's records, and the lost updates left a title both asleep and
held. Pinned here, with a second :class:`SharedMap` over the same file standing
in for the other process:

* each sees the other's change without a restart, and no update is lost
  (sequential and concurrent, through the inter-process lock);
* the legacy ``_load()``-mutate-``_persist()`` pattern (``core.rename_stores``)
  merges too;
* a sleeping+held pair heals to held (a hold outranks a sleep);
* prune spares a title the shared ``state.json`` still has.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from backend.web.core import held, sleep, title_store


def _other(mod) -> title_store.SharedMap:
    """The same store as another process sees it."""
    return title_store.SharedMap(mod._path, mod._clean, "other process")


def _clean(d):
    return {str(k): v for k, v in (d or {}).items()}


# --------------------------------------------------------------------------- #
# SharedMap
# --------------------------------------------------------------------------- #
def test_two_maps_see_each_other_and_lose_nothing(tmp_path):
    p = str(tmp_path / "m.json")
    a = title_store.SharedMap(lambda: p, _clean, "a")
    b = title_store.SharedMap(lambda: p, _clean, "b")
    assert a.data() == {} and b.data() == {}
    with a.transaction() as recs:
        recs["x"] = 1
    with b.transaction() as recs:  # b loaded {} before: must not drop x
        recs["y"] = 2
    assert a.data() == {"x": 1, "y": 2} == b.data()
    with a.transaction() as recs:
        recs.pop("y")
    assert b.data() == {"x": 1}
    assert json.loads(open(p).read()) == {"x": 1}


def test_persist_merges_only_this_process_changes(tmp_path):
    p = str(tmp_path / "m.json")
    a = title_store.SharedMap(lambda: p, _clean, "a")
    b = title_store.SharedMap(lambda: p, _clean, "b")
    with a.transaction() as recs:
        recs.update({"keep": 1, "gone": 2, "moved": 3})
    d = b.data()  # b's view, then a writes behind its back
    with a.transaction() as recs:
        recs["new"] = 4
    d.pop("gone")
    d["moved2"] = d.pop("moved")
    b.persist()
    assert json.loads(open(p).read()) == {"keep": 1, "new": 4, "moved2": 3}
    assert a.data() == b.data() == {"keep": 1, "new": 4, "moved2": 3}


def test_a_failed_transaction_changes_nothing(tmp_path):
    p = str(tmp_path / "m.json")
    a = title_store.SharedMap(lambda: p, _clean, "a")
    with a.transaction() as recs:
        recs["x"] = 1
    with pytest.raises(RuntimeError):
        with a.transaction() as recs:
            recs["y"] = 2
            raise RuntimeError("boom")
    assert a.data() == {"x": 1}
    assert json.loads(open(p).read()) == {"x": 1}


def test_concurrent_writers_through_the_file_lock_lose_nothing(tmp_path):
    """Two maps (own mutexes: only the flock on ``m.json.lock`` orders them,
    as between two processes), four threads each."""
    p = str(tmp_path / "m.json")
    maps = [title_store.SharedMap(lambda: p, _clean, n) for n in "ab"]

    def work(m, tag):
        for i in range(25):
            with m.transaction() as recs:
                recs["%s-%d" % (tag, i)] = i

    threads = [
        threading.Thread(target=work, args=(maps[i % 2], "t%d" % i)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(open(p).read())) == 200
    assert len(maps[0].data()) == len(maps[1].data()) == 200


def test_state_titles_reads_the_shared_engine_file(tmp_path):
    store = tmp_path / "held.json"
    assert title_store.state_titles(str(store)) == set()
    (tmp_path / "state.json").write_text(
        json.dumps({"instances": [{"title": "a"}, {"title": "b"}, {"x": 1}]})
    )
    assert title_store.state_titles(str(store)) == {"a", "b"}


# --------------------------------------------------------------------------- #
# held
# --------------------------------------------------------------------------- #
def test_held_sees_another_servers_hold_and_keeps_it():
    other = _other(held)
    assert not held.is_held("a")
    with other.transaction() as recs:
        recs["a"] = {"since": 5.0, "reason": held.REASON_KILLED}
    assert held.info("a") == {"since": 5.0, "reason": held.REASON_KILLED}
    assert held.hold("b", held.REASON_CRASH)
    assert set(other.data()) == {"a", "b"}
    assert held.release("b") and set(other.data()) == {"a"}
    # The other server released it: this one knows without a restart.
    with other.transaction() as recs:
        recs.pop("a")
    assert not held.is_held("a") and held.held() == {}


def test_held_rename_moves_one_key():
    other = _other(held)
    held.hold("old", held.REASON_RESTART)
    with other.transaction() as recs:
        recs["theirs"] = {"since": 1.0, "reason": held.REASON_CRASH}
    held.rename("old", "new")
    assert set(other.data()) == {"new", "theirs"}


def test_prune_spares_a_title_the_shared_state_file_has():
    held.hold("a", held.REASON_RESTART)
    held.hold("b", held.REASON_RESTART)
    state = os.path.join(os.path.dirname(held._path()), "state.json")
    with open(state, "w") as f:
        json.dump({"instances": [{"title": "b"}]}, f)
    held.prune(["zzz"])  # this engine knows neither; the shared file knows b
    assert set(held.held()) == {"b"}


# --------------------------------------------------------------------------- #
# sleep
# --------------------------------------------------------------------------- #
def test_sleep_sees_another_servers_sleep_and_keeps_it():
    other = _other(sleep)
    assert not sleep.is_sleeping("x")
    with other.transaction() as recs:
        recs["x"] = {"since": 5.0, "reason": sleep.REASON_MEMORY, "freed_mb": 9}
    # Asleep on THIS server too: no "agent offline" alarm, painted Sleeping.
    assert sleep.info("x")["reason"] == sleep.REASON_MEMORY
    sleep.mark("y", sleep.REASON_IDLE)
    assert set(other.data()) == {"x", "y"}
    sleep.drop("y")
    sleep.woke("nothing")
    assert set(other.data()) == {"x"}
    with other.transaction() as recs:  # the other server woke it
        recs.pop("x")
    assert not sleep.is_sleeping("x")


def test_the_legacy_load_persist_pattern_merges():
    """``core.rename_stores`` moves a key with _LOCK/_load/_persist."""
    other = _other(sleep)
    sleep.mark("old", sleep.REASON_IDLE)
    with sleep._LOCK:
        d = sleep._load()
        with other.transaction() as recs:  # lands between load and persist
            recs["theirs"] = {"since": 1.0, "reason": "idle", "freed_mb": 0}
        d["new"] = d.pop("old")
        sleep._persist()
    assert set(other.data()) == {"new", "theirs"}


# --------------------------------------------------------------------------- #
# never both asleep and held
# --------------------------------------------------------------------------- #
def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f)


def test_a_pair_on_disk_heals_to_held():
    """What two servers' lost updates left in ~/.mindflock on 2026-11-10."""
    _write(
        sleep._path(),
        {
            "v": {"since": 1.0, "reason": "memory", "freed_mb": 279},
            "s": {"since": 1.0, "reason": "idle", "freed_mb": 1},
        },
    )
    _write(held._path(), {"v": {"since": 2.0, "reason": held.REASON_RESTART}})
    assert not sleep.is_sleeping("v") and sleep.info("v") is None
    assert held.is_held("v")
    assert set(json.loads(open(sleep._path()).read())) == {"s"}  # healed on disk


def test_reconcile_heals_every_pair_and_hold_drops_the_sleep():
    sleep.mark("a", sleep.REASON_IDLE)
    sleep.mark("b", sleep.REASON_IDLE)
    with _other(held).transaction() as recs:  # held behind this server's back
        recs["a"] = {"since": 9.0, "reason": held.REASON_CRASH}
    assert sleep.reconcile() == ["a"]
    assert set(sleep._load()) == {"b"}
    held.hold("b", held.REASON_KILLED)  # a new hold ends the sleep at once
    assert set(json.loads(open(sleep._path()).read())) == set()
    assert held.is_held("b")


def test_a_wake_of_the_pair_is_refused_as_held():
    from backend.web.core.agent_sessions import HELD_REFUSED

    held.hold("h", held.REASON_CRASH)
    with _other(sleep).transaction() as recs:
        recs["h"] = {"since": 1.0, "reason": "idle", "freed_mb": 0}
    assert sleep.ensure_awake(object(), "h", "test") == (False, HELD_REFUSED)
    assert held.is_held("h") and "h" not in sleep._load()


# --------------------------------------------------------------------------- #
# one sweeper per config dir
# --------------------------------------------------------------------------- #
def _lease_doc():
    return json.loads(open(sleep._lease_path()).read())


def _dead_pid() -> int:
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_the_lease_goes_to_one_process_at_a_time():
    assert sleep.hold_lease("job", 60.0)
    mine = _lease_doc()["job"]
    assert mine["pid"] == os.getpid()
    assert sleep.hold_lease("job", 60.0)  # renewing
    # Another live server holds it (its pid runs: our parent will do).
    doc = _lease_doc()
    doc["job"] = {"owner": "other", "pid": os.getppid(), "until": mine["until"] + 60}
    _write(sleep._lease_path(), doc)
    assert not sleep.hold_lease("job", 60.0)
    assert sleep.hold_lease("other-job", 60.0)  # leases are per job
    # Its owner died: taken over at once.
    doc["job"]["pid"] = _dead_pid()
    _write(sleep._lease_path(), doc)
    assert sleep.hold_lease("job", 60.0)
    assert _lease_doc()["job"]["owner"] == sleep._me()


def test_an_expired_lease_is_taken_over_and_release_hands_it_on():
    import time

    _write(
        sleep._lease_path(),
        {"job": {"owner": "other", "pid": os.getppid(), "until": time.time() - 1}},
    )
    assert sleep.hold_lease("job", 60.0)
    sleep.release_lease("job")
    assert "job" not in _lease_doc()
    # Not ours: release leaves it.
    _write(
        sleep._lease_path(),
        {"job": {"owner": "other", "pid": os.getppid(), "until": time.time() + 60}},
    )
    sleep.release_lease("job")
    assert "job" in _lease_doc()
