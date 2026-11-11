"""A conversational stop phrase stops dictation only said on its own.

The owner, 2026-11-10: the desktop trace heard "that Because it's bad
practice, so make sure that that's all." and took the closing "that's all"
for the stop phrase — listening ended mid-message. The stop family splits:

- "stop listening", "mic off", "pause listening", "stop recording" (and the
  person's own phrases of two words or more) end ANY phrase;
- "that's all", "done talking" (``voice_words.STOP_ALONE``, with their kin
  and heard forms) stop only as the WHOLE phrase — a filler before it
  allowed ("Okay, that's all.") or an end phrase ("over and out, that's
  all" sends, then stops); anywhere else they are words.

The server (``voice_words.control_kind``, ``voice_router.command``), the
desktop (lib/voiceWords ``STOP_ALONE``, lib/voiceDesktop ``stopTail``:
voiceStopAlone.test.ts) and the phone (mobile.js ``VOICE_STOP_ALONE``,
``voiceTail``) keep one list and read alike.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.web.core import voice_router as vr
from backend.web.core import voice_words as vw

ROOT = Path(__file__).resolve().parents[2]
MOBILE = ROOT / "backend" / "web" / "static" / "mobile.js"
WORDS_TS = ROOT / "frontend" / "src" / "lib" / "voiceWords.ts"

#: The owner's phrase, exactly as the trace logged it.
OWNER = "that Because it's bad practice, so make sure that that's all."


# --- the server's reading ------------------------------------------------------------
@pytest.mark.parametrize(
    "text,kind",
    [
        (OWNER, None),
        ("fix it, that's all", None),
        ("fix it that is all", None),
        ("and then I'm done talking", None),
        ("fix it done talkin", None),
        ("That's all.", "stop"),
        ("that is all", "stop"),
        ("Okay, that's all.", "stop"),
        ("um, I'm done talking.", "stop"),
        ("fix it, stop listening", "stop"),
        ("fix it, mic off", "stop"),
        ("fix it pause listening", "stop"),
        ("Fix it, stop recording.", "stop"),
        ("over and out, that's all", "stop"),
        ("ship it over and out, that's all", "stop"),
        ("fix it stop", None),  # one word: the router's, said on its own
    ],
)
def test_control_kind(text, kind):
    assert vw.control_kind(text) == kind


def test_stop_alone_classifies_the_forms():
    for p in (
        "that's all",
        "Thats all",
        "that is all",
        "I'm done talking",
        "then talking",
        "halt",
    ):
        assert vw.stop_alone(p), p
    for p in (
        "stop listening",
        "stop recording",
        "mic off",
        "pause listening",
        "we're done",
    ):
        assert not vw.stop_alone(p), p


def test_the_persons_own_phrases_of_two_words_end_any_phrase():
    words = {"stop": ["halt", "we're done", "that's all"]}
    assert vw.control_kind("fix it, we're done", words) == "stop"
    assert vw.control_kind("fix it halt", words) is None
    assert vw.control_kind("fix it, that's all", words) is None
    assert vw.control_kind("that's all", words) == "stop"


def test_the_router_reads_it_whole_a_filler_allowed():
    assert vr.command("that's all") == {"kind": "stop", "arg": ""}
    assert vr.command("Um, that's all.") == {"kind": "stop", "arg": ""}
    assert vr.command("okay, I'm done talking") == {"kind": "stop", "arg": ""}
    assert vr.command(OWNER) is None
    assert vr.command("uh, fix the login bug") is None


# --- one list in three places ----------------------------------------------------
def _list(src: str, start: str) -> list:
    body = src.split(start, 1)[1].split("];", 1)[0]
    return re.findall(r'"([^"]+)"', body)


def _keys(forms) -> set:
    """The forms of two words or more, as matcher words (a one-word form is
    whole-only by its length alone: "stop", "top", "halt")."""
    return {k for k in (" ".join(vw._words(f)) for f in forms) if " " in k}


def test_the_clients_keep_the_servers_list():
    server = _keys(vw._STOP_ALONE_FORMS)
    assert "that is all" in server and "then talking" in server
    ts = WORDS_TS.read_text()
    js = MOBILE.read_text()
    assert _keys(_list(ts, "export const STOP_ALONE: readonly string[] = [")) == server
    assert _keys(_list(js, "var VOICE_STOP_ALONE = [")) == server
    fillers = list(vw.STOP_FILLERS)
    assert _list(ts, "export const STOP_FILLERS: readonly string[] = [") == fillers
    assert _list(js, "var VOICE_STOP_FILLERS = [") == fillers


# --- the phone page, under node ---------------------------------------------------
def _matchers(js: str) -> str:
    """The words block and the matchers (test_mobile_voice_words._matchers)."""
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
        + js[js.index("  function voiceUndoParse(") : js.index("  function voiceUndo(")]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_phone_reads_alike_under_node():
    cases = [
        [OWNER, False],
        ["fix it, that's all", False],
        ["fix it done talkin", False],
        ["That's all.", False],
        ["Okay, that's all.", False],
        ["um I'm done talking", False],
        ["fix it, stop listening", False],
        ["over and out, that's all", False],
        # A stream's committed words so far: the phrase may go on.
        ["that's all", True],
        ["fix it, stop listening", True],
    ]
    script = (
        _matchers(MOBILE.read_text())
        + "var C = %s;\n" % json.dumps(cases)
        + "console.log(JSON.stringify(C.map(function (c) {"
        " var t = voiceTail(c[0], false, c[1]); return t ? t.kind + '|' + t.text : null; })));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [
        None,
        None,
        None,
        "stop|",
        "stop|",
        "stop|",
        "stop|fix it",
        # voiceStopPhrase sends the "over and out", then stops.
        "stop|over and out",
        None,
        "stop|fix it",
    ]


def test_the_phone_stream_waits_for_the_phrase_end():
    js = MOBILE.read_text()
    words = js.split("    function streamWords(", 1)[1].split("\n    }\n", 1)[0]
    assert "voiceTail(g.pend, false, true)" in words
