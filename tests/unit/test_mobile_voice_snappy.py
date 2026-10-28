"""The phone's (/m) voice, made snappy: every user-visible wait on the
phone page is either gone or bounded by something measured.

* **"over" waits for /end by a budget** (``voiceEndBudgetMs``): the most,
  1.5 s, only while nothing is known of this server; once /end has been
  timed here (``voice.rec.endMs``, a running mean seeded by GET
  /api/voice's advisory ``final_ms``) 1.5× that plus 100 ms, held to
  0.3–1.5 s (the desktop app's rule); a late /end grows the next budget.
* **Nods are made ahead for a nearby voice** (``voiceTtsNearby``): not only
  ``local`` / ``system`` but any host whose detail names a loopback or
  private address — the owner's Voice Lab at 127.0.0.1:8770.
* **The chime never delays the first word**: under a chime's, a barge-in's
  or a confirmation's mute the recorder keeps the audio (a word that starts
  under it loses nothing) but no word starts in it; only a line said OVER
  the listen deafens it (``voiceMicDeaf``).
* **The ring is alive**: a fast attack (80 ms), a slower release (300 ms),
  a 150 ms CSS ease.
* **Barge-in opens the server recorder at once** (its stream opens under
  the speaker's tail); only the phone's own recognizer waits the 150 ms.
* **The Enter waits for no round trip**: the ack baseline is read alongside
  POST /send, not before it.
* **The strip says where it is**: *Sending…* for the moment a prompt is on
  its way; *Heard nothing.* for a moment when a window closed without a
  word (a cue, not a blank); the row never changes height when Cancel
  comes and goes; the buttons are thumb-sized.

Pure rules run as-is under node; the behaviour in a browser is walked by
``test_mobile_voice_browser.py``."""

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


def _css() -> str:
    return (STATIC / "mobile.css").read_text()


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


# --------------------------------------------------------------------------- #
# The "over" budget
# --------------------------------------------------------------------------- #
@needs_node
def test_the_over_budget_follows_how_long_end_takes():
    js = _js()
    assert "var VOICE_CONTROL_END_MS = 1500;" in js
    assert "var VOICE_CONTROL_END_MIN_MS = 300;" in js
    script = (
        _consts(js, "VOICE_CONTROL_END_MS", "VOICE_CONTROL_END_MIN_MS")
        + _whole(js, "voiceEndBudgetMs")
        + "console.log(JSON.stringify([0, undefined, null, 'x', 20, 200, 800, 1200, 1600, 5000].map(voiceEndBudgetMs)));"
    )
    out = _run(script)
    # Nothing known: the most. Known: 1.5× + 100 ms, held to 0.3–1.5 s (the desktop's rule).
    assert out == [1500, 1500, 1500, 1500, 300, 400, 1300, 1500, 1500, 1500]


def test_the_budget_is_measured_seeded_and_grows_when_end_is_late():
    js = _js()
    stt = js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]
    fire = _inner(stt, "controlFire")
    assert "var budget = voiceEndBudgetMs(voice.rec.endMs);" in fire
    assert "}, budget);" in fire and "VOICE_CONTROL_END_MS" not in fire
    # Late: the next budget grows toward 1.2× this one — it took at least this.
    assert (
        "voice.rec.endMs = Math.round(((voice.rec.endMs || budget) + budget * 1.2) / 2);"
        in fire
    )
    assert "voice.rec.ctl.budget = budget;" in fire
    # Measured: every /end's round trip into a running mean.
    end = _inner(stt, "streamEnd")
    assert "var rt = Date.now() - t0;" in end
    assert (
        "voice.rec.endMs = voice.rec.endMs ? Math.round((voice.rec.endMs + rt) / 2) : rt;"
        in end
    )
    assert end.index("voice.rec.endMs =") < end.index("voiceStreamFinal(text, x.j)")
    # Seeded by the server's advisory, when it sends one, until measured.
    cfg = _whole(js, "voiceLoadCfg")
    assert (
        "var adv = Number(voice.cfg.server_ready.final_ms || voice.cfg.server_ready.stt_final_ms || j.final_ms) || 0;"
        in cfg
    )
    assert "if (adv > 0 && !voice.rec.endMs) voice.rec.endMs = adv;" in cfg
    assert "passMs: 0, endMs: 0 }" in js
    # Diagnostics say what it waited for and what /end takes here.
    diag = _whole(js, "voiceDiag")
    assert (
        '" (waited for /end up to " + (voice.rec.ctl.budget / 1000).toFixed(1) + " s"'
        in diag
    )
    assert '"; /end takes ~" + (voice.rec.endMs / 1000).toFixed(1) + " s here"' in diag


# --------------------------------------------------------------------------- #
# Nods made ahead for a nearby voice
# --------------------------------------------------------------------------- #
@needs_node
def test_nearby_voices_get_their_confirmations_made_at_the_tap():
    js = _js()
    script = (
        js[
            js.index("  var VOICE_NEARBY_RE = ") : js.index(
                "  function voiceTtsNearby("
            )
        ]
        + _whole(js, "voiceTtsNearby")
        + """
var cases = [
  {tts_backend: "local"},
  {tts_backend: "system", tts_detail: "Windows voices"},
  {tts_backend: "openai", tts_detail: "tts-1 (Donna) at 127.0.0.1:8770"},
  {tts_backend: "openai", tts_detail: "tts-1 at http://localhost:8770/v1"},
  {tts_backend: "openai", tts_detail: "piper at 192.168.1.20:5000"},
  {tts_backend: "openai", tts_detail: "voice at mac-mini.local:8770"},
  {tts_backend: "openai", tts_detail: "voice at mac-mini.tail1234.ts.net:8770"},
  {tts_backend: "openai", tts_detail: "voice at 100.64.0.9:8770"},
  {tts_backend: "openai", tts_detail: "tts-1 (alloy) at api.openai.com"},
  {tts_backend: "openai", tts_detail: "tts-1 at 127.0.0.1.evil.example.com"},
  {tts_backend: "openai"},
  {},
];
console.log(JSON.stringify(cases.map(function (c) { return voiceTtsNearby(c); })));
"""
    )
    out = _run(script)
    assert out == [
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    pre = _whole(js, "voiceNodPrefetch")
    assert "if (!spoke && !voiceTtsNearby()) return;" in pre
    assert 'b !== "local"' not in pre
    # "Sent." is the first line made: the most said.
    assert 'var VOICE_NOD_LINES = ["Sent.", ' in js


# --------------------------------------------------------------------------- #
# The chime never delays the first word; barge-in opens the recorder at once
# --------------------------------------------------------------------------- #
def test_a_mute_keeps_the_audio_and_only_a_line_over_the_listen_is_deaf():
    js = _js()
    stt = js[js.index("  function ServerSTT(") : js.index("  // --- ServerTTS")]
    block = stt.split("g.proc.onaudioprocess = function (ev) {")[1].split(
        "\n      };\n"
    )[0]
    # Deaf (the block skipped, the floor still learnt) only under a line over the listen.
    assert "if (voiceMicDeaf()) {" in block
    assert "if (voiceMicMuted()) {" not in block
    # Under a chime / the wake chime / a barge-in / a nod's tail: the block
    # is kept, no word starts, the ring sits it out.
    assert (
        "var muted = (h.skipUntil && Date.now() < h.skipUntil) || voiceMicMuted();"
        in block
    )
    assert "if (h.skipUntil && Date.now() < h.skipUntil) return;" not in block
    assert block.index("var muted =") < block.index("g.blocks.push(block);")
    assert (
        "var loud = !muted && rms > Math.max(VOICE_RMS_SPEECH, g.floor * 3);" in block
    )
    assert "h.onLevel(muted ? 0 : voiceLevelOf(rms, g.floor))" in block
    deaf = _whole(js, "voiceMicDeaf")
    assert "return !!(p && p.over);" in deaf
    assert (
        "return voiceMicDeaf() || Date.now() < (voice.micMuteUntil || 0) + (grace || 0);"
        in _whole(js, "voiceMicMuted")
    )


def test_barge_in_starts_the_server_recorder_at_once():
    js = _js()
    listen = _whole(js, "voiceListen")
    assert "var mute = L.muteUntil - Date.now(), gen = L.gen;" in listen
    # The phone's own recognizer (which cannot be deafened) still waits the tail out…
    assert 'if (mute > 0 && voice.stt.name === "phone") {' in listen
    # …the server's recorder starts now, deaf to a word for that long.
    assert (
        "if (mute > 0) voice.micMuteUntil = Math.max(voice.micMuteUntil || 0, Date.now() + mute);"
        in listen
    )
    assert listen.count("voiceRecStart();") == 2
    assert "var VOICE_BARGE_MUTE_MS = 150;" in js and "var VOICE_CHIME_MS = 230;" in js


# --------------------------------------------------------------------------- #
# The ring: fast attack, slower release
# --------------------------------------------------------------------------- #
@needs_node
def test_the_ring_rises_fast_and_falls_slowly():
    js = _js()
    script = (
        _consts(js, "VOICE_RING_ATTACK_MS", "VOICE_RING_RELEASE_MS")
        + _whole(js, "voiceLevelStep")
        + """
var up = voiceLevelStep(0, 1, 80), down = voiceLevelStep(1, 0, 80), down300 = voiceLevelStep(1, 0, 300);
console.log(JSON.stringify([+up.toFixed(3), +down.toFixed(3), +down300.toFixed(3), voiceLevelStep(0.5, 0.5, 1000)]));
"""
    )
    up, down, down300, flat = _run(script)
    # 80 ms in: 63% of the way up, but only 23% of the way down.
    assert abs(up - 0.632) < 0.01 and abs(down - 0.766) < 0.01
    assert abs(down300 - 0.368) < 0.01 and flat == 0.5
    css = _css()
    ring = css.split("#voice-btn::after, #asking-mic::after {")[1].split("}")[0]
    assert "transition: opacity 150ms ease;" in ring
    assert (
        "transition: transform 150ms ease;"
        in css.split("#voice-level-bar {")[1].split("}")[0]
    )


# --------------------------------------------------------------------------- #
# Fewer waits on the way out: the send, the server voice
# --------------------------------------------------------------------------- #
def test_the_send_does_not_wait_for_the_baseline_read():
    js = _js()
    send = _whole(js, "voiceSend")
    assert "var baseline = voiceLastReply(title), seq = ++voiceSendSeq;" in send
    assert "voiceLastReply(title).then(function (base) {" not in send
    # The Enter is timed before the request goes; /send's answer is acted on
    # (the box emptied, "working") the moment it is in, and only the ack
    # poll waits for the baseline — for this send alone, while it is working.
    assert send.index("voiceCtlSent();") < send.index(
        'fetch("/api/instances/" + encodeURIComponent(title) + "/send"'
    )
    assert "baseline.then(" not in send.split('voiceSetState("working", "");')[0]
    assert (
        'if (talk) baseline.then(function (base) { if (base !== null && seq === voiceSendSeq && voice.state === "working") voiceAckStart(title, base); });'
        in send
    )
    assert "var voiceSendSeq = 0;" in js


def test_the_server_voice_is_asked_two_ahead_and_joins_short_sentences():
    js = _js()
    assert (
        "var VOICE_SPEAK_AHEAD = 2;" in js and "var VOICE_SPEAK_MIN_CHARS = 20;" in js
    )
    start = js.index("  function ServerTTS(phone) {")
    tts = js[start : js.index("\n  }\n", start)]
    speak = _inner(tts, "speak")
    assert (
        "ahead.length < VOICE_SPEAK_AHEAD" in speak and "got = ahead.shift();" in speak
    )
    # Cancel still aborts every request on its way.
    assert (
        "aborts.splice(0).forEach(function (a) { try { a.abort(); } catch (e) {} });"
        in _inner(tts, "cancel")
    )


# --------------------------------------------------------------------------- #
# The strip: a word for every moment, nothing jumps, thumbs fit
# --------------------------------------------------------------------------- #
def test_the_strip_cues_and_sizes():
    js = _js()
    word = _whole(js, "voiceStripWord")
    assert 'if (state === "sending") return "Sending…";' in word
    assert "if (note) return String(note);" in word
    note = _whole(js, "voiceStripNote")
    assert 'voice.quietWord = String(word || "");' in note
    expired = _whole(js, "voiceListenExpired")
    assert "var heard = voiceListen_.heard;" in expired
    assert expired.index("voiceIdle();") < expired.index(
        'if (!heard) voiceStripNote("Heard nothing.");'
    )
    css = _css()
    row = css.split("#voice-strip-row {")[1].split("}")[0]
    assert "min-height: 36px;" in row
    btns = css.split("#voice-hush, #voice-cancel, #voice-gear {")[1].split("}")[0]
    assert "min-height: 36px;" in btns
    assert "#voice-gear { font-size: 16px; padding: 3px 9px; min-width: 36px; }" in css
