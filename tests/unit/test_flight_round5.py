"""Sky and routes clarity, round 5 — the server, the MCP, the phone and voice
side of the owner's ten decisions (the Sky's own: frontend
``src/__tests__/skyRound5.test.ts``):

* the kinds carry ``can_fail`` (a Wait for me and a Wait cannot fail) and
  ``needs_session`` (Retry on a closed session's stop only where it works),
  over HTTP and in ``describe_flight``;
* a session at a Wait for me stop reads "Needs you: <stop>", one at a halt
  point "Paused before <stop>" (needs you), a person's pause "Paused by you"
  — on the phone and in voice, as on the Sky;
* the phone leads with Let the agent fix it on a test / CI / Verify failure,
  and says a stop held by an offline / limited agent is held.
"""

from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from backend.mcp import flights as mcp_flights
from backend.web.core import flights as fl
from backend.web.core import voice_router as vr

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = (ROOT / "backend/web/static/mobile.js").read_text()


# --- 6. can_fail / 7. needs_session on the kinds ---------------------------


def _kinds() -> dict:
    return {k["kind"]: k for k in fl.kinds_dto()["kinds"]}


def test_every_kind_says_whether_it_can_fail():
    kinds = _kinds()
    assert all(isinstance(k["can_fail"], bool) for k in kinds.values())
    assert kinds["you"]["can_fail"] is False
    assert kinds["wait"]["can_fail"] is False
    for kind in ("agent", "checks", "ship", "verify", "command"):
        assert kinds[kind]["can_fail"] is True, kind


def test_a_plug_in_kind_can_say_it_cannot_fail(monkeypatch):
    plug = next(iter(fl._kinds.all_kinds()))
    monkeypatch.setitem(plug.spec, "can_fail", False)
    assert fl.can_fail(plug.kind) is False


def test_every_kind_says_whether_it_needs_the_session():
    kinds = _kinds()
    assert kinds["agent"]["needs_session"] is True
    assert kinds["checks"]["needs_session"] is True
    assert kinds["wait"]["needs_session"] is False
    assert kinds["notify"]["needs_session"] is False


def test_the_http_kinds_carry_the_flags():
    from backend.web.server import app

    body = TestClient(app).get("/api/flights/kinds").json()
    wait = next(k for k in body["kinds"] if k["kind"] == "wait")
    assert wait["can_fail"] is False and wait["needs_session"] is False


def test_describe_flight_says_which_kinds_cannot_fail():
    dto = fl.kinds_dto()
    lines = {line.split(" · ", 1)[0]: line for line in mcp_flights.compact_kinds(dto)}
    assert mcp_flights.CANNOT_FAIL in lines["wait"]
    assert mcp_flights.CANNOT_FAIL in lines["you"]
    assert mcp_flights.CANNOT_FAIL not in lines["checks"]
    stops = mcp_flights.cookbook_view(dto)["stops"]
    wait = next(s for s in stops if s.startswith("wait"))
    assert mcp_flights.CANNOT_FAIL in wait
    # Beside the options, never cut off the end of a long line.
    if " · use: " in wait:
        assert wait.index(mcp_flights.CANNOT_FAIL) < wait.index(" · use: ")
    assert not any(
        mcp_flights.CANNOT_FAIL in s for s in stops if s.startswith("checks")
    )


# --- 1. / 2. the same state reads the same in voice and on the phone --------


def _row(flight: dict, **over) -> dict:
    row = {"title": "sc-1", "status": "running", "activity": "idle", "flight": flight}
    row.update(over)
    return row


def _flight(state: str, kind: str, title: str, actor: str = "robot", reason=""):
    return {
        "id": "f_1",
        "plan_name": "Ship it",
        "state": state,
        "reason": reason,
        "step": {"id": "s1", "kind": kind, "title": title, "actor": actor},
    }


def test_voice_a_wait_for_me_stop_needs_you():
    ss = vr.session_state(
        _row(_flight("waiting_you", "you", "Wait for me: Approve the merge", "you"))
    )
    assert ss == {
        "state": "needs_you",
        "label": "Needs you: Approve the merge",
        "reason": "flight_you",
        "in_bell": True,
    }
    assert vr._state_clause("Login fix", ss) == (
        "Login fix needs you: Approve the merge"
    )


def test_voice_a_halt_point_needs_you_and_a_pause_of_yours_is_calm():
    ss = vr.session_state(_row(_flight("paused", "ship", "Push", reason="halt point")))
    assert ss["label"] == "Paused before Push" and ss["in_bell"] is True
    assert ss["reason"] == "flight_paused"
    assert vr._state_word(ss) == "paused before Push, at your halt point"
    mine = vr.session_state(_row(_flight("paused", "ship", "Push")))
    assert mine == {
        "state": "idle",
        "label": "Paused by you",
        "reason": "",
        "in_bell": False,
    }
    server = vr.session_state(
        _row(_flight("paused", "ship", "Push", reason="stop removed"))
    )
    assert server["label"] == "Paused route"
    halted = vr.session_state(_row(_flight("halted", "checks", "CI checks")))
    assert halted["reason"] == "flight_halted" and halted["in_bell"] is True


def test_the_phone_says_needs_you_and_paused_by_you():
    assert 'flight_you: "Needs you",' in JS
    assert 'flight_paused: "Paused before its halt point",' in JS
    assert (
        'return t ? REASON_LABEL.flight_you + ": " + t : REASON_LABEL.flight_you;' in JS
    )
    assert 'reason: "flight_paused", label: pat ? "Paused before " + pat' in JS
    assert 'label: pwhy ? "Paused route" : "Paused by you"' in JS
    # Never "Wait for me" as a session's state word.
    assert 'flight_you: "Wait for me"' not in JS


# --- 8. which remedy leads, on the phone -----------------------------------


def test_the_phone_leads_with_the_fix_on_a_test_ci_or_verify_failure():
    assert "var FIX_LEADS = { test: true, checks: true, verify: true };" in JS
    assert (
        "acts: fl.step ? (fixLeads ? [FIX, RETRY, SKIP] : [RETRY, FIX, SKIP]) : [],"
        in JS
    )
    assert '"/op/fix"' in JS
    assert 'label: "Let the agent fix it"' in JS


# --- 10. a stop held by its session, on the phone and in voice -------------


def test_the_phone_says_a_held_stop():
    assert "function heldWords(inst, ss) {" in JS
    assert '(ss.state === "limit" ? " · usage limit" : " · agent offline")' in JS
    assert 'held.className = "sess-state sess-held tone-waiting";' in JS
    assert 'var hw = inst ? heldWords(inst, ss) : "";' in JS
    assert "voiceStateLine(name, ss, inst)" in JS
