"""Receipts of the built-in kinds (docs/flights.md "Round 15", "**Receipts.**"):
``detail.receipt`` on the ``done`` a judge (or an executor that is done at
once: ticket / notify / split) returns, ≤ 80 chars, kept in the step's detail
once the stop has flown."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    yield


def step(kind, sid=None, on_fail="halt", **config):
    out = {"kind": kind, "config": config, "on_fail": on_fail}
    if sid:
        out["id"] = sid
    return out


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def fly(*steps, session="s", subject=None, now=NOW):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=now)
    return F.take_off(plan, session=session, subject=subject, now=now)


_N = [0]


def started(*steps, detail=None, since=NOW, **kw):
    """A flight (on a session of its own) whose first step is running since
    ``since``."""
    _N[0] += 1
    kw.setdefault("session", "s%d" % _N[0])
    f = fly(*steps, **kw)
    ss = f["step_states"][f["steps"][0]["id"]]
    ss.update(state="running", since=since, detail=dict(detail or {}))
    f["state"] = "running"
    return f, ss


def judge(f, obs, now):
    return F.next_action(f, dict({"session": sess()}, **obs), now)


def receipt(action):
    assert action["action"] == "done", action
    return action["detail"]["receipt"]


def ap(f, lane, **kw):
    rec = {"source": "flight", "item": f["id"], "lane": lane, "depth": lane}
    rec.update(kw)
    return rec


# --------------------------------------------------------------------------- #
# The pure judges
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "lane,url,want",
    [
        ("pr", "https://github.com/o/r/pull/291", "PR #291"),
        ("pr", "https://gitlab.com/o/r/-/merge_requests/7", "PR #7"),
        ("pr", "", "PR opened"),
        ("merge", "https://github.com/o/r/pull/291", "merged"),
        ("push", "", "pushed"),
        ("commit", "", "committed"),
    ],
)
def test_ship_receipt_names_the_pr_or_the_lane(lane, url, want):
    f, _ = started(step("ship", lane=lane))
    a = judge(
        f, {"session": sess(), "autopilot": ap(f, lane, state="done", url=url)}, NOW
    )
    assert receipt(a) == want
    assert F.apply(f, a, NOW)["step_states"]["s1"]["detail"]["receipt"] == want


def test_agent_receipt_is_how_long_it_took_to_settle():
    f, ss = started(step("agent", prompt="go", join=False))
    ss["detail"].update(worked_at=NOW + 10, idle_since=NOW + 11 * 60)
    a = judge(f, {"session": sess()}, NOW + 12 * 60)
    assert receipt(a) == "settled after 12 min"
    g, gs = started(step("agent", prompt="go"))
    gs["detail"].update(worked_at=NOW + 10, idle_since=NOW + 7200)
    a = judge(g, {"session": sess(), "children": []}, NOW + 7500)
    assert receipt(a) == "settled after 2 h 5 min"


def test_checks_receipt():
    f, _ = started(step("checks"))
    assert receipt(judge(f, {"checks": {"verdict": "ok"}}, NOW + 5)) == "all green"
    late = NOW + F.CHECKS_NONE_GRACE_S + 1
    assert receipt(judge(f, {"checks": {"verdict": "none"}}, late)) == "no checks"


def test_reviews_receipt_counts_the_rounds():
    detail = {"phase": "watch", "seeded": True, "seen": ["1"], "review_rounds": 2}
    f, _ = started(step("reviews", quiet_minutes=10), detail=detail)
    obs = {"session": sess(pr_url="https://x/pull/1"), "reviews": {"ids": ["1"]}}
    assert receipt(judge(f, obs, NOW + 601)) == "2 rounds, quiet"
    g, gs = started(step("reviews", quiet_minutes=10), detail=dict(detail))
    gs["detail"]["review_rounds"] = 1
    assert receipt(judge(g, obs, NOW + 601)) == "1 round, quiet"
    gs["detail"].pop("review_rounds")
    assert receipt(judge(g, obs, NOW + 601)) == "no rounds, quiet"


def test_command_receipt():
    f, _ = started(step("command", command="true"))
    a = judge(f, {"command": {"running": False, "exit": 0, "output": "ok"}}, NOW)
    assert receipt(a) == "exit 0" and a["detail"]["output"] == "ok"


def test_wait_receipt_is_the_wait():
    f, _ = started(step("wait", minutes=10), detail={"until": NOW + 600})
    assert receipt(judge(f, {}, NOW + 601)) == "10 min"
    g, _ = started(step("wait", until="18:00"), detail={"until": NOW + 5})
    assert receipt(judge(g, {}, NOW + 6)) == "until 18:00"


def test_join_receipt_counts_the_workers_done():
    f, _ = started(step("join", on_failed="continue"))
    kids = [
        {
            "title": t,
            "present": True,
            "activity": "idle",
            "report": {"id": t, "status": status, "ts": NOW + 60},
        }
        for t, status in (("a", "done"), ("b", "done"), ("c", "failed"))
    ]
    a = judge(f, {"children": kids, "session": sess()}, NOW + 600)
    assert receipt(a) == "2 of 3 done"
    g, _ = started(step("join"))
    assert receipt(judge(g, {"children": [], "session": sess()}, NOW)) == "no workers"


def test_verify_receipt_is_answered_of_all():
    v = {
        "plan": "tp1",
        "state": "done",
        "steps": ["a", "b", "c", "d"],
        "answered": 3,
        "settled": True,
        "open_agent": [],
        "open_human": [],
    }
    f, _ = started(step("verify", who="both"), detail={"plan": "tp1"})
    assert receipt(judge(f, {"verify": v, "session": sess()}, NOW)) == "3 of 4"
    # An older observation without the count: what is not open is answered.
    old = dict(v, settled=False, open_agent=["x"], open_human=[])
    old.pop("answered")
    g, _ = started(step("verify", who="agent"), detail={"plan": "tp1"})
    g["step_states"]["s1"]["detail"]["run_started"] = True
    old["settled"] = True
    assert receipt(judge(g, {"verify": old, "session": sess()}, NOW)) == "3 of 4"


def test_receipts_are_short():
    long_state = "x" * 200
    assert len(F.ticket_receipt({"state": long_state})) > F.MAX_RECEIPT
    a = F._receipt({"action": "done"}, "→ " + long_state)
    assert len(a["detail"]["receipt"]) == F.MAX_RECEIPT


def test_ticket_receipt_words():
    assert F.ticket_receipt({"state": "Done", "comment": "x"}) == "→ Done"
    assert F.ticket_receipt({"state": "", "comment": "Merged"}) == "commented"
    assert F.ticket_receipt({}) == "nothing to do"


# --------------------------------------------------------------------------- #
# The executors that are done at once (flight_driver._perform)
# --------------------------------------------------------------------------- #
@pytest.fixture
def srv(monkeypatch):
    inst = SimpleNamespace(
        Path="/repo",
        Branch="feat",
        Status=SimpleNamespace(name="Running"),
        GetWorktreePath=lambda: "/repo",
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: "/repo"),
    )
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": inst}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)

    def fake(flight, now, agent):
        return {
            "present": flight["session"] == "s",
            "pending": False,
            "status": "running",
            "sleeping": False,
            "activity": "idle",
            "branch": "feat",
            "folder": "/repo",
            "pr_url": "",
        }

    monkeypatch.setattr(D, "_session_obs", fake)
    return ns


def drive(f):
    asyncio.run(D.step_flight(f["id"]))
    return F.get(f["id"])


def test_notify_receipt(srv, monkeypatch):
    monkeypatch.setattr(D, "_run_notify", lambda f, s: None)
    got = drive(fly(step("notify", text="hi"), now=time.time()))
    assert got["step_states"]["s1"]["detail"]["receipt"] == "sent"


@pytest.mark.parametrize(
    "config,want",
    [
        ({"state": "Done", "comment": ""}, "→ Done"),
        ({"comment": "Merged"}, "commented"),
    ],
)
def test_ticket_receipt(srv, monkeypatch, config, want):
    class Prov:
        async def list_states(self):
            return [{"id": "500", "name": "Done"}]

        async def set_state(self, tid, state):
            pass

        async def add_comment(self, tid, body):
            pass

    import backend.ticket_ingestion.config as cfg_mod
    import backend.ticket_ingestion.providers as prov_mod

    monkeypatch.setattr(
        cfg_mod,
        "config_for_launch",
        lambda _: SimpleNamespace(source_for=lambda k: object()),
    )
    monkeypatch.setattr(prov_mod, "get_provider", lambda src: Prov())
    f = fly(
        step("ticket", **config),
        subject={"kind": "ticket", "source": "sc", "id": "42"},
        now=time.time(),
    )
    assert drive(f)["step_states"]["s1"]["detail"]["receipt"] == want


def test_split_receipt_counts_the_workers(srv, monkeypatch):
    # Every worker already exists (a restart mid-split): adopted, none spawned.
    monkeypatch.setattr(D, "_split_base", lambda f, s: ("head", "claude", "s-w"))
    monkeypatch.setattr(D, "_adopted_workers", lambda f, s, p: ["s-w1", "s-w2", "s-w3"])
    monkeypatch.setattr(D, "_footer_ok", lambda provider: False)
    monkeypatch.setattr(D, "_row", lambda title: {})
    got = drive(fly(step("split", prompts="a\nb\nc"), now=time.time()))
    d = got["step_states"]["s1"]["detail"]
    assert d["children"] == ["s-w1", "s-w2", "s-w3"] and d["receipt"] == "3 workers"


def test_verify_observation_counts_the_answered_items(monkeypatch):
    from backend.web.core import test_plans as _tp

    plan = {
        "branch": "feat",
        "state": "done",
        "steps": [
            {"id": "a", "actor": "agent", "text": "A"},
            {"id": "b", "actor": "human", "text": "B"},
            {"id": "c", "actor": "human", "text": "C"},
        ],
        "runs": [{"results": {"a": {"result": "pass"}, "b": {"result": "fail"}}}],
    }
    monkeypatch.setattr(_tp, "get", lambda pid: plan)
    monkeypatch.setattr(_tp, "is_settled", lambda p: False)
    monkeypatch.setattr(_tp, "open_human_steps", lambda p: [plan["steps"][2]])
    monkeypatch.setattr(_tp, "failed_steps", lambda p: [])
    flight = {"session": "s", "repo_root": "/repo", "branch": "feat"}
    obs = D._verify_obs(flight, {"detail": {"plan": "tp1"}}, {})
    assert obs["answered"] == 2 and obs["steps"] == ["a", "b", "c"]
