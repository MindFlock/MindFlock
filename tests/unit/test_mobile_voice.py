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
    # The strip above the composer: state word, caption, Cancel, the gear,
    # the one big "Tap to hear" button, an error line.
    for el in (
        'id="voice-strip"',
        'id="voice-state"',
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
    listening goes on across pauses; a trailing "over" sends the box, a
    trailing "disregard" empties it and listens on; Cancel drops the words,
    VOICE_DICT_IDLE_MS of silence stops with them kept, never sent, and there
    is no countdown anywhere. Commands are commands only while the box is
    empty; a dialog answer needs no "over"."""
    js = _js()
    v = _voice_section(js)
    heard = _fn(js, "voiceHeard")
    assert "var p = voiceDictParse(text);" in heard
    assert 'p.kind === "disregard"' in heard and 'composeEl.value = "";' in heard
    words = _fn(js, "voiceHeardWords")
    assert 'if (p.kind === "over") {' in words and "voiceSend(composeEl.value)" in words
    assert (
        "if (p.text && !composeEl.value.trim()) {" in heard
    )  # commands: empty box only
    assert "voiceDialogAnswer(p.text);" in heard
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
        'if (mode !== "dialog" && composeEl.value.trim()) { voiceListenStop(); return; }'
        in expired
    )
    assert '"Kept, not sent — Send sends it, or tap the mic to go on"' in _fn(
        js, "voiceListenStop"
    )
    # The hint names the gesture that sends, with the radio words.
    assert (
        'var VOICE_HINT_TAP = "Tap again or say “over” to send, “disregard” to start over";'
        in v
    )
    assert (
        'var VOICE_HINT_HOLD = "Release or say “over” to send, “disregard” to start over";'
        in v
    )
    assert "return voiceListen_.held ? VOICE_HINT_HOLD : VOICE_HINT_TAP;" in _fn(
        js, "voiceDictHint"
    )
    assert "VOICE_DICT_HINT" not in v
    # Cancel while dictating drops the words.
    assert (
        'if (voiceIsListening()) { composeEl.value = ""; autosizeCompose(); voice.dictated = false; }'
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
    """Push to talk (prefs.ptt, "toggle" by default): in toggle mode the
    second tap on the mic or the strip SENDS the box (an empty box only
    stops); in hold mode pointerdown on the mic starts listening and
    pointerup / pointercancel sends — after waiting for the words still on
    their way (VOICE_SEND_WAIT_MS for the phone's last phrase, as long as a
    server clip is pending, a route answer) — and a release that heard
    nothing only stops. The click a release fires is swallowed; the click
    still turns voice on and starts a tap-style listen for a keyboard. The
    silence stop keeps the words, unsent."""
    js = _js()
    v = _voice_section(js)
    html = _html()
    assert "var VOICE_SEND_WAIT_MS = 1500;" in v
    # The sheet's choice, and the default.
    assert (
        'prefs: { rate: 1, voiceName: "", handsFree: false, ptt: "toggle", replies: true }'
        in v
    )
    sel = html.split('<select id="voice-ptt"')[1].split("</select>")[0]
    assert '<option value="toggle">Tap to talk</option>' in sel
    assert '<option value="hold">Hold to talk</option>' in sel
    assert ">Push to talk<" in html
    # Toggle: the second tap (mic or strip) sends; a dialog tap only stops.
    tap = _fn(js, "voiceTap")
    assert "if (voiceIsListening()) { voiceListenSend(); return; }" in tap
    assert "voiceListenDone();" not in tap
    strip = v.split('voiceStrip.addEventListener("click", function (ev) {')[1].split(
        "\n  });\n"
    )[0]
    assert "if (voiceIsListening()) voiceListenSend();" in strip
    send = _fn(js, "voiceListenSend")
    assert 'if (L.mode === "dialog") { voiceListenDone(); return; }' in send
    assert "L.sendAfter = true;" in send and "L.until = 0;" in send
    assert "if (s.finish) s.finish(); else if (s.end) s.end();" in send
    soon = _fn(js, "voiceListenFlushSoon")
    assert (
        "L.routing || (s.pending && s.pending()) || (s.active() && Date.now() < L.sendBy)"
        in soon
    )
    flush = _fn(js, "voiceListenFlush")
    assert (
        "if (composeEl.value.trim() && (L.heard || !L.held)) { voiceSend(composeEl.value); return; }"
        in flush
    )
    assert "voiceListenStop();" in flush
    # The phrase that ends after the press: PhoneSTT's graceful end().
    stt = _fn(js, "PhoneSTT")
    assert "function end() {" in stt and "r.stop();" in stt.split("function end() {")[1]
    # The recognizer ending / a phrase landing while a send is due flushes.
    assert "if (L.sendAfter) { voiceListenFlushSoon(); return; }" in _fn(
        js, "voiceRecStart"
    )
    assert "if (voiceListen_.sendAfter) { voiceListenFlushSoon(); return; }" in _fn(
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
    assert "if (voiceIsListening() && voiceListen_.held) voiceListenSend();" in end
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
    btn = _css().split("#voice-btn {")[1].split("}")[0]
    assert "touch-action: none;" in btn and "-webkit-touch-callout: none;" in btn
    # The idle caption names the gesture.
    assert (
        'return voice.prefs.ptt === "hold" ? "Hold the mic to talk" : "Tap to talk";'
        in _fn(js, "voiceTalkHint")
    )
    # The silence stop is still the kept-not-sent one.
    assert "voiceListenSend" not in _fn(js, "voiceListenExpired")


def test_mobile_js_spoken_replies_say_per_prompt():
    """Spoken replies (prefs.replies, on by default): POST /send asks for the
    Say line only for a prompt that was dictated while it is on — a typed
    prompt costs no Say tokens — and a reply to a say:false prompt is never
    read out, only "Your turn". "Sent to X." and the ack are spoken only with
    Spoken replies on."""
    js = _js()
    html = _html()
    assert 'id="voice-replies"' in html and "Spoken replies" in html
    send = _fn(js, "voiceSend")
    assert "var dictated = !!voice.dictated;" in send
    assert "var say = !!(voice.prefs.replies && dictated);" in send
    assert "voice.expectSay = say;" in send and "voice.dictated = false;" in send
    assert (
        "if (base !== null && voice.prefs.replies) voiceAckStart(title, base);" in send
    )
    assert (
        "if (voice.prefs.replies) speakLine(sent, true); else voiceCaption(sent);"
        in send
    )
    # Dictated words set the flag; emptying the box clears it.
    assert "voice.dictated = true;" in _fn(js, "voiceHeardWords")
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
    assert '"Replies: " + (voice.prefs.replies ? "on" : "off")' in diag
    assert '"Push to talk: " + (voice.prefs.ptt === "hold" ? "hold" : "tap")' in diag
    assert (
        '"Last prompt: "' in diag
        and '"dictated" : "typed"' in diag
        and '", say "' in diag
    )


def test_mobile_js_reply_lines_start_with_the_session_name():
    """Every spoken reply line opens with the session's display name, so a
    voice is never anonymous: the Say line, the ack, "<name> finished.", the
    poll fallback (which goes through voiceReply) and the dialog read-out."""
    js = _js()
    reply = _fn(js, "voiceReply")
    assert 'speakLine(name + ": " + say)' in reply
    assert 'speakLine(name + " finished.")' in reply
    assert "var name = nameOf(voice.session);" in reply
    assert 'speakLine(nameOf(title) + ": " + line, true)' in _fn(js, "voiceAckTick")
    assert 'nameOf(voice.session) + ": " + stripMarkdown(d.question' in _fn(
        js, "voiceDialogSpeech"
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
    assert "JSON.stringify({ text: text, session: voice.session || current })" in route
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
        and "voiceHeardWords(p);" in fallback
    )
    assert 'case "prompt": voiceHeardWords(p); return false;' in routed
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
    keys = re.findall(r"^\s+(\w+): ", persist.split("JSON.stringify({")[1], re.M)
    assert keys == [
        "session",
        "on",
        "rate",
        "voiceName",
        "handsFree",
        "ptt",
        "replies",
    ], keys
    assert persist.lstrip().startswith("try {")
    load = _fn(js, "voiceLoadPrefs")
    assert 'voice.prefs.ptt = j.ptt === "hold" ? "hold" : "toggle";' in load
    assert "voice.prefs.replies = j.replies !== false;" in load


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
    assert "Math.min(VOICE_CLIP_MAX_MS, remaining)" in stt
    assert "h.onLevel(Math.min(1, rms * 4))" in stt
    # finish(): join, resample to 16 kHz, encode, upload as audio/wav.
    assert "var VOICE_WAV_RATE = 16000;" in v
    assert (
        "voiceWavEncode(voiceResample(all, g.rate, VOICE_WAV_RATE), VOICE_WAV_RATE)"
        in stt
    )
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


def test_mobile_js_diagnostics_recorder_and_built_in_engine():
    js = _js()
    diag = _fn(js, "voiceDiag")
    assert '"WAV 16 kHz (ScriptProcessor)"' in diag
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
        "out.dict = ['fix the login bug over', 'Over.', 'disregard', 'Disregard that.', 'move it over', 'hangover', 'fix it, over!', 'nothing here', ''].map(function (t) { var p = voiceDictParse(t); return p.kind + '|' + p.text; });\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    # Radio words: the LAST word, any case or punctuation; mid-sentence is a word.
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
def test_voice_wav_encoder_under_node():
    """voiceResample / voiceWavEncode, the page's own copies run as-is: the
    44-byte RIFF header of 16-bit PCM mono, samples scaled and clamped,
    48 kHz → 16 kHz a third of the length (linear interpolation)."""
    js = _js()
    fns = js[js.index("  function voiceResample(") : js.index("  function ServerSTT(")]
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
        "out.down = r.length; out.down3 = [r[0], r[1], r[2]].map(function (x) { return Math.round(x * 48000); });\n"
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
    assert out["down3"] == [0, 3, 6]
    assert out["same"] is True
    assert out["up"] == 4
    # Between the samples, the straight line; past the last, the last.
    assert out["mid"] == [0, 0.5, 1, 0.5, 0, 0]
    assert out["empty"] == 44
