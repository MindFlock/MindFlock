"""A review stop converges (task ``rails-review``): later rounds are
follow-ups of the earlier findings, a reviewer that gives no verdict is never
the agent's to fix, no report is never a silent pass, and running out of
rounds is a question for the person (``rounds`` / ``unresolved``)."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import review as R

NOW = 1_800_000_000.0
FINDINGS = "1. a.py:3 — off by one\n2. b.py:9 — None deref"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    R._FINISHED.clear()
    yield
    R._FINISHED.clear()


def sess(**kw):
    return dict({"present": True, "status": "running", "activity": "idle"}, **kw)


def fly(on_fail="agent", session="s", **config):
    st = {"kind": "review", "config": config, "on_fail": on_fail}
    plan = F.save_plan({"name": "P", "steps": [st, {"kind": "notify"}]}, now=NOW)
    return F.take_off(plan, session=session, now=NOW)


def ss_of(f):
    return f["step_states"][f["steps"][0]["id"]]


def finished(text, title="review-1"):
    entry = {
        "title": title,
        "state": "done",
        "report": {"id": "m1", "status": "done", "summary": text, "ts": NOW + 5},
    }
    return {"entry": entry, "text": text, "report": True, "closed": "deleted"}


@pytest.fixture
def spawned(monkeypatch):
    """``spawn_worker`` faked: records each brief, hands out review-N; the
    session's HEAD is ``state["head"]`` and ``state["follows"]`` says whether
    the earlier sha is still its ancestor."""
    state = {"briefs": [], "n": 0, "head": "sha1", "follows": True, "since": []}

    async def _spawn(flight, step, prompt, **kw):
        state["n"] += 1
        state["briefs"].append(prompt)
        return "review-%d" % state["n"]

    def _head(flight, since=""):
        state["since"].append(since)
        return state["head"], bool(since) and state["follows"]

    monkeypatch.setattr(D, "spawn_worker", _spawn)
    monkeypatch.setattr(R, "_base_of", lambda flight: "main")
    monkeypatch.setattr(R, "_head_of", _head)
    return state


def run(f, now):
    """One driver pass that asks a reviewer: next_action → _run → apply."""
    a = F.next_action(f, {"session": sess()}, now)
    assert a["action"] == "run", a
    out = asyncio.run(R._run(f, f["steps"][0], a, now))
    F.apply(f, out, now)
    return out


def review_finds(f, text, now):
    title = ss_of(f)["detail"]["children"][0]
    a = F.next_action(f, {"session": sess(), "review": finished(text, title)}, now)
    F.apply(f, a, now)
    return a


def agent_fixes(f, now):
    """The agent's fix turn ends and settles: the stop's retry comes due."""
    after = sess(worked_at=now + 1, turn_ended_at=now + 2)
    F.apply(f, F.next_action(f, {"session": after}, now + 3), now + 3)
    a = F.next_action(f, {"session": after}, now + 400)
    assert a["action"] == "run" and a.get("retry") is True
    out = asyncio.run(R._run(f, f["steps"][0], a, now + 400))
    F.apply(f, out, now + 400)
    return out


# --------------------------------------------------------------------------- #
# 1. Follow-up rounds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", R.MODES)
def test_the_follow_up_brief(mode):
    text = R.followup_brief(
        "s", "main", "abc123", FINDINGS, focus="RULE-1", mode=mode, round_no=2
    )
    assert "FOLLOW-UP" in text and "round 2" in text
    assert FINDINGS in text  # verbatim
    assert "at commit abc123" in text
    assert "`git log abc123..HEAD`" in text and "`git diff abc123..HEAD`" in text
    assert "INTRODUCED" in text and "rebuttal" in text
    assert "`git diff main...abc123`" in text
    assert "data loss, a security hole, or a crash on a common path" in text
    assert "`No findings`" in text and "1. path/to/file.py:123 —" in text
    assert "should block the merge, most serious first" in text
    assert "Change NO files" in text
    if mode == "bugs":
        assert "Also: RULE-1" in text
    else:
        assert "----- rules -----\nRULE-1\n" in text


@pytest.mark.parametrize("mode", R.MODES)
def test_every_first_brief_asks_only_for_what_blocks_the_merge(mode):
    text = R.brief("s", "main", "", mode)
    assert "Report only findings that should block the merge" in text
    assert "most serious first" in text


def test_the_round_after_the_fix_follows_up(spawned):
    f = fly()
    out = run(f, NOW)
    assert out["detail"]["reviewed_sha"] == "sha1" and out["detail"]["round"] == 1
    assert "git diff main...HEAD" in spawned["briefs"][0]
    assert spawned["since"] == [""]

    a = review_finds(f, FINDINGS, NOW + 10)
    assert a["action"] == "fail" and a["then"] == "agent"
    d = ss_of(f)["detail"]
    # The hand-off keeps what the next round follows up.
    assert d["phase"] == "fix"
    assert d["followup"] == {"findings": FINDINGS, "sha": "sha1", "round": 1}
    assert "check your fixes" in D._fix_note(f["steps"][0], a)

    spawned["head"] = "sha2"
    out = agent_fixes(f, NOW + 20)
    brief = spawned["briefs"][1]
    assert spawned["since"][-1] == "sha1"
    assert "FOLLOW-UP" in brief and FINDINGS in brief
    assert "`git diff sha1..HEAD`" in brief
    assert out["detail"]["reviewed_sha"] == "sha2" and out["detail"]["round"] == 2
    assert out["note"] == "asking review-2 to check the fixes (second opinion, round 2)"
    assert ss_of(f)["rounds"] == 1

    # Round 2 finds one the fixes introduced: round 3 follows up THAT round.
    review_finds(f, "1. c.py:1 — the fix leaks a handle", NOW + 500)
    assert ss_of(f)["detail"]["followup"]["sha"] == "sha2"
    spawned["head"] = "sha3"
    agent_fixes(f, NOW + 600)
    brief = spawned["briefs"][2]
    assert "round 3" in brief and "`git diff sha2..HEAD`" in brief
    assert "c.py:1 — the fix leaks a handle" in brief and "a.py:3" not in brief

    a = review_finds(f, "No findings", NOW + 1100)
    assert a["action"] == "done" and f["cursor"] == 1


def test_a_rebase_falls_back_to_a_full_review(spawned):
    f = fly()
    run(f, NOW)
    review_finds(f, FINDINGS, NOW + 10)
    spawned["follows"] = False  # sha1 is no longer in the branch
    out = agent_fixes(f, NOW + 20)
    assert "FOLLOW-UP" not in spawned["briefs"][1]
    assert "git diff main...HEAD" in spawned["briefs"][1]
    assert out["detail"]["round"] == 1
    assert "a full review" in out["note"]


def test_no_recorded_sha_falls_back_to_a_full_review(spawned):
    spawned["head"] = ""  # HEAD unreadable when round 1 was asked
    f = fly()
    run(f, NOW)
    review_finds(f, FINDINGS, NOW + 10)
    assert ss_of(f)["detail"]["followup"]["sha"] == ""
    agent_fixes(f, NOW + 20)
    assert "FOLLOW-UP" not in spawned["briefs"][1]
    assert spawned["since"][-1] == ""


def test_a_persons_retry_follows_up_and_a_jump_starts_fresh(spawned):
    f = fly(on_fail="halt")
    run(f, NOW)
    a = review_finds(f, FINDINGS, NOW + 10)
    assert a["then"] == "halt" and f["state"] == "halted"
    F.op_retry(f, f["steps"][0]["id"], now=NOW + 20)
    assert ss_of(f)["detail"]["followup"]["findings"] == FINDINGS
    spawned["head"] = "sha2"
    run(f, NOW + 21)
    assert "FOLLOW-UP" in spawned["briefs"][1]
    # A jump back onto the stop forgets it: a fresh full review.
    review_finds(f, FINDINGS, NOW + 30)
    F.op_jump(f, f["steps"][0]["id"], now=NOW + 40)
    assert "followup" not in ss_of(f)["detail"]
    run(f, NOW + 41)
    assert "FOLLOW-UP" not in spawned["briefs"][2]


def test_a_reask_within_a_follow_up_round_stays_a_follow_up(spawned):
    f = fly()
    run(f, NOW)
    review_finds(f, FINDINGS, NOW + 10)
    spawned["head"] = "sha2"
    agent_fixes(f, NOW + 20)
    gone = {"entry": {"title": "review-2", "state": "gone"}, "text": ""}
    a = F.next_action(f, {"session": sess(), "review": gone}, NOW + 500)
    assert a["action"] == "run" and a["detail"]["reasks"] == 1
    out = asyncio.run(R._run(f, f["steps"][0], a, NOW + 500))
    assert "FOLLOW-UP" in spawned["briefs"][2]
    assert out["detail"]["round"] == 2 and out["detail"]["reasks"] == 1
    assert ss_of(f)["rounds"] == 1  # the re-ask spent no fix round


# --------------------------------------------------------------------------- #
# 4. Rounds are the stop's own; running out is a question
# --------------------------------------------------------------------------- #
def _to_last_round(f, spawned, rounds):
    run(f, NOW)
    t = NOW
    for k in range(rounds):
        a = review_finds(f, FINDINGS, t + 10)
        assert a["action"] == "fail" and a["then"] == "agent", (k, a)
        spawned["head"] = "sha%d" % (k + 2)
        agent_fixes(f, t + 20)
        t += 1000
    return t


def test_out_of_rounds_asks_the_person(spawned):
    f = fly(rounds=2)
    t = _to_last_round(f, spawned, 2)
    a = review_finds(f, FINDINGS, t + 10)
    assert a["action"] == "you"
    assert a["note"] == "2 findings still open after 2 rounds"
    ss = ss_of(f)
    assert ss["state"] == "waiting_you" and f["state"] == "waiting_you"
    assert ss["detail"]["ask"] == "unresolved"
    assert ss["detail"]["findings"] == FINDINGS
    assert ss["detail"]["receipt"] == "2 findings left open"
    assert "failed_reason" not in ss["detail"]
    assert F.you_text(f)[0] == ("Second opinion: 2 findings still open after 2 rounds")
    assert not any(h["event"] == "halted" for h in f["history"])
    # It stays parked: no new reviewer, no hand-off.
    nxt = F.next_action(f, {"session": sess()}, t + 20)
    assert nxt["action"] == "wait"


def test_done_on_the_question_carries_on_with_the_findings_recorded(spawned):
    f = fly(rounds=1)
    t = _to_last_round(f, spawned, 1)
    review_finds(f, FINDINGS, t + 10)
    F.op_done(f, f["steps"][0]["id"], now=t + 20)
    ss = ss_of(f)
    assert ss["state"] == "done" and f["cursor"] == 1
    assert ss["detail"]["findings"] == FINDINGS
    assert ss["detail"]["receipt"] == "2 findings left open"


def test_retry_on_the_question_gives_another_set_of_rounds(spawned):
    f = fly(rounds=1)
    t = _to_last_round(f, spawned, 1)
    review_finds(f, FINDINGS, t + 10)
    F.op_retry(f, f["steps"][0]["id"], now=t + 20)
    ss = ss_of(f)
    assert ss["state"] == "pending" and ss["rounds"] == 0
    assert f["state"] == "running"
    run(f, t + 21)
    assert "FOLLOW-UP" in spawned["briefs"][-1]
    # The rounds start over: these findings go back to the agent.
    a = review_finds(f, FINDINGS, t + 30)
    assert a["action"] == "fail" and a["then"] == "agent"


def test_continue_finishes_the_stop_with_the_findings_kept(spawned):
    f = fly(rounds=1, unresolved="continue")
    t = _to_last_round(f, spawned, 1)
    a = review_finds(f, FINDINGS, t + 10)
    assert a["action"] == "done" and a["note"] == "2 findings left open"
    ss = ss_of(f)
    assert ss["state"] == "done" and f["cursor"] == 1
    assert ss["detail"]["findings"] == FINDINGS
    assert ss["detail"]["receipt"] == "2 findings left open"


def test_more_rounds_than_the_global_limit(spawned):
    assert F.MAX_FIX_ROUNDS < 4
    f = fly(rounds=4)
    t = _to_last_round(f, spawned, 4)  # asserts each one went to the agent
    assert ss_of(f)["rounds"] == 4
    assert ss_of(f)["detail"]["fix"]["of"] == 4
    assert review_finds(f, FINDINGS, t + 10)["action"] == "you"


def test_rounds_are_clamped_and_unresolved_checked():
    # A plan as written is refused out of range; a stored one is clamped.
    with pytest.raises(F.PlanError, match="rounds on a Review stop"):
        F.normalize_step({"kind": "review", "config": {"rounds": 99}})
    assert R._config({"rounds": 99}, False, "x")["rounds"] == R.MAX_ROUNDS
    assert R._config({"rounds": 0}, False, "x")["rounds"] == 1
    assert R._config({"rounds": "x"}, False, "x")["rounds"] == R.DEFAULT_ROUNDS
    step = F.normalize_step({"kind": "review", "config": {"rounds": 7}})
    assert step["config"]["rounds"] == 7
    with pytest.raises(F.PlanError):
        R._config({"unresolved": "maybe"}, True, "steps[0].config")
    assert R._config({"unresolved": "maybe"}, False, "x")["unresolved"] == "ask"
    assert R._config({"unresolved": "CONTINUE"}, True, "x")["unresolved"] == "continue"


def test_other_kinds_keep_the_global_limit():
    test_step = F.normalize_step({"kind": "test", "config": {}})
    assert F.fix_round_limit(test_step) == F.MAX_FIX_ROUNDS
    review = F.normalize_step({"kind": "review", "config": {"rounds": 5}})
    assert F.fix_round_limit(review) == 5
    f = fly(rounds=5)
    assert F.returns_of(f)[f["steps"][0]["id"]]["of"] == 5


# --------------------------------------------------------------------------- #
# 3. No report is never a silent pass: the last reply is read
# --------------------------------------------------------------------------- #
def test_observe_reads_the_last_reply_before_closing(monkeypatch):
    f = fly()
    a = F.next_action(f, {"session": sess()}, NOW)
    F.apply(f, dict(a, detail={"children": ["review-1"], "base": "main"}), NOW)
    kid = {
        "title": "review-1",
        "present": True,
        "status": "running",
        "activity": "idle",
        "activity_since": NOW - 1000,
        "created": NOW - 1000,
        "settle_s": 8.0,
        "report": None,
    }
    order: list = []
    monkeypatch.setattr(D, "_children_obs", lambda flight, step: [kid])
    monkeypatch.setattr(R, "_report_message", lambda parent, t, since: None)

    def _reply(title):
        order.append("reply")
        return "I checked it all.\n\x071. a.py:3 — off by one\x07"

    def _close(parent, title):
        order.append("close")
        return "deleted"

    monkeypatch.setattr(R, "_last_reply", _reply)
    monkeypatch.setattr(R, "_close", _close)
    step = f["steps"][0]
    o = asyncio.run(R._observe(f, step, ss_of(f), {}, NOW + 30))
    assert o["entry"]["state"] == "done" and o["report"] is False
    assert order == ["reply", "close"]
    assert "\x07" not in o["reply"] and "1. a.py:3" in o["reply"]
    a = F.next_action(f, {"session": sess(), "review": o}, NOW + 30)
    assert a["action"] == "fail" and a["reason"].startswith(
        "the second opinion has 1 finding:"
    )


def test_parse_reply():
    assert R.parse_reply("") is None
    assert R.parse_reply("It all looks reasonable to me.") is None
    assert R.parse_reply("**No findings.**") == (0, "")
    # A "No findings" before a list is not the verdict.
    n, text = R.parse_reply("No findings yet\n1. a.py:3 — off by one")
    assert n == 1 and text == "1. a.py:3 — off by one"
    # A list, then the verdict: the verdict wins.
    assert R.parse_reply("1. ran the tests\n2. read the diff\nNo findings") == (
        0,
        "",
    )


# --------------------------------------------------------------------------- #
# The session's HEAD and the ancestor check (a real repository)
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_head_of_reads_the_sha_and_spots_a_rebase(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("2\n")
    _git(repo, "commit", "-qam", "two")
    second = _git(repo, "rev-parse", "HEAD")
    inst = SimpleNamespace(GetWorktreePath=lambda: str(repo))
    monkeypatch.setattr(
        D,
        "_server",
        lambda: SimpleNamespace(ENGINE=SimpleNamespace(instances={"s": inst})),
    )
    assert R._head_of({"session": "s"}, first) == (second, True)
    assert R._head_of({"session": "s"}, "") == (second, False)
    # Rewritten history: the old sha is no longer an ancestor.
    _git(repo, "commit", "-q", "--amend", "-m", "two, amended")
    head, follows = R._head_of({"session": "s"}, second)
    assert head != second and follows is False
    assert R._head_of({"session": "gone"}, first) == ("", False)
