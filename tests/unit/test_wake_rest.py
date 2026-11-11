"""The wake word wherever it is judged (voice-command polish round,
2026-11-10).

A. Said at the START of a phrase while listening, misheard: the owner's
   trace typed "Mindful, I'll open settings.", "mindfulout open intake.",
   "Mindful luck. Tell me what's going on with," and "Line flock." as
   words. ``core.voice_words.wake_at`` now takes the family the standby
   judge takes (``wake_like``) before a request (``wake_asks``) or alone,
   and never an ordinary sentence ("mindful of the deadline"). The desktop
   (lib/voiceWords ``wakeAt``) and the phone (mobile.js ``voiceWakeAt``)
   run the same cases: tests/unit/data/voice_sounds.json ``wake``.
B. Said from standby WITH a request in one breath ("MindFlock, next
   session"): ``core.wake.judge_rest`` → ``POST /api/voice/wake`` answers
   ``{"wake": true, "rest": "next session", …}``; the plain pass checks
   it (``veto_rest``).
C. ``speech_moonshine.control_only``: a switch to a NAME waits for
   whisper's words; the instant path stays for argument-free commands; a
   heard wake form in front is dropped.
D. The desktop's built-in words and kin are the server's.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import speech
from backend.web.core import speech_moonshine as sm
from backend.web.core import voice_router as vr
from backend.web.core import voice_words as vw
from backend.web.core import wake
from backend.web.routes import speech as routes

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (ROOT / "tests" / "unit" / "data" / "voice_sounds.json").read_text()
)
W = FIXTURE["wake"]


# --- A: the shared tables and cases -------------------------------------------------
def test_the_fixture_pins_the_servers_tables():
    assert W["leads"] == list(vw.WAKE_LEADS)
    assert W["plain"] == list(vw.WAKE_PLAIN)
    assert W["sound"] == vw.WAKE_SOUND
    assert W["tails"] == list(vw.WAKE_TAILS)
    assert W["near"] == vw.WAKE_NEAR
    assert W["mine"] == list(vw.WAKE_MINE)
    assert W["asks"] == list(vw.WAKE_ASKS)
    assert W["ask_fillers"] == list(vw.WAKE_ASK_FILLERS)
    assert W["ask_you"] == list(vw.WAKE_ASK_YOU)
    assert FIXTURE["kin"] == {
        k: {p: list(v) for p, v in by.items()} for k, by in vw.KIN.items()
    }


@pytest.mark.parametrize("words, reason", W["like"])
def test_wake_like(words, reason):
    assert vw.wake_like(words) == reason


@pytest.mark.parametrize("text, rest", W["at"])
def test_wake_at_and_rest(text, rest):
    assert vw.wake_rest(text) == rest
    assert (vw.wake_at(text) is None) is (rest is None)


def test_the_standby_judge_takes_the_same_family():
    """``core.wake``'s strict rule for ours is ``voice_words.wake_like``:
    one table, so a form the standby wakes on is one a request starts with."""
    for words, reason in W["like"]:
        assert wake._strict(list(words)) == reason, words
    assert wake.judge("Mindful luck.") == (True, "sound")
    assert wake.judge("Line flock.") == (True, "near")


@pytest.mark.parametrize(
    "text",
    [
        "Mine flock, open intake.",
        "My flock, open settings.",
        "Mindful, I'll open settings.",
        "mindfulout open intake.",
        "Mindful luck. Tell me what's going on with,",
        "line flock open settings",
    ],
)
def test_the_router_takes_the_trace_phrases_through_voice_words(text):
    """voice_router.command reads ``voice_words.wake_at``: the trace phrases
    reach the assistant with no router change."""
    got = vr.command(text)
    assert got is not None and got["kind"] == "assistant", got
    assert got["text"] == vw.wake_rest(text).rstrip(",")


@pytest.mark.parametrize("text", [t for t, r in W["at"] if r is None and t])
def test_the_router_never_addresses_an_ordinary_sentence(text):
    got = vr.command(text)
    assert got is None or got["kind"] != "assistant", got


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_phone_reads_the_wake_word_alike_under_node():
    from tests.unit.test_mobile_voice_words import _js, _matchers

    script = _matchers(_js()) + "var F = %s;\n" % json.dumps(FIXTURE) + """
voiceWordsApply(F.words);
var out = {
  tables: {leads: VOICE_WAKE_LEADS, plain: VOICE_WAKE_PLAIN, sound: VOICE_WAKE_SOUND, tails: VOICE_WAKE_TAILS,
           near: VOICE_WAKE_NEAR, mine: VOICE_WAKE_MINE, asks: VOICE_WAKE_ASKS, ask_fillers: VOICE_WAKE_ASK_FILLERS, ask_you: VOICE_WAKE_ASK_YOU},
  like: F.wake.like.map(function (c) { return voiceWakeLike(c[0]); }),
  at: F.wake.at.map(function (c) { return voiceWakeRest(c[0]); }),
  idx: F.wake.at.map(function (c) { return voiceWakeAt(c[0]); }),
};
console.log(JSON.stringify(out));
"""
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    for k, v in out["tables"].items():
        assert v == W[k], k
    assert out["like"] == [r for _, r in W["like"]]
    for (text, rest), got, idx in zip(W["at"], out["at"], out["idx"]):
        assert got == rest, text
        assert idx == vw.wake_at(text), text


# --- B: the word and a request in one breath, from standby --------------------------
@pytest.mark.parametrize(
    "text, rest",
    [
        ("MindFlock, next session.", "next session."),
        ("MindFlock, open settings.", "open settings."),
        ("Hey MindFlock, switch to docs.", "switch to docs."),
        ("Mind flock, what needs me?", "what needs me?"),
        ("My flock, open intake.", "open intake."),
        ("Mindful, I'll open settings", "open settings"),
        ("Mindful, I've opened settings.", "open settings."),
        ("MindFlock, session three", "session three"),
        ("MindFlock, can you scroll up", "can you scroll up"),
    ],
)
def test_judge_rest_takes_a_request(text, rest):
    assert wake.judge_rest(text) == (True, "rest", rest)


@pytest.mark.parametrize(
    "text",
    [
        "MindFlock is great",  # about it
        "My clock is broken.",  # an ordinary sentence starting "my …"
        "Mindful of the time.",
        "Mind the gap please",
        "MindFlock, I think the build is broken",  # not a request it knows
        "MindFlock, approve it",  # never an answer from standby
        "MindFlock, send it",
        "MindFlock, open the settings for sitecheck bot seven and read it back",  # > 8 words
        "Find out what is wrong",
    ],
)
def test_judge_rest_never_wakes_on_a_sentence(text):
    got = wake.judge_rest(text)
    assert got[0] is False and got[2] == "", got


def test_judge_rest_keeps_the_lone_word_rules():
    assert wake.judge_rest("MindFlock.") == (True, "word", "")
    assert wake.judge_rest("Hey, MindFlock.") == (True, "word", "")
    assert wake.judge_rest("") == (False, "empty", "")
    # "Mindful, I'll" — a quick "MindFlock" and its tail, alone — wakes.
    assert wake.judge_rest("Mindful, I'll.") == (True, "sound", "")
    # A person's own word with a request after it.
    assert wake.judge_rest("Jarvis, open intake", "jarvis") == (
        True,
        "rest",
        "open intake",
    )
    assert wake.judge_rest("Jarvis is here", "jarvis")[0] is False


@pytest.mark.parametrize(
    "plain, rest, keep",
    [
        ("Mind flock, open intake.", "open intake.", True),  # the word, plainly
        ("Mindful, open intake.", "open intake.", True),  # the plain model's MindFlock
        ("Line flock open intake", "open intake", True),
        ("Find out, open intake", "open intake", True),  # same rest, an odd lead
        ("Mind, open settings.", "open settings.", False),  # plainly "mind"
        ("Mine, next session.", "next session.", False),
        ("open settings.", "open settings.", False),  # the hint put MindFlock in front
        ("What do you think?", "what do you think", False),
        ("Something else entirely.", "open intake", False),
    ],
)
def test_veto_rest(plain, rest, keep):
    assert (wake.veto_rest(plain, rest) == "") is keep


def test_a_four_second_clip_fits_the_decode():
    """The clients send up to 4 s now: the decode's token cap holds a
    request of :data:`wake.REST_MAX` words and the encoder pad the clip."""
    assert wake.MAX_TOKENS >= 24 and wake.REST_MAX == 8
    assert wake.PAD_SECONDS >= 4.0
    assert wake.decode_options("MindFlock")["max_new_tokens"] == wake.MAX_TOKENS


class _Fake(speech.Backend):
    def __init__(self):
        self.id, self.kind, self.label = "fake", "stt", "fake"
        self.text = ""
        self.plain: object = None

    def ready(self):
        return True, "fake is ready"

    def transcribe(self, audio, mime, lang="", prompt=""):
        return self.text

    def speak(self, text, voice=""):  # pragma: no cover - not used
        return b"", "audio/mpeg"

    def wake_verify_transcribe(self, audio, mime, lang=""):
        return self.plain


client = TestClient(server.app)


@pytest.fixture
def stt(isolate_settings_store, monkeypatch):
    fake = _Fake()
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [fake], "tts": []})
    routes._WAKE_RECENT.clear()
    yield fake
    routes._WAKE_RECENT.clear()


def _wake(fake, prompted, plain=None):
    fake.text, fake.plain = prompted, plain
    r = client.post(
        "/api/voice/wake?lang=en",
        content=b"RIFFclip",
        headers={"Content-Type": "audio/wav"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_the_route_answers_the_rest(stt):
    body = _wake(stt, "MindFlock, next session.", "Mind flock, next session.")
    assert body["wake"] is True and body["reason"] == "rest"
    assert body["rest"] == "next session."
    rows = client.get("/api/voice/wake/recent").json()["attempts"]
    assert rows[0]["rest"] == "next session." and rows[0]["wake"] is True


def test_the_route_rest_is_empty_for_the_word_alone_and_a_no(stt):
    body = _wake(stt, "MindFlock.", "Mind flock.")
    assert body["wake"] is True and body["rest"] == ""
    body = _wake(stt, "My clock is broken.")
    assert body["wake"] is False and body["rest"] == ""
    rows = client.get("/api/voice/wake/recent").json()["attempts"]
    assert [r["rest"] for r in rows] == ["", ""]


def test_the_plain_pass_vetoes_a_rest(stt):
    body = _wake(stt, "MindFlock, open settings.", "Mind, open settings.")
    assert body["wake"] is False and body["rest"] == ""
    assert body["reason"].startswith("plain:")


def test_a_custom_word_with_a_request(stt):
    S.update_settings(voice={"words": {"wake": "jarvis"}})
    S.invalidate()
    body = _wake(stt, "Jarvis, open intake.")
    assert body["wake"] is True and body["rest"] == "open intake."


# --- C: Moonshine's instant path and names --------------------------------------------
@pytest.mark.parametrize(
    "heard, only",
    [
        # A switch / interrupt / status to a NAME: whisper's words decide it.
        ("Switch to sitecheck bot seven.", False),
        ("Go to docs.", False),
        ("Interrupt docs.", False),
        ("How is docs doing?", False),
        ("What is docs asking?", False),
        # No argument: instant, as before.
        ("Next session.", True),
        ("Session three.", True),
        ("Previous one.", True),
        ("Interrupt.", True),
        ("Status.", True),
        ("Where am I?", True),
        ("Go back.", True),
        ("Stop.", True),
        # A heard wake form in front is dropped first.
        ("Mind flock, stop.", True),
        ("Mindful, scroll up.", True),
        ("Hey MindFlock, next session.", True),
        ("My flock, switch to docs.", False),
    ],
)
def test_control_only_waits_for_whisper_on_a_name(heard, only):
    assert sm.control_only(heard) is only


def test_unwake_drops_the_heard_forms():
    for said in (
        "Mindflock, stop.",
        "Mind flock, stop.",
        "Mindful, stop.",
        "Hey MindFlock, stop.",
    ):
        assert sm._unwake(said.split(), None) == ["stop."], said
    assert sm._unwake("mindful of the time".split(), None) == ["of", "the", "time"]
    assert sm._unwake("fix the bug".split(), None) == ["fix", "the", "bug"]


# --- D: the desktop's built-in words are the server's -------------------------------
def test_the_desktop_defaults_are_the_servers():
    """lib/voiceWords BUILTIN_VOICE_WORDS and PHRASE_KIN, as written, against
    DEFAULTS and KIN (voiceWakeForms.test.ts pins them against the fixture
    too)."""
    ts = (ROOT / "frontend" / "src" / "lib" / "voiceWords.ts").read_text()
    block = ts.split("export const BUILTIN_VOICE_WORDS: VoiceWords = {")[1].split("};")[
        0
    ]
    for kind in vw.KINDS:
        line = next(l for l in block.splitlines() if l.strip().startswith(kind + ":"))
        assert (
            json.loads(line.split(":", 1)[1].strip().rstrip(",")) == vw.DEFAULTS[kind]
        ), kind
    assert 'wake: "mindflock"' in block
