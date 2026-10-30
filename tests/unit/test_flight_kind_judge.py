"""The ``judge`` stop (``backend.web.core.flight_kinds.judge``): config,
answer reading, ``by: model`` on a thread with a fake one-shot, ``by: agent``
through the session's transcript, and a later stop's ``when`` reading the
answer (docs/flights.md "Round 15")."""

import asyncio
import threading
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import judge as J

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(J, "_ANSWERS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(D, "worktree_of", lambda flight: "/repo")
    monkeypatch.setattr(
        D,
        "when_diff",
        lambda flight, folder: {
            "files": ["a.py", "db/001.sql"],
            "added": 3,
            "removed": 1,
            "base": "main",
            "head": "abc",
        },
    )
    yield


def step(kind, sid=None, when=None, **config):
    out = {"kind": kind, "config": config}
    if sid:
        out["id"] = sid
    if when is not None:
        out["when"] = when
    return out


def fly(*steps, session="s"):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    return F.take_off(plan, session=session, now=NOW)


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def _join_threads():
    for t in threading.enumerate():
        if t.name.startswith("mf-flight-judge-"):
            t.join(5)


def start(f, now=NOW):
    """run → the kind's run (as the driver does) → the step is running."""
    a = F.next_action(f, {"session": sess()}, now)
    assert a["action"] == "run", a
    f = F.apply(f, a, now)
    a = asyncio.run(J._run(f, f["steps"][f["cursor"]], a, now))
    f["step_states"][f["steps"][f["cursor"]]["id"]]["detail"].update(a["detail"])
    _join_threads()
    return f


def observe(f, s=None, now=NOW):
    step_ = f["steps"][f["cursor"]]
    ss = f["step_states"][step_["id"]]
    s = s or sess()
    got = asyncio.run(J._observe(f, step_, ss, s, now))
    return {"session": s, "judge": got}


# --------------------------------------------------------------------------- #
# Registry + config
# --------------------------------------------------------------------------- #
def test_judge_is_a_work_kind_in_the_registry():
    spec = F.STEP_KINDS["judge"]
    assert spec["family"] == "work" and spec["actor"] == "robot"
    assert spec["risk"] == [] and spec["default_on_fail"] == "halt"
    assert [f["name"] for f in spec["fields"]] == ["question", "options", "by"]
    by = next(f for f in spec["fields"] if f["name"] == "by")
    assert by["options"] == ["model", "agent"] and by["default"] == "model"
    assert "judge" in F.SESSION_KINDS and "judge" in F.AGENT_KINDS
    dto = {k["kind"]: k for k in F.kinds_dto()["kinds"]}
    assert dto["judge"]["label"] == "Judge"


def test_config_requires_a_question_and_shapes_the_options():
    s = F.normalize_step(step("judge", question="Schema change?"), 0)
    assert s["config"] == {
        "question": "Schema change?",
        "options": "yes, no",
        "by": "model",
    }
    assert s["title"] == "Judge: Schema change?"
    s = F.normalize_step(
        step(
            "judge",
            question="Size?",
            options=" Small,LARGE ,small,, medium",
            by="AGENT",
        ),
        0,
    )
    assert (
        s["config"]["options"] == "small, large, medium"
        and s["config"]["by"] == "agent"
    )
    with pytest.raises(F.PlanError) as err:
        F.normalize_step(step("judge"), 3)
    assert err.value.field == "steps[3].config.question"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step(step("judge", question="q", by="oracle"), 0)
    assert err.value.field == "steps[0].config.by"
    # Lenient: a bad ``by`` falls back, an empty question is kept (the editor says).
    s = F.normalize_step(step("judge", by="oracle"), 0, strict=False)
    assert s["config"]["by"] == "model" and s["title"] == "Judge"


@pytest.mark.parametrize(
    "raw, options, want",
    [
        ("yes", ["yes", "no"], "yes"),
        ("  No.\n", ["yes", "no"], "no"),
        ("I think so.\nANSWER: yes", ["yes", "no"], "yes"),
        ("ANSWER: Large", ["small", "large"], "large"),
        ("The answer is no", ["yes", "no"], "no"),
        ("yes and no", ["yes", "no"], None),
        ("maybe", ["yes", "no"], None),
        ("", ["yes", "no"], None),
        ("ANSWER: nope", ["yes", "no"], None),
    ],
)
def test_pick(raw, options, want):
    assert J.pick(raw, options) == want


def test_answer_after_the_question_only():
    text = "## User\nQ one?\nANSWER: no\n## Claude\nANSWER: no\n## User\nQ two?\n## Claude\nsure\nANSWER: yes\n"
    assert J.answer_after(text, "Q two?") == "yes"
    assert J.answer_after(text, "Q one?") == "no"
    assert J.answer_after(text, "Q three?") is None
    assert J.answer_after(None, "Q two?") is None
    assert J.answer_after("## User\nQ two?\n## Claude\nhm\n", "Q two?") is None


# --------------------------------------------------------------------------- #
# by: model
# --------------------------------------------------------------------------- #
def test_model_judge_asks_with_the_diff_stat_and_records_the_value(monkeypatch):
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return "Yes\n"

    monkeypatch.setattr(J, "_ask_model", ask)
    f = start(fly(step("judge", question="Does it touch the schema?")))
    assert f["step_states"]["s1"]["detail"]["by"] == "model"
    assert len(prompts) == 1
    assert "a.py\ndb/001.sql\n2 files, +3 -1" in prompts[0]
    assert "Does it touch the schema?" in prompts[0]
    assert "exactly one of: yes, no" in prompts[0]
    a = F.next_action(f, observe(f), NOW + 5)
    assert a == {
        "action": "done",
        "note": "the model answered yes",
        "detail": {"value": "yes", "receipt": "yes", "answer": "Yes"},
    }
    f = F.apply(f, a, NOW + 5)
    assert f["step_states"]["s1"]["detail"]["value"] == "yes" and f["state"] == "done"


def test_model_judge_waits_while_asking(monkeypatch):
    gate = threading.Event()

    def slow(prompt):
        gate.wait(5)
        return "no"

    monkeypatch.setattr(J, "_ask_model", slow)
    f = fly(step("judge", question="Q?"))
    a = F.next_action(f, {"session": sess()}, NOW)
    f = F.apply(f, a, NOW)
    a = asyncio.run(J._run(f, f["steps"][0], a, NOW))
    f["step_states"]["s1"]["detail"].update(a["detail"])
    a = F.next_action(f, observe(f), NOW + 1)
    assert a["action"] == "wait" and a["note"] == "asking the model: Q?"
    gate.set()
    _join_threads()
    a = F.next_action(f, observe(f), NOW + 2)
    assert a["action"] == "done" and a["detail"]["value"] == "no"


def test_model_answer_outside_the_options_fails(monkeypatch):
    monkeypatch.setattr(J, "_ask_model", lambda p: "Maybe, it depends.")
    f = start(fly(step("judge", question="Q?", options="yes, no")))
    a = F.next_action(f, observe(f), NOW + 5)
    assert a["action"] == "fail" and a["then"] == "halt"
    assert a["reason"] == "the model answered 'Maybe, it depends.' — not one of yes, no"


def test_model_refusal_fails_with_its_reason(monkeypatch):
    def none(p):
        raise J.StepFailed("no installed CLI has a headless mode")

    monkeypatch.setattr(J, "_ask_model", none)
    f = start(fly(step("judge", question="Q?")))
    a = F.next_action(f, observe(f), NOW + 5)
    assert (
        a["action"] == "fail" and a["reason"] == "no installed CLI has a headless mode"
    )
    monkeypatch.setattr(J, "_ask_model", lambda p: 1 / 0)
    f = start(fly(step("judge", question="Q?"), session="s2"))
    a = F.next_action(f, observe(f), NOW + 5)
    assert a["reason"].startswith("could not ask the model: ")


def test_model_judge_lost_on_restart_asks_again(monkeypatch):
    monkeypatch.setattr(J, "_ask_model", lambda p: "yes")
    f = start(fly(step("judge", question="Q?")))
    J._ANSWERS.clear()  # a restarted server has no thread record
    a = F.next_action(f, observe(f), NOW + 5)
    assert a["action"] == "run" and "interrupted" in a["note"]


# --------------------------------------------------------------------------- #
# by: agent
# --------------------------------------------------------------------------- #
def test_agent_judge_delivers_the_question_and_reads_the_transcript(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append((title, text)))
    text = {"t": None}
    monkeypatch.setattr(J, "_transcript", lambda flight, s: text["t"])
    f = start(fly(step("judge", question="Did you change the schema?", by="agent")))
    assert sent[0][0] == "s"
    assert "Did you change the schema?" in sent[0][1]
    assert "ANSWER: <one of: yes, no>" in sent[0][1]
    d = f["step_states"]["s1"]["detail"]
    assert d["by"] == "agent" and d["sent"] is True
    # The settle gate first: working, then idle for a settle window.
    a = F.next_action(f, observe(f, sess(activity="working")), NOW + 1)
    assert a["action"] == "wait" and a["note"] == "the agent is working"
    f = F.apply(f, a, NOW + 1)
    a = F.next_action(f, observe(f), NOW + 2)
    assert a["note"] == "the agent just went idle"
    f = F.apply(f, a, NOW + 2)
    text["t"] = "## User\nDid you change the schema?\n## Claude\nANSWER: yes\n"
    a = F.next_action(f, observe(f), NOW + 40)
    assert a["action"] == "done" and a["detail"] == {
        "value": "yes",
        "receipt": "yes",
        "answer": "yes",
    }
    assert a["note"] == "the agent answered yes"


def test_agent_judge_without_an_answer_line_fails(monkeypatch):
    monkeypatch.setattr(D, "_deliver", lambda title, text: None)
    f = start(fly(step("judge", question="Q?", by="agent")))
    f["step_states"]["s1"]["detail"].update(worked_at=NOW + 1, idle_since=NOW + 2)
    monkeypatch.setattr(
        J, "_transcript", lambda flight, s: "## User\nQ?\n## Claude\nsure\n"
    )
    a = F.next_action(f, observe(f), NOW + 40)
    assert a["action"] == "fail" and a["reason"] == "the agent gave no ANSWER: line"
    monkeypatch.setattr(J, "_transcript", lambda flight, s: None)
    a = F.next_action(f, observe(f), NOW + 40)
    assert a["action"] == "fail" and "Claude session" in a["reason"]


def test_agent_judge_reads_only_once_idle(monkeypatch):
    monkeypatch.setattr(D, "_deliver", lambda title, text: None)
    monkeypatch.setattr(
        J, "_transcript", lambda flight, s: pytest.fail("read while working")
    )
    f = start(fly(step("judge", question="Q?", by="agent")))
    got = observe(f, sess(activity="working"))["judge"]
    assert got == {"answer": None, "transcript": None}


# --------------------------------------------------------------------------- #
# when reads the judge
# --------------------------------------------------------------------------- #
def test_a_later_when_reads_the_judge(monkeypatch):
    monkeypatch.setattr(J, "_ask_model", lambda p: "yes")
    f = start(
        fly(
            step("judge", sid="j", question="Schema?"),
            step("agent", sid="a", when="judge is yes", prompt="write a migration"),
            step("agent", sid="b", when="judge j is no", prompt="nothing to do"),
        )
    )
    f = F.apply(f, F.next_action(f, observe(f), NOW + 5), NOW + 5)
    assert f["cursor"] == 1
    facts = asyncio.run(D.when_facts(f, f["steps"][1], sess(), NOW))
    assert facts == {"judges": {"j": "yes"}, "judge_last": "yes"}
    assert F.next_action(f, {"session": sess(), "facts": facts}, NOW)["action"] == "run"
    f["step_states"]["a"]["state"] = "done"
    f["cursor"] = 2
    facts = asyncio.run(D.when_facts(f, f["steps"][2], sess(), NOW))
    a = F.next_action(f, {"session": sess(), "facts": facts}, NOW)
    assert a == {
        "action": "skip_when",
        "note": "skipped: judge j is no — judge j answered yes",
    }
    f = F.apply(f, a, NOW)
    assert f["state"] == "done" and f["step_states"]["b"]["state"] == "skipped"


def test_ask_model_uses_the_describe_machinery(monkeypatch, tmp_path):
    from backend.web.core import commit_message as cm

    monkeypatch.setattr(
        cm, "pick_argv", lambda prompt, p, fb, purpose: ["fake", prompt]
    )
    monkeypatch.setattr(cm, "neutral_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(cm, "_run", lambda argv, cwd, timeout: "\x1b[1m no \x1b[0m")
    assert J._ask_model("Q") == " no "
    monkeypatch.setattr(
        cm,
        "pick_argv",
        lambda *a, **kw: (_ for _ in ()).throw(cm.CommitMessageError("none")),
    )
    with pytest.raises(J.StepFailed):
        J._ask_model("Q")
