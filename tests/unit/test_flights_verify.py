"""Flights × Verify (docs/flights.md "Verify steps").

* A person answering the last open human item of a checklist a flight's verify
  step waits on emits ``session.test_plan_settled`` once, on the transition,
  and the flight driver taps it (a wake-up).
* A checklist asked for by a flight's verify step and the push watcher's own
  are ONE checklist: ``ensure_plan_for`` dedupes on (repo, branch).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import test_plans as tp
from backend.web.core import test_plans_driver as tpd

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    seen: list = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda event, **kw: seen.append((event, kw))
    )
    yield seen


def _checklist(plan_id="sc-1") -> dict:
    return tp.upsert(
        {
            "id": plan_id,
            "title": plan_id,
            "repo_root": "/tmp/repo",
            "branch": "feature/%s" % plan_id,
            "sha": "a" * 40,
            "live_branch": "main",
            "state": "due",
            "steps": [
                {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"},
                {"id": "s2", "text": "Look", "expect": "Right", "actor": "human"},
                {"id": "s3", "text": "Click", "expect": "Works", "actor": "human"},
            ],
            "runs": [],
        }
    )


def _waiting_flight(session="sess", plan_id="sc-1") -> dict:
    f = F.take_off(F.get_plan("builtin:verify"), session=session)

    def park(x):
        sid = x["steps"][0]["id"]
        x["state"] = "waiting_you"
        x["step_states"][sid].update(state="waiting_you", detail={"plan": plan_id})

    return F.mutate(f["id"], park)


def _answer(step, result="pass", plan_id="sc-1"):
    r = client.post(
        "/api/test-plans/%s/result" % plan_id, json={"step_id": step, "result": result}
    )
    assert r.status_code == 200, r.text


def _settled(seen):
    return [kw for ev, kw in seen if ev == "session.test_plan_settled"]


def test_settling_the_last_human_item_is_said_once(_isolated):
    seen = _isolated
    _checklist()
    f = _waiting_flight()
    _answer("s2")
    assert _settled(seen) == []  # s3 is still yours
    _answer("s3", "fail")
    (kw,) = _settled(seen)
    assert kw["session"] == "sess"
    assert kw["data"] == {
        "plan": "sc-1",
        "title": "sc-1",
        "branch": "feature/sc-1",
        "flight": f["id"],
    }
    # Already settled: re-answering is not a transition.
    _answer("s3", "pass")
    assert len(_settled(seen)) == 1


def test_the_first_answer_can_be_the_last_one_owed(_isolated):
    # Nobody has checked the plan yet (no run), so nothing is "open" before
    # this answer — but the answer opens the run AND settles it. The flight
    # parked on the plan still has to hear it.
    seen = _isolated
    plan = _checklist()
    plan["steps"] = [s for s in plan["steps"] if s["id"] == "s2"]
    tp.upsert(plan)
    f = _waiting_flight()
    _answer("s2")
    (kw,) = _settled(seen)
    assert kw["data"]["flight"] == f["id"]
    _answer("s2", "fail")
    assert len(_settled(seen)) == 1


def test_nothing_is_said_when_no_flight_waits_on_it(_isolated):
    seen = _isolated
    _checklist()
    _waiting_flight(plan_id="other")
    _answer("s2")
    _answer("s3")
    assert _settled(seen) == []


def test_the_driver_taps_it(monkeypatch):
    woke: list = []
    monkeypatch.setattr(D, "wake", lambda: woke.append(1))
    assert "session.test_plan_settled" in D.WAKE_EVENTS
    D._on_event({"event": "session.test_plan_settled", "session": "sess"})
    assert woke == [1]


def test_a_flights_checklist_and_the_push_watchers_are_one(tmp_path, monkeypatch):
    """The verify step finds or makes the session's plan (empty: the agent
    writes its own steps when the check starts); the push watcher's
    ``session.pushed`` asks again for the same (repo, branch) — one plan, and
    nothing generated over the self-verify's plan."""
    wt = tmp_path / "wt"
    wt.mkdir()
    inst = SimpleNamespace(
        Program="claude",
        GetWorktreePath=lambda: str(wt),
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: str(tmp_path)),
    )
    server.ENGINE.instances["sess"] = inst
    monkeypatch.setattr(server, "_current_branch", lambda w: "feature/x")
    monkeypatch.setattr(server, "_git_head_sha", lambda w: "b" * 40)
    monkeypatch.setattr(tpd, "_verify_auto_for", lambda *a: True)
    monkeypatch.setattr(tpd, "_test_plan_intent", lambda *a: "")
    generated: list = []
    monkeypatch.setattr(
        tpd, "_generate_test_plan", lambda title, *a, **k: generated.append(title)
    )

    class _Now:
        """threading.Thread, run on the spot."""

        def __init__(self, target, args=(), **_kw):
            self._run = lambda: target(*args)

        def start(self):
            self._run()

    monkeypatch.setattr(tpd, "threading", SimpleNamespace(Thread=_Now))
    flight = F.take_off(F.get_plan("builtin:verify"), session="sess")
    # The verify step's ask (flight_driver → server._ensure_verify_plan).
    assert D._ensure_checklist(flight) == {"plan": "sess"}
    # …and the push watcher, for the same branch.
    tpd._on_session_pushed({"event": "session.pushed", "session": "sess"})
    tpd._on_session_pushed({"event": "session.pushed", "session": "sess"})
    plans = [
        p
        for p in tp.list_plans()
        if p["branch"] == "feature/x" and p["repo_root"] == str(tmp_path)
    ]
    assert len(plans) == 1
    assert plans[0]["origin"] == "manual" and plans[0]["steps"] == []
    assert generated == []
