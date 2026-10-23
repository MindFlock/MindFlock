"""Talking to a session from the phone (/m): structural contract checks.

The phone page can run a whole conversation with the selected session by
voice: tap the mic, dictate, the words go through ``POST /send``, the page
speaks what the agent is doing, and when the turn ends (or the agent asks)
it speaks the session's ``Say:`` line and listens for the answer. Permission
dialogs are read aloud and answered with yes / no / always / a number — never
free text. All of it is vanilla JS in ``mobile.js`` (one ``VOICE`` section),
served as-is, so these tests pin the wiring the way the rest of the mobile
tests do: ids, function names, the word tables, the request bodies, the
iOS rules. The pure matchers (commands, dialog answers, chunking) are also run
under node, the way ``test_mobile_parity`` runs ``sessionState``.

What the server side of this contract looks like is in the voice routes'
own tests; here every route is a string the page must hit with the right
shape (``dialog_safe: true`` on the send, ``by: "user"`` + ``dialog_id`` on
the answer, ``view=last_reply&max_chars=600`` for the ack).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

ROOT = Path(__file__).resolve().parents[2]


def _js() -> str:
    return client.get("/mobile.js").text


def _html() -> str:
    return client.get("/m").text


def _css() -> str:
    return client.get("/mobile.css").text


def _voice_section(js: str) -> str:
    start = js.index("  // VOICE — talking to the selected session from the phone")
    end = js.index("  // ---- Update banner: a newer release")
    return js[start:end]


def _fn(js: str, name: str) -> str:
    """The body of one top-level ``function name(`` in the IIFE."""
    body = js.split("  function %s(" % name)[1].split("\n  }\n")[0]
    return body.split("{", 1)[1]


# --------------------------------------------------------------------------- #
# Markup: the mic, the strip, the sheet, the Safari banner
# --------------------------------------------------------------------------- #
def test_mobile_html_has_the_voice_controls():
    html = _html()
    # The mic lives in the composer bar next to Send, painted by data-state.
    assert 'id="voice-btn"' in html
    composer = html.split('<div id="composer">')[1].split("</div>")[0]
    assert 'id="send"' in composer and 'id="voice-btn"' in composer
    assert 'data-state="off"' in composer
    # The strip above the composer: state word, caption, countdown, Cancel,
    # the gear, the one big "Tap to hear" button, an error line.
    for el in (
        'id="voice-strip"',
        'id="voice-state"',
        'id="voice-caption"',
        'id="voice-countdown"',
        'id="voice-countdown-bar"',
        'id="voice-cancel"',
        'id="voice-gear"',
        'id="voice-hear"',
        'id="voice-error"',
    ):
        assert el in html, el
    assert 'id="voice-strip" class="hidden"' in html  # off by default
    assert html.index('id="voice-strip"') < html.index('id="composer"')
    # The voice sheet: on/off, hands-free, rate, voice, the read-only approve
    # line, Diagnostics.
    for el in (
        'id="voice-sheet"',
        'id="voice-on-toggle"',
        'id="voice-handsfree"',
        'id="voice-rate"',
        'id="voice-voice"',
        'id="voice-approve-line"',
        'id="voice-diag"',
        'id="voice-done"',
    ):
        assert el in html, el
    assert "Approve dialogs by voice: on (Settings → Your phone)" in html
    assert ">Diagnostics<" in html
    assert "Hands-free" in html


def test_mobile_html_safari_banner():
    html = _html()
    assert 'id="voice-safari-banner"' in html
    assert "Voice needs Safari: open this page in a Safari tab" in html
    # The URL to carry over: a selectable field (the clipboard fallback) and
    # a Copy button.
    assert 'id="voice-safari-url"' in html
    assert 'id="voice-safari-copy"' in html
    assert 'id="voice-safari-banner" class="hidden"' in html


def test_mobile_css_voice_rules():
    css = _css()
    for sel in (
        "#voice-btn",
        '#voice-btn[data-state="listening"]',
        '#voice-btn[data-state="speaking"]',
        '#voice-btn[data-state="working"]',
        "#voice-strip",
        "#voice-countdown-bar",
        "#voice-hear",
        "#voice-safari-banner",
        "#voice-sheet .sheet-box",
        ".voice-diag",
    ):
        assert sel in css, sel
    # 16px floor on the URL field too: below that iOS Safari zooms on focus.
    url_rule = css.split("#voice-safari-url {")[1].split("}")[0]
    assert "font-size: 16px" in url_rule
    # The strip must never claim the composer's rule (keys stays bottommost).
    assert css.count("#composer {") == 1


# --------------------------------------------------------------------------- #
# The state machine and the adapters
# --------------------------------------------------------------------------- #
def test_mobile_js_voice_state_machine_and_adapters():
    js = _js()
    v = _voice_section(js)
    for fn in (
        "function PhoneSTT(",
        "function PhoneTTS(",
        "function sentenceChunks(",
        "function stripMarkdown(",
        "function firstSentence(",
        "function speakLine(",
        "function voiceListen(",
        "function voiceHeard(",
        "function voiceCommand(",
        "function voiceConfirm(",
        "function voiceSend(",
        "function voiceAckTick(",
        "function voiceReply(",
        "function voiceGotIdle(",
        "function voiceEventsOpen(",
        "function voiceOnPoll(",
        "function voiceDialog(",
        "function voiceDialogAnswer(",
        "function voiceParseAnswer(",
        "function voiceSwitch(",
        "function voiceMatchSession(",
        "function voiceNeedsMe(",
        "function voiceStatus(",
        "function voiceCondense(",
        "function voiceTurnOn(",
        "function voiceOff(",
        "function voiceTap(",
        "function voiceOnSelect(",
        "function voiceWakeLock(",
        "function voicePrime(",
    ):
        assert fn in v, fn
    # The nine states, named in the header so the next reader has them.
    header = v.split("var voiceBtn = ")[0]
    for state in (
        "off",
        "idle",
        "listening",
        "confirming",
        "sending",
        "working",
        "speaking",
        "awaiting",
        "dialog",
    ):
        assert re.search(r"^  //   %s\b" % state, header, re.M), state
    # One `voice` object with swappable adapters (a later ServerSTT /
    # ServerTTS replaces them when GET /api/voice says "server").
    assert "voice.stt = PhoneSTT();" in v
    assert "voice.tts = PhoneTTS();" in v
    assert "window.mfVoice = voice;" in v
    assert 'fetch("/api/voice")' in v
    # The PhoneSTT surface and its recognizer settings.
    stt = _fn(js, "PhoneSTT")
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in v
    assert "r.continuous = false;" in stt
    assert "r.interimResults = true;" in stt
    assert 'r.lang = navigator.language || "en-US";' in stt
    assert (
        'return { name: "phone", available: available, start: start, stop: stop' in stt
    )
    # Restarts belong to the state machine, not the adapter.
    assert "r.start()" in stt and stt.count("r.start()") == 1
    assert "voiceRecStart();" in _fn(js, "voiceRecStart")
    # The PhoneTTS surface: prime / speak → Promise / cancel; chunked; the
    # onend timeout fallback; utterances kept referenced until they end.
    tts = _fn(js, "PhoneTTS")
    assert (
        'return { name: "phone", available: available, prime: prime, speak: speak, cancel: cancel'
        in tts
    )
    assert "return new Promise(function (resolve) {" in tts
    assert "var parts = sentenceChunks(text);" in tts
    assert "words * VOICE_WORD_MS + VOICE_CHUNK_GRACE_MS" in tts
    assert "var VOICE_WORD_MS = 400;" in v and "var VOICE_CHUNK_GRACE_MS = 1500;" in v
    assert "keep.push(u);" in tts
    assert "synth.cancel();" in tts


def test_mobile_js_prime_runs_inside_the_tap_before_anything_async():
    """iOS drops every later speak() unless the first one ran synchronously
    inside the gesture. So prime() is the FIRST statement of each gesture
    handler, and prime() itself awaits nothing."""
    js = _js()
    v = _voice_section(js)
    tap = _fn(js, "voiceTap")
    assert tap.lstrip().startswith("voicePrime();")
    prime = _fn(js, "voicePrime")
    assert prime.lstrip().startswith("voice.tts.prime();")
    assert "then(" not in prime and "await" not in prime
    # The audio session follows inside the same gesture (silent switch).
    assert 'navigator.audioSession.type = "playback"' in prime
    tts_prime = _fn(js, "PhoneTTS").split("function prime() {")[1].split("\n    }\n")[0]
    assert 'new SpeechSynthesisUtterance("")' in tts_prime
    assert "synth.speak(u);" in tts_prime
    assert "then(" not in tts_prime
    # The deep-link / resume button and the sheet's toggle are gestures too.
    hear = v.split('voiceHearBtn.addEventListener("click", function () {')[1].split(
        "});"
    )[0]
    assert hear.lstrip().startswith("voicePrime();")
    toggle = v.split('voiceOnToggle.addEventListener("change", function () {')[1].split(
        "});"
    )[0]
    assert toggle.lstrip().startswith("voicePrime();")
    # click, never touchstart (iOS counts only the former as an activation).
    assert 'voiceBtn.addEventListener("click", voiceTap);' in v
    assert '"touchstart"' not in v
    # Then the wake lock, re-requested when the page comes back.
    assert 'navigator.wakeLock.request("screen")' in v
    turn_on = _fn(js, "voiceTurnOn")
    assert "voiceWakeLock();" in turn_on and "voiceEventsOpen();" in turn_on
    vis = v.split('document.addEventListener("visibilitychange"')[1].split("\n  });\n")[
        0
    ]
    assert "voiceWakeLock();" in vis and "voiceEventsOpen();" in vis
    assert "voiceWakeRelease();" in vis and "voice.stt.stop();" in vis


def test_mobile_js_voice_requests_have_the_contract_shape():
    js = _js()
    v = _voice_section(js)
    # The flag.
    assert '"/api/instances/" + encodeURIComponent(title) + "/voice"' in v
    assert "JSON.stringify({ on: !!on })" in v
    # The send: dialog_safe so a prompt never lands in a dialog.
    send = _fn(js, "voiceSend")
    assert '"/api/instances/" + encodeURIComponent(title) + "/send"' in send
    assert "JSON.stringify({ text: text, dialog_safe: true })" in send
    assert '"Sent to " + name + "."' in send
    assert '" is waking up, try again in a moment."' in send
    assert '"Couldn\'t send."' in send
    # The ack poll reads the newest reply, bounded.
    assert '"/output?view=last_reply&max_chars=600"' in v
    assert "var VOICE_ACK_POLL_MS = 3000;" in v
    assert "firstSentence(text)" in _fn(js, "voiceAckTick")
    # The answer: pinned to the dialog it was read from, by the person.
    ans = _fn(js, "voiceDialogAnswer")
    assert '"/api/instances/" + encodeURIComponent(title) + "/answer"' in ans
    assert (
        'JSON.stringify({ keys: [String(opt.key)], dialog_id: d.id, by: "user" })'
        in ans
    )
    assert "j.dialog_changed" in ans and "j.dialog_answered" in ans
    assert '"Already answered."' in ans
    # The dialog itself, quietly (204 when there is none).
    assert '"/dialog?quiet=1"' in v
    assert "r.status === 204 ? null" in v
    assert '" is waiting on you: open the screen."' in v
    # The on-demand summary and the say line.
    assert '"/voice/condense"' in v
    assert "/api/events" in v
    # Both event names, for the voice session only.
    on_event = v.split("voice.onEvent = function (f) {")[1].split("\n  };\n")[0]
    assert "f.session !== voice.session" in on_event
    assert (
        '"session.activity_changed"' in on_event and '"session.turn_ended"' in on_event
    )
    assert 'f.new === "idle"' in on_event and 'f.new === "clarify"' in on_event
    # Reconnect with backoff 1 s .. 30 s, resuming from the last seq.
    assert "Math.min(30000, wait * 2)" in v
    assert '"?since=" + voice.eventsSeq' in v
    assert "var VOICE_CONFIRM_MS = 2500;" in v


def test_mobile_js_dialog_word_tables():
    js = _js()
    v = _voice_section(js)
    yes = v.split("var VOICE_YES = ")[1].split("\n")[0]
    no = v.split("var VOICE_NO = ")[1].split("\n")[0]
    always = v.split("var VOICE_ALWAYS = ")[1].split("\n")[0]
    for w in (
        "yes",
        "yeah",
        "yep",
        "yup",
        "allow",
        "approve",
        "okay",
        "ok",
        "go ahead",
        "do it",
        "sure",
    ):
        assert w in yes, w
    for w in ("no", "nope", "deny", "reject", "don'?t", "cancel", "skip", "stop"):
        assert w in no, w
    # "always" is accepted only when SAID: the matcher tests for the literal
    # word, and nothing in the yes table can reach it.
    assert r"\balways\b" in always
    assert "always" not in yes
    parse = _fn(js, "voiceParseAnswer")
    assert "VOICE_ALWAYS.test(t)" in parse
    assert parse.index("VOICE_ALWAYS.test(t)") < parse.index("VOICE_YES.test(t)")
    # Numbers one..nine, first/second/third, "option N".
    num = v.split("var VOICE_NUM = ")[1].split("\n")[0]
    for w in ("one", "two", "three", "nine", "first", "second", "third", "1", "9"):
        assert re.search(r"\b%s\b" % w, num), w
    # The retry line, two retries, then idle; the no-approval line.
    assert '"I didn\'t catch that: say yes, no, or always."' in v
    assert "voice.dialogTries >= 2" in v
    assert '"Answer it on the screen."' in v
    assert "voice.cfg.approve_dialogs" in _fn(js, "voiceReadDialog")
    # The spoken choices.
    assert '"Say yes, no, or always."' in v and '"Say yes or no."' in v


def test_mobile_js_voice_commands_and_switching():
    js = _js()
    v = _voice_section(js)
    cmds = v.split("var VOICE_CMD = [")[1].split("];")[0]
    for kind in (
        "cancel",
        "switch",
        "needs",
        "status",
        "condense",
        "repeat",
        "off",
        "stop",
    ):
        assert 'kind: "%s"' % kind in cmds, kind
    assert "never ?mind" in cmds and "scratch that" in cmds
    assert "say that again" in cmds and "stop listening" in cmds and "voice off" in cmds
    assert "what did it say" in cmds
    # Matched on the whole transcript, case- and punctuation-insensitive.
    norm = _fn(js, "voiceNorm")
    assert ".toLowerCase()" in norm and "[^a-z0-9' ]+" in norm
    # Switching: display names first (nameOf), prefix → word overlap → loose.
    match = _fn(js, "voiceMatchSession")
    assert "nameOf(i.title)" in match
    assert "indexOf(q) === 0" in match
    assert "words.indexOf(w) >= 0" in match
    assert "q.indexOf(r.n) >= 0" in match
    sw = _fn(js, "voiceSwitch")
    assert "select(title);" in sw and '"Now on " + m.hit.name' in sw
    assert '"Which one: "' in sw
    # "What needs me": the rail's attention set, three at most.
    needs = _fn(js, "voiceNeedsMe")
    assert "sessionState(i).inBell" in needs and ".slice(0, 3)" in needs
    assert '" is asking: "' in needs and '"Nothing needs you."' in needs
    # Status reads last_turn and how long it has been working.
    status = _fn(js, "voiceStatus")
    assert "inst.last_turn" in status and "activity_since" in status


def test_mobile_js_reply_loop_rules():
    js = _js()
    v = _voice_section(js)
    reply = _fn(js, "voiceReply")
    # Never the same say twice; a question → listen; hands-free → listen.
    assert "voiceSaidAlready(say" in reply
    after = _fn(js, "voiceAfterReply")
    assert 'voiceListen("answer", VOICE_ANSWER_MS)' in after
    assert "voice.prefs.handsFree" in after
    assert "var VOICE_ANSWER_MS = 12000;" in v and "var VOICE_HANDSFREE_MS = 8000;" in v
    assert '"Still there? "' in _fn(js, "voiceListenExpired")
    # The poll fallback: the row's idle + last_say, the dialog on clarify.
    poll = _fn(js, "voiceOnPoll")
    assert 'inst.activity === "idle" && inst.last_say' in poll
    assert 'inst.activity === "clarify"' in poll
    assert "voiceOnPoll();" in _fn(js, "poll")
    # Voice follows the selected session.
    assert "voiceOnSelect(title);" in _fn(js, "select")
    # The Send button sends a dictated draft at once, through /send.
    assert "if (voiceTakesSend()) return;" in _fn(js, "sendCompose")
    # Speaking pauses recognition so the phone does not hear itself.
    speak = _fn(js, "speakLine")
    assert "voice.stt.stop();" in speak and "voice.tts.cancel();" in speak
    # Android: a short buzz when listening starts.
    assert "navigator.vibrate(30)" in _fn(js, "voiceListen")
    # Deep link ?voice=1 → the one big button; nothing speaks before a tap.
    assert 'new URLSearchParams(location.search).get("voice") === "1"' in v
    assert '"Tap to hear " + nameOf(title)' in v
    assert '"Tap to resume voice"' in v
    # Persistence: exactly these, wrapped.
    persist = _fn(js, "voicePersist")
    assert "localStorage.setItem(VOICE_STORE" in persist
    for key in ("session:", "on:", "rate:", "voiceName:", "handsFree:"):
        assert key in persist, key
    assert persist.lstrip().startswith("try {")


def test_mobile_js_voice_ios_rules():
    js = _js()
    v = _voice_section(js)
    # Recognition missing on iOS → the Safari banner, with the page's URL.
    assert "IOS && !voice.stt.available()" in v
    banner = _fn(js, "voiceShowSafariBanner")
    assert '"/m?s=" + encodeURIComponent(current) + "&voice=1"' in banner
    assert "navigator.clipboard.writeText" in v
    # Standalone detection is the page's existing STANDALONE (navigator.standalone
    # || display-mode: standalone), shown in Diagnostics.
    assert "navigator.standalone" in js
    assert "STANDALONE ?" in _fn(js, "voiceDiag")
    # Never an <audio> element: it kills iOS recognition when it finishes.
    code = re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", v)
    assert (
        "<audio" not in code
        and "new Audio(" not in code
        and 'createElement("audio")' not in code
    )
    # Diagnostics name what matters.
    diag = _fn(js, "voiceDiag")
    for word in (
        "Recognition:",
        "Speech:",
        "Mode:",
        "Wake lock:",
        "Events socket:",
        "Audio session:",
    ):
        assert word in diag, word


def test_mobile_js_voice_calls_no_native_dialog():
    v = _voice_section(_js())
    code = re.sub(r"/\*.*?\*/", "", v, flags=re.S)
    code = re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", code)
    assert not re.findall(r"(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(", code)


# --------------------------------------------------------------------------- #
# The pure matchers, run under node
# --------------------------------------------------------------------------- #
def _node():
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_voice_matchers_under_node():
    js = _js()
    text_fns = js[js.index("  function sentenceChunks(") : js.index("  var voice = {")]
    cmd_fns = js[
        js.index("  function voiceNorm(") : js.index("  function voiceRunCommand(")
    ]
    dialog_fns = js[
        js.index('  var VOICE_ORD = ["one"') : js.index("  function voiceDialog() {")
    ]
    cases = {
        "yes": ["yes", "Yeah, sure!", "ok go ahead", "Allow it."],
        "no": ["no", "No, don't.", "nope", "skip that"],
        "always": [
            "always",
            "Always allow.",
            "yes, always",
            "don't ask again",
            "yes and do not ask again",
        ],
        "none": ["banana", "", "maybe later"],
        "two": ["option two", "two", "the second one", "2"],
    }
    script = (
        'var voice = { session: "s" }; function nameOf(t) { return t; }\n'
        + text_fns
        + cmd_fns
        + dialog_fns
        + "\nvar OPTS = [{key:'1',label:'Yes',kind:'yes'},{key:'2',label:'Yes, always',kind:'always'},{key:'3',label:'No',kind:'no'}];\n"
        "var YN = [{key:'1',label:'Yes',kind:'yes'},{key:'2',label:'No',kind:'no'}];\n"
        "var OTHER = [{key:'1',label:'main',kind:'other'},{key:'2',label:'develop',kind:'other'},{key:'3',label:'release',kind:'other'}];\n"
        "var cases = " + json.dumps(cases) + ";\n"
        "var out = { answers: {}, yesOnly: {}, cmds: {}, chunks: null, strip: null, first: null, choices: {} };\n"
        "for (var k in cases) out.answers[k] = cases[k].map(function (t) { var o = voiceParseAnswer(t, OPTS); return o ? o.key : null; });\n"
        "out.yesOnly.always = voiceParseAnswer('always allow', YN).key;\n"
        "out.yesOnly.yeah = voiceParseAnswer('yeah sure', YN).key;\n"
        "out.other = ['option two', 'three', 'first', 'yes'].map(function (t) { var o = voiceParseAnswer(t, OTHER); return o ? o.key : null; });\n"
        "['Switch to the invoice parser.', 'go to API docs', \"What's it doing?\", 'Never mind.', 'scratch that', 'who needs me', 'summarize that', 'Say that again', 'voice off', 'Stop listening', 'stop', 'fix the login bug', 'please cancel', 'status please'].forEach(function (t) { var c = voiceCommand(t); out.cmds[t] = c ? c.kind + (c.arg ? ':' + c.arg : '') : null; });\n"
        "out.chunks = sentenceChunks('Fixed it. Should I push? Yes!');\n"
        'out.strip = stripMarkdown("I\'ll look at the **login handler** first. Then `auth.py` and [docs](http://x.y). ```js\\ncode\\n```");\n'
        'out.first = firstSentence("I\'ll look at the **login handler** first. Then more.");\n'
        "out.choices.full = voiceDialogChoices({ options: OPTS });\n"
        "out.choices.yn = voiceDialogChoices({ options: YN });\n"
        "out.choices.other = voiceDialogChoices({ options: OTHER });\n"
        "out.choices.two = voiceDialogChoices({ options: OTHER.slice(0, 2) });\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["answers"]["yes"] == ["1", "1", "1", "1"]
    assert out["answers"]["no"] == ["3", "3", "3", "3"]
    # "always" only when said — and said in any of its forms.
    assert out["answers"]["always"] == ["2", "2", "2", "2", "2"]
    assert out["answers"]["none"] == [None, None, None]
    assert out["answers"]["two"] == ["2", "2", "2", "2"]
    # With no always option, "always" is still a yes; "yeah sure" never widens.
    assert out["yesOnly"] == {"always": "1", "yeah": "1"}
    assert out["other"] == ["2", "3", "1", None]
    assert out["cmds"] == {
        "Switch to the invoice parser.": "switch:the invoice parser",
        "go to API docs": "switch:api docs",
        "What's it doing?": "status",
        "Never mind.": "cancel",
        "scratch that": "cancel",
        "who needs me": "needs",
        "summarize that": "condense",
        "Say that again": "repeat",
        "voice off": "off",
        "Stop listening": "stop",
        "stop": "stop",
        "fix the login bug": None,
        "please cancel": "cancel",
        "status please": "status",
    }
    assert out["chunks"] == ["Fixed it.", "Should I push?", "Yes!"]
    assert out["strip"] == "I'll look at the login handler first. Then and docs."
    assert out["first"] == "I'll look at the login handler first."
    assert out["choices"]["full"] == "Say yes, no, or always."
    assert out["choices"]["yn"] == "Say yes or no."
    assert (
        out["choices"]["other"]
        == "one: main. two: develop. three: release. Say one, two, or three."
    )
    assert out["choices"]["two"] == "one: main. two: develop. Say one or two."
