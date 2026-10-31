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
    assert "carryFrom(g);" in end and "recStop(g);" in end
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
var voice = { prefs: { rate: 1 } }, ends = 0, utts = [];
function voiceClipEnded() { ends++; }
function sentenceChunks(t) { return String(t).split(/(?<=\.)\s+/).filter(Boolean); }
function voiceShapeSpoken(t) { return t; }
function SpeechSynthesisUtterance(t) { this.text = t; }
var window = { SpeechSynthesisUtterance: SpeechSynthesisUtterance,
  speechSynthesis: { speak: function (u) { utts.push(u); }, cancel: function () {}, getVoices: function () { return []; } } };
var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
"""
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
        "if (mine.h.onEarly) mine.h.onEarly(c.text);"
    )
    assert fire.index("mine.h.onEarly(c.text);") < fire.index("finish();")
    assert (
        "onEarly: function (t) { if (voiceIsListening()) voiceRouteEarly(t, mode); },"
        in _whole(js, "voiceRecStart")
    )
    early = _whole(js, "voiceRouteEarly")
    assert 'if (p.kind !== "over" && p.kind !== "out") return;' in early
    # The box without the guess decides, as voiceHeard's does after it takes the guess back.
    assert "!voice.box && !!p.text && !base.trim() && !L.routing" in early
    assert 'voiceStripNote("Sending…");' in early
    assert 'answer: voiceRoute(p.text, "", true) };' in early
    route = _whole(js, "voiceRoute")
    # The early answer is used once, for the same words in the same listen, fresh.
    assert (
        "early.gen === L.gen && early.norm === voiceNorm(text) && Date.now() - early.at < VOICE_EARLY_MAX_MS"
        in route
    )
    assert "if (!fresh) L.early = null;" in route
    assert "return early.answer;" in route
    # voiceHeard still routes the phrase the one way.
    assert "voiceRoute(p.text).then(function (res) {" in _whole(js, "voiceHeard")


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
    }
    got = {n: _consts(js, n).strip().split(" = ", 1)[1].split(";", 1)[0] for n in want}
    assert got == want


def test_the_phone_recognizer_waits_a_breath_before_a_line_plays():
    js = _js()
    assert "var VOICE_MID_QUIET_PHONE_MS = 800;" in js
    assert "(phone ? VOICE_MID_QUIET_PHONE_MS : VOICE_MID_QUIET_MS)" in _whole(
        js, "voiceMidPhrase"
    )
