"""The phone page (/m) answers what a session's Verify or route waits on.

The session sheet (⋯, or a long press on a row) shows the Verify and the
route a session waits on — the stop, the reason in the server's words, the
person's steps — and the taps that answer them, the desktop's own routes
(lib/verifyActions.ts, lib/flightsApi.ts). The models are pure and run here
under node, like tests/unit/test_mobile_needs_you.py; the wiring is read as
text."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
JS = STATIC / "mobile.js"
HTML = STATIC / "mobile.html"
CSS = STATIC / "mobile.css"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return JS.read_text()


def _fn(name: str) -> str:
    js = _js()
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start)
    return js[start:end]


def _run(rows: list, expr: str):
    """Evaluate `expr` (over `rows`) with the page's pure state helpers."""
    js = _js()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]
    script = (
        "var localStorage = { getItem: function () { return null; }, setItem: function () {} };\n"
        + src
        + "\nvar rows = "
        + json.dumps(rows)
        + ";\nconsole.log(JSON.stringify("
        + expr
        + "));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _verify(state: str, **extra) -> dict:
    v = {
        "state": state,
        "plan": "work",
        "steps": {"total": 3, "agent_done": 2, "human": 1},
        "human_steps": [],
        "failed_steps": [],
        "detail": "",
        "at": 1,
    }
    v.update(extra)
    return v


def _flight(
    state: str, title: str, kind: str = "checks", actor: str = "robot", reason: str = ""
) -> dict:
    return {
        "id": "f_1",
        "plan_name": "Ticket to PR",
        "cursor": 1,
        "total": 3,
        "state": state,
        "reason": reason,
        "step": {
            "id": "s2",
            "kind": kind,
            "title": title,
            "state": "pending",
            "actor": actor,
        },
    }


def _keys(m) -> list:
    return [a["key"] for a in m["acts"]] if m else []


def _labels(m) -> list:
    return [a["label"] for a in m["acts"]] if m else []


# --- Verify ---------------------------------------------------------------


@needs_node
def test_verify_actions_per_state():
    rows = [
        {"title": "none", "activity": "idle"},
        {
            "title": "you",
            "activity": "idle",
            "verify": _verify(
                "needs_you",
                detail="1 step needs you — check it, then confirm.",
                human_steps=[
                    {
                        "id": "h1",
                        "text": "Open the login page",
                        "expect": "The new button shows",
                        "why": "agent couldn't: no browser",
                    }
                ],
            ),
        },
        {"title": "run", "activity": "working", "verify": _verify("verifying")},
        {
            "title": "bad",
            "activity": "idle",
            "verify": _verify(
                "failed",
                detail="1 step failed: Login works.",
                failed_steps=[
                    {"id": "a1", "text": "Login works", "note": "500 on submit"}
                ],
            ),
        },
        {
            "title": "stuck",
            "activity": "idle",
            "verify": _verify(
                "blocked", detail="The check stopped before it finished."
            ),
        },
        {
            "title": "ok",
            "activity": "idle",
            "verify": _verify("passed", detail="All 3 steps passed."),
        },
    ]
    out = _run(rows, "rows.map(verifyModel)")
    none, you, run, bad, stuck, ok = out
    assert _labels(none) == ["Verify"]
    assert none["head"] == ""
    # Needs you: the two answers, the server's detail, the person's steps
    # with what to look for and why it is theirs.
    assert _labels(you) == ["Confirm verified", "Not verified…"]
    assert _keys(you) == ["verify_yes", "verify_no"]
    assert you["detail"] == "1 step needs you — check it, then confirm."
    assert you["steps"] == [
        {
            "text": "Open the login page",
            "expect": "The new button shows",
            "why": "agent couldn't: no browser",
        }
    ]
    assert you["stepsLabel"] == "Your step"
    assert you["head"].startswith("Confirm verified")
    assert _labels(run) == ["Stop verifying"]
    assert run["head"] == "Verifying — 2 of 2 steps done"
    assert _labels(bad) == ["Verify again", "Let its agent fix it"]
    assert _keys(bad) == ["verify_start", "verify_fix"]
    assert bad["steps"][0] == {
        "text": "Login works",
        "expect": "500 on submit",
        "why": "",
    }
    assert bad["stepsLabel"] == "What failed"
    assert bad["head"] == "Verify failed — 1 step"
    assert _labels(stuck) == ["Verify again"]
    assert stuck["head"] == "Verify blocked"
    assert stuck["detail"] == "The check stopped before it finished."
    assert _labels(ok) == ["Verify again"]
    assert [m["waits"] for m in out] == [False, True, True, True, True, False]


@needs_node
def test_verify_answers_need_the_plan():
    # Every route but /verify is addressed by the plan: a row without one
    # (an older server) offers only what can work.
    rows = [
        {"title": "a", "verify": _verify("needs_you", plan="")},
        {"title": "b", "verify": _verify("verifying", plan="")},
        {"title": "c", "verify": _verify("failed", plan="")},
    ]
    out = _run(
        rows,
        "rows.map(function (r) { return verifyModel(r).acts.map(function (a) { return a.key; }); })",
    )
    assert out == [[], [], ["verify_start"]]


@needs_node
def test_needs_you_hint_names_the_phone_buttons_not_the_desktop():
    rows = [
        {
            "title": "you",
            "activity": "idle",
            "verify": _verify(
                "needs_you",
                human_steps=[{"id": "h1", "text": "Try it", "expect": "It works"}],
            ),
        },
        {
            "title": "stuck",
            "activity": "idle",
            "verify": _verify("blocked", detail="No results file"),
        },
    ]
    out = _run(rows, "rows.map(function (r) { return sessionState(r).hint; })")
    assert "desktop" not in out[0]
    assert "Do this step, then press Confirm verified (or Not verified…)" in out[0]
    assert "• Try it — It works" in out[0]
    assert out[1].startswith("Verify couldn't go on — No results file")
    assert "Verify again" in out[1]


# --- the route --------------------------------------------------------------


@needs_node
def test_route_actions_per_state():
    rows = [
        {
            "title": "halt",
            "activity": "idle",
            "flight": _flight("halted", "CI checks", reason="CI checks failed: 2 red"),
        },
        {"title": "pause", "activity": "idle", "flight": _flight("paused", "Open PR")},
        {
            "title": "point",
            "activity": "idle",
            "flight": _flight("paused", "Merge", reason="halt point"),
        },
        {
            "title": "you",
            "activity": "idle",
            "flight": _flight("waiting_you", "Try it on staging", "you", "you"),
        },
        {
            "title": "vstop",
            "activity": "idle",
            "flight": _flight("waiting_you", "Verify", "verify"),
        },
        {
            "title": "fly",
            "activity": "working",
            "flight": _flight("running", "Agent works"),
        },
        {"title": "none", "activity": "idle"},
        # A worker's branch-only dto is not a route of its own.
        {
            "title": "branch",
            "activity": "idle",
            "flight": dict(_flight("halted", "X"), id=""),
        },
    ]
    halt, pause, point, you, vstop, fly, none, branch = _run(
        rows, "rows.map(routeModel)"
    )
    assert _labels(halt) == ["Retry", "Skip"]
    assert halt["head"] == "Route “Ticket to PR” · Halted at CI checks"
    # The reason in the server's words, without the stop's name in front.
    assert halt["detail"] == "Failed: 2 red"
    assert _labels(pause) == ["Carry on"]
    assert pause["head"] == "Route “Ticket to PR” · Paused at Open PR"
    assert _labels(point) == ["Carry on"]
    assert point["head"] == "Route “Ticket to PR” · Paused before Merge"
    assert point["detail"] == "It reached a halt point."
    assert _labels(you) == ["Done", "Skip"]
    assert you["head"] == "Route “Ticket to PR” · Wait for me: Try it on staging"
    # A Verify stop is answered by its Verify, never by Done (409).
    assert _labels(vstop) == ["Skip"]
    assert "Verify" in vstop["detail"]
    assert fly is None and none is None and branch is None


@needs_node
def test_route_reason_prefers_the_stops_own_note():
    fl = _flight("waiting_you", "Approve", "you", "you")
    fl["step"]["note"] = "Check the release notes read right"
    out = _run([{"title": "x", "flight": fl}], "routeModel(rows[0]).detail")
    assert out == "Check the release notes read right"


@needs_node
def test_paused_hint_names_carry_on_not_the_desktop():
    rows = [{"title": "p", "activity": "idle", "flight": _flight("paused", "Open PR")}]
    hint = _run(rows, "sessionState(rows[0]).hint")
    assert "desktop" not in hint
    assert "Carry on" in hint and "Open PR" in hint


# --- the wiring -----------------------------------------------------------


def test_sheet_draws_route_and_verify_blocks():
    fn = _fn("renderActSheet")
    assert "verifyModel(inst)" in fn and "routeModel(inst)" in fn
    assert 'waitBlock(title, rm, "act-route")' in fn
    assert 'waitBlock(title, vm, "act-verify")' in fn
    # Not on a peer's shared session: those are the sharer's to answer.
    assert "var vm = peer ? null" in fn and "var rm = peer ? null" in fn
    # What waits on you comes first; a plain Verify after Restart / Wake /
    # Resume, never on an agent that is gone or still starting.
    assert fn.index("if (vm && vm.waits)") < fn.index('actRow("Restart"')
    plain = fn.index("if (vm && !vm.waits")
    assert fn.index('actRow("Resume session"') < plain < fn.index('actRow("Close"')
    assert (
        'ss.state !== "offline" && ss.state !== "starting"' in fn[plain : plain + 120]
    )


def test_wait_actions_post_the_desktops_routes():
    fn = _fn("waitAct")
    assert '"/api/instances/" + encodeURIComponent(title) + "/verify", {}' in fn
    assert 'planApi(plan, "confirm"), { verified: true }' in fn
    assert "{ verified: false, note: note }" in fn
    assert 'planApi(plan, "fix"), { in_session: true }' in fn
    assert 'planApi(plan, "cancel")' in fn
    assert (
        '"/api/flights/" + encodeURIComponent(fl.id) + "/steps/" + encodeURIComponent(fl.step.id) + "/" + verb'
        in fn
    )
    assert '"/api/flights/" + encodeURIComponent(fl.id) + "/resume"' in fn
    assert '"/api/test-plans/" + encodeURIComponent(plan) + "/" + verb' in _fn(
        "planApi"
    )
    # Every response field the other side adds is optional.
    assert (
        "j && j.queued" in fn
        and "j && j.delivered === false" in fn
        and "j && j.session" in fn
    )


def test_not_verified_note_is_typed_in_the_ask_sheet():
    js = _js()
    fn = _fn("waitAct")
    assert (
        "askSheet(" in fn
        and 'placeholder: "What did you see? The agent gets this"' in fn
    )
    # Never a native dialog: they do nothing in the desktop app.
    for bad in ("prompt(", "alert(", "confirm(", "window.prompt", "window.confirm"):
        assert not re.search(r"(?<![\w.])" + re.escape(bad), fn), bad
    ask = _fn("askSheet")
    assert 'askFieldEl.classList.toggle("hidden", !field)' in ask
    assert 'askFieldEl.value = ""' in ask
    assert "run(typed)" in js
    html = HTML.read_text()
    box = html[html.index('id="ask-sheet"') : html.index('id="ask-ok"')]
    assert (
        'id="ask-field"' in box
        and "hidden" in box.split('id="ask-field"')[1].split(">")[0]
    )
    assert ".ask-field {" in CSS.read_text()


def test_refusals_keep_the_servers_sentence():
    fn = _fn("actPost")
    # The fix route's refusal is a plain sentence in `detail`.
    assert "j.detail || j.error" in fn
    assert "actError(failWord" in fn


def test_wait_block_shows_what_to_look_for_and_why():
    fn = _fn("waitBlock")
    assert '"Look for: " + s.expect' in fn
    assert '"Why it\'s yours: " + s.why' in fn
    css = CSS.read_text()
    for cls in (".act-wait {", ".act-wait-steps", ".act-step-expect"):
        assert cls in css


def test_phone_says_no_desktop_only_for_verify_or_routes():
    js = _js()
    assert "confirm verified on the desktop" not in js
    assert "resume it from the desktop" not in js
