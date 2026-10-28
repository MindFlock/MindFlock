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
    assert (
        "function fill() { while (asked < parts.length && ahead.length < VOICE_SPEAK_AHEAD) ahead.push(fetchClip(ctx, parts[asked++], keep)); }"
        in speak
    )
    assert "var part = parts[i++], got = ahead.shift();" in speak
    assert speak.index(
        "fill();   // the next sentences are made while this one plays"
    ) < speak.index(
        "(x.audio ? play(ctx, x.audio) : phone.speak(part)).then(next, next);"
    )
    # A cancelled line stops before its next clip; cancel aborts the fetches.
    assert "if (g !== gen) { resolve(); return; }" in speak
    cancel = _inner(tts, "cancel")
    assert "gen++;" in cancel and "a.abort();" in cancel
    # No AudioContext running: the phone's voice says the whole line, once.
    assert "return phone.speak(text);" in speak
    # The phone's own voice reads the text shaped for it (spoken-text.js).
    assert "var parts = sentenceChunks(voiceShapeSpoken(text));" in _whole(
        js, "PhoneTTS"
    )
    # Diagnostics: the first sound's time and the sentence count.
    assert "{ first: first, parts: parts.length }" in speak
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
