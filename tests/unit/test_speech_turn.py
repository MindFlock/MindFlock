"""``core.speech_turn``: the end-of-turn verdict (which rule, which hold, how
``p`` rises with the quiet) table-driven, the person's own voice words, and
the stream chunk response's ``turn``. Nothing here loads a model.
"""

from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import speech, speech_stream
from backend.web.core import speech_turn as st

SHORT, Q, SENT, DEF, LONG = (
    st.SHORT_MS,
    st.QUESTION_MS,
    st.SENTENCE_MS,
    st.DEFAULT_MS,
    st.LONG_MS,
)

# (what the recogniser wrote so far, reason, hold)
TABLE = [
    # nothing yet
    ("", "empty", DEF),
    ("  ", "empty", DEF),
    ("...", "empty", DEF),
    # radio / stop / hush phrases (voice_words.control_kind)
    ("Over.", "control", SHORT),
    ("over", "control", SHORT),
    ("Ova.", "control", SHORT),
    ("Over and out.", "control", SHORT),
    ("Disregard.", "control", SHORT),
    ("Disregard that.", "control", SHORT),
    ("Fix the failing test, over.", "control", SHORT),
    ("Add a docstring to the new function, disregard.", "control", SHORT),
    ("Commit what you have so far, over and out.", "control", SHORT),
    ("Stop listening.", "control", SHORT),
    ("Mic off.", "control", SHORT),
    ("That's all.", "control", SHORT),
    ("Stop talking.", "control", SHORT),
    ("be quiet please", "control", SHORT),
    # whisper's filler after a finished radio phrase
    ("Show failures. Over. The", "control", SHORT),
    ("Fix it, disregard. And", "control", SHORT),
    ("Over. And", "connector", LONG),  # "over and out" may be on its way
    ("Fix it, over the", "connector", LONG),  # no full stop: dictation
    ("Fix the bug. And", "connector", LONG),  # no radio phrase before it
    # whole-phrase router commands without an argument
    ("Stop.", "command", SHORT),
    ("Repeat.", "command", SHORT),
    ("say that again", "command", SHORT),
    ("Never mind.", "command", SHORT),
    ("Next session.", "command", SHORT),
    ("What needs me?", "command", SHORT),
    ("Switch to session three.", "command", SHORT),
    ("Voice off.", "command", SHORT),
    # a radio word's first words: "and out" may follow
    ("Over and", "connector", LONG),
    # the wake word alone: the question comes next
    ("Mindflock,", "wake", LONG),
    ("Mindflock", "wake", LONG),
    ("Mind flock.", "wake", LONG),
    ("Mind clock", "wake", LONG),
    ("Hey Mindflock.", "wake", LONG),
    # trails off
    ("Fix the failing test,", "comma", LONG),
    ("Let me think -", "comma", LONG),
    ("so the thing is...", "comma", LONG),
    ("the plan is:", "comma", LONG),
    ("Run the tests and", "connector", LONG),
    ("Rename the helper to", "connector", LONG),
    ("Switch to", "connector", LONG),
    ("Open the", "connector", LONG),
    ("Fix the.", "connector", LONG),
    ("Show me the ticket for", "connector", LONG),
    ("I think I", "connector", LONG),
    ("Check the inbox, um", "connector", LONG),
    ("It should", "connector", LONG),
    ("So", "connector", LONG),
    # a question mark wins over a connector
    ("What is it for?", "question", Q),
    ("Mindflock, how do fences work?", "question", Q),
    ("Is the pull request merged?", "question", Q),
    ("Ship it!", "question", Q),
    # a sentence
    ("The diff looks wrong.", "sentence", SENT),
    ("Run the tests again.", "sentence", SENT),
    # anything else: today's hold
    ("Fix it.", "default", DEF),
    ("OK.", "default", DEF),
    ("Thank you.", "default", DEF),
    ("Switch to sitecheck bot seven", "default", DEF),
    ("Fix the failing test", "default", DEF),
    ("Go?", "default", DEF),
    ("revert that", "default", DEF),
    ("Mindful", "default", DEF),
]


@pytest.mark.parametrize("text,reason,hold", TABLE)
def test_the_rules(text, reason, hold):
    got = st.score(text)
    assert (got.reason, got.hold_ms) == (reason, hold)


def test_every_reason_is_in_the_table():
    assert {r for _t, r, _h in TABLE} | {"unheard"} == set(st._HOLD)


def test_dictation_ending_in_over_is_never_a_lone_command():
    """A plain "over" inside dictation is not a radio word: no short hold."""
    assert st.score("Move it over the bridge").reason == "default"
    assert st.score("this regard is important").hold_ms == DEF


class TestQuiet:
    def test_p_starts_at_the_prior_and_reaches_one_at_the_hold(self):
        start = st.score("The diff looks wrong.", 0)
        half = st.score("The diff looks wrong.", SENT / 2)
        full = st.score("The diff looks wrong.", SENT)
        more = st.score("The diff looks wrong.", SENT * 3)
        assert start.p == st._PRIOR["sentence"]
        assert start.p < half.p < full.p == more.p == 1.0
        assert {start.hold_ms, half.hold_ms, full.hold_ms} == {SENT}

    def test_trailing_off_starts_low(self):
        assert st.score("and the", 0).p < st.score("Fix it.", 0).p
        assert st.score("Fix it.", 0).p < st.score("Over.", 0).p

    def test_negative_quiet_is_none(self):
        assert st.score("Fix it.", -500).p == st._PRIOR["default"]

    def test_nothing_heard_is_zero_whatever_the_quiet(self):
        assert st.score("", 5000) == st.Turn(0.0, DEF, "empty")


class TestUnheard:
    @pytest.mark.parametrize("text", ["Over.", "Repeat.", "The diff looks wrong."])
    def test_words_behind_the_voice_get_the_default_hold(self, text):
        got = st.score(text, 400, heard=False)
        assert (got.reason, got.hold_ms) == ("unheard", DEF)

    def test_unheard_and_empty_is_still_empty(self):
        assert st.score("", 0, heard=False).reason == "empty"


class TestOwnWords:
    def test_the_persons_send_word_is_a_control(self):
        words = {"send": ["roger"]}
        assert st.score("Fix it, roger.", words=words).reason == "control"
        # "over" is no longer theirs: a plain sentence.
        assert st.score("Fix it, over.", words=words).reason == "sentence"
        assert st.score("Fix it over", words=words).reason == "default"

    def test_the_persons_wake_word(self):
        words = {"wake": "computer"}
        assert st.score("Computer,", words=words).reason == "wake"
        assert st.score("Mindflock,", words=words).reason == "comma"

    def test_effective_words_read_the_same(self):
        from backend.web.core import voice_words

        eff = voice_words.effective({"send": ["roger"]})
        assert st.score("roger", words=eff).reason == "control"

    def test_settings_object(self):
        settings = SimpleNamespace(voice=SimpleNamespace(words={"clear": ["scrap"]}))
        assert st.score("Do it, scrap that.", words=settings).reason == "control"


def test_a_router_failure_is_no_command(monkeypatch):
    from backend.web.core import voice_router

    def boom(*_a, **_k):
        raise RuntimeError("router broke")

    monkeypatch.setattr(voice_router, "command", boom)
    assert st.score("Repeat.").reason == "default"


def test_json_shape():
    assert st.score("Over.", SHORT).json() == {
        "p": 1.0,
        "hold_ms": SHORT,
        "reason": "control",
    }
    assert st.hold_for("and") == LONG


def test_holds_are_ordered():
    assert SHORT < Q <= SENT < DEF < LONG
    # Not under 900: whisper puts a "." at every pause (the module doc).
    assert min(Q, SENT) >= 900


# --- the chunk response ------------------------------------------------------
client = TestClient(server.app)


class _Fake(speech.Backend):
    id, kind, label, streams = "fake", "stt", "fake", True

    def __init__(self):
        self.passes: list = []

    def ready(self):
        return True, "fake is ready"

    def stream_transcriber(self, lang="", hints=""):
        def run(audio, prompt):
            return self.passes.pop(0)

        return run

    def final_transcriber(self, lang="", hints=""):
        return None


@pytest.fixture
def fake(isolate_settings_store, monkeypatch):
    stt = _Fake()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [stt], "tts": []})
    speech_stream.clear()
    yield stt
    speech_stream.clear()


def _wav(seconds=0.3, sample=b"\x00\x10"):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(sample * int(seconds * 16000))
    return buf.getvalue()


def _chunk(sid, body):
    return client.post(
        "/api/voice/stream/%s/chunk" % sid,
        content=body,
        headers={"Content-Type": "audio/wav"},
    )


def _words(text, step=0.05):
    return [(w, i * step, i * step + 0.04) for i, w in enumerate(text.split())]


class TestChunkResponse:
    def test_turn_rides_the_chunk_response(self, fake):
        sid = client.post("/api/voice/stream", json={"lang": "en-US"}).json()["id"]
        fake.passes += [_words("Fix the failing test"), _words("Fix the failing test,")]
        first = _chunk(sid, _wav()).json()
        # Additive: every key a client reads today is still there.
        assert {"committed", "partial", "ms", "window_s", "audio_s"} <= set(first)
        assert first["turn"] == {"p": 0.5, "hold_ms": DEF, "reason": "default"}
        second = _chunk(sid, _wav()).json()
        assert second["turn"]["reason"] == "comma"
        assert second["turn"]["hold_ms"] == LONG
        # Quiet after a heard tail: the same words, the quiet counted.
        fake.passes += [_words("Fix the failing test, over.")]
        third = _chunk(sid, _wav()).json()
        assert third["turn"]["reason"] == "control"
        fake.passes += [_words("Fix the failing test, over.")]
        quiet = _chunk(sid, _wav(0.3, b"\x00\x00")).json()
        assert quiet["turn"]["reason"] == "control"
        assert quiet["turn"]["hold_ms"] == SHORT
        assert quiet["turn"]["p"] > third["turn"]["p"] - 0.01

    def test_the_persons_words_are_read(self, fake, monkeypatch):
        from backend.web.routes import speech as routes
        from backend.web.core import voice_words

        monkeypatch.setattr(
            routes, "_words", lambda: voice_words.effective({"send": ["roger"]})
        )
        sid = client.post("/api/voice/stream", json={}).json()["id"]
        fake.passes += [_words("Fix it roger")]
        assert _chunk(sid, _wav()).json()["turn"]["reason"] == "control"

    def test_a_failing_verdict_leaves_the_chunk_answered(self, fake, monkeypatch):
        def boom(self, words=None):
            raise RuntimeError("verdict broke")

        monkeypatch.setattr(speech_stream.Stream, "turn", boom)
        sid = client.post("/api/voice/stream", json={}).json()["id"]
        fake.passes += [_words("hello there")]
        r = _chunk(sid, _wav())
        assert r.status_code == 200
        assert r.json()["partial"] == "hello there" and "turn" not in r.json()
