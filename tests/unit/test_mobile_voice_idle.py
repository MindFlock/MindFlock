"""The phone page (/m) leaves Voice Lab off when voice isn't in use (the
owner, 2026-11-04: "I want Voice Lab off when I'm not using it"). A local
voice host stops after 10 idle minutes, so the page may only reach it through
speech:

* ``voiceLoadCfg`` — read on page load, refocus, the voice sheet — asks
  ``GET /api/voice?warm=0`` (the config without readying the server's
  voice) unless something will be SAID soon (``voiceWantsVoice``): voice on
  with replies or confirmations, Everything, or the wake-word standby.
  Dictation alone never readies it: recognition runs in the server itself.
* The first line after the voice went idle (``voiceTtsMayBeCold``) waits
  for its first clip as long as the request may take (the host starting and
  loading its model) instead of handing the line to the phone's voice after
  a few seconds, and past ``VOICE_STARTING_MS`` the strip says
  *Starting voice…*."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "backend" / "web" / "static" / "mobile.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return JS.read_text()


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


def _var(js: str, name: str) -> str:
    start = js.index("  var " + name + " =")
    return js[start : js.index("\n", start) + 1]


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# --------------------------------------------------------------------------- #
# Reading the config never wakes the voice unless speech is coming
# --------------------------------------------------------------------------- #
@needs_node
def test_only_coming_speech_readies_the_servers_voice():
    script = _whole(_js(), "voiceWantsVoice") + """
var voice;
function wants(state, prefs) {
  voice = { state: state, prefs: Object.assign({ replies: "mine", wake: false, confirm: false }, prefs) };
  return voiceWantsVoice();
}
console.log(JSON.stringify({
  load_mine: wants("off", {}),
  load_off: wants("off", { replies: "off" }),
  dictating_replies_off: wants("listening", { replies: "off" }),
  on_mine: wants("idle", {}),
  on_off_confirm: wants("idle", { replies: "off", confirm: true }),
  everything: wants("off", { replies: "all" }),
  standby: wants("off", { replies: "off", wake: true }),
}));
"""
    out = _run(script)
    # A page load, a refocus, the sheet with voice off: never.
    assert out["load_mine"] is False
    assert out["load_off"] is False
    # Dictation with nothing read back: never (recognition is the server's own).
    assert out["dictating_replies_off"] is False
    # Something will be said: a reply, a confirmation, Everything, standby's switch.
    assert out["on_mine"] is True
    assert out["on_off_confirm"] is True
    assert out["everything"] is True
    assert out["standby"] is True


def test_voice_load_cfg_asks_warm_0_unless_speech_is_coming():
    load = _whole(_js(), "voiceLoadCfg")
    assert 'fetch(voiceWantsVoice() ? "/api/voice" : "/api/voice?warm=0")' in load
    # No other plain GET /api/voice on the page.
    assert _js().count('fetch("/api/voice")') == 0


# --------------------------------------------------------------------------- #
# A cold voice: waited for, and said so
# --------------------------------------------------------------------------- #
@needs_node
def test_cold_means_nothing_said_yet_or_the_last_line_ten_minutes_ago():
    js = _js()
    script = _var(js, "VOICE_COLD_MS") + _whole(js, "voiceTtsMayBeCold") + """
var m = 60 * 1000;
console.log(JSON.stringify([
  VOICE_COLD_MS,
  voiceTtsMayBeCold(0, 5),
  voiceTtsMayBeCold(undefined, 5),
  voiceTtsMayBeCold(1000, 1000 + 10 * m - 1),
  voiceTtsMayBeCold(1000, 1000 + 10 * m),
]));
"""
    assert _run(script) == [600000, True, True, False, True]


@needs_node
def test_the_strip_says_starting_voice_while_a_cold_voice_starts():
    script = _whole(_js(), "voiceStripWord") + """
console.log(JSON.stringify([
  voiceStripWord("idle", true, "", false, false, true),
  voiceStripWord("idle", true, "", false, false, false),
  voiceStripWord("idle", true, ""),
  voiceStripWord("idle", false, "", false, false, true),
  voiceStripWord("idle", true, "Quiet.", false, false, true),
]));
"""
    assert _run(script) == ["Starting voice…", "Speaking…", "Speaking…", "", "Quiet."]


def test_a_cold_voices_first_clip_is_waited_for_and_marked_starting():
    js = _js()
    said = _inner(js, "said")
    assert "var cold = voiceTtsMayBeCold(voice.rec.speakOkAt, t0);" in said
    # Only the line's first clip, and only on a voice that may be cold.
    assert "var starting = cold && i === 1;" in said
    # Every clip of a cold line may take as long as the server waits for a
    # starting host (COLD_START_TIMEOUT 90 s), past the usual 30 s cap.
    assert (
        "var cap = cold ? VOICE_SPEAK_COLD_MAX_MS : VOICE_SPEAK_FETCH_MAX_MS;" in said
    )
    assert "fetchClip(ctx, parts[asked++], keep, cap)" in said
    assert _var(js, "VOICE_SPEAK_COLD_MAX_MS").startswith(
        "  var VOICE_SPEAK_COLD_MAX_MS = 100000;"
    )
    # It may take as long as the request may (the host starting, its model
    # loading) before the phone's voice takes the line over.
    assert (
        "starting ? cap : voiceSpeakWaitMs(part.length, voice.rec.speakMsPerChar)"
        in said
    )
    assert "if (starting) startingSoon(g);" in said
    assert "if (starting) startingEnd();" in said
    # The voice is up once a clip comes back from it.
    assert "if (audio) voice.rec.speakOkAt = Date.now();" in _inner(js, "fetchClip")
    # The note shows after VOICE_STARTING_MS, for this line only, and a cancel ends it.
    soon = _inner(js, "startingSoon")
    assert "VOICE_STARTING_MS" in soon and "if (g !== gen) return;" in soon
    assert "voice.ttsStarting = true;" in soon
    tts = js[js.index("  function ServerTTS(") :]
    assert "startingEnd();" in _inner(tts, "cancel")
    assert "!!voice.ttsStarting" in _whole(js, "voiceStripPaint")
    assert _var(js, "VOICE_STARTING_MS").startswith("  var VOICE_STARTING_MS = 1000;")


# --------------------------------------------------------------------------- #
# Recognition loads only while someone listens
# --------------------------------------------------------------------------- #
@needs_node
def test_the_standby_holds_a_listening_lease_renewed_and_given_back():
    js = _js()
    script = (
        _var(js, "VOICE_LISTEN_RENEW_MS")
        + _var(js, "voiceLease")
        + _whole(js, "voiceListenLease")
        + """
var VOICE_CLIENT_ID = "m-page", posts = [], ticks = [], cleared = 0;
fetch = function (url, o) { posts.push([url, JSON.parse(o.body)]); return Promise.resolve({}); };
setInterval = function (fn, ms) { ticks.push([fn, ms]); return ticks.length; };
clearInterval = function () { cleared++; };
voiceListenLease(true);
voiceListenLease(true);   // idempotent: the standby syncs often
ticks[0][0]();            // the renewal
voiceListenLease(false);
voiceListenLease(false);
console.log(JSON.stringify({ posts: posts, every: ticks.map(function (t) { return t[1]; }), on: voiceLease.on }));
"""
    )
    out = _run(script)
    assert out["posts"] == [
        ["/api/voice/listen", {"on": True, "client": "m-page"}],
        ["/api/voice/listen", {"on": True, "client": "m-page"}],
        ["/api/voice/listen", {"on": False, "client": "m-page"}],
    ]
    assert out["every"] == [60000]  # well inside the server's 180 s lease
    assert out["on"] is False


def test_the_lease_follows_the_standby():
    sync = _whole(_js(), "voiceWakeSync")
    # Wanted (the pref on, voice on, the page in front), a mic here and the
    # server able to recognise: the lease is on; anything else gives it back.
    assert (
        "voiceListenLease(want && A.available() && (voice.cfg.server_ready || {}).stt !== false);"
        in sync
    )
    assert sync.index("voiceListenLease(") < sync.index("A.hold(want);")


@needs_node
def test_the_strip_says_starting_the_mic_while_recognition_loads():
    script = _whole(_js(), "voiceStripWord") + """
console.log(JSON.stringify([
  voiceStripWord("listening", false, "", false, false, false, true),
  voiceStripWord("listening", false, "", false, false, false, false),
  voiceStripWord("listening", false, "", true, false, false, true),
  voiceStripWord("idle", false, "", false, false, false, true),
  voiceStripWord("listening", true, "", false, false, false, true),
]));
"""
    assert _run(script) == [
        "Starting the mic…",
        "Listening…",
        "Mic paused…",
        "",
        "Speaking…",
    ]


def test_a_streams_loading_shows_until_recognition_answers():
    js = _js()
    stt = js[js.index("  function ServerSTT(") :]
    assert "voiceSttLoading(x.j.loading === true);" in _inner(stt, "streamOpen")
    assert "voiceSttLoading(false);   // a pass came back" in _inner(stt, "streamChunk")
    assert "voiceSttLoading(false);" in _inner(stt, "streamEnd")
    assert "voiceSttLoading(false);" in _inner(stt, "streamFail")
    assert "!!voice.sttLoading);" in _whole(js, "voiceStripPaint")
