"""The phone (/m) stops talking when you act: a hand on the ask sheet cuts
what is read about that ask (``voiceSpeechStopFor``: other sessions' lines
keep their place) and the step it moves to is not read for
``ASK_HAND_QUIET_MS`` (``askHandQuiet``); the mic while a line is said cuts it
before listening, ~150 ms later (``voiceBargeIn``); "stop talking" — the
router's kind ``hush`` or the local table — drops the queue and the mic
listens on (``voiceHush``). The review step's Submit answers / Cancel are the
step row's buttons, a multi-select's primary is labelled from ``button``, and
Everything sets the server's say-all marker. Pure rules run as-is under node;
the behaviour in a browser is walked by ``test_mobile_voice_browser.py``."""

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


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_queue_cancel_is_scoped_to_one_session():
    script = _whole(_js(), "voiceLineAbout") + """
var Q = [
  {text: "Parser asks: which DB?", title: "parser", bg: true},
  {text: "Docs built.", title: "docs", bg: true},
  {text: "Which features? Say one or two.", title: "parser", bg: false},
  {text: "Ticked Auth.", title: "", bg: false},
  {text: "Sent to docs.", title: "", bg: true},
];
function hits(title, session) { return Q.filter(function (x) { return voiceLineAbout(x, title, session); }).map(function (x) { return x.text; }); }
console.log(JSON.stringify({
  voiceOnIt: hits("parser", "parser"),
  voiceElsewhere: hits("parser", "docs"),
  voiceOff: hits("parser", ""),
  none: hits("", "parser"),
}));
"""
    out = _run(script)
    # Voice on that session: its lines and the conversation's (untitled, not beside it).
    assert out["voiceOnIt"] == [
        "Parser asks: which DB?",
        "Which features? Say one or two.",
        "Ticked Auth.",
    ]
    # Voice on another one: only the lines titled with it; the conversation stays.
    assert out["voiceElsewhere"] == [
        "Parser asks: which DB?",
        "Which features? Say one or two.",
    ]
    assert out["voiceOff"] == out["voiceElsewhere"]
    assert out["none"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_no_narration_within_three_seconds_of_a_hand():
    js = _js()
    assert "var ASK_HAND_QUIET_MS = 3000;" in js
    script = _whole(js, "askHandQuiet") + """
console.log(JSON.stringify([
  askHandQuiet(0, 5000, 3000),
  askHandQuiet(10000, 10500, 3000),
  askHandQuiet(10000, 12999, 3000),
  askHandQuiet(10000, 13000, 3000),
]));
"""
    assert _run(script) == [True, False, False, True]
    # Every way the sheet moves on reads only after it: another ask, voice's
    # next step, the poll's dialog — and a step reached by hand is never
    # read by a later poll.
    assert "if (!askMayNarrate()) return;" in _whole(js, "askMaybeSpeak")
    assert "!askMayNarrate()" in _whole(js, "askSelVoiceNext")
    assert "if (ask.open && !askMayNarrate()) return;" in _whole(js, "voiceDialog")
    assert (
        "if (!askMayNarrate() && voice.session === e.title) voice.dialogKey = String(d.id);"
        in _whole(js, "askSelRefetch")
    )


def test_a_hand_on_the_sheet_stops_its_speech():
    js = _js()
    hands = _whole(js, "askHandsOn")
    assert "ask.handAt = Date.now();" in hands
    assert "voiceSpeechStopFor(e.title);" in hands
    assert "ask.sel.voice = false;" in hands  # the next step is not voice's to read
    # The answer-listen for it ends (the dialog listen, the field's dictation).
    assert 'voice.state === "dialog"' in hands and "voice.box === askTextEl" in hands
    assert "voiceStopForCommand();" in hands and 'voiceIdle("");' in hands
    # Wired on every hand: options, the review's buttons, Back / Next, Send,
    # Enter, typing, Later, the stepper.
    for wired in (
        "askHandsOn(); askAnswer(opt);",
        "askHandsOn(); askSelTap(opt);",
        "askHandsOn(); askSelPrev();",
        "askHandsOn(); askSelNext();",
        "askHandsOn(); askSend();",
        'askTextEl.addEventListener("input", askHandsOn);',
        "askHandsOn(); askLater(false);",
        "askHandsOn(); askStep(-1);",
    ):
        assert wired in js, wired
    stop = _whole(js, "voiceSpeechStopFor")
    assert "voiceLineAbout(x, title, voice.session)" in stop
    assert "voice.tts.cancel();" in stop and "p.resolve(false);" in stop


def test_barge_in_cuts_the_speaker_before_the_mic():
    js = _js()
    assert "var VOICE_BARGE_MUTE_MS = 150;" in js
    barge = _whole(js, "voiceBargeIn")
    assert barge.index("voiceSpeechStopFor(") < barge.index("voiceListen_.muteUntil")
    assert "voice.tts.cancel();" in barge
    # Before the listen, in each gesture that listens.
    tap = _whole(js, "voiceTap")
    assert tap.index("voiceBargeIn();\n    voice.box = null;\n    voiceListen(") > 0
    hold = _whole(js, "voiceHoldStart")
    assert hold.index("voiceBargeIn();") < hold.index("voiceListen(")
    mic = _whole(js, "askMicTap")
    assert mic.index("voiceBargeIn(e.title);") < mic.index("askListenFor(e, false);")
    # The listen opens the recognizer only once the mute is over (same listen).
    listen = _whole(js, "voiceListen")
    assert "var mute = L.muteUntil - Date.now(), gen = L.gen;" in listen
    assert "voiceListen_.gen === gen && voiceIsListening()" in listen


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_stop_talking_is_hush_not_stop():
    js = _js()
    table = js[
        js.index("  function voiceNorm(") : js.index("  function voiceRunCommand(")
    ]
    phrases = [
        "stop talking",
        "Stop talking.",
        "be quiet",
        "quiet",
        "shut up",
        "stop reading",
        "enough",
        "that's enough",
        "okay stop",
        "OK, stop.",
        "hush please",
        "stop",
        "stop listening",
        "stop the server",
        "quiet the logs",
    ]
    script = (
        table
        + "console.log(JSON.stringify(%s.map(function (t) { var c = voiceCommand(t); return c ? c.kind : null; })));"
        % json.dumps(phrases)
    )
    kinds = dict(zip(phrases, _run(script)))
    for p in phrases[:11]:
        assert kinds[p] == "hush", p
    assert kinds["stop"] == "stop" and kinds["stop listening"] == "stop"
    assert kinds["stop the server"] is None and kinds["quiet the logs"] is None
    # The router's kind, and the local one: the mic goes on, nothing typed.
    kinds_list = js.split("var VOICE_ROUTE_KINDS = [")[1].split("];")[0]
    assert '"hush"' in kinds_list
    routed = _whole(js, "voiceRouted")
    assert 'case "hush": voiceHush(); return false;' in routed
    assert 'if (cmd && cmd.kind === "hush") { voiceHush(); return false; }' in routed
    # First in voiceHeard, any mode (before a dialog's "no" reads "stop").
    heard = _whole(js, "voiceHeard")
    assert heard.index("if (voiceIsHush(") < heard.index("var stop = voiceTail(")
    hush = _whole(js, "voiceHush")
    assert "voiceSpeechStop(true);" in hush and "voiceQuiet();" in hush
    assert 'voiceCaption("Quiet.");' in _whole(js, "voiceQuiet")
    assert "voiceListenStop" not in hush and "voiceStopForCommand" not in hush
    # The strip: a Stop talking button while a line is said; a tap on the strip too.
    html = (STATIC / "mobile.html").read_text()
    assert (
        '<button type="button" id="voice-hush" class="hidden">Stop talking</button>'
        in html
    )
    assert 'voiceHushBtn.addEventListener("click", voiceHush);' in js
    assert "else if (voiceSpeech.playing) voiceHush();" in js


def test_review_and_multi_select_buttons():
    js = _js()
    render = _whole(js, "askRenderSelect")
    # The review: no numbered rows; its options are the step row's buttons.
    assert "(review ? [] : d.options).forEach(" in render
    assert "askSelActs(d.options)" in render
    assert 'askSelNextBtn.textContent = String(d.button || "Next") + " ›";' in render
    acts = _whole(js, "askSelActs")
    assert 'o.kind === "yes"' in acts and '" asking-act-primary"' in acts
    assert "asking-opt-key" not in acts
    html = (STATIC / "mobile.html").read_text()
    sel = html[html.index('<div id="asking-sel"') :].split("</div>")[0]
    assert (
        sel.index('id="asking-sel-prev"')
        < sel.index('id="asking-sel-acts"')
        < sel.index('id="asking-sel-next"')
    )
    css = (STATIC / "mobile.css").read_text()
    assert (
        ".asking-sel .asking-act-primary" in css
        and "#asking-sel-next { margin-left: auto;" in css
    )


def test_say_all_follows_everything():
    js = _js()
    post = _whole(js, "voiceSayAllPost")
    assert '"/api/voice/say-all"' in post and "JSON.stringify({ on: !!on })" in post
    assert "r.status === 403" in post and "voice.sayAll.denied = true;" in post
    sync = _whole(js, "voiceSayAllSync")
    assert "if (voice.sayAll.synced) return;" in sync
    # On load only ever ON: another device may be the one on Everything.
    assert (
        'if (voice.prefs.replies === "all" && !serverOn) voiceSayAllPost(true);' in sync
    )
    assert "voiceSayAllPost(false)" not in sync
    # Only a GET that carries it (an older server is left alone).
    assert 'if (typeof j.say_all === "boolean") voiceSayAllSync(j.say_all);' in _whole(
        js, "voiceLoadCfg"
    )
    # Switching: only across "all".
    assert '(was === "all") !== (voice.prefs.replies === "all")' in js


def test_docs_describe_hush_and_barge_in():
    doc = (ROOT / "docs" / "web-ui.md").read_text()
    phone = doc[doc.index("## Mobile UI (`/m`)") :]
    for words in ("barge", "Stop talking", "stop talking", "3 s", "say-all"):
        assert words in phone, words
