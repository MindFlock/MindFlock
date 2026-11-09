"""Sentence context on the phone page (2026-11, "isn't super great with
context of sentences"): each phrase's stream opens with the message's words
already said ahead of it — ``POST /api/voice/stream {context, session}`` —
which the server hands whisper as its prompt.

The helpers run as written under node (the slices test_mobile_voice_words.py
runs): the cap (``VOICE_CONTEXT_MAX`` at a word boundary), the open's body,
and ``voiceStreamContext`` — the box's text, the phrase that just ended (its
final not typed yet) counted as heard, never a control word, nothing after
a send or a clear. The wiring into ServerSTT is pinned by source; the
headless harness (test_mobile_voice_browser.py) checks the real opens."""

from __future__ import annotations

import json
import re
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


def _inner(js: str, name: str) -> str:
    """A function nested in an adapter (four-space indent)."""
    start = js.index("    function " + name + "(")
    end = js.index("\n    }\n", start) + len("\n    }\n")
    return js[start:end]


def _harness(js: str) -> str:
    stt = _stt(js)
    cap = re.search(r"    var VOICE_CONTEXT_MAX = \d+;\n", stt).group(0)
    return (
        "var voice = { prefs: {}, box: null, session: 'Login fix', guess: { box: null, typed: '' } };\n"
        "var composeEl = { value: '' };\n"
        "function vbox() { return voice.box || composeEl; }\n"
        "var VOICE_CLIENT_ID = 'm-1';\n"
        "function lang() { return 'en-US'; }\n"
        + _matchers(js)
        + "voiceWordsApply(null);\n"
        + cap
        + _inner(stt, "contextTail")
        + _inner(stt, "streamBody")
        + "".join(
            _whole(js, n)
            for n in (
                "voiceJoin",
                "voiceUndoParse",
                "voiceGuessBase",
                "voiceStreamContext",
            )
        )
    )


def _stt(js: str) -> str:
    return js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]


@needs_node
def test_the_cap_is_400_at_a_word_boundary():
    out = _run(_harness(_js()) + """
var words = []; for (var i = 0; i < 120; i++) words.push("word" + i);
var long = words.join(" ");
var tail = new Array(200).join("a") + " " + new Array(201).join("b");
console.log(JSON.stringify({
  max: VOICE_CONTEXT_MAX,
  long: contextTail(long), text: long,
  exact: contextTail(new Array(51).join("x") + " " + tail), tail: tail,
  short: contextTail("  fix the\\n login   bug "),
  // the last 400 are "one zzz…": after a space they stand; after "q"
  // ("qone"), "one" is a broken word, left out whole
  space: contextTail("wwww one " + new Array(397).join("z")),
  broken: contextTail("wwww qone " + new Array(397).join("z")),
  z396: new Array(397).join("z"),
}));
""")
    assert out["max"] == 400
    assert len(out["long"]) <= 400 and len(out["long"]) > 390
    assert out["text"].endswith(out["long"])
    # starts on a whole word: the character before it is a space
    assert out["text"][len(out["text"]) - len(out["long"]) - 1] == " "
    assert out["long"].endswith("word119")
    assert out["exact"] == out["tail"] and len(out["exact"]) == 400
    assert out["short"] == "fix the login bug"
    assert out["space"] == "one " + out["z396"] and len(out["space"]) == 400
    assert out["broken"] == out["z396"]


@needs_node
def test_the_open_body_carries_context_and_session_only_when_there_are_any():
    out = _run(_harness(_js()) + """
console.log(JSON.stringify([
  streamBody({ context: null }),
  streamBody({ context: { context: "", session: "" } }),
  streamBody({ context: { context: " fix the bug ", session: "Login fix" } }),
  streamBody({ context: { context: new Array(201).join("word ") } }).context.length,
]));
""")
    assert out[0] == {"lang": "en-US", "client": "m-1"}
    assert out[1] == {"lang": "en-US", "client": "m-1"}
    assert out[2] == {
        "lang": "en-US",
        "client": "m-1",
        "context": "fix the bug",
        "session": "Login fix",
    }
    assert out[3] <= 400


@needs_node
def test_stream_context_reads_the_box_and_the_phrase_just_ended():
    out = _run(_harness(_js()) + """
var o = {};
// A fresh message: an empty box, none.
o.fresh = voiceStreamContext("prompt", "");
// The box holds phrase one (typed): the next phrase opens with it.
composeEl.value = "fix the login bug";
o.typed = voiceStreamContext("prompt", "");
// Phrase two just ended, its guess in the box, its final not typed yet:
// its words as heard stand in for the guess.
composeEl.value = "fix the login bug and add a";
voice.guess = { box: composeEl, typed: " and add a" };
o.carried = voiceStreamContext("prompt", "and add a test");
// Its held-back start of a control phrase is never context.
o.held = voiceStreamContext("prompt", "and add a test stop");
// It ended in "over" / "over and out" / "disregard": the next starts afresh.
o.over = voiceStreamContext("prompt", "and add a test over");
o.out = voiceStreamContext("prompt", "and add a test over and out");
o.clear = voiceStreamContext("prompt", "disregard");
// "scratch that": the phrase is taken back, the box before it stands.
o.undo = voiceStreamContext("prompt", "no wait scratch that");
// A stop phrase: the words before it are kept, never the phrase.
o.stop = voiceStreamContext("prompt", "that's all");
// After a send (the box emptied) or a clear: what is left — nothing.
voice.guess = { box: null, typed: "" };
composeEl.value = "";
o.sent = voiceStreamContext("prompt", "");
// A send on its way: the box still holds the message until /send answers.
composeEl.value = "fix the login bug";
voice.state = "sending";
o.sending = voiceStreamContext("prompt", "");
voice.state = "listening";
// A dialog's answer or an answer listen: no context at all.
composeEl.value = "fix the login bug";
o.dialog = voiceStreamContext("dialog", "");
o.answer = voiceStreamContext("answer", "");
// The ask sheet's field: its own text.
voice.box = { value: "use the staging db" };
o.sheet = voiceStreamContext("prompt", "");
console.log(JSON.stringify(o));
""")
    s = "Login fix"
    assert out["fresh"] == {"context": "", "session": s}
    assert out["typed"] == {"context": "fix the login bug", "session": s}
    assert out["carried"]["context"] == "fix the login bug and add a test"
    assert out["held"]["context"] == "fix the login bug and add a test"
    assert out["over"]["context"] == ""
    assert out["out"]["context"] == ""
    assert out["clear"]["context"] == ""
    assert out["undo"]["context"] == "fix the login bug"
    assert out["stop"]["context"] == "fix the login bug"
    assert out["sent"] == {"context": "", "session": s}
    assert out["sending"] == {"context": "", "session": s}
    assert out["dialog"] is None
    assert out["answer"] is None
    assert out["sheet"]["context"] == "use the staging db"


def test_server_stt_opens_every_stream_with_its_context():
    js = _js()
    stt = _stt(js)
    open_ = _inner(stt, "streamOpen")
    assert (
        "body: JSON.stringify(g.context ? streamBody(g) : "
        "{ lang: lang(), client: VOICE_CLIENT_ID })," in open_
    )
    # a fresh start asks the handlers with no phrase before it; a carried
    # phrase (opened as the last one ended) with that phrase's words
    assert (
        'g.context = contextOf(h, "");\n      if (streams()) streamOpen(g, mine);' in js
    )
    end = _inner(stt, "recEnd")
    assert (
        'g.nextContext = contextOf(mine.h, voiceJoin(g.pend, g.partial) || text || "");\n'
        "        carryFrom(g, key);" in end
    )
    assert "c.context = g.nextContext || null;" in _inner(stt, "carryFrom")
    # the state machine's handler, per listen mode
    assert "context: function (prev) { return voiceStreamContext(mode, prev); }," in js
    # the warm-up open stays bare
    warm = _inner(stt, "warmStt")
    assert "body: JSON.stringify({ lang: lang(), client: VOICE_CLIENT_ID })" in warm
