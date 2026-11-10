"""The phone's (/m) voice, polished: a streamed phrase's accurate final
(``POST /api/voice/stream/{id}/end`` → ``final`` / ``same``) is typed over
the fast words before the rules read it (``voiceStreamFinal``); the strip
says one word — *Listening…*, *Speaking…*, *Quiet.* — and never the words
heard or said (``voiceStripWord``); the server's voice speaks a line
sentence by sentence, the next one made while the one before plays
(``voiceSpeakSentences``, ``ServerTTS``). Pure rules run as-is under node;
the behaviour in a browser — the box replaced, the strip's DOM, the first
sound after the first sentence's fetch, cancel dropping the rest — is
walked by ``test_mobile_voice_browser.py``."""

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


def _inner(js: str, name: str) -> str:
    """A function nested in an adapter (four-space indent)."""
    start = js.index("    function " + name + "(")
    end = js.index("\n    }\n", start) + len("\n    }\n")
    return js[start:end]


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# --------------------------------------------------------------------------- #
# The accurate final
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_final_replaces_the_fast_words_only_when_it_differs():
    script = _whole(_js(), "voiceStreamFinal") + """
console.log(JSON.stringify([
  voiceStreamFinal("fix the bag", {final: "fix the bug", same: false, final_ms: 1200}),
  voiceStreamFinal("fix the bug", {final: "fix the bug", same: true, final_ms: 900}),
  voiceStreamFinal("fix the bag", {final: "fix the bug", same: true, final_ms: 900}),
  voiceStreamFinal("fix the bag", {committed: "bag"}),
  voiceStreamFinal("fix the bag", {final: "", same: false}),
  voiceStreamFinal(" fix the bug ", {final: "fix the bug "}),
  voiceStreamFinal("", {final: "submit"}),
]));
"""
    out = _run(script)
    # A different final: its words, and the box is corrected.
    assert out[0] == {"text": "fix the bug", "fixed": True, "ms": 1200}
    # `same`: nothing to retype (even if the strings differ, the server said so).
    assert out[1] == {"text": "fix the bug", "fixed": False, "ms": 900}
    assert out[2] == {"text": "fix the bag", "fixed": False, "ms": 900}
    # No final (an older server, a backend with one model): the fast words stand.
    assert out[3] == {"text": "fix the bag", "fixed": False, "ms": 0}
    # An empty final never wipes words the fast pass heard.
    assert out[4]["text"] == "fix the bag" and not out[4]["fixed"]
    # The same words (spacing aside): no retype.
    assert out[5] == {"text": "fix the bug", "fixed": False, "ms": 0}
    assert out[6] == {"text": "submit", "fixed": True, "ms": 0}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_only_the_accurate_passs_final_is_a_sample():
    # A Moonshine stream answers a control-only phrase (or a timed-out
    # pass) with Moonshine's own text: no sample for the "over" budget.
    script = _whole(_js(), "voiceTimedFinal") + """
console.log(JSON.stringify([
  voiceTimedFinal({final: "fix the bug", final_ms: 700}),
  voiceTimedFinal({final: "fix the bug", final_by: "whisper"}),
  voiceTimedFinal({final: "Stop.", final_by: "moonshine"}),
  voiceTimedFinal({committed: "bag"}),
  voiceTimedFinal(null),
]));
"""
    assert _run(script) == [True, True, False, False, False]
    end = _inner(_js(), "streamEnd")
    assert "if (voiceTimedFinal(x.j)) {" in end


def test_the_final_is_typed_over_the_phrase_before_the_rules_read_it():
    js = _js()
    end = _inner(js, "streamEnd")
    # The whole phrase (its committed words + the guess) is replaced through
    # onGuess — voiceGuessShow, bounded at the phrase's start, the caret at
    # the end — and only then does onFinal (radio words, router, dialogs) run
    # on the final words.
    assert (
        "var text = voiceJoin(g.pend, x.j.committed), fin = voiceStreamFinal(text, x.j);"
        in end
    )
    assert end.index("mine.h.onGuess(fin.text)") < end.index(
        "streamDone(g, mine, fin.text);"
    )
    assert "if (fin.fixed && token === mine && mine.h.onGuess)" in end
    # Diagnostics: "final 1.2 s" once, on the last transcribe line.
    assert "finalMs: fin.ms, fixed: fin.fixed" in end
    diag = _whole(js, "voiceDiagLast")
    assert '", final " + (x.finalMs / 1000).toFixed(1) + " s"' in diag


# --------------------------------------------------------------------------- #
# The strip: one word
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_strip_says_one_word():
    script = _whole(_js(), "voiceStripWord") + """
var states = ["off", "idle", "listening", "awaiting", "dialog", "sending", "working", "speaking"];
var out = {};
states.forEach(function (s) {
  out[s] = [voiceStripWord(s, false, ""), voiceStripWord(s, true, ""), voiceStripWord(s, false, "Quiet.")];
});
console.log(JSON.stringify(out));
"""
    out = _run(script)
    for s in ("listening", "awaiting", "dialog"):
        assert out[s][0] == "Listening…", s
    # The moment a prompt is on its way: the radio word was heard.
    assert out["sending"][0] == "Sending…"
    for s in ("off", "idle", "working", "speaking"):
        assert out[s][0] == "", s
    # A line playing says Speaking… whatever the state (a line with voice off).
    assert all(v[1] == "Speaking…" for v in out.values())
    # A note (a hush's "Quiet.", a closed window's "Heard nothing.") comes first.
    assert all(v[2] == "Quiet." for v in out.values())
    words = {w for v in out.values() for w in v}
    assert words == {"", "Listening…", "Speaking…", "Sending…", "Quiet."}
    note = _run(
        _whole(_js(), "voiceStripWord")
        + 'console.log(JSON.stringify([voiceStripWord("idle", false, "Heard nothing."), voiceStripWord("listening", true, "")]));'
    )
    assert note == ["Heard nothing.", "Speaking…"]


def test_the_strip_never_shows_the_words_heard_or_said():
    js = _js()
    cap = _whole(js, "voiceCaption")
    # The loop's caption is kept for the ask sheet and Diagnostics — never
    # drawn: only voiceStripPaint writes the strip, and only its one word.
    assert "voiceCaptionEl" not in cap and "voice.caption = String(text" in cap
    paint = _whole(js, "voiceStripPaint")
    assert "voiceCaptionEl.textContent = w;" in paint
    assert js.count("voiceCaptionEl.textContent =") == 1
    assert "voiceCaptionEl.appendChild" not in js and "voice-partial" not in js
    # Speaking spans the line: the queue's playing line, or the state.
    assert '!!voiceSpeech.playing || voice.state === "speaking"' in paint
    # Quiet. for VOICE_QUIET_CAPTION_MS after a hush, then nothing — one
    # mechanism (voiceStripNote) for every moment's word.
    q = _whole(js, "voiceStripNote")
    assert "voice.quietUntil = Date.now() + VOICE_QUIET_CAPTION_MS;" in q
    assert "setTimeout(voiceStripPaint, VOICE_QUIET_CAPTION_MS + 20);" in q
    assert 'voiceStripNote("Quiet.")' in _whole(js, "voiceQuiet")
    assert 'if (!heard) voiceStripNote("Heard nothing.");' in _whole(
        js, "voiceListenExpired"
    )
    assert 'Date.now() < voice.quietUntil ? voice.quietWord : ""' in paint
    # The ask sheet is unchanged: its mirror still reads the full caption.
    assert 'var cap = mine ? String(voice.caption || "") : "";' in _whole(
        js, "askVoiceMirror"
    )
    # Nothing grows: one line, ellipsised; the level meter overlays the
    # strip's top edge instead of taking a row.
    css = (STATIC / "mobile.css").read_text()
    rule = css.split("#voice-caption {")[1].split("}")[0]
    assert "white-space: nowrap;" in rule and "max-height" not in rule
    lvl = css.split("#voice-level {")[1].split("}")[0]
    assert "position: absolute;" in lvl and "margin-top" not in lvl
    assert "position: relative;" in css.split("#voice-strip {")[1].split("}")[0]


# --------------------------------------------------------------------------- #
# Speech by sentence
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_splitter():
    js = _js()
    script = (
        js[
            js.index("  var VOICE_SPEAK_MIN_CHARS") : js.index(
                "  function voiceSpeakSentences("
            )
        ]
        + _whole(js, "voiceSpeakSentences")
        + _whole(js, "sentenceCut")
        + """
var cases = [
  "Fixed the login bug in auth.py. The token check was off by one; it now passes. Should I push it?",
  "Use a flag, e.g. the dry run, before 3.5 ships. Then tag it.",
  "Pushed to the U.S. mirror, i.e. the one Dr. Smith runs. All good there.",
  "Done. Tests pass now.",
  "Fixed it all and pushed. Ok?",
  "Ok.",
  "",
  "It said \\"done.\\" Then it stopped! Odd, right?",
  new Array(30).join("word, ") + "end. Next one here.",
];
console.log(JSON.stringify(cases.map(voiceSpeakSentences)));
"""
    )
    out = _run(script)
    # . ? ! and ; end a sentence; a piece under 20 characters ("it now
    # passes.") joins the next — each request costs the voice its fixed time.
    assert out[0] == [
        "Fixed the login bug in auth.py.",
        "The token check was off by one;",
        "it now passes. Should I push it?",
    ]
    # "e.g." and "3.5" stay inside their sentence; a short last one joins the one before.
    assert out[1] == ["Use a flag, e.g. the dry run, before 3.5 ships. Then tag it."]
    # Abbreviations ("U.S.", "i.e.", "Dr.") never end one.
    assert out[2] == [
        "Pushed to the U.S. mirror, i.e. the one Dr. Smith runs. All good there."
    ]
    # A sentence under 20 characters joins the next…
    assert out[3] == ["Done. Tests pass now."]
    # …and the last one joins the one before it.
    assert out[4] == ["Fixed it all and pushed. Ok?"]
    assert out[5] == ["Ok."] and out[6] == []
    # Closing quotes stay with their sentence.
    assert out[7] == ['It said "done." Then it stopped! Odd, right?']
    assert "var VOICE_SPEAK_MIN_CHARS = 20;" in js
    # A sentence past 180 characters is cut at a comma.
    assert all(len(p) <= 181 for p in out[8]) and len(out[8]) >= 2


def test_the_server_voice_speaks_one_sentence_ahead():
    js = _js()
    start = js.index("  function ServerTTS(phone) {")
    tts = js[start : js.index("\n  }\n", start)]
    speak = _inner(tts, "speak")
    assert (
        "var parts = voiceSpeakSentences(text), keep = !!(opts && opts.cache);" in speak
    )
    # Sentences 1 and 2 are asked for at once, and from then two ahead of
    # the one playing (VOICE_SPEAK_AHEAD): a voice that makes one at a time
    # is never idle between sentences; a cancel wastes at most two.
    assert "var VOICE_SPEAK_AHEAD = 2;" in js
    said = _inner(tts, "said")
    assert "return said(ctx, parts, keep, g);" in speak
    assert (
        "function fill() { while (asked < parts.length && ahead.length < VOICE_SPEAK_AHEAD) ahead.push(fetchClip(ctx, parts[asked++], keep, cap)); }"
        in said
    )
    assert "var part = parts[i++], got = ahead.shift(), waitT = null;" in said
    assert said.index(
        "fill();   // the next sentences are made while this one plays"
    ) < said.index("(x.audio ? play(ctx, x.audio) : phone.speak(part)).then(")
    # A cancelled line stops before its next clip; cancel aborts the fetches.
    assert "if (g !== gen) { resolve(); return; }" in said
    cancel = _inner(tts, "cancel")
    assert "gen++;" in cancel and "a.abort();" in cancel
    # The running path still says the line through said(); only a context
    # that would not run hands it to the phone's voice.
    assert speak.index("if (!ok) {") < speak.index("return said(ctx, parts, keep, g);")
    # Two ways a line stops waiting, told apart: cancel aborts EVERY request
    # on its way; the timeout fallback in said() aborts none (the late clips
    # land in memory).
    assert (
        "aborts.splice(0).forEach(function (a) { try { a.abort(); } catch (e) {} });"
        in cancel
    )
    assert "abort" not in said
    assert "phone.speak(rest).then(next, next);" in said
    # No AudioContext running: the phone's voice says the whole line, once.
    assert "return phone.speak(text);" in speak
    # The phone's own voice reads the text shaped for it (spoken-text.js).
    assert "var parts = sentenceChunks(voiceShapeSpoken(text));" in _whole(
        js, "PhoneTTS"
    )
    # Diagnostics: the first sound's time and the sentence count.
    assert "{ first: first, parts: parts.length }" in said
    assert '", first sound " + (x.first / 1000).toFixed(1) + " s"' in _whole(
        js, "voiceDiagLast"
    )
    assert "sentenceGroups" not in js and "VOICE_SPEAK_MAX_CHARS" not in js


def test_the_phone_section_documents_the_polish():
    doc = (ROOT / "docs" / "web-ui.md").read_text()
    phone = " ".join(doc[doc.index("## Mobile UI (`/m`)") :].split())
    for words in (
        "`final`",
        "`same`",
        "*Listening…*",
        "*Speaking…*",
        "*Quiet.*",
        "sentence by sentence",
        "200 ms after",
        "listening starts **at once**",
        "constant thin red ring",
    ):
        assert words in phone, words


# =========================================================================== #
# Voice-command polish (2026-11-10): a spoken command triggers the first
# time on the phone, and a miss never passes silently.
# =========================================================================== #
GATE = json.loads(
    (ROOT / "tests" / "unit" / "data" / "voice_gate_phrases.json").read_text()
)


def _matchers_harness(js: str) -> str:
    """The words block and every matcher (test_mobile_voice_words' slices),
    with the fixture's sessions as the page's list."""
    from tests.unit.test_mobile_voice_words import _matchers

    rows = [{"title": n.replace(" ", "-"), "name": n} for n in GATE["sessions"]]
    return (
        "var voice = { prefs: {} }, instances = %s;\n" % json.dumps(rows)
        + "function nameOf(t) { for (var i = 0; i < instances.length; i++) if (instances[i].title === t) return instances[i].name; return t; }\n"
        + _matchers(js)
        + "voiceWordsApply(null);\n"
    )


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_every_route_and_want_phrase_is_held_whole_for_the_router():
    """Gate parity (voice_gate_phrases.json): every phrase the router takes
    — and every `want` phrasing but "pause", which stays words — may be a
    command at the phone's gate (voiceMayBeCommand), so its words are never
    guessed into the box while the router reads it; the fillers the router
    drops before a command ("um", "so", "okay so", "can you") are dropped."""
    js = _js()
    said = [p for p, _ in GATE["route"]] + [w for w in GATE["want"] if w != "pause"]
    out = _run(
        _matchers_harness(js)
        + "console.log(JSON.stringify(%s.filter(function (t) { return !voiceMayBeCommand(t); })));"
        % json.dumps(said)
    )
    assert out == []


@needs_node
def test_text_phrases_stay_words_even_without_the_router():
    """Every `text` phrase ends typed: the router says prompt (its own test),
    and with no router the phone's own table acts on none of them — "open
    the settings file…" is a focus only on a session named so — and none is
    read as said to MindFlock mid-message."""
    js = _js()
    out = _run(_matchers_harness(js) + """
function voiceStrictMatch(q) {
  q = voiceNorm(q).replace(/^the /, "");
  return instances.some(function (i) { return voiceNorm(i.name).indexOf(q) === 0; });
}
console.log(JSON.stringify(%s.map(function (t) {
  var c = voiceCommand(t);
  if (c && c.kind === "focus" && !voiceStrictMatch(c.arg)) c = null;
  return [t, c ? c.kind : null, voiceWakeRestOf(t)];
})));
""" % json.dumps(GATE["text"]))
    assert [r for r in out if r[1] is not None or r[2] is not None] == []


@needs_node
def test_numbers_to_ninety_nine_and_the_switch_the_name_gate():
    js = _js()
    out = _run(_matchers_harness(js) + """
console.log(JSON.stringify({
  n: ["twenty one", "twenty-one", "ninety nine", "nineteen", "seventy", "13", "two", "third", "twenty bob", "potato"].map(voiceNumber),
  sw: ["switch the docs", "switch the", "switch the bug", "switch the database driver to postgres"].map(voiceMayBeCommand),
  idx: [voiceCommand("session twenty one"), voiceCommand("session number ninety nine")],
}));
""")
    assert out["n"] == [21, 21, 99, 19, 70, 13, 2, 3, 0, 0]
    assert out["sw"] == [True, True, False, False]
    # The local table (the router down) switches by a two-word number too.
    assert [c and c["kind"] for c in out["idx"]] == ["switch_index", "switch_index"]


@needs_node
def test_a_phrase_said_to_mindflock_is_found_mid_message():
    """voiceWakeRestOf: the words after a leading wake word ("" alone), or
    null. The word itself counts with or without a pause; an ordinary word
    heard for it ("my flock", "mindful") only with the pause the recognizer
    writes after a name said to it. vc-words' wake_at twin (voiceWakeRest)
    is used when the page has it."""
    js = _js()
    cases = [
        "MindFlock, next session",
        "mindflock",
        "Hey MindFlock what needs me",
        "Mind flock open intake.",
        "My flock, open settings.",
        "Mine flock, open intake.",
        "Mindful, I'll open settings.",
        "my flock open settings",
        "my flag is not being read",
        "i mind that the flock test is slow",
        "mindful of the flaky tests",
        "fix the login bug",
    ]
    out = _run(
        _matchers_harness(js)
        + "var a = %s.map(voiceWakeRestOf);\n" % json.dumps(cases)
        + "var voiceWakeRest = function (t) { return t === 'line flock open settings' ? 'open settings' : null; };\n"
        + "console.log(JSON.stringify({a: a, twin: [voiceWakeRestOf('line flock open settings'), voiceWakeRestOf('mindflock open')]}));"
    )
    # "Mindful, I'll …": the twin (vc-words) also drops the misheard "I'll".
    assert out["a"][6] in ("I'll open settings.", "open settings.")
    out["a"][6] = "*"
    assert out["a"] == [
        "next session",
        "",
        "what needs me",
        "open intake.",
        "open settings.",
        "open intake.",
        "*",
        None,
        None,
        None,
        None,
        None,
    ]
    # The twin decides when it is there.
    assert out["twin"] == ["open settings", None]


def _route_kinds(js: str) -> str:
    """VOICE_ROUTE_KINDS as the page builds it (the list and every push)."""
    import re

    first = re.search(r"  var VOICE_ROUTE_KINDS = \[[^\]]*\];", js).group(0)
    pushes = re.findall(r"  VOICE_ROUTE_KINDS\.push\([^)]*\);", js)
    return first + "\n" + "\n".join(pushes) + "\n"


@needs_node
def test_the_router_kinds_the_phone_takes_and_an_unknown_one():
    """voiceRoute: the router's `assistant` and `help` are the page's kinds
    (before, `assistant` became a prompt and "MindFlock, open settings" was
    TYPED into the agent); a kind it does not know is {kind: "unknown"} —
    never a prompt — and a prompt's `near` comes through as sent."""
    js = _js()
    script = (
        "var window = globalThis, current = 'docs';\n"
        "var voice = { session: 'docs' }, voiceListen_ = { early: null, gen: 1 };\n"
        "var VOICE_ROUTE_TIMEOUT_MS = 2000, VOICE_EARLY_MAX_MS = 10000;\n"
        + _route_kinds(js)
        + _whole(js, "voiceNorm")
        + "function voiceRouteBody(t) { return { text: t }; }\n"
        + "function voicePendingKeep() {}\n"
        + _whole(js, "voiceRoute")
        + """
var replies = [{kind: "assistant", text: "open settings"}, {kind: "help", text: "how do red zones work"},
               {kind: "frobnicate"}, {kind: "prompt", near: {kind: "switch", why: "no_session", arg: "docks", suggest: "docs"}}];
var out = [], i = 0;
globalThis.fetch = function () { var j = replies[i++]; return Promise.resolve({ ok: true, json: function () { return Promise.resolve(j); } }); };
(function next() {
  if (i >= replies.length) { console.log(JSON.stringify({out: out, last: voice.lastRoute})); return; }
  voiceRoute("x", "", true, 0).then(function (r) { out.push(r); next(); });
})();
"""
    )
    out = _run(script)
    assert out["out"][0] == {"kind": "assistant", "text": "open settings"}
    assert out["out"][1]["kind"] == "help"
    assert out["out"][2] == {"kind": "unknown", "said": "frobnicate"}
    assert out["out"][3]["near"]["arg"] == "docks"


def _routed_harness(js: str) -> str:
    return (
        _matchers_harness(js)
        + """
var did = [];
function rec(name) { return function () { did.push([name].concat([].slice.call(arguments))); return name === "voiceHeardRest" ? false : undefined; }; }
var voiceHush = rec("voiceHush"), voiceHeardRest = rec("voiceHeardRest"), voiceStripNote = rec("voiceStripNote"),
    voiceStopForCommand = rec("voiceStopForCommand"), voiceRunCommand = rec("voiceRunCommand"),
    voiceAssistTurn = rec("voiceAssistTurn"), voiceIdle = rec("voiceIdle");
function voiceStrictMatch() { return false; }
"""
        + _whole(js, "voiceNearNote")
        + _whole(js, "voiceRouteMissNote")
        + _whole(js, "voiceRouted")
    )


@needs_node
def test_routed_assistant_unknown_near_and_a_fallback_say_why():
    """voiceRouted: `assistant` ends the listen and goes to the assistant
    (the router's words, not the phrase) — nothing typed; an unknown kind is
    typed as words and the strip says so; a prompt with `near` is typed and the
    strip says why ("Typed — no session called docks (did you mean
    docs?)"); a route that timed out says the phone's own table acted, or
    that a phrase that may have been a command was typed."""
    js = _js()
    out = _run(_routed_harness(js) + """
var o = {};
function run(name, res, text) { did = []; voice.routeWhy = res && res.why0; var r = voiceRouted(res && res.kind ? res : null, {text: text}); o[name] = {r: r, did: did}; }
run("assistant", {kind: "assistant", text: "open settings"}, "mindflock open settings");
run("unknown", {kind: "unknown", said: "teleport"}, "teleport me to docs");
run("near", {kind: "prompt", near: {kind: "switch", why: "no_session", arg: "docks", suggest: "docs"}}, "switch to docks");
run("nodialog", {kind: "prompt", near: {kind: "answer", why: "no_dialog"}}, "yes");
run("plain", {kind: "prompt"}, "fix the login bug");
run("timeoutCmd", {why0: "timeout"}, "next session");
run("timeoutTyped", {why0: "timeout"}, "scroll the thing");
run("timeoutWords", {why0: "timeout"}, "fix the login bug");
console.log(JSON.stringify(o));
""")
    a = out["assistant"]
    assert a["r"] is True and ["voiceAssistTurn", "open settings"] in a["did"]
    assert not any(d[0] == "voiceHeardRest" for d in a["did"])
    u = out["unknown"]
    assert any(d[0] == "voiceHeardRest" for d in u["did"])
    assert any(d[0] == "voiceStripNote" and "teleport" in d[1] for d in u["did"])
    assert [
        "voiceStripNote",
        "Typed — no session called docks (did you mean docs?)",
    ] in out["near"]["did"]
    assert ["voiceStripNote", "Typed — nothing is being asked"] in out["nodialog"][
        "did"
    ]
    assert not any(d[0] == "voiceStripNote" for d in out["plain"]["did"])
    t = out["timeoutCmd"]
    assert (
        t["r"] is True
        and [
            "voiceStripNote",
            "The server was slow — done with the phone's own commands",
        ]
        in t["did"]
    )
    assert ["voiceStripNote", "The server was slow — typed"] in out["timeoutTyped"][
        "did"
    ]
    # Plain dictation typed while the server is down: no note on every phrase.
    assert not any(d[0] == "voiceStripNote" for d in out["timeoutWords"]["did"])


def _assist_harness(js: str) -> str:
    return (
        _matchers_harness(js)
        + "var current = 'docs';\n"
        + js[
            js.index("  var VOICE_ASSIST_CONV_MS") : js.index(
                "  // One phrase to the assistant (the listen is over"
            )
        ]
    )


@needs_node
def test_the_assistant_events_on_the_phone():
    """The assistant's NDJSON → what the phone does: a focus switches, words
    are typed, a new session it started is selected, `say` / `ask` /
    `confirm` / `error` are spoken (an ask or confirm opens the mic for the
    answer), `did` is captioned; a dialog, a Settings screen, the sky, a
    command or a click is not on the phone — the strip says so, nothing
    typed. A confirm's yes / no is sent as its answer."""
    js = _js()
    nd = (
        "\n".join(
            json.dumps(e)
            for e in [
                {"type": "act", "action": {"do": "open", "target": "intake"}},
                {
                    "type": "did",
                    "ok": True,
                    "text": "Started fix-login in sitecheck.",
                    "action": {"do": "new_session"},
                    "detail": {"title": "fix-login"},
                },
                {"type": "say", "text": "Two sessions need you."},
                {
                    "type": "confirm",
                    "id": "c1",
                    "text": "Delete parser?",
                    "action": {"do": "session_action"},
                },
                {"type": "done", "ms": 5},
            ]
        )
        + "\nnot json\n"
    )
    out = _run(_assist_harness(js) + """
var ev = voiceAssistEvents(%s);
var plan = voiceAssistPlan(ev);
var acts = [{do: "focus", session: "docs"}, {do: "type", text: "hello"}, {do: "view", mode: "sky"},
            {do: "command", label: "Open the palette"}, {do: "pref", key: "x"}, {do: "close"}].map(voiceAssistAct);
voiceAssist_.pending = { kind: "confirm", id: "c1", at: Date.now() };
var yes = voiceAssistBody("yeah", Date.now());
voiceAssist_.pending = { kind: "confirm", id: "c1", at: Date.now() };
var other = voiceAssistBody("what about docs", Date.now());
var conv = [yes.conversation === other.conversation, voiceAssistBody("x", Date.now() + VOICE_ASSIST_CONV_MS + 1).conversation === yes.conversation];
var err = voiceAssistPlan([{type: "error", text: "MindFlock couldn't answer that."}]);
console.log(JSON.stringify({n: ev.length, plan: plan, acts: acts, yes: yes, other: other, conv: conv, err: err,
  yn: ["yes", "MindFlock, yes please", "nope", "maybe"].map(voiceAssistYesNo)}));
""" % json.dumps(nd))
    assert out["n"] == 5
    p = out["plan"]
    assert p["speak"] == ["Two sessions need you.", "Delete parser?"]
    assert p["pending"] == {"kind": "confirm", "id": "c1"}
    assert p["select"] == "fix-login" and p["chime"] is True
    assert p["notes"] == [
        "Intake isn't on the phone — open it on the desktop",
        "Started fix-login in sitecheck.",
    ]
    assert p["type"] == "" and p["focus"] == ""
    assert out["acts"][0] == {"focus": "docs"} and out["acts"][1] == {"type": "hello"}
    assert all("note" in a for a in out["acts"][2:])
    assert out["acts"][2]["note"] == "The sky isn't on the phone"
    assert out["yes"]["confirm"] == {"id": "c1", "yes": True}
    assert out["yes"]["snapshot"] == {"focused": None} or out["yes"]["snapshot"][
        "focused"
    ] in ("docs", None)
    assert "confirm" not in out["other"]
    assert out["conv"] == [True, False]
    assert out["err"]["failed"] is True and out["err"]["speak"] == [
        "MindFlock couldn't answer that."
    ]
    assert out["yn"] == [True, True, False, None]


def _addressed_harness(js: str) -> str:
    return (
        _matchers_harness(js)
        + """
var did = [];
function rec(name, ret) { return function () { did.push([name].concat([].slice.call(arguments))); return ret; }; }
var voiceListen_ = { addressed: "" };
var voiceStopForCommand = rec("stop"), voiceAssistTurn = rec("assist"), voiceIdle = rec("idle"),
    voiceRouted = rec("routed", false);
var waiting = false;
function voiceAssistWaiting() { return waiting; }
function voiceStrictMatch() { return false; }
"""
        + _whole(js, "voiceAddressed")
        + js[js.index("  var VOICE_NEAR_ASSIST") : js.index("  var VOICE_NEAR_ASSIST")]
        + "  var VOICE_NEAR_ASSIST = "
        + js.split("  var VOICE_NEAR_ASSIST = ")[1].split(";")[0]
        + ";\n"
        + _whole(js, "voiceAddressedRouted")
    )


@needs_node
def test_a_phrase_said_to_mindflock_routes_and_never_types_mid_message():
    """voiceAddressed: a phrase that starts with the wake word ("rest"),
    the words said with the word in one breath ("rest"), the assistant's
    question answered ("answer"), the first phrase of a listen the wake word
    started ("first"), else null (dictation as ever). voiceAddressedRouted:
    a command acts; a "rest" the router reads as a prompt goes to the
    assistant (never typed); a "first" goes to the assistant only on a near
    miss of a switch / status / read / question, else it is dictation; with
    no router a "rest" the phone can't do is not typed either."""
    js = _js()
    out = _run(_addressed_harness(js) + """
var o = {};
o.mid = voiceAddressed("MindFlock, next session");
o.alone = [voiceAddressed("Mindflock."), voiceListen_.addressed];
voiceListen_.addressed = "first"; o.first = voiceAddressed("open settings"); o.after = voiceListen_.addressed;
o.plain = voiceAddressed("fix the login bug");
voiceListen_.addressed = "rest"; o.rest = voiceAddressed("next session");
waiting = true; voiceListen_.addressed = "answer"; o.answer = voiceAddressed("yes"); waiting = false;
voiceListen_.addressed = "answer"; o.stale = voiceAddressed("yes");
function go(name, res, mode, text) { did = []; var r = voiceAddressedRouted(res, {text: text}, mode); o[name] = {r: r, did: did.map(function (d) { return d[0] === "routed" ? ["routed", d[1] && d[1].kind] : d; })}; }
go("cmd", {kind: "switch_rel", delta: 1}, "rest", "next session");
go("restPrompt", {kind: "prompt"}, "rest", "open intake");
go("firstNear", {kind: "prompt", near: {kind: "switch", why: "no_session", arg: "settings"}}, "first", "open settings");
go("firstWords", {kind: "prompt"}, "first", "fix the login bug");
go("firstNearAnswer", {kind: "prompt", near: {kind: "answer", why: "no_dialog"}}, "first", "yes");
go("restDown", null, "rest", "open intake");
go("restDownCmd", null, "rest", "next session");
console.log(JSON.stringify(o));
""")
    assert out["mid"] == {"mode": "rest", "text": "next session"}
    assert out["alone"][0]["alone"] is True and out["alone"][1] == "first"
    assert (
        out["first"] == {"mode": "first", "text": "open settings"}
        and out["after"] == ""
    )
    assert out["plain"] is None
    assert out["rest"] == {"mode": "rest", "text": "next session"}
    assert out["answer"] == {"mode": "answer", "text": "yes"}
    assert out["stale"] is None  # the question expired: dictation
    assert out["cmd"]["did"] == [["routed", "switch_rel"]]
    assert ["assist", "open intake"] in out["restPrompt"]["did"] and out["restPrompt"][
        "r"
    ] is True
    assert ["assist", "open settings"] in out["firstNear"]["did"]
    assert out["firstWords"]["did"] == [["routed", "prompt"]]
    # Answers and sends are never loosened: a near answer stays words.
    assert out["firstNearAnswer"]["did"] == [["routed", "prompt"]]
    assert (
        out["restDown"]["r"] is True
        and ["idle", "Couldn't reach MindFlock — nothing done"]
        in out["restDown"]["did"]
    )
    assert out["restDownCmd"]["did"] == [["routed", None]]  # the phone's own table


@needs_node
def test_the_word_and_a_command_in_one_breath_is_one_clip():
    """Contract 1: a lone utterance of up to 4 s goes to POST /api/voice/wake
    ("MindFlock, next session" in one breath, 2.6 s) — before, anything past
    1.6 s was a sentence and dropped. A voice held for 3.5 s is one clip, not
    cut where the old creeping floor turned it "quiet" (~2.4 s); past 4 s it
    is a sentence. voiceWakeLead: the standby's audio after the word."""
    from tests.unit.test_mobile_wake import _detector

    js = _js()
    out = _run(_detector(js) + """
function run(seq) {
  var d = voiceWakeNew(), out = [];
  seq.forEach(function (p) { for (var i = 0; i < p[1]; i++) { var r = voiceWakeStep(d, p[0], 40); if (r) out.push(r); } });
  return out;
}
var Q = 0.002, L = 0.2;
var blocks = [0, 1, 2, 3, 4, 5].map(function (i) { var x = new Float32Array(640); x.fill(i); return { at: i * 40, x: x }; });
var lead = voiceWakeLead(blocks, 120, 16000, 0.003);
console.log(JSON.stringify({
  breath: run([[Q, 20], [L, 65], [Q, 12]]),
  held: run([[Q, 20], [L, 88], [Q, 12]]),
  sentence: run([[Q, 20], [L, 105], [Q, 12]]).map(function (r) { return r.kind; }),
  lead: { n: lead.pcm.length, first: lead.pcm[0], ms: lead.ms, floor: lead.floor },
  none: voiceWakeLead(blocks, 1000, 16000, 0),
  capped: voiceWakeLead([{ at: 0, x: new Float32Array(16000 * 5) }], 0, 16000, 0).ms,
}));
""")
    assert [(r["kind"], r["ms"]) for r in out["breath"]] == [("clip", 2600)]
    assert [(r["kind"], r["ms"]) for r in out["held"]] == [("clip", 3520)]
    assert out["sentence"] == ["long"]
    assert out["lead"] == {"n": 1920, "first": 3, "ms": 120, "floor": 0.003}
    assert out["none"] is None
    assert out["capped"] == 3000


@needs_node
def test_woken_with_rest_handles_it_as_said_to_mindflock():
    """voiceWoke(rest, clip): listening starts as for the word alone; with
    `rest` (the command said in the same breath) that is the listen's first
    phrase, said to MindFlock — handled at once, never repeated; without it
    the listen's first phrase is addressed ("first"). The standby's audio
    after the word is taken BEFORE the chime sounds, for the recording."""
    js = _js()
    out = _run(
        """
var order = [], heard = [];
var document = { hidden: false }, navigator = {};
var voice = { state: "idle" }, listening = false;
var voiceListen_ = { addressed: "", woke: null };
var voiceWake_ = { wakes: 0, endAt: Date.now() - 300, wakeMs: -1 };
var VOICE_WAKE_CHIME_MS = 130, VOICE_WAKE_SKIP_PAD_MS = 100, VOICE_TAP_MS = 8000;
var voiceAdapters = { serverStt: { standbyAfter: function (t) { order.push("lead@" + t); return { pcm: [1], rate: 16000 }; } } };
function voiceIsListening() { return listening; }
function voiceWakeChime() { order.push("chime"); }
function voiceCaption() {}
function voiceBargeIn() {}
function voiceListen() { order.push("listen:" + JSON.stringify(!!voiceListen_.woke.lead)); listening = true; voiceListen_.addressed = ""; }
function voiceHeard(t, mode, typed, at) { heard.push([t, mode, typed, voiceListen_.addressed]); }
"""
        + _whole(js, "voiceWoke")
        + """
voiceWoke("", { to: 1600 });
var a = { order: order, addressed: voiceListen_.addressed, heard: heard.slice() };
order = []; heard = []; listening = false;
voiceWoke("next session", { to: 2400 });
console.log(JSON.stringify({ alone: a, rest: { order: order, heard: heard, addressed: voiceListen_.addressed } }));
"""
    )
    assert out["alone"]["order"] == ["lead@1600", "chime", "listen:true"]
    assert out["alone"]["addressed"] == "first" and out["alone"]["heard"] == []
    # The rest is the first phrase, addressed: no lead-in (the clip held it all).
    assert out["rest"]["order"] == ["chime", "listen:false"]
    assert out["rest"]["heard"] == [["next session", "prompt", "", "rest"]]


def test_the_lead_in_reaches_the_new_recording():
    """The standby's buffer is handed over (finding: audio said between the
    word's end and the new recording was lost): voiceRecStart passes the
    lead to ServerSTT.start, which feeds it through recBlock right after
    the recording starts — before the live blocks, the chime's skip not
    applied to it, the standby's floor as the room's."""
    js = _js()
    rs = _whole(js, "voiceRecStart")
    assert "lead: woke ? woke.lead : null," in rs
    stt = js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]
    start = stt.split("    function start(h) {")[1].split("\n    }\n")[0]
    assert start.index("recRun(g, mine, h, capMs);") < start.index(
        "if (h.lead) recLead(g, mine, h, h.lead);"
    )
    lead = stt.split("    function recLead(g, mine, h, lead) {")[1].split("\n    }\n")[
        0
    ]
    assert "g.handing = true;" in lead and "finally { g.handing = false; }" in lead
    assert "recBlock(g, mine, h, { inputBuffer:" in lead
    assert "if (g.handing) muted = false;" in stt
    assert "standbyAfter: standbyAfter" in stt


@needs_node
def test_a_command_right_after_a_reply_is_not_lost():
    """A phrase after a line said over the listen: the echo strip takes off
    only the line's own words ("Sent." heard back), never a command after
    them, and the phone recognizer's deaf window is the line and 150 ms."""
    js = _js()
    out = _run(
        _matchers_harness(js)
        + """
var voiceSpeech = { playing: null };
voice.stt = { name: "phone" };
voice.nodEcho = { text: "sent", until: Date.now() + 4000 };
function voiceTtsAudible() { return false; }
var VOICE_NOD_MUTE_MS = 150;
"""
        + _whole(js, "voiceEchoStrip")
        + _whole(js, "voiceEchoing")
        + """
var o = { cmd: voiceEchoStrip("next session"), lead: voiceEchoStrip("Sent next session"), echo: voiceEchoStrip("Sent.") };
voice.lineEndAt = Date.now() - 200; o.after200 = voiceEchoing();
voice.lineEndAt = Date.now() - 50; o.after50 = voiceEchoing();
console.log(JSON.stringify(o));
"""
    )
    assert out == {
        "cmd": "next session",
        "lead": "next session",
        "echo": "",
        "after200": False,
        "after50": True,
    }
