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
    # The strip above the composer: its one word (the caption), Cancel, the
    # gear, the one big "Tap to hear" button, an error line. No second
    # "state word" beside the caption: the strip says one word.
    assert 'id="voice-state"' not in html
    for el in (
        'id="voice-strip"',
        'id="voice-caption"',
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
        "function voiceDictParse(",
        "function voiceListenContinue(",
        "function voiceListenStop(",
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
    # The eight states, named in the header so the next reader has them.
    header = v.split("var voiceBtn = ")[0]
    for state in (
        "off",
        "idle",
        "listening",
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
    assert "r.continuous = true;" in stt
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


def test_mobile_js_a_network_error_is_said_not_retried_in_silence():
    """A Chromium without Google's speech key (Arc, Brave, Vivaldi, Opera)
    fails every recognizer start with ``network`` and hears nothing. The
    first is retried like a blip; the second in a listening window stops the
    loop with the reason on the strip (and in Diagnostics as the last
    error), never eight silent restarts back to "Tap to talk"."""
    js = _js()
    rs = _fn(js, "voiceRecStart")
    assert 'code === "network"' in rs
    assert "++voiceListen_.netErrs >= 2" in rs
    assert (
        "Speech recognition failed (network): this browser cannot reach its "
        "speech service. Use Chrome, Edge or Safari, or turn on server "
        "recognition (Settings → Voice)." in rs
    )
    assert "L.netErrs = 0;" in _fn(js, "voiceListen")


def test_mobile_js_radio_rules_over_and_disregard():
    """Dictation is radio-style: every phrase lands in the compose box and
    listening goes on across pauses; a trailing "over" sends the box (with
    the phrase's words), a trailing "disregard" empties it and listens on,
    "submit" is a word (retired as a radio word); Cancel drops the
    words, VOICE_DICT_IDLE_MS of silence stops with them kept, never sent,
    and there is no countdown anywhere. Commands are commands only while the
    box is empty; a dialog answer needs no "over"."""
    js = _js()
    v = _voice_section(js)
    heard = _fn(js, "voiceHeard")
    assert "var p = voiceDictParse(text);" in heard
    assert 'p.kind === "disregard"' in heard and 'vbox().value = "";' in heard
    words = _fn(js, "voiceHeardWords")
    # "over" sends (an empty box: says so and listens on) — listening on
    # while the listen is continuous; "over and out" sends and stops (an
    # empty box: nothing sent, just stops). No radio word only keeps.
    assert 'var out = p.kind === "out";' in words
    assert 'voiceCaption("Nothing to send yet");' in words
    assert (
        'if (out) { voiceStopForCommand(); voiceListenKept(); voiceNod("Over and out."); return; }'
        in words
    )
    assert (
        "if (voiceListen_.cont) { voiceSendOn(vbox().value); voiceListenContinue(); return; }"
        in words
    )
    assert (
        'voiceSend(vbox().value, undefined, out ? "Sent. Over and out." : "Sent.");'
        in words
    )
    assert words.count("voiceSend(") == 1 and '"submit"' not in words
    # The continuous send never stops the mic: the box empties at once, a
    # failure puts the words back in front of what was said since.
    on = _fn(js, "voiceSendOn")
    assert "voice.stt.stop()" not in on and "voiceSetState(" not in on
    assert (
        'box.value = "";' in on
        and 'box.value = since ? text + " " + since : text;' in on
    )
    assert "body: JSON.stringify({ text: text, dialog_safe: true, say: say })," in on
    # A continuous listen has no window: its end moves out whenever read.
    until = _fn(js, "voiceListenUntil")
    assert (
        "if (L.cont) L.until = Math.max(L.until, Date.now() + VOICE_DICT_IDLE_MS);"
        in until
    )
    listen = _fn(js, "voiceListen")
    assert 'L.cont = !!cont && !held && mode === "prompt";' in listen
    assert "var left = voiceListenUntil() - Date.now();" in listen
    assert "until: voiceListenUntil()," in _fn(js, "voiceRecStart")
    assert "if (Date.now() < voiceListenUntil()) {" in _fn(js, "voiceRecStart")
    # What a continuous listen is: the owner's own start — a tap, the
    # first tap that turns voice on, the wake word, the ask sheet's mic.
    assert 'voiceListen("prompt", VOICE_TAP_MS, false, true);' in _fn(js, "voiceTap")
    assert 'voiceListen("prompt", VOICE_TAP_MS, false, true);' in _fn(js, "voiceWoke")
    assert 'if (listenAfter) voiceListen("prompt", VOICE_TAP_MS, false, true);' in js
    assert 'voiceListen("prompt", VOICE_ANSWER_MS, false, true);' in _fn(
        js, "askListenFor"
    )
    # A hold never is (its "over" sends and ends it); every stop clears it.
    assert 'voiceListen("prompt", VOICE_DICT_IDLE_MS, true);' in _fn(
        js, "voiceHoldStart"
    )
    for fn in (
        "voiceListenStop",
        "voiceListenKept",
        "voiceListenOver",
        "voiceStopForCommand",
    ):
        assert "cont = false;" in _fn(js, fn), fn
    kept = _fn(js, "voiceListenKept")
    assert "voiceIdle(vbox().value.trim() ? voiceKept() : undefined);" in kept
    assert "voiceSend" not in kept
    assert 'return "In the box — say " + voiceSaid("send") + ", or tap Send";' in _fn(
        js, "voiceKept"
    )
    assert (
        "if (p.text && !composeEl.value.trim()) {" in heard
    )  # commands: empty box only
    assert "voiceDialogHeard(p.text);" in heard
    assert "voiceDialogAnswer(text);" in _fn(js, "voiceDialogHeard")
    assert "voiceListenContinue();" in heard and "voiceListenContinue();" in words
    # The phrase keeps the recognizer: no stop on a final, a restart only
    # when the adapter ended with it.
    cont = _fn(js, "voiceListenContinue")
    assert "if (!voice.stt.active()) voiceRecStart();" in cont
    assert (
        "voice.stt.stop();"
        not in _fn(js, "PhoneSTT").split("r.onend")[0].split("r.onresult")[1]
    )
    # Words move the window out; silence with words kept, not sent.
    assert "L.until = Math.max(L.until, Date.now() + VOICE_DICT_IDLE_MS);" in _fn(
        js, "voiceHeardSome"
    )
    expired = _fn(js, "voiceListenExpired")
    assert (
        'if (mode !== "dialog" && vbox().value.trim()) { voiceListenStop(); return; }'
        in expired
    )
    assert '"Kept, not sent — Send sends it, or tap the mic to go on"' in _fn(
        js, "voiceListenStop"
    )
    # The hints name the radio words the owner has (and, holding, the
    # release): built from the words, never a fixed string.
    assert (
        'return "Say " + voiceSaid("send") + " to send, " + voiceSaid("end") + " to stop, " + voiceSaid("clear") + " to start over";'
        in _fn(js, "voiceHintTap")
    )
    assert 'return "Release to keep it; say " + voiceSaid("send") + " to send";' in _fn(
        js, "voiceHintHold"
    )
    assert "“over” to keep" not in v and "Release or say" not in v
    assert "VOICE_HINT_TAP" not in v and "VOICE_KEPT" not in v
    assert "return voiceListen_.held ? voiceHintHold() : voiceHintTap();" in _fn(
        js, "voiceDictHint"
    )
    assert "VOICE_DICT_HINT" not in v
    # Cancel while dictating drops the words.
    assert (
        'if (voiceIsListening()) { vbox().value = ""; vboxSized(); voice.dictated = false; }'
        in _fn(js, "voiceCancelTap")
    )
    # No countdown left anywhere.
    for gone in (
        "confirming",
        "voiceConfirm",
        "voiceCountdown",
        "VOICE_CONFIRM_MS",
        "voiceHoldText",
    ):
        assert gone not in v, gone
    assert "voice-countdown" not in _html() and "voice-countdown" not in _css()


def test_mobile_js_push_to_talk_toggle_and_hold():
    """Push to talk (prefs.ptt, "toggle" by default): the gesture end means
    "over", never a send. In toggle mode the second tap on the mic or the
    strip stops listening; in hold mode pointerdown on the mic starts
    listening and pointerup / pointercancel stops it — after waiting for the
    words still on their way (VOICE_OVER_WAIT_MS for the phone's last
    phrase, as long as a server clip is pending, a route answer) — with the
    words left in the box. Only "over" and the Send button send. The click
    a release fires is swallowed; the click still turns voice on and starts
    a tap-style listen for a keyboard. The silence stop keeps the words,
    unsent."""
    js = _js()
    v = _voice_section(js)
    html = _html()
    assert "var VOICE_OVER_WAIT_MS = 1500;" in v
    for gone in ("voiceListenSend", "sendAfter", "sendBy", "VOICE_SEND_WAIT_MS"):
        assert gone not in v, gone
    # The sheet's choice, and the default.
    assert (
        'prefs: { rate: 1, voiceName: "", handsFree: false, ptt: "toggle", replies: "mine", wake: false, confirm: true, sounds: true }'
        in v
    )
    sel = html.split('<select id="voice-ptt"')[1].split("</select>")[0]
    assert '<option value="toggle">Tap to talk</option>' in sel
    assert '<option value="hold">Hold to talk</option>' in sel
    assert ">Push to talk<" in html
    # Toggle: the second tap (mic or strip) is "over"; a dialog tap only stops.
    tap = _fn(js, "voiceTap")
    assert "if (voiceIsListening()) { voiceListenOver(); return; }" in tap
    assert "voiceListenDone();" not in tap
    strip = v.split('voiceStrip.addEventListener("click", function (ev) {')[1].split(
        "\n  });\n"
    )[0]
    assert "if (voiceIsListening()) voiceListenOver();" in strip
    over = _fn(js, "voiceListenOver")
    assert 'if (L.mode === "dialog") { voiceListenDone(); return; }' in over
    assert "L.overAfter = true;" in over and "L.until = 0;" in over
    assert "if (s.finish) s.finish(); else if (s.end) s.end();" in over
    soon = _fn(js, "voiceListenFlushSoon")
    assert (
        "L.routing || (s.pending && s.pending()) || (s.active() && Date.now() < L.overBy)"
        in soon
    )
    # Once the words are in: stop and keep them — no path to a send.
    flush = _fn(js, "voiceListenFlush")
    assert "voiceListenKept();" in flush
    for fn in ("voiceListenOver", "voiceListenFlushSoon", "voiceListenFlush"):
        assert "voiceSend" not in _fn(js, fn), fn
    assert '(voiceListen_.held ? "Release to keep it" : "Tap to keep it")' in _fn(
        js, "voiceSetState"
    )
    # The phrase that ends after the press: PhoneSTT's graceful end().
    stt = _fn(js, "PhoneSTT")
    assert "function end() {" in stt and "r.stop();" in stt.split("function end() {")[1]
    # The recognizer ending / a phrase landing while an "over" is due flushes.
    assert "if (L.overAfter) { voiceListenFlushSoon(); return; }" in _fn(
        js, "voiceRecStart"
    )
    assert "if (voiceListen_.overAfter) { voiceListenFlushSoon(); return; }" in _fn(
        js, "voiceListenContinue"
    )
    # Hold: the pointer events on the mic, the click kept for the rest.
    assert 'voiceBtn.addEventListener("click", voiceTap);' in v
    assert 'voiceBtn.addEventListener("pointerdown", voiceHoldStart);' in v
    assert 'voiceBtn.addEventListener("pointerup", voiceHoldEnd);' in v
    assert 'voiceBtn.addEventListener("pointercancel", voiceHoldEnd);' in v
    hold = _fn(js, "voiceHoldStart")
    assert 'if (voice.prefs.ptt !== "hold" || (ev && ev.button)) return;' in hold
    assert 'if (s === "off" || s === "sending" || voiceIsListening()' in hold
    assert 'voiceListen("prompt", VOICE_DICT_IDLE_MS, true);' in hold
    assert "setPointerCapture(ev.pointerId)" in hold
    end = _fn(js, "voiceHoldEnd")
    assert "if (voiceIsListening() && voiceListen_.held) voiceListenOver();" in end
    assert (
        'ev.type !== "pointercancel"' in end
        and "voiceHold.swallowUntil = Date.now() + 1000;" in end
    )
    assert (
        "if (voiceHold.swallowUntil > Date.now()) { voiceHold.swallowUntil = 0; return; }"
        in tap
    )
    # Hold mode: the click that turns voice on does not start listening.
    assert 'voiceTurnOn(current, voice.prefs.ptt !== "hold")' in tap
    assert "L.held = !!held;" in _fn(js, "voiceListen")
    # A long press is a press on the phone, not a callout or a scroll.
    btn = _css().split("#voice-btn, #asking-mic {")[1].split("}")[0]
    assert "touch-action: none;" in btn and "-webkit-touch-callout: none;" in btn
    # The idle caption names the gesture.
    assert (
        'return voice.prefs.ptt === "hold" ? "Hold the mic to talk" : "Tap to talk";'
        in _fn(js, "voiceTalkHint")
    )
    # The silence stop is still the kept-not-sent one.
    assert "voiceListenOver" not in _fn(js, "voiceListenExpired")


def test_mobile_js_spoken_replies_say_per_prompt():
    """Spoken replies (prefs.replies: off | mine, the default | all): POST
    /send asks for the Say line for a dictated prompt with "mine" — a typed
    prompt costs no Say tokens — and for every prompt with "all"
    (Everything); a reply to a say:false prompt is never read out, only
    "Your turn". "Sent to X." and the ack are spoken unless it is off."""
    js = _js()
    html = _html()
    assert 'id="voice-replies"' in html and "Spoken replies" in html
    sel = html.split('<select id="voice-replies"')[1].split("</select>")[0]
    assert re.findall(r'<option value="(\w+)"', sel) == ["off", "mine", "all"]
    assert "Replies to what I said by voice" in sel and "Everything" in sel
    send = _fn(js, "voiceSend")
    assert "var dictated = !!voice.dictated;" in send
    assert (
        'var say = voice.prefs.replies === "all" || (voice.prefs.replies === "mine" && dictated);'
        in send
    )
    assert "voice.expectSay = say;" in send and "voice.dictated = false;" in send
    assert 'var talk = voice.prefs.replies !== "off";' in send
    assert "if (base !== null && talk) voiceAckStart(title, base);" in send
    assert "if (talk) speakLine(sent, true); else voiceCaption(sent);" in send
    # Everything with voice off: a typed prompt goes through /send too, say:true.
    every = _fn(js, "everythingTakesSend")
    assert "body: JSON.stringify({ text: text, dialog_safe: true, say: true })" in every
    assert "if (everythingTakesSend()) return;" in _fn(js, "sendCompose")
    # Dictated words set the flag; emptying the box clears it.
    assert "voiceAppend(p.text);" in _fn(js, "voiceHeardWords")
    assert "voice.dictated = true;" in _fn(js, "voiceAppend")
    assert "voice.dictated = false;" in _fn(js, "voiceHeard")  # disregard
    assert "voice.dictated = false;" in _fn(js, "voiceCancelTap")
    assert "if (!composeEl.value.trim()) voice.dictated = false;" in _voice_section(js)
    # The reply to a prompt that asked for no Say line: quiet, "Your turn".
    reply = _fn(js, "voiceReply")
    quiet = reply.split("if (voice.expectSay === false) {")[1].split("\n    }\n")[0]
    assert 'if (voice.state === "working") voiceIdle("Your turn");' in quiet
    assert "speakLine" not in quiet and "voiceAfterReply" not in quiet
    assert "voice.lastSay = {" in quiet  # noted, so the poll does not bring it back
    assert reply.index("if (voice.expectSay === false) {") < reply.index("if (!say) {")
    # A new session (or a move) has no prompt of ours outstanding.
    assert "voice.expectSay = null;" in _fn(js, "voiceTurnOn")
    assert "voice.expectSay = null;" in _fn(js, "voiceMoveTo")
    diag = _fn(js, "voiceDiag")
    assert (
        '"Replies: " + ({ off: "off", mine: "to what you said by voice", all: "everything" })[voice.prefs.replies]'
        in diag
    )
    assert '"Push to talk: " + (voice.prefs.ptt === "hold" ? "hold" : "tap")' in diag
    assert (
        '"Last prompt: "' in diag
        and '"dictated" : "typed"' in diag
        and '", say "' in diag
    )


def test_mobile_js_reply_lines_name_another_session_only():
    """A spoken line carries the session it is about (speakLine's title);
    the queue names it first only when the phone is on another session
    (voiceLineText, run under node in test_mobile_speech_queue): the Say
    line, the ack and the dialog read-out pass their session and no name of
    their own."""
    js = _js()
    reply = _fn(js, "voiceReply")
    assert "speakLine(say, false, voice.session)" in reply
    assert 'speakLine(name + " finished.")' in reply
    assert "speakLine(line, true, title)" in _fn(js, "voiceAckTick")
    assert "var parts = [stripMarkdown(d.question" in _fn(js, "voiceDialogSpeech")
    assert "speakLine(voiceDialogSpeech(d), false, voice.session)" in _fn(
        js, "voiceReadDialog"
    )
    assert "var said = voiceLineText(line, current, nameOf);" in _fn(
        js, "voiceSpeechPump"
    )
    assert "voiceReply({" in _fn(js, "voiceOnPoll")


def test_mobile_js_router_first_then_the_local_table():
    """The first phrase said into an empty box goes to POST /api/voice/route
    {text, session} BEFORE the local VOICE_CMD table: switch / new select the
    session it names (a new one may need a fresh list first) and speak its
    say; the say kinds speak; the bare kinds run the local handlers; prompt
    puts the words in the box. A non-200, the network, VOICE_ROUTE_TIMEOUT_MS
    or an answer with no known kind falls back to the local table."""
    js = _js()
    v = _voice_section(js)
    assert "var VOICE_ROUTE_TIMEOUT_MS = 2000;" in v
    route = _fn(js, "voiceRoute")
    assert 'fetch("/api/voice/route", {' in route
    assert (
        "JSON.stringify({ text: text, session: session || voice.session || current })"
        in route
    )
    assert "VOICE_ROUTE_TIMEOUT_MS" in route and "Promise.race([asked, late])" in route
    assert "if (!r.ok) {" in route and "return null;" in route
    assert "VOICE_ROUTE_KINDS.indexOf(j.kind) < 0" in route
    kinds = v.split("var VOICE_ROUTE_KINDS = [")[1].split("];")[0]
    for kind in (
        "prompt",
        "switch",
        "new",
        "ambiguous",
        "no-session",
        "new-failed",
        "list",
        "needs",
        "status",
        "cancel",
        "stop",
        "off",
        "repeat",
        "condense",
    ):
        assert '"%s"' % kind in kinds, kind
    # voiceHeard asks the router; the local table is the fallback in voiceRouted.
    heard = _fn(js, "voiceHeard")
    assert "voiceRoute(p.text).then(" in heard
    assert "voiceCommand(" not in heard
    assert "if (L.gen !== gen) return;" in heard
    assert "if (L.routing) { L.routeQ.push([text, mode]); return; }" in heard
    routed = _fn(js, "voiceRouted")
    fallback = routed.split("if (!res) {")[1].split("\n    }\n")[0]
    assert (
        "var cmd = voiceCommand(p.text);" in fallback
        and "if (!cmd) return voiceHeardRest(p);" in fallback
    )
    # A prompt is still checked for an answer at its end (voiceHeardRest).
    assert 'case "prompt": return voiceHeardRest(p);' in routed
    assert "voiceHeardWords(p);" in _fn(js, "voiceHeardRest")
    # A command ends the listen: nothing queued behind it is dictated.
    assert (
        "if (voiceRouted(res, p)) { L.draining = false; L.routeQ = []; return; }"
        in heard
    )
    assert 'voiceRouteSelect(String(res.title || ""), res.kind === "new")' in routed
    assert "speakLine(say ||" in routed
    assert 'voiceRunCommand({ kind: res.kind, arg: "" });' in routed
    assert "voiceNeedsMe();" in routed and "voiceStatus();" in routed
    sel = _fn(js, "voiceRouteSelect")
    assert "select(title);" in sel and "return poll().then(" in sel
    assert "pendingNew = title;" in sel
    assert 'return fetch("/api/instances")' in _fn(js, "poll")
    # The silence clock waits for a route answer like a pending clip.
    assert "voiceListen_.routing" in _fn(js, "voiceListen")


def test_mobile_js_wider_vocabulary_numbers_stops_and_answers():
    """The phone's wider command set. The Sessions sheet numbers its rows in
    the order shown and holds that order while open; "session two" is the
    second row, "next session" a step from this one. A stop phrase acts at
    the end of ANY phrase or streamed run (the box kept); while the session
    is asking, an answer at the end answers it, the stream holding back up
    to three words that may start one. An answer with dialog_id + keys is
    POSTed only with Approve dialogs by voice on; one for another session
    keeps voice where it is; a question's answer is a prompt."""
    js = _js()
    v = _voice_section(js)
    # Numbered rows, held while the sheet is open; rowOrder is the order.
    rows = _fn(js, "renderRows")
    assert (
        'var open = !!sessionsSheet && !sessionsSheet.classList.contains("hidden");'
        in rows
    )
    assert "rowOrder = kept.concat(" in rows and "rowOrder = sorted;" in rows
    assert "var row = sessionRow(instByTitle(title));" in rows
    assert 'num.className = "sess-num";' in rows
    assert "num.textContent = String(i + 1);" in rows
    assert "main.insertBefore(num, main.firstChild);" in rows
    assert ".sess-num {" in _css()
    assert 'id="sessions-voice-note"' in _html()
    # switch_index / switch_rel: the rows as shown.
    assert 'return n >= 1 && n <= rowOrder.length ? rowOrder[n - 1] : "";' in _fn(
        js, "voiceRowTitle"
    )
    idx = _fn(js, "voiceSwitchIndex")
    assert "var title = voiceRowTitle(n);" in idx
    assert '"There is no session " + n + "."' in idx
    rel = _fn(js, "voiceSwitchRel")
    assert "rowOrder.indexOf(voice.session || current)" in rel
    assert "(((i + delta) % n) + n) % n" in rel
    routed = _fn(js, "voiceRouted")
    assert "voiceSwitchIndex(Number(res.index) || 0);" in routed
    assert "voiceSwitchRel(Number(res.delta) || 1);" in routed
    assert "voiceRoutedAnswer(res, p, false);" in routed
    kinds = v.split("var VOICE_ROUTE_KINDS = [")[1].split("];")[0]
    for kind in ("switch_index", "switch_rel", "answer", "question"):
        assert '"%s"' % kind in kinds, kind
    # An unknown kind (a newer server) is a prompt, not the local table.
    route = _fn(js, "voiceRoute")
    assert 'return { kind: "prompt" };' in route
    # Stop phrases: first in voiceHeard (any mode, before a dialog's "no"),
    # and the moment a stream settles one.
    heard = _fn(js, "voiceHeard")
    assert heard.index("var stop = voiceTail(text, false);") < heard.index(
        'if (mode === "dialog") {'
    )
    stopper = _fn(js, "voiceStopPhrase")
    assert "voiceStopForCommand();" in stopper and "voiceListenKept();" in stopper
    assert "voiceSend" not in stopper
    stt = _fn(js, "ServerSTT")
    words = stt.split("function streamWords(g, mine, committed, partial) {")[1]
    assert 'if (tail && tail.kind === "stop") {' in words
    # A guess holds back what may start a control phrase — the answers too
    # while the session is asking (not into the ask sheet's field).
    assert "Math.min(VOICE_TAIL_MAX, words.length)" in _fn(js, "voiceHoldBack")
    assert "voiceHoldBack(all, !voice.box && !!voiceAsks(voice.session))[0];" in _fn(
        js, "voiceGuessShow"
    )
    # Answers only while the session is asking.
    rest = _fn(js, "voiceHeardRest")
    assert "voiceAsks(voice.session) ? voiceTail(p.text, true) : null" in rest
    # The router's dialog answer: POST /answer gated by approve_dialogs.
    ra = _fn(js, "voiceRoutedAnswer")
    gate = ra.split("if (res.dialog_id && res.keys && res.keys.length) {")[1]
    assert gate.index("voice.cfg.approve_dialogs") < gate.index("voiceAnswerPost(")
    assert '"Answer it on the screen."' in gate
    assert "voiceRoute(p.text, title).then(" in ra  # only the row number: ask again
    assert "if (res.no_dialog)" in ra
    local = _fn(js, "voiceAnswerLocal")
    assert local.index("voice.cfg.approve_dialogs") < local.index("voiceAnswerPost(")
    assert "voiceParseAnswer(a.said, d.options)" in local  # "always" only when said
    # A targeted answer never moves voice: no select(), POST to that title.
    other = _fn(js, "voiceAnswerOther")
    assert "select(" not in other and "voiceAnswerLocal(title, ta);" in other
    assert "if (!title || title === voice.session) return false;" in other
    post = _fn(js, "voiceAnswerPost")
    assert "var mine = title === voice.session" in post
    assert "select(" not in post
    # A question's answer is a prompt: here with the box, there without it.
    send = _fn(js, "voiceAnswerSend")
    assert "voiceSend(voiceJoin(composeEl.value, words));" in send
    assert "else voiceSend(words, title);" in send
    vs = _fn(js, "voiceSend")
    assert "var other = title !== here;" in vs
    keep = vs.split("if (r.ok && other) {")[1].split("\n          }\n")[0]
    assert 'composeEl.value = ""' not in keep and "voiceAckStart" not in keep
    # Said name first.
    assert 'name + ": " + say' in _fn(js, "voiceNameFirst")
    # Diagnostics: the last route kind, and the numbering.
    diag = _fn(js, "voiceDiag")
    assert '"Router: " + (voice.lastRoute' in diag
    assert '"Session numbers: "' in diag


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
    # The hold-to-talk handlers are gestures too.
    assert _fn(js, "voiceHoldStart").lstrip().startswith("voicePrime();")
    assert _fn(js, "voiceHoldEnd").lstrip().startswith("voicePrime();")
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
    assert "JSON.stringify({ text: text, dialog_safe: true, say: say })" in send
    assert '"Sent to " + name + "."' in send
    assert '" is waking up, try again in a moment."' in send
    assert '"Couldn\'t send."' in send
    # The ack poll reads the newest reply, bounded.
    assert '"/output?view=last_reply&max_chars=600"' in v
    assert "var VOICE_ACK_POLL_MS = 3000;" in v
    assert "firstSentence(text)" in _fn(js, "voiceAckTick")
    # The answer: pinned to the dialog it was read from, by the person.
    assert (
        'voiceAnswerPost(voice.session, d.id, askOptKeys(opt), voiceKindWord(opt), "");'
        in _fn(js, "voiceDialogAnswer")
    )
    ans = _fn(js, "voiceAnswerPost")
    assert '"/api/instances/" + encodeURIComponent(title) + "/answer"' in ans
    assert 'var body = { keys: keys, dialog_id: dialogId, by: "user" };' in ans
    # A select question's free-text answer from the router carries its words.
    assert "if (text) body.text = String(text);" in ans
    assert "JSON.stringify(body)" in ans
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
    assert "var VOICE_DICT_IDLE_MS = 120000;" in v


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
    # "always" is accepted only when SAID as the whole answer: the matcher is
    # anchored at both ends, and nothing in the yes table can reach it.
    assert always.startswith("/^") and always.rstrip(";").endswith("$/")
    assert "always" in always
    assert "always" not in yes
    parse = _fn(js, "voiceParseAnswer")
    assert "VOICE_ALWAYS.test(t)" in parse
    # A no anywhere is a no: it is tested before yes, and only the anchored
    # always form (which cannot contain a refusal) comes before it.
    assert (
        parse.index("VOICE_ALWAYS.test(t)")
        < parse.index("VOICE_NO.test(t)")
        < parse.index("VOICE_YES.test(t)")
    )
    # Numbers never pick a permission option.
    assert "if (permission) {" in parse
    assert parse.index("if (permission) {") < parse.index("VOICE_NUM.exec(t)")
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
    assert "say that again" in cmds and "voice off" in cmds
    # Stop and hush are the owner's words (VOICE_W, from GET /api/voice).
    assert (
        'kind: "stop",     words: "stopCmd"' in cmds
        and 'kind: "hush",     words: "hush"' in cmds
    )
    assert (
        '"stop listening"' in v.split("var VOICE_WORDS_DEFAULT = {")[1].split("};")[0]
    )
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
    assert "voiceSwitchTo(m.hit.title);" in sw
    assert '"Which one: "' in sw
    to = _fn(js, "voiceSwitchTo")
    assert "select(title);" in to and '"Now on " + nameOf(title)' in to
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
    # ...once the speech queue has drained.
    assert 'voiceListenAfter("answer", VOICE_ANSWER_MS)' in after
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
    # Speaking pauses recognition so the phone does not hear itself; lines
    # are queued, never cut by the next one (test_mobile_speech_queue).
    assert "voice.stt.stop();" in _fn(js, "speakLine")
    assert "voice.stt.stop();" in _fn(js, "voiceSpeechPump")
    assert "voice.tts.cancel();" not in _fn(js, "speakLine")
    # Android: a short buzz when listening starts.
    assert "navigator.vibrate(30)" in _fn(js, "voiceListen")
    # Deep link ?voice=1 → the one big button; nothing speaks before a tap.
    assert 'new URLSearchParams(location.search).get("voice") === "1"' in v
    assert '"Tap to hear " + nameOf(title)' in v
    assert '"Tap to resume voice"' in v
    # Persistence: exactly these, wrapped.
    persist = _fn(js, "voicePersist")
    assert "localStorage.setItem(VOICE_STORE" in persist
    keys = re.findall(r"^\s+(\w+): ", persist.split("JSON.stringify({")[1], re.M)
    assert keys == [
        "session",
        "on",
        "rate",
        "voiceName",
        "handsFree",
        "ptt",
        "replies",
        "wake",
        "confirm",
        "sounds",
    ], keys
    assert persist.lstrip().startswith("try {")
    load = _fn(js, "voiceLoadPrefs")
    assert 'voice.prefs.ptt = j.ptt === "hold" ? "hold" : "toggle";' in load
    assert "voice.prefs.replies = voiceRepliesPref(j.replies);" in load


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


def test_mobile_js_server_recognizer_records_wav():
    """The server recognizer records 16 kHz 16-bit PCM mono WAV off the
    shared AudioContext — what the built-in Windows / macOS recognizers need
    and every other backend accepts — with one recorder for every browser:
    no MediaRecorder (webm on Chrome, mp4 on Safari) is left."""
    js = _js()
    v = _voice_section(js)
    assert "MediaRecorder" not in v
    assert "createAnalyser" not in v and "SERVER_MIMES" not in v
    stt = _fn(js, "ServerSTT")
    assert "var ctx = voiceAudioCtx();" in stt
    assert "ctx.createMediaStreamSource(s)" in stt
    assert "ctx.createScriptProcessor(4096, 1, 1)" in stt
    # Chrome runs a ScriptProcessor only when it reaches the destination:
    # through a muted gain, so the mic never plays.
    assert "g.mute.gain.value = 0;" in stt
    assert (
        "g.proc.connect(g.mute);" in stt and "g.mute.connect(ctx.destination);" in stt
    )
    # Channel 0 copied per block; the silence detector reads the same blocks.
    assert "new Float32Array(ev.inputBuffer.getChannelData(0))" in stt
    assert "Math.max(VOICE_RMS_SPEECH, g.floor * 3)" in stt
    assert "now - g.lastLoud > VOICE_SILENCE_MS" in stt
    # A phrase arrives 1.1 s after the last word, not 1.8 s; the cap stays.
    assert "var VOICE_SILENCE_MS = 1100;" in v
    assert "var VOICE_CLIP_MAX_MS = 12000;" in v
    assert (
        "Math.min(streams() ? VOICE_STREAM_MAX_MS : VOICE_CLIP_MAX_MS, remaining)"
        in stt
    )
    # The level is floor-relative (voiceLevelOf): a room's hum sits at zero.
    assert "h.onLevel(voiceLevelOf(rms, g.floor))" in stt
    # finish(): join, resample to 16 kHz (anti-aliased, async), encode,
    # upload as audio/wav. pending() covers the resample too.
    assert "var VOICE_WAV_RATE = 16000;" in v
    fin = stt.split("function finish() {")[1].split("\n    function upload(")[0]
    assert "uploading = true;" in fin
    assert fin.index("uploading = true;") < fin.index("voiceResampleClip(all, g.rate)")
    assert "if (token !== mine) return;" in fin
    assert "var wav = voiceWavEncode(pcm, VOICE_WAV_RATE);" in fin
    assert "voiceResample(" not in fin
    # The browser's resampler first; voiceResample only as its fallback.
    clip = _fn(js, "voiceResampleClip")
    assert "window.OfflineAudioContext || window.webkitOfflineAudioContext" in clip
    assert "new OAC(1, Math.ceil((samples.length * to) / from), to)" in clip
    assert "octx.createBufferSource()" in clip and "octx.startRendering()" in clip
    assert "octx.oncomplete" in clip  # older WebKit: the event, no promise
    assert "VOICE_RESAMPLE_TIMEOUT_MS" in clip
    assert "resolve(voiceResample(samples, from, to));" in clip
    assert "voice.rec.resampler = " in clip
    # The fallback is a low-pass decimator, never a plain interpolation down.
    rs = _fn(js, "voiceResample")
    assert "Math.sin(2 * Math.PI * fc * x) / (Math.PI * x)" in rs
    assert "var fc = VOICE_SINC_CUTOFF / step" in rs
    assert "if (step < 1) {" in rs  # interpolation only going UP
    assert 'new Blob([wav], { type: "audio/wav" })' in stt
    assert 'headers: { "Content-Type": mime }' in stt
    assert 'voice.rec.mime = "audio/wav";' in stt
    # Every way out takes the graph down.
    assert stt.count("recStop(g)") >= 3
    assert "n.disconnect();" in stt
    assert "function voiceWavEncode(samples, rate)" in v
    assert "function voiceResample(samples, from, to)" in v
    # The way to turn it on is the Settings → Voice tab now.
    assert '"Server recognition is off: Settings → Voice"' in stt
    assert "Settings → Your phone → Voice" not in js
    assert "Settings → Your phone → Voice" not in _html()
    assert "or turn on server recognition in Settings → Voice" in _html()


def test_mobile_js_server_recognizer_streams_words_as_you_speak():
    """On a server that streams (GET /api/voice → server_ready.stt_stream)
    the phrase goes out in chunks while it is spoken: one stream per phrase,
    the first chunk after 150 ms of speech, later ones paced by the last
    pass's time and only once the previous request is back; the phrase so
    far typed into the box as a guess and corrected by every answer, the
    whole phrase through the radio rules at its end (/end — the accurate
    model's ``final`` when it differs), DELETE on stop. A 409 on the open or
    any failed request turns the phrase into a clip."""
    js = _js()
    v = _voice_section(js)
    stt = _fn(js, "ServerSTT")
    # The routes, and the stream's own shape.
    assert 'streamReq(g, "/api/voice/stream", {' in stt
    assert "body: JSON.stringify({ lang: lang() })," in stt
    assert (
        'function streamUrl(sid, tail) { return "/api/voice/stream/" + '
        'encodeURIComponent(sid) + (tail ? "/" + tail : ""); }' in stt
    )
    assert 'streamUrl(g.sid, "chunk")' in stt and 'streamUrl(g.sid, "end")' in stt
    assert 'fetch(streamUrl(sid, ""), { method: "DELETE" })' in stt
    assert 'headers: { "Content-Type": "audio/wav" },' in stt
    # Only when the server says it streams; the clip path stays.
    assert (
        "function streams() { return !!(voice.cfg.server_ready && "
        "voice.cfg.server_ready.stt_stream); }" in stt
    )
    assert "if (streams()) streamOpen(g, mine);" in stt
    assert '"/api/voice/transcribe?lang="' in stt
    # Pacing: the first chunk after 150 ms of speech (the lead-in rides
    # along); later ones once voiceChunkMinMs(the last pass) of new audio is
    # in; never two requests in flight.
    assert "var VOICE_FIRST_CHUNK_MS = 150;" in v
    assert "var VOICE_STREAM_CHUNK_MS = 300;" in v
    pump = stt.split("function streamPump(g, mine) {")[1].split("\n    }\n")[0]
    assert "g.busy) return;" in pump
    assert "g.queued * 1000 >= g.rate * voiceChunkMinMs(passMs)" in pump
    assert "(g.queued - g.speechAt) * 1000 >= g.rate * VOICE_FIRST_CHUNK_MS" in pump
    assert "g.speechAt = g.queued - block.length;" in stt
    chunk = stt.split("function streamChunk(g, mine) {")[1].split("\n    }\n")[0]
    assert "g.busy = true;" in chunk and "g.busy = false;" in chunk
    assert "voiceResampleClip(all, g.rate)" in chunk  # 16 kHz, as a clip
    assert "voiceWavEncode(pcm, VOICE_WAV_RATE)" in chunk
    assert "g.blocks.splice(0, n);" in chunk  # cut only once answered
    # Only a first chunk is padded (before its audio): the stream is one piece.
    assert "if (first && pcm.length < min) {" in chunk
    assert "pad.set(pcm, min - pcm.length);" in chunk
    # The pass's time paces the next chunk (a quiet chunk's 0 does not).
    assert "if (ms > 0) passMs = voice.rec.passMs = ms;" in chunk
    assert "(g.trace.passes = g.trace.passes || []).push(ms);" in chunk
    # The audio processor pumps; the silence stop ends the phrase as before.
    assert 'else if (g.mode === "stream") streamPump(g, mine);' in stt
    # The cap drops a phrase the detector missed only when the stream
    # showed no words either.
    assert "if (g.frames && !g.heard && !g.words) { discard();" in stt
    # The phrase so far — committed words + the unstable tail — goes into
    # the box as a guess (onGuess, the "typed" mark) and to onInterim (the
    # ask sheet's mirror; the strip shows none of it); onFinal only at the
    # end (or for a stop phrase).
    words = stt.split("function streamWords(g, mine, committed, partial) {")[1]
    words = words.split("\n    }\n")[0]
    assert "var shown = voiceJoin(g.pend, g.partial);" in words
    assert "if (h.onInterim) h.onInterim(shown, g.partial);" in words
    assert 'if (h.onGuess && h.onGuess(shown)) voiceTraceMark(g, "typed");' in words
    assert words.count("h.onFinal(") == 1  # the stop phrase's
    assert "voiceStreamSplit" not in js and "h.whole" not in js
    rs = _fn(js, "voiceRecStart")
    assert (
        "onGuess: function (t) { if (!voiceIsListening()) return false; voiceHeardSome(); return voiceGuessShow(t); },"
        in rs
    )
    # The final takes the guess back first: the rules read the box as it
    # was before the phrase, and only final words (while the router has the
    # phrase before it, the guess in the box is that one's: it stays).
    assert 'voiceHeard(t, mode, voiceListen_.routing ? "" : voiceGuessDrop());' in rs
    assert rs.index("voiceGuessDrop();   // the phrase ended with no words") > rs.index(
        "onEnd: function () {"
    )
    # Never guessed: a dialog's words, anything while the router has a
    # phrase, a first phrase into an empty box that may be a command.
    gs = _fn(js, "voiceGuessShow")
    assert 'if (L.mode === "dialog" || L.routing) return false;' in gs
    assert "!voice.box && !base.trim() && voiceMayBeCommand(all)" in gs
    assert "var put = voiceGuessPut(cur, G.typed, words);" in gs
    assert "box.setSelectionRange(put.value.length, put.value.length);" in gs
    # A first phrase typed as it was said stays in the box while the router
    # reads its final words, and goes with the answer: no blink.
    heard = _fn(js, "voiceHeard")
    route = heard.split("if (p.text && !composeEl.value.trim()) {")[1]
    assert route.index("if (typed) voiceGuessHold(typed);") < route.index(
        "voiceRoute(p.text)"
    )
    assert route.index("L.routing = false;\n        voiceGuessDrop();") < route.index(
        "voiceRouted(res, p)"
    )
    # Listening that ends before the phrase's end keeps the guess as text.
    assert "if (!voiceIsListening()) { voiceLevel(null); voiceGuessKeep(); }" in _fn(
        js, "voiceSetState"
    )
    # The end: the rest, then /end, its words through onFinal — the
    # accurate final (when it differs) typed over the phrase first.
    end = stt.split("function streamEnd(g, mine) {")[1].split("\n    }\n")[0]
    assert "fin = voiceStreamFinal(text, x.j);" in end
    assert "streamDone(g, mine, fin.text);" in end
    fin = stt.split("function finish() {")[1].split("\n    function clipSend(")[0]
    assert 'if (g.mode === "clip") clipSend(g, mine);' in fin
    assert "else streamPump(g, mine);" in fin
    # stop()/discard() DELETE the stream (an open on its way deletes its id).
    disc = stt.split("function discard() {")[1].split("\n    }\n")[0]
    assert "streamDrop(live);" in disc
    drop = stt.split("function streamDrop(g) {")[1].split("\n    }\n")[0]
    assert "streamDelete(g.sid);" in drop
    assert "if (g.dead) { streamDelete(id); return; }" in stt
    # Fallbacks: a 409 → clips until GET /api/voice says otherwise; any
    # failure → the phrase as a clip behind the words so far.
    assert (
        "if (x.r.status === 409 && voice.cfg.server_ready) "
        "voice.cfg.server_ready.stt_stream = false;" in stt
    )
    fail = stt.split("function streamFail(g, mine, why) {")[1].split("\n    }\n")[0]
    assert 'g.mode = "clip";' in fail and "streamDelete(sid);" in fail
    assert "g.lead = voiceJoin(g.pend, g.partial);" in fail
    assert "if (g.ending && token === mine) clipSend(g, mine);" in fail
    assert (
        'upload(new Blob([wav], { type: "audio/wav" }), "audio/wav", mine, g.lead);'
        in stt
    )
    assert "var text = voiceJoin(lead, x.j.text);" in stt
    # Diagnostics: which way, and the last chunk.
    diag = _fn(js, "voiceDiag")
    assert '"Recognition stream: " + voiceDiagStream()' in diag
    ds = _fn(js, "voiceDiagStream")
    assert '"words as you speak (" + pass + last + ")"' in ds
    assert '"~" + voicePassSeconds(voice.rec.passMs) + " s/pass, "' in ds
    assert 'typed: "typed"' in _fn(js, "voiceDiagTrace")
    assert '"one clip per phrase"' in ds
    assert '"last chunk " + c.ms + " ms"' in ds


def test_mobile_js_server_recognizer_starts_fast_and_shows_it_hears():
    """The phone mic starts at once and shows it is picking the voice up:
    the microphone stays warm VOICE_KEEP_STREAM_MS after a phrase (reused
    by the next start, released on expiry, on voice off, and soon after the
    page hides); the stream opens alongside getUserMedia; nothing quiet is
    uploaded before the first word (a VOICE_LEAD_MS lead-in, so the first
    chunk goes as speech starts); "Hearing you…" while the detector hears a
    voice; a floor-relative, rAF-smoothed ring round the mic (and the ask
    sheet's); a tick on the first word; a start-up trace behind
    cs_voice_debug."""
    js = _js()
    v = _voice_section(js)
    css = _css()
    stt = _fn(js, "ServerSTT")
    assert "var VOICE_KEEP_STREAM_MS = 90000;" in v
    assert "var VOICE_KEEP_HIDDEN_MS = 15000;" in v
    assert "var VOICE_LEAD_MS = 300;" in v
    # The warm microphone: reused when live, its grace re-armed by every
    # way a phrase ends, cleared by the next start.
    gs = stt.split("function getStream() {")[1].split("\n    }\n")[0]
    assert gs.lstrip().startswith("clearTimeout(keepTimer);")
    assert "if (micLive(stream)) return Promise.resolve(stream);" in gs
    assert "micStop(stream);" in gs  # a dead one's tracks go before a new ask
    keep = stt.split("function keep(ms, hidden) {")[1].split("\n    }\n")[0]
    assert "}, ms || voice.keepMs);" in keep
    assert "/^(sending|working|speaking)$/.test(voice.state)" in keep
    assert "if (!document.hidden &&" in keep
    for fn in ("function finish() {", "function discard() {"):
        assert "keep();" in stt.split(fn)[1].split("\n    }\n")[0], fn
    rel = stt.split("function release(ms) {")[1].split("\n    }\n")[0]
    assert "if (ms > 0) { keep(ms, true); return; }" in rel
    assert "micStop(s);" in rel
    assert "keepMs: VOICE_KEEP_STREAM_MS," in v
    # Voice off gives it back now; the page hiding, soon.
    assert "voiceSttRelease();" in _fn(js, "voiceOff")
    vis = v.split('document.addEventListener("visibilitychange"')[1].split("\n  });\n")[
        0
    ]
    assert "voiceSttRelease(VOICE_KEEP_HIDDEN_MS);" in vis
    # The stream opens before the microphone is there.
    start = stt.split("function start(h) {")[1].split("\n    }\n")[0]
    assert start.index("if (streams()) streamOpen(g, mine);") < start.index(
        "getStream().then("
    )
    # Before the first word: only the lead-in is kept, nothing is sent.
    assert "(g.queued - g.blocks[0].length) * 1000 >= g.rate * VOICE_LEAD_MS" in stt
    pump = stt.split("function streamPump(g, mine) {")[1].split("\n    }\n")[0]
    assert "if (!g.heard) return;" in pump
    # A short first chunk is padded before its audio (where the quiet was).
    assert "pad.set(pcm, min - pcm.length);" in stt
    # "Hearing you…" while a voice comes in and nothing came back yet.
    st = stt.split("function recStatus(g, h) {")[1].split("\n    }\n")[0]
    assert "if (!h.onStatus || g.words) return;" in st
    assert '"Hearing you…"' in st and '"Listening… "' in st
    # The ring: still — one step (quiet / hearing / loud) from a rAF loop
    # over a ~400 ms smoothed level, written only when it changes; the CSS
    # eases the ring's OPACITY between the steps. No scale, no pulse.
    assert "requestAnimationFrame(voiceRingFrame)" in _fn(js, "voiceLevel")
    paint = _fn(js, "voiceRingPaint")
    assert 'step = voicePickup(v), s = ["0", "0.5", "1"][step]' in paint
    assert 'el.style.setProperty("--pick", s)' in paint and "--lvl" not in js
    assert "[voiceBtn, voiceLevelEl, askMicBtn]" in paint
    assert "var VOICE_RING_SMOOTH_MS = 400;" in v
    assert "Math.exp(-dt / VOICE_RING_SMOOTH_MS)" in _fn(js, "voiceLevelStep")
    assert "return v < 0.1 ? 0 : v < 0.5 ? 1 : 2;" in _fn(js, "voicePickup")
    ring = css.split("#voice-btn::after, #asking-mic::after {")[1].split("}")[0]
    assert "transition: opacity 400ms ease;" in ring and "transform" not in ring
    assert "border: 2px solid var(--tone-broken);" in ring
    mic = css[css.index("#voice-btn, #asking-mic {") : css.index("#voice-strip {")]
    assert "scale(" not in mic and "animation" not in mic
    assert "opacity: calc(0.4 + var(--pick, 0) * 0.6);" in mic
    # The meter: only while the ⚙ sheet is open.
    assert (
        'voiceLevelEl.classList.contains("hidden") && !voiceSheet.classList.contains("hidden")'
        in _fn(js, "voiceLevel")
    )
    assert 'voiceLevelEl.classList.add("hidden");' in _fn(js, "voiceSheetClose")
    assert "transform: scaleX(var(--pick, 0));" in css
    # No greyed partial anywhere on the strip: the words are in the box.
    assert "voice-partial" not in css and "voice-partial" not in js
    # The tick: once per phrase, never on top of the listening buzz.
    tick = _fn(js, "voiceSpeechTick")
    assert "Date.now() - voice.buzzAt < VOICE_TICK_GAP_MS" in tick
    assert "voice.buzzAt = Date.now(); try { navigator.vibrate(30); }" in _fn(
        js, "voiceListen"
    )
    assert "if (loud && !g.heard) {" in stt and "if (h.onSpeech) h.onSpeech();" in stt
    # The trace, and its switch.
    assert 'localStorage.getItem("cs_voice_debug")' in _fn(js, "voiceDebugOn")
    for mark in ('"mic"', '"open"', '"speech"', '"words"', '"typed"', '"settled"'):
        assert "voiceTraceMark(g, " + mark + ")" in stt, mark
    assert '"Last start: " + voiceDiagTrace(voice.rec.trace)' in _fn(js, "voiceDiag")


def test_mobile_js_diagnostics_recorder_and_built_in_engine():
    js = _js()
    diag = _fn(js, "voiceDiag")
    assert '"WAV 16 kHz (ScriptProcessor)"' in diag
    # Which resampler ran (or would), and the silence stop.
    assert '"Resampler: " + (voice.rec.resampler ||' in diag
    assert '"Silence stop: " + VOICE_SILENCE_MS + " ms after speech' in diag
    assert "MediaRecorder" not in diag
    # GET /api/voice's `system` (the server's built-in engine), said only
    # when the server sends it.
    assert "var sys = voice.cfg.system;" in diag
    assert (
        'sys && sys.platform ? "Built-in engine: " + sys.platform + '
        '(sys.detail ? " — " + sys.detail : "") : ""' in diag
    )
    assert "].filter(Boolean);" in diag
    load = _fn(js, "voiceLoadCfg")
    assert (
        'voice.cfg.system = j.system && typeof j.system === "object" ? j.system : null;'
        in load
    )


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
    dict_fns = js[
        js.index("  function voiceDictParse(") : js.index(
            "  // --- voice commands (matched"
        )
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
        "none": ["banana", "", "maybe later", "I always forget which"],
        # A refusal that contains the word is a NO, never the standing permission.
        "refusals": ["no, not always", "No! don't always allow that", "no, option 2"],
        # Numbers never pick a permission option (recognisers turn "to" into "2").
        "two": ["option two", "two", "the second one", "2", "number two"],
    }
    script = (
        'var voice = { session: "s" }; function nameOf(t) { return t; }\n'
        + text_fns
        + cmd_fns
        + dialog_fns
        + dict_fns
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
        "out.dict = ['fix the login bug over', 'Over.', 'disregard', 'Disregard that.', 'move it over', 'hangover', 'fix it, over!', 'nothing here', '', 'fix the login bug submit', 'Submit.', 'submit', 'add a test, Submit!', 'submitted', 'over it', 'Submit it', 'fix it over and out', 'over, out', 'Over and out.', 'move it over and', 'over and out of here', 'ship it, over. Out!'].map(function (t) { var p = voiceDictParse(t); return p.kind + '|' + p.text; });\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    # Radio words ("over", "disregard"): the LAST word, any case or
    # punctuation; mid-sentence is a word.
    assert out["dict"] == [
        "over|fix the login bug",
        "over|",
        "disregard|",
        "disregard|",
        "over|move it",
        "|hangover",
        "over|fix it",
        "|nothing here",
        "|",
        # "submit" is retired as a radio word: plain words, any case.
        "|fix the login bug submit",
        "|Submit.",
        "|submit",
        "|add a test, Submit!",
        "|submitted",
        # "that / it" after the word belongs to "disregard" only.
        "|over it",
        "|Submit it",
        # "over and out" (or "over, out") — the longer phrase first.
        "out|fix it",
        "out|",
        "out|",
        "|move it over and",
        "|over and out of here",
        "out|ship it",
    ]
    assert out["answers"]["yes"] == ["1", "1", "1", "1"]
    assert out["answers"]["no"] == ["3", "3", "3", "3"]
    # "always" only when said — and said in any of its forms.
    assert out["answers"]["always"] == ["2", "2", "2", "2", "2"]
    assert out["answers"]["none"] == [None, None, None, None]
    assert out["answers"]["refusals"] == ["3", "3", "3"]
    assert out["answers"]["two"] == [None, None, None, None, None]
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


@pytest.mark.skipif(_node() is None, reason="needs node")
def _control_fns(js: str) -> str:
    """voiceNorm, the dialog word tables, voiceDictParse and the control
    phrases (voiceTail, voiceTargetAnswer, …): what the matchers below run."""
    return (
        js[js.index("  function voiceNorm(") : js.index("  var VOICE_CMD = [")]
        + js[
            js.index('  var VOICE_ORD = ["one"') : js.index(
                "  function voiceParseAnswer("
            )
        ]
        + js[
            js.index("  function voiceDictParse(") : js.index(
                "  // --- voice commands (matched"
            )
        ]
    )


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_voice_guess_retype_under_node():
    """The guess model, run as-is: a streamed phrase is typed as it is
    heard and every answer corrects it from where it differs — the common
    prefix stays, the differing tail is taken back, the new tail goes in;
    the words before the guess are never touched; pacing follows the
    server's pass time."""
    js = _js()
    fns = js[js.index("  function voiceRetype(") : js.index("  function ServerSTT(")]
    # The pacing constants, as the page has them.
    consts = "\n".join(
        ln
        for ln in js.splitlines()
        if re.match(r"\s*var VOICE_(STREAM_CHUNK|CHUNK_MIN|CHUNK_MAX)_MS = ", ln)
    )
    assert consts.count("var ") == 3
    script = consts + "\n" + fns + _control_fns(js) + """
var out = {};
out.retype = [["fix the bu", "fix the bag"], ["fix the", "fix the bug"], ["fix the bug in", "fix the"],
              ["add a tesst", "add a test"], ["", "fix"], ["fix", ""], ["caf\u00e9", "cafe"], ["same", "same"]]
  .map(function (x) { return voiceRetype(x[0], x[1]); });
// Answers into a box that already holds a phrase: the guess grows, changes
// mid-phrase, shrinks, then is all held back.
var seq = [], v = "draft", t = "";
["fix the", "fix the parser", "fix a parser in", "fix a parser", ""].forEach(function (w) {
  var r = voiceGuessPut(v, t, w); v = r.value; t = r.typed; seq.push([v, t, r.erase, r.type]);
});
out.seq = seq;
out.empty = [voiceGuessPut("", "", "fix the"), voiceGuessPut("draft ", "", "fix")];
out.edited = voiceGuessPut("draft fix it", " fix the", "fix the bug");
// Committed words beyond what was typed (the guess was a prefix of them).
out.beyond = voiceGuessPut("draft fix th", " fix th", "fix the bug");
out.pace = [null, 0, -5, 42, 190, 400, 750, 2000].map(voiceChunkMinMs);
out.join = [voiceJoin("a ", " b"), voiceJoin("", "b"), voiceJoin("a", null)];
console.log(JSON.stringify(out));
"""
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["retype"] == [
        {"erase": "u", "type": "ag"},  # mid-word change
        {"erase": "", "type": " bug"},  # the guess grew
        {"erase": " bug in", "type": ""},  # a shorter new guess
        {"erase": "st", "type": "t"},
        {"erase": "", "type": "fix"},
        {"erase": "fix", "type": ""},
        {"erase": "\u00e9", "type": "e"},  # one code point
        {"erase": "", "type": ""},
    ]
    assert out["seq"] == [
        ["draft fix the", " fix the", "", " fix the"],
        ["draft fix the parser", " fix the parser", "", " parser"],
        ["draft fix a parser in", " fix a parser in", "the parser", "a parser in"],
        ["draft fix a parser", " fix a parser", " in", ""],
        ["draft", "", " fix a parser", ""],  # never past the phrase's start
    ]
    # An empty box: no leading space; a box ending in a space: none added.
    assert [e["value"] for e in out["empty"]] == ["fix the", "draft fix"]
    # Edited by hand: the guess can't be found, so nothing is taken back.
    assert out["edited"]["value"] == "draft fix it fix the bug"
    assert out["edited"]["erase"] == ""
    assert out["beyond"]["value"] == "draft fix the bug"
    assert out["beyond"]["erase"] == "" and out["beyond"]["type"] == "e bug"
    # 300 ms until a pass says; then 0.8 x the pass, held to 150-600 ms.
    assert out["pace"] == [300, 300, 300, 150, 152, 320, 600, 600]
    assert out["join"] == ["a b", "b", "a"]


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_voice_guess_hold_back_and_commands_under_node():
    """What a guess never types: a trailing control phrase or its start —
    the radio words ("over", "disregard that"), a stop phrase ("stop" may be
    "stop recording"), "stop talking" and its kin, and while the session is
    asking an answer ("yes", "option" …two) — until the next word rules it
    out; and a first phrase into an empty box that may still be a router
    command (it waits whole for the router)."""
    js = _js()
    cases = [
        "",
        "fix",
        "fix the login bug",
        "fix the login bug over",
        "fix it. Over.",
        "fix it over .",
        "go over the",
        "go over",
        "fix it disregard",
        "fix it disregard that",
        "add a test submit",
        "move it over to",
        "fix it stop",
        "fix it stop recording",
        "stop the server",
        "remove the logs that's",
        "remove the logs that is all",
        "turn the mic",
        "keep it quiet",
        "please stop talking",
        "add a test and yes",
        "pick option",
        "fix it over and",
        "fix it over and out",
        "carry it over and back",
    ]
    asking = [
        "add a test and yes",
        "pick option",
        "fix the login bug",
        "it is no",
        "do it",
    ]
    commands = [
        "",
        "fix the login bug",
        "switch",
        "switch to the parser",
        "switch the bug",
        "go back",
        "go fix it",
        "what's",
        "what is the parser asking",
        "what was it asking",
        "session number two",
        "why is it slow",
        "new session for the parser",
        "start a new session",
        "a test",
        "okay status",
        "please fix it",
        "hey",
        "yes",
        "no thanks",
        "ok do it",
        "this",
        "this function is broken",
        "session three yes",
        "the second one",
        "the bug",
        "stop",
        "stop the server",
        "Stop talking.",
        "open the readme",
        "option two",
        "two bugs remain",
        "never mind",
        "summarize that",
        "summarize the logs",
    ]
    script = (
        _control_fns(js)
        + "\nconsole.log(JSON.stringify({plain: %s.map(function (t) { return voiceHoldBack(t); }),"
        " asking: %s.map(function (t) { return voiceHoldBack(t, true); }),"
        " cmd: %s.map(function (t) { return voiceMayBeCommand(t); })}));\n"
        % (json.dumps(cases), json.dumps(asking), json.dumps(commands))
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["plain"] == [
        ["", ""],
        ["fix", ""],
        ["fix the login bug", ""],
        ["fix the login bug", "over"],
        ["fix it.", "Over."],
        ["fix it", "over ."],
        # "over" mid-sentence is a word: typed once the next word is in.
        ["go over the", ""],
        ["go", "over"],
        ["fix it", "disregard"],
        ["fix it", "disregard that"],
        # "submit" is a word now: typed as it comes.
        ["add a test submit", ""],
        ["move it over to", ""],
        # A stop phrase never reaches the box, even half said.
        ["fix it", "stop"],
        ["fix it", "stop recording"],
        ["stop the server", ""],
        ["remove the logs", "that's"],
        ["remove the logs", "that is all"],
        ["turn the", "mic"],
        ["keep it", "quiet"],
        ["please", "stop talking"],
        # Not asking: an answer word is a word.
        ["add a test and yes", ""],
        ["pick option", ""],
        # "over and" may become "over and out": held; "back" rules it out.
        ["fix it", "over and"],
        ["fix it", "over and out"],
        ["carry it over and back", ""],
    ]
    assert out["asking"] == [
        ["add a test and", "yes"],
        ["pick", "option"],
        ["fix the login bug", ""],
        ["it is", "no"],
        ["", "do it"],
    ]
    assert dict(zip(commands, out["cmd"])) == {
        "": True,
        "fix the login bug": False,
        "switch": True,
        "switch to the parser": True,
        "switch the bug": False,
        "go back": True,
        "go fix it": False,
        "what's": True,
        "what is the parser asking": True,
        "what was it asking": True,
        "session number two": True,
        "why is it slow": False,
        "new session for the parser": True,
        "start a new session": True,
        "a test": False,
        "okay status": True,
        "please fix it": False,
        "hey": True,
        "yes": True,
        "no thanks": True,
        "ok do it": True,
        "this": True,
        "this function is broken": False,
        "session three yes": True,
        "the second one": True,
        "the bug": False,
        "stop": True,
        "stop the server": False,
        "Stop talking.": True,
        "open the readme": True,
        "option two": True,
        "two bugs remain": False,
        "never mind": True,
        "summarize that": True,
        "summarize the logs": False,
    }


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_voice_control_phrases_under_node():
    """The control phrases, run as-is. A stop phrase at the END of any phrase
    stops (the words before it kept); while the session is asking, an answer
    at the end answers (a number only as the whole phrase or with its
    marker); a whole phrase can answer another session by row number or by
    name; the command table knows numbered and stepped switching, the
    switch verbs and "what is it asking"."""
    js = _js()
    cmd_fns = js[
        js.index("  var VOICE_CMD = [") : js.index("  function voiceRunCommand(")
    ]
    tails = [
        ["fix the parser stop recording", False],
        ["Stop listening.", False],
        ["remove the logs, that's all", False],
        ["mic off", False],
        ["I'm done talking", False],
        ["make it stop", False],
        ["stop the server", False],
        ["fix the parser yes", False],
        ["fix the parser yes", True],
        ["No, thanks.", True],
        ["always allow", True],
        ["yes and don't ask again", True],
        ["two", True],
        ["add two", True],
        ["use option two", True],
        ["the second one", True],
        ["answer yes", True],
        ["what about the tests", True],
    ]
    targets = [
        "session three yes",
        "Session 2, option one",
        "session two",
        "session banana yes",
        "tell invoice parser no",
        "tell parser to go ahead",
        "answer the login fix always",
        "answer yes",
        "tell me a joke",
    ]
    cmds = [
        "session two",
        "go to session 3",
        "focus on session number four",
        "next session",
        "switch to the previous one",
        "focus parser",
        "open the readme",
        "show login fix",
        "select api docs",
        "what is it asking",
        "What's it asking?",
        "what does it want",
        "stop listening",
    ]
    script = (
        _control_fns(js)
        + cmd_fns
        + "\nvar out = {tails: %s.map(function (x) { var t = voiceTail(x[0], x[1]); return t ? [t.kind, t.choice || '', t.number || 0, t.text, t.said] : null; }),"
        " targets: %s.map(function (x) { var t = voiceTargetAnswer(x); return t ? [t.index || t.name, t.choice, t.number || 0, t.said] : null; }),"
        " cmds: %s.map(function (x) { var c = voiceCommand(x); return c ? c.kind + (c.arg ? ':' + c.arg : '') : null; }),"
        " nums: ['two', '2', 'second', 'twelve', '12', 'banana'].map(voiceNumber)};\n"
        "console.log(JSON.stringify(out));\n"
        % (json.dumps(tails), json.dumps(targets), json.dumps(cmds))
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["tails"] == [
        ["stop", "", 0, "fix the parser", "stop recording"],
        ["stop", "", 0, "", "Stop listening"],
        ["stop", "", 0, "remove the logs", "that's all"],
        ["stop", "", 0, "", "mic off"],
        ["stop", "", 0, "", "I'm done talking"],
        None,
        None,
        # Not asking: "yes" is a word.
        None,
        ["answer", "yes", 0, "fix the parser", "yes"],
        ["answer", "no", 0, "", "No, thanks"],
        ["answer", "always", 0, "", "always allow"],
        ["answer", "always", 0, "", "yes and don't ask again"],
        # A bare number answers only as the whole phrase.
        ["answer", "number", 2, "", "two"],
        None,
        ["answer", "number", 2, "use", "option two"],
        ["answer", "number", 2, "", "the second one"],
        ["answer", "yes", 0, "", "answer yes"],
        None,
    ]
    assert out["targets"] == [
        [3, "yes", 0, "yes"],
        [2, "number", 1, "option one"],
        None,
        None,
        ["invoice parser", "no", 0, "no"],
        ["parser", "yes", 0, "go ahead"],
        ["the login fix", "always", 0, "always"],
        None,
        None,
    ]
    assert out["cmds"] == [
        "switch_index:two",
        "switch_index:3",
        "switch_index:four",
        "switch_rel:next",
        "switch_rel:previous",
        "focus:parser",
        "focus:the readme",
        "focus:login fix",
        "focus:api docs",
        "question",
        "question",
        "question",
        "stop",
    ]
    assert out["nums"] == [2, 2, 2, 12, 12, 0]


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_voice_wav_encoder_under_node():
    """voiceResample / voiceWavEncode, the page's own copies run as-is: the
    44-byte RIFF header of 16-bit PCM mono, samples scaled and clamped,
    48 kHz → 16 kHz a third of the length — low-pass filtered: what a 16 kHz
    clip cannot hold (above its 8 kHz Nyquist) is filtered out instead of
    folding back into the speech band, while speech keeps its level."""
    js = _js()
    fns = js[
        js.index("  var VOICE_RESAMPLE_TIMEOUT_MS") : js.index("  function ServerSTT(")
    ]
    script = (
        fns + "\nvar out = {};\n"
        "var buf = voiceWavEncode(new Float32Array([0, 1, -1, 0.5, 2, -3, NaN]), 16000);\n"
        "var v = new DataView(buf), b = new Uint8Array(buf);\n"
        "function ascii(at, n) { return String.fromCharCode.apply(null, Array.from(b.slice(at, at + n))); }\n"
        "out.len = buf.byteLength;\n"
        "out.riff = ascii(0, 4); out.size = v.getUint32(4, true); out.wave = ascii(8, 4);\n"
        "out.fmt = ascii(12, 4); out.fmtSize = v.getUint32(16, true); out.pcm = v.getUint16(20, true);\n"
        "out.channels = v.getUint16(22, true); out.rate = v.getUint32(24, true);\n"
        "out.byteRate = v.getUint32(28, true); out.align = v.getUint16(32, true); out.bits = v.getUint16(34, true);\n"
        "out.data = ascii(36, 4); out.dataSize = v.getUint32(40, true);\n"
        "out.samples = []; for (var i = 0; i < 7; i++) out.samples.push(v.getInt16(44 + i * 2, true));\n"
        "var sec = new Float32Array(48000); for (var i = 0; i < sec.length; i++) sec[i] = i / 48000;\n"
        "var r = voiceResample(sec, 48000, 16000);\n"
        "out.down = r.length; out.ramp = [r[1000], r[1001], r[8000]].map(function (x) { return Math.round(x * 48000); });\n"
        "function gain(f, from) { var n = from, x = new Float32Array(n), i, s = 0, t = 0;\n"
        "  for (i = 0; i < n; i++) { x[i] = Math.sin(2 * Math.PI * f * i / from); t += x[i] * x[i]; }\n"
        "  var y = voiceResample(x, from, 16000);\n"
        "  for (i = 200; i < y.length - 200; i++) s += y[i] * y[i];\n"
        "  return 10 * Math.log10((s / (y.length - 400)) / (t / n)); }\n"
        "out.db = {}; [300, 3000, 7000, 10000, 12000, 20000].forEach(function (f) { out.db[f] = gain(f, 48000); out.db['44k' + f] = gain(f, 44100); });\n"
        "out.same = voiceResample(sec, 16000, 16000) === sec;\n"
        "out.up = voiceResample(new Float32Array([0, 1]), 8000, 16000).length;\n"
        "out.mid = Array.from(voiceResample(new Float32Array([0, 1, 0]), 8000, 16000));\n"
        "out.empty = voiceWavEncode(new Float32Array(0), 16000).byteLength;\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["len"] == 44 + 7 * 2
    assert (out["riff"], out["wave"], out["fmt"], out["data"]) == (
        "RIFF",
        "WAVE",
        "fmt ",
        "data",
    )
    assert out["size"] == 36 + 14 and out["dataSize"] == 14
    assert out["fmtSize"] == 16 and out["pcm"] == 1 and out["channels"] == 1
    assert out["rate"] == 16000 and out["byteRate"] == 32000
    assert out["align"] == 2 and out["bits"] == 16
    # 1 → 32767, -1 → -32768, out of range clamped, NaN → silence.
    assert out["samples"] == [0, 32767, -32768, 16384, 32767, -32768, 0]
    assert out["down"] == 16000
    # A symmetric kernel keeps a straight line straight (away from the ends).
    assert out["ramp"] == [3000, 3003, 24000]
    db = out["db"]
    for rate in ("", "44k"):
        # Speech keeps its level: a 300 Hz and a 3 kHz tone, within 0.1 dB.
        assert abs(db[rate + "300"]) < 0.1, db
        assert abs(db[rate + "3000"]) < 0.1, db
        # Inside the clip's band but near its edge: dented, never gone.
        assert -6 < db[rate + "7000"] < 0, db
        # Above the 16 kHz clip's 8 kHz Nyquist: filtered out (>20 dB of
        # energy lost) instead of folding back to 6, 4 kHz… at full level as
        # a plain interpolation would.
        for f in ("10000", "12000", "20000"):
            assert db[rate + f] < -20, (rate + f, db)
    assert out["same"] is True
    assert out["up"] == 4
    # Between the samples, the straight line; past the last, the last.
    assert out["mid"] == [0, 0.5, 1, 0.5, 0, 0]
    assert out["empty"] == 44


@pytest.mark.skipif(_node() is None, reason="needs node")
def test_guess_control_under_node():
    """A streamed guess that ENDS in a control phrase acts once the voice has
    been quiet VOICE_CONTROL_TAIL_MS (ServerSTT's controlFire): "over" and
    "over and out" send (they wait for /end's accurate final, at most
    VOICE_CONTROL_END_MS), "disregard", a stop phrase and "stop talking"
    act on the guess alone; anything else is no control."""
    js = _js()
    script = (
        _control_fns(js)
        + js[js.index("  var VOICE_CMD = [") : js.index("  function voiceRunCommand(")]
        + "console.log(JSON.stringify(['fix the bug over', 'fix it over and out', 'no disregard',"
        " 'fix it stop recording', 'please stop talking', 'go over the', 'fix the bug', ''].map(function (t) {"
        " var c = voiceGuessControl(t); return c ? c.word + '|' + c.send : null; })));"
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [
        "over|true",
        "over and out|true",
        "disregard|false",
        "stop|false",
        "stop talking|false",
        None,
        None,
        None,
    ]
    v = _voice_section(js)
    assert (
        "var VOICE_CONTROL_TAIL_MS = 250;" in v
        and "var VOICE_CONTROL_END_MS = 2500;" in v
    )
    # The recorder fires it: quiet VOICE_CONTROL_TAIL_MS after a guess that
    # ends in one (the next guess may rule it out), never while loud.
    assert "g.ctl = voiceGuessControl(shown);" in js
    assert (
        "if (g.ctl && g.heard && !loud && now - g.lastLoud >= VOICE_CONTROL_TAIL_MS) controlFire(g, mine);"
        in js
    )
    fire = js[js.index("    function controlFire(g, mine) {") :]
    fire = fire[: fire.index("\n    }\n") + 6]
    assert "streamDrop(g);" in fire and "streamDone(g, mine, c.text);" in fire
    assert "}, VOICE_CONTROL_END_MS);" in fire and "finish();" in fire
    # The Enter is timed from the word's end: "over → sent 0.9 s".
    assert "voiceCtlSent();" in _fn(js, "voiceSendOn") and "voiceCtlSent();" in _fn(
        js, "voiceSend"
    )
    assert '" → sent " + (voice.rec.ctl.sentMs / 1000).toFixed(1) + " s"' in v


def test_no_hint_names_submit_as_a_radio_word():
    """'submit' is retired as a radio word: no hint, caption or placeholder
    on the phone page tells the owner to say it (a dialog's own "Submit" /
    "Submit answers" buttons are labels, not radio words)."""
    import re

    js, html = _js(), _html()
    for src in (js, html):
        assert "“submit”" not in src and "“Submit”" not in src
        assert not re.search(r"say\W{0,3}submit", src, re.I), re.search(
            r"say\W{0,3}submit", src, re.I
        )
    v = _voice_section(js)
    # Every hint names the radio words the owner has (voiceSaid: the first
    # phrase of each kind from GET /api/voice's words; ours by default).
    assert ' to send, " + voiceSaid("end") + " to stop, "' in v
    assert (
        'send: ["over"], end: ["over and out", "over, out"], clear: ["disregard"],' in v
    )
    # The ⚙ sheet's switches for the confirmations and the chimes.
    assert '<input type="checkbox" id="voice-confirm" />' in html
    assert '<input type="checkbox" id="voice-sounds" />' in html
