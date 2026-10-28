"""The phone page's wake word: "MindFlock" on its own starts listening.

The lone-utterance detector (``voiceWakeStep``), the clip slicer
(``voiceWakeSlice``) and the gate (``voiceWakeGate``) are the page's own
copies, run as-is under node; the markup, the pref and the wiring are pinned
as text. The live loop — the standby on the warm mic, ``POST
/api/voice/wake``, the chime, the pauses, 409 — runs in headless Chrome
(``tests/unit/data/voice_browser/voice_browser.js``, the "wake:" checks).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js() -> str:
    return client.get("/mobile.js").text


def _html() -> str:
    return client.get("/m").text


def _css() -> str:
    return client.get("/mobile.css").text


def _fn(js: str, name: str) -> str:
    """The body of one top-level ``function name(`` in the IIFE."""
    body = js.split("  function %s(" % name)[1].split("\n  }\n")[0]
    return body.split("{", 1)[1]


def _node():
    return shutil.which("node")


def _run(script: str) -> dict:
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _detector(js: str) -> str:
    """The detector's constants and functions, with the recorder's
    loudness threshold they share."""
    rms = re.search(r"  var VOICE_RMS_SPEECH = [^;]+;", js).group(0)
    start = js.index("  // The wake word's lone-utterance detector")
    end = js.index("  function ServerSTT(")
    return rms + "\n" + js[start:end]


# --------------------------------------------------------------------------- #
# The detector, under node
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(_node() is None, reason="needs node")
def test_wake_detector_takes_only_a_lone_utterance():
    """Level sequences of 40 ms blocks (quiet 0.002, loud 0.2): a word of
    250..1600 ms with 350 ms of quiet before and 200 ms after is a clip —
    its lead-in 100 ms before the first loud block, its end the block the
    quiet tail reached 200 ms (the clip goes then, ``end`` = the sound's
    end); shorter is "short", longer "long" (never a clip, however it ends);
    a dip shorter than the tail is inside the word; sound with no quiet
    before it (the start, or less than 350 ms after a clip) is part of
    something longer; a steady hum above the absolute threshold is the
    floor, not a word."""
    script = (
        _detector(_js()) + "\n"
        "function run(seq) {\n"
        "  var d = voiceWakeNew(), out = [];\n"
        "  seq.forEach(function (p) { for (var i = 0; i < p[1]; i++) {\n"
        "    var r = voiceWakeStep(d, p[0], 40); if (r) { r.at = d.t; out.push(r); } } });\n"
        "  return out;\n"
        "}\n"
        "var Q = 0.002, L = 0.2, out = {};\n"
        "out.right = run([[Q, 20], [L, 15], [Q, 12]]);\n"
        "out.short = run([[Q, 20], [L, 5], [Q, 12]]);\n"
        "out.long = run([[Q, 20], [L, 45], [Q, 20]]);\n"
        "out.dip = run([[Q, 20], [L, 10], [Q, 4], [L, 10], [Q, 12]]);\n"
        "out.openTail = run([[Q, 20], [L, 15], [Q, 4]]);\n"
        "out.followOn = run([[Q, 20], [L, 10], [Q, 6], [L, 10], [Q, 12]]);\n"
        "out.noQuietBefore = run([[L, 10], [Q, 12]]);\n"
        "out.afterSentence = run([[Q, 20], [L, 45], [Q, 9], [L, 10], [Q, 12]]);\n"
        "out.shortGap = run([[Q, 20], [L, 10], [Q, 12], [L, 5], [Q, 4], [L, 10], [Q, 12]]);\n"
        "out.hum = run([[0.03, 30], [0.03, 15], [0.03, 12]]);\n"
        "out.humWord = run([[0.01, 20], [0.3, 10], [0.01, 12]]);\n"
        "out.edgeMin = run([[Q, 20], [L, 7], [Q, 12]]);\n"
        "out.edgeMax = run([[Q, 20], [L, 40], [Q, 12]]);\n"
        "var blocks = [0, 1, 2, 3, 4, 5].map(function (i) { var x = new Float32Array(640); x.fill(i); return { at: i * 40, x: x }; });\n"
        "var s = voiceWakeSlice(blocks, 50, 130, 16000);\n"
        "out.slice = { len: s.length, first: s[0], last: s[s.length - 1] };\n"
        "out.sliceAll = voiceWakeSlice(blocks, 0, 1000, 16000).length;\n"
        "out.sliceNone = voiceWakeSlice(blocks, 500, 900, 16000).length;\n"
        "out.k = [VOICE_WAKE_QUIET_MS, VOICE_WAKE_TAIL_MS, VOICE_WAKE_MIN_MS, VOICE_WAKE_MAX_MS, VOICE_WAKE_LEAD_MS, VOICE_WAKE_BLOCK];\n"
        "console.log(JSON.stringify(out));\n"
    )
    out = _run(script)
    assert out["k"] == [350, 200, 250, 1600, 100, 2048]
    # The right length: one clip, the lead-in 100 ms before its first loud
    # block (800 ms), out the block the tail reaches 200 ms (5 blocks) after
    # its last (1400) — 160 ms sooner than the old 350 ms tail.
    assert out["right"] == [
        {"kind": "clip", "from": 700, "to": 1600, "ms": 600, "end": 1400, "at": 1600}
    ]
    assert out["short"] == [{"kind": "short", "ms": 200, "at": 1200}]
    # A sentence: "long" the block it passes 1600 ms, and never a clip after.
    assert [r["kind"] for r in out["long"]] == ["long"]
    # The stop inside the word (160 ms < 200): one clip over both halves.
    assert [(r["kind"], r["ms"]) for r in out["dip"]] == [("clip", 960)]
    # The quiet tail not yet 200 ms: nothing yet.
    assert out["openTail"] == []
    # Sound 240 ms after a clip went had no 350 ms of quiet before it: the
    # first was the start of something longer, the second is never a clip.
    assert [r["kind"] for r in out["followOn"]] == ["clip"]
    # Sound from the first block on had no quiet before it.
    assert out["noQuietBefore"] == []
    # Right after a sentence, a word with only 360 ms of quiet between is
    # still a lone utterance (the quiet before is what counts).
    assert [r["kind"] for r in out["afterSentence"]] == ["long", "clip"]
    # A click, then a word 160 ms later: one clip — the click and the word
    # are one utterance (the gap is inside it).
    kinds = [(r["kind"], r.get("ms")) for r in out["shortGap"]]
    assert kinds == [("clip", 400), ("clip", 760)], kinds
    assert out["hum"] == []
    assert [r["kind"] for r in out["humWord"]] == ["clip"]
    # 280 ms (7 blocks) is a word, 1600 ms (40 blocks) still one.
    assert [r["kind"] for r in out["edgeMin"]] == ["clip"]
    assert [r["kind"] for r in out["edgeMax"]] == ["clip"]
    # The slice: every block that overlaps 50..130 ms (40, 80, 120), joined.
    assert out["slice"] == {"len": 1920, "first": 1, "last": 3}
    assert out["sliceAll"] == 3840 and out["sliceNone"] == 0


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_wake_gate_under_node():
    """voiceWakeGate, the page's own: off, hidden, listening, a recording,
    the page speaking or < 500 ms after, a check in flight, < 1.5 s since the
    last check — in that order — else a check may go."""
    js = _js()
    consts = re.findall(r"  var VOICE_WAKE_(?:GAP|AFTER_SPEECH)_MS = [^;]+;", js)
    script = (
        "\n".join(consts)
        + "\nfunction voiceWakeGate("
        + js.split("  function voiceWakeGate(")[1].split("\n  }\n")[0]
        + "\n  }\n"
        "var ok = { on: true, hidden: false, listening: false, recording: false, speaking: false,\n"
        "           spokeAgo: 99999, inflight: false, sinceCheck: 99999 };\n"
        "function g(o) { return voiceWakeGate(Object.assign({}, ok, o)); }\n"
        "console.log(JSON.stringify({\n"
        "  ok: g({}), off: g({ on: false }), hidden: g({ hidden: true }), listening: g({ listening: true }),\n"
        "  recording: g({ recording: true }), speaking: g({ speaking: true }), after: g({ spokeAgo: 499 }),\n"
        "  afterDone: g({ spokeAgo: 500 }), inflight: g({ inflight: true }), rate: g({ sinceCheck: 1499 }),\n"
        "  rateDone: g({ sinceCheck: 1500 }), order: g({ hidden: true, speaking: true, inflight: true }),\n"
        "}));\n"
    )
    out = _run(script)
    assert out == {
        "ok": "",
        "off": "off",
        "hidden": "hidden",
        "listening": "listening",
        "recording": "recording",
        "speaking": "speaking",
        "after": "speaking",
        "afterDone": "",
        "inflight": "inflight",
        "rate": "rate",
        "rateDone": "",
        "order": "hidden",
    }


# --------------------------------------------------------------------------- #
# Markup, the pref, the wiring
# --------------------------------------------------------------------------- #
def test_wake_switch_in_the_voice_sheet():
    html = _html()
    sheet = html.split('id="voice-sheet"')[1].split("</div>\n    </div>")[0]
    # Where Spoken replies lives: right after it.
    assert sheet.index('id="voice-replies"') < sheet.index('id="voice-wake"')
    label = sheet.split('id="voice-wake"')[1].split("</label>")[0]
    # The word is the owner's (voiceWordsShow fills it from GET /api/voice).
    assert 'Listen for “<span class="voice-wake-word">MindFlock</span>”' in label
    assert "mic stays open" in label and "mic indicator" in label
    assert "iPhone" in label and "never in the background" in label
    assert 'id="voice-wake-note" class="new-muted hidden"' in sheet


def test_wake_pref_load_save_and_default():
    js = _js()
    assert 'replies: "mine", wake: false, confirm: false, sounds: true }' in js
    assert "voice.prefs.wake = j.wake === true;" in _fn(js, "voiceLoadPrefs")
    assert "wake: voice.prefs.wake," in _fn(js, "voicePersist")
    # The switch: the pref, saved, the context made in this tap.
    sw = js.split('voiceWakeEl.addEventListener("change"')[1].split("});")[0]
    assert "voice.prefs.wake = voiceWakeEl.checked;" in sw
    assert (
        "voicePrime();" in sw and "voicePersist();" in sw and "voiceWakeSync();" in sw
    )
    assert "voice.prefs.wake || voice.prefs.sounds !== false) voiceAudioCtx();" in _fn(
        js, "voicePrime"
    )
    assert "voiceWakeEl.checked = !!voice.prefs.wake;" in _fn(js, "voiceSheetOpen")
    off = _fn(js, "voiceWakeOff")
    assert "voice.prefs.wake = false;" in off and "voicePersist();" in off
    assert "voiceWakeEl.checked = false;" in off and "voiceError(msg)" in off


def test_wake_request_shape_and_answers():
    js = _js()
    check = _fn(js, "voiceWakeCheck")
    assert 'fetch("/api/voice/wake?lang=" + encodeURIComponent(' in check
    assert 'headers: { "Content-Type": "audio/wav" }' in check
    assert "if (voiceWakeLive()) return;" in check
    assert "W.inflight = true;" in check and "W.at = Date.now();" in check
    assert "x.r.status === 409" in check and "voiceWakeOff(" in check
    assert "if (x.j.wake) voiceWoke();" in check
    woke = _fn(js, "voiceWoke")
    assert "voiceWakeChime();" in woke and 'voiceCaption("Yes?");' in woke
    assert "navigator.vibrate(12)" in woke
    # Exactly a tap in toggle mode — at once, the chime in the same moment
    # (no timer before listening), the chime's audio skipped by the recorder.
    # …and continuous, as a tap is: listening until "over and out".
    assert (
        'voiceListen("prompt", VOICE_TAP_MS, false, true);' in woke
        and "voiceBargeIn();" in woke
    )
    assert "setTimeout" not in woke
    assert woke.index("voiceWakeChime();") < woke.index(
        'voiceListen("prompt", VOICE_TAP_MS, false, true);'
    )
    assert (
        "voiceListen_.woke = { skipUntil: now + VOICE_WAKE_CHIME_MS + VOICE_WAKE_SKIP_PAD_MS, wakeMs: W.wakeMs };"
        in woke
    )
    assert "W.wakeMs = Math.max(0, now - (W.endAt || now));" in woke
    rs = _fn(js, "voiceRecStart")
    assert (
        "skipUntil: woke ? woke.skipUntil : 0," in rs
        and "wakeMs: woke ? woke.wakeMs : -1," in rs
    )
    stt = js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]
    # The chime's audio is kept (a word under it loses nothing) but no word starts in it.
    assert (
        "var muted = (h.skipUntil && Date.now() < h.skipUntil) || voiceMicMuted();"
        in stt
    )
    assert "g.trace.wake = h.wakeMs;" in stt
    # The clip goes in the block its tail ends: the synchronous decimator,
    # never the browser's async render; the word's end on the wall clock.
    sb = stt.split("function standbyBlock(w, data) {")[1].split("\n    }\n")[0]
    assert "voiceResampleClip" not in sb
    assert (
        "var pcm = voiceResample(voiceWakeSlice(w.blocks, r.from, r.to, w.rate), w.rate, VOICE_WAV_RATE);"
        in sb
    )
    assert "r.endAt = Math.round(Date.now() - (r.to - r.end));" in sb
    assert "W.endAt = r && r.endAt ? r.endAt : Date.now();" in check
    # Diagnostics: "wake 0.6 s" (the word's end → listening).
    assert '", wake " + (W.wakeMs / 1000).toFixed(1) + " s' in _fn(js, "voiceDiagWake")
    assert '"wake " + (t.wake / 1000).toFixed(1) + " s' in _fn(js, "voiceDiagTrace")
    # The wake word's chime is the start chime (voiceChime: the page's
    # AudioContext, no asset).
    assert 'var ok = voiceChime("start");' in _fn(js, "voiceWakeChime")
    chime = _fn(js, "voiceChime")
    assert "voice.audioCtx" in chime and "createOscillator" in chime
    assert "var VOICE_WAKE_GAP_MS = 1500;" in js
    assert "var VOICE_WAKE_AFTER_SPEECH_MS = 500;" in js


def test_wake_lifecycle_wiring():
    js = _js()
    # Every state change, every line said or ended, the page hiding and
    # coming back: the standby follows.
    assert "voiceWakeSync();" in _fn(js, "voiceSetState")
    assert "voiceWakeSpeaking(on);" in _fn(js, "voiceHushShow")
    vis = js.split('document.addEventListener("visibilitychange"')[1].split(
        "\n  });\n"
    )[0]
    assert vis.count("voiceWakeSync();") == 2
    sync = _fn(js, "voiceWakeSync")
    assert "A.hold(want);" in sync
    assert "A.standby(voiceWakeHandlers)" in sync and "A.standbyStop();" in sync
    assert 'voiceBtn.toggleAttribute("data-standby", run)' in sync
    # The phone's own recognizer gets the mic to itself.
    assert 'why === "listening" && voice.stt !== A && A.warm()) A.release();' in sync
    # ServerSTT: the grace never gives the stream back under the standby;
    # release() and a recording's start() stop it; one getUserMedia at a time.
    stt = js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]
    assert "if (!document.hidden && (standing || holding)) { keep(); return; }" in stt
    # Letting go never re-arms the grace: a hidden page's short one stands.
    assert "function hold(on) { holding = !!on; }" in stt
    assert "standbyStop();   // a belt" in stt
    assert "    function release(ms) {\n      discard();\n      standbyStop();" in stt
    assert "if (asking) return asking;" in stt
    assert "ctx.createScriptProcessor(VOICE_WAKE_BLOCK, 1, 1)" in stt
    assert (
        "standby: standby, standbyStop: standbyStop, standbyOn: standbyOn, hold: hold"
        in stt
    )
    # The meter stays hidden: the standby moves the ring only.
    assert 'if (!ring && voiceLevelEl.classList.contains("hidden") &&' in js


def test_wake_standby_look():
    css = _css()
    # The standby is a small grey dot, still: no ring, nothing that moves.
    assert '#voice-btn[data-standby]:not([data-state="listening"])::after' not in css
    sel = '#voice-btn[data-standby]:not([data-state="listening"])::before,'
    assert sel in css
    rule = css.split(sel)[1].split("}")[0]
    assert "background: var(--muted);" in rule
    assert "animation" not in rule and "transform" not in rule
