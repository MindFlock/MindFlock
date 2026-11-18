"""To-do auto sources (addons/todo/auto.py): Verify waiting on you, a route
parked on you and a team-run escalation each add an item — and resolve it
when the thing moved on. The plan / flight / run stores are faked behind the
module's reader seams (``auto._test_plans`` …); the To-do store is a tmp dir
(never the real ``~/.mindflock-assistant``)."""

from __future__ import annotations

import types

import pytest

from backend.web.addons import assistant as A
from backend.web.addons.todo import auto, store as S
from backend.web.core import test_plans as real_tp


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    d = tmp_path / "assistant"
    d.mkdir()
    monkeypatch.setattr(A, "ASSIST_DIR", d)
    monkeypatch.setattr(A, "ASSIST_TODOS", d / "todos.json")
    monkeypatch.setattr(A, "ASSIST_CLAUDE_MD", d / "CLAUDE.md")
    monkeypatch.setattr(A, "ASSIST_USER_MD", d / "user_instructions.md")
    return d


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def plans(monkeypatch):
    db: dict = {}
    fake = types.SimpleNamespace(
        get=lambda pid: db.get(pid),
        list_plans=lambda: list(db.values()),
        confirm_steps=real_tp.confirm_steps,
        run_progress=real_tp.run_progress,
    )
    monkeypatch.setattr(auto, "_test_plans", lambda: fake)
    return db


def _plan(pid="sess-a", outcome="needs_you", results=None, steps=None):
    return {
        "id": pid,
        "title": "Login fix",
        "steps": (
            steps
            if steps is not None
            else [
                {
                    "id": "s1",
                    "text": "Log in as admin",
                    "expect": "the dashboard",
                    "actor": "human",
                },
                {
                    "id": "s2",
                    "text": "Paste the invite",
                    "expect": "joined",
                    "actor": "human",
                    "paste": "/join abc",
                },
                {"id": "s3", "text": "Agent step", "expect": "", "actor": "agent"},
            ]
        ),
        "runs": [
            {
                "outcome": outcome,
                "results": (
                    results if results is not None else {"s3": {"result": "pass"}}
                ),
            }
        ],
    }


def _checked(pid="sess-a", outcome="needs_you"):
    return {
        "event": "session.test_plan_checked",
        "session": pid,
        "new": "needs_you",
        "data": {"plan": pid, "outcome": outcome},
    }


@pytest.fixture()
def flights(monkeypatch):
    db: dict = {}

    def waiting_step(f):
        return f["steps"][f["cursor"]] if f["cursor"] < len(f["steps"]) else None

    def you_text(f):
        cur = waiting_step(f)
        return cur["config"].get("text") or cur["title"], list(
            cur["config"].get("items") or []
        )

    fake = types.SimpleNamespace(
        get=lambda fid: db.get(fid),
        list_flights=lambda include_finished=True: list(db.values()),
        waiting_step=waiting_step,
        you_text=you_text,
    )
    monkeypatch.setattr(auto, "_flights", lambda: fake)
    return db


def _flight(fid="f1", state="waiting_you", kind="you", ask=None, since=100.0):
    detail = {"ask": ask} if ask else {}
    return {
        "id": fid,
        "plan_name": "Ship it",
        "session": "sess-b",
        "state": state,
        "cursor": 0,
        "steps": [
            {
                "id": "st1",
                "kind": kind,
                "title": "Approve",
                "config": {"text": "Approve the deploy", "items": ["Look at staging"]},
            }
        ],
        "step_states": {"st1": {"state": state, "since": since, "detail": detail}},
    }


@pytest.fixture()
def runs(monkeypatch):
    from backend.web.core import team_runs as real_tr

    db: dict = {}
    fake = types.SimpleNamespace(
        load=lambda rid: db.get(rid),
        list_runs=lambda include_finished=True: list(db.values()),
        RUN_FINISHED=real_tr.RUN_FINISHED,
        ANNOUNCED_REASONS=real_tr.ANNOUNCED_REASONS,
        ref_of=lambda t: t.get("ticket_id") or t.get("title"),
        lead_gone=lambda run, now: run.get("_lead_gone", ""),
    )
    monkeypatch.setattr(auto, "_team_runs", lambda: fake)
    return db


def _open():
    return [i for i in S.list_items() if not i["done"]]


# --------------------------------------------------------------------------- #
# 1. Verify
# --------------------------------------------------------------------------- #
def test_verify_needs_you_adds_one_item_per_step_then_resolves(plans):
    plans["sess-a"] = _plan()
    auto.handle(_checked())
    items = _open()
    assert [i["key"] for i in items] == ["verify:sess-a:s1", "verify:sess-a:s2"]
    s1, s2 = items
    assert s1["text"] == "Log in as admin"
    assert s1["kind"] == "manual_test" and s1["source"] == "verify"
    assert s1["session"] == "sess-a"
    assert "Expect: the dashboard" in s1["details"]
    assert "Confirm verified on sess-a" in s1["details"]
    assert "Paste:\n/join abc" in s2["details"]  # paste-ready text

    # Same event again: no doubles.
    auto.handle(_checked())
    assert len(_open()) == 2

    # The person answers s1 in Verify: its item closes, s2 stays.
    plans["sess-a"] = _plan(
        results={"s3": {"result": "pass"}, "s1": {"result": "pass"}}
    )
    auto.resolve()
    assert [i["key"] for i in _open()] == ["verify:sess-a:s2"]

    # Confirmed: the outcome moved on and everything closes.
    plans["sess-a"] = _plan(outcome="passed")
    auto.handle(
        {
            "event": "session.test_plan_settled",
            "session": "sess-a",
            "data": {"plan": "sess-a"},
        }
    )
    assert _open() == []


def test_verify_without_open_steps_asks_for_confirm(plans):
    plans["sess-a"] = _plan(
        results={
            "s1": {"result": "pass"},
            "s2": {"result": "pass"},
            "s3": {"result": "pass"},
        }
    )
    auto.handle(_checked())
    (item,) = _open()
    assert item["text"] == "Confirm verified: Login fix"
    assert item["key"] == "verify:sess-a:confirm"


def test_verify_settled_by_hand_then_asks_for_the_confirm(plans):
    plans["sess-a"] = _plan()
    auto.handle(_checked())
    plans["sess-a"] = _plan(
        results={
            "s1": {"result": "pass"},
            "s2": {"result": "pass"},
            "s3": {"result": "pass"},
        }
    )
    auto.handle(
        {
            "event": "session.test_plan_settled",
            "session": "sess-a",
            "data": {"plan": "sess-a"},
        }
    )
    assert [i["key"] for i in _open()] == ["verify:sess-a:confirm"]


def test_a_passed_verify_adds_nothing(plans):
    plans["sess-a"] = _plan(outcome="passed")
    auto.handle(_checked(outcome="passed"))
    assert _open() == []


def test_ticking_a_verify_item_off_does_not_touch_the_plan(plans):
    plans["sess-a"] = _plan()
    auto.handle(_checked())
    before = repr(plans["sess-a"])
    S.patch_item(_open()[0]["id"], {"done": True})
    assert repr(plans["sess-a"]) == before


def test_a_gone_plan_closes_only_when_the_store_reads(plans):
    plans["sess-a"] = _plan()
    auto.handle(_checked())
    # Store reads EMPTY (missing / unreadable): nothing is closed.
    plans.clear()
    auto.resolve()
    assert len(_open()) == 2
    # Store reads, the plan is not in it: gone, closed.
    plans["other"] = _plan(pid="other", outcome="passed")
    auto.resolve()
    assert _open() == []


def test_an_unreadable_store_closes_nothing(plans, monkeypatch):
    plans["sess-a"] = _plan()
    auto.handle(_checked())

    def boom():
        raise OSError("disk gone")

    fake = auto._test_plans()
    monkeypatch.setattr(fake, "list_plans", boom)
    assert auto.resolve() == []
    assert len(_open()) == 2


def test_resolve_never_closes_a_non_auto_item(plans):
    S.add_items([S.clean_new({"text": "mine", "key": "verify:gone:s1"})])
    S.add_items(
        [S.clean_new({"text": "agent's", "source": "agent", "key": "verify:gone:s2"})]
    )
    plans["other"] = _plan(pid="other")
    auto.resolve()
    assert len(_open()) == 2


# --------------------------------------------------------------------------- #
# 2. Flights
# --------------------------------------------------------------------------- #
def _needs_you(fid="f1", text="", items=None):
    return {
        "event": "flight.needs_you",
        "session": "sess-b",
        "data": {"id": fid, "text": text, "items": items or []},
    }


def test_flight_parked_on_you_adds_then_resolves(flights):
    flights["f1"] = _flight()
    auto.handle(_needs_you(text="Approve the deploy", items=["Look at staging"]))
    (item,) = _open()
    assert item["key"] == "flight:f1:st1:100"
    assert item["kind"] == "needs_you" and item["source"] == "flight"
    assert item["session"] == "sess-b"
    assert item["text"] == "Approve the deploy"
    assert "- Look at staging" in item["details"]
    # It moves on.
    flights["f1"] = _flight(state="running")
    auto.handle({"event": "flight.changed", "session": "sess-b", "data": {"id": "f1"}})
    assert _open() == []


def test_a_restarted_stop_is_a_new_item(flights):
    flights["f1"] = _flight(since=100.0)
    auto.handle(_needs_you())
    flights["f1"] = _flight(since=200.0)
    auto.handle(_needs_you())
    (item,) = _open()
    assert item["key"] == "flight:f1:st1:200"


def test_no_double_for_a_flight_parked_on_a_verify_stop(flights, plans):
    plans["sess-b"] = _plan(pid="sess-b")
    flights["f1"] = _flight(kind="verify")
    auto.handle(_checked(pid="sess-b"))
    auto.handle(_needs_you())
    assert {i["source"] for i in _open()} == {"verify"}
    # …but a Verify stop that could not run IS the flight's to report.
    flights["f2"] = _flight(fid="f2", kind="verify", ask="blocked")
    auto.handle(_needs_you(fid="f2"))
    assert {i["source"] for i in _open()} == {"verify", "flight"}


def test_a_finished_flight_closes_its_item(flights):
    flights["f1"] = _flight()
    auto.handle(_needs_you())
    flights["f1"] = _flight(state="done")
    auto.handle({"event": "flight.finished", "session": "sess-b", "data": {"id": "f1"}})
    assert _open() == []


# --------------------------------------------------------------------------- #
# 3. Team runs
# --------------------------------------------------------------------------- #
def _run(rid="r_abcd", state="running", task_state="needs_you", reason="stuck", **kw):
    run = {
        "id": rid,
        "name": "Batch",
        "state": state,
        "paused": False,
        "pause_reason": "",
        "tasks": [{"id": "t1", "state": task_state, "reason": reason}],
    }
    run.update(kw)
    return run


def _run_needs_you(task="t1", reason="stuck"):
    return {
        "event": "run.needs_you",
        "session": "worker-1",
        "data": {
            "run": "r_abcd",
            "name": "Batch",
            "task": task,
            "title": "worker-1",
            "ref": "SC-12",
            "reason": reason,
            "text": "stuck after two nudges",
            "detail": "Batch: SC-12 stuck after two nudges",
        },
    }


def test_run_escalation_adds_then_resolves(runs):
    runs["r_abcd"] = _run()
    auto.handle(_run_needs_you())
    (item,) = _open()
    assert item["key"] == "run:r_abcd:t1:stuck"
    assert item["kind"] == "needs_you" and item["source"] == "run"
    assert item["session"] == "worker-1"
    assert item["text"] == "SC-12: stuck after two nudges"
    runs["r_abcd"] = _run(task_state="working", reason="")
    auto.handle({"event": "run.changed", "session": "", "data": {"run": "r_abcd"}})
    assert _open() == []


def test_a_budget_pause_resolves_when_the_run_resumes(runs):
    runs["r_abcd"] = _run(paused=True, pause_reason="budget")
    auto.handle(_run_needs_you(task="", reason="budget"))
    assert len(_open()) == 1
    auto.resolve()
    assert len(_open()) == 1
    runs["r_abcd"] = _run(paused=False)
    auto.resolve()
    assert _open() == []


def test_a_finished_run_closes_everything(runs):
    runs["r_abcd"] = _run()
    auto.handle(_run_needs_you())
    runs["r_abcd"] = _run(state="done")
    auto.handle({"event": "run.finished", "session": "", "data": {"run": "r_abcd"}})
    assert _open() == []


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #
def test_the_bus_callback_only_queues():
    src = auto.AutoSources()
    src.on_event({"event": "flight.needs_you", "data": {}})
    src.on_event({"event": "session.created", "data": {}})
    src.on_event("not even a dict")  # never raises
    assert src._q.qsize() == 1


def test_a_failing_handler_does_not_kill_the_worker(monkeypatch):
    import threading

    calls = []
    done = threading.Event()

    def handle(env):
        calls.append(env["event"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        done.set()

    monkeypatch.setattr(auto, "handle", handle)
    monkeypatch.setattr(auto, "FIRST_RECONCILE_S", 3600.0)
    src = auto.AutoSources()
    subs = []
    src.start(lambda name, cb: subs.append(cb) or (lambda: None))
    subs[0]({"event": "run.changed", "data": {}})
    subs[0]({"event": "run.changed", "data": {}})
    assert done.wait(5)
    src.stop()
    assert calls == ["run.changed", "run.changed"]


# --------------------------------------------------------------------------- #
# The reconcile pass also ADDS what is missing
# --------------------------------------------------------------------------- #
@pytest.fixture()
def nothing(plans, flights, runs):
    """All three stores faked (and empty unless a test fills them)."""
    return plans, flights, runs


def test_a_quiet_you_stop_gets_its_item_on_the_pass(nothing):
    _plans, flights, _runs = nothing
    # A "you" stop with notify: false never emits flight.needs_you.
    flights["f1"] = _flight()
    flights["f1"]["steps"][0]["config"]["notify"] = False
    auto.reconcile()
    (item,) = _open()
    assert item["key"] == "flight:f1:st1:100" and item["source"] == "flight"
    # A second pass is a no-op (no update, no event churn).
    before = S.list_items()
    auto.reconcile()
    assert S.list_items() == before


def test_after_a_restart_the_pass_adds_every_open_ask(nothing):
    plans, flights, runs = nothing
    plans["sess-a"] = _plan()
    flights["f1"] = _flight()
    runs["r_abcd"] = _run(
        tasks=[
            {
                "id": "t1",
                "state": "needs_you",
                "reason": "stuck",
                "title": "w1",
                "detail": "stuck after two nudges",
                "kind": "task",
                "incarnation": 5.0,
            },
            {"id": "t2", "state": "needs_you", "reason": "prompt", "title": "w2"},
        ],
        paused=True,
        pause_reason="budget",
    )
    added = auto.ensure()
    assert sorted(added) == sorted(
        [
            "verify:sess-a:s1",
            "verify:sess-a:s2",
            "flight:f1:st1:100",
            "run:r_abcd:t1:stuck",
            "run:r_abcd::budget",
        ]
    )  # a dialog ("prompt") is not a run escalation: needs_input covers it
    run_item = next(i for i in _open() if i["key"] == "run:r_abcd:t1:stuck")
    assert run_item["session"] == "w1" and "stuck after two nudges" in run_item["text"]
    # The event path then finds them there: no doubles.
    auto.handle(_checked())
    auto.handle(_needs_you())
    assert len(_open()) == 5


def test_the_pass_never_brings_back_what_a_person_closed(nothing):
    plans, flights, _runs = nothing
    plans["sess-a"] = _plan()
    flights["f1"] = _flight()
    auto.reconcile()
    by_key = {i["key"]: i for i in _open()}
    # Ticked off by hand, and deleted.
    S.patch_item(by_key["verify:sess-a:s1"]["id"], {"done": True})
    S.delete_item(by_key["flight:f1:st1:100"]["id"])
    auto.reconcile()
    assert [i["key"] for i in _open()] == ["verify:sess-a:s2"]


def test_a_new_round_of_the_same_ask_comes_back(nothing):
    plans, _flights, _runs = nothing
    plans["sess-a"] = _plan()
    plans["sess-a"]["runs"][-1]["at"] = 1000.0
    auto.reconcile()
    s1 = next(i for i in _open() if i["key"] == "verify:sess-a:s1")
    S.patch_item(s1["id"], {"done": True})
    auto.reconcile()
    assert "verify:sess-a:s1" not in {i["key"] for i in _open()}
    # A re-run started after that close asks again.
    plans["sess-a"]["runs"][-1]["at"] = __import__("time").time() + 60
    auto.reconcile()
    assert "verify:sess-a:s1" in {i["key"] for i in _open()}


def test_a_server_close_does_not_block_a_later_add(nothing):
    plans, _flights, _runs = nothing
    plans["sess-a"] = _plan()
    auto.reconcile()
    plans["sess-a"] = _plan(outcome="passed")
    auto.reconcile()
    assert _open() == []
    assert all(i.get("auto_closed") for i in S.list_items())
    plans["sess-a"] = _plan()  # same keys, waiting on the person again
    auto.reconcile()
    assert len(_open()) == 2
    assert not any(i.get("auto_closed") for i in _open())


def test_the_pass_adds_no_double_at_a_verify_stop(nothing):
    plans, flights, _runs = nothing
    plans["sess-b"] = _plan(pid="sess-b")
    flights["f1"] = _flight(kind="verify")
    auto.reconcile()
    assert {i["source"] for i in _open()} == {"verify"}
    assert len(_open()) == 2


def test_an_unreadable_store_skips_only_its_own_asks(nothing, monkeypatch):
    plans, flights, _runs = nothing
    plans["sess-a"] = _plan()
    flights["f1"] = _flight()

    def boom(**_kw):
        raise OSError("disk gone")

    monkeypatch.setattr(auto._flights(), "list_flights", boom)
    auto.ensure()
    assert {i["source"] for i in _open()} == {"verify"}
