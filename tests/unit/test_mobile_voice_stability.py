"""The phone page's voice, held to what it promises across the moments
nobody plans for (the stability sweep, 2026-11-12):

* **An upload's timeout is its own.** A clip upload stopped part way (a
  stop, Cancel, the page hiding) left its 45 s timer running, and that timer
  aborted whichever upload was current when it fired — the NEXT phrase's,
  which then failed with "the server took too long".
* **Send keeps what was typed meanwhile.** A voice send took its words out of
  the box (``voiceSentTake``: words added since stay) and then emptied the
  box anyway, so anything typed while /send was on its way was lost.
* **Send waits for the accurate words.** Tapping Send while a phrase was
  still being recognised sent the box as it stood — the fast recognizer's
  live guess — and stopped recognition, so whisper's final was dropped. The
  owner's rule: every word that goes to a session is whisper's.
* **The screen lock goes with voice.** Turning voice off while the screen
  wake lock was still being granted left the lock held (the screen never
  slept with voice off); asking twice meanwhile held two.

Functions run as written under node, with the page's collaborators stubbed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "backend" / "web" / "static"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _whole(js: str, name: str) -> str:
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return js[start:end]


def _inner(js: str, name: str) -> str:
    start = js.index("    function " + name + "(")
    end = js.index("\n    }\n", start) + len("\n    }\n")
    return js[start:end]


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# An upload's timeout is its own
# --------------------------------------------------------------------------- #
@needs_node
def test_a_stopped_uploads_timer_never_aborts_the_next_upload():
    js = _js()
    stt = js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]
    script = (
        """
(async function () {
  var timers = [];
  function setTimeout(f, ms) { timers.push(f); return timers.length; }
  function clearTimeout(id) { if (id) timers[id - 1] = null; }
  var window = { AbortController: AbortController };
  var VOICE_UPLOAD_TIMEOUT_MS = 45000;
  var token = null, uploading = false, aborter = null;
  function lang() { return "en"; }
  var voice = { rec: {} };
  function voiceJoin(a, b) { return [a, b].filter(Boolean).join(" "); }
  var reqs = [], out = [];
  function fetch(url, o) {
    return new Promise(function (resolve, reject) {
      var i = reqs.length;
      reqs.push({ resolve: resolve, aborted: false });
      o.signal.addEventListener("abort", function () {
        reqs[i].aborted = true;
        var e = new Error("aborted"); e.name = "AbortError"; reject(e);
      });
    });
  }
"""
        + _inner(stt, "upload")
        + """
  function hooks(n) {
    return { h: { onFinal: function (t) { out.push(n + " final " + t); },
                  onError: function (c, m) { out.push(n + " error " + m); } } };
  }
  var tick = function () { return new Promise(function (r) { global.setTimeout(r, 0); }); };
  // Phrase A uploads; a stop (ServerSTT discard()) drops it.
  var A = hooks("A");
  token = A;
  upload(new Blob(["a"]), "audio/wav", A, "", 0);
  token = null; uploading = false; aborter.abort(); aborter = null;
  await tick();
  // Phrase B uploads; A's 45 s comes round while B is on its way.
  var B = hooks("B");
  token = B;
  upload(new Blob(["b"]), "audio/wav", B, "", 0);
  timers.forEach(function (f, i) { if (f && i === 0) f(); });
  await tick();
  reqs[1].resolve({ ok: true, json: function () { return Promise.resolve({ text: "fix it" }); } });
  await tick(); await tick();
  console.log(JSON.stringify({ bAborted: reqs[1].aborted, out: out }));
})();
"""
    )
    got = _run(script)
    assert got == {"bAborted": False, "out": ["B final fix it"]}


# --------------------------------------------------------------------------- #
# The send path
# --------------------------------------------------------------------------- #
_SEND_STUBS = """
var calls = [];
var composeEl = { value: "" };
var current = "s1";
var voice = { state: "listening", session: "s1", box: null, prefs: { replies: "off" },
              dictated: true, stt: { stop: function () { calls.push("stt.stop"); } },
              guess: { box: null, typed: "" }, lastSay: {} };
var voiceListen_ = { timer: null, overAfter: false, overBy: 0, until: 0, cont: false,
                     stopAfter: false, routing: false, mode: "prompt" };
var voiceSendSeq = 0;
function vbox() { return voice.box || composeEl; }
function vboxSized() {}
function autosizeCompose() {}
function nameOf(t) { return t; }
function voiceSetState(s) { voice.state = s; }
function voiceError() {}
function voiceNod() {}
function voiceCtlSent() {}
function voiceHeardNow() {}
function voiceIdle(c) { calls.push("idle"); voice.state = "idle"; }
function voiceCaption() {}
function voiceAckStart() {}
function askSentByVoice() {}
function askInDialog() {}
function speakLine() { return Promise.resolve(false); }
function voiceLastReply() { return Promise.resolve(null); }
function voiceIsListening() {
  return voice.state === "listening" || voice.state === "awaiting" || voice.state === "dialog";
}
function voiceKept() { return "kept"; }
var tick = function () { return new Promise(function (r) { setTimeout(r, 0); }); };
"""


@needs_node
def test_a_voice_send_keeps_the_words_typed_while_it_was_on_its_way():
    js = _js()
    script = _SEND_STUBS + _whole(js, "voiceSentTake") + _whole(js, "voiceSend") + """
var answer;
function fetch() { return new Promise(function (r) { answer = r; }); }
(async function () {
  composeEl.value = "fix the login bug";
  voiceSend(composeEl.value);
  await tick();
  // While /send is on its way the person types more into the box.
  composeEl.value = "fix the login bug and the docs too";
  answer({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
  await tick(); await tick();
  console.log(JSON.stringify({ box: composeEl.value, state: voice.state }));
})();
"""
    assert _run(script) == {"box": "and the docs too", "state": "working"}


def _takes_send_script(js: str, body: str) -> str:
    return (
        _SEND_STUBS
        + "function voiceSend(t) { calls.push('send:' + t); voice.state = 'sending'; }\n"
        + _whole(js, "voiceTakesSend")
        + _whole(js, "voiceListenStop")
        + _whole(js, "voiceListenKept")
        + _whole(js, "voiceListenOver")
        + _whole(js, "voiceListenFlushSoon")
        + _whole(js, "voiceListenFlush")
        + "var VOICE_OVER_WAIT_MS = 2500;\n"
        + body
    )


@needs_node
def test_send_tapped_mid_phrase_sends_the_accurate_words_not_the_guess():
    js = _js()
    script = _takes_send_script(
        js,
        """
// ServerSTT: finish() ends the recording and the phrase is on its way
// (pending) until its words are back.
var active = true, onItsWay = false;
voice.stt.active = function () { return active; };
voice.stt.pending = function () { return onItsWay; };
voice.stt.finish = function () { calls.push("stt.finish"); active = false; onItsWay = true; return true; };
(async function () {
  // The fast recognizer's live guess is in the box; the phrase's final is not.
  composeEl.value = "fix the log in bug";
  voice.guess = { box: composeEl, typed: "fix the log in bug" };
  var took = voiceTakesSend();
  var sentEarly = calls.filter(function (c) { return c.indexOf("send:") === 0; });
  // The phrase's final lands (onFinal: the guess swapped for whisper's words)…
  voice.guess = { box: null, typed: "" };
  composeEl.value = "fix the login bug";
  onItsWay = false;
  // …and the listen goes on to its end (voiceListenContinue / onEnd).
  if (voiceListen_.overAfter) voiceListenFlushSoon();
  console.log(JSON.stringify({ took: took, sentEarly: sentEarly,
    sent: calls.filter(function (c) { return c.indexOf("send:") === 0; }) }));
})();
""",
    )
    assert _run(script) == {
        "took": True,
        "sentEarly": [],
        "sent": ["send:fix the login bug"],
    }


@needs_node
def test_send_with_nothing_in_flight_sends_at_once_and_push_to_talk_still_only_keeps():
    js = _js()
    script = _takes_send_script(
        js,
        """
voice.stt.active = function () { return false; };
voice.stt.pending = function () { return false; };
var out = {};
// Listening, the box's words all final: Send sends them now.
composeEl.value = "fix it";
voiceTakesSend();
out.now = calls.slice();
// Push to talk's end afterwards keeps the words, never sends (no stale send).
calls.length = 0;
voice.state = "listening";
composeEl.value = "more words";
voiceListenOver();
out.ptt = calls.filter(function (c) { return c.indexOf("send:") === 0; });
console.log(JSON.stringify(out));
""",
    )
    got = _run(script)
    assert "send:fix it" in got["now"]
    assert got["ptt"] == []


# --------------------------------------------------------------------------- #
# The screen wake lock
# --------------------------------------------------------------------------- #
@needs_node
def test_voice_off_while_the_wake_lock_is_granted_releases_it():
    js = _js()
    script = (
        """(async function () {
var grants = [], locks = [];
var document = { hidden: false };
var navigator = { wakeLock: { request: function () {
  return new Promise(function (r) { grants.push(r); });
} } };
var voice = { state: "idle", wake: null };
function makeLock() {
  var l = { released: false, release: function () { l.released = true; return Promise.resolve(); },
            addEventListener: function () {} };
  locks.push(l);
  return l;
}
"""
        + _whole(js, "voiceWakeLock")
        + _whole(js, "voiceWakeRelease")
        + """
var tick = function () { return new Promise(function (r) { setTimeout(r, 0); }); };
  // Voice on (and the page coming back) ask for it twice before it is granted.
  voiceWakeLock();
  voiceWakeLock();
  // Voice goes off before the browser grants it.
  voiceWakeRelease();
  voice.state = "off";
  grants.forEach(function (g) { g(makeLock()); });
  await tick();
  console.log(JSON.stringify({ asked: grants.length, held: locks.filter(function (l) { return !l.released; }).length,
                               kept: !!voice.wake }));
})();
"""
    )
    assert _run(script) == {"asked": 1, "held": 0, "kept": False}
