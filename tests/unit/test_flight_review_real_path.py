"""A review stop's rounds through the driver's REAL pass (``_step_member``:
next_action → ``_mark_intent`` → ``_perform`` → ``_record_outcome``), not by
calling ``review._run`` on the flight before the mark. The mark records a
fresh ``run`` with an empty detail; what a later round needs (the earlier
findings, the re-ask count) must survive it, or every round is a fresh full
review and every fix round restarts the re-asks."""

import asyncio

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import review as R

NOW = 1_800_000_000.0
FINDINGS = "1. a.py:3 — off by one\n2. b.py:9 — None deref"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The driver's pass with only its edges faked: what ``observe`` sees,
    the reviewer spawn, the session's HEAD, the agent's inbox."""
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    R._FINISHED.clear()
    w = {
        "obs": {},
        "briefs": [],
        "titles": [],
        "head": "sha1",
        "since": [],
        "delivered": [],
    }

    async def _observe(view, now):
        return dict(w["obs"])

    async def _spawn(flight, step, prompt, **kw):
        title = "review-%d" % (len(w["titles"]) + 1)
        w["titles"].append(title)
        w["briefs"].append(prompt)
        return title

    def _head(flight, since=""):
        w["since"].append(since)
        return w["head"], bool(since)

    monkeypatch.setattr(D, "observe", _observe)
    monkeypatch.setattr(D, "spawn_worker", _spawn)
    monkeypatch.setattr(D, "_deliver", lambda t, x: w["delivered"].append(x))
    monkeypatch.setattr(D, "_announce", lambda before, after: None)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")
    monkeypatch.setattr(D, "_close_abandoned_reviewers", lambda b, a: [])
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(R, "_base_of", lambda flight: "main")
    monkeypatch.setattr(R, "_head_of", _head)
    yield w
    R._FINISHED.clear()


def sess(**kw):
    return dict({"present": True, "status": "running", "activity": "idle"}, **kw)


def fly(**config):
    st = {"kind": "review", "config": config, "on_fail": "agent"}
    plan = F.save_plan({"name": "P", "steps": [st, {"kind": "notify"}]}, now=NOW)
    return F.take_off(plan, session="s", now=NOW)["id"]


def tick(w, fid, now, **obs):
    """One real pass of the driver over the flight's current stop."""
    w["obs"] = dict({"session": sess()}, **obs)
    f = F.get(fid)
    out = asyncio.run(D._step_member(fid, f, f["cursor"], now))
    return out[1] if out else None


def detail(fid):
    f = F.get(fid)
    return f["step_states"][f["steps"][0]["id"]]["detail"]


def ss(fid):
    f = F.get(fid)
    return f["step_states"][f["steps"][0]["id"]]


def finds(text, title):
    entry = {
        "title": title,
        "state": "done",
        "report": {"id": "m", "status": "done", "summary": text, "ts": NOW + 5},
    }
    return {"entry": entry, "text": text, "report": True, "closed": "deleted"}


def gone(title):
    return {"entry": {"title": title, "state": "gone", "started": True}, "text": ""}


def fix_and_retry(w, fid, t):
    """The agent's fix turn ends and settles; the retry pass asks again."""
    after = sess(worked_at=t + 1, turn_ended_at=t + 2)
    w["obs"] = {"session": after}
    tick(w, fid, t + 3, session=after)
    a = tick(w, fid, t + 400, session=after)
    assert a is not None and a["action"] == "run", a
    return a


def test_findings_fix_retry_is_a_follow_up_on_the_real_path(world):
    w = world
    fid = fly()
    a = tick(w, fid, NOW)
    assert a["action"] == "run" and w["titles"] == ["review-1"]
    d = detail(fid)
    assert d["reviewed_sha"] == "sha1" and d["round"] == 1
    assert d["children"] == ["review-1"]

    a = tick(w, fid, NOW + 10, review=finds(FINDINGS, "review-1"))
    assert a["action"] == "fail" and a["then"] == "agent"
    assert "check your fixes" in w["delivered"][-1]
    assert detail(fid)["followup"] == {"findings": FINDINGS, "sha": "sha1", "round": 1}

    w["head"] = "sha2"
    a = fix_and_retry(w, fid, NOW + 20)
    brief = w["briefs"][1]
    assert w["since"][-1] == "sha1"
    assert "FOLLOW-UP" in brief and "round 2" in brief
    assert FINDINGS in brief and "`git diff sha1..HEAD`" in brief
    d = detail(fid)
    assert d["round"] == 2 and d["reviewed_sha"] == "sha2"
    assert d["followup"]["sha"] == "sha1" and d["children"] == ["review-2"]
    assert ss(fid)["note"].startswith("asking review-2 to check the fixes")

    # Round 2 clean: done.
    a = tick(w, fid, NOW + 500, review=finds("No findings", "review-2"))
    assert a["action"] == "done" and F.get(fid)["cursor"] == 1


def test_a_persons_retry_from_the_unresolved_park_follows_up(world):
    w = world
    fid = fly(rounds=1)
    tick(w, fid, NOW)
    tick(w, fid, NOW + 10, review=finds(FINDINGS, "review-1"))
    w["head"] = "sha2"
    fix_and_retry(w, fid, NOW + 20)
    a = tick(w, fid, NOW + 500, review=finds(FINDINGS, "review-2"))
    assert a["action"] == "you"
    f = F.get(fid)
    assert f["state"] == "waiting_you"
    assert detail(fid)["ask"] == "unresolved"
    assert detail(fid)["followup"] == {"findings": FINDINGS, "sha": "sha2", "round": 2}

    # The person presses Retry (the route's own op).
    sid = f["steps"][0]["id"]
    D.operate(fid, "retry", sid)
    assert ss(fid)["rounds"] == 0 and "ask" not in detail(fid)
    w["head"] = "sha3"
    a = tick(w, fid, NOW + 600)
    assert a["action"] == "run"
    brief = w["briefs"][-1]
    assert "FOLLOW-UP" in brief and "round 3" in brief
    assert "`git diff sha2..HEAD`" in brief
    assert detail(fid)["round"] == 3 and detail(fid)["reviewed_sha"] == "sha3"
    # Its findings go back to the agent: a new set of rounds.
    a = tick(w, fid, NOW + 700, review=finds(FINDINGS, w["titles"][-1]))
    assert a["action"] == "fail" and a["then"] == "agent"


def test_reasks_are_bounded_across_the_whole_visit(world):
    w = world
    fid = fly(rounds=3)
    tick(w, fid, NOW)
    tick(w, fid, NOW + 10, review=finds(FINDINGS, "review-1"))
    w["head"] = "sha2"
    fix_and_retry(w, fid, NOW + 20)  # review-2: the round-2 follow-up

    # review-2 vanishes: re-ask 1 — still the round-2 follow-up.
    a = tick(w, fid, NOW + 500, review=gone("review-2"))
    assert a["action"] == "run" and detail(fid)["reasks"] == 1
    assert "FOLLOW-UP" in w["briefs"][-1] and "round 2" in w["briefs"][-1]
    assert detail(fid)["round"] == 2 and detail(fid)["followup"]["sha"] == "sha1"
    assert ss(fid)["rounds"] == 1

    # review-3 finds something: the fix round keeps the visit's re-ask count.
    tick(w, fid, NOW + 600, review=finds("1. c.py:1 — leak", "review-3"))
    assert detail(fid)["reasks"] == 1
    w["head"] = "sha3"
    fix_and_retry(w, fid, NOW + 700)  # review-4
    assert detail(fid)["reasks"] == 1 and detail(fid)["round"] == 3

    a = tick(w, fid, NOW + 1200, review=gone("review-4"))
    assert a["action"] == "run" and detail(fid)["reasks"] == 2
    a = tick(w, fid, NOW + 1300, review=gone("review-5"))
    # Out of re-asks for this visit: the person, plain reason, no round spent.
    assert a["action"] == "fail" and a["then"] == "halt"
    f = F.get(fid)
    assert f["state"] == "halted" and f["reason"] == "the reviewer did not finish"
    assert ss(fid)["rounds"] == 2
    assert len(w["titles"]) == 5
    # A person's Retry starts the re-asks over and stays a follow-up.
    D.operate(fid, "retry", f["steps"][0]["id"])
    assert "reasks" not in detail(fid)
    assert detail(fid)["followup"]["findings"] == "1. c.py:1 — leak"
