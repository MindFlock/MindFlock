"""The joint check (backend.peer.joint_check): the pure model and its verdict
rule, the ``joint_check`` wire validators (and the ``check`` inside a
``joint_state`` answer), two services syncing their sides back to back, the
four routes, the shared agent's ``peer_check`` and its side guard, the ask's
deadline, and an older peer that answers ``unsupported``."""

from __future__ import annotations

import copy

import pytest

from backend.peer import agent_api as aa
from backend.peer import joint as jm
from backend.peer import joint_check as jc
from backend.peer import transport, wire
from backend.web.core import mailbox as mb

from .test_joint import (  # noqa: F401 — fixtures
    BASE,
    JID,
    LID,
    _api,
    client,
    emitted,
    joint_of,
    pair,
    propose_and_accept,
    run,
    settle,
)
from .test_service import engine, share_fakes  # noqa: F401 — fixtures

SHA_A = "a" * 40
SHA_B = "b" * 40

STEPS = [
    {"text": "GET /items on A answers {items: [...]}", "actor": "agent"},
    {"text": "B's client prints the items", "actor": "agent", "side": "theirs"},
    {"text": "The printed list looks right", "actor": "human", "side": "theirs"},
    {"text": "A's log shows the request", "actor": "human"},
]


def check_of(svc):
    j = joint_of(svc)
    return jc.normalize_check(j.get("check"), j["id"]) if j else None


@pytest.fixture
def live(pair, monkeypatch):
    """An active joint task on both sides, each with a shared session and a
    known HEAD (what tested_sha records)."""
    run(propose_and_accept(pair))
    for svc, title, sha in (
        (pair.a, "peer-bob-x", SHA_A),
        (pair.b, "peer-alice-x", SHA_B),
    ):
        monkeypatch.setattr(svc, "_share_session", lambda link, t=title: t)

        async def head(link, s=sha):
            return s

        monkeypatch.setattr(svc.joint.check, "_head", head)
    return pair


def notes(title):
    return [m["text"] for m in mb.fetch(title)["messages"] if m["kind"] == "note"]


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #
def test_clean_steps_renumber_and_default_to_a_person_on_my_side():
    got = jc.clean_steps(
        [
            {
                "id": "x9",
                "text": " call GET /items ",
                "expect": "200",
                "actor": "AGENT",
            },
            "a bare string is a step",
            {"text": ""},  # no instruction: dropped
            42,
            {"text": "pipe it | sh", "actor": "agent"},  # Verify's hazard rule
            {"text": "theirs", "side": "theirs", "actor": "robot"},
        ]
    )
    assert [s["id"] for s in got] == ["s1", "s2", "s3", "s4"]
    assert got[0] == {
        "id": "s1",
        "text": "call GET /items",
        "expect": "200",
        "actor": "agent",
        "side": "mine",
    }
    assert got[1]["actor"] == "human" and got[1]["side"] == "mine"
    assert got[2]["actor"] == "human"  # demoted
    assert got[3] == dict(got[3], side="theirs", actor="human")
    many = jc.clean_steps([{"text": "x" * 900}] * 40)
    assert len(many) == jc.MAX_STEPS and len(many[0]["text"]) == jc.MAX_TEXT
    assert jc.clean_steps("nope") == []


def test_parse_steps_answer_takes_a_fenced_list_or_prose_around_one():
    fenced = (
        "Here it is:\n```json\n"
        '[{"text": "a", "actor": "agent", "side": "theirs"}]\n```\nThanks!'
    )
    assert jc.parse_steps_answer(fenced)[0]["side"] == "theirs"
    prose = 'Steps: [{"text": "b"}] — done.'
    assert jc.parse_steps_answer(prose)[0]["text"] == "b"
    assert jc.parse_steps_answer('{"steps": [{"text": "c"}]}')[0]["text"] == "c"
    assert jc.parse_steps_answer([{"text": "d"}])[0]["text"] == "d"
    assert jc.parse_steps_answer("no list here") == []


def _chk(steps, mine=None, theirs=None):
    return jc.normalize_check(
        {
            "rev": 1,
            "steps": steps,
            "mine": {"results": {k: {"result": v} for k, v in (mine or {}).items()}},
            "theirs": {
                "results": {k: {"result": v} for k, v in (theirs or {}).items()}
            },
        },
        JID,
    )


S = [
    {"id": "s1", "text": "a", "actor": "agent", "side": "mine"},
    {"id": "s2", "text": "b", "actor": "agent", "side": "theirs"},
    {"id": "s3", "text": "c", "actor": "human", "side": "theirs"},
]


@pytest.mark.parametrize(
    "mine,theirs,want",
    [
        ({}, {}, "in_progress"),  # a fresh checklist: no agent has run yet
        ({}, {"s3": "pass"}, "in_progress"),  # agents still to go
        ({"s1": "pass"}, {"s3": "pass"}, "in_progress"),
        ({}, {"s3": "blocked"}, "in_progress"),  # agents first, even so
        ({"s1": "pass"}, {"s2": "pass"}, "needs_you"),  # now a person's step waits
        ({"s1": "pass"}, {"s2": "pass", "s3": "blocked"}, "needs_you"),
        ({"s1": "pass"}, {"s2": "pass", "s3": "pass"}, "verified"),
        ({"s1": "fail"}, {}, "failed"),  # fail beats everything
        ({"s1": "pass"}, {"s2": "blocked", "s3": "pass"}, "needs_you"),
        ({"s1": "blocked"}, {}, "needs_you"),  # its person can answer it now
        # A result filed under the wrong side counts for nothing.
        ({"s1": "pass", "s2": "pass", "s3": "pass"}, {}, "in_progress"),
    ],
)
def test_the_verdict_rule(mine, theirs, want):
    assert jc.verdict(_chk(S, mine, theirs)) == want


def test_no_steps_is_empty_and_a_stored_check_is_tolerated():
    assert jc.verdict(None) == "empty" and jc.verdict(_chk([])) == "empty"
    odd = jc.normalize_check(
        {
            "rev": "2",
            "steps": [
                {"id": "s1", "text": "a", "actor": "x", "side": "y"},
                {"id": "bad"},
            ],
            "mine": {"results": {"s1": {"result": "maybe"}, "s9": {"result": "pass"}}},
            "ask": "no",
        },
        JID,
    )
    assert odd["rev"] == 0 and odd["id"] == "joint:" + JID
    assert odd["steps"] == [
        {"id": "s1", "text": "a", "expect": "", "actor": "human", "side": "mine"}
    ]
    assert odd["mine"]["results"] == {
        "s1": {"result": "", "note": "", "at": "", "by": "agent"}
    }
    assert odd["ask"]["state"] == "" and jc.normalize_check("x", JID) is None


def test_event_text_names_the_failed_step_and_side():
    c = _chk(S, {"s1": "pass"}, {"s2": "fail"})
    assert (
        jc.event_text(c, "Bob") == "Joint check with Bob failed — step 2 on Bob's side"
    )
    c = _chk(S, {"s1": "fail"})
    assert jc.event_text(c, "Bob").endswith("step 1 on your side")
    c = _chk(S, {"s1": "pass"}, {"s2": "pass", "s3": "pass"})
    assert "every step passed on both sides" in jc.event_text(c, "Bob")


# --------------------------------------------------------------------------- #
# The wire
# --------------------------------------------------------------------------- #
CELL = {"result": "pass", "note": "ok", "at": "2026-10-15T10:00:00Z", "by": "agent"}
SIDE = {"results": {"s1": CELL}, "tested_sha": SHA_A, "at": "2026-10-15T10:00:00Z"}
STEP = {"id": "s1", "text": "t", "expect": "", "actor": "agent", "side": "mine"}
GOOD = {"id": JID, "rev": 2, "side_results": SIDE, "steps": [STEP], "by": "me"}


def test_joint_check_is_a_messages_op():
    from backend.peer import service as svc_mod

    assert "joint_check" in wire.OPS
    assert transport._OP_PERM["joint_check"] == "messages"
    assert svc_mod._OP_PERM["joint_check"] == "messages"


def test_good_joint_check_requests_pass_strict():
    assert wire.validate_request("joint_check", copy.deepcopy(GOOD)) == GOOD
    only = {
        "id": JID,
        "rev": 1,
        "side_results": {"results": {}, "tested_sha": "", "at": ""},
    }
    assert wire.validate_request("joint_check", copy.deepcopy(only)) == only
    assert wire.validate_response("joint_check", {"ok": True, "rev": 0})


@pytest.mark.parametrize(
    "patch",
    [
        {"rev": 0},
        {"by": "agent"},
        {"steps": [dict(STEP, id="step1")]},
        {"steps": [STEP, STEP]},  # duplicate id
        {"steps": [dict(STEP, side="both")]},
        {"steps": [dict(STEP, actor="robot")]},
        {"steps": [dict(STEP, text="")]},
        {"steps": [dict(STEP, text="x" * (wire.MAX_CHECK_TEXT + 1))]},
        {"steps": [dict(STEP, id="s%d" % i) for i in range(wire.MAX_CHECK_STEPS + 1)]},
        {"side_results": dict(SIDE, tested_sha="HEAD")},
        {"side_results": dict(SIDE, results={"s1": dict(CELL, result="ok")})},
        {"side_results": dict(SIDE, results={"s1": dict(CELL, by="peer")})},
        {"side_results": dict(SIDE, results={"s1": dict(CELL, note="x" * 301)})},
        {"side_results": dict(SIDE, results={"s1": dict(CELL, at="today")})},
        {"side_results": dict(SIDE, repo_path="/home/x")},
    ],
)
def test_bad_joint_check_requests_are_refused(patch):
    with pytest.raises(wire.ProtocolError):
        wire.validate_request("joint_check", dict(copy.deepcopy(GOOD), **patch))


def test_steps_and_by_go_together():
    p = copy.deepcopy(GOOD)
    del p["by"]
    with pytest.raises(wire.ProtocolError):
        wire.validate_request("joint_check", p)


def _joint_with_check(steps=None):
    j = jm.new_joint(JID, "brief", "me", "## c")
    j["state"] = "active"
    j["check"] = {
        "rev": 1,
        "by": "me",
        "steps": steps if steps is not None else [STEP],
        "mine": SIDE,
    }
    return j


def test_joint_state_view_carries_the_check_and_an_older_peer_drops_it():
    view = jm.wire_view(_joint_with_check())
    assert view["check"] == {
        "rev": 1,
        "by": "me",
        "steps": [STEP],
        "side_results": SIDE,
    }
    assert wire.validate_response("joint_state", {"joint": copy.deepcopy(view)})
    bad = copy.deepcopy(view)
    bad["check"]["steps"][0]["side"] = "both"
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("joint_state", {"joint": bad})
    # No written checklist: no key (an older peer's strict check is happy).
    assert "check" not in jm.wire_view(jm.new_joint(JID, "brief", "me", ""))
    assert "check" not in jm.wire_view(_joint_with_check(steps=[]))


def test_a_full_joint_state_with_a_full_check_fits_one_frame():
    big = "\U0001f600"
    j = jm.new_joint(JID, big * jm.MAX_BRIEF, "me", big * jm.MAX_CONTRACT)
    for i in range(jm.MAX_PROPOSALS):
        jm.merge_proposal(
            j,
            {
                "id": "cp_%08x" % i,
                "by": "me",
                "text": big * jm.MAX_PROPOSAL,
                "at": jm.now_iso(),
                "state": "open",
            },
        )
    steps = [
        dict(STEP, id="s%d" % (i + 1), text=big * jc.MAX_TEXT, expect=big * jc.MAX_TEXT)
        for i in range(jc.MAX_STEPS)
    ]
    cells = {s["id"]: dict(CELL, note=big * jc.MAX_NOTE) for s in steps}
    j["check"] = {
        "rev": 9,
        "by": "me",
        "steps": steps,
        "mine": dict(SIDE, results=cells),
    }
    view = jm.wire_view(j)
    assert len(view["check"]["steps"]) == jc.MAX_STEPS
    frame = {"t": "res", "id": 1, "ok": True, "p": {"joint": view}}
    assert len(wire.encode(frame)) <= wire.MAX_FRAME


# --------------------------------------------------------------------------- #
# Two services back to back
# --------------------------------------------------------------------------- #
def test_a_checklist_written_on_a_reaches_b_with_the_sides_flipped(live, emitted):
    out = run(live.a.joint.check.write(LID, STEPS))
    assert out["synced"] is True
    ca, cb = check_of(live.a), check_of(live.b)
    assert ca["rev"] == cb["rev"] == 1
    assert ca["by"] == "me" and cb["by"] == "peer"
    assert [s["side"] for s in ca["steps"]] == ["mine", "theirs", "theirs", "mine"]
    assert [s["side"] for s in cb["steps"]] == ["theirs", "mine", "mine", "theirs"]
    assert ca["verdict"] == cb["verdict"] == "in_progress"
    ev = [d for n, d in emitted if n == "peer.joint_check"]
    assert ev[0]["by"] == "you" and ev[-1]["by"] == "peer"
    assert all(
        set(d) >= {"link_id", "peer_name", "joint_id", "verdict", "by"} for d in ev
    )


def test_results_sync_to_the_peer_and_the_verdict_agrees(live, emitted):
    run(live.a.joint.check.write(LID, STEPS))
    api_a, api_b = _api(live.a, "/tmp"), _api(live.b, "/tmp")

    async def go():
        await api_a._op_check(
            {
                "action": "record",
                "step_id": "s1",
                "result": "pass",
                "note": "200 OK",
                "steps": None,
            },
            None,
        )
        await live.a.joint.check.answer(LID, "s4", "pass", "saw it in the log")
        await api_b._op_check(
            {
                "action": "record",
                "step_id": "s2",
                "result": "pass",
                "note": "",
                "steps": None,
            },
            None,
        )
        await live.b.joint.check.answer(LID, "s3", "pass")
        await settle()

    run(go())
    ca, cb = check_of(live.a), check_of(live.b)
    assert ca["mine"]["tested_sha"] == SHA_A and ca["theirs"]["tested_sha"] == SHA_B
    assert cb["mine"]["tested_sha"] == SHA_B and cb["theirs"]["tested_sha"] == SHA_A
    assert ca["theirs"]["results"]["s2"]["result"] == "pass"
    assert ca["mine"]["results"]["s4"]["by"] == "human"
    assert ca["verdict"] == cb["verdict"] == "verified"
    bells = [d for n, d in emitted if n == "peer.joint_check" and d["notify"]]
    assert {d["text"] for d in bells} == {
        "Joint check with Bob verified — every step passed on both sides",
        "Joint check with Alice verified — every step passed on both sides",
    }
    # A failure flips both sides, and rings once more.
    run(live.b.joint.check.answer(LID, "s3", "fail", "list was empty"))
    assert check_of(live.a)["verdict"] == "failed"
    last = [d for n, d in emitted if n == "peer.joint_check"][-1]
    assert last["notify"] and last["text"] == (
        "Joint check with Bob failed — step 3 on Bob's side"
    )


def test_a_peer_can_only_report_its_own_side(live):
    run(live.a.joint.check.write(LID, STEPS))
    # B (its terms) claims results for the steps that are A's to check.
    p = {
        "id": joint_of(live.a)["id"],
        "rev": 1,
        "side_results": {
            "results": {
                "s1": dict(CELL),  # A's agent step
                "s4": dict(CELL, by="human"),  # A's person's step
                "s2": dict(CELL),  # really theirs
            },
            "tested_sha": SHA_B,
            "at": "",
        },
    }
    res = run(live.a.joint.check.inbound(live.a.store.get(LID), LID, p))
    assert res == {"ok": True, "rev": 1}
    ca = check_of(live.a)
    assert set(ca["theirs"]["results"]) == {"s2"} and ca["mine"]["results"] == {}


def test_a_rewrite_clears_every_result_and_the_proposers_copy_wins_a_tie(live):
    run(live.a.joint.check.write(LID, STEPS))
    run(live.a.joint.check.answer(LID, "s4", "pass"))
    assert check_of(live.b)["theirs"]["results"]
    run(live.b.joint.check.write(LID, [{"text": "only one", "actor": "human"}]))
    ca, cb = check_of(live.a), check_of(live.b)
    assert ca["rev"] == cb["rev"] == 2 and len(ca["steps"]) == 1
    assert ca["mine"]["results"] == {} and ca["steps"][0]["side"] == "theirs"
    # Both write rev 3 while apart; the proposer's (A's) copy wins.
    live.wa.up = live.wb.up = False
    run(live.a.joint.check.write(LID, [{"text": "alice's"}]))
    run(live.b.joint.check.write(LID, [{"text": "bob's"}]))
    live.wa.up = live.wb.up = True
    j_a = joint_of(live.a)
    run(
        live.b.joint.check.inbound(
            live.b.store.get(LID), LID, {"id": j_a["id"], **jm.wire_view(j_a)["check"]}
        )
    )
    run(
        live.a.joint.check.inbound(
            live.a.store.get(LID),
            LID,
            {"id": j_a["id"], **jm.wire_view(joint_of(live.b))["check"]},
        )
    )

    async def drain():
        await settle()

    run(drain())
    assert check_of(live.a)["steps"][0]["text"] == "alice's"
    assert check_of(live.b)["steps"][0]["text"] == "alice's"


def test_results_for_an_old_revision_are_ignored_and_answered_with_mine(live):
    run(live.a.joint.check.write(LID, STEPS))
    run(live.a.joint.check.write(LID, STEPS[:1]))  # rev 2
    stale = {
        "id": joint_of(live.a)["id"],
        "rev": 1,
        "side_results": {"results": {}, "tested_sha": "", "at": ""},
    }
    live.wa.sent.clear()
    res = run(live.a.joint.check.inbound(live.a.store.get(LID), LID, stale))
    assert res == {"ok": False, "rev": 2}

    async def drain():
        await settle()

    run(drain())
    assert any(op == "joint_check" and "steps" in p for op, p in live.wa.sent)


def test_resync_carries_the_check_both_ways(live):
    live.wa.up = live.wb.up = False
    run(live.a.joint.check.write(LID, STEPS))
    assert check_of(live.b) is None
    live.wa.up = live.wb.up = True
    run(live.b.joint.resync(LID))
    assert check_of(live.b)["rev"] == 1 and len(check_of(live.b)["steps"]) == 4
    # A result recorded while apart reaches them on the next resync.
    live.wa.up = live.wb.up = False
    run(live.a.joint.check.answer(LID, "s4", "pass"))
    live.wa.up = live.wb.up = True
    run(live.a.joint.resync(LID))
    assert check_of(live.b)["theirs"]["results"]["s4"]["result"] == "pass"


def test_an_older_peer_keeps_the_results_local(live):
    def unsupported(p):
        raise transport.PeerOpError("unsupported")

    live.wa.override["joint_check"] = unsupported
    out = run(live.a.joint.check.write(LID, STEPS))
    assert out["synced"] is False and out["check"]["theirs_unsupported"] is True
    run(live.a.joint.check.answer(LID, "s4", "pass"))
    ca = check_of(live.a)
    assert ca["mine"]["results"]["s4"]["result"] == "pass"
    assert check_of(live.b) is None
    # They update: the next push clears the flag.
    del live.wa.override["joint_check"]
    run(live.a.joint.check.answer(LID, "s4", "fail"))
    assert check_of(live.a)["theirs_unsupported"] is False
    assert check_of(live.b)["theirs"]["results"]["s4"]["result"] == "fail"


# --------------------------------------------------------------------------- #
# Ask and run (through the shared session's mailbox)
# --------------------------------------------------------------------------- #
def test_ask_my_agent_then_it_writes_the_checklist(live):
    out = run(live.a.joint.check.ask(LID))
    assert out["check"]["ask"]["state"] == "asking"
    (note,) = notes("peer-bob-x")
    assert note.startswith("Joint check — your user asks")
    assert "peer_check" in note and "peer_get_diff" in note and "CONTRACT.md" in note
    api = _api(live.a, "/tmp")
    answer = '```json\n[{"text": "GET /items", "actor": "agent"}, {"text": "client prints", "actor": "agent", "side": "theirs"}]\n```'

    async def go():
        res = await api._op_check(
            {
                "action": "write",
                "steps": answer,
                "step_id": None,
                "result": None,
                "note": "",
            },
            None,
        )
        assert res == {"ok": True, "steps": 2}
        # The ask is spent: a second write is refused.
        with pytest.raises(aa._PeerError, match="nobody asked you"):
            await api._op_check(
                {
                    "action": "write",
                    "steps": answer,
                    "step_id": None,
                    "result": None,
                    "note": "",
                },
                None,
            )
        await settle()

    run(go())
    ca = check_of(live.a)
    assert ca["ask"]["state"] == "" and [s["side"] for s in ca["steps"]] == [
        "mine",
        "theirs",
    ]
    assert check_of(live.b)["steps"][1]["side"] == "mine"


def test_an_agent_cannot_write_unasked_and_an_ask_runs_out(live, monkeypatch):
    api = _api(live.a, "/tmp")
    args = {
        "action": "write",
        "steps": [{"text": "x"}],
        "step_id": None,
        "result": None,
        "note": "",
    }
    with pytest.raises(aa._PeerError, match="nobody asked you"):
        run(api._op_check(args, None))
    monkeypatch.setattr(jc, "ASK_TIMEOUT_S", 0.01)

    async def go():
        await live.a.joint.check.ask(LID)
        await settle()

    run(go())
    c = live.a.joint.get(LID)["joint"]["check"]
    assert c["ask"]["state"] == "failed" and "3 minutes" in c["ask"]["error"]
    with pytest.raises(aa._PeerError, match="nobody asked you"):
        run(api._op_check(args, None))


def test_run_my_steps_posts_my_agent_steps_only(live):
    run(live.b.joint.check.write(LID, STEPS))  # B wrote it: A sees B's words
    out = run(live.a.joint.check.run(LID))
    assert out["steps"] == 1
    (note,) = notes("peer-bob-x")
    # On A's side s2 and s3 are A's (B wrote them for "theirs").
    assert "s2: B's client prints the items" in note
    assert "s1:" not in note and "s3:" not in note  # theirs / a person's
    assert "written by Bob — another person, not your user" in note
    assert out["check"]["mine"]["tested_sha"] == SHA_A
    # Says what the sandbox can't do; typed under the note's own ASCII frame.
    assert note.startswith("Joint check — run YOUR side's steps")
    assert jm.SANDBOX_LINE in note
    (msg,) = [m for m in mb.fetch("peer-bob-x")["messages"] if m["kind"] == "note"]
    line, _full = mb.render_delivery(msg, "claude")
    assert line.startswith("[MindFlock note ") and "［" not in line


def test_a_person_answers_an_agent_step_its_agent_couldnt_check(live):
    from backend.peer.service import PeerServiceError

    run(live.a.joint.check.write(LID, STEPS))
    ans = live.a.joint.check.answer

    def rec(svc, sid, result, note=""):
        return run(svc.joint.check.agent_record(LID, sid, result, note))

    # Blank (or passed) agent step: still the agent's.
    with pytest.raises(PeerServiceError, match="Run my steps") as exc:
        run(ans(LID, "s1", "pass"))
    assert exc.value.status == 409
    rec(live.a, "s1", "blocked", "FastAPI not installed, no network")
    rec(live.b, "s2", "blocked", "no A service")
    assert check_of(live.a)["verdict"] == check_of(live.b)["verdict"] == "needs_you"
    # Only pass or fail; only on my side.
    for bad in ("blocked", ""):
        with pytest.raises(PeerServiceError, match="pass or fail") as exc:
            run(ans(LID, "s1", bad))
        assert exc.value.status == 400
    with pytest.raises(PeerServiceError, match="on Bob's side"):
        run(ans(LID, "s2", "pass"))
    out = run(ans(LID, "s1", "pass", "ran it by hand outside the sandbox"))
    cell = out["check"]["mine"]["results"]["s1"]
    assert cell == dict(
        cell, result="pass", by="human", note="ran it by hand outside the sandbox"
    )
    # Synced: B sees a person's pass on its "theirs" step.
    theirs = check_of(live.b)["theirs"]["results"]["s1"]
    assert theirs["result"] == "pass" and theirs["by"] == "human"
    # Once answered it is no longer blocked: the override is spent.
    with pytest.raises(PeerServiceError, match="Run my steps"):
        run(ans(LID, "s1", "fail"))
    # No note of the person's own: the agent's is kept, marked as such.
    out = run(live.b.joint.check.answer(LID, "s2", "pass"))
    assert out["check"]["mine"]["results"]["s2"]["note"] == "Agent: no A service"
    # It counts like any person's answer.
    run(live.a.joint.check.answer(LID, "s4", "pass"))
    run(live.b.joint.check.answer(LID, "s3", "pass"))
    assert check_of(live.a)["verdict"] == check_of(live.b)["verdict"] == "verified"
    # Run my steps clears it again for the agent.
    run(live.a.joint.check.run(LID))
    assert "s1" not in check_of(live.a)["mine"]["results"]


def test_ask_and_run_need_my_shared_session(live, monkeypatch):
    run(live.a.joint.check.write(LID, STEPS))
    monkeypatch.setattr(live.a, "_share_session", lambda link: "")
    from backend.peer.service import PeerServiceError

    for fn in (live.a.joint.check.ask, live.a.joint.check.run):
        with pytest.raises(PeerServiceError, match="no shared session"):
            run(fn(LID))


# --------------------------------------------------------------------------- #
# The agent's side guard
# --------------------------------------------------------------------------- #
def test_peer_check_records_only_my_agent_steps(live):
    run(live.a.joint.check.write(LID, STEPS))
    api = _api(live.a, "/tmp")

    def rec(sid, result="pass"):
        return run(
            api._op_check(
                {
                    "action": "record",
                    "step_id": sid,
                    "result": result,
                    "note": "",
                    "steps": None,
                },
                None,
            )
        )

    with pytest.raises(aa._PeerError, match="on Bob's side — only their agent"):
        rec("s2")
    with pytest.raises(aa._PeerError, match="a person's step"):
        rec("s4")
    with pytest.raises(aa._PeerError, match="no such step"):
        rec("s9")
    assert rec("s1") == {"ok": True, "verdict": "in_progress"}  # s2 still to go
    got = run(
        api._op_check(
            {
                "action": "read",
                "step_id": None,
                "result": None,
                "note": "",
                "steps": None,
            },
            None,
        )
    )
    assert got["id"] == "joint:" + joint_of(live.a)["id"]
    assert set(got["results"]) == {"s1"} and len(got["steps"]) == 4
    assert got["asking"] is False


def test_peer_check_args():
    assert aa.validate_args("check", {"action": "read"}) == {
        "action": "read",
        "step_id": None,
        "result": None,
        "note": "",
        "steps": None,
    }
    for args in (
        {},
        {"action": "accept"},
        {"action": "record", "result": "maybe"},
        {"action": "write", "steps": []},
        {"action": "write", "steps": [1]},
        {"action": "read", "id": "x"},
    ):
        with pytest.raises(aa.ArgError):
            aa.validate_args("check", args)


def test_peer_check_tool_maps_to_the_agent_api():
    from types import SimpleNamespace

    from backend.mcp import peer_tools as pt
    from backend.mcp.protocol import ToolError

    calls = []

    class Client:
        def call(self, op, args, timeout=None):
            calls.append((op, args))
            return {}

    tool = {t.name: t for t in pt.build_peer_tools(Client())}["peer_check"]
    ctx = SimpleNamespace()
    tool.handler({"action": "read"}, ctx)
    tool.handler(
        {"action": "record", "step_id": "s1", "result": "pass", "note": "ok"}, ctx
    )
    tool.handler({"action": "write", "steps": [{"text": "a"}]}, ctx)
    assert calls == [
        ("check", {"action": "read"}),
        (
            "check",
            {"action": "record", "step_id": "s1", "result": "pass", "note": "ok"},
        ),
        ("check", {"action": "write", "steps": [{"text": "a"}]}),
    ]
    with pytest.raises(ToolError, match="record needs"):
        tool.handler({"action": "record", "step_id": "s1"}, ctx)
    from backend.providers import mcp_attach

    assert "peer_check" in mcp_attach._PEER_TOOLS_FALLBACK
    assert "peer_check" in pt.PEER_INSTRUCTIONS


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_check_routes(live, client):
    # An active joint with no checklist yet: an empty one, so the card can act.
    empty = client.get(BASE + "/joint").json()["joint"]["check"]
    assert empty["steps"] == [] and empty["verdict"] == "empty"
    assert empty["link_id"] == LID and empty["rev"] == 0
    assert "check" not in client.get("/api/peer").json()["links"][0]["joint"]
    assert client.post(BASE + "/joint/check", json={"steps": []}).status_code == 400
    r = client.post(BASE + "/joint/check", json={"steps": STEPS})
    assert r.status_code == 200, r.text
    c = r.json()["check"]
    assert c["verdict"] == "in_progress" and c["link_id"] == LID and r.json()["synced"]
    assert client.get(BASE + "/joint").json()["joint"]["check"]["rev"] == 1
    summary = client.get("/api/peer").json()["links"][0]["joint"]["check"]
    assert summary == {"verdict": "in_progress", "steps": 4, "passed": 0}
    # A person answers only a person's step on their own side.
    ans = BASE + "/joint/check/answer"
    r = client.post(ans, json={"step_id": "s4", "result": "pass", "note": "seen"})
    assert (
        r.status_code == 200
        and r.json()["check"]["mine"]["results"]["s4"]["note"] == "seen"
    )
    r = client.post(ans, json={"step_id": "s3", "result": "pass"})
    assert r.status_code == 409 and "on Bob's side" in r.json()["error"]
    r = client.post(ans, json={"step_id": "s1", "result": "pass"})
    assert r.status_code == 409 and "Run my steps" in r.json()["error"]
    # …until its agent couldn't check it.
    run(live.a.joint.check.agent_record(LID, "s1", "blocked", "no network"))
    r = client.post(ans, json={"step_id": "s1", "result": "pass", "note": "by hand"})
    assert r.status_code == 200, r.text
    assert r.json()["check"]["mine"]["results"]["s1"]["by"] == "human"
    assert (
        client.post(ans, json={"step_id": "s4", "result": "maybe"}).status_code == 400
    )
    assert client.post(ans, json={"step_id": "s9", "result": "pass"}).status_code == 404
    r = client.post(ans, json={"step_id": "s4", "result": ""})  # clear it
    assert "s4" not in r.json()["check"]["mine"]["results"]
    r = client.post(BASE + "/joint/check/run")
    assert r.status_code == 200 and r.json()["steps"] == 1
    r = client.post(BASE + "/joint/check/ask")
    assert r.status_code == 200 and r.json()["check"]["ask"]["state"] == "asking"


def test_check_routes_need_an_active_joint(pair, client):
    assert client.post(BASE + "/joint/check", json={"steps": STEPS}).status_code == 404
    run(pair.a.joint.propose(LID, "brief"))
    r = client.post(BASE + "/joint/check", json={"steps": STEPS})
    assert r.status_code == 409 and "active" in r.json()["error"]
    assert client.get(BASE + "/joint").json()["joint"]["check"] is None


@pytest.mark.parametrize(
    "path,body",
    [
        ("/joint/check", {"steps": STEPS}),
        ("/joint/check/ask", None),
        ("/joint/check/answer", {"step_id": "s1", "result": "pass"}),
        ("/joint/check/run", None),
    ],
)
def test_check_routes_refuse_remote_control(live, client, monkeypatch, path, body):
    from backend.web.core import remote

    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    kw = {"headers": {"X-MindFlock-Remote": "other-device-key"}}
    if body is not None:
        kw["json"] = body
    r = client.post(BASE + path, **kw)
    assert r.status_code == 403
    assert check_of(live.a) is None
