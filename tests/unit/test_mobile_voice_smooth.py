"""The phone's (/m) voice, made smooth: no dead mic, no stacked waits, no
stuck or doubled audio.

* **The mic is never deaf between phrases** (``ServerSTT``): when a phrase
  ends and the listen goes on (``h.carry``), its recording graph goes on
  recording the NEXT phrase (``carryFrom``) and that phrase's stream opens
  alongside the last one's /end and the router; the next ``start()`` takes
  it (``carryTake``) — no getUserMedia, no new graph, no gap. A carried
  phrase nobody takes in ``VOICE_CARRY_MAX_MS`` goes; ``stop()`` drops it.
* **Deaf only while audible** (``voiceMicDeaf``): a line over the listen
  skips the recorder's blocks only while sound is coming out
  (``voiceTtsAudible``: a clip playing, the phone's voice saying one), not
  while the server voice is still being made. Deaf blocks lower the floor
  only, learn the line's level (``bleed``), and freeze the silence clock.
* **The server voice is bounded** (``ServerTTS``): every /speak request has
  its own cap (``VOICE_SPEAK_FETCH_MAX_MS``); a clip being waited for gets
  ``voiceSpeakWaitMs`` and then the phone's voice says the rest of the line
  in one go; clips made lately stay in memory (a line said again is not
  made again); one audio owner — a new line or clip stops the one playing.
* **The restart after an end is that listen's only** (``restartTok``).
* **"over" overlaps /end and the router** (``voiceRouteEarly``): the guess's
  phrase goes to the router while /end makes the final, never when its
  routing could act by itself (``voiceRouteSafe``); the strip says
  *Sending…* at once.

Cycle 2:

* **A carried phrase is its own listen's only** (``voiceCarryKey``): it is
  bound to the dictation listen it was recorded in (mode, generation, box);
  a dialog's or an answer's listen has no key — it never carries and never
  takes one, so a "yes" said while dictating can never answer a dialog.
* **A line's sound waits for the owner's pause** (``voiceSpeakWhenQuiet``):
  a clip back from the server's voice, or the phone's voice starting a
  sentence, while the owner talks over the listen is held until they pause —
  the recorder is never deafened mid-word. The phone recognizer hears the
  owner until the line first sounds (``voiceEchoing``).
* **A muted microphone is a pause** (``micMuted``/``micPause``): nothing
  recorded, the silence clock stands still, the strip says *Mic paused…*,
  'unmute' resumes; only 'ended' (or the device gone) is a loss.
* **The server's stream cap is passing** (``voiceStreamFull``): a cap 409 is
  asked again, bounded, then that phrase goes as a clip — streaming stays on;
  an open waits for the DELETEs on their way, and "disregard" drops its
  stream before the carried phrase's opens.

Pure rules run as-is under node; the behaviour in a browser is walked by
``test_mobile_voice_browser.py`` (tests/unit/data/voice_browser)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _whole(js: str, name: str) -> str:
    """``function name(…) { … }`` as written (two-space indent), for node."""
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return js[start:end]


def _inner(js: str, name: str) -> str:
    """A function nested in an adapter (four-space indent)."""
    start = js.index("    function " + name + "(")
    end = js.index("\n    }\n", start) + len("\n    }\n")
    return js[start:end]


def _consts(js: str, *names: str) -> str:
    out = []
    for n in names:
        line = next(
            ln for ln in js.splitlines() if ln.lstrip().startswith("var " + n + " = ")
        )
        out.append(line.strip())
    return "\n".join(out) + "\n"


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _stt(js: str) -> str:
    return js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]


def _tts(js: str) -> str:
    start = js.index("  function ServerTTS(phone) {")
    return js[start : js.index("\n  }\n", start)]


# --------------------------------------------------------------------------- #
# The mic between phrases
# --------------------------------------------------------------------------- #
def test_a_phrase_end_carries_the_recorder_to_the_next_phrase():
    js = _js()
    stt = _stt(js)
    assert "var VOICE_CARRY_MAX_MS = 10000;" in js
    # Every phrase end that used to take the graph down asks first.
    fin = _inner(stt, "finish")
    assert "recEnd(g, mine, g.fast);" in fin and "recStop(g);" not in fin
    fire = _inner(stt, "controlFire")
    assert "recEnd(g, mine, c.text);" in fire
    end = _inner(stt, "recEnd")
    assert 'mine.h.carry && mine.h.carry(text || "")' in end
    assert "carryFrom(g, key);" in end and "recStop(g);" in end
    # The graph moves to the next phrase; its stream opens now.
    cf = _inner(stt, "carryFrom")
    assert "g.source = g.proc = g.mute = null;" in cf
    assert "c.proc.onaudioprocess = function (ev) { recBlock(c, null, {}, ev); };" in cf
    assert "if (streams()) streamOpen(c, null);" in cf
    assert "VOICE_CARRY_MAX_MS" in cf
    # start() takes it before stop() (which would drop it), on the same live mic.
    start = _inner(stt, "start")
    assert start.index("var c = carry;") < start.index("stop();")
    assert "c.mic === stream && micLive(stream)" in start
    assert "carryTake(c, mine, h, capMs);" in start
    take = _inner(stt, "carryTake")
    assert "clearTimeout(keepTimer);" in take and "live = g;" in take
    assert "recRun(g, mine, h, capMs);" in take
    assert "if (h.onSpeech) h.onSpeech();" in take  # a word heard before it was taken
    # A carried phrase records and finds its first word, but reports and
    # sends nothing; it keeps only the lead-in before speech.
    blk = _inner(stt, "recBlock")
    assert "if (mine ? token !== mine || rec !== g : carry !== g) return;" in blk
    assert '(g.mode !== "clip" || !mine)' in blk
    assert "if (!mine) return;" in blk
    # Its stream's open goes to whoever took it; a pump needs an owner.
    # (both ways back: a failed open of a taken phrase that already ended goes as a clip)
    assert _inner(stt, "streamOpen").count("mine = mine || g.mine || null;") == 2
    assert "if (mine) live = g;" in _inner(stt, "streamOpen")
    assert _inner(stt, "streamPump").count("if (!mine ||") == 1
    # stop() drops it (graph down, stream DELETEd).
    assert "carryDrop();" in _inner(stt, "discard")
    drop = _inner(stt, "carryDrop")
    assert "recStop(g);" in drop and "streamDrop(g);" in drop


def test_the_state_machine_says_when_a_listen_goes_on():
    js = _js()
    rs = _whole(js, "voiceRecStart")
    carry = rs.split("carry: function (t) {")[1].split("\n      },\n")[0]
    # Not for a dialog, a stop being asked, push to talk's end…
    assert 'L.mode === "dialog" || L.stopAfter || L.overAfter' in carry
    # …nor after "over and out" or a stop phrase in the guess.
    assert 'voiceDictParse(t).kind !== "out" && !voiceTail(t, false)' in carry
    # The tap that ends a dialog's listen says so BEFORE it finishes the clip.
    done = _whole(js, "voiceListenDone")
    assert done.index("voiceListen_.stopAfter = true;") < done.index("s.finish()")


# --------------------------------------------------------------------------- #
# Deaf only while audible
# --------------------------------------------------------------------------- #
def test_the_recorder_is_deaf_only_while_the_line_is_audible():
    js = _js()
    assert "return !!(p && p.over) && voiceTtsAudible();" in _whole(js, "voiceMicDeaf")
    aud = _whole(js, "voiceTtsAudible")
    assert 'typeof t.audible !== "function"' in aud  # an adapter that can't say: deaf
    # Both voices say when they sound.
    phone = _whole(js, "PhoneTTS")
    assert (
        "sounding = u;" in phone
        and "function audible() { return !!sounding; }" in phone
    )
    assert "audible: audible" in phone
    tts = _tts(js)
    assert (
        "function audible() { return !!playing || (!!phone.audible && phone.audible()); }"
        in tts
    )
    # A deaf block: the floor only goes down, the bleed is learnt, the
    # silence clock stands still.
    blk = _inner(_stt(js), "recBlock")
    deaf = blk.split("if (voiceMicDeaf()) {")[1].split("\n      }\n")[0]
    assert "if (g.floor === 0 || q < g.floor) g.floor = q;" in deaf
    assert "g.floor * 1.05" not in deaf
    assert "g.bleed = Math.max(q, (g.bleed || 0) * 0.97);" in deaf
    assert (
        "g.lastLoud = Math.min(now, g.lastLoud + (d.length * 1000) / g.rate);" in deaf
    )
    assert "return;" in deaf
    # A clip's end mutes a word's START for a moment (its echo), the audio kept.
    assert (
        "voice.micMuteUntil = Math.max(voice.micMuteUntil || 0, Date.now() + VOICE_NOD_MUTE_MS);"
        in _whole(js, "voiceClipEnded")
    )


# --------------------------------------------------------------------------- #
# The server voice
# --------------------------------------------------------------------------- #
@needs_node
def test_the_wait_for_a_clip_is_bounded_and_learned():
    js = _js()
    script = (
        _consts(js, "VOICE_SPEAK_WAIT_MIN_MS", "VOICE_SPEAK_WAIT_MAX_MS")
        + _whole(js, "voiceSpeakWaitMs")
        + """
console.log(JSON.stringify([
  voiceSpeakWaitMs(30, 0), voiceSpeakWaitMs(180, 0), voiceSpeakWaitMs(10, 0),
  voiceSpeakWaitMs(30, 1), voiceSpeakWaitMs(100, 20), voiceSpeakWaitMs(180, 70),
  voiceSpeakWaitMs(0, 0), voiceSpeakWaitMs(50, NaN)
]));
"""
    )
    out = _run(script)
    # Unknown voice: 4 s + 0.1 s a character, at least 4 s, at most 20 s.
    assert out[0] == 7000 and out[1] == 20000 and out[2] == 5000
    # Learned: 3× the per-character time plus a second, held to 4..20 s.
    assert out[3] == 4000 and out[4] == 7000 and out[5] == 20000
    assert out[6] == 4100 and out[7] == 9000


def test_the_server_voice_never_hangs_never_doubles_and_keeps_its_clips():
    js = _js()
    assert "var VOICE_SPEAK_FETCH_MAX_MS = 30000;" in js
    assert "var VOICE_SPEAK_KEEP = 8;" in js
    tts = _tts(js)
    fetch = _inner(tts, "fetchClip")
    # Every request has its own cap, and reports it as a timeout.
    assert "VOICE_SPEAK_FETCH_MAX_MS" in fetch and "a.abort();" in fetch
    assert "timeout: true" in fetch
    # A clip made lately plays from memory; every clip made is kept.
    assert (
        "if (recent.has(key)) return Promise.resolve({ audio: recent.get(key)" in fetch
    )
    assert "if (audio) remember(key, audio);" in fetch
    assert "while (recent.size > VOICE_SPEAK_KEEP)" in _inner(tts, "remember")
    # The wait: bounded, then the phone's voice says the REST, once; the
    # requests on their way are not aborted (they land in memory).
    said = _inner(tts, "said")
    assert "Promise.race([got, wait])" in said
    assert "voiceSpeakWaitMs(part.length, voice.rec.speakMsPerChar)" in said
    assert 'var rest = parts.slice(i - 1).join(" ");' in said
    assert "phone.speak(rest).then(next, next);" in said
    assert "abort" not in said
    # A clip that could not start is said by the phone's voice, not lost.
    assert (
        "if (ok === false && g === gen) { phone.speak(part).then(next, next); return; }"
        in said
    )
    # One audio owner: a new line cuts the one being said; a clip stops any
    # clip still sounding before it starts.
    speak = _inner(tts, "speak")
    assert "if (owner) cancel();" in speak
    play = _inner(tts, "play")
    assert play.index("hush();") < play.index("src.start(0)")
    assert "owner = null;" in _inner(tts, "cancel") and "hush();" in _inner(
        tts, "cancel"
    )
    # A suspended / interrupted context is asked to resume, briefly, first.
    run = _inner(tts, "running")
    assert "ctx.resume()" in run and "VOICE_SPEAK_RESUME_MS" in run


# ServerTTS run whole under node: a fake server (``delay[text]`` ms, Infinity
# never answers; aborts recorded), a fake AudioContext (one counter of
# sources sounding) and a fake phone voice. The waits are shrunk (the real
# values are pinned by test_the_numbers_are_the_documented_ones); the code
# reads them at call time, so a scenario may change them.
_TTS_PRELUDE = r"""
var window = { AudioContext: function () {}, AbortController: AbortController, fetch: true };
var VOICE_SPEAK_AHEAD = 2, VOICE_SPEAK_KEEP = 8, VOICE_CHUNK_GRACE_MS = 20;
var VOICE_SPEAK_FETCH_MAX_MS = 150, VOICE_SPEAK_RESUME_MS = 50;
var VOICE_SPEAK_WAIT_MIN_MS = 60, VOICE_SPEAK_WAIT_MAX_MS = 200;
var voice = { prefs: { rate: 1 }, rec: {}, cfg: {} };
var clipEnds = 0;
function voiceClipEnded() { clipEnds++; }
function voiceNodPrefetch() {}
function voiceTtsKey() { return "lab|v"; }
function voiceSpeakSentences(t) { return String(t).split(/(?<=\.)\s+/).filter(Boolean); }
var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
// The server: a request per POST /api/voice/speak; delay[text] ms (Infinity: never answers).
var reqs = [], delay = {};
fetch = function (url, o) {
  var text = JSON.parse(o.body).text, r = { text: text, aborted: false, done: false };
  reqs.push(r);
  return new Promise(function (resolve, reject) {
    var ms = delay[text] || 5, t = null;
    if (ms !== Infinity) t = setTimeout(function () {
      r.done = true;
      resolve({ ok: true, headers: { get: function () { return "lab"; } }, arrayBuffer: function () { return Promise.resolve(text); } });
    }, ms);
    o.signal.addEventListener("abort", function () { clearTimeout(t); r.aborted = true; reject(new Error("aborted")); });
  });
};
// The AudioContext: a clip plays for its duration; one counter of sources sounding.
var plays = [], sounding = 0, maxSounding = 0;
var ctx = {
  state: "running", destination: {},
  resume: function () { return new Promise(function () {}); },   // never resumes by itself
  decodeAudioData: function (buf, ok) { ok({ text: buf, duration: 0.03 }); },
  createBufferSource: function () {
    var s = { playbackRate: { value: 1 }, connect: function () {}, on: false, stopped: false };
    s.start = function () {
      s.on = true; sounding++; maxSounding = Math.max(maxSounding, sounding);
      plays.push({ text: s.buffer.text, at: Date.now(), src: s });
      s.t = setTimeout(function () { if (!s.on) return; s.on = false; sounding--; if (s.onended) s.onended(); }, s.buffer.duration * 1000);
    };
    s.stop = function () { clearTimeout(s.t); if (s.on) { s.on = false; sounding--; } s.stopped = true; };
    return s;
  },
};
function voiceAudioCtx() { return ctx; }
// The owner talking over the listen (voiceSpeakHeld): until heldUntil.
var heldUntil = 0, VOICE_HOLD_POLL_MS = 10, VOICE_HOLD_MAX_MS = 1000, VOICE_HOLD_REPLY_MS = 3000;
var voiceSpeech = { playing: null };
function voiceStripPaint() {}
function voiceSpeakHeld() { return Date.now() < heldUntil; }
// The phone's voice: what it was asked to say, in calls.
var phoneSaid = [], phoneCancels = 0;
var phone = { prime: function () {}, voices: function () { return []; }, audible: function () { return false; },
  speak: function (t) { phoneSaid.push(t); return Promise.resolve(); }, cancel: function () { phoneCancels++; } };
"""


def _tts_run(body: str):
    js = _js()
    script = (
        _TTS_PRELUDE
        + _whole(js, "voiceSpeakWaitMs")
        + _whole(js, "voiceSpeakWhenQuiet")
        + _whole(js, "ServerTTS")
        + "var tts = ServerTTS(phone), out = {};\n(async function () {\n"
        + body
        + "\nconsole.log(JSON.stringify(out));\n})().catch(function (e) { console.error(e); process.exit(1); });\n"
    )
    return _run(script)


@needs_node
def test_a_late_clip_hands_the_rest_to_the_phone_once_and_lands_in_memory():
    out = _tts_run("""
VOICE_SPEAK_FETCH_MAX_MS = 2000;
delay["Two."] = 400;
var t0 = Date.now();
await tts.speak("One. Two. Three.");
out.took = Date.now() - t0;
out.played = plays.map(function (p) { return p.text; });
out.phone = phoneSaid.slice();
await sleep(500);   // the late clip lands meanwhile
out.reqs = reqs.map(function (r) { return [r.text, r.done, r.aborted]; });
// The same line again: every clip from memory, none made twice.
await tts.speak("One. Two. Three.");
out.reqs2 = reqs.length;
out.played2 = plays.slice(1).map(function (p) { return p.text; });
out.backend = voice.rec.lastSpeak.backend;
out.phone2 = phoneSaid.length;
""")
    # The running path: sentence 1 played from the server's clip.
    assert out["played"] == ["One."]
    # Clip 2 not back within its wait: the phone says 2..n in ONE call,
    # never switching back when the clip lands.
    assert out["phone"] == ["Two. Three."]
    assert out["took"] < 400
    # The clips on their way were let finish (not aborted)…
    assert out["reqs"] == [
        ["One.", True, False],
        ["Two.", True, False],
        ["Three.", True, False],
    ]
    # …and the line said again is made by nobody: memory.
    assert out["reqs2"] == 3 and out["backend"] == "memory"
    assert out["played2"] == ["One.", "Two.", "Three."] and out["phone2"] == 1


@needs_node
def test_a_request_past_its_cap_is_aborted_and_the_rest_goes_to_the_phone_at_once():
    out = _tts_run("""
VOICE_SPEAK_FETCH_MAX_MS = 100;   // under the 200 ms wait: the cap fires first
delay["Slow one."] = Infinity;
var t0 = Date.now();
await tts.speak("Slow one. After.");
out.took = Date.now() - t0;
out.phone = phoneSaid.slice();
out.played = plays.length;
out.slow = reqs.filter(function (r) { return r.text === "Slow one."; }).map(function (r) { return r.aborted; });
out.error = voice.rec.lastSpeak.error;
""")
    assert out["slow"] == [True]  # aborted by its own cap
    assert "took over" in out["error"]
    # The sentence after the timeout is not waited for a second time.
    assert out["phone"] == ["Slow one. After."] and out["played"] == 0
    assert 90 <= out["took"] < 200, out["took"]


@needs_node
def test_cancel_aborts_every_request_on_its_way():
    out = _tts_run("""
VOICE_SPEAK_FETCH_MAX_MS = 2000;
delay["Hang one."] = delay["Hang two."] = Infinity;
var p = tts.speak("Hang one. Hang two.");
await sleep(20);
tts.cancel();
await p;   // resolves: the speech queue never stalls on a cancelled line
out.aborted = reqs.map(function (r) { return r.aborted; });
out.phone = phoneSaid.length;
out.phoneCancels = phoneCancels;
""")
    assert out["aborted"] == [True, True]
    assert out["phone"] == 0 and out["phoneCancels"] >= 1


@needs_node
def test_a_context_that_does_not_resume_hands_the_line_to_the_phone_once():
    out = _tts_run("""
ctx.state = "suspended";
var t0 = Date.now();
await tts.speak("Wake me. Please.");
out.took = Date.now() - t0;
out.phone = phoneSaid.slice();
ctx.state = "running";   // it resumes later: nothing more is played
await sleep(100);
out.reqs = reqs.length;
out.played = plays.length;
out.error = voice.rec.lastSpeak.error;
""")
    assert out["phone"] == ["Wake me. Please."]
    assert 45 <= out["took"] < 300, out["took"]
    assert out["reqs"] == 0 and out["played"] == 0
    assert out["error"].startswith("AudioContext suspended")


@needs_node
def test_one_audio_owner_a_new_line_stops_the_clip_playing():
    out = _tts_run("""
ctx.decodeAudioData = function (buf, ok) { ok({ text: buf, duration: /^Long/.test(buf) ? 0.5 : 0.03 }); };
var aDone = false;
var a = tts.speak("Long line A. More of A.").then(function () { aDone = true; });
await sleep(60);
out.audibleA = tts.audible();
var b = tts.speak("Line B.");
await Promise.all([a, b]);
await sleep(100);   // nothing of A's line comes back after B
out.aDone = aDone;
out.plays = plays.map(function (p) { return [p.text, p.src.stopped]; });
out.maxSounding = maxSounding;
out.audibleAfter = tts.audible();
out.clipEnds = clipEnds;
""")
    assert out["audibleA"] is True and out["audibleAfter"] is False
    # A was stopped and the rest of its line dropped, B played; never two
    # sources at once; A's promise resolved (the speech queue never stalls).
    assert out["plays"] == [["Long line A.", True], ["Line B.", False]]
    assert out["maxSounding"] == 1 and out["aDone"] is True
    assert out["clipEnds"] == 2  # each clip's end opens the nod mute once


@needs_node
def test_the_recent_clips_are_a_small_lru():
    tts = _tts(_js())
    script = (
        "var VOICE_SPEAK_KEEP = 8, recent = new Map();\n"
        + _inner(tts, "remember")
        + """
for (var i = 0; i < 8; i++) remember("k" + i, i);
remember("k0", "again");   // re-remembered: now the newest
remember("k8", 8);         // one over: the oldest (k1) goes
console.log(JSON.stringify({ keys: Array.from(recent.keys()), k0: recent.get("k0") }));
"""
    )
    out = _run(script)
    assert out["keys"] == ["k2", "k3", "k4", "k5", "k6", "k7", "k0", "k8"]
    assert out["k0"] == "again"


@needs_node
def test_the_phone_voice_is_audible_from_speak_to_its_end():
    js = _js()
    script = (
        r"""
var VOICE_WORD_MS = 10, VOICE_CHUNK_GRACE_MS = 20;
var voice = { prefs: { rate: 1 }, rec: {} }, ends = 0, utts = [];
var heldUntil = 0, VOICE_HOLD_POLL_MS = 10, VOICE_HOLD_MAX_MS = 1000, VOICE_HOLD_REPLY_MS = 3000;
var voiceSpeech = { playing: null };
function voiceStripPaint() {}
function voiceSpeakHeld() { return Date.now() < heldUntil; }
function voiceClipEnded() { ends++; }
function sentenceChunks(t) { return String(t).split(/(?<=\.)\s+/).filter(Boolean); }
function voiceShapeSpoken(t) { return t; }
function SpeechSynthesisUtterance(t) { this.text = t; }
var window = { SpeechSynthesisUtterance: SpeechSynthesisUtterance,
  speechSynthesis: { speak: function (u) { utts.push(u); }, cancel: function () {}, getVoices: function () { return []; } } };
var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
"""
        + _whole(js, "voiceSpeakWhenQuiet")
        + _whole(js, "PhoneTTS")
        + """
(async function () {
  var t = PhoneTTS(), out = {};
  out.before = t.audible();
  var p = t.speak("Hello there.");
  out.during = t.audible();
  utts[0].onend();
  await p;
  out.after = t.audible();
  out.ends1 = ends;
  // Cancelled: silent at once, and its late onend is not a clip's end.
  t.speak("Cut me off.");
  out.speaking = t.audible();
  t.cancel();
  out.cancelled = t.audible();
  utts[1].onend();
  out.ends2 = ends;
  // Chunked, onend never fires: each chunk's timer ends it.
  var n = utts.length, q = t.speak("One two. Three four.");
  out.chunk = t.audible();
  await q;
  out.chunks = utts.length - n;
  out.chunkAfter = t.audible();
  out.ends3 = ends;
  console.log(JSON.stringify(out));
})();
"""
    )
    out = _run(script)
    assert out["before"] is False and out["during"] is True and out["after"] is False
    assert out["ends1"] == 1
    assert out["speaking"] is True and out["cancelled"] is False
    assert out["ends2"] == 1  # the cancelled utterance's end calls no voiceClipEnded
    assert out["chunk"] is True and out["chunks"] == 2
    assert out["chunkAfter"] is False and out["ends3"] == 3


# --------------------------------------------------------------------------- #
# The restart after an end
# --------------------------------------------------------------------------- #
def test_the_restart_after_an_end_is_that_listens_only():
    js = _js()
    rs = _whole(js, "voiceRecStart")
    on_end = rs.split("onEnd: function () {")[1]
    assert "var gen = L.gen, tok = ++L.restartTok;" in on_end
    assert (
        "if (M.gen !== gen || M.restartTok !== tok || !voiceIsListening() || M.mode !== mode || voice.stt.active()) return;"
        in on_end
    )
    # Any start, and any new listen, makes a pending restart stale.
    assert "voiceListen_.restartTok++;" in rs
    listen = _whole(js, "voiceListen")
    assert "L.restartTok++;" in listen and "L.early = null;" in listen


# --------------------------------------------------------------------------- #
# "over": /end and the router overlap
# --------------------------------------------------------------------------- #
@needs_node
def test_only_a_phrase_that_cannot_act_by_itself_is_routed_early():
    js = _js()
    script = (
        _consts(js, "VOICE_PENDING_MS")
        + _whole(js, "voiceNorm")
        + _whole(js, "voiceRouteSafe")
        + """
var voice = { routePending: null };
var out = {};
["run the tests", "fix the session cookie", "new session fix the bug", "start another agent",
 "spin one up", "switch to api docs", "what sessions do I have", "start a new session",
 "spin up an agent", "agents", "make it sessional", "an agentic loop"].forEach(function (t) { out[t] = voiceRouteSafe(t); });
voice.routePending = { data: { kind: "pick" }, at: Date.now() };
out.pending = voiceRouteSafe("the first one");
voice.routePending = { data: { kind: "pick" }, at: Date.now() - VOICE_PENDING_MS - 1 };
out.stale = voiceRouteSafe("the first one");
console.log(JSON.stringify(out));
"""
    )
    out = _run(script)
    assert out["run the tests"] is True and out["switch to api docs"] is True
    # Anything that may start a session (the router's "new") waits for its final.
    for t in (
        "fix the session cookie",
        "new session fix the bug",
        "start another agent",
        "spin one up",
        "what sessions do I have",
        "start a new session",
        "spin up an agent",
        "agents",
    ):
        assert out[t] is False, t
    # Whole words only: a word that merely starts with one goes early.
    assert out["make it sessional"] is True and out["an agentic loop"] is True
    # The router's own question being answered: never early; a stale one is gone.
    assert out["pending"] is False and out["stale"] is True


def test_over_asks_the_router_while_end_makes_the_final():
    js = _js()
    stt = _stt(js)
    fire = _inner(stt, "controlFire")
    # The hook goes before /end (finish), after the fast words are kept.
    assert fire.index("g.fast = c.text;") < fire.index(
        "if (mine.h.onEarly) mine.h.onEarly(c.text, g.spokeAt);"
    )
    assert fire.index("mine.h.onEarly(c.text, g.spokeAt);") < fire.index("finish();")
    assert (
        "onEarly: function (t, at) { if (voiceIsListening()) voiceRouteEarly(t, mode, voiceSpokeAt(at)); },"
        in _whole(js, "voiceRecStart")
    )
    early = _whole(js, "voiceRouteEarly")
    assert 'if (p.kind !== "over" && p.kind !== "out") return;' in early
    # The box without the guess decides, as voiceHeard's does after it takes the guess back.
    assert "!voice.box && !!p.text && !base.trim() && !L.routing" in early
    assert 'voiceStripNote("Sending…");' in early
    assert 'answer: voiceRoute(p.text, "", true, spokeAt) };' in early
    route = _whole(js, "voiceRoute")
    # The early answer is used once, for the same words in the same listen, fresh.
    assert (
        "early.gen === L.gen && early.norm === voiceNorm(text) && Date.now() - early.at < VOICE_EARLY_MAX_MS"
        in route
    )
    assert "if (!fresh) L.early = null;" in route
    assert "return early.answer;" in route
    # voiceHeard still routes the phrase the one way.
    assert 'voiceRoute(p.text, "", false, p.spokeAt).then(function (res) {' in _whole(
        js, "voiceHeard"
    )


# --------------------------------------------------------------------------- #
# No echoes, no double sends, no dead mic, barge-in
# --------------------------------------------------------------------------- #
def test_a_send_to_another_session_is_a_caption_never_spoken():
    send = _whole(_js(), "voiceSend")
    other = send.split("if (r.ok && other) {")[1].split("\n          }\n")[0]
    assert "speakLine(" not in other
    assert (
        'voiceIdle(j && j.queued ? "Queued for " + name + "." : "Sent to " + name + ".");'
        in other
    )


@needs_node
def test_a_send_that_went_takes_its_words_out_of_the_box_whatever_the_state():
    js = _js()
    send = _whole(js, "voiceSend")
    # Before the state check: a Cancel or a hidden page mid-send cannot leave them.
    assert send.index("if (r.ok && !other) voiceSentTake(box, text);") < send.index(
        'if (voice.state !== "sending") return;'
    )
    script = (
        "var composeEl = {}; function autosizeCompose() {}\n"
        + _whole(js, "voiceSentTake")
        + """
function run(v, t) { var b = { value: v }; voiceSentTake(b, t); return b.value; }
console.log(JSON.stringify([run("fix it", "fix it"), run("  fix it ", "fix it"), run("fix it and more", "fix it"),
  run("something else", "fix it"), run("", "fix it"), run("fix it", "")]));
"""
    )
    assert _run(script) == ["", "", "and more", "something else", "", "fix it"]


def test_a_lost_microphone_is_noticed_and_asked_for_again():
    stt = _stt(_js())
    gs = _inner(stt, "getStream")
    assert 't.addEventListener("ended", function () { micLost(s); });' in gs
    assert (
        'addEventListener("devicechange", function () { if (stream && !micLive(stream)) micLost(stream); })'
        in stt
    )
    lost = _inner(stt, "micLost")
    assert "stream = null;" in lost and "carryDrop();" in lost
    # What was heard still goes; an empty phrase ends (the machine restarts → a new getUserMedia, or its error).
    assert "if (g.heard || g.words) { finish(); return; }" in lost
    assert "if (mine.h.onEnd) mine.h.onEnd();" in lost
    # A dead microphone's graph is never carried to the next phrase.
    assert "micLive(g.mic)" in _inner(stt, "recEnd")


def test_barge_in_by_voice_is_opt_in_and_keeps_the_first_word():
    js = _js()
    assert "var VOICE_BARGE_RMS = 0.04;" in js and "var VOICE_BARGE_X = 3;" in js
    assert (
        "var VOICE_BARGE_BLOCKS = 3;" in js and "var VOICE_BARGE_LEARN_MS = 300;" in js
    )
    assert 'localStorage.getItem("cs_voice_barge")' in _whole(js, "voiceBargeOn")
    stt = _stt(js)
    blk = _inner(stt, "recBlock")
    deaf = blk.split("if (voiceMicDeaf()) {")[1].split("\n      }\n")[0]
    assert "if (mine && h.onBarge) recBarge(g, mine, h, d, q, now);" in deaf
    assert "g.barge = null;" in blk  # a new line learns its own level
    b = _inner(stt, "recBarge")
    assert "q <= Math.max(VOICE_BARGE_RMS, B.peak * VOICE_BARGE_X)" in b
    assert "B.run.length < VOICE_BARGE_BLOCKS || !voiceBargeOn()" in b
    assert b.index("h.onBarge();") < b.index(
        "g.blocks.push(b);"
    )  # cut first, then the words count
    assert (
        "onBarge: function () { if (voiceIsListening()) voiceBargeIn(); },"
        in _whole(js, "voiceRecStart")
    )


@needs_node
def test_barge_in_is_on_only_when_the_flag_says_so():
    js = _js()
    script = (
        """
var store = {}, broken = false;
var localStorage = { getItem: function (k) { if (broken) throw new Error("denied"); return k in store ? store[k] : null; } };
"""
        + _whole(js, "voiceBargeOn")
        + """
var out = [voiceBargeOn()];
["1", "true", "yes", "0", "false", ""].forEach(function (v) { store.cs_voice_barge = v; out.push(voiceBargeOn()); });
store.cs_voice_barge = "1"; broken = true; out.push(voiceBargeOn());
console.log(JSON.stringify(out));
"""
    )
    # unset, "1", "true", "yes", "0", "false", "", storage refusing → off.
    assert _run(script) == [False, True, True, True, False, False, False, False]


@needs_node
def test_a_queued_line_waits_for_the_recognizers_quiet():
    js = _js()
    script = (
        _consts(
            js, "VOICE_MID_WORDS_MS", "VOICE_MID_QUIET_MS", "VOICE_MID_QUIET_PHONE_MS"
        )
        + _whole(js, "voiceMidPhrase")
        + """
var voice = { stt: { name: "phone" } };
function at(stt, wordAgo, loudAgo) {
  var now = Date.now();
  voice.stt.name = stt; voice.wordAt = now - wordAgo; voice.loudAt = loudAgo == null ? 0 : now - loudAgo;
  return voiceMidPhrase();
}
console.log(JSON.stringify({
  phone500: at("phone", 500), phone700: at("phone", 700), phone900: at("phone", 900),
  server200: at("server", 100, 200), server300: at("server", 100, 300), serverOld: at("server", 2000, 10),
}));
"""
    )
    out = _run(script)
    # The phone's recognizer (no level): held until 800 ms after its last word.
    assert (
        out["phone500"] is True and out["phone700"] is True and out["phone900"] is False
    )
    # The server's recorder: 250 ms since the voice was last loud.
    assert out["server200"] is True and out["server300"] is False
    # Words older than VOICE_MID_WORDS_MS: not mid-sentence at all.
    assert out["serverOld"] is False


def test_the_numbers_are_the_documented_ones():
    js = _js()
    want = {
        "VOICE_CARRY_MAX_MS": "10000",
        "VOICE_SPEAK_KEEP": "8",
        "VOICE_SPEAK_FETCH_MAX_MS": "30000",
        "VOICE_SPEAK_RESUME_MS": "400",
        "VOICE_SPEAK_WAIT_MIN_MS": "4000",
        "VOICE_SPEAK_WAIT_MAX_MS": "20000",
        "VOICE_MID_QUIET_MS": "250",
        "VOICE_MID_QUIET_PHONE_MS": "800",
        "VOICE_BARGE_LEARN_MS": "300",
        "VOICE_BARGE_RMS": "0.04",
        "VOICE_BARGE_X": "3",
        "VOICE_BARGE_BLOCKS": "3",
        "VOICE_PENDING_MS": "30000",
        "VOICE_EARLY_MAX_MS": "10000",
        "VOICE_HOLD_POLL_MS": "50",
        "VOICE_HOLD_MAX_MS": "10000",
        "VOICE_HOLD_REPLY_MS": "3000",
        "VOICE_ANSWER_AFTER_MS": "300",
        "VOICE_SPOKE_LEAD_MS": "2000",
        "VOICE_MIC_PAUSE_REASK_MS": "5000",
        "VOICE_STREAM_DELETE_WAIT_MS": "400",
        "VOICE_STREAM_FULL_RETRY_MS": "300",
        "VOICE_STREAM_FULL_MAX_MS": "1500",
    }
    got = {n: _consts(js, n).strip().split(" = ", 1)[1].split(";", 1)[0] for n in want}
    assert got == want


def test_the_phone_recognizer_waits_a_breath_before_a_line_plays():
    js = _js()
    assert "var VOICE_MID_QUIET_PHONE_MS = 800;" in js
    assert "(phone ? VOICE_MID_QUIET_PHONE_MS : VOICE_MID_QUIET_MS)" in _whole(
        js, "voiceMidPhrase"
    )


# --------------------------------------------------------------------------- #
# Cycle 2: the carried phrase is its own listen's only (safety)
# --------------------------------------------------------------------------- #
@needs_node
def test_the_carry_key_is_dictation_only_and_names_its_listen():
    script = _whole(_js(), "voiceCarryKey") + """
var box = { id: "asking-text" };
console.log(JSON.stringify([
  voiceCarryKey("prompt", 3, null), voiceCarryKey("prompt", 3, box), voiceCarryKey("prompt", 4, null),
  voiceCarryKey("dialog", 3, null), voiceCarryKey("answer", 3, null), voiceCarryKey("prompt", 3, {}),
]));
"""
    out = _run(script)
    assert out == [
        "prompt#3#compose",
        "prompt#3#asking-text",
        "prompt#4#compose",
        "",  # a dialog's listen: never carries, never takes
        "",  # an answer's: the same
        "prompt#3#box",
    ]


def test_a_carried_phrase_is_taken_only_by_the_listen_it_was_recorded_in():
    js = _js()
    stt = _stt(js)
    # The state machine hands its listen's key to every start, and a phrase
    # carries only while it is still that listen.
    rs = _whole(js, "voiceRecStart")
    assert "var carryKey = voiceCarryKey(mode, voiceListen_.gen, voice.box);" in rs
    assert "carryKey: carryKey," in rs
    carry = rs.split("carry: function (t) {")[1].split("\n      },\n")[0]
    assert (
        "if (!carryKey || carryKey !== voiceCarryKey(L.mode, L.gen, voice.box)) return false;"
        in carry
    )
    # The recorder binds the carried phrase to the key; none → never carried.
    end = _inner(stt, "recEnd")
    assert 'var key = mine.h.carryKey || "";' in end
    assert "if (key && g.proc" in end
    assert "c.key = key;" in _inner(stt, "carryFrom")
    # start() takes it only with the same key; any other start (a dialog's,
    # an answer's: no key) drops it and records why.
    start = _inner(stt, "start")
    assert "c && c.key && c.key === h.carryKey && c.proc" in start
    assert "voice.rec.carryRefused = {" in start
    assert start.index("voice.rec.carryRefused") < start.index("carryDrop();\n      }")
    # The test hook the harness's safety scenario starts a dialog's listen with.
    assert "voice.listenFor = function (mode, ms, d) {" in js


# --------------------------------------------------------------------------- #
# Cycle 2: a line's sound waits for the owner's pause
# --------------------------------------------------------------------------- #
@needs_node
def test_a_clip_ready_while_the_owner_talks_waits_for_the_pause():
    out = _tts_run("""
heldUntil = Date.now() + 150;   // the owner is talking for another 150 ms
var t0 = Date.now();
await tts.speak("One.");
out.waited = plays[0].at - t0;
out.held = voice.rec.held;
// Cut while held: nothing ever sounds, the line resolves (the queue never stalls).
heldUntil = Date.now() + 10000;
var n = plays.length, done = false;
var p = tts.speak("Two.").then(function () { done = true; });
await sleep(60);
tts.cancel();
await sleep(60);
out.cutDone = done;
out.cutPlays = plays.length - n;
// Nobody talking: at once (no poll).
heldUntil = 0;
var t1 = Date.now();
await tts.speak("Three.");
out.direct = plays[plays.length - 1].at - t1;
""")
    assert out["waited"] >= 140
    assert out["held"]["n"] == 2 and out["held"]["waiting"] is False
    assert out["cutDone"] is True and out["cutPlays"] == 0
    assert out["direct"] < 40


@needs_node
def test_the_phone_voice_waits_for_the_pause_too():
    js = _js()
    script = (
        r"""
var VOICE_WORD_MS = 10, VOICE_CHUNK_GRACE_MS = 20;
var voice = { prefs: { rate: 1 }, rec: {} }, utts = [];
var heldUntil = Date.now() + 120, VOICE_HOLD_POLL_MS = 10, VOICE_HOLD_MAX_MS = 1000, VOICE_HOLD_REPLY_MS = 3000;
var voiceSpeech = { playing: null };
function voiceStripPaint() {}
function voiceSpeakHeld() { return Date.now() < heldUntil; }
function voiceClipEnded() {}
function sentenceChunks(t) { return String(t).split(/(?<=\.)\s+/).filter(Boolean); }
function voiceShapeSpoken(t) { return t; }
function SpeechSynthesisUtterance(t) { this.text = t; }
var window = { SpeechSynthesisUtterance: SpeechSynthesisUtterance,
  speechSynthesis: { speak: function (u) { u.at = Date.now(); utts.push(u); }, cancel: function () {}, getVoices: function () { return []; } } };
"""
        + _whole(js, "voiceSpeakWhenQuiet")
        + _whole(js, "PhoneTTS")
        + """
var t = PhoneTTS(), t0 = Date.now();
t.speak("Held.").then(function () {
  console.log(JSON.stringify({ waited: utts[0].at - t0, audibleWhileHeld: false }));
});
"""
    )
    out = _run(script)
    assert out["waited"] >= 110


@needs_node
def test_who_holds_a_lines_sound():
    js = _js()
    script = (
        "var VOICE_MID_QUIET_MS = 250, VOICE_MID_QUIET_PHONE_MS = 800;\n"
        "var voiceSpeech = { playing: null }, listening = true;\n"
        "function voiceIsListening() { return listening; }\n"
        "var voice = { stt: { name: 'server' }, loudAt: 0, wordAt: 0 };\n"
        + _whole(js, "voiceSpeakHeld")
        + """
var now = Date.now(), out = [];
voiceSpeech.playing = { over: true };
voice.loudAt = now - 100; out.push(voiceSpeakHeld());      // talking (level), over the listen: held
voice.loudAt = now - 400; out.push(voiceSpeakHeld());      // a pause: goes
voice.loudAt = now; voiceSpeech.playing = { over: false };
out.push(voiceSpeakHeld());                               // not said over a listen: never held
voiceSpeech.playing = { over: true }; listening = false;
out.push(voiceSpeakHeld());                               // the listen is over: goes
listening = true; voice.stt.name = "phone"; voice.loudAt = 0;
voice.wordAt = now - 500; out.push(voiceSpeakHeld());     // the phone's recognizer: a word 0.5 s ago
voice.wordAt = now - 900; out.push(voiceSpeakHeld());
console.log(JSON.stringify(out));
"""
    )
    assert _run(script) == [True, False, False, False, True, False]


@needs_node
def test_the_phone_recognizer_hears_the_owner_until_the_line_sounds():
    js = _js()
    script = (
        "var VOICE_NOD_MUTE_MS = 150, voiceSpeech = { playing: null }, audible = false;\n"
        "function voiceTtsAudible() { return audible; }\n"
        "var voice = { stt: { name: 'phone' }, micMuteUntil: 0, lineEndAt: 0 };\n"
        + _whole(js, "voiceEchoing")
        + """
var out = [];
voiceSpeech.playing = { over: true };
out.push(voiceEchoing());                 // being made / held: the owner, not echo
audible = true; out.push(voiceEchoing()); // sounding: echo
audible = false; voiceSpeech.playing.sounded = true;
out.push(voiceEchoing());                 // between its sentences: still its echo
voice.stt.name = "server"; out.push(voiceEchoing());   // the server recorder is deafened instead
console.log(JSON.stringify(out));
"""
    )
    assert _run(script) == [False, True, True, False]
    assert "if (voiceSpeech.playing) voiceSpeech.playing.sounded = true;" in _whole(
        js, "voiceClipEnded"
    )
    assert "line.sounded = false;" in _whole(js, "voiceSpeechPump")


def test_both_voices_hold_their_sound_for_the_pause():
    js = _js()
    said = _inner(_tts(js), "said")
    assert (
        "if (x.audio) voiceSpeakWhenQuiet(function () { return g === gen; }, sound); else sound(true);"
        in said
    )
    phone = _whole(js, "PhoneTTS")
    assert (
        "voiceSpeakWhenQuiet(function () { return g === gen; }, function (go) { if (go) say(); else resolve(); });"
        in phone
    )


# --------------------------------------------------------------------------- #
# Cycle 2: a muted microphone is a pause, not a loss
# --------------------------------------------------------------------------- #
def test_a_muted_microphone_is_a_pause_not_a_loss():
    stt = _stt(_js())
    live = _inner(stt, "micLive")
    assert 't.readyState !== "ended"' in live and "muted" not in live
    assert "return !!t.muted;" in _inner(stt, "micMuted")
    gs = _inner(stt, "getStream")
    assert 't.addEventListener("mute", function () { micPause(s, true); });' in gs
    assert 't.addEventListener("unmute", function () { micPause(s, false); });' in gs
    pause = _inner(stt, "micPause")
    assert "voice.rec.micPaused = on ? Date.now() : 0;" in pause
    assert "voiceStripPaint();" in pause
    assert "micLost" not in pause and "finish()" not in pause
    # A paused block: nothing recorded, the silence clock stands still —
    # checked right after the deaf branch (which already records nothing).
    blk = _inner(stt, "recBlock")
    paused = blk.split("if (micMuted(g.mic)) {")[1].split("\n      }\n")[0]
    assert "g.lastLoud = Math.min(now, g.lastLoud + (n * 1000) / g.rate);" in paused
    assert "g.blocks.push" not in paused and "return;" in paused
    assert blk.index("if (voiceMicDeaf()) {") < blk.index("if (micMuted(g.mic)) {")
    assert blk.index("if (micMuted(g.mic)) {") < blk.index("g.blocks.push(block);")
    assert "var t = paused ? VOICE_MIC_PAUSED_TEXT" in _inner(stt, "recStatus")
    assert "function paused() { return !!(token || carry) && micMuted(stream); }" in stt
    assert "paused: paused," in stt


@needs_node
def test_the_strip_says_the_mic_is_paused():
    script = _whole(_js(), "voiceStripWord") + """
console.log(JSON.stringify([voiceStripWord("listening", false, "", true), voiceStripWord("dialog", false, "", true),
  voiceStripWord("listening", false, ""), voiceStripWord("listening", true, "", true), voiceStripWord("idle", false, "", true)]));
"""
    assert _run(script) == ["Mic paused…", "Mic paused…", "Listening…", "Speaking…", ""]


# --------------------------------------------------------------------------- #
# Cycle 2: the server's stream cap is passing, never a sticky downgrade
# --------------------------------------------------------------------------- #
@needs_node
def test_a_cap_409_is_told_from_a_backend_that_does_not_stream():
    script = _whole(_js(), "voiceStreamFull") + """
console.log(JSON.stringify([
  voiceStreamFull({ error: "4 voice streams are already open: end one first" }),
  voiceStreamFull({ error: "builtin recognition does not stream: send one clip per phrase" }),
  voiceStreamFull({}), voiceStreamFull(null),
]));
"""
    assert _run(script) == [True, False, False, False]


def test_the_stream_cap_is_retried_and_deletes_land_first():
    stt = _stt(_js())
    so = _inner(stt, "streamOpen")
    # A cap 409: asked again, bounded; still full → this phrase is a clip
    # (streamFail) and streaming stays on.
    full = so.split("if (x.r.status === 409 && voiceStreamFull(x.j)) {")[1].split(
        "\n          }\n"
    )[0]
    assert "VOICE_STREAM_FULL_RETRY_MS <= VOICE_STREAM_FULL_MAX_MS" in full
    assert "deletesSettled().then(ask);" in full
    assert "streamFail(g, mine, streamWhy(x));" in full
    assert "stt_stream = false" not in full
    # Only the other 409 turns streaming off, and it comes after the cap's.
    assert so.index("voiceStreamFull(x.j)") < so.index("stt_stream = false")
    # Every open waits for the DELETEs on their way.
    assert "if (deleting.length) deletesSettled().then(ask); else ask();" in so
    sd = _inner(stt, "streamDelete")
    assert "deleting.push(p);" in sd
    assert "VOICE_STREAM_DELETE_WAIT_MS" in _inner(stt, "deletesSettled")
    # "disregard": its stream goes before the carried phrase's opens.
    fire = _inner(stt, "controlFire")
    nosend = fire.split("if (!c.send) {")[1]
    assert nosend.index("streamDrop(g);") < nosend.index("recEnd(g, mine, c.text);")


# --------------------------------------------------------------------------- #
# Cycle 2, run: the pure rules and the recorder's pieces under node
# --------------------------------------------------------------------------- #
def _cap_pattern():
    """voiceStreamFull's pattern, as a Python regex."""
    import re

    fn = _whole(_js(), "voiceStreamFull")
    m = re.search(r"return /(.+?)/(\w*)\.test\(", fn)
    assert m, fn
    return re.compile(m.group(1), re.I if "i" in m.group(2) else 0)


def test_the_cap_pattern_matches_the_servers_own_409_and_only_it(monkeypatch):
    """The phone keeps streaming on for the cap 409 and turns it off for any
    other — by the error's TEXT. Run the route itself: a reword on the server
    must keep the client's pattern matching the cap's 409 only."""
    from types import SimpleNamespace

    from backend.web.routes import speech as routes

    pattern = _cap_pattern()
    backend = SimpleNamespace(
        id="local",
        streams=True,
        stream_transcriber=lambda lang: object(),
        final_transcriber=lambda lang, hints: object(),
    )
    monkeypatch.setattr(routes, "_refusal", lambda kind: (backend, None))
    monkeypatch.setattr(routes, "_hints", lambda: "")
    monkeypatch.setattr(routes._stream, "open_stream", lambda *a, **k: None)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    cap = routes.voice_stream_open(request, {"lang": "en-US"})
    assert cap.status_code == 409
    err = json.loads(cap.body)["error"]
    assert str(routes._stream.MAX_STREAMS) in err and pattern.search(err)
    backend.streams = False
    nostream = routes.voice_stream_open(request, {"lang": "en-US"})
    assert nostream.status_code == 409
    assert not pattern.search(json.loads(nostream.body)["error"])
    # No ready backend: core.speech.select's reasons, never the cap's.
    for why in (
        "no recognition backend is ready (local models not installed)",
        "unknown recognition backend 'x' (one of local, builtin)",
    ):
        assert not pattern.search(why)


@needs_node
def test_the_cap_409_rule_on_every_other_body():
    script = _whole(_js(), "voiceStreamFull") + """
console.log(JSON.stringify([
  voiceStreamFull({ error: "local recognition does not stream: send one clip per phrase" }),
  voiceStreamFull({ error: "no recognition backend is ready (local models not installed)" }),
  voiceStreamFull(undefined), voiceStreamFull({ error: null }), voiceStreamFull({ error: "END ONE FIRST" }),
]));
"""
    assert _run(script) == [False, False, False, False, True]


@needs_node
def test_every_listen_but_dictation_has_no_carry_key():
    script = _whole(_js(), "voiceCarryKey") + """
console.log(JSON.stringify([
  voiceCarryKey("wake", 3, null), voiceCarryKey("", 3, { id: "b" }), voiceCarryKey(undefined, 3, null),
  voiceCarryKey("prompt", 3, { id: "a" }) === voiceCarryKey("prompt", 3, { id: "b" }),
  voiceCarryKey("prompt", 3, { id: "a" }) === voiceCarryKey("prompt", 4, { id: "a" }),
  voiceCarryKey("prompt", 3, { id: "a" }) === voiceCarryKey("prompt", 3, { id: "a" }),
]));
"""
    assert _run(script) == ["", "", "", False, False, True]


def _hold_run(body: str):
    js = _js()
    script = (
        "var VOICE_HOLD_POLL_MS = 10, VOICE_HOLD_MAX_MS = 200, VOICE_HOLD_REPLY_MS = 3000;\n"
        "var voice = { rec: {} }, held = false, voiceSpeech = { playing: null }, paints = [];\n"
        "function voiceStripPaint() { paints.push(!!voice.holding); }\n"
        "function voiceSpeakHeld() { return typeof held === 'function' ? held() : held; }\n"
        "function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }\n"
        + _whole(js, "voiceSpeakWhenQuiet")
        + "var out = {};\n(async function () {\n"
        + body
        + "\nconsole.log(JSON.stringify(out));\n})().catch(function (e) { console.error(e); process.exit(1); });\n"
    )
    return _run(script)


@needs_node
def test_the_hold_goes_at_once_waits_for_the_pause_gives_up_and_is_capped():
    out = _hold_run("""
// Nobody talking: go(true) synchronously, nothing counted.
var calls = [];
voiceSpeakWhenQuiet(function () { return true; }, function (go) { calls.push(go); });
out.sync = calls.slice();
out.rec0 = voice.rec.held || null;
// Talking for 60 ms: go(true) after the pause, R.ms ~ the hold.
held = true;
var t0 = Date.now(), at = 0;
voiceSpeakWhenQuiet(function () { return true; }, function (go) { calls.push(go); at = Date.now() - t0; });
out.waitingDuring = voice.rec.held.waiting;
await sleep(60);
held = false;
await sleep(40);
out.after = calls.slice(1);
out.at = at;
out.R1 = JSON.parse(JSON.stringify(voice.rec.held));
// The line is cut mid-hold: go(false), never go(true).
held = true;
var alive = true, cut = [];
voiceSpeakWhenQuiet(function () { return alive; }, function (go) { cut.push(go); });
await sleep(30);
alive = false;
await sleep(40);
out.cut = cut;
out.R2 = JSON.parse(JSON.stringify(voice.rec.held));
// Talking for ever: go(true) at VOICE_HOLD_MAX_MS, still held.
var t2 = Date.now(), capped = [], cappedAt = 0;
voiceSpeakWhenQuiet(function () { return true; }, function (go) { capped.push(go); cappedAt = Date.now() - t2; });
await sleep(320);
out.capped = capped;
out.cappedAt = cappedAt;
out.R3 = JSON.parse(JSON.stringify(voice.rec.held));
""")
    assert out["sync"] == [True] and out["rec0"] is None
    assert out["waitingDuring"] is True
    assert out["after"] == [True] and 55 <= out["at"] < 100
    assert out["R1"]["n"] == 1 and out["R1"]["waiting"] is False
    assert 55 <= out["R1"]["ms"] < 100
    assert out["cut"] == [False]
    assert out["R2"]["n"] == 2 and out["R2"]["waiting"] is False
    assert out["capped"] == [True] and 200 <= out["cappedAt"] < 260
    assert out["R3"]["n"] == 3 and out["R3"]["waiting"] is False
    assert 200 <= out["R3"]["ms"] < 260


@needs_node
def test_no_line_playing_holds_nothing():
    js = _js()
    script = (
        "var VOICE_MID_QUIET_MS = 250, VOICE_MID_QUIET_PHONE_MS = 800;\n"
        "var voiceSpeech = { playing: null };\n"
        "function voiceIsListening() { return true; }\n"
        "var voice = { stt: { name: 'phone' }, loudAt: Date.now(), wordAt: Date.now() };\n"
        + _whole(js, "voiceSpeakHeld")
        + """
var out = [voiceSpeakHeld()];
voiceSpeech.playing = { over: true }; voice.wordAt = 0; out.push(voiceSpeakHeld());   // never a word: goes
voice.stt.name = "server"; voice.loudAt = 0; out.push(voiceSpeakHeld());
console.log(JSON.stringify(out));
"""
    )
    assert _run(script) == [False, False, False]


@needs_node
def test_the_echo_window_after_a_mute_or_a_lines_end():
    js = _js()
    script = (
        "var VOICE_NOD_MUTE_MS = 150, voiceSpeech = { playing: null };\n"
        "function voiceTtsAudible() { return false; }\n"
        "var voice = { stt: { name: 'phone' }, micMuteUntil: 0, lineEndAt: 0 };\n"
        + _whole(js, "voiceEchoing")
        + """
var now = Date.now(), out = [];
voiceSpeech.playing = { over: true, sounded: false };
voice.micMuteUntil = now + 500; out.push(voiceEchoing());   // a confirmation's mute window is open: echo
voice.micMuteUntil = now - 1; out.push(voiceEchoing());     // passed, not sounded: the owner
voiceSpeech.playing = null;
voice.lineEndAt = now - 50; out.push(voiceEchoing());       // a line just ended: its tail is still echo
voice.lineEndAt = now - 400; out.push(voiceEchoing());      // past VOICE_NOD_MUTE_MS
voiceSpeech.playing = { over: false, sounded: true }; out.push(voiceEchoing());   // not over the listen
console.log(JSON.stringify(out));
"""
    )
    assert _run(script) == [True, False, True, False, False]


@needs_node
def test_the_strip_pauses_every_listening_state():
    script = _whole(_js(), "voiceStripWord") + """
console.log(JSON.stringify([voiceStripWord("awaiting", false, "", true), voiceStripWord("awaiting", false, "", false),
  voiceStripWord("dialog", false, ""), voiceStripWord("sending", false, "", true), voiceStripWord("listening", false, "Stopped.", true)]));
"""
    assert _run(script) == [
        "Mic paused…",
        "Listening…",
        "Listening…",
        "Sending…",
        "Stopped.",
    ]


@needs_node
def test_a_muted_track_is_live_an_ended_one_is_not():
    stt = _stt(_js())
    script = _inner(stt, "micLive") + _inner(stt, "micMuted") + """
function S(tracks, active) { return { active: active, getAudioTracks: function () { return tracks; } }; }
var boom = { getAudioTracks: function () { throw new Error("gone"); } };
console.log(JSON.stringify({
  live: [micLive(S([{ readyState: "live", muted: true }])), micLive(S([{ readyState: "ended", muted: false }])),
         micLive(S([{ readyState: "live" }], false)), micLive(null), micLive(boom)],
  muted: [micMuted(S([{ readyState: "live", muted: false }, { readyState: "live", muted: true }])),
          micMuted(S([{ readyState: "live", muted: false }])), micMuted(null), micMuted(boom)],
}));
"""
    out = _run(script)
    # (a throwing getAudioTracks: live — never a false loss; not muted)
    assert out["live"] == [True, False, False, False, True]
    assert out["muted"] == [True, False, False, False]


@needs_node
def test_a_recent_mute_reuses_the_mic_an_old_or_unknown_one_asks_again():
    js = _js()
    stt = _stt(js)
    script = (
        _consts(js, "VOICE_MIC_PAUSE_REASK_MS")
        + "var clock = 100000; Date.now = function () { return clock; };\n"
        "var stream = null, asking = null, keepTimer = null, gum = 0, stops = 0, fresh = { fresh: true, getAudioTracks: function () { return []; } };\n"
        "function micStop(s) { if (s) stops++; }\n"
        "function voiceAudioSession() {}\n"
        "var window = { isSecureContext: true };\n"
        # (node 21+ has a read-only global navigator: replace it)
        "Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: function () { gum++; return Promise.resolve(fresh); } } } });\n"
        + _inner(stt, "micLive")
        + _inner(stt, "micMuted")
        + _inner(stt, "getStream")
        + """
function muted(at) { return { __mutedAt: at, getAudioTracks: function () { return [{ readyState: "live", muted: true }]; } }; }
(async function () {
  var out = [];
  for (var at of [clock - 1000, clock - 6000, 0, null]) {
    var s = at === null ? { getAudioTracks: function () { return [{ readyState: "live", muted: false }]; } } : muted(at);
    stream = s; gum = 0; stops = 0;
    var got = await getStream();
    out.push({ reused: got === s, gum: gum, stops: stops });
  }
  console.log(JSON.stringify(out));
})();
"""
    )
    assert _run(script) == [
        {
            "reused": True,
            "gum": 0,
            "stops": 0,
        },  # muted 1 s ago: it comes back by itself
        {
            "reused": False,
            "gum": 1,
            "stops": 1,
        },  # 6 s: asked for anew, the old one stopped first
        {"reused": False, "gum": 1, "stops": 1},  # muted since before we knew
        {"reused": True, "gum": 0, "stops": 0},  # not muted
    ]


@needs_node
def test_a_pause_is_the_current_mics_and_unmute_repaints_the_words():
    stt = _stt(_js())
    script = (
        "var Date_ = Date.now; Date.now = function () { return 5000; };\n"
        "var voice = { rec: {} }, painted = 0, interim = [], status = [];\n"
        "function voiceStripPaint() { painted++; }\n"
        "function voiceJoin(a, b) { return [a, b].filter(Boolean).join(' '); }\n"
        "function recStatus(g, h) { status.push(g.said); }\n"
        "var mic = { id: 'mic' }, other = { id: 'other' };\n"
        "var stream = mic, rec = null, token = null;\n" + _inner(stt, "micPause") + """
var out = {};
micPause(other, true);                        // not our microphone: nothing
out.other = { paused: voice.rec.micPaused || 0, painted: painted, untouched: !("__mutedAt" in other) };
var g = { mic: other, said: "Listening… 2 s", words: true, pend: "rename the", partial: "config" };
rec = g; token = { h: { onInterim: function (t, p) { interim.push([t, p]); } } };
micPause(mic, true);                          // ours, but the recording is on another mic: no caption touched
out.foreign = { paused: voice.rec.micPaused, pauses: voice.rec.micPauses, at: mic.__mutedAt, said: g.said, status: status.length };
g.mic = mic;
micPause(mic, true);
out.on = { said: g.said, status: status.length, interim: interim.length, pauses: voice.rec.micPauses };
micPause(mic, false);
out.off = { paused: voice.rec.micPaused, at: mic.__mutedAt, interim: interim, status: status.length, painted: painted };
console.log(JSON.stringify(out));
"""
    )
    out = _run(script)
    assert out["other"] == {"paused": 0, "painted": 0, "untouched": True}
    assert out["foreign"] == {
        "paused": 5000,
        "pauses": 1,
        "at": 5000,
        "said": "Listening… 2 s",
        "status": 0,
    }
    # Paused with words heard: the caption is redrawn (recStatus says Mic paused).
    assert out["on"] == {"said": "", "status": 1, "interim": 0, "pauses": 2}
    # Unmuted: the words come back, joined as one phrase.
    assert out["off"]["paused"] == 0 and out["off"]["at"] == 0
    assert out["off"]["interim"] == [["rename the config", "config"]]
    assert (
        out["off"]["status"] == 1 and out["off"]["painted"] == 3
    )  # once per pause/unmute of ours


@needs_node
def test_a_muted_block_records_nothing_and_the_silence_clock_stands_still():
    stt = _stt(_js())
    script = (
        "var clock = 10000; Date.now = function () { return clock; };\n"
        "var VOICE_SILENCE_MS = 1100, muted = true, levels = [], ended = 0;\n"
        "var g = { mic: {}, heard: true, rate: 16000, lastLoud: 10000, blocks: [], frames: 0, queued: 0 };\n"
        "var token = { h: {} }, rec = g, carry = null;\n"
        "function voiceMicDeaf() { return false; }\n"
        "function micMuted() { return muted; }\n"
        "function finish() { ended++; }\n"
        "var h = { onLevel: function (l) { levels.push(l); } };\n"
        + _inner(stt, "recBlock")
        + """
function ev(n) { return { inputBuffer: { length: n, getChannelData: function () { return new Float32Array(n); } } }; }
var out = {};
// 3 s of mute, 4096-frame blocks (256 ms at 16 kHz), each delivered in time.
for (var i = 0; i < 12; i++) { clock += 256; recBlock(g, token, h, ev(4096)); }
out.blocks = g.blocks.length; out.frames = g.frames; out.levels = levels.every(function (l) { return l === 0; }) && levels.length;
out.behind = clock - g.lastLoud;   // the clock moved with the blocks: no quiet counted
// A burst of blocks delivered late: never pushed past now.
clock += 10; recBlock(g, token, h, ev(16000));
out.capped = g.lastLoud === clock;
out.ended = ended;
console.log(JSON.stringify(out));
"""
    )
    out = _run(script)
    assert out["blocks"] == 0 and out["frames"] == 0
    assert out["levels"] == 12
    assert (
        out["behind"] == 0
    )  # 12 × 256 ms of mute: the 1.1 s silence clock never started
    assert out["capped"] is True and out["ended"] == 0


def _stream_open_run(body: str):
    js = _js()
    stt = _stt(js)
    script = (
        _consts(js, "VOICE_STREAM_FULL_RETRY_MS", "VOICE_STREAM_FULL_MAX_MS")
        + _whole(js, "voiceStreamFull")
        + r"""
var clock = 0, timers = [];
Date.now = function () { return clock; };
setTimeout = function (fn, ms) { timers.push({ at: clock + (ms || 0), fn: fn }); return timers.length; };
function flush() { return new Promise(function (r) { setImmediate(r); }); }
async function runAll() {
  for (var k = 0; k < 200; k++) {
    await flush(); await flush();
    if (!timers.length) return;
    timers.sort(function (a, b) { return a.at - b.at; });
    var t = timers.shift(); clock = t.at; t.fn();
  }
}
var voice = { rec: {}, cfg: { server_ready: { stt_stream: true } } };
var asks = [], answer = null, fails = [], deleting = [], live = null, inflight = 0, maxInflight = 0;
var VOICE_CLIENT_ID = "m-test";
function lang() { return "en-US"; }
function streamReq(g, path, opts) {
  asks.push(clock); inflight++; maxInflight = Math.max(maxInflight, inflight);
  var a = answer(asks.length);
  return Promise.resolve().then(function () { inflight--; return a; });
}
function deletesSettled() { return Promise.resolve(); }
function streamDelete() {}
function streamFail(g, mine, why) { fails.push({ at: clock, why: why }); }
function streamWhy(x) { return (x && x.j && x.j.error) || "?"; }
function voiceTraceMark() {}
function streamPump() {}
function cap() { return { r: { ok: false, status: 409 }, j: { error: "4 voice streams are already open: end one first" } }; }
"""
        + _inner(stt, "streamOpen")
        + "var out = {};\n(async function () {\n"
        + body
        + "\nconsole.log(JSON.stringify(out));\n})().catch(function (e) { console.error(e); process.exit(1); });\n"
    )
    return _run(script)


@needs_node
def test_the_cap_retry_is_bounded_then_a_clip_and_streaming_stays_on():
    out = _stream_open_run("""
answer = function () { return cap(); };
var g = {};
streamOpen(g, { h: {} });
await runAll();
out.asks = asks; out.fails = fails.length; out.on = voice.cfg.server_ready.stt_stream;
out.full = voice.rec.streamFull.n; out.busy = !!(g.busy || g.opening); out.max = maxInflight;
""")
    # Asked at t0 and every 300 ms while t + 300 <= 1500: never past the bound.
    assert out["asks"] == [0, 300, 600, 900, 1200, 1500]
    assert out["fails"] == 1 and out["on"] is True
    assert out["full"] == 6 and out["busy"] is False
    assert out["max"] == 1  # never two opens in flight


@needs_node
def test_a_cap_that_frees_streams_and_another_409_turns_streaming_off():
    out = _stream_open_run("""
answer = function (n) { return n < 3 ? cap() : { r: { ok: true, status: 200 }, j: { id: "s9" } }; };
var g = {};
streamOpen(g, { h: {} });
await runAll();
out.freed = { asks: asks.slice(), sid: g.sid, mode: g.mode, fails: fails.length, on: voice.cfg.server_ready.stt_stream };
asks = []; fails = [];
answer = function () { return { r: { ok: false, status: 409 }, j: { error: "local recognition does not stream: send one clip per phrase" } }; };
var g2 = {};
streamOpen(g2, { h: {} });
await runAll();
out.other = { asks: asks.length, fails: fails.length, on: voice.cfg.server_ready.stt_stream };
""")
    assert out["freed"] == {
        "asks": [0, 300, 600],
        "sid": "s9",
        "mode": "stream",
        "fails": 0,
        "on": True,
    }
    assert out["other"] == {"asks": 1, "fails": 1, "on": False}


@needs_node
def test_a_listen_that_ends_during_the_cap_wait_asks_no_more():
    out = _stream_open_run("""
answer = function () { return cap(); };
var g = {};
streamOpen(g, { h: {} });
await flush(); await flush();
out.waiting = !!(g.busy && g.opening);
g.dead = true;
await runAll();
out.asks = asks.length; out.busy = !!g.busy; out.opening = !!g.opening; out.fails = fails.length;
""")
    assert out == {
        "waiting": True,
        "asks": 1,
        "busy": False,
        "opening": False,
        "fails": 0,
    }


@needs_node
def test_an_open_waits_for_the_deletes_on_their_way_bounded():
    js = _js()
    stt = _stt(js)
    script = (
        "var VOICE_STREAM_DELETE_WAIT_MS = 80;\n"
        "var deleting = [], replies = {};\n"
        "function streamUrl(sid) { return '/api/voice/stream/' + sid; }\n"
        "function fetch(url) { return new Promise(function (res, rej) { replies[url.split('/').pop()] = { res: res, rej: rej }; }); }\n"
        "function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }\n"
        + _inner(stt, "streamDelete")
        + _inner(stt, "deletesSettled")
        + """
(async function () {
  var out = {}, t0 = Date.now();
  await deletesSettled();
  out.none = Date.now() - t0;
  streamDelete("a"); streamDelete("b"); streamDelete("");
  out.pending = deleting.length;
  var done = false, t1 = Date.now();
  deletesSettled().then(function () { done = true; });
  setTimeout(function () { replies.a.res({}); }, 10);
  setTimeout(function () { replies.b.res({}); }, 20);
  await sleep(40);
  out.landed = { done: done, left: deleting.length, ms: Date.now() - t1 };
  streamDelete("c");   // never answers
  var t2 = Date.now();
  await deletesSettled();
  out.hung = { ms: Date.now() - t2, left: deleting.length };
  streamDelete("d");
  replies.d.rej(new Error("offline"));   // a failed DELETE still leaves the list
  await sleep(5);
  out.rejected = deleting.length;
  console.log(JSON.stringify(out));
})();
"""
    )
    out = _run(script)
    assert out["none"] < 20
    assert out["pending"] == 2  # an empty id deletes nothing
    assert out["landed"]["done"] is True and out["landed"]["left"] == 0
    assert 75 <= out["hung"]["ms"] < 150 and out["hung"]["left"] == 1
    assert out["rejected"] == 1  # only the hung one is left


# --------------------------------------------------------------------------- #
# Cycle 3: an answer said before its dialog was there (R8-bis)
# --------------------------------------------------------------------------- #
def _seen_script(body: str) -> str:
    """voice.dialogSeen's functions as written, over a stub voice."""
    js = _js()
    return (
        _consts(js, "VOICE_ANSWER_AFTER_MS", "VOICE_SPOKE_LEAD_MS")
        + "var voice = { dialogSeen: {}, early: 0, state: 'listening', session: 'a', lastRoute: '' };\n"
        + "var voiceListen_ = { startedAt: 0, lastFinalAt: 0, listenAt: 0 };\n"
        + "function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }\n"
        + _whole(js, "voiceSpokeAt")
        + _whole(js, "voiceDialogNote")
        + _whole(js, "voiceDialogRows")
        + _whole(js, "voiceDialogSeenBody")
        + _whole(js, "voiceAnswerInTime")
        + "var out = {};\n(async function () {\n"
        + body
        + "\nconsole.log(JSON.stringify(out));\n})().catch(function (e) { console.error(e); process.exit(1); });\n"
    )


@needs_node
def test_an_answer_is_in_time_only_300_ms_after_this_pages_sighting():
    out = _run(_seen_script("""
voice.dialogSeen.a = { id: "d1", at: 1000 };
out.under = voiceAnswerInTime("a", "d1", 1299);
out.exact = voiceAnswerInTime("a", "d1", 1300);
out.later = voiceAnswerInTime("a", "d1", 5000);
out.before = voiceAnswerInTime("a", "d1", 900);
out.otherId = voiceAnswerInTime("a", "d2", 5000);
out.otherTitle = voiceAnswerInTime("b", "d1", 5000);
out.noSpoke = [voiceAnswerInTime("a", "d1", 0), voiceAnswerInTime("a", "d1", undefined), voiceAnswerInTime("a", "d1", NaN)];
// A dialog listen opened once the page had the dialog: every word in it is in time.
out.listenAfter = voiceAnswerInTime("a", "d1", 0, 1000);
out.listenBefore = voiceAnswerInTime("a", "d1", 0, 999);
out.numericId = (voice.dialogSeen.n = { id: "7", at: 0 }, voiceAnswerInTime("n", 7, 300));
"""))
    assert out["under"] is False and out["exact"] is True and out["later"] is True
    assert out["before"] is False
    assert out["otherId"] is False and out["otherTitle"] is False
    assert out["noSpoke"] == [False, False, False]
    assert out["listenAfter"] is True and out["listenBefore"] is False
    assert out["numericId"] is True


@needs_node
def test_the_rows_note_a_dialog_once_and_clear_only_what_they_confirmed():
    out = _run(_seen_script("""
voiceDialogRows([{ title: "a", activity: "clarify", asking: { kind: "dialog", dialog_id: "d1" } }]);
var first = voice.dialogSeen.a.at;
out.noted = voice.dialogSeen.a.id;
await sleep(15);
// The next poll, the same dialog: the first time stays.
voiceDialogRows([{ title: "a", activity: "clarify", asking: { kind: "dialog", dialog_id: "d1" } }]);
out.kept = voice.dialogSeen.a.at === first;
// A new id: a new sighting, now.
voiceDialogRows([{ title: "a", activity: "clarify", asking: { kind: "dialog", dialog_id: "d2" } }]);
out.renewed = [voice.dialogSeen.a.id, voice.dialogSeen.a.at > first];
// A dialog row with no id yet changes nothing.
voiceDialogRows([{ title: "a", activity: "clarify", asking: { kind: "dialog" } }]);
out.noIdKeeps = voice.dialogSeen.a.id;
// A clarify row asking nothing yet keeps it; a working row clears it.
voiceDialogRows([{ title: "a", activity: "clarify" }]);
out.clarifyKeeps = !!voice.dialogSeen.a;
voiceDialogRows([{ title: "a", activity: "working" }]);
out.cleared = !("a" in voice.dialogSeen);
// A sighting from a dialog read / the sheet (no row confirmed it): a
// lagging row that asks nothing never clears it.
voiceDialogNote("b", "d9");
voiceDialogRows([{ title: "b", activity: "working" }, null, {}]);
out.lagSafe = voice.dialogSeen.b && voice.dialogSeen.b.id;
// …until a row shows it and then asks nothing.
voiceDialogRows([{ title: "b", activity: "clarify", asking: { kind: "dialog", dialog_id: "d9" } }]);
voiceDialogRows([{ title: "b", activity: "idle" }]);
out.thenCleared = !("b" in voice.dialogSeen);
// A question ask is no dialog sighting.
voiceDialogRows([{ title: "c", activity: "idle", asking: { kind: "question", dialog_id: "q1" } }]);
out.question = "c" in voice.dialogSeen;
voiceDialogRows(null); voiceDialogRows(undefined);
"""))
    assert out["noted"] == "d1" and out["kept"] is True
    assert out["renewed"] == ["d2", True]
    assert out["noIdKeeps"] == "d2"
    assert out["clarifyKeeps"] is True and out["cleared"] is True
    assert out["lagSafe"] == "d9" and out["thenCleared"] is True
    assert out["question"] is False


@needs_node
def test_the_route_body_carries_ages_never_the_pages_clock_alone():
    js = _js()
    out = _run(
        _seen_script(
            """
var pending = null;
function voicePendingTake() { var p = pending; pending = null; return p; }
"""
            + _whole(js, "voiceRouteBody")
            + """
var now = Date.now();
voice.dialogSeen.a = { id: "d1", at: now - 2000 };
voice.dialogSeen.b = { id: "d2", at: now + 50 };   // a clock step: never a negative age
var b = voiceRouteBody("yes", "a", now - 400);
out.withSpoke = { text: b.text, session: b.session, ago: b.spoke_ms_ago, at: b.spoke_at === now - 400, seen: b.dialog_seen };
var nb = voiceRouteBody("yes", "a", 0);
out.noSpoke = { keys: Object.keys(nb).sort(), seen: Object.keys(nb.dialog_seen).sort() };
var ub = voiceRouteBody("yes", "a");
out.undefSpoke = "spoke_ms_ago" in ub || "spoke_at" in ub;
pending = { kind: "pick" };
out.pending = voiceRouteBody("two", "a", now).pending;
// At most 64 sessions' sightings go.
for (var i = 0; i < 100; i++) voice.dialogSeen["s" + i] = { id: "x", at: now };
out.capped = Object.keys(voiceDialogSeenBody(now)).length;
"""
        )
    )
    w = out["withSpoke"]
    assert (w["text"], w["session"]) == ("yes", "a")
    assert 400 <= w["ago"] < 450 and w["at"] is True
    assert w["seen"]["a"]["id"] == "d1" and 2000 <= w["seen"]["a"]["ms_ago"] < 2050
    assert w["seen"]["b"] == {"id": "d2", "ms_ago": 0}
    assert out["noSpoke"]["keys"] == ["dialog_seen", "session", "text"]
    assert out["noSpoke"]["seen"] == ["a", "b"]
    assert out["undefSpoke"] is False
    assert out["pending"] == {"kind": "pick"}
    assert out["capped"] == 64


@needs_node
def test_a_phrase_without_a_start_time_is_dated_early():
    out = _run(_seen_script("""
out.server = voiceSpokeAt(12345);
out.serverStr = voiceSpokeAt("12345");
// The phone recognizer: no level, no timestamp — the listen's start...
voiceListen_.startedAt = 5000; voiceListen_.lastFinalAt = 0;
out.first = voiceSpokeAt(undefined);
// ...or 2 s before the previous phrase came in, whichever is later.
voiceListen_.lastFinalAt = 9000;
out.lead = voiceSpokeAt(0);
voiceListen_.lastFinalAt = 6000;
out.startWins = voiceSpokeAt(null);
voiceListen_.startedAt = 0; voiceListen_.lastFinalAt = 0;
out.nothing = voiceSpokeAt(-1);
"""))
    assert out["server"] == 12345 and out["serverStr"] == 12345
    assert out["first"] == 5000
    assert out["lead"] == 9000 - 2000
    assert out["startWins"] == 5000
    # Nothing known: 0, which voiceAnswerInTime never takes as in time.
    assert out["nothing"] == 0


@needs_node
def test_an_answer_post_begun_too_soon_posts_nothing_and_says_so():
    js = _js()
    out = _run(
        _seen_script(
            """
var posts = [], states = [], said = [], read = 0;
function nameOf(t) { return t === "b" ? "Billing" : t; }
function fetch(url, opts) { posts.push(url); return new Promise(function () {}); }
function voiceSetState(s, w) { states.push(s); voice.state = s; }
function voiceSayThenIdle(t) { said.push(t); }
function voiceDialog() { read++; }
"""
            + _whole(js, "voiceEarly")
            + _whole(js, "voiceAnswerPost")
            + """
var now = Date.now();
voice.dialogSeen.a = { id: "d1", at: now };
voice.dialogSeen.b = { id: "d2", at: now };
// This session's dialog, begun 100 ms after the page had it: refused, read out.
voiceAnswerPost("a", "d1", ["1"], "yes", "", undefined, now + 100);
out.mine = { posts: posts.length, states: states.slice(), read: read, early: voice.early, route: voice.lastRoute };
// Another session's: refused, said with its name.
voice.state = "listening";
voiceAnswerPost("b", "d2", ["1"], "yes", "", undefined, now + 100);
out.other = { posts: posts.length, said: said.slice(), early: voice.early };
// A stale id: refused, and the newer id is noted.
voiceAnswerPost("a", "d0", ["1"], "yes", "", undefined, now + 5000);
out.stale = { posts: posts.length, seen: voice.dialogSeen.a.id };
// In time: the POST goes, pinned to the dialog.
voice.dialogSeen.a = { id: "d1", at: now - 1000 };
voiceAnswerPost("a", "d1", ["1"], "yes", "", undefined, now);
out.ok = posts.slice();
// Voice off: refused and counted, nothing said.
voice.state = "off"; said = [];
voiceAnswerPost("b", "d2", ["1"], "yes", "", undefined, 0);
out.off = { said: said.length, early: voice.early };
"""
        )
    )
    assert out["mine"]["posts"] == 0 and out["mine"]["read"] == 1
    assert out["mine"]["states"] == ["idle"] and out["mine"]["early"] == 1
    assert out["mine"]["route"] == "early (d1): nothing pressed"
    assert out["other"]["posts"] == 0
    assert out["other"]["said"] == ["Billing asked just now: say your answer again."]
    assert out["stale"] == {"posts": 0, "seen": "d0"}
    assert out["ok"] == ["/api/instances/a/answer"]
    assert out["off"] == {"said": 0, "early": 4}


@needs_node
def test_a_clarify_reads_the_dialog_at_once_even_mid_dictation():
    """The ``clarify`` event's GET /dialog goes while a listen is on: the
    page notes the dialog (so the next words can answer it) but reads
    nothing over the owner; a 204 drops the sighting."""
    js = _js()
    out = _run(
        _seen_script(
            """
var gets = [], reply = { status: 200, body: { id: "d7", parsed: true, options: [{ key: "1", kind: "yes" }] } };
voice.dialogBusy = false;
function fetch(url) {
  gets.push(url);
  var r = reply;
  return Promise.resolve({ status: r.status, ok: r.status < 300, json: function () { return Promise.resolve(r.body); } });
}
function voiceIsListening() { return true; }
"""
            + _whole(js, "voiceDialog")
            + """
voiceDialog();
await sleep(5);
out.noted = voice.dialogSeen.a && voice.dialogSeen.a.id;
out.gets = gets.slice();
out.busy = voice.dialogBusy;
// A question in prose is no dialog to note.
delete voice.dialogSeen.a;
reply = { status: 200, body: { id: "q1", kind: "question" } };
voiceDialog(); await sleep(5);
out.question = "a" in voice.dialogSeen;
// Nothing on screen (204): the sighting goes.
voice.dialogSeen.a = { id: "d7", at: 1 };
reply = { status: 204, body: null };
voiceDialog(); await sleep(5);
out.gone = "a" in voice.dialogSeen;
"""
        )
    )
    assert out["gets"] == ["/api/instances/a/dialog?quiet=1"]
    assert out["noted"] == "d7" and out["busy"] is False
    assert out["question"] is False and out["gone"] is False
    # And the event handler calls it on clarify for this session.
    on = _js()
    start = on.index("  voice.onEvent = function (f) {")
    assert 'else if (f.new === "clarify") voiceDialog();' in on[start : start + 600]


@needs_node
def test_a_reply_line_is_held_three_seconds_in_all_not_per_sentence():
    """VOICE_HOLD_REPLY_MS: a room that never goes quiet holds a line's
    sentences together for at most the reply's budget (scaled down here),
    each one also within VOICE_HOLD_MAX_MS; the strip says it is waiting."""
    js = _js()
    script = (
        "var VOICE_HOLD_POLL_MS = 10, VOICE_HOLD_MAX_MS = 200, VOICE_HOLD_REPLY_MS = 300;\n"
        "var voice = { rec: {} }, voiceSpeech = { playing: null }, paints = [];\n"
        "function voiceStripPaint() { paints.push(!!voice.holding); }\n"
        "function voiceSpeakHeld() { return true; }\n"  # talking for ever
        "function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }\n"
        + _whole(js, "voiceSpeakWhenQuiet")
        + """
function hold() {
  return new Promise(function (res) {
    var t = Date.now();
    voiceSpeakWhenQuiet(function () { return true; }, function (go) { res([go, Date.now() - t]); });
  });
}
(async function () {
  var out = {}, t0 = Date.now();
  voiceSpeech.playing = { text: "One. Two. Three." };
  out.waits = [await hold(), await hold(), await hold()];
  out.total = Date.now() - t0;
  out.left = voiceSpeech.playing.holdLeft;
  out.capped = voice.rec.held.capped;
  out.n = voice.rec.held.n;
  out.paints = paints.slice(0, 2);
  out.holding = !!voice.holding;
  // A new line has its own budget.
  voiceSpeech.playing = { text: "Next." };
  out.next = await hold();
  console.log(JSON.stringify(out));
})().catch(function (e) { console.error(e); process.exit(1); });
"""
    )
    out = _run(script)
    (g1, w1), (g2, w2), (g3, w3) = out["waits"]
    assert g1 is g2 is g3 is True
    assert 200 <= w1 < 260  # the first sentence: the per-sentence cap
    assert 80 <= w2 < 160  # the second: what is left of the reply's
    assert w3 < 10  # the third: no hold left, at once
    assert out["total"] < 400 and out["left"] == 0
    assert out["capped"] == 2 and out["n"] == 2
    assert out["paints"] == [True, False] and out["holding"] is False
    assert out["next"][0] is True and 200 <= out["next"][1] < 260
