"""Stability sweep of the flight driver: defects found and pinned here, each
against the same fakes ``test_flights_driver.py`` uses (a fake server
namespace, fake rows), never the real engine or ``~/.mindflock``."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    yield


class FakeInst:
    def __init__(self, path="/repo"):
        self.Path = path
        self.Branch = "feat"
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


@pytest.fixture
def row(monkeypatch):
    state = {"activity": "idle", "branch": "feat", "folder": "/repo", "pr_url": ""}

    def fake(flight, now, agent):
        out = {
            "present": flight["session"] == "s",
            "pending": False,
            "status": "running",
            "sleeping": False,
        }
        out.update(state)
        return out

    monkeypatch.setattr(D, "_session_obs", fake)
    return state


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


def run(coro):
    return asyncio.run(coro)


def test_a_stale_performing_mark_does_not_stall_the_flight_forever(
    srv, row, monkeypatch
):
    """A server that died mid-effect leaves ``detail.performing`` behind. The
    judge stops honouring it after PERFORM_STALE_S and asks for the effect
    again; the intent record must then take the step over, not refuse it
    for ever (the flight sat "running" with no write and no reason)."""
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(s["id"]))
    f = fly({"kind": "notify", "config": {"text": "hi"}})

    def died_mid_effect(x):
        x["step_states"]["s1"]["detail"]["performing"] = {
            "token": "dead-server",
            "at": time.time() - F.PERFORM_STALE_S - 5,
            "action": "run",
            "phase": "",
        }

    F.mutate(f["id"], died_mid_effect)
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sent == ["s1"]
    assert got["state"] == "done"
    assert "performing" not in got["step_states"]["s1"]["detail"]


def test_a_fresh_performing_mark_still_blocks_a_second_effect(srv, row, monkeypatch):
    """The other half: a mark inside PERFORM_STALE_S (another server is
    carrying the effect out right now) is never doubled."""
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(s["id"]))
    f = fly({"kind": "notify", "config": {"text": "hi"}})

    def busy(x):
        x["step_states"]["s1"]["detail"]["performing"] = {
            "token": "other",
            "at": time.time(),
            "action": "run",
            "phase": "",
        }

    F.mutate(f["id"], busy)
    run(D.step_flight(f["id"]))
    assert sent == []
    assert F.get(f["id"])["state"] == "running"


def test_the_lease_is_not_rewritten_on_every_pass(srv, row, monkeypatch):
    """Per-tick cost: ``flights.claim`` rewrites the WHOLE store (up to
    KEEP_FINISHED finished flights) — every active flight on every pass
    (each 5 s, and on every wake event) cost a full deep copy + atomic write
    of the store. Its own holder refreshes it only once a quarter of
    LEASE_STALE_S has passed; a wait that did not change writes nothing."""
    monkeypatch.setattr(D, "_deliver", lambda t, x: None)
    f = fly({"kind": "agent", "config": {"prompt": "go"}})
    run(D.step_flight(f["id"]))
    row["activity"] = "working"
    run(D.step_flight(f["id"]))  # records worked_at
    saves = []
    real = F._save_flights_locked
    monkeypatch.setattr(
        F, "_save_flights_locked", lambda fl: saves.append(1) or real(fl)
    )
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    assert saves == []
    # Once the refresh is due, the lease is written again (and only it).
    t = time.time() + D._LEASE_REFRESH_S + 1
    monkeypatch.setattr(D.time, "time", lambda: t)
    run(D.step_flight(f["id"]))
    assert len(saves) == 1
    got = F.get(f["id"])
    assert got["owner"] == "boot-1" and got["owner_at"] == t


def test_another_live_servers_lease_is_skipped_without_a_write(srv, row, monkeypatch):
    """The other server's flight is skipped from the cached read — no
    write-lock read-modify-write of the whole store per pass — and taken
    over once its lease is stale."""
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(1))
    f = fly({"kind": "notify", "config": {}})
    F.claim(f["id"], "other-boot", time.time())
    loads = []
    real = F._load_flights_locked
    monkeypatch.setattr(F, "_load_flights_locked", lambda: loads.append(1) or real())
    run(D.step_flight(f["id"]))
    assert sent == [] and loads == []
    t = time.time() + F.LEASE_STALE_S + 1
    monkeypatch.setattr(D.time, "time", lambda: t)
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sent == [1] and got["owner"] == "boot-1" and got["boot"] == "boot-1"


def test_an_error_judging_a_stop_is_said_on_the_flight(srv, row, monkeypatch):
    """An exception while observing or judging one flight was only logged
    (every pass, for ever): the route sat on its old note with nothing for a
    person to read. It now says so on the stop — and the other flights of
    the pass are still driven."""
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(f["id"]))
    bad = fly({"kind": "notify", "config": {"text": "x"}})
    good = fly({"kind": "notify", "config": {"text": "y"}}, session="t")
    srv.ENGINE.instances["t"] = FakeInst()
    real = F.next_action

    def judge(view, obs, now):
        if view["id"] == bad["id"]:
            raise RuntimeError("the judge broke")
        return real(view, obs, now)

    monkeypatch.setattr(F, "next_action", judge)
    run(D.run_pass())
    got = F.get(bad["id"])
    assert got["state"] == "running"
    assert "the judge broke" in got["step_states"]["s1"]["note"]
    assert sent == [good["id"]] and F.get(good["id"])["state"] == "done"
    # Said once: the same error on the next pass writes nothing new.
    saves = []
    real_save = F._save_flights_locked
    monkeypatch.setattr(
        F, "_save_flights_locked", lambda fl: saves.append(1) or real_save(fl)
    )
    run(D.step_flight(bad["id"]))
    assert saves == []
