"""The phone (/m) speaks one line at a time and reads what it should: the
pure rules behind it, run as-is under node — the speech queue's step
(``voiceQueueStep``: arrival order, hold while listening, the one-minute
drop), the name rule (``voiceLineText``: another session's name first, the
phone's own bare), Everything's turn rule (``voiceTurnLine``: every session,
once per reply, never voice's own while voice is on), the Spoken replies
setting read from storage (``voiceRepliesPref``: an older boolean), and the
select question's spoken picks (``askSelRouted``, ``askSelByWords``). The
behaviour in a browser is walked by ``test_mobile_voice_browser.py``."""

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


def _whole(js: str, name: str) -> str:
    """``function name(…) { … }`` as written, for node."""
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return js[start:end]


def _run(script: str) -> dict:
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _fns(*names: str) -> str:
    js = _js()
    return "".join(_whole(js, n) for n in names)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_queue_step_order_hold_and_age():
    script = _fns("voiceQueueStep") + """
var q = [{text: "a", at: 1000}, {text: "b", at: 2000}, {text: "c", at: 70500}];
var o = {playing: false, hold: false, maxAge: 60000};
function names(r) { return {next: r.next ? r.next.text : null, drop: r.drop.map(function (x) { return x.text; })}; }
console.log(JSON.stringify({
  first: names(voiceQueueStep(q, 5000, o)),
  playing: names(voiceQueueStep(q, 5000, {playing: true, hold: false, maxAge: 60000})),
  hold: names(voiceQueueStep(q, 5000, {playing: false, hold: true, maxAge: 60000})),
  late: names(voiceQueueStep(q, 62500, o)),
  allOld: names(voiceQueueStep(q, 200000, o)),
  empty: names(voiceQueueStep([], 5000, o)),
}));
"""
    out = _run(script)
    # Strict arrival order: the oldest line first.
    assert out["first"] == {"next": "a", "drop": []}
    # Nothing while a line plays or the mic listens (and nothing dropped then).
    assert out["playing"] == {"next": None, "drop": []}
    assert out["hold"] == {"next": None, "drop": []}
    # Older than a minute when its turn comes: dropped, the next fresh one said.
    assert out["late"] == {"next": "c", "drop": ["a", "b"]}
    assert out["allOld"] == {"next": None, "drop": ["a", "b", "c"]}
    assert out["empty"] == {"next": None, "drop": []}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_line_text_names_only_another_session():
    script = _fns("voiceNorm", "voiceLineText") + """
var NAMES = {"login-fix": "Login fix", "invoice-parser": "Invoice parser"};
function name(t) { return NAMES[t] || t; }
console.log(JSON.stringify([
  voiceLineText({text: "Fixed it.", title: "login-fix"}, "login-fix", name),
  voiceLineText({text: "Parsed twelve files.", title: "invoice-parser"}, "login-fix", name),
  voiceLineText({text: "Invoice parser: done.", title: "invoice-parser"}, "login-fix", name),
  voiceLineText({text: "Sent to API docs.", title: ""}, "login-fix", name),
  voiceLineText({text: "  Spaced.  ", title: "invoice-parser"}, "", name),
]));
"""
    assert _run(script) == [
        "Fixed it.",
        "Invoice parser: Parsed twelve files.",
        "Invoice parser: done.",
        "Sent to API docs.",
        "Invoice parser: Spaced.",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_everything_reads_every_session_once():
    script = _fns("voiceTurnLine") + """
function f(ev, t, say, extra) {
  var x = {event: ev, session: t, data: {say: say}};
  if (ev === "session.activity_changed") x.new = extra || "idle";
  return x;
}
var heard = {};
var out = {};
out.other = voiceTurnLine(f("session.activity_changed", "b", "Done."), "all", "a", heard);
heard.b = "Done.";
out.again = voiceTurnLine(f("session.turn_ended", "b", "Done."), "all", "a", heard);
out.newWords = voiceTurnLine(f("session.turn_ended", "b", "Tests pass."), "all", "a", heard);
out.voiceOwn = voiceTurnLine(f("session.turn_ended", "a", "Mine."), "all", "a", heard);
out.ownVoiceOff = voiceTurnLine(f("session.turn_ended", "a", "Mine."), "all", "", heard);
out.mine = voiceTurnLine(f("session.turn_ended", "c", "Hi."), "mine", "", heard);
out.off = voiceTurnLine(f("session.turn_ended", "c", "Hi."), "off", "", heard);
out.noSay = voiceTurnLine(f("session.turn_ended", "c", ""), "all", "", heard);
out.working = voiceTurnLine(f("session.activity_changed", "c", "Hi.", "working"), "all", "", heard);
out.question = voiceTurnLine({event: "session.question", session: "c", data: {say: "Hi?"}}, "all", "", heard);
console.log(JSON.stringify(out));
"""
    out = _run(script)
    assert out["other"] == {"title": "b", "say": "Done."}
    assert out["again"] is None  # idle and turn_ended carry the same reply
    assert out["newWords"] == {"title": "b", "say": "Tests pass."}
    assert out["voiceOwn"] is None  # voiceReply reads voice's own session
    assert out["ownVoiceOff"] == {"title": "a", "say": "Mine."}
    for k in ("mine", "off", "noSay", "working", "question"):
        assert out[k] is None, k
    # A new turn ("working") clears the memory, so the same words are read again.
    on_turn = _whole(_js(), "voiceOnTurn")
    assert 'f.new === "working"' in on_turn and "delete voice.heard[t];" in on_turn
    assert "speakLine(line.say, true, line.title, at);" in on_turn


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_replies_pref_reads_an_older_boolean():
    script = _fns("voiceRepliesPref") + """
console.log(JSON.stringify([true, false, undefined, null, "off", "mine", "all", "bogus"].map(voiceRepliesPref)));
"""
    assert _run(script) == ["mine", "off", "mine", "mine", "off", "mine", "all", "mine"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_select_spoken_picks():
    js = _js()
    strip = _whole(js, "stripMarkdown")
    script = (
        _fns("voiceNorm", "askOptKeys", "askSelRouted", "askSelByWords") + strip + """
var OPTS = [{key: "1", label: "Postgres", kind: "other", keys: ["1"]},
            {key: "2", label: "SQLite", kind: "other", keys: ["Up", "2"]},
            {key: "3", label: "Type something", kind: "text", keys: ["3"]}];
function k(o) { return o ? o.key : null; }
console.log(JSON.stringify({
  routedKeys: k(askSelRouted({kind: "answer", keys: ["Up", "2"]}, OPTS)),
  routedNumber: k(askSelRouted({kind: "answer", number: 1}, OPTS)),
  routedPrompt: k(askSelRouted({kind: "prompt"}, OPTS)),
  routedNull: k(askSelRouted(null, OPTS)),
  words: k(askSelByWords("let's use postgres please", OPTS)),
  wordsCase: k(askSelByWords("SQLite.", OPTS)),
  wordsPart: k(askSelByWords("post", OPTS)),
  wordsNone: k(askSelByWords("mysql", OPTS)),
}));
"""
    )
    out = _run(script)
    # The router's keys match the option's own keys (a cursor on the text
    # row makes them ["Up", "2"]); its number, the option's key.
    assert out["routedKeys"] == "2"
    assert out["routedNumber"] == "1"
    assert out["routedPrompt"] is None and out["routedNull"] is None
    # An option named by its whole label, any case or punctuation.
    assert out["words"] == "1" and out["wordsCase"] == "2"
    assert out["wordsPart"] is None and out["wordsNone"] is None


def test_speech_queue_wiring():
    js = _js()
    speak = _whole(js, "speakLine")
    assert "voiceSpeech.q.push(" in speak and "voiceSpeechPump();" in speak
    # Never a cancel of the line being said: queued, not superseded.
    assert "voice.tts.cancel()" not in speak
    pump = _whole(js, "voiceSpeechPump")
    assert "hold: voiceIsListening()" in pump
    assert "maxAge: VOICE_LINE_MAX_AGE_MS" in pump and "console.debug(" in pump
    assert "var VOICE_LINE_MAX_AGE_MS = 60000;" in js
    # A tap on the mic stops the line being said and holds the rest.
    assert "voiceSpeechStop(false);" in _whole(js, "voiceListen")
    # Cancel / voice off / hidden: every line goes.
    assert "voiceSpeechStop(true);" in _whole(js, "voiceCancelTap")
    assert "voiceSpeechStop(true);" in _whole(js, "voiceOff")
    # A listen after a read-out waits for the queue to drain.
    after = _whole(js, "voiceListenAfter")
    assert "voiceSpeechDrain(" in after
    for fn in ("voiceAfterReply", "voiceReadDialog", "voiceDialogRetry"):
        assert "voiceListenAfter(" in _whole(js, fn), fn
    # "repeat" says the last line as said.
    assert "voice.lastLine = said;" in pump
    assert "speakLine(voice.lastLine" in _whole(js, "voiceRunCommand")
    # Everything keeps the socket whatever voice is.
    assert 'voice.prefs.replies === "all"' in _whole(js, "voiceEventsWanted")
