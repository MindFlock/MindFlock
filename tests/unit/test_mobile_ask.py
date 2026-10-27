"""The phone (/m) answers "a session is asking you something": the row's
``asking`` reads as a Question (never Idle), and the ask sheet (mobile.js ASK
section) speaks the server contract — options → ``/answer`` with ``keys``, a
prose question → ``/send`` (dialog-safe), Later → ``/asking/dismiss``. The
behaviour itself is walked in headless Chrome by
``test_mobile_voice_browser.py``; these pin what that harness cannot see
without a browser."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _ask_section() -> str:
    js = _js()
    return js[
        js.index("  // ASK — a session is asking you something") : js.index(
            "  // ---- Update banner"
        )
    ]


def _fn(src: str, name: str) -> str:
    start = src.index("  function " + name + "(")
    end = src.index("\n  }\n", start)
    return src[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_asking_rows_read_as_a_question_and_rank_first():
    js = _js()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]
    rows = {
        "prose": {
            "title": "p",
            "activity": "idle",
            "asking": {"kind": "question", "text": "Add tests?", "ts": 1},
        },
        "dialog": {
            "title": "d",
            "activity": "working",
            "asking": {
                "kind": "dialog",
                "text": "Allow git push?",
                "ts": 2,
                "dialog_id": "x",
            },
        },
        "none": {"title": "n", "activity": "idle", "asking": None},
        "older": {"title": "o", "activity": "clarify"},
    }
    script = (
        "var localStorage = { getItem: function () { return null; }, setItem: function () {} };\n"
        + src
        + "\nvar rows = "
        + json.dumps(rows)
        + ";\nvar out = {};\n"
        "for (var k in rows) { var s = sessionState(rows[k]);"
        " out[k] = [s.label, s.hint, attnRank(rows[k])]; }\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["prose"] == ["Question", "The agent asked you a question: Add tests?", 0]
    assert out["dialog"] == [
        "Question",
        "The agent asked you a question: Allow git push?",
        0,
    ]
    assert out["none"][0] == "Idle" and out["none"][2] == 9
    assert out["older"][:2] == ["Question", "The agent asked you a question"]


def test_ask_sheet_speaks_the_server_contract():
    a = _ask_section()
    answer = _fn(a, "askAnswer")
    assert (
        'body: JSON.stringify({ keys: askOptKeys(opt), dialog_id: String(d.id), by: "user" })'
        in answer
    )
    assert "j.dialog_changed" in answer and "j.dialog_answered" in answer
    assert (
        "opt.keys && opt.keys.length ? opt.keys.map(String) : [String(opt.key)]"
        in _fn(a, "askOptKeys")
    )
    send = _fn(a, "askSend")
    assert "body: JSON.stringify({ text: text, dialog_safe: true })" in send
    assert "j.in_dialog" in send and "askInDialog(e.title)" in send
    later = _fn(a, "askLater")
    assert '"/asking/dismiss"' in later and 'body: "{}"' in later
    assert 'e.kind === "question"' in later  # a dialog is snoozed here, never dismissed
    assert "/dialog?quiet=1" in _fn(a, "askFetchDialog")
    assert 'fetch("/api/notify/ntfy")' in _fn(a, "askLoadSettings")
    assert "j.ask_popup" in _fn(a, "askLoadSettings")


def test_ask_sheet_open_rules_and_notifications():
    a = _ask_section()
    refresh = _fn(a, "askRefresh")
    # Never one put off, answered, already shown, or while typing into it.
    assert (
        "!ask.answered[e.key] && !ask.later[e.key] && !ask.seen[e.key] && !askTypingInto(e.title)"
        in refresh
    )
    assert "if (!ask.popup) return;" in refresh
    # The push's deep link opens before (and regardless of) the pop-up setting.
    assert refresh.index("askDeepLink()") < refresh.index("if (!ask.popup) return;")
    # A notification only for a hidden page; visible → a buzz, not while listening.
    announce = _fn(a, "askAnnounce")
    assert (
        'document.visibilityState !== "visible"' in announce
        and "askNotify(e)" in announce
    )
    assert "voiceIsListening()" in announce
    assert '"(" + n + ") " + ask.baseTitle' in _fn(a, "askTitleCount")
    # Dialogs first, then oldest first.
    assert 'a.kind === "dialog" ? -1 : 1' in _fn(a, "askOrder")


def test_ask_sheet_markup_and_no_native_dialogs():
    html = (STATIC / "mobile.html").read_text()
    sheet = html[html.index('<div id="asking-sheet"') :]
    assert 'class="sheet sheet-center hidden"' in sheet[:120]
    for el in (
        "asking-num",
        "asking-name",
        "asking-step",
        "asking-prev",
        "asking-next",
        "asking-kind",
        "asking-question",
        "asking-command",
        "asking-options",
        "asking-reply",
        "asking-text",
        "asking-mic",
        "asking-later",
        "asking-open",
        "asking-send",
        "asking-hear",
    ):
        assert 'id="' + el + '"' in sheet, el
    assert 'id="ask-notify-btn"' in html
    css = (STATIC / "mobile.css").read_text()
    centre = css[css.index(".sheet.sheet-center {") :].split("}")[0]
    assert "align-items: center;" in centre and "justify-content: center;" in centre
    # The desktop app has no prompt()/confirm()/alert(): every question is a sheet.
    a = _ask_section()
    for native in ("prompt(", "confirm(", "alert("):
        assert "window." + native not in a and "\n    " + native not in a


def test_docs_describe_the_phone_ask_sheet():
    doc = (ROOT / "docs" / "web-ui.md").read_text()
    phone = doc[doc.index("## Mobile UI (`/m`)") :]
    sec = phone[
        phone.index("### A session is asking you something") : phone.index(
            "### Voice: talking to a session"
        )
    ]
    for words in (
        "**Later**",
        "/asking/dismiss",
        "Notify me",
        "Home Screen",
        "ntfy",
        "1 of N",
    ):
        assert words in sec, words
