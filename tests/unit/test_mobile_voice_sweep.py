"""The phone page's side of the voice sweep (2026-11): the router's new
kinds (``scroll`` / ``interrupt`` done on the server and captioned here,
``switch_back`` / ``where`` / ``commands`` the page's own), the ``undo``
radio word ("scratch that"), and ``heard`` — Moonshine's text of the phrase
— sent to ``/api/voice/route``. The matchers and helpers run as written
under node (the slices test_mobile_voice_words.py runs)."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.unit.test_mobile_voice_words import _js, _matchers

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _whole(js: str, name: str) -> str:
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return js[start:end]


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _harness(js: str) -> str:
    return "var voice = { prefs: {} };\n" + _matchers(js) + "voiceWordsApply(null);\n"


@needs_node
def test_local_table_back_where_and_no_scratch_that():
    js = _js()
    out = _run(_harness(js) + """
var out = {};
["scratch that", "go back", "back", "last session", "the one before", "where am I",
 "which session is this", "never mind", "previous session"].forEach(function (t) {
  var c = voiceCommand(t); out[t] = c ? c.kind : null;
});
console.log(JSON.stringify(out));
""")
    assert out == {
        "scratch that": None,
        "go back": "switch_back",
        "back": "switch_back",
        "last session": "switch_back",
        "the one before": "switch_back",
        "where am I": "where",
        "which session is this": "where",
        "never mind": "cancel",
        "previous session": "switch_rel",
    }


@needs_node
def test_may_be_command_holds_the_new_commands_whole():
    js = _js()
    out = _run(_harness(js) + _whole(js, "voiceMayBeCommand") + """
var out = {};
["scroll up", "page down", "back to the bottom", "more", "keep going", "interrupt",
 "press escape", "escape", "where am i", "what can i say", "help", "fix the login bug",
 "add a test for the parser"].forEach(function (t) { out[t] = voiceMayBeCommand(t); });
console.log(JSON.stringify(out));
""")
    assert out.pop("fix the login bug") is False
    assert out.pop("add a test for the parser") is False
    assert all(out.values()), out


@needs_node
def test_undo_parse_reads_the_words_at_the_end():
    js = _js()
    out = _run(_harness(js) + _whole(js, "voiceUndoParse") + """
var out = {};
["scratch that", "Scratch that.", "fix the login bug, scratch that", "undo that", "can you undo that",
 "scratch that idea and start over", "fix the bug"].forEach(function (t) { out[t] = voiceUndoParse(t); });
voiceWordsApply({ undo: ["strike that"] });
out.custom = [voiceUndoParse("strike that"), voiceUndoParse("scratch that")];
voiceWordsApply({});
out.missing = voiceUndoParse("scratch that");
console.log(JSON.stringify(out));
""")
    assert out["scratch that"] == {"before": ""}
    assert out["Scratch that."] == {"before": ""}
    assert out["fix the login bug, scratch that"] == {"before": "fix the login bug"}
    # "undo that" is no default: "can you undo that" is words for the agent
    assert out["undo that"] is None
    assert out["can you undo that"] is None
    assert out["scratch that idea and start over"] is None
    assert out["fix the bug"] is None
    # the owner's own undo words; an older server without the field: ours
    assert out["custom"] == [{"before": ""}, None]
    assert out["missing"] == {"before": ""}


@needs_node
def test_a_guess_never_types_the_undo_words():
    js = _js()
    out = _run(_harness(js) + """
console.log(JSON.stringify([voiceHoldBack("fix the bug scratch"), voiceHoldBack("fix the bug scratch that")]));
""")
    assert out == [["fix the bug", "scratch"], ["fix the bug", "scratch that"]]


@needs_node
def test_undo_takes_back_the_last_phrase_or_the_phrase_itself():
    js = _js()
    out = _run(
        _harness(js)
        + """
var composeEl = { value: "" }, notes = [], continued = 0;
function vbox() { return composeEl; }
function vboxSized() {}
function autosizeCompose() {}
function voiceStripNote(w) { notes.push(w); }
function voiceListenContinue() { continued++; }
"""
        + _whole(js, "voiceAppend")
        + _whole(js, "voiceUndoParse")
        + _whole(js, "voiceUndo")
        + """
var out = {};
voiceAppend("fix the login bug");
voiceAppend("and add a test");
voiceUndo(voiceUndoParse("scratch that"));
out.alone = composeEl.value;
voiceUndo(voiceUndoParse("rename the module, scratch that"));
out.inPhrase = composeEl.value;   // its own words never went in; the box is untouched
voiceUndo(voiceUndoParse("scratch that"));   // nothing appended since: nothing to take
out.twice = composeEl.value;
composeEl.value = "";            // sent
voiceAppend("one");
composeEl.value = "edited by hand";
voiceUndo(voiceUndoParse("scratch that"));
out.edited = composeEl.value;
out.notes = notes;
out.continued = continued;
console.log(JSON.stringify(out));
"""
    )
    assert out["alone"] == "fix the login bug"
    assert out["inPhrase"] == "fix the login bug"
    assert out["twice"] == "fix the login bug"
    assert out["edited"] == "edited by hand"
    assert out["notes"] == [
        "Taken back",
        "Taken back",
        "Nothing to take back",
        "Nothing to take back",
    ]
    assert out["continued"] == 4


def test_voice_heard_checks_undo_before_the_radio_words():
    js = _js()
    body = _whole(js, "voiceHeard")
    assert body.index("voiceUndoParse(text)") < body.index(
        "var p = voiceDictParse(text);"
    )
    assert 'mode !== "dialog" && voiceUndoParse(text)' in body


@needs_node
def test_acted_words_are_one_or_two_words_and_no_speech():
    js = _js()
    out = _run(
        """
var voice = { session: "docs" }, current = "docs";
function nameOf(t) { return t === "docs" ? "API docs" : t; }
"""
        + _whole(js, "voiceActedWord")
        + """
console.log(JSON.stringify([
  voiceActedWord({ kind: "scroll", dir: "up", amount: "page", ok: true, title: "docs" }),
  voiceActedWord({ kind: "scroll", dir: "down", amount: "half", ok: true, title: "docs" }),
  voiceActedWord({ kind: "scroll", dir: "up", amount: "top", ok: true, title: "docs" }),
  voiceActedWord({ kind: "scroll", dir: "down", amount: "bottom", ok: true, title: "docs" }),
  voiceActedWord({ kind: "scroll", dir: "up", amount: "page", ok: false, title: "docs" }),
  voiceActedWord({ kind: "interrupt", ok: true, title: "docs" }),
  voiceActedWord({ kind: "interrupt", ok: true, title: "parser" }),
  voiceActedWord({ kind: "interrupt", ok: false, title: "docs" }),
]));
"""
    )
    assert out == [
        "Scrolled up",
        "Scrolled down",
        "At the top",
        "At the bottom",
        "Couldn't scroll",
        "Interrupted",
        "Interrupted parser",
        "Couldn't interrupt",
    ]
    acted = _whole(js, "voiceActed")
    assert "speakLine" not in acted and "voiceNod" not in acted
    assert 'voiceChime("done")' in acted


def test_routed_scroll_and_interrupt_keep_listening():
    js = _js()
    routed = _whole(js, "voiceRouted")
    case = routed[routed.index('case "scroll":') : routed.index('case "commands":')]
    assert "voiceActed(res)" in case and "return false" in case
    assert "voiceStopForCommand" not in case
    for kind in ("scroll", "interrupt", "switch_back", "where", "commands"):
        assert (
            '"%s"' % kind
            in js[js.index("var VOICE_ROUTE_KINDS") : js.index("var VOICE_PENDING_MS")]
        )
    run = _whole(js, "voiceRunCommand")
    assert 'case "switch_back": voiceSwitchBack()' in run
    assert 'case "where": voiceWhere()' in run


def test_select_remembers_the_previous_session():
    js = _js()
    sel = _whole(js, "select")
    assert "if (current && title && title !== current) previousSel = current;" in sel
    assert sel.index("previousSel = current") < sel.index("current = title;")
    back = _whole(js, "voiceSwitchBack")
    assert "voiceSwitchTo(previousSel)" in back


@needs_node
def test_switch_back_and_where_say_the_session():
    js = _js()
    out = _run(
        """
var said = [], switched = [], current = "docs", previousSel = "parser", voice = { session: "docs" };
function instByTitle(t) { return t === "docs" || t === "parser" ? { title: t } : null; }
function nameOf(t) { return t === "docs" ? "API docs" : t; }
function endSentence(s) { return /[.!?]$/.test(s) ? s : s + "."; }
function voiceSayThenIdle(l) { said.push(l); }
function voiceSwitchTo(t) { switched.push(t); }
"""
        + _whole(js, "voiceSwitchBack")
        + _whole(js, "voiceWhere")
        + """
voiceSwitchBack(); voiceWhere();
previousSel = null; voiceSwitchBack();
previousSel = "gone"; voiceSwitchBack();
voice.session = null; current = null; voiceWhere();
console.log(JSON.stringify({ said: said, switched: switched }));
"""
    )
    assert out["switched"] == ["parser"]
    assert out["said"] == [
        "API docs.",
        "There's no session to go back to.",
        "There's no session to go back to.",
        "No session is open.",
    ]


@needs_node
def test_heard_rides_with_its_own_phrase_only():
    js = _js()
    out = _run(
        _harness(js)
        + _whole(js, "voiceHeardFor").replace(
            "  function voiceHeardFor(",
            "var VOICE_HEARD_MAX_MS = 15000;\n  function voiceHeardFor(",
        )
        + """
var out = {};
voice.heardEnd = { final: "School up, over.", heard: "scroll up over", at: Date.now() };
out.cut = voiceHeardFor("School up");
out.whole = voiceHeardFor("School up, over.");
out.other = voiceHeardFor("fix the login bug");
voice.heardEnd.at = Date.now() - 60000;
out.old = voiceHeardFor("School up");
voice.heardEnd = null;
out.none = voiceHeardFor("School up");
console.log(JSON.stringify(out));
"""
    )
    assert out == {
        "cut": "scroll up",
        "whole": "scroll up",
        "other": "",
        "old": "",
        "none": "",
    }
    body = _whole(js, "voiceRouteBody")
    assert "var heard = voiceHeardFor(text);" in body and "body.heard = heard" in body
    assert "voice.heardEnd = typeof x.j.heard" in js


def test_commands_sheet_is_built_with_text_never_html_from_the_server():
    js = _js()
    show = _whole(js, "voiceCommandsShow")
    assert "textContent" in show
    # The items are the server's words: never written as markup.
    assert "it.say +" not in show.split("innerHTML")[1].split(";")[0]
    assert "openSheet(voiceCmdSheet)" in show
    assert "voiceSayThenIdle" in show
