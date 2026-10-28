"""The phone page's voice words: every client-side word check is built from
``GET /api/voice``'s ``words`` (Settings → Voice; ours by default), and the
"How to talk" tutorial card names them.

The matchers run as-is under node (the same slices test_mobile_voice.py
runs); the headless-Chrome harness (voice_browser.js) drives the words from
the mocked server, their refresh and the card end to end. The sound-alike
tolerance — ``words.heard`` and the phonetic key — is pinned by
``tests/unit/data/voice_sounds.json``, the fixture the server's reference
(``core.voice_words.control_kind``) and the desktop (``voiceEars.test.ts``)
run too.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
DOCS = ROOT / "docs" / "web-ui.md"


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _html() -> str:
    return (STATIC / "mobile.html").read_text()


def _fn(js: str, name: str) -> str:
    body = js.split("  function %s(" % name)[1].split("\n  }\n")[0]
    return body.split("{", 1)[1]


def _matchers(js: str) -> str:
    """The words block and every matcher built from it (test_mobile_voice's
    _control_fns plus the command table)."""
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
        + js[js.index("  var VOICE_CMD = [") : js.index("  function voiceRunCommand(")]
    )


CUSTOM = {
    "send": ["roger"],
    "end": ["roger and out", "roger, out"],
    "clear": ["scrap that", "never mind that"],
    "stop": ["halt", "halt listening", "we're done"],
    "hush": ["silence", "pipe down"],
    "wake": "hey jarvis",
}


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


PROBE = """
function probe() {
  var o = {};
  o.dict = %(dict)s.map(function (t) { var p = voiceDictParse(t); return p.kind + "|" + p.text; });
  o.tail = %(tail)s.map(function (t) { var x = voiceTail(t, false); return x ? x.text + "|" + x.said : null; });
  o.cmd = %(cmd)s.map(function (t) { var c = voiceCommand(t); return c ? c.kind : null; });
  o.hold = %(hold)s.map(function (t) { return voiceHoldBack(t); });
  o.may = %(may)s.map(function (t) { return voiceMayBeCommand(t); });
  o.guess = %(guess)s.map(function (t) { var c = voiceGuessControl(t); return c ? c.word + "|" + c.send : null; });
  o.said = [voiceSaid("send"), voiceSaid("end"), voiceSaid("clear"), voiceWakeName()];
  return o;
}
"""

CASES = {
    "dict": [
        "fix the bug roger",
        "fix it, Roger and out.",
        "ship it roger out!",
        "fix the bug over",
        "fix it over and out",
        "toss it scrap that",
        "toss it, scrap that.",
        "toss it disregard",
        "rogers",
        "roger that",
    ],
    "tail": [
        "fix it halt listening",
        "fix it we're done",
        "fix it were done",
        "fix it halt",
        "fix it that's all",
        "fix it stop listening",
    ],
    "cmd": [
        "silence",
        "Pipe down.",
        "stop talking",
        "okay stop",
        "halt",
        "halt listening",
        "stop",
        "quiet",
        "turn off the mic",
    ],
    "hold": [
        "fix it roger",
        "fix it roger and",
        "fix it scrap",
        "fix it halt",
        "fix it pipe",
        "fix it over",
        "fix it disregard",
        "go over the",
    ],
    "may": ["silence", "halt", "pipe down", "stop", "be quiet", "halt the server"],
    "guess": [
        "fix the bug roger",
        "fix it roger and out",
        "toss it scrap that",
        "fix it halt listening",
        "please silence",
        "fix the bug over",
    ],
}


def _probe_script(js: str, words) -> str:
    return (
        _matchers(js)
        + PROBE % {k: json.dumps(v) for k, v in CASES.items()}
        + "var before = probe();\n"
        + "voiceWordsApply(%s);\n" % json.dumps(words)
        + "var after = probe();\n"
        + "voiceWordsApply(null);\n"
        + "var back = probe();\n"
        + "console.log(JSON.stringify({before: before, after: after, back: back, w: VOICE_W.words}));\n"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_matchers_are_built_from_the_words_under_node():
    out = _run(_probe_script(_js(), CUSTOM))
    before, after, back = out["before"], out["after"], out["back"]
    # Ours before GET /api/voice says anything (and after an older server's
    # answer with no words): the radio words as they always were.
    assert before["dict"][3:5] == ["over|fix the bug", "out|fix it"]
    assert before["dict"][7] == "disregard|toss it"
    assert before["dict"][0] == "|fix the bug roger"
    assert before["said"] == ["“over”", "“over and out”", "“disregard”", "MindFlock"]
    assert back == before
    assert out["w"]["send"] == ["over"] and out["w"]["wake"] == "mindflock"
    # The owner's words: send, end (longest first: "roger and out" beats
    # "roger"; the comma optional), clear (with its optional tail words).
    assert after["dict"] == [
        "over|fix the bug",
        "out|fix it",
        "out|ship it",
        "|fix the bug over",
        "|fix it over and out",
        "disregard|toss it",
        "disregard|toss it",
        "|toss it disregard",
        "|rogers",
        "|roger that",
    ]
    # A stop phrase of two words or more ends any phrase (an apostrophe
    # optional); a one-word one ("halt") only on its own; ours are gone.
    assert after["tail"] == [
        "fix it|halt listening",
        "fix it|we're done",
        "fix it|were done",
        None,
        None,
        None,
    ]
    assert after["cmd"] == [
        "hush",
        "hush",
        None,
        None,
        "stop",
        "stop",
        None,
        None,
        None,
    ]
    assert before["cmd"] == [
        (
            "hush"
            if c in ("stop talking", "okay stop", "quiet")
            else (
                None
                if c in ("silence", "Pipe down.", "halt", "halt listening")
                else "stop"
            )
        )
        for c in CASES["cmd"]
    ]
    # A guess holds back what may start one of the owner's phrases — and
    # types ours, which are words now.
    assert after["hold"] == [
        ["fix it", "roger"],
        ["fix it", "roger and"],
        ["fix it", "scrap"],
        ["fix it", "halt"],
        ["fix it", "pipe"],
        ["fix it over", ""],
        ["fix it disregard", ""],
        ["go over the", ""],
    ]
    # (A lone "stop" is still all answer words — "stop" refuses a dialog.)
    assert after["may"] == [True, True, True, True, False, False]
    # The fast control on a streamed guess, named by the owner's word.
    assert after["guess"] == [
        "roger|true",
        "roger and out|true",
        "scrap that|false",
        "stop|false",
        "stop talking|false",
        None,
    ]
    assert after["said"] == ["“roger”", "“roger and out”", "“scrap that”", "Hey Jarvis"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_words_are_cleaned_and_fall_back_per_kind_under_node():
    js = _js()
    script = (
        _matchers(js)
        + "console.log(JSON.stringify(["
        + "voiceWordsClean(null),"
        + "voiceWordsClean({send: [], end: 'over and out', clear: [' Scrap  That ', 7, '', 'scrap that'], wake: 'x1'}),"
        + "voiceWordsClean({stop: ['Halt’s it'], wake: ' Hey  Jarvis '})"
        + "]));"
    )
    none, partial, apos = _run(script)
    assert none["send"] == ["over"] and none["end"] == ["over and out", "over, out"]
    assert none["stop"][0] == "stop" and none["hush"][0] == "stop talking"
    # An empty list, a string, junk: that kind is ours; the rest is cleaned.
    assert partial["send"] == ["over"] and partial["end"] == [
        "over and out",
        "over, out",
    ]
    assert partial["clear"] == ["scrap that"]
    assert partial["wake"] == "mindflock"
    assert apos["stop"] == ["halt's it"] and apos["wake"] == "hey jarvis"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_regex_specials_in_a_phrase_are_escaped_under_node():
    js = _js()
    script = (
        _matchers(js)
        + "voiceWordsApply({send: ['c++ go'], clear: ['a.b']});"
        + "console.log(JSON.stringify(['fix c go', 'fix it a b', 'fix it axb', 'fix it c++ go'].map(function (t) {"
        + " var p = voiceDictParse(t); return p.kind + '|' + p.text; })));"
    )
    # voiceNorm drops what is not a letter, digit or apostrophe, so a
    # phrase with a special in it is its words ("c go", "a b"); none
    # reaches a pattern raw (the server only ever sends letters, digits,
    # spaces, apostrophes and commas). Said with its specials, the phrase
    # is still its letters (voiceSoundTail: "c++ go" is "c go").
    assert _run(script) == [
        "over|fix",
        "disregard|fix it",
        "|fix it axb",
        "over|fix it",
    ]


def test_the_words_come_from_get_api_voice_on_every_refetch():
    js = _js()
    load = _fn(js, "voiceLoadCfg")
    assert "voiceWordsApply(j.words);" in load and "voiceWordsShow(hint);" in load
    assert load.index("var hint = voiceDictHint();") < load.index(
        "voiceWordsApply(j.words);"
    )
    # Ours at once, before any fetch.
    assert "  voiceWordsApply(null);   // ours, until GET /api/voice says\n" in js
    # The refetches: voice on, the sheet, visible again.
    assert "voiceLoadCfg();" in _fn(js, "voiceTurnOn")
    assert "voiceLoadCfg();" in _fn(js, "voiceSheetOpen")
    show = _fn(js, "voiceWordsShow")
    assert '.querySelectorAll(".voice-wake-word")' in show
    assert "voiceTutFill();" in show
    assert "if (was && voice.caption === was) voiceCaption(voiceDictHint());" in show
    # No matcher keeps a fixed radio word any more.
    assert "(over)(?:" not in js and "(disregard)(?:" not in js
    for gone in ("VOICE_STOP =", "VOICE_HOLD_STOP", "VOICE_HOLD_ALWAYS"):
        assert gone not in js, gone
    assert "var m = VOICE_W.dict.exec(t);" in _fn(js, "voiceDictParse")
    assert "VOICE_W.stop.test(t)" in _fn(js, "voiceTail")
    assert "VOICE_W.holdStop.concat(VOICE_W.holdAlways" in _fn(js, "voiceMayStart")
    assert "VOICE_MAY_CMD.concat(VOICE_W.mayCmd)" in _fn(js, "voiceMayBeCommand")
    # The wake word is named from the words everywhere the page says it.
    assert "“MindFlock”" not in "\n".join(
        ln for ln in js.splitlines() if not ln.lstrip().startswith("//")
    )
    html = _html()
    assert 'Listen for “<span class="voice-wake-word">MindFlock</span>”' in html


def test_how_to_talk_card_markup_and_wiring():
    html, js = _html(), _js()
    sheet = html.split('id="voice-sheet"')[1].split('id="voice-tut"')[0]
    # The ⚙ sheet's row, right under voice mode.
    assert (
        sheet.index('id="voice-on-toggle"')
        < sheet.index('id="voice-tut-open"')
        < sheet.index('id="voice-ptt"')
    )
    assert ">How to talk <span" in sheet
    card = html.split(
        '<div id="voice-tut" class="sheet hidden" role="dialog" aria-labelledby="voice-tut-title">'
    )[1].split("    </div>\n")[0]
    assert '<ul id="voice-tut-list" class="voice-tut-list"></ul>' in card
    assert '<button type="button" id="voice-tut-ok">Got it</button>' in card
    assert "Settings → Voice" in card
    # Per browser, its own key (cs_voice keeps its exact key set).
    assert 'var VOICE_TUT_STORE = "cs_voice_tutorial";' in js
    assert 'localStorage.setItem(VOICE_TUT_STORE, "1");' in _fn(js, "voiceTutDone")
    # The first turn-on opens it and its listen waits for Got it.
    on = _fn(js, "voiceTurnOn")
    assert on.index("if (voiceTutFirst(listenAfter)) return;") < on.index(
        'if (listenAfter) voiceListen("prompt", VOICE_TAP_MS, false, true);'
    )
    first = _fn(js, "voiceTutFirst")
    assert "voiceTutSeen() || voiceTut_.shown || (ask && ask.open)" in first
    assert 'voiceListen("prompt", VOICE_TAP_MS, false, true)' in first
    assert (
        'getElementById("voice-tut-open").addEventListener("click", function () { voiceTutOpen(null); });'
        in js
    )
    assert (
        'getElementById("voice-tut-ok").addEventListener("click", voiceTutDone);' in js
    )
    # Its copy is the owner's words, never a fixed one.
    lines = _fn(js, "voiceTutLines")
    for said in (
        'voiceSaid("send")',
        'voiceSaid("end")',
        'voiceSaid("clear")',
        'voiceSaid("hush")',
        "voiceWakeName()",
        "W.stop.slice(0, 2)",
    ):
        assert said in lines, said
    assert "“over”" not in lines and "“disregard”" not in lines
    for topic in (
        "session three",
        "next session",
        "“yes”",
        "Off, Mine",
        "Everything",
        "Hold the mic",
    ):
        assert topic in lines, topic
    # Text only: the words are the server's.
    fill = _fn(js, "voiceTutFill")
    assert (
        "textContent" in fill
        and 'innerHTML = ""' in fill
        and "innerHTML +=" not in fill
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_tutorial_lines_follow_the_words_under_node():
    js = _js()
    script = (
        "var voice = { prefs: { ptt: 'toggle' } };\n"
        + _matchers(js)
        + js[
            js.index("  function voiceTutLines(") : js.index("  function voiceTutFill(")
        ]
        + "var a = voiceTutLines(); voiceWordsApply(%s); var b = voiceTutLines();"
        % json.dumps(CUSTOM)
        + " voice.prefs.ptt = 'hold'; var c = voiceTutLines();"
        + " console.log(JSON.stringify([a, b, c].map(function (x) { return x.map(function (l) { return l.join(': '); }).join('\\n'); })));"
    )
    ours, theirs, hold = _run(script)
    assert "Say “over” at the end to send — listening stays on." in ours
    assert (
        "“over and out” sends and stops listening. “disregard” empties the box" in ours
    )
    assert "Say “stop” or “stop listening” to stop listening" in ours
    assert "“stop talking” cuts a spoken reply" in ours
    assert "Listen for “MindFlock”" in ours
    assert "Tap the mic and talk" in ours
    assert "Say “roger” at the end to send" in theirs
    assert (
        "“roger and out” sends and stops listening. “scrap that” empties the box"
        in theirs
    )
    assert "Say “halt” or “halt listening” to stop listening" in theirs
    assert "“silence” cuts a spoken reply" in theirs
    assert "Listen for “Hey Jarvis”" in theirs
    assert "“over" not in theirs and "disregard" not in theirs
    assert "Hold the mic while you speak" in hold


def test_docs_name_the_words_and_the_card():
    doc = DOCS.read_text()
    phone = doc[
        doc.index("cs_voice_tutorial") - 3000 : doc.index("cs_voice_tutorial") + 1500
    ]
    assert "Settings → Voice" in phone and "How to talk" in phone


# --- a word heard a little off: the shared fixture ----------------------------------
FIXTURE = ROOT / "tests" / "unit" / "data" / "voice_sounds.json"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_sound_fixture_reads_alike_under_node():
    f = json.loads(FIXTURE.read_text())
    script = _matchers(_js()) + "var F = %s;\n" % json.dumps(f) + """
// voiceHeard's order: the hush (the whole phrase), a stop phrase at the end, the radio words.
function kindOf(t) {
  var p = voiceDictParse(t);
  if (voiceIsHush(p.text || t)) return "hush";
  if (voiceTail(t, false)) return "stop";
  return { over: "send", out: "end", disregard: "clear" }[p.kind] || null;
}
var out = { keys: {}, alike: [], cases: [] };
Object.keys(F.keys).forEach(function (t) { out.keys[t] = voiceSoundKey(t); });
F.alike.forEach(function (a) { out.alike.push(voiceSoundsLike(a[0], a[1])); });
F.cases.forEach(function (c) { voiceWordsApply(c.words === "custom" ? F.custom : F.words); out.cases.push(kindOf(c.text)); });
voiceWordsApply(F.words);
out.heard = VOICE_W.words.heard;
out.hold = ["fix the bug this", "fix the bug this regard", "fix the bug this time", "ship it over an"].map(function (t) { return voiceHoldBack(t); });
out.guess = ["fix the bug ova", "ship it over an out", "scrap it this regard", "fix it stop listing", "be quite"].map(function (t) { var c = voiceGuessControl(t); return c ? c.word + "|" + c.send : null; });
voiceWordsApply(null);
out.none = [voiceDictParse("fix the bug ova").kind, voiceDictParse("fix it be quite").kind, voiceIsHush("be quite"), voiceIsHush("a nuff"), VOICE_W.words.heard.send.length];
console.log(JSON.stringify(out));
"""
    out = _run(script)
    assert out["keys"] == f["keys"]
    assert out["alike"] == [a[2] for a in f["alike"]]
    for c, got in zip(f["cases"], out["cases"]):
        assert got == c["kind"], c
    # The heard forms are the server's, bounded, never a phrase itself.
    assert out["heard"] == f["words"]["heard"]
    # A streamed guess holds back a heard form while it may still be said.
    assert out["hold"] == [
        ["fix the bug", "this"],
        ["fix the bug", "this regard"],
        ["fix the bug this time", ""],
        ["ship it", "over an"],
    ]
    # The fast control on a guess names the owner's word, not the form.
    assert out["guess"] == [
        "over|true",
        "over and out|true",
        "disregard|false",
        "stop|false",
        "stop talking|false",
    ]
    # Before GET /api/voice answers: the words alone (and what sounds like
    # them — "be quite" is "be quiet" either way), no forms.
    assert out["none"] == ["", "", True, False, 0]


def test_heard_forms_and_the_sound_key_are_wired():
    js = _js()
    apply = _fn(js, "voiceWordsApply")
    assert "H = w.heard" in apply and "voiceWordsWide(w.stop, H.stop)" in apply
    assert "voiceWordsAlt(w.end.concat(H.end)" in apply
    parse = _fn(js, "voiceDictParse")
    assert (
        "voiceSoundTail(t, W.end)" in parse
        and "voiceSoundTail(t, W.clear, VOICE_CLEAR_TAILS)" in parse
    )
    hush = _fn(js, "voiceIsHush")
    assert "voiceSoundsLike(t, p)" in hush and "VOICE_W.hush.test(t)" in hush
    # The stop tail stays word for word.
    assert "voiceSound" not in _fn(js, "voiceTail")
    # One rule table, the server's (core.voice_words._SOUND_RULES), in order.
    from backend.web.core import voice_words as vw

    rules = js.split("  var VOICE_SOUND_RULES = [")[1].split("  ];")[0]
    pairs = re.findall(r"\[/(.+?)/g, \"(.*?)\"\]", rules)
    assert pairs == [(p, r) for p, r in vw._SOUND_RULES]
    assert "var VOICE_HEARD_MAX = 24;" in js and vw.HEARD_MAX == 24
