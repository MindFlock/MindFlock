"""Return wires explain themselves (docs/vision/fixwire.md): which stops can
send work back to the agent and why (``returns_of``), and the fix under way
(``detail.fix``) — fed from a Ship stop's lane record and from ``on_fail:
agent`` rounds, with one ``fixing`` / ``fixed`` history event each. Pure, on
tmp stores (never the real engine or ``~/.mindflock``)."""

import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    yield


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


AGENT = {"kind": "agent", "config": {"prompt": "work the ticket"}}


def SHIP(lane, **kw):
    return dict({"kind": "ship", "config": {"lane": lane}}, **kw)


def CHECKS(on_fail="agent"):
    return {"kind": "checks", "config": {}, "on_fail": on_fail}


def _at(f, i):
    """``f`` with every stop before ``i`` done and stop ``i`` running."""
    for s in f["steps"][:i]:
        f["step_states"][s["id"]].update(state="done", since=NOW - 3600)
    f["cursor"] = i
    f["state"] = "running"
    f["step_states"][f["steps"][i]["id"]].update(state="running", since=NOW - 60)
    return f


def _lane(f, lane, **rec):
    base = {
        "source": "flight",
        "item": f["id"],
        "lane": lane,
        "depth": lane,
        "state": "running",
        "note": "",
        "fixing": "",
        "fix_reason": "",
        "fix_rounds": 0,
        "fix_since": 0.0,
    }
    base.update(rec)
    return base


def _pass(f, obs, now=NOW):
    return F.apply(f, F.next_action(f, obs, now), now)


SESS = {"present": True, "activity": "working"}


# --------------------------------------------------------------------------- #
# returns: which stops can go back, and the case on their wire
# --------------------------------------------------------------------------- #
def test_the_fast_track_route_returns_from_commit_and_push_only():
    f = fly(AGENT, SHIP("commit"), SHIP("push"), SHIP("pr"))
    ids = [s["id"] for s in f["steps"]]
    r = F.returns_of(f)
    assert set(r) == {ids[1], ids[2]}
    assert r[ids[1]] == {
        "case": "if pre-commit fails",
        "of": 2,
        "kinds": ["precommit"],
        "to": ids[0],
    }
    assert r[ids[2]]["case"] == "if the checks fail"
    assert r[ids[2]]["to"] == ids[0]  # the nearest Agent stop before it


def test_a_lone_ship_stop_covers_every_rung_it_climbs():
    f = fly(SHIP("merge"))
    r = F.returns_of(f)[f["steps"][0]["id"]]
    assert r["kinds"] == ["precommit", "check", "ci", "conflict"]
    assert r["case"].startswith("if pre-commit fails or the checks fail")
    # No Agent stop before it: back to the session's agent tile.
    assert r["to"] == F.LOOP_AGENT


def test_lane_failure_notes_off_hands_nothing_back():
    f = fly(AGENT, SHIP("commit"))
    assert F.returns_of(f, lane_notes=False) == {}


def test_an_on_fail_agent_checks_stop_returns_with_its_case():
    f = fly(AGENT, CHECKS("agent"))
    r = F.returns_of(f)[f["steps"][1]["id"]]
    assert r["case"] == "if CI checks fail"
    assert r["of"] == F.MAX_FIX_ROUNDS


def test_on_fail_halt_draws_no_return():
    f = fly(AGENT, CHECKS("halt"), {"kind": "notify", "config": {"text": "hi"}})
    assert F.returns_of(f) == {}


def test_reviews_always_go_back_to_the_agent():
    f = fly(AGENT, {"kind": "reviews", "config": {"max_rounds": 3}})
    r = F.returns_of(f)[f["steps"][1]["id"]]
    assert r["case"] == "if reviewers ask for changes"
    assert r["of"] == 3


def test_the_api_dto_carries_returns_but_the_store_does_not():
    f = fly(AGENT, SHIP("commit"))
    out = D.list_dto()
    assert out[0]["returns"][f["steps"][1]["id"]]["case"] == "if pre-commit fails"
    assert "returns" not in F.get(f["id"])


# --------------------------------------------------------------------------- #
# detail.fix: a Ship stop's lane hands the failure to the agent
# --------------------------------------------------------------------------- #
def test_a_lane_fixing_precommit_shows_on_the_ship_stop():
    f = _at(fly(AGENT, SHIP("commit"), SHIP("push")), 1)
    sid = f["steps"][1]["id"]
    rec = _lane(
        f,
        "commit",
        fixing="precommit",
        fix_reason="pre-commit failed at ruff\nlong output",
        fix_rounds=1,
        fix_since=NOW - 30,
        note="the agent is fixing it: pre-commit failed at ruff",
    )
    _pass(f, {"session": SESS, "autopilot": rec})
    fix = f["step_states"][sid]["detail"]["fix"]
    assert fix["active"] is True
    assert fix["round"] == 1 and fix["of"] == 2
    assert fix["reason"] == "pre-commit failed at ruff"
    assert fix["to"] == f["steps"][0]["id"]
    assert fix["since"] == NOW - 30
    # Many driver passes, one history event.
    for k in range(3):
        _pass(f, {"session": SESS, "autopilot": rec}, NOW + k)
    fixing = [h for h in f["history"] if h["event"] == "fixing"]
    assert len(fixing) == 1
    assert fixing[0]["note"] == (
        "pre-commit failed at ruff — the agent is fixing it (round 1 of 2)"
    )


def test_the_fix_ends_fixed_when_the_ship_stop_passes():
    f = _at(fly(AGENT, SHIP("commit"), SHIP("push")), 1)
    sid = f["steps"][1]["id"]
    fixing = _lane(
        f,
        "commit",
        fixing="precommit",
        fix_reason="pre-commit failed at ruff",
        fix_rounds=1,
        fix_since=NOW,
    )
    _pass(f, {"session": SESS, "autopilot": fixing})
    after = dict(fixing, fixing="")
    _pass(f, {"session": SESS, "autopilot": after}, NOW + 5)
    assert f["step_states"][sid]["detail"]["fix"]["active"] is False
    done = dict(after, state="done")
    _pass(f, {"session": SESS, "autopilot": done}, NOW + 10)
    fix = f["step_states"][sid]["detail"]["fix"]
    assert f["step_states"][sid]["state"] == "done"
    assert fix["active"] is False and fix["rounds"] == 1
    assert fix["outcome"] == "fixed"
    fixed = [h for h in f["history"] if h["event"] == "fixed"]
    assert [h["note"] for h in fixed] == ["fixed after 1 round"]


def test_a_ship_stop_whose_lane_never_handed_back_has_no_fix():
    f = _at(fly(AGENT, SHIP("commit")), 1)
    _pass(f, {"session": SESS, "autopilot": _lane(f, "commit", note="committing")})
    assert "fix" not in f["step_states"][f["steps"][1]["id"]]["detail"]


def test_a_lane_that_halts_after_its_rounds_keeps_the_fix_as_halted():
    f = _at(fly(AGENT, SHIP("commit")), 1)
    sid = f["steps"][1]["id"]
    rec = _lane(
        f,
        "commit",
        fixing="precommit",
        fix_reason="pre-commit failed at ruff",
        fix_rounds=2,
        fix_since=NOW,
    )
    _pass(f, {"session": SESS, "autopilot": rec})
    halted = dict(rec, fixing="", state="halted", reason="pre-commit failed at ruff")
    _pass(f, {"session": SESS, "autopilot": halted}, NOW + 5)
    fix = f["step_states"][sid]["detail"]["fix"]
    assert f["state"] == "halted"
    assert fix["active"] is False and fix["outcome"] == "halted"
    assert not [h for h in f["history"] if h["event"] == "fixed"]


# --------------------------------------------------------------------------- #
# detail.fix: an on_fail: agent stop's rounds
# --------------------------------------------------------------------------- #
def test_an_on_fail_agent_round_is_a_fix_with_its_round():
    f = _at(fly(AGENT, CHECKS("agent")), 1)
    sid = f["steps"][1]["id"]
    act = {"action": "fail", "reason": "CI failed: lint", "then": "agent"}
    F.apply(f, act, NOW)
    ss = f["step_states"][sid]
    fix = ss["detail"]["fix"]
    assert ss["detail"]["phase"] == "fix"
    assert fix == {
        "active": True,
        "round": 1,
        "rounds": 1,
        "of": F.MAX_FIX_ROUNDS,
        "reason": "CI failed: lint",
        "since": NOW,
        "to": f["steps"][0]["id"],
    }
    # The retry after the agent's fix: the fix is over, its record stays.
    F.apply(f, {"action": "run", "retry": True, "note": "retrying"}, NOW + 60)
    fix = ss["detail"]["fix"]
    assert fix["active"] is False and "phase" not in ss["detail"]
    # Second failure: round 2, a second `fixing` event.
    F.apply(f, dict(act, reason="CI failed: tests"), NOW + 120)
    assert ss["detail"]["fix"]["round"] == 2 and ss["detail"]["fix"]["active"]
    F.apply(f, {"action": "run", "retry": True, "note": "retrying"}, NOW + 180)
    F.apply(f, {"action": "done", "note": "green"}, NOW + 240)
    fix = ss["detail"]["fix"]
    assert fix["outcome"] == "fixed" and fix["rounds"] == 2
    events = [h["event"] for h in f["history"] if h["step"] == sid]
    assert events.count("fixing") == 2
    assert events.count("fixed") == 1
    assert [h["note"] for h in f["history"] if h["event"] == "fixed"] == [
        "fixed after 2 rounds"
    ]


def test_the_fix_goes_to_the_session_agent_with_no_agent_stop_before():
    f = _at(fly(CHECKS("agent")), 0)
    F.apply(f, {"action": "fail", "reason": "red", "then": "agent"}, NOW)
    fix = f["step_states"][f["steps"][0]["id"]]["detail"]["fix"]
    assert fix["to"] == F.LOOP_AGENT


def test_the_next_action_resolves_a_failed_checks_stop_to_a_fix():
    f = _at(fly(AGENT, CHECKS("agent")), 1)
    obs = {"session": SESS, "checks": {"verdict": "failed", "failing": ["lint"]}}
    _pass(f, obs)
    fix = f["step_states"][f["steps"][1]["id"]]["detail"].get("fix")
    assert fix and fix["active"] and fix["round"] == 1


def test_short_reason_is_one_short_line():
    assert F.short_reason("  a\n b ") == "a"
    assert len(F.short_reason("x" * 200)) == 90
