"""The ``budget`` stop (``flight_kinds/budget.py``, docs/flights.md "Round
15") and the core's guard seam it rides on (``Kind.guard``: a done step
before the cursor that keeps watching, ``flights._guard_trip`` and the
driver's ``obs["guard"]``) — against fakes, never the real engine."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flight_kinds as K
from backend.web.core import flights as F
from backend.web.core import session_stats
from backend.web.core.flight_kinds import budget as B

NOW = time.time()
SESS = {"session": {"present": True, "status": "running", "activity": "working"}}


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    yield


_N = [0]


def _flight(*steps):
    """A flight on a fresh session (the store allows one per session)."""
    _N[0] += 1
    plan = F.adhoc_plan(list(steps), name="t")
    return F.take_off(plan, session="s%d" % _N[0], now=NOW)


def _budget(**config):
    return {"kind": "budget", "config": config}


def _past_budget(f, detail=None):
    """The budget stop (step 0) done, the cursor on step 1 running."""
    b, cur = f["steps"][0], f["steps"][1]
    f["step_states"][b["id"]]["state"] = "done"
    f["step_states"][b["id"]]["detail"] = dict(detail or {})
    f["cursor"] = 1
    f["step_states"][cur["id"]].update(state="running", since=NOW)
    return b, cur


def _obs(sid, **seen):
    return dict(
        SESS, guard={sid: dict({"usd": 0.0, "tokens": 0, "elapsed_min": 0.0}, **seen)}
    )


# --------------------------------------------------------------------------- #
# The kind
# --------------------------------------------------------------------------- #
def test_budget_is_registered_as_outside_and_perches_on_fail():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "budget")
    assert row["family"] == "outside" and row["risk"] == []
    assert row["default_on_fail"] == "you"
    assert "budget" not in F.SESSION_KINDS
    assert [x["name"] for x in row["fields"]] == [
        "max_usd",
        "max_minutes",
        "max_tokens",
        "scope",
    ]


def test_config_coerces_and_titles_itself():
    step = F.normalize_step(_budget(max_usd="5", max_minutes="60"), 0)
    assert step["config"] == {
        "max_usd": 5.0,
        "max_minutes": 60,
        "max_tokens": 0,
        "scope": "flight",
    }
    assert step["title"] == "Budget $5"
    assert step["on_fail"] == "you"
    assert F.normalize_step(_budget(max_tokens=2_000_000), 0)["title"] == (
        "Budget 2.0M tokens"
    )
    assert F.normalize_step(_budget(), 0)["title"] == "Budget"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step(_budget(scope="galaxy"), 0)
    assert err.value.field == "steps[0].config.scope"
    with pytest.raises(F.PlanError):
        F.normalize_step(_budget(max_usd=-1), 0)
    lenient = F.normalize_step(_budget(scope="galaxy", max_usd=-1), 0, strict=False)
    assert lenient["config"]["scope"] == "flight" and lenient["config"]["max_usd"] == 0


def _judge(config, seen):
    f = _flight(_budget(**config))
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    return F.next_action(f, dict(SESS, budget=seen), NOW)


def test_within_budget_passes_with_a_receipt():
    out = _judge({"max_usd": 5}, {"usd": 1.2, "tokens": 10, "elapsed_min": 3})
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "$1.20 of $5"
    out = _judge({"max_minutes": 60}, {"usd": 9, "tokens": 0, "elapsed_min": 12.7})
    assert out["detail"]["receipt"] == "12 min of 60"
    assert _judge({}, {"usd": 9})["note"] == "no limit set"


def test_over_budget_fails_and_perches_you():
    out = _judge({"max_usd": 5}, {"usd": 5.2, "tokens": 0, "elapsed_min": 0})
    assert out["action"] == "fail"
    assert out["reason"] == "over budget: $5.20 of $5"
    assert out["then"] == "you"
    out = _judge(
        {"max_usd": 5, "max_tokens": 1000}, {"usd": 1, "tokens": 1500, "elapsed_min": 0}
    )
    assert out["reason"] == "over budget: 1k of 1k"


def test_unobserved_budget_waits():
    assert _judge({"max_usd": 5}, None)["action"] == "wait"


def test_observe_reads_the_sessions_usage(monkeypatch):
    inst = SimpleNamespace(Title="s")
    monkeypatch.setattr(
        D,
        "_server",
        lambda: SimpleNamespace(ENGINE=SimpleNamespace(instances={"s": inst})),
    )
    seen = {}

    def tokens(i):
        seen["inst"] = i
        return {
            "in": 100,
            "out": 50,
            "cache_write": 10,
            "cache_read": 10**6,
            "cost": 1.5,
        }

    monkeypatch.setattr(session_stats, "_session_tokens", tokens)
    f = _flight(_budget(max_usd=5))
    f["session"] = "s"
    f["started"] = NOW - 600
    step = f["steps"][0]
    out = asyncio.run(B._observe(f, step, f["step_states"][step["id"]], {}, NOW))
    assert seen["inst"] is inst
    assert out == {"usd": 1.5, "tokens": 160, "elapsed_min": 10.0}
    f["session"] = "gone"
    out = asyncio.run(B._observe(f, step, f["step_states"][step["id"]], {}, NOW))
    assert out["usd"] == 0.0 and out["tokens"] == 0


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #
def test_guard_trips_the_current_step_with_the_budgets_on_fail():
    f = _flight(_budget(max_usd=5), {"kind": "agent", "config": {"prompt": "go"}})
    b, cur = _past_budget(f)
    assert cur["on_fail"] == "halt"
    out = F.next_action(f, _obs(b["id"], usd=5.5), NOW)
    assert out["action"] == "fail"
    assert out["reason"] == "over budget: $5.50 of $5"
    assert out["then"] == "you", "the BUDGET stop's on_fail, not the agent's"
    assert out["guard"] == b["id"]
    F.apply(f, out, NOW)
    assert f["state"] == "waiting_you"
    assert f["step_states"][cur["id"]]["state"] == "waiting_you"
    assert f["step_states"][b["id"]]["detail"]["tripped_at"] == {"usd": 5.5}


def test_guard_does_not_trip_again_until_the_figure_grows_ten_percent():
    f = _flight(_budget(max_usd=5), {"kind": "agent", "config": {"prompt": "go"}})
    b, cur = _past_budget(f)
    F.apply(f, F.next_action(f, _obs(b["id"], usd=5.5), NOW), NOW)
    # The owner carries on (Retry on the perched step).
    F.op_retry(f, cur["id"], NOW + 1)
    f["step_states"][cur["id"]]["state"] = "running"
    out = F.next_action(f, _obs(b["id"], usd=6.0), NOW + 2)
    assert out["action"] != "fail", "within 10 % of the trip: carry on"
    out = F.next_action(f, _obs(b["id"], usd=6.06), NOW + 3)
    assert out["action"] == "fail" and out["guard"] == b["id"]
    assert out["guard_detail"]["tripped_at"] == {"usd": 6.06}


def test_guard_trips_before_a_pending_step_starts():
    f = _flight(_budget(max_minutes=30), {"kind": "notify", "config": {"text": "hi"}})
    b, cur = _past_budget(f)
    f["step_states"][cur["id"]]["state"] = "pending"
    out = F.next_action(f, _obs(b["id"], elapsed_min=31), NOW)
    assert out["action"] == "fail" and out["reason"] == "over budget: 31 min of 30"
    assert F.next_action(f, _obs(b["id"], elapsed_min=29), NOW)["action"] == "run"


def test_scope_here_is_no_guard_and_unknown_usage_never_trips():
    f = _flight(
        _budget(max_usd=5, scope="here"), {"kind": "agent", "config": {"prompt": "go"}}
    )
    b, _cur = _past_budget(f)
    assert F.next_action(f, _obs(b["id"], usd=50), NOW)["action"] != "fail"
    f = _flight(_budget(max_usd=5), {"kind": "agent", "config": {"prompt": "go"}})
    b, _cur = _past_budget(f)
    assert F.next_action(f, SESS, NOW)["action"] != "fail", "no observation"


# --------------------------------------------------------------------------- #
# The seam, with a fake kind
# --------------------------------------------------------------------------- #
@pytest.fixture
def tripwire():
    seen = {"observed": 0, "raise": None}

    async def observe(flight, step, ss, sess, now):
        seen["observed"] += 1
        if seen["raise"] is not None:
            raise seen["raise"]
        return {"hot": True}

    def guard(flight, step, obs, now):
        got = (obs.get("guard") or {}).get(step["id"]) or {}
        if got.get("hot"):
            return {"action": "fail", "reason": "tripwire"}
        return None

    K.register(
        K.Kind(
            kind="tripwire",
            spec={"label": "Tripwire", "default_on_fail": "skip"},
            config=lambda raw, strict, where: {},
            judge=lambda flight, step, ss, obs, now: {"action": "done", "note": ""},
            run=None,
            observe=observe,
            needs_session=False,
            guard=guard,
        )
    )
    F.install_kinds()
    yield seen
    K._REGISTRY.pop("tripwire", None)
    F.STEP_KINDS.pop("tripwire", None)
    F.install_kinds()


def test_a_guard_kind_fails_the_current_step_with_its_own_on_fail(tripwire):
    f = _flight({"kind": "tripwire"}, {"kind": "agent", "config": {"prompt": "go"}})
    g, cur = _past_budget(f)
    out = F.next_action(f, dict(SESS, guard={g["id"]: {"hot": True}}), NOW)
    assert out == {
        "action": "fail",
        "reason": "tripwire",
        "then": "skip",
        "guard": g["id"],
    }
    F.apply(f, out, NOW)
    assert f["step_states"][cur["id"]]["state"] == "failed"
    assert F.next_action(f, dict(SESS, guard={g["id"]: {}}), NOW)["action"] != "fail"


def test_a_guard_step_not_done_does_not_guard(tripwire):
    f = _flight({"kind": "tripwire"}, {"kind": "agent", "config": {"prompt": "go"}})
    g, _cur = _past_budget(f)
    f["step_states"][g["id"]]["state"] = "failed"
    out = F.next_action(f, dict(SESS, guard={g["id"]: {"hot": True}}), NOW)
    assert out["action"] != "fail"


def test_the_driver_observes_done_guard_steps(tripwire, monkeypatch):
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: {"present": True})
    f = _flight({"kind": "tripwire"}, {"kind": "notify", "config": {"text": "x"}})
    g, cur = _past_budget(f)
    obs = asyncio.run(D.observe(f, NOW))
    assert obs["guard"] == {g["id"]: {"hot": True}}
    assert isinstance(obs["session"], dict), "obs stays a plain dict"
    tripwire["raise"] = K.StepFailed("no usage", {"why": 1})
    obs = asyncio.run(D.observe(f, NOW))
    assert obs["guard"][g["id"]] == {"failed": "no usage", "detail": {"why": 1}}
    tripwire["raise"] = RuntimeError("boom")
    assert not asyncio.run(D.observe(f, NOW)).get("guard")
    # Perched on you: nothing to guard, nothing observed.
    tripwire["raise"] = None
    f["step_states"][cur["id"]]["state"] = "waiting_you"
    before = tripwire["observed"]
    assert not asyncio.run(D.observe(f, NOW)).get("guard")
    assert tripwire["observed"] == before
