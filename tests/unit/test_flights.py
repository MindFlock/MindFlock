"""Flights: the stores, the step registry, the built-ins, the normaliser and
the pure planner (``backend.web.core.flights``). No I/O beyond a tmp store."""

import copy
import json

import pytest

from backend.web.core import flights as F

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    yield tmp_path / "flights"


def plan_of(*steps, name="P"):
    return F.save_plan({"name": name, "steps": list(steps)}, now=NOW)


def step(kind, sid=None, on_fail="halt", **config):
    out = {"kind": kind, "config": config, "on_fail": on_fail}
    if sid:
        out["id"] = sid
    return out


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def fly(*steps, session="s", subject=None, **kw):
    p = plan_of(*steps)
    return F.take_off(p, session=session, subject=subject, now=NOW, **kw)


def ap(f, lane, **kw):
    """An autopilot record THIS flight armed for ``lane``."""
    rec = {"source": "flight", "item": f["id"], "lane": lane, "depth": lane}
    rec.update(kw)
    return rec


def run_step(f, now=NOW):
    """Apply a ``run`` for the current pending step (what the driver does)."""
    a = F.next_action(f, {"session": sess()}, now)
    assert a["action"] == "run", a
    return F.apply(f, a, now)


# --------------------------------------------------------------------------- #
# Registry + normaliser
# --------------------------------------------------------------------------- #
def test_kinds_registry_drives_the_editor():
    dto = F.kinds_dto()
    kinds = [k["kind"] for k in dto["kinds"]]
    assert kinds == [
        "agent",
        "ship",
        "verify",
        "you",
        "wait",
        "reviews",
        "checks",
        "command",
        "ticket",
        "notify",
    ]
    for k in dto["kinds"]:
        assert k["actor"] in ("robot", "you")
        assert k["default_title"] and k["label"] and k["summary"]
        for fld in k["fields"]:
            assert fld["type"] in ("text", "textarea", "int", "bool", "select")
            if fld["type"] == "select":
                assert fld["options"]
    assert dto["on_fail"] == ["halt", "agent", "skip", "you"]


@pytest.mark.parametrize(
    "raw,field",
    [
        ({"kind": "teleport"}, "steps[0].kind"),
        ({"kind": "ship", "config": {"lane": "deploy"}}, "steps[0].config.lane"),
        ({"kind": "command", "config": {}}, "steps[0].config.command"),
        ({"kind": "wait", "config": {"until": "25:99"}}, "steps[0].config.until"),
        ({"kind": "verify", "config": {"who": "robot"}}, "steps[0].config.who"),
        ({"kind": "agent", "on_fail": "panic"}, "steps[0].on_fail"),
        ("nope", "steps[0]"),
    ],
)
def test_strict_normaliser_names_the_bad_field(raw, field):
    with pytest.raises(F.PlanError) as err:
        F.normalize_plan({"name": "x", "steps": [raw]})
    assert err.value.field == field


def test_plan_needs_steps_name_and_at_most_forty():
    with pytest.raises(F.PlanError) as e1:
        F.normalize_plan({"name": "x", "steps": []})
    assert e1.value.field == "steps"
    with pytest.raises(F.PlanError) as e2:
        F.normalize_plan({"steps": [step("agent")]})
    assert e2.value.field == "name"
    with pytest.raises(F.PlanError):
        F.normalize_plan({"name": "x", "steps": [step("agent")] * 41})
    with pytest.raises(F.PlanError) as e3:
        F.normalize_plan(
            {"name": "x", "trigger": {"kind": "cron"}, "steps": [step("agent")]}
        )
    assert e3.value.field == "trigger.kind"


def test_steps_get_ids_titles_and_defaults():
    p = F.normalize_plan(
        {
            "name": "x",
            "steps": [
                step("ship", sid="a", lane="merge"),
                step("ship", sid="a"),  # duplicate id: renumbered
                step("wait", minutes=5),
                step("wait", until="18:30"),
                step("you", items="one\n\ntwo", on_fail="agent"),
                step("reviews"),
            ],
        }
    )
    ids = [s["id"] for s in p["steps"]]
    assert len(set(ids)) == 6 and ids[0] == "a"
    assert [s["title"] for s in p["steps"][:4]] == [
        "Merge",
        "Push",
        "Wait 5 min",
        "Wait until 18:30",
    ]
    assert p["steps"][1]["config"]["lane"] == "push"
    assert p["steps"][3]["config"] == {"minutes": 0, "until": "18:30"}
    assert p["steps"][4]["config"]["items"] == ["one", "two"]
    assert p["steps"][4]["on_fail"] == "halt"  # a you step cannot fail
    assert p["steps"][5]["config"] == {
        "quiet_minutes": 10,
        "max_rounds": 5,
        "include_bots": True,
    }


def test_lenient_normaliser_drops_junk_steps():
    steps = F.normalize_steps(
        [step("teleport"), "x", step("notify", text="hi")], strict=False
    )
    assert [s["kind"] for s in steps] == ["notify"]


# --------------------------------------------------------------------------- #
# Plans store + built-ins
# --------------------------------------------------------------------------- #
def test_builtins_are_seeded_first_and_shaped_as_documented():
    plans = F.list_plans()
    assert [p["id"] for p in plans[:4]] == list(F.BUILTIN_IDS)
    t = F.get_plan("builtin:ticket")
    assert (
        t["name"] == "Ticket to merge"
        and t["builtin"]
        and t["trigger"]["kind"] == "ticket"
    )
    assert [s["kind"] for s in t["steps"]] == [
        "agent",
        "ship",
        "ship",
        "verify",
        "ship",
        "reviews",
        "checks",
        "you",
        "ship",
        "ticket",
    ]
    assert [s["config"].get("lane") for s in t["steps"] if s["kind"] == "ship"] == [
        "commit",
        "push",
        "pr",
        "merge",
    ]
    assert t["steps"][-1]["config"]["comment"] == "Merged by MindFlock"
    v = F.get_plan("builtin:verify")
    assert v["steps"][0]["config"] == {"who": "both", "wait_live": True}


def test_save_edit_delete_and_reset(tmp_path):
    p = plan_of(step("notify", text="x"), name="Mine")
    assert p["id"].startswith("p_") and not p["builtin"] and p["updated"] == NOW
    p2 = F.save_plan(dict(p, name="Renamed"))
    assert p2["id"] == p["id"] and F.get_plan(p["id"])["name"] == "Renamed"
    assert [x["name"] for x in F.list_plans()][4:] == ["Renamed"]
    with pytest.raises(F.FlightError) as err:
        F.save_plan(dict(p, id="p_nope"))
    assert err.value.status == 404
    # Built-ins are editable, and deleting one resets it.
    t = F.get_plan("builtin:ticket")
    F.save_plan(dict(t, name="My ticket flow", steps=t["steps"][:2]))
    assert F.get_plan("builtin:ticket")["name"] == "My ticket flow"
    assert F.delete_plan("builtin:ticket") == "reset"
    assert F.get_plan("builtin:ticket")["name"] == "Ticket to merge"
    F.save_plan(dict(t, name="Again"))
    assert F.reset_plan("builtin:ticket")["name"] == "Ticket to merge"
    assert F.reset_plan(p["id"]) is None
    assert F.delete_plan(p["id"]) == "deleted"
    assert F.get_plan(p["id"]) is None
    assert F.delete_plan(p["id"]) == ""


def test_strict_read_for_sync_raises_on_a_corrupt_file(_store):
    plan_of(step("notify"), name="A")
    assert set(F.read_plans_strict()) >= set(F.BUILTIN_IDS)
    (_store / "plans.json").write_text("{nope")
    with pytest.raises(ValueError):
        F.read_plans_strict()


def test_put_plan_is_the_sync_write():
    F.put_plan(
        "p_remote",
        {"name": "From mac", "steps": [step("wait", minutes=3)], "updated": 5.0},
    )
    got = F.get_plan("p_remote")
    assert got["name"] == "From mac" and got["updated"] == 5.0
    with pytest.raises(ValueError):
        F.put_plan("p_bad", {"name": "x", "steps": [step("teleport")]})


@pytest.mark.parametrize(
    "kind,depth,want",
    [
        ("ticket", "commit", "builtin:ticket@commit"),
        ("ticket", "push", "builtin:ticket@push"),
        ("ticket", "pr", "builtin:ticket@pr"),
        ("ticket", "merge", "builtin:ticket"),
        ("issue", "pr", "builtin:ticket@pr"),
        ("pr", "push", "builtin:pr@push"),
        ("pr", "pr", "builtin:pr"),
        ("schedule", "pr", "builtin:schedule"),
        ("ticket", "off", ""),
        ("ticket", "agent", ""),
        ("nope", "pr", ""),
    ],
)
def test_plan_for_depth(kind, depth, want):
    assert F.plan_for_depth(kind, depth) == want


def test_depth_cut_is_a_virtual_plan():
    cut = F.get_plan("builtin:ticket@pr")
    assert [s["title"] for s in cut["steps"]] == [
        "Agent works the ticket",
        "Commit",
        "Push",
        "Verify",
        "Open PR",
    ]
    assert cut["id"] == "builtin:ticket@pr" and "Open PR" in cut["name"]
    assert F.get_plan("builtin:nope@pr") is None
    with pytest.raises(F.PlanError):
        F.save_plan(dict(cut))


# --------------------------------------------------------------------------- #
# Take-off
# --------------------------------------------------------------------------- #
def test_attach_starts_at_the_first_non_agent_step():
    f = fly(step("agent"), step("ship", lane="commit"), step("notify"), attach=True)
    assert f["cursor"] == 1 and f["state"] == "running"
    assert f["step_states"]["s1"]["state"] == "skipped"
    assert f["history"][-1]["event"] == "jumped"


def test_start_at_and_unknown_step():
    f = fly(step("agent"), step("notify"), step("wait"), start_at="s3")
    assert f["cursor"] == 2
    with pytest.raises(F.FlightError) as err:
        fly(step("agent"), session="t", start_at="zz")
    assert err.value.error == "unknown_step"


def test_seeded_first_agent_step_is_already_running():
    f = fly(step("agent", prompt="go"), step("notify"), seeded=True)
    assert f["cursor"] == 0
    ss = f["step_states"]["s1"]
    assert ss["state"] == "running" and ss["detail"] == {"seeded": True}
    a = F.next_action(f, {"session": sess(activity="working")}, NOW + 1)
    assert a["action"] == "wait"  # never "run" — the prompt is already sent


def test_one_flight_per_session_and_halted_ones_are_replaced():
    f1 = fly(step("notify"), session="t")
    with pytest.raises(F.FlightError) as err:
        fly(step("notify"), session="t")
    assert err.value.status == 409 and err.value.error == "in_flight"
    F.mutate(
        f1["id"],
        lambda f: F.apply(f, {"action": "fail", "reason": "x", "then": "halt"}, NOW),
    )
    assert F.get(f1["id"])["state"] == "halted"
    f2 = fly(step("notify"), session="t")
    assert F.get(f1["id"])["state"] == "cancelled"
    assert F.for_session("t")["id"] == f2["id"]


def test_arm_pending_before_the_session_exists():
    f = F.arm_pending(
        "sc-12-fix",
        "builtin:ticket",
        {"kind": "ticket", "source": "sc", "id": "12", "title": "Fix"},
    )
    assert (
        f["session"] == "sc-12-fix" and f["state"] == "running" and f["by"] == "intake"
    )
    assert f["step_states"]["s1"]["state"] == "running"
    assert f["subject"]["kind"] == "ticket" and f["subject"]["id"] == "12"
    with pytest.raises(F.FlightError):
        F.arm_pending("x", "nope", {})


def test_fill_prompt_placeholders():
    f = {
        "subject": {"kind": "ticket", "id": "sc-9", "title": "Fix login"},
        "branch": "b1",
        "plan_name": "P",
    }
    assert (
        F.fill_prompt("{ticket}|{pr}|{branch}|{subject}|{other}", f)
        == "sc-9||b1|Fix login|{other}"
    )
    pr = {
        "subject": {"kind": "pr", "id": "7", "url": "https://x/7"},
        "branch": "",
        "plan_name": "P",
    }
    assert F.fill_prompt("{pr} {subject}", pr) == "https://x/7 7"


# --------------------------------------------------------------------------- #
# The planner: kinds
# --------------------------------------------------------------------------- #
def test_session_holds_are_visible_waits():
    f = fly(step("ship", lane="push"))
    for s, note in [
        ({"present": False}, "waiting for s to start"),
        ({"present": False, "pending": True}, "the session is starting"),
        (sess(status="paused"), "the session is paused"),
    ]:
        a = F.next_action(f, {"session": s}, NOW)
        assert a == {"action": "wait", "note": note}
    # A sleeping session is not a hold (the driver wakes it).
    assert F.next_action(f, {"session": sess(sleeping=True)}, NOW)["action"] == "run"
    # A step that needs no session runs without one.
    g = fly(step("wait", minutes=1), session="")
    assert F.next_action(g, {"session": {"present": False}}, NOW)["action"] == "run"


def test_agent_step_settles_like_autopilot():
    f = run_step(fly(step("agent", prompt="do it")))
    t = NOW
    a = F.next_action(f, {"session": sess(activity="working")}, t + 1)
    assert a["action"] == "wait" and a["detail"]["worked_at"] == t + 1
    f = F.apply(f, a, t + 1)
    a = F.next_action(f, {"session": sess()}, t + 2)
    assert a["note"] == "the agent just went idle"
    f = F.apply(f, a, t + 2)
    for s, note in [
        (sess(queue_pending=True), "the prompt queue still has work"),
        (
            sess(background_waiting=True),
            "a background job the agent started is still waiting",
        ),
        (sess(limited=True), "usage limit — waiting for the window to reopen"),
    ]:
        assert F.next_action(f, {"session": s}, t + 40)["note"] == note
    assert (
        F.next_action(f, {"session": sess()}, t + 10)["note"]
        == "confirming the agent is done"
    )
    assert F.next_action(f, {"session": sess()}, t + 33)["action"] == "done"


def test_agent_step_needs_work_evidence_then_gives_up():
    f = run_step(fly(step("agent", prompt="x")))
    f = F.apply(f, F.next_action(f, {"session": sess()}, NOW + 1), NOW + 1)
    a = F.next_action(f, {"session": sess()}, NOW + 40)
    assert a["note"] == "waiting for the agent to start"
    # A turn that ended after the step began is evidence too.
    a = F.next_action(f, {"session": sess(turn_ended_at=NOW + 20)}, NOW + 40)
    assert a["action"] == "done"
    a = F.next_action(f, {"session": sess()}, NOW + F.AGENT_START_DEADLINE_S + 5)
    assert a["action"] == "fail" and a["then"] == "halt"


def test_agent_step_with_no_prompt_only_waits_for_settle():
    f = fly(step("agent"))
    f = F.apply(f, {"action": "run", "detail": {"no_prompt": True}}, NOW)
    f = F.apply(f, F.next_action(f, {"session": sess()}, NOW + 1), NOW + 1)
    assert F.next_action(f, {"session": sess()}, NOW + 40)["action"] == "done"


def test_ship_step_follows_the_autopilot_record():
    f = run_step(fly(step("ship", lane="push")))
    assert (
        F.next_action(f, {"session": sess(), "autopilot": None}, NOW)["action"]
        == "fail"
    )
    running = ap(f, "push", state="running", step="push", note="pushing")
    assert (
        F.next_action(f, {"session": sess(), "autopilot": running}, NOW)["note"]
        == "pushing"
    )
    held = ap(f, "push", state="done", depth="commit", ask_first=True)
    assert (
        "your go"
        in F.next_action(f, {"session": sess(), "autopilot": held}, NOW)["note"]
    )
    halted = ap(f, "push", state="halted", reason="hooks failed")
    a = F.next_action(f, {"session": sess(), "autopilot": halted}, NOW)
    assert a["action"] == "fail" and a["reason"] == "hooks failed"
    done = ap(f, "push", state="done", url="")
    a = F.next_action(f, {"session": sess(), "autopilot": done}, NOW)
    assert a["action"] == "done"
    f = F.apply(f, a, NOW)
    assert f["state"] == "done" and f["finished"] == NOW


def _vfacts(**kw):
    base = {
        "plan": "s",
        "branch": "b",
        "state": "generated",
        "steps": ["s1"],
        "settled": False,
        "open_human": [],
        "open_agent": [],
        "failed": [],
        "live": False,
        "live_branch": "main",
    }
    base.update(kw)
    return base


def test_verify_step_writes_runs_parks_and_settles():
    f = run_step(fly(step("verify", who="both")))
    assert (
        F.next_action(f, {"session": sess(), "verify": {}}, NOW)["note"]
        == "writing the checklist"
    )
    assert (
        F.next_action(f, {"session": sess(), "verify": {}}, NOW + 4000)["action"]
        == "fail"
    )
    gen = _vfacts(state="generating")
    assert (
        F.next_action(f, {"session": sess(), "verify": gen}, NOW)["note"]
        == "writing the checklist"
    )
    v = _vfacts(open_agent=["curl it"], open_human=["look at it"])
    a = F.next_action(f, {"session": sess(), "verify": v}, NOW)
    assert a["action"] == "run" and a["phase"] == "agent_run"
    f = F.apply(f, dict(a, detail=dict(a["detail"], run_started=True)), NOW)
    assert f["step_states"]["s1"]["state"] == "running"
    a = F.next_action(f, {"session": sess(), "verify": _vfacts(state="running")}, NOW)
    assert a["note"] == "the agent is working through the checklist"
    a = F.next_action(
        f, {"session": sess(), "verify": _vfacts(open_human=["look at it"])}, NOW
    )
    assert a["action"] == "you" and a["detail"]["items"] == ["look at it"]
    f = F.apply(f, a, NOW)
    assert (
        f["state"] == "waiting_you" and f["step_states"]["s1"]["state"] == "waiting_you"
    )
    # Still open: no second "you" (it would re-announce).
    a = F.next_action(
        f, {"session": sess(), "verify": _vfacts(open_human=["look at it"])}, NOW
    )
    assert a["action"] == "wait"
    # Settled in the Checklists tab: the flight carries on by itself.
    a = F.next_action(f, {"session": sess(), "verify": _vfacts(settled=True)}, NOW)
    assert a["action"] == "done"
    assert F.apply(f, a, NOW)["state"] == "done"


def test_verify_agent_only_and_failures_and_wait_live():
    f = run_step(fly(step("verify", who="agent", wait_live=True)))
    v = _vfacts(open_human=["you look"])
    assert (
        "reach main" in F.next_action(f, {"session": sess(), "verify": v}, NOW)["note"]
    )
    v = _vfacts(open_human=["you look"], live=True)
    assert F.next_action(f, {"session": sess(), "verify": v}, NOW)["action"] == "done"
    v = _vfacts(failed=["login works"], live=True)
    a = F.next_action(f, {"session": sess(), "verify": v}, NOW)
    assert a["action"] == "fail" and "login works" in a["reason"]
    v = _vfacts(state="failed", steps=[], error="model down", live=True)
    assert (
        "model down"
        in F.next_action(f, {"session": sess(), "verify": v}, NOW)["reason"]
    )


def test_verify_who_you_never_starts_the_agent():
    f = run_step(fly(step("verify", who="you")))
    a = F.next_action(
        f, {"session": sess(), "verify": _vfacts(open_agent=["curl"])}, NOW
    )
    assert a["action"] == "you" and a["detail"]["items"] == ["curl"]


def test_you_step_parks_and_done_advances():
    f = fly(step("you", text="Approve it", items=["a", "b"]), step("notify"))
    a = F.next_action(f, {"session": {"present": False}}, NOW)
    assert a["action"] == "you" and a["note"] == "Approve it"
    f = F.apply(f, a, NOW)
    assert f["state"] == "waiting_you"
    assert F.you_text(f) == ("Approve it", ["a", "b"])
    with pytest.raises(F.FlightError) as err:
        F.op_done(copy.deepcopy(f), "s2", now=NOW)
    assert err.value.error == "not_waiting"
    f = F.op_done(f, "s1", "lgtm", now=NOW + 5)
    assert f["cursor"] == 1 and f["state"] == "running"
    assert f["step_states"]["s1"]["state"] == "done"


def test_wait_step_minutes_and_until():
    f = fly(step("wait", minutes=5))
    a = F.next_action(f, {}, NOW)
    assert a["action"] == "run"
    f = F.apply(f, dict(a, detail={"until": NOW + 300}), NOW)
    assert F.next_action(f, {}, NOW + 100)["note"] == "3m left"
    assert F.next_action(f, {}, NOW + 300)["action"] == "done"
    until = F.next_until("00:00", NOW)
    assert NOW < until <= NOW + 86400


def test_reviews_step_rounds():
    f = run_step(fly(step("reviews", quiet_minutes=10, max_rounds=1)))
    f = F.apply(f, {"action": "run", "phase": "x", "detail": {"phase": "watch"}}, NOW)
    o = {"session": sess(pr_url="https://pr/1")}
    assert (
        F.next_action(f, {"session": sess()}, NOW)["note"]
        == "waiting for the pull request"
    )
    assert F.next_action(f, {"session": sess()}, NOW + 2000)["action"] == "fail"
    # The comments already there when the step began are seeded, not sent.
    a = F.next_action(f, dict(o, reviews={"ids": ["1"]}), NOW + 1)
    assert a["action"] == "wait" and a["detail"] == {"seen": ["1"], "seeded": True}
    f = F.apply(f, a, NOW + 1)
    a = F.next_action(f, dict(o, reviews={"ids": ["1", "2"]}), NOW + 60)
    assert a["action"] == "run" and a["phase"] == "send" and a["comments"] == ["2"]
    f = F.apply(
        f,
        dict(
            a,
            detail={
                "phase": "agent",
                "phase_since": NOW + 60,
                "seen": ["1", "2"],
                "review_rounds": 1,
            },
        ),
        NOW + 60,
    )
    f = F.apply(
        f,
        F.next_action(
            f, dict(o, session=sess(pr_url="u", activity="working")), NOW + 70
        ),
        NOW + 70,
    )
    f = F.apply(
        f, F.next_action(f, dict(o, session=sess(pr_url="u")), NOW + 80), NOW + 80
    )
    a = F.next_action(f, dict(o, session=sess(pr_url="u")), NOW + 120)
    assert a["action"] == "run" and a["phase"] == "push"
    f = F.apply(f, dict(a, detail={"phase": "push"}), NOW + 120)
    a = F.next_action(
        f, dict(o, autopilot=ap(f, "push", state="running", note="pushing")), NOW + 130
    )
    assert a["note"] == "pushing"
    mine = {"state": "done", "source": "session", "lane": "push"}
    assert F.next_action(f, dict(o, autopilot=mine), NOW + 135)["note"] == F._BUSY_NOTE
    a = F.next_action(f, dict(o, autopilot=ap(f, "push", state="done")), NOW + 140)
    assert a["detail"]["phase"] == "watch" and a["detail"]["quiet_since"] == NOW + 140
    f = F.apply(f, a, NOW + 140)
    assert (
        F.next_action(f, dict(o, reviews={"ids": ["1", "2"]}), NOW + 200)["action"]
        == "wait"
    )
    # A third comment past max_rounds fails.
    a = F.next_action(f, dict(o, reviews={"ids": ["1", "2", "3"]}), NOW + 300)
    assert a["action"] == "fail" and "1 rounds" in a["reason"]
    # Quiet long enough: done.
    a = F.next_action(f, dict(o, reviews={"ids": ["1", "2"]}), NOW + 140 + 600)
    assert a["action"] == "done"


def test_checks_step():
    f = run_step(fly(step("checks", timeout_minutes=5)))
    assert (
        F.next_action(f, {"session": sess(), "checks": {"verdict": "ok"}}, NOW)[
            "action"
        ]
        == "done"
    )
    assert (
        F.next_action(f, {"session": sess(), "checks": {"verdict": "none"}}, NOW + 10)[
            "action"
        ]
        == "wait"
    )
    assert (
        F.next_action(f, {"session": sess(), "checks": {"verdict": "none"}}, NOW + 200)[
            "action"
        ]
        == "done"
    )
    a = F.next_action(
        f,
        {
            "session": sess(),
            "checks": {"verdict": "failed", "failing": ["lint", "test"]},
        },
        NOW,
    )
    assert a["action"] == "fail" and a["reason"] == "failing checks: lint, test"
    assert (
        F.next_action(f, {"session": sess(), "checks": {"verdict": "pending"}}, NOW)[
            "note"
        ]
        == "checks are running"
    )
    assert (
        F.next_action(
            f, {"session": sess(), "checks": {"verdict": "pending"}}, NOW + 400
        )["action"]
        == "fail"
    )


def test_command_step():
    f = run_step(fly(step("command", command="make test")))
    assert (
        F.next_action(f, {"session": sess(), "command": {"running": True}}, NOW)["note"]
        == "running: make test"
    )
    a = F.next_action(
        f,
        {"session": sess(), "command": {"running": False, "exit": 0, "output": "ok"}},
        NOW,
    )
    assert a["action"] == "done" and a["detail"]["output"] == "ok"
    a = F.next_action(
        f,
        {"session": sess(), "command": {"running": False, "exit": 2, "output": "boom"}},
        NOW,
    )
    assert a["action"] == "fail" and a["reason"] == "exited 2: boom"
    # Restarted mid-command: no job → run it again.
    assert (
        F.next_action(f, {"session": sess(), "command": None}, NOW)["action"] == "run"
    )


def test_ticket_step_skips_without_a_ticket_and_runs_with_one():
    f = fly(step("ticket", comment="hi"))
    a = F.next_action(f, {}, NOW)
    assert a["action"] == "advance"
    f = F.apply(f, a, NOW)
    assert f["state"] == "done" and f["step_states"]["s1"]["state"] == "skipped"
    g = fly(
        step("ticket"),
        session="t2",
        subject={"kind": "ticket", "source": "sc", "id": "5"},
    )
    assert F.next_action(g, {}, NOW)["action"] == "run"


def test_notify_step_runs_without_a_session():
    f = fly(step("notify", text="hi"), session="")
    assert F.next_action(f, {}, NOW)["action"] == "run"


# --------------------------------------------------------------------------- #
# on_fail
# --------------------------------------------------------------------------- #
def _failing(on_fail):
    f = run_step(
        fly(step("checks", on_fail=on_fail), step("notify"), session="s-" + on_fail)
    )
    return f, F.next_action(
        f, {"session": sess(), "checks": {"verdict": "failed"}}, NOW
    )


def test_on_fail_halt():
    f, a = _failing("halt")
    assert a["then"] == "halt"
    f = F.apply(f, a, NOW)
    assert f["state"] == "halted" and f["reason"].startswith("failing checks")
    assert [h["event"] for h in f["history"][-2:]] == ["failed", "halted"]


def test_on_fail_skip_moves_on_marking_the_step_failed():
    f, a = _failing("skip")
    f = F.apply(f, a, NOW)
    assert f["cursor"] == 1 and f["state"] == "running"
    assert f["step_states"]["s1"]["state"] == "failed"


def test_on_fail_you_parks_with_the_failure_text():
    f, a = _failing("you")
    f = F.apply(f, a, NOW)
    assert f["state"] == "waiting_you"
    assert F.you_text(f)[0].startswith("Checks failed: failing checks")
    f = F.op_done(f, "s1", now=NOW)
    assert f["cursor"] == 1


def test_on_fail_agent_hands_off_retries_then_halts():
    f, a = _failing("agent")
    assert a["then"] == "agent"
    f = F.apply(f, a, NOW)
    ss = f["step_states"]["s1"]
    assert (
        ss["rounds"] == 1 and ss["detail"]["phase"] == "fix" and f["state"] == "running"
    )
    # Waits for the fix turn, then re-runs the step.
    f = F.apply(
        f, F.next_action(f, {"session": sess(activity="working")}, NOW + 1), NOW + 1
    )
    f = F.apply(f, F.next_action(f, {"session": sess()}, NOW + 2), NOW + 2)
    a = F.next_action(f, {"session": sess()}, NOW + 40)
    assert a["action"] == "run" and a["retry"]
    f = F.apply(f, a, NOW + 40)
    assert (
        f["step_states"]["s1"]["detail"] == {} and f["step_states"]["s1"]["rounds"] == 1
    )
    f["step_states"]["s1"]["rounds"] = F.MAX_FIX_ROUNDS
    a = F.next_action(f, {"session": sess(), "checks": {"verdict": "failed"}}, NOW + 50)
    assert a["then"] == "halt" and "after 2 fix rounds" in a["reason"]
    # No session to hand to: halt.
    g = copy.deepcopy(f)
    g["step_states"]["s1"]["rounds"] = 0
    a = F.next_action(g, {"session": {"present": False, "pending": False}}, NOW)
    assert a["action"] == "wait"
    assert F.fail_action(g, "x", {"session": {"present": False}})["then"] == "halt"


# --------------------------------------------------------------------------- #
# Operations + restart + store mechanics
# --------------------------------------------------------------------------- #
def test_skip_retry_jump_pause_resume_cancel():
    f = fly(step("notify"), step("checks"), step("wait"), step("notify"))
    f = F.op_skip(f, "s3", now=NOW)  # ahead: marked, cursor unchanged
    assert f["cursor"] == 0 and f["step_states"]["s3"]["state"] == "skipped"
    f = F.op_skip(f, "s1", now=NOW)
    assert f["cursor"] == 1
    f = F.apply(f, {"action": "fail", "reason": "red", "then": "halt"}, NOW)
    assert f["state"] == "halted"
    f = F.op_retry(f, "s2", now=NOW)
    assert f["state"] == "running" and f["step_states"]["s2"]["state"] == "pending"
    f = F.op_jump(f, "s4", now=NOW)
    assert f["cursor"] == 3 and f["step_states"]["s2"]["state"] == "skipped"
    f = F.op_jump(f, "s2", now=NOW)
    assert f["cursor"] == 1 and all(
        f["step_states"][s]["state"] == "pending" for s in ("s2", "s3", "s4")
    )
    f = F.op_pause(f, now=NOW)
    assert f["state"] == "paused" and f["reason"] == "" and "paused" not in f
    assert f["history"][-1]["event"] == "paused"
    assert F.next_action(f, {"session": sess()}, NOW)["action"] == "wait"
    assert F.op_pause(f, now=NOW)["state"] == "paused"  # twice: a no-op
    f = F.op_resume(f, now=NOW)
    assert f["state"] == "running" and f["history"][-1]["event"] == "resumed"
    f = F.op_cancel(f, now=NOW)
    assert f["state"] == "cancelled" and f["finished"] == NOW
    with pytest.raises(F.FlightError) as err:
        F.op_resume(f, now=NOW)
    assert err.value.error == "finished"


def test_resume_reruns_a_failed_step_and_restores_waiting_you():
    f = run_step(fly(step("checks"), session="r1"))
    f = F.apply(f, {"action": "fail", "reason": "x", "then": "halt"}, NOW)
    f = F.op_resume(f, now=NOW)
    assert f["step_states"]["s1"]["state"] == "pending" and f["state"] == "running"
    g = fly(step("you", text="t"), session="r2")
    g = F.apply(g, F.next_action(g, {}, NOW), NOW)
    g = F.op_resume(F.op_pause(g, now=NOW), now=NOW)
    assert g["state"] == "waiting_you"


def test_restart_reconciles_from_the_stored_cursor(_store):
    f = fly(
        step("agent"),
        step("ship", lane="commit"),
        step("ship", lane="push"),
        attach=True,
    )
    f = run_step(f)
    F.mutate(f["id"], lambda x: x.update(f))
    # A "new server": everything re-read from disk.
    again = F.get(f["id"])
    assert again["cursor"] == 1 and again["step_states"]["s2"]["state"] == "running"
    a = F.next_action(
        again, {"session": sess(), "autopilot": ap(again, "commit", state="done")}, NOW
    )
    assert a["action"] == "done"
    again = F.apply(again, a, NOW)
    assert again["cursor"] == 2 and again["step_states"]["s3"]["state"] == "pending"
    raw = json.loads((_store / "flights.json").read_text())
    assert f["id"] in raw["flights"]


def test_normalize_is_the_migration(_store):
    _store.mkdir(parents=True, exist_ok=True)
    (_store / "flights.json").write_text(
        json.dumps(
            {
                "flights": {
                    "f_old": {
                        "plan_id": "p",
                        "steps": [{"kind": "notify"}],
                        "state": "weird",
                        "cursor": 99,
                    },
                    "f_bad": "nope",
                    "f_empty": {"steps": []},
                }
            }
        )
    )
    got = F.list_flights()
    assert [f["id"] for f in got] == ["f_old"]
    f = got[0]
    assert f["state"] == "running" and f["cursor"] == 1 and f["announced"] == []
    assert f["step_states"]["s1"] == {
        "state": "pending",
        "since": 0.0,
        "note": "",
        "detail": {},
        "rounds": 0,
    }


def test_claim_lease():
    f = fly(step("notify"))
    got, took = F.claim(f["id"], "boot-a", NOW)
    assert got is not None and took
    assert F.claim(f["id"], "boot-b", NOW + 10) == (None, False)
    got, took = F.claim(f["id"], "boot-a", NOW + 20)
    assert got is not None and not took
    got, took = F.claim(f["id"], "boot-b", NOW + 20 + F.LEASE_STALE_S + 1)
    assert got is not None and took


def test_prune_respects_the_grace():
    f = fly(step("notify"), session="gone")
    assert F.prune(set(), NOW + 10) == []
    assert F.get(f["id"])["gone_since"] == NOW + 10
    assert F.prune({"gone"}, NOW + 20) == []
    assert F.get(f["id"])["gone_since"] == 0.0
    assert F.prune(set(), NOW + 100) == []
    out = F.prune(set(), NOW + 100 + F.GONE_GRACE_S + 1)
    assert [x["id"] for x in out] == [f["id"]] and out[0]["state"] == "cancelled"
    # A flight armed before its session exists keeps the arm grace from take-off.
    g = F.take_off(F.get_plan("builtin:schedule"), session="later", now=NOW + 5000)
    F.prune(set(), NOW + 5000)
    assert F.prune(set(), NOW + 5000 + F.ARM_GRACE_S - 5) == []


def test_row_dto_and_event_data():
    assert F.row_dto("nobody") is None
    f = fly(step("you", text="Approve"), step("notify"), session="row")
    f = F.mutate(f["id"], lambda x: F.apply(x, F.next_action(x, {}, NOW), NOW))
    dto = F.row_dto("row")
    assert dto == {
        "id": f["id"],
        "plan_name": "P",
        "cursor": 0,
        "total": 2,
        "state": "waiting_you",
        "reason": "",
        "step": {
            "id": "s1",
            "kind": "you",
            "title": "Check it",
            "state": "waiting_you",
            "actor": "you",
        },
    }
    ev = F.event_data(f)
    assert set(ev) >= {
        "id",
        "plan_name",
        "session",
        "state",
        "cursor",
        "step",
        "reason",
        "detail",
    }
    assert ev["step"] == {
        "id": "s1",
        "kind": "you",
        "title": "Check it",
        "state": "waiting_you",
    }
    assert ev["detail"] == "row: Approve"
    F.mutate(f["id"], lambda x: F.op_cancel(x, now=NOW))
    assert F.row_dto("row") is None


def test_list_flights_keeps_at_most_keep_finished(monkeypatch):
    monkeypatch.setattr(F, "KEEP_FINISHED", 2)
    ids = []
    for i in range(4):
        f = fly(step("notify"), session="k%d" % i)
        F.mutate(f["id"], lambda x, i=i: F.op_cancel(x, now=NOW + i))
        ids.append(f["id"])
    fly(step("notify"), session="k9")
    kept = {f["id"] for f in F.list_flights()}
    assert ids[0] not in kept and ids[1] not in kept and ids[3] in kept


def test_paused_is_a_real_state():
    assert "paused" in F.STATES and "paused" not in F.ACTIVE
    # A paused you-step: Done ticks it off but the flight stays paused.
    f = fly(step("you", text="ok"), step("notify"), step("notify"), session="pz")
    f = F.apply(f, F.next_action(f, {}, NOW), NOW)
    f = F.op_pause(f, now=NOW)
    assert f["state"] == "paused"
    assert F.next_action(f, {}, NOW) == {"action": "wait", "note": ""}
    f = F.op_done(f, "s1", now=NOW)
    assert f["cursor"] == 1 and f["state"] == "paused"
    f = F.op_skip(f, "s2", now=NOW)
    assert f["cursor"] == 2 and f["state"] == "paused"
    # Jump / retry are a "go": the flight runs again.
    assert F.op_jump(copy.deepcopy(f), "s3", now=NOW)["state"] == "running"
    # A halted flight is not paused by Pause.
    g = run_step(fly(step("checks"), session="ph"))
    g = F.apply(g, {"action": "fail", "reason": "x", "then": "halt"}, NOW)
    assert F.op_pause(g, now=NOW)["state"] == "halted"


def test_a_paused_flight_holds_its_session_and_shows_on_the_row():
    f = fly(step("notify"), session="hold")
    F.mutate(f["id"], lambda x: F.op_pause(x, now=NOW))
    with pytest.raises(F.FlightError) as err:
        fly(step("notify"), session="hold")
    assert err.value.error == "in_flight"
    assert F.row_dto("hold")["state"] == "paused"
    assert F.event_data(F.get(f["id"]))["detail"] == "hold: paused"
    # A paused flight whose session is gone is still cancelled by prune.
    F.prune(set(), NOW)
    assert F.prune(set(), NOW + F.GONE_GRACE_S + F.ARM_GRACE_S + 1)[0]["id"] == f["id"]


def test_builtin_you_steps_carry_no_you_prefix():
    for pid in F.BUILTIN_IDS:
        for s in F.get_plan(pid)["steps"]:
            assert not s["title"].lower().startswith("you:"), (pid, s["title"])
    assert F.STEP_KINDS["you"]["default_title"] == "Check it"
    assert F.get_plan("builtin:ticket")["steps"][7]["title"] == "Approve the merge"


# --------------------------------------------------------------------------- #
# Review fixes
# --------------------------------------------------------------------------- #
def test_ship_ignores_a_persons_own_fast_track_run():
    """Review #7: only the record this flight armed (source, item, lane) is
    this step's outcome."""
    f = run_step(fly(step("ship", lane="pr"), session="own"))
    for rec in (
        {"state": "done", "source": "session", "item": "", "lane": "pr", "depth": "pr"},
        ap(f, "pr", state="done", item="f_other"),
        ap(f, "merge", state="halted", reason="x"),
    ):
        a = F.next_action(f, {"session": sess(), "autopilot": rec}, NOW)
        assert a == {"action": "wait", "note": "Fast-track is busy with your own run"}
    assert (
        F.next_action(
            f, {"session": sess(), "autopilot": ap(f, "pr", state="done")}, NOW
        )["action"]
        == "done"
    )


def test_empty_command_fails_at_start_and_never_indexes():
    """Review #8: a synced plan can carry ``command: ""`` (lenient)."""
    plan = F.put_plan(
        "p_sync",
        {"name": "x", "steps": [{"kind": "command", "config": {"command": ""}}]},
    )
    f = F.take_off(plan, session="cmd", now=NOW)
    a = F.next_action(f, {"session": sess()}, NOW)
    assert a["action"] == "fail" and a["reason"] == "no command" and a["then"] == "halt"
    # A running one (an older record) never IndexErrors either.
    f = F.apply(f, {"action": "run", "detail": {}}, NOW)
    assert (
        F.next_action(f, {"session": sess(), "command": {"running": True}}, NOW)["note"]
        == "running: the command"
    )


def test_a_performing_mark_blocks_a_second_effect_until_stale():
    """Review #1: an effect recorded as under way is never started again,
    unless its mark is so old the server that set it must have died."""
    f = fly(step("notify"), session="perf")
    f["step_states"]["s1"]["detail"]["performing"] = {"token": "t", "at": NOW}
    assert F.next_action(f, {}, NOW + 5) == {
        "action": "wait",
        "note": "carrying out Notify me",
    }
    assert F.next_action(f, {}, NOW + F.PERFORM_STALE_S + 1)["action"] == "run"


def test_bad_start_at_is_refused_before_anything_exists():
    """Review #3 (the pure half): start_cursor validates without creating."""
    plan = plan_of(step("agent"), step("notify"))
    with pytest.raises(F.FlightError) as err:
        F.start_cursor(plan, "zz")
    assert err.value.error == "unknown_step"
    assert F.start_cursor(plan, "", attach=True) == 1
    assert F.start_cursor(plan, "s2") == 1 and F.start_cursor(plan) == 0
    assert F.list_flights() == []


def test_replaced_halted_flight_is_reported_to_the_caller():
    """Review #4: the caller gets the flight it replaced, to announce."""
    f1 = fly(step("checks"), session="rep")
    F.mutate(
        f1["id"],
        lambda f: F.apply(f, {"action": "fail", "reason": "x", "then": "halt"}, NOW),
    )
    replaced = []
    fly(step("notify"), session="rep", replaced=replaced)
    assert [r["id"] for r in replaced] == [f1["id"]]
    assert (
        replaced[0]["state"] == "cancelled"
        and replaced[0]["reason"] == "replaced by a new flight"
    )


def test_the_store_is_read_once_per_change(monkeypatch):
    """Review #5: N session rows asking for_session cost one parse."""
    fly(step("notify"), session="c1")
    reads = []
    real = F.read_json
    monkeypatch.setattr(
        F, "read_json", lambda *a, **kw: reads.append(1) or real(*a, **kw)
    )
    F._CACHE["key"] = None  # as if another process had written it
    assert F.for_session("c1") is not None
    assert F.for_session("nobody") is None
    assert F.row_dto("c1")["state"] == "running"
    assert F.get(F.for_session("c1")["id"])["session"] == "c1"
    assert len(reads) == 1
    # A write invalidates (and re-primes) it; what readers get is a copy.
    got = F.for_session("c1")
    got["session"] = "mutated"
    assert F.for_session("c1")["session"] == "c1"
    F.mutate(got["id"], lambda x: F.op_pause(x, now=NOW))
    assert F.for_session("c1")["state"] == "paused"
    assert len(reads) == 1


def test_builtin_ticket_step_only_comments():
    """Review #2: the built-in's last step comments; it moves nothing."""
    last = F.get_plan("builtin:ticket")["steps"][-1]
    assert last["title"] == "Ticket: comment"
    assert last["config"] == {"state": "", "comment": "Merged by MindFlock"}
