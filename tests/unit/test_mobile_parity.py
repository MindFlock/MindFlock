"""The phone (/m) can do what the rail can, with the rail's words.

Pins for mobile.js / mobile.html / mobile.css:

1. **No native dialogs.** ``confirm()``, ``prompt()`` and ``alert()`` do nothing
   in the desktop app and are a silent "no" in some phone PWA shells; every
   question on the page is an inline sheet or strip.
2. **One state table.** The six states, their labels and their tone names are
   the ones ``frontend/src/lib/sessionState.ts`` defines — read from that file,
   not restated here, so a rename on the desktop fails this test until the
   phone follows.
3. **Session actions hit the rail's routes**: Close → ``/close``, Delete →
   ``/cleanup`` (its 409 "folder in use" said in the rail's sentence), Resume
   (a paused session only — no Pause, as on the desktop's menus) →
   ``/resume``, Restart → ``/revive``, Recently closed →
   ``GET /api/recently-closed`` + ``POST …/<id>/reopen``.
4. **Labels**: Terminal (not Shell); the New sheet's buttons are the desktop
   New dialog's.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _html() -> str:
    return (STATIC / "mobile.html").read_text()


def _css() -> str:
    return (STATIC / "mobile.css").read_text()


def _strip_js_comments(src: str) -> str:
    # Good enough for this file: no `//` or `/*` inside its string literals
    # except URLs, which never sit next to a dialog call.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


# --------------------------------------------------------------------------- #
# 1. No native dialogs
# --------------------------------------------------------------------------- #
NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(")


def test_mobile_js_calls_no_native_dialog():
    code = _strip_js_comments(_js())
    hits = NATIVE.findall(code)
    assert not hits, "native dialog call(s) in mobile.js: %r" % hits
    assert "window.confirm" not in code and "window.alert" not in code


def test_mobile_html_calls_no_native_dialog():
    html = re.sub(r"<!--.*?-->", "", _html(), flags=re.S)
    assert not NATIVE.findall(html)


def test_questions_are_sheets():
    js, html = _js(), _html()
    assert 'id="ask-sheet"' in html and "function askSheet(" in js
    # Merge asks first, in the desktop's words, on the sheet.
    merge = js.split("  function doMerge(")[1].split("\n  }\n")[0]
    assert '"Merge this PR?"' in merge and "askSheet(" in merge
    # The marker the old confirm() pointed at never existed on the desktop.
    assert "🟡" not in js


# --------------------------------------------------------------------------- #
# 2. One state table, the rail's
# --------------------------------------------------------------------------- #
def _ts_record(name: str) -> dict:
    ts = (ROOT / "frontend" / "src" / "lib" / "sessionState.ts").read_text()
    body = ts.split("export const %s" % name)[1].split("};")[0]
    return dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", body))


def test_state_labels_match_the_rail():
    js = _js()
    labels = _ts_record("STATE_LABEL")
    assert set(labels) == {"starting", "working", "idle", "limit", "offline"}
    table = js.split("  var SESSION_STATES = {")[1].split("};")[0]
    for state, label in labels.items():
        assert re.search(
            r'\b%s:\s*\{ tone: "\w+",\s*label: "%s" \}' % (state, re.escape(label)),
            table,
        ), state
    assert "needs_you:" in table
    reasons = _ts_record("REASON_LABEL")
    rtable = js.split("  var REASON_LABEL = {")[1].split("};")[0]
    for reason, label in reasons.items():
        assert '%s: "%s"' % (reason, label) in rtable, reason
    # The words a reader sees, spelled out once more for grep.
    for word in (
        "Starting",
        "Working",
        "Question",
        "Pre-commit failed",
        "Checks failing",
        "Setup failed",
        "Idle",
        "Waiting on limit",
        "Offline",
    ):
        assert '"%s"' % word in js, word
    assert '", resumes " + fmtClock(resumeAt)' in js


def test_tone_names_are_the_rails_and_styled():
    js, css = _js(), _css()
    table = js.split("  var SESSION_STATES = {")[1].split("};")[0]
    tones = re.findall(r'tone: "(\w+)"', table)
    assert sorted(tones) == sorted(
        ["muted", "working", "attention", "neutral", "waiting", "broken"]
    )
    for t in tones:
        assert ".dot.tone-%s" % t in css, t
        assert ".tone-%s " % t in css, t
        assert "--tone-%s:" % t in css, t
    # The header dot, the chip and the row words read the same object.
    dot = js.split("  function updateDot() {")[1].split("\n  }\n")[0]
    assert 'dotEl.className = "dot tone-" + ss.tone' in dot
    assert 'stateChipEl.className = "state-chip tone-" + ss.tone' in dot
    assert 'word.className = "sess-state tone-" + ss.tone' in js


def _node():
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_session_state_reads_like_the_rail():
    """Run the phone's sessionState over sample rows and compare words/tones."""
    js = _js()
    start = js.index("  var SESSION_STATES = {")
    end = js.index("  // O1: attention rank")
    attn_end = js.index("  function poll() {")
    src = js[start:end] + js[end:attn_end]
    rows = {
        "loading": {"status": "loading"},
        "working": {"activity": "working"},
        "clarify": {"activity": "clarify"},
        "precommit": {"activity": "idle", "stage": "interrupt"},
        "checks": {"activity": "idle", "check": {"state": "failed"}},
        "setup": {"setup": {"state": "failed"}},
        "idle": {"activity": "idle"},
        "limit": {"activity": "limit"},
        "offline": {"activity": "offline"},
        "paused": {"status": "paused", "activity": "offline"},
        "missing": {"workspace_missing": True},
    }
    script = (
        src
        + "\nvar rows = "
        + json.dumps(rows)
        + ";\nvar out = {};\nfor (var k in rows) { var s = sessionState(rows[k]);"
        " out[k] = [s.label, s.tone, s.restartable, attnRank(rows[k])]; }\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["loading"][:2] == ["Starting", "muted"]
    assert out["working"][:2] == ["Working", "working"]
    assert out["clarify"][:2] == ["Question", "attention"] and out["clarify"][3] == 0
    assert out["precommit"][:2] == ["Pre-commit failed", "attention"]
    assert out["checks"][:2] == ["Checks failing", "attention"]
    assert out["checks"][3] == 2
    assert out["setup"][:2] == ["Setup failed", "attention"]
    assert out["idle"][:2] == ["Idle", "neutral"] and out["idle"][3] == 9
    assert out["limit"][:2] == ["Waiting on limit", "waiting"]
    # Offline is the one with Restart, and it is in the bell (ranked).
    assert out["offline"] == ["Offline", "broken", True, 1]
    # Outside the table, as on the rail.
    assert out["paused"][:3] == ["Paused", "muted", False]
    assert out["missing"][:3] == ["Missing", "broken", False]


# --------------------------------------------------------------------------- #
# 3. Session actions
# --------------------------------------------------------------------------- #
def test_actions_sheet_markup_and_entry_points():
    js, html = _js(), _html()
    for el in ("sessions-sheet", "act-sheet", "recent-sheet", "more-btn", "picker"):
        assert 'id="%s"' % el in html, el
    # The picker is a button now (an <option> carries no state and no ⋯).
    assert '<select id="picker"' not in html
    assert 'pickerEl.addEventListener("click", openSessionsSheet);' in js
    # Header ⋯ → the current session; a row's ⋯ and a long press → that row's.
    assert (
        'getElementById("more-btn").addEventListener("click", function () { openActSheet(current); });'
        in js
    )
    row = js.split("  function sessionRow(inst) {")[1].split("\n  }\n")[0]
    assert (
        'more.addEventListener("click", function () { openActSheet(title); });' in row
    )
    assert "LONG_PRESS_MS" in row and '"contextmenu"' in row
    assert "openActSheet(title);" in row


def test_actions_hit_the_rails_routes():
    js = _js()
    assert 'postFor(title, "close", {})' in js
    assert 'postFor(title, "cleanup", {})' in js
    # No Pause on the phone either (owner: "drop the pause button entirely");
    # the palette, the CLI and the API still pause.
    assert 'sessionCall(title, "pause",' not in js
    assert 'sessionCall(title, "resume",' in js
    assert 'sessionCall(title, "revive",' in js
    call = js.split("  function sessionCall(")[1].split("\n  }\n")[0]
    assert "postFor(title, route, {})" in call
    post = js.split("  function postFor(")[1].split("\n  }\n")[0]
    assert '"/api/instances/" + encodeURIComponent(title) + "/" + action' in post
    assert 'method: "POST"' in post
    assert 'fetch("/api/recently-closed")' in js
    assert '"/api/recently-closed/" + encodeURIComponent(ent.id) + "/reopen"' in js


def test_action_words_are_the_rails():
    js = _js()
    assert '"Close", "stops the agent and moves it to Recently closed"' in js
    assert '"Closed — reopen from Recently closed"' in js
    assert '"Delete…"' in js
    assert '"Deletes the folder and branch, including uncommitted changes.' in js
    # No Pause row; Resume only on a paused session.
    assert '"Pause", "stops the agent' not in js
    assert '"Resume session"' in js
    assert '"Restart"' in js
    # Restart is offered only where the rail offers it (offline, restartable).
    render = js.split("  function renderActSheet() {")[1].split("\n  }\n")[0]
    assert "if (ss.restartable)" in render
    assert 'if (inst.status === "paused")' in render


def test_delete_asks_inline_and_says_who_holds_the_folder():
    js = _js()
    ask = js.split("  function askDelete(title, btn) {")[1].split("\n  }\n")[0]
    assert "cancel.focus();" in ask  # a second tap never deletes
    assert "folderInUse(err)" in ask and "folderInUseText(held)" in ask
    assert '"Folder in use by "' in js and '". Close those first."' in js
    assert 'err.body.error !== "folder in use"' in js
    assert "err.body.sessions" in js


# --------------------------------------------------------------------------- #
# Push / PR / Merge gates
# --------------------------------------------------------------------------- #
def test_gates_are_explained_on_the_sheet():
    js = _js()
    assert "err.body && err.body.red_zone_breaches" in js
    assert "err.status !== 409" in js
    assert '" blocked — red zone changed"' in js
    assert '" blocked — changes outside the green zone"' in js
    for verb in ('"Push"', '"Open PR"', '"Merge"'):
        assert "explainRedZone(title, %s, rz, function () {" % verb in js, verb
    # The O3 check gate resends with force, for the session it was asked about.
    push = js.split("  function doPush(")[1].split("\n  }\n")[0]
    assert '"Push blocked — checks haven\'t passed"' in push
    assert "doPush(true, title, overrideRz);" in push


def test_stage_resets_after_a_pr_opens():
    pr = _js().split("  function doMakePr(")[1].split("\n  }\n")[0]
    assert 'postFor(title, "reset-stage", {})' in pr
    # Only on a real PR — not on the compare-page fallback.
    assert pr.index("j.ok === false") < pr.index('"reset-stage"')


# --------------------------------------------------------------------------- #
# 4. Labels
# --------------------------------------------------------------------------- #
def test_terminal_not_shell():
    html = _html()
    assert 'data-tab="shell">Terminal</button>' in html
    assert ">Shell<" not in html


def test_new_sheet_buttons_are_the_desktops():
    html, js = _html(), _js()
    # The desktop's action row (NewSessionDialog's FooterActions).
    dialog = (
        ROOT
        / "frontend"
        / "src"
        / "components"
        / "dialogs"
        / "newSession"
        / "FooterActions.tsx"
    ).read_text()
    run_start = (ROOT / "frontend" / "src" / "lib" / "runStart.ts").read_text()
    # Round 5: Start is the one primary; the agent-assisted path and the
    # full form are secondary (r5-new #2).
    assert "Let the agent set it up…" in dialog
    assert "More options" in dialog
    assert 'return "Create session";' in run_start
    assert '<button type="button" id="new-go">Let the agent set it up…</button>' in html
    assert '<button type="button" id="new-start">Start</button>' in html
    assert ">More options</button>" in html
    assert 'var NEW_GO_LABEL = "Let the agent set it up…";' in js
    assert '"Continue"' not in js and '"Start session"' not in js
