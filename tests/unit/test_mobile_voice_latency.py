"""The phone's (/m) wait after the person stops talking (the STT latency
audit, 2026-11) — the desktop app's rules, so both surfaces feel the same:

* **"over" goes without the final when it would buy little**: the phrase's
  fast words all settled (no unstable tail) and the finals here seldom
  changed a word lately (``voiceFastStands``) — at once, or the moment the
  phrase's last chunk settles them. Its /end still comes back, to be
  measured (``g.late``), never typed.
* **The budget is a median of finals that came** (``voiceEndTypicalMs``):
  one slow /end no longer moves the next budget by half its excess, and a
  timeout (no ``final``) is never a sample.
* **A settled sentence ends sooner** (``voiceTurnHoldMs``'s ``settled``).
* **Warm before the press**: the capture worklet loads in the gesture that
  primes voice; recognition warms on a config read while voice is on and
  the server says it is not loaded (``voiceSttWarmWanted``).

Pure rules run as-is under node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "backend" / "web" / "static" / "mobile.js").read_text()

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _whole(name: str) -> str:
    start = JS.index("  function " + name + "(")
    end = JS.index("\n  }\n", start) + len("\n  }\n")
    return JS[start:end]


def _inner(name: str) -> str:
    start = JS.index("    function " + name + "(")
    end = JS.index("\n    }\n", start) + len("\n    }\n")
    return JS[start:end]


def _consts(*names: str) -> str:
    out = []
    for n in names:
        line = next(
            ln for ln in JS.splitlines() if ln.lstrip().startswith("var " + n + " = ")
        )
        out.append(line.strip())
    return "\n".join(out) + "\n"


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@needs_node
def test_the_typical_end_is_a_median_of_finals_that_came():
    script = (
        _consts("VOICE_END_SAMPLES")
        + _whole("voiceEndTypicalMs")
        + _whole("voiceKeepLast")
        + "var a = []; [300, 320, 1500, 310].forEach(function (x) { a = voiceKeepLast(a, x, VOICE_END_SAMPLES); });"
        + "var many = []; for (var i = 0; i < 20; i++) many = voiceKeepLast(many, i, VOICE_END_SAMPLES);"
        + "console.log(JSON.stringify([voiceEndTypicalMs([], 0), voiceEndTypicalMs([], 900),"
        + " voiceEndTypicalMs([300], 900), voiceEndTypicalMs([300, 700]), voiceEndTypicalMs(a), many]));"
    )
    out = _run(script)
    # Nothing measured: the seed (the server's advisory), else unknown.
    assert out[0] == 0 and out[1] == 900
    # Measured beats the seed; an even count takes the middle two.
    assert out[2] == 300 and out[3] == 500
    # One slow /end among quick ones moves it not at all (a 50/50 mean: +600 ms).
    assert out[4] == 315
    assert out[5] == list(range(13, 20))


@needs_node
def test_the_fast_words_stand_only_when_finals_seldom_change_them():
    script = (
        _consts(
            "VOICE_FAST_TRUST_MIN",
            "VOICE_FAST_TRUST_WINDOW",
            "VOICE_FAST_TRUST_MAX_CHANGED",
        )
        + _whole("voiceFastStands")
        + _whole("voicePlainWords")
        + "console.log(JSON.stringify([voiceFastStands(undefined), voiceFastStands([false, false]),"
        + " voiceFastStands([false, false, false]), voiceFastStands([false, false, false, true]),"
        + " voiceFastStands([false, false, true, true]),"
        + " voicePlainWords('Fix the  bug, over.') === voicePlainWords('fix the bug over'),"
        + " voicePlainWords('fix the bag') === voicePlainWords('fix the bug')]));"
    )
    assert _run(script) == [False, False, True, True, False, True, False]


@needs_node
def test_a_settled_sentence_ends_sooner_once_the_box_holds_words():
    script = (
        _consts(
            "VOICE_SILENCE_MS",
            "VOICE_TURN_MIN_MS",
            "VOICE_TURN_MAX_MS",
            "VOICE_SETTLED_HOLD_MS",
        )
        + _whole("voiceTurnHoldMs")
        + "var s = { hold_ms: 900, reason: 'sentence' };"
        + "console.log(JSON.stringify([voiceTurnHoldMs(s, true, false, true), voiceTurnHoldMs(s, true, false, false),"
        + " voiceTurnHoldMs(s, true, false), voiceTurnHoldMs(s, true, true, true), voiceTurnHoldMs(s, false, false, true),"
        + " voiceTurnHoldMs({ hold_ms: 1500, reason: 'comma' }, true, false, true)]));"
    )
    # Settled, words in the box: 600. Unsettled / unknown (an older call):
    # the verdict's. An empty box (routed alone): the verdict's. Not fresh:
    # never under the silence stop. A comma is not a sentence's end.
    assert _run(script) == [600, 900, 900, 900, 1100, 1500]


@needs_node
def test_recognition_warms_only_when_it_helps():
    script = (
        _consts("VOICE_STT_WARM_EVERY_MS")
        + _whole("voiceSttWarmWanted")
        + "var cold = { stt: 'server', server_ready: { stt_stream: true, stt_loaded: false } };"
        + "var now = 1e9;"
        + "console.log(JSON.stringify([voiceSttWarmWanted(cold, true, 0, now), voiceSttWarmWanted(cold, false, 0, now),"
        + " voiceSttWarmWanted({ stt: 'server', server_ready: { stt_stream: true } }, true, 0, now),"
        + " voiceSttWarmWanted({ stt: 'server', server_ready: { stt_stream: true, stt_loaded: true } }, true, 0, now),"
        + " voiceSttWarmWanted({ stt: 'phone', server_ready: { stt_stream: true, stt_loaded: false } }, true, 0, now),"
        + " voiceSttWarmWanted(cold, true, now - 1000, now)]));"
    )
    # Voice on, a cold streaming server recognizer: warm. Voice off, an older
    # server (no stt_loaded), loaded already, the phone's own recognizer, or
    # warmed a moment ago: never.
    assert _run(script) == [True, False, False, False, False, False]


def test_over_waits_only_for_a_final_that_can_still_matter():
    fire = _inner("controlFire")
    assert (
        "var budget = !g.partial && voiceFastStands(voice.rec.finals) ? 0 : voiceEndBudgetMs(voice.rec.endMs);"
        in fire
    )
    # Its /end is kept (measured, never typed) when it is on its way or can go now.
    assert "if (g.endSent) g.late = true;" in fire
    assert "else if (!g.busy && g.sid) { g.late = true; streamEnd(g, mine); }" in fire
    # The phrase's last chunk settling the words fires it at once.
    chunk = _inner("streamChunk")
    assert (
        "if (g.endFire && !g.partial && voiceFastStands(voice.rec.finals)) { g.endFire(); return; }"
        in chunk
    )
    end = _inner("streamEnd")
    assert "g.endSent = true;" in end
    assert "if (g.late) return;" in end
    # Whether the final changed a word: the trust the fast path reads.
    assert (
        "voice.rec.finals = voiceKeepLast(voice.rec.finals, x.j.same !== true && voicePlainWords(x.j.final) !== voicePlainWords(text), VOICE_FAST_TRUST_WINDOW);"
        in end
    )
    # A settled phrase's quiet: recHoldMs hands voiceTurnHoldMs whether it settled.
    assert "!!g.pend && !g.partial);" in _inner("recHoldMs")


def test_warm_ups_are_wired():
    prime = _whole("voicePrime")
    assert "if (voiceMayCapture() && stt && stt.preload) stt.preload();" in prime
    assert "function preload() { capLoad(); }" in JS
    assert "voiceSttWarm();" in _whole("voiceLoadCfg")
    warm = _inner("warmStt")
    assert (
        'fetch("/api/voice/stream", {' in warm and "streamDelete(String(j.id))" in warm
    )
    assert "warmStt: warmStt, preload: preload };" in JS
