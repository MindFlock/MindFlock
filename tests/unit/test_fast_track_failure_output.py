"""Fast-track hands a failed pre-commit hook to the agent WITH its output, the
steps left, and right away.

The owner's words: "When a pre-commit hook fails and we have fast-track on,
some of the text from that failure should then immediately go into the session
and try to continue the fast-track from within the agent, telling the agent
what steps need to be done that are left."

* :func:`autopilot.precommit_output` — the failed hook's section of the shell
  pane, scrubbed and capped (head + tail for a huge one);
* :func:`autopilot.remaining_steps` — what the run still does by itself after
  the agent's commit, for every depth × stage;
* the driver types the note NOW when the agent is idle (the agent-message
  lane's ``delivery: "now"`` gates), else leaves it to the lane; one note per
  failure, a fresh one (counted) when the same hook fails again.
"""

import asyncio
import os

import pytest

from backend.config import settings as settings_mod
from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import autopilot_driver as driver
from backend.web.core import mailbox


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", os.path.join(tmp_path, "ap.json"))
    monkeypatch.setattr(driver, "_run_member", lambda title: None)
    monkeypatch.setattr(driver, "_emit_autopilot", lambda title: None)
    monkeypatch.setattr(driver, "_emit_autopilot_event", lambda title: None)


#: A real-shaped pane: the typed one-liner, a passing hook, the failing one,
#: the next hook's line, and the shell prompt the one-liner returned to.
MYPY_PANE = """\
$ L="$(git rev-parse --absolute-git-dir)/mindflock_precommit.lock"; rm -f .mindflock_commit_status; touch "$L"
black....................................................................Passed
mypy.....................................................................Failed
- hook id: mypy
- exit code: 1

backend/app.py:12: error: Incompatible return value (got "int", expected "str")  [return-value]
backend/app.py:40: error: Name `foo` is not defined; did you mean '$bar'?  [name-defined]
Found 2 errors in 1 file (checked 1 source file)

flake8...................................................................Passed
erik@host:~/wt (feature/x) $
"""


# --- precommit_output -------------------------------------------------------- #
def test_the_failed_hooks_section_is_kept_and_nothing_else():
    out = ap.precommit_output(MYPY_PANE, "mypy")
    assert out.splitlines() == [
        "- hook id: mypy",
        "- exit code: 1",
        'backend/app.py:12: error: Incompatible return value (got "int", '
        'expected "str")  [return-value]',
        "backend/app.py:40: error: Name foo is not defined did you mean bar?  "
        "[name-defined]",
        "Found 2 errors in 1 file (checked 1 source file)",
    ]


def test_the_output_carries_none_of_the_unsafe_characters():
    out = ap.precommit_output(MYPY_PANE + "`rm -rf ~`; echo $HOME \\ 'x'\n", "mypy")
    for ch in "`$;'\\":
        assert ch not in out


def test_the_last_attempt_wins():
    """The commit one-liner re-commits after an auto-fixer: the pane holds the
    run twice, and only the latest failure is the one to fix."""
    first = "mypy....Failed\n- hook id: mypy\n\nold.py:1: error: stale\n"
    second = "mypy....Failed\n- hook id: mypy\n\nnew.py:2: error: fresh\n$ "
    out = ap.precommit_output(first + second, "mypy")
    assert "fresh" in out and "stale" not in out


def test_a_later_typed_command_ends_the_section():
    pane = (
        "mypy....Failed\n- hook id: mypy\nx.py:1: error: e\n"
        "$ rm -f .mindflock_commit_status; git commit -F .mindflock_commit_msg\n"
        "something else\n"
    )
    assert ap.precommit_output(pane, "mypy") == "- hook id: mypy\nx.py:1: error: e"


def test_with_no_hook_id_it_reads_under_the_last_failed_line():
    pane = "Run checks....Failed\nsomething broke\nbadly\nlint....Passed\n"
    assert ap.precommit_output(pane, "") == "something broke\nbadly"
    # An id that is not in the pane falls back the same way.
    assert ap.precommit_output(pane, "nope") == "something broke\nbadly"


def test_no_failure_means_no_output():
    assert ap.precommit_output("", "mypy") == ""
    assert ap.precommit_output("black....Passed\n$ ", "black") == ""


def test_ansi_and_control_characters_are_stripped():
    pane = (
        "\x1b[41mmypy....Failed\x1b[m\n- hook id: mypy\n"
        "\x1b[1mx.py:1:\x1b[0m error\x07 here\tthere\n"
    )
    assert ap.precommit_output(pane, "mypy") == (
        "- hook id: mypy\nx.py:1: error here    there"
    )


def test_a_huge_output_keeps_its_head_and_tail():
    lines = ["line %03d" % i for i in range(300)]
    pane = "tests....Failed\n- hook id: run-tests\n" + "\n".join(lines) + "\n"
    out = ap.precommit_output(pane, "run-tests").splitlines()
    assert len(out) == ap.OUTPUT_MAX_LINES + 1  # the marker
    assert out[: ap.OUTPUT_HEAD_LINES] == ["- hook id: run-tests"] + lines[:4]
    assert out[ap.OUTPUT_HEAD_LINES] == "… 276 lines …"  # 301 - 5 - 20
    assert out[-20:] == lines[-20:]


def test_long_lines_are_cut_and_the_whole_block_is_capped():
    lines = ["%03d " % i + "x" * 400 for i in range(40)]
    pane = "tests....Failed\n- hook id: run-tests\n" + "\n".join(lines) + "\n"
    out = ap.precommit_output(pane, "run-tests")
    assert len(out) <= ap.OUTPUT_MAX_CHARS
    rows = out.splitlines()
    assert all(len(r) <= ap.OUTPUT_LINE_CHARS for r in rows)
    assert rows[-1].startswith("039 "), "the end of the run survives"
    # The marker counts every line dropped, including the ones the char cap cut.
    marker = next(r for r in rows if r.startswith("… "))
    shown = len(rows) - 1
    assert marker == "… %d lines …" % (41 - shown)


def test_one_dropped_line_is_singular():
    lines = ["l%d" % i for i in range(25)]
    pane = "t....Failed\n- hook id: t\n" + "\n".join(lines)
    assert "… 1 line …" in ap.precommit_output(pane, "t")


# --- the note ---------------------------------------------------------------- #
def _detail(output=None):
    d = {"fix": "precommit", "hook": "mypy", "step": "mypy"}
    d["output"] = ap.precommit_output(MYPY_PANE, "mypy") if output is None else output
    return d


def test_the_note_quotes_the_output_as_an_indented_block():
    note = ap.failure_note(
        _detail(), resumes=True, remaining=["push the branch", "open the PR"]
    )
    rows = note.splitlines()
    assert rows[0] == (
        "Fast-track paused: the pre-commit step mypy failed, so nothing was "
        'committed ("pre-commit run mypy" shows why). Its output:'
    )
    block = rows[1:-1]
    assert block[0] == "    │ - hook id: mypy"
    assert all(r.startswith("    │ ") for r in block) and len(block) == 5
    assert rows[-1].startswith(
        "Fix it and commit. Fast-track will then push the branch and open the PR "
        "by itself, so fixing and committing is all that is left for you."
    )
    assert "tell the user" in rows[-1]
    for ch in "`$;'\\":
        assert ch not in note


def test_typed_it_is_still_one_line_with_the_gutter_between_output_lines():
    msg = mailbox.post("s1", ap.failure_note(_detail()), kind=mailbox.NOTE_KIND)
    line, full = mailbox.render_delivery(msg, "claude")
    assert full and "\n" not in line
    assert "Its output: │ - hook id: mypy │ - exit code: 1 │ backend/app.py:12" in line


def test_an_empty_output_leaves_the_note_one_line():
    note = ap.failure_note(_detail(output=""), resumes=True, remaining=[])
    assert "\n" not in note and "Its output" not in note
    assert "That commit is the last step of this Fast-track run." in note


def test_output_is_only_quoted_for_a_precommit_stop():
    note = ap.failure_note({"fix": "ci", "output": "boom"}, resumes=True)
    assert "boom" not in note and "\n" not in note


def test_the_note_says_when_the_same_failure_is_back():
    note = ap.failure_note(_detail(output=""), attempt=2)
    assert "the pre-commit step mypy failed again (second time), so" in note
    assert "failed again (third time)" in ap.failure_note(_detail(""), attempt=3)
    assert "(7th time)" in ap.failure_note(_detail(""), attempt=7)
    assert "again" not in ap.failure_note(_detail(""), attempt=1)
    check = ap.failure_note({"fix": "check"}, attempt=2)
    assert "verification check failed again (second time) on your" in check
    ci = ap.failure_note({"fix": "ci"}, attempt=2)
    assert "CI failed again (second time) on the pull request" in ci
    conflict = ap.failure_note(
        {"fix": "conflict", "reason": "cannot merge — it is behind"}, attempt=2
    )
    assert "the pull request still cannot merge (second time): it is behind." in (
        conflict
    )


def test_without_remaining_steps_the_generic_resume_sentence_stays():
    note = ap.failure_note(_detail(output=""), resumes=True)
    assert "Fast-track carries on from your commit (push, PR) by itself." in note


# --- remaining_steps ---------------------------------------------------------- #
STAGES = (
    "",
    "provisioning",
    "agent",
    "precommit",
    "interrupt",
    "committed",
    "pushed",
    "pr",
)
#: depth → (steps while no PR is open, steps once the PR is open)
EXPECTED = {
    "agent": ([], []),
    "commit": ([], []),
    "push": (["push the branch"], ["push it to the open PR"]),
    "pr": (["push the branch", "open the PR"], ["push it to the open PR"]),
    "merge": (
        ["push the branch", "open the PR", "merge it once CI is green"],
        ["push it to the open PR", "merge it once CI is green"],
    ),
}


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("depth", ap.DEPTHS)
def test_remaining_steps_for_every_depth_and_stage(depth, stage):
    want = EXPECTED[depth][1 if stage == "pr" else 0]
    assert ap.remaining_steps({"depth": depth}, {"stage": stage}) == want
    gated = ap.remaining_steps(
        {"depth": depth}, {"stage": stage, "check_required": True}
    )
    assert gated == (["run the verification check"] + want if want else [])


def test_no_target_means_no_steps():
    assert ap.remaining_steps({"depth": "off"}, {"stage": "agent"}) == []
    assert ap.remaining_steps({}, {}) == []


def test_the_steps_in_words():
    note = ap.failure_note(
        _detail(output=""),
        resumes=True,
        remaining=ap.remaining_steps({"depth": "merge"}, {"stage": "interrupt"}),
    )
    assert (
        "Fast-track will then push the branch, open the PR and merge it once CI is "
        "green by itself, so fixing and committing is all that is left for you."
    ) in note
    one = ap.failure_note(
        _detail(output=""), resumes=True, remaining=["push the branch"]
    )
    assert "Fast-track will then push the branch by itself" in one


# --- the record ---------------------------------------------------------------- #
def test_the_record_fields_normalise_and_clear():
    ap.arm("s1", depth="pr", message="m")
    rec = ap.get("s1")
    assert (rec["fix_output"], rec["fix_key"], rec["fix_times"], rec["fix_sent"]) == (
        "",
        "",
        0,
        "",
    )
    ap.update("s1", fix_output="x" * 5000, fix_times="nope", fix_key="precommit:mypy")
    rec = ap.get("s1")
    assert len(rec["fix_output"]) <= ap.OUTPUT_MAX_CHARS + 200
    assert rec["fix_times"] == 0 and rec["fix_key"] == "precommit:mypy"
    ap.update("s1", fix_output="boom")
    assert ap.dto("s1")["fix_output"] == "boom"
    assert ap.halt("s1", "x")["fix_output"] == "boom", "a halted row still shows it"
    assert ap.finish("s1")["fix_output"] == ""
    ap.arm("s1", depth="pr", message="m")
    assert ap.get("s1")["fix_key"] == "", "a re-arm counts from scratch"


# --- the driver ---------------------------------------------------------------- #
def _snap(**over):
    s = {
        "stage": "interrupt",
        "failed_step": "mypy",
        "failed_hook": "mypy",
        "dirty": True,
        "beyond_base": 1,
        "activity": "idle",
        "limited": False,
        "queue_pending": False,
        "background_waiting": False,
        "check": None,
        "check_required": False,
        "pr_checks": "ok",
        "merge_blockers": [],
        "on_base_branch": False,
        "branch": "feature/x",
        "has_origin": True,
        "head": "aaa",
        "now": 10_000.0,
    }
    s.update(over)
    return s


def _notes(title="s1"):
    return [
        m
        for m in mailbox.fetch(title, mark_read=False)["messages"]
        if m["kind"] == "note"
    ]


@pytest.fixture
def pane(monkeypatch):
    """The shell pane the driver reads; tests swap its text."""
    box = {"text": MYPY_PANE}
    monkeypatch.setattr(
        server, "_capture_shell_pane", lambda title, lines=400: box["text"]
    )
    return box


class _Calls(list):
    pass


@pytest.fixture
def typed_calls(monkeypatch):
    calls = _Calls()
    calls.result = ("delivered", "")
    monkeypatch.setitem(server.ENGINE.instances, "s1", object())

    def deliver(inst, title, msg, activities=server._MSG_NOW_ACTIVITIES):
        calls.append((title, msg["id"], tuple(activities)))
        return calls.result

    monkeypatch.setattr(server, "_mail_deliver_now", deliver)
    return calls


def _step(monkeypatch, tmp_path, snap):
    seen = (ap.get("s1"), object(), str(tmp_path), snap)
    monkeypatch.setattr(driver, "_autopilot_observe", lambda title: seen)
    acted = []

    async def act(title, wt, rec, snap, action, detail):
        acted.append(action)

    monkeypatch.setattr(driver, "_autopilot_act", act)
    asyncio.run(driver._autopilot_step("s1"))
    return acted


def test_a_failed_hook_hands_its_output_and_the_steps_left_to_the_agent_now(
    tmp_path, monkeypatch, pane, typed_calls
):
    ap.arm("s1", depth="merge", message="m")
    ap.update("s1", commits=1, step="commit")
    _step(monkeypatch, tmp_path, _snap())
    (note,) = _notes()
    assert note["delivery"] == "now"
    assert "Its output:\n    │ - hook id: mypy" in note["text"]
    assert "Found 2 errors in 1 file" in note["text"]
    assert "push the branch, open the PR and merge it once CI is green" in note["text"]
    # Typed straight away, and only into an idle agent.
    assert typed_calls == [("s1", note["id"], ("idle",))]
    rec = ap.get("s1")
    assert rec["state"] == "running" and rec["fixing"] == "precommit"
    assert rec["fix_output"] == ap.precommit_output(MYPY_PANE, "mypy")
    assert ap.dto("s1")["fix_output"].startswith("- hook id: mypy")
    assert (rec["fix_key"], rec["fix_times"]) == ("precommit:mypy", 1)


def test_an_agent_that_cannot_take_it_now_gets_it_from_the_lane(
    tmp_path, monkeypatch, pane, typed_calls
):
    typed_calls.result = ("pending", "someone is typing in the recipient's pane")
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1, step="commit")
    _step(monkeypatch, tmp_path, _snap())
    (note,) = _notes()
    assert len(typed_calls) == 1
    assert note["state"] == "pending", "the mailbox lane types it at the next idle"
    assert mailbox.next_pending("s1")["id"] == note["id"]
    assert ap.get("s1")["fixing"] == "precommit", "the hand-off stands either way"


def test_no_live_session_leaves_it_to_the_lane(tmp_path, monkeypatch, pane):
    called = []
    monkeypatch.setattr(server, "_mail_deliver_now", lambda *a, **k: called.append(a))
    monkeypatch.setattr(server.ENGINE, "instances", {})
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1, step="commit")
    _step(monkeypatch, tmp_path, _snap())
    (note,) = _notes()
    assert note["state"] == "pending" and called == []


def test_the_same_failure_seen_again_is_not_told_twice(tmp_path, pane, typed_calls):
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1)
    detail = {"fix": "precommit", "hook": "mypy", "step": "mypy", "output": "x"}
    snap = _snap()
    assert driver._autopilot_tell_agent(
        "s1", str(tmp_path), ap.get("s1"), detail, True, snap
    )
    assert driver._autopilot_tell_agent(
        "s1", str(tmp_path), ap.get("s1"), detail, True, snap
    )
    assert len(_notes()) == 1 and len(typed_calls) == 1
    assert ap.get("s1")["fix_times"] == 1


def test_the_same_hook_failing_after_the_fix_is_a_fresh_counted_note(
    tmp_path, monkeypatch, pane, typed_calls
):
    """The whole loop: fail → note → the agent fixes (not enough) → the run
    re-commits → the hook fails again → a second note with the NEW output and
    "second time" → again → out of hand-offs, a stopped note says "third time"."""
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1, step="commit")
    _step(monkeypatch, tmp_path, _snap())
    assert len(_notes()) == 1

    # The agent worked the fix and settled; the run re-commits (fixed) and
    # the fix turn's output is history.
    rec = ap.get("s1")
    ap.update("s1", worked_at=rec["fix_since"] + 1, idle_since=0.0)
    acted = _step(monkeypatch, tmp_path, _snap(now=rec["fix_since"] + 100))
    assert acted == ["commit"]
    assert ap.get("s1")["fix_output"] == "" and ap.get("s1")["fixing"] == ""
    ap.update("s1", commits=2)  # what the real commit action records

    pane["text"] = MYPY_PANE.replace("Found 2 errors", "Found 1 error")
    _step(monkeypatch, tmp_path, _snap(now=rec["fix_since"] + 200))
    first, second = _notes()
    assert "failed again (second time)" in second["text"]
    assert "Found 1 error" in second["text"] and "Found 2 errors" not in second["text"]
    assert ap.get("s1")["fix_times"] == 2 and ap.get("s1")["fix_rounds"] == 2

    rec = ap.get("s1")
    ap.update("s1", worked_at=rec["fix_since"] + 1, idle_since=0.0, fixing="")
    ap.update("s1", commits=3)
    _step(monkeypatch, tmp_path, _snap(now=rec["fix_since"] + 300))
    third = _notes()[-1]
    assert third["text"].startswith("Fast-track stopped:")
    assert "failed again (third time)" in third["text"]
    rec = ap.get("s1")
    assert rec["state"] == "halted" and rec["note"] == driver._TOLD_AGENT
    assert rec["fix_output"], "the halted row still shows what failed"
    assert len(typed_calls) == 3


def test_a_different_failure_starts_the_count_again(tmp_path, pane, typed_calls):
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1, fix_key="precommit:black", fix_times=2)
    detail = {"fix": "precommit", "hook": "mypy", "step": "mypy"}
    driver._autopilot_tell_agent(
        "s1", str(tmp_path), ap.get("s1"), detail, True, _snap()
    )
    (note,) = _notes()
    assert "again" not in note["text"]
    assert (ap.get("s1")["fix_key"], ap.get("s1")["fix_times"]) == ("precommit:mypy", 1)


def test_notes_off_still_records_the_output_and_halts(
    tmp_path, monkeypatch, pane, typed_calls
):
    s = settings_mod.load_settings()
    s.repository.fasttrack_failure_notes = False
    settings_mod.save_settings(s)
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1)
    _step(monkeypatch, tmp_path, _snap())
    rec = ap.get("s1")
    assert rec["state"] == "halted" and _notes() == [] and typed_calls == []
    assert rec["fix_output"].startswith("- hook id: mypy")


def test_an_unreadable_pane_still_sends_the_note(tmp_path, monkeypatch, typed_calls):
    def boom(title, lines=400):
        raise OSError("no tmux")

    monkeypatch.setattr(server, "_capture_shell_pane", boom)
    ap.arm("s1", depth="pr", message="m")
    ap.update("s1", commits=1)
    _step(monkeypatch, tmp_path, _snap())
    (note,) = _notes()
    assert "Its output" not in note["text"] and "\n" not in note["text"]


# --- "now" only into an idle agent -------------------------------------------- #
@pytest.fixture
def now_gates(monkeypatch):
    """The real ``_mail_deliver_now`` with every tmux probe stubbed open."""
    state = {"activity": "idle", "human": False, "typed": []}
    monkeypatch.setattr(server, "_typing_held", lambda inst, title: False)
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: False)
    monkeypatch.setattr(
        server, "_human_at_keys", lambda t, probe_tmux=True: state["human"]
    )
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    monkeypatch.setattr(server, "_pane_holds_agent", lambda name, program=None: True)
    monkeypatch.setattr(server, "_agent_activity", lambda inst, t: state["activity"])
    monkeypatch.setattr(server, "_tmux_client_input_recent", lambda t, s: False)

    def mail_type(inst, title, name, msg, now):
        state["typed"].append(msg["id"])
        return "delivered"

    monkeypatch.setattr(server, "_mail_type", mail_type)
    return state


class _Inst:
    Program = "claude"


def test_now_types_into_an_idle_agent_only(now_gates):
    msg = mailbox.post("s1", "note", kind=mailbox.NOTE_KIND, delivery="now")
    idle = ("idle",)
    assert server._mail_deliver_now(_Inst(), "s1", msg, activities=idle)[0] == (
        "delivered"
    )
    now_gates["activity"] = "working"
    result, why = server._mail_deliver_now(_Inst(), "s1", msg, activities=idle)
    assert result == "pending" and "mid-turn" in why
    # The default "now" still types into a working agent (its input box).
    assert server._mail_deliver_now(_Inst(), "s1", msg)[0] == "delivered"
    for activity in ("clarify", "limit", "offline"):
        now_gates["activity"] = activity
        assert server._mail_deliver_now(_Inst(), "s1", msg, activities=idle)[0] == (
            "pending"
        )
    assert len(now_gates["typed"]) == 2


def test_now_never_types_over_a_person(now_gates):
    now_gates["human"] = True
    msg = mailbox.post("s1", "note", kind=mailbox.NOTE_KIND, delivery="now")
    result, why = server._mail_deliver_now(_Inst(), "s1", msg, activities=("idle",))
    assert result == "pending" and "typing" in why and now_gates["typed"] == []


def test_now_never_types_into_a_dialog(now_gates, monkeypatch):
    monkeypatch.setattr(server, "_mail_type", lambda *a: server._MAIL_ON_PROMPT)
    msg = mailbox.post("s1", "note", kind=mailbox.NOTE_KIND, delivery="now")
    result, why = server._mail_deliver_now(_Inst(), "s1", msg, activities=("idle",))
    assert result == "pending" and "prompt" in why
