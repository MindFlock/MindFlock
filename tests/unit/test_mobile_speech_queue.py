"""The phone (/m) speaks one line at a time and reads what it should: the
pure rules behind it, run as-is under node — the speech queue's step
(``voiceQueueStep``: arrival order, hold while listening, the one-minute
drop), the name rule (``voiceLineText``: another session's name first, the
phone's own bare), Everything's turn rule (``voiceTurnLine``: every session,
once per reply, never voice's own while voice is on), the Spoken replies
setting read from storage (``voiceRepliesPref``: an older boolean), and the
select question's spoken picks (``askSelRouted``, ``askSelByWords``). The
behaviour in a browser is walked by ``test_mobile_voice_browser.py``."""

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


def _run(script: str) -> dict:
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _fns(*names: str) -> str:
    js = _js()
    return "".join(_whole(js, n) for n in names)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_queue_step_order_hold_and_age():
    script = _fns("voiceQueueStep") + """
var q = [{text: "a", at: 1000}, {text: "b", at: 2000}, {text: "c", at: 70500}];
var o = {playing: false, hold: false, maxAge: 60000};
function names(r) { return {next: r.next ? r.next.text : null, drop: r.drop.map(function (x) { return x.text; })}; }
console.log(JSON.stringify({
  first: names(voiceQueueStep(q, 5000, o)),
  playing: names(voiceQueueStep(q, 5000, {playing: true, hold: false, maxAge: 60000})),
  hold: names(voiceQueueStep(q, 5000, {playing: false, hold: true, maxAge: 60000})),
  late: names(voiceQueueStep(q, 62500, o)),
  allOld: names(voiceQueueStep(q, 200000, o)),
  empty: names(voiceQueueStep([], 5000, o)),
}));
"""
    out = _run(script)
    # Strict arrival order: the oldest line first.
    assert out["first"] == {"next": "a", "drop": []}
    # Nothing while a line plays or the mic listens (and nothing dropped then).
    assert out["playing"] == {"next": None, "drop": []}
    assert out["hold"] == {"next": None, "drop": []}
    # Older than a minute when its turn comes: dropped, the next fresh one said.
    assert out["late"] == {"next": "c", "drop": ["a", "b"]}
    assert out["allOld"] == {"next": None, "drop": ["a", "b", "c"]}
    assert out["empty"] == {"next": None, "drop": []}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_line_text_names_only_another_session():
    script = _fns("voiceNorm", "voiceLineText") + """
var NAMES = {"login-fix": "Login fix", "invoice-parser": "Invoice parser"};
function name(t) { return NAMES[t] || t; }
console.log(JSON.stringify([
  voiceLineText({text: "Fixed it.", title: "login-fix"}, "login-fix", name),
  voiceLineText({text: "Parsed twelve files.", title: "invoice-parser"}, "login-fix", name),
  voiceLineText({text: "Invoice parser: done.", title: "invoice-parser"}, "login-fix", name),
  voiceLineText({text: "Sent to API docs.", title: ""}, "login-fix", name),
  voiceLineText({text: "  Spaced.  ", title: "invoice-parser"}, "", name),
]));
"""
    assert _run(script) == [
        "Fixed it.",
        "Invoice parser: Parsed twelve files.",
        "Invoice parser: done.",
        "Sent to API docs.",
        "Invoice parser: Spaced.",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_everything_reads_every_session_once():
    script = _fns("voiceTurnLine") + """
function f(ev, t, say, extra) {
  var x = {event: ev, session: t, data: {say: say}};
  if (ev === "session.activity_changed") x.new = extra || "idle";
  return x;
}
var heard = {};
var out = {};
out.other = voiceTurnLine(f("session.activity_changed", "b", "Done."), "all", "a", heard);
heard.b = "Done.";
out.again = voiceTurnLine(f("session.turn_ended", "b", "Done."), "all", "a", heard);
out.newWords = voiceTurnLine(f("session.turn_ended", "b", "Tests pass."), "all", "a", heard);
out.voiceOwn = voiceTurnLine(f("session.turn_ended", "a", "Mine."), "all", "a", heard);
out.ownVoiceOff = voiceTurnLine(f("session.turn_ended", "a", "Mine."), "all", "", heard);
out.mine = voiceTurnLine(f("session.turn_ended", "c", "Hi."), "mine", "", heard);
out.off = voiceTurnLine(f("session.turn_ended", "c", "Hi."), "off", "", heard);
out.noSay = voiceTurnLine(f("session.turn_ended", "c", ""), "all", "", heard);
out.working = voiceTurnLine(f("session.activity_changed", "c", "Hi.", "working"), "all", "", heard);
out.question = voiceTurnLine({event: "session.question", session: "c", data: {say: "Hi?"}}, "all", "", heard);
console.log(JSON.stringify(out));
"""
    out = _run(script)
    assert out["other"] == {"title": "b", "say": "Done."}
    assert out["again"] is None  # idle and turn_ended carry the same reply
    assert out["newWords"] == {"title": "b", "say": "Tests pass."}
    assert out["voiceOwn"] is None  # voiceReply reads voice's own session
    assert out["ownVoiceOff"] == {"title": "a", "say": "Mine."}
    for k in ("mine", "off", "noSay", "working", "question"):
        assert out[k] is None, k
    # A new turn ("working") clears the memory, so the same words are read again.
    on_turn = _whole(_js(), "voiceOnTurn")
    assert 'f.new === "working"' in on_turn and "delete voice.heard[t];" in on_turn
    # Only news is read (voiceIsNews, below), as a reply line (its context).
    assert (
        "if (!voiceIsNews(voicePhoneMs(voiceTurnEnded(f.ts, (f.data || {}).idle_for)), voiceHeardAt(line.title), Date.now())) return;"
        in on_turn
    )
    assert "speakLine(line.say, true, line.title, at, true);" in on_turn


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_replies_pref_reads_an_older_boolean():
    script = _fns("voiceRepliesPref") + """
console.log(JSON.stringify([true, false, undefined, null, "off", "mine", "all", "bogus"].map(voiceRepliesPref)));
"""
    assert _run(script) == ["mine", "off", "mine", "mine", "off", "mine", "all", "mine"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_select_spoken_picks():
    js = _js()
    strip = _whole(js, "stripMarkdown")
    script = (
        _fns("voiceNorm", "askOptKeys", "askSelRouted", "askSelByWords") + strip + """
var OPTS = [{key: "1", label: "Postgres", kind: "other", keys: ["1"]},
            {key: "2", label: "SQLite", kind: "other", keys: ["Up", "2"]},
            {key: "3", label: "Type something", kind: "text", keys: ["3"]}];
function k(o) { return o ? o.key : null; }
console.log(JSON.stringify({
  routedKeys: k(askSelRouted({kind: "answer", keys: ["Up", "2"]}, OPTS)),
  routedNumber: k(askSelRouted({kind: "answer", number: 1}, OPTS)),
  routedPrompt: k(askSelRouted({kind: "prompt"}, OPTS)),
  routedNull: k(askSelRouted(null, OPTS)),
  words: k(askSelByWords("let's use postgres please", OPTS)),
  wordsCase: k(askSelByWords("SQLite.", OPTS)),
  wordsPart: k(askSelByWords("post", OPTS)),
  wordsNone: k(askSelByWords("mysql", OPTS)),
}));
"""
    )
    out = _run(script)
    # The router's keys match the option's own keys (a cursor on the text
    # row makes them ["Up", "2"]); its number, the option's key.
    assert out["routedKeys"] == "2"
    assert out["routedNumber"] == "1"
    assert out["routedPrompt"] is None and out["routedNull"] is None
    # An option named by its whole label, any case or punctuation.
    assert out["words"] == "1" and out["wordsCase"] == "2"
    assert out["wordsPart"] is None and out["wordsNone"] is None


def test_speech_queue_wiring():
    js = _js()
    speak = _whole(js, "speakLine")
    assert "voiceSpeech.q.push(" in speak and "voiceSpeechPump();" in speak
    # Never a cancel of the line being said: queued, not superseded.
    assert "voice.tts.cancel()" not in speak
    pump = _whole(js, "voiceSpeechPump")
    assert "hold: hold," in pump and "var hold = voiceIsListening() &&" in pump
    assert "maxAge: VOICE_LINE_MAX_AGE_MS" in pump and "console.debug(" in pump
    assert "var VOICE_LINE_MAX_AGE_MS = 60000;" in js
    # A tap on the mic stops the line being said and holds the rest.
    assert "voiceSpeechStop(false);" in _whole(js, "voiceListen")
    # Cancel / voice off / hidden: every line goes.
    assert "voiceSpeechStop(true);" in _whole(js, "voiceCancelTap")
    assert "voiceSpeechStop(true);" in _whole(js, "voiceOff")
    # A listen after a read-out waits for the queue to drain.
    after = _whole(js, "voiceListenAfter")
    assert "voiceSpeechDrain(" in after
    for fn in ("voiceAfterReply", "voiceReadDialog", "voiceDialogRetry"):
        assert "voiceListenAfter(" in _whole(js, fn), fn
    # "repeat" says the last line as said.
    assert "voice.lastLine = said;" in pump
    assert "speakLine(voice.lastLine" in _whole(js, "voiceRunCommand")
    # Everything keeps the socket whatever voice is.
    assert 'voice.prefs.replies === "all"' in _whole(js, "voiceEventsWanted")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_nods_jump_the_queue_play_over_a_listen_and_go_stale():
    """A spoken confirmation ("Sent.", voiceNod) queues at the FRONT — after
    the confirmations already there, before every other line — plays even
    while the mic listens (the listen's hold keeps every other line), and is
    dropped once VOICE_NOD_MAX_AGE_MS (5 s) old by its turn; the switch off
    (prefs.confirm false) or voice off says nothing."""
    js = _js()
    consts = "var VOICE_NOD_MAX_AGE_MS = 5000;\n"
    script = consts + _fns("voiceQueueStep", "voiceNod") + """
var voice = {prefs: {confirm: true}, state: "listening"};
var voiceSpeakSeq = 0, pumps = 0;
var voiceSpeech = {q: [{text: "reply", at: Date.now()}, {text: "other", at: Date.now()}]};
function voiceSpeechPump() { pumps++; }
voiceNod("Sent.");
voiceNod("Cleared.");
var order = voiceSpeech.q.map(function (x) { return x.text; });
var now = voiceSpeech.q[0].at;
function names(r) { return {next: r.next ? r.next.text : null, drop: r.drop.map(function (x) { return x.text; })}; }
var o = {playing: false, hold: true, maxAge: 60000, nodAge: 5000};
var out = {order: order, pumps: pumps,
  hold: names(voiceQueueStep(voiceSpeech.q, now + 10, o)),
  stale: names(voiceQueueStep(voiceSpeech.q, now + 6000, o)),
  open: names(voiceQueueStep(voiceSpeech.q.slice(2), now, {playing: false, hold: false, maxAge: 60000, nodAge: 5000}))};
voice.prefs.confirm = false;
voiceNod("Stopped.");
voice.prefs.confirm = true; voice.state = "off";
voiceNod("Stopped.");
out.after = voiceSpeech.q.length;
console.log(JSON.stringify(out));
"""
    out = _run(script)
    # Front of the queue, in the order they were made.
    assert out["order"] == ["Sent.", "Cleared.", "reply", "other"] and out["pumps"] == 2
    # While the mic listens: the nod plays, never the reply behind it.
    assert out["hold"] == {"next": "Sent.", "drop": []}
    # Older than 5 s by its turn: dropped; the reply still holds.
    assert out["stale"] == {"next": None, "drop": ["Sent.", "Cleared."]}
    assert out["open"] == {"next": "reply", "drop": []}
    # Off (the switch, or voice): nothing queued.
    assert out["after"] == 4


def test_nod_and_listen_wiring():
    js = _js()
    pump = _whole(js, "voiceSpeechPump")
    # Continuous listening holds the queue only mid-sentence; a line said
    # while listening is said over it, the recorder deaf, never stopped.
    assert "var hold = voiceIsListening() && (!voiceListen_.cont || mid);" in pump
    assert "if ((line.over = voiceIsListening())) {" in pump
    assert "voice.tts.speak(said, line.nod ? { cache: true } : undefined)" in pump
    end = _whole(js, "voiceSpeechEnd")
    assert "voice.micMuteUntil = Date.now() + VOICE_NOD_MUTE_MS;" in end
    assert "var VOICE_NOD_MUTE_MS = 150;" in js
    muted = _whole(js, "voiceMicMuted")
    assert "!!(p && p.over)" in muted
    assert "if (voiceMicMuted()) {" in js  # the recorder's block handler
    # The five lines, fired where they belong.
    assert 'voiceNod("Cleared.");' in _whole(js, "voiceHeard")
    assert 'voiceNod("Stopped.");' in _whole(js, "voiceStopPhrase")
    assert 'voiceNod("Sent.");' in _whole(js, "voiceSendOn")
    words = _whole(js, "voiceHeardWords")
    assert (
        '"Sent. Over and out." : "Sent."' in words
        and 'voiceNod("Over and out.")' in words
    )
    # A reply in continuous listening is said, not held; nothing after it stops the listen.
    assert '(voice.state === "listening" && !voiceListen_.cont)' in _whole(
        js, "voiceReply"
    )
    assert "if (voiceIsListening() && voiceListen_.cont) return;" in _whole(
        js, "voiceAfterReply"
    )
    # The chimes: every start, every stop, held while a line is said, off with Sounds.
    chime = _whole(js, "voiceChime")
    assert (
        "voice.prefs.sounds === false" in chime
        and 'if (p && (!p.nod || kind === "stop")) { voice.chimeWait = kind; return false; }'
        in chime
    )
    assert 'if (!voiceIsListening() && !L.woke) voiceChime("start");' in _whole(
        js, "voiceListen"
    )
    assert 'voiceChime("stop");' in _whole(js, "voiceSetState")
    # A held chime goes once nothing is said and the next line is not a
    # confirmation (after the last nod in a row), the next line after it.
    assert (
        "if (!S.playing && voice.chimeWait && !(line && line.nod) && voiceChimeFlush() && line) {"
        in pump
        and "S.timer = setTimeout(voiceSpeechPump, VOICE_CHIME_MS);" in pump
    )
    assert "voiceChimeFlush" not in end
    # A cut line never leaves its chime for later.
    assert 'voice.chimeWait = "";' in _whole(js, "voiceSpeechStop")
    assert 'voice.chimeWait = "";' in _whole(js, "voiceBargeIn")


def test_a_stop_never_cuts_its_own_confirmation():
    """A stop phrase, "over and out" and a send's nod queue the confirmation
    BEFORE listening ends — so the stop chime (held for a nod) comes after
    the line, never over it — and cancel nothing; the phone's recognizer is
    not restarted under a line said over the listen ("Cleared.")."""
    js = _js()
    stop = _whole(js, "voiceStopPhrase")
    assert stop.index('voiceNod("Stopped.");') < stop.index("voiceListenKept();")
    assert stop.index('voiceNod("Stopped.");') < stop.index("voiceIdle(")
    assert "voiceSpeechStop" not in stop and "tts.cancel" not in stop
    words = _whole(js, "voiceHeardWords")
    assert (
        'voiceStopForCommand(); voiceNod("Over and out."); voiceListenKept(); return; }'
        in words
    )
    send = _whole(js, "voiceSend")
    assert send.index("if (nod) voiceNod(nod);") < send.index(
        'voiceSetState("sending", text);'
    )
    heard = _whole(js, "voiceHeard")
    assert heard.index('voiceListenContinue();\n      voiceNod("Cleared.");') > 0
    # Listening over, the line said over it is over none: no echo strip after.
    assert "if (p && p.over) { p.over = false; voice.nodEcho = null; }" in _whole(
        js, "voiceSetState"
    )
    rec = _whole(js, "voiceRecStart")
    assert 'if (voice.stt.name === "phone" && p && p.nod && p.over) {' in rec
    assert "}, VOICE_REC_WAIT_MS);" in rec


# --------------------------------------------------------------------------- #
# News only: what is said by itself, and where it is from
# --------------------------------------------------------------------------- #
_MIN = 60000


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_is_news_table():
    """A reply is news only when it ended after the listener could have
    heard it and no more than ten minutes ago; an unknown end never is."""
    script = "var VOICE_NEWS_MS = 10 * 60000;\n" + _fns("voiceIsNews") + """
var NOW = 100 * 60000, M = 60000;
console.log(JSON.stringify({
  justNow:        voiceIsNews(NOW - 5000,    NOW - M,      NOW),
  beforeHeard:    voiceIsNews(NOW - 2 * M,   NOW - M,      NOW),
  atHeard:        voiceIsNews(NOW - M,       NOW - M,      NOW),
  neverHeard:     voiceIsNews(NOW - 3 * M,   0,            NOW),
  nineMin:        voiceIsNews(NOW - 9 * M,   NOW - 20 * M, NOW),
  tenMin:         voiceIsNews(NOW - 10 * M,  NOW - 20 * M, NOW),
  elevenMin:      voiceIsNews(NOW - 11 * M,  NOW - 20 * M, NOW),
  hourOld:        voiceIsNews(NOW - 60 * M,  0,            NOW),
  unknown:        voiceIsNews(0,             0,            NOW),
  garbage:        voiceIsNews("x",           0,            NOW),
}));
"""
    out = _run(script)
    assert out == {
        "justNow": True,
        "beforeHeard": False,  # ended before voice on / the switch / the page back
        "atHeard": False,
        "neverHeard": True,
        "nineMin": True,
        "tenMin": True,
        "elevenMin": False,  # too old, heard or not
        "hourOld": False,  # "done for a while": never read by itself
        "unknown": False,
        "garbage": False,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_context_lead_table():
    """A reply opens with "In <name>, on <task>:" when it is about another
    session than the one on screen, or one not heard from in ten minutes (or
    ever); bare for the session talked to right along."""
    script = (
        "var VOICE_CONTEXT_GAP_MS = 10 * 60000;\n"
        + _fns("voiceNorm", "voiceContextLead")
        + """
var NOW = 100 * 60000, M = 60000;
function lead(o) {
  return voiceContextLead(Object.assign({title: "a", here: "a", name: "Login fix",
    headline: "Fix the pin bar", lastAt: NOW - M, now: NOW, text: "Fixed it."}, o));
}
console.log(JSON.stringify({
  ownRecent:   lead({}),
  ownAway:     lead({lastAt: NOW - 11 * M}),
  ownNever:    lead({lastAt: 0}),
  other:       lead({here: "b"}),
  otherNoTask: lead({here: "b", headline: null}),
  acronym:     lead({here: "b", headline: "API docs cleanup."}),
  named:       lead({here: "b", text: "Login fix: fixed it."}),
  alreadyIn:   lead({here: "b", text: "In Login fix, all done."}),
  noName:      lead({here: "b", name: ""}),
  noTitle:     lead({title: "", here: "b"}),
}));
"""
    )
    out = _run(script)
    assert out == {
        "ownRecent": "",
        "ownAway": "In Login fix, on fix the pin bar: ",
        "ownNever": "In Login fix, on fix the pin bar: ",
        "other": "In Login fix, on fix the pin bar: ",
        "otherNoTask": "In Login fix: ",
        "acronym": "In Login fix, on API docs cleanup: ",
        "named": "",
        "alreadyIn": "",
        "noName": "",
        "noTitle": "",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_turn_ended_less_its_dwell():
    script = _fns("voiceTurnEnded") + """
console.log(JSON.stringify([voiceTurnEnded(1000), voiceTurnEnded(1000, 12), voiceTurnEnded("1000", "12.5"),
  voiceTurnEnded(1000, -3), voiceTurnEnded(0, 12), voiceTurnEnded(null, 12)]));
"""
    assert _run(script) == [1000, 988, 987.5, 1000, 0, 0]


def test_every_unasked_reply_path_asks_is_news():
    """Voice on, the poll, the events (and their replay), the page coming
    back, a switch, Everything, resuming voice: each goes through voiceIsNews
    (voiceReply for the session voice is on); the deep link and
    voiceSayLast are asked for, so they do not."""
    js = _js()
    reply = _whole(js, "voiceReply")
    assert (
        "if (!(data && data.demand) && !voiceIsNews(voiceReplyEnded(data), voiceHeardAt(voice.session), Date.now()))"
        in reply
    )
    # The gate comes before anything is said.
    assert reply.index("voiceIsNews(") < reply.index("speakLine(")
    for fn in ("voiceOnPoll", "voiceGotIdle"):
        assert "voiceReply({" in _whole(js, fn)
    assert "at: voiceTurnEnded(frameTs, data.idle_for)" in _whole(js, "voiceGotIdle")
    assert "voiceIsNews(" in _whole(js, "voiceOnTurn")
    switch = _whole(js, "voiceSwitchTo")
    assert "news = voiceSwitchNews(title);" in switch
    assert switch.index("voiceSwitchNews(") < switch.index("select(title)")
    assert "last_say" not in switch
    assert "voiceIsNews(" in _whole(js, "voiceSwitchNews")
    hear = _whole(js, "voiceHearNow")
    assert "(!newsOnly || voiceIsNews(" in hear and "demand: true" in hear
    assert "voiceHearNow(title, true);" in js  # the resume offer: news only
    # The lines: voice on / the page back move the boundary; a send makes its
    # reply news whatever comes between.
    assert "voice.newsFrom = Date.now();" in _whole(js, "voiceTurnOn")
    assert "if (!document.hidden) voice.newsFrom = Date.now();" in js
    assert js.count("voiceHeardNow(title, true)") == 3  # voiceSend ×2, voiceSendOn
    say_last = _whole(js, "voiceSayLast")
    assert "voiceIsNews" not in say_last and "voice.sayLast = voiceSayLast;" in js
