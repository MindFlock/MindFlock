"""The accurate pass held back by memory: "over" still has its words.

Owner report 2026-11-10: with Windows memory critically low, every phrase's
whisper pass was refused ("local recognition held back: …") and the log
said so on every phrase. ``/end`` must still answer the fast text with its
control phrase in it — Moonshine's own text as the ``final`` (what the
client reads "over" / "over and out" off), and the words committed — and
the hold-back is logged once per episode, not once per phrase.
(The client half — the send itself — is pinned in
frontend/src/__tests__/voiceHeldBack.test.ts.)
"""

from __future__ import annotations

import logging

import pytest

from backend.web.core import speech
from backend.web.core import speech_stream as ss
from tests.unit.test_speech_moonshine import loud, make, quiet

HELD = "local recognition held back: Windows memory is critically low: 1.2 GB free"


class Held:
    """An accurate pass refused for memory, as ``LocalSTT._model`` refuses it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, audio):
        self.calls += 1
        raise speech.SpeechError(HELD)


class Ok:
    def __init__(self, text: str):
        self.text = text

    def __call__(self, audio):
        return self.text


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    ss.clear()
    yield
    ss.clear()


def _phrase(script, accurate, final=None) -> dict:
    """A Moonshine phrase: 1.5 s of speech then a quiet tail, ended."""
    st, _t = make(script, final=final, accurate=accurate)
    for chunk in (loud(1.0), loud(0.5), quiet(0.6)):
        st.feed(chunk)
    return st.finish()


@pytest.mark.parametrize(
    "script, said",
    [
        ([[(1, "Ship it")], [(1, "Ship it over")]], "Ship it over"),
        ([[(1, "Ship it")], [(1, "Ship it, over.")]], "Ship it, over."),
        (
            [[(1, "Ship it.", True)], [(1, "Ship it.", True), (2, "Over.")]],
            "Ship it. Over.",
        ),
        (
            [[(1, "Ship it over and")], [(1, "Ship it over and out.")]],
            "Ship it over and out.",
        ),
    ],
)
def test_end_answers_the_fast_text_with_its_control_phrase(script, said):
    acc = Held()
    out = _phrase(script, acc)
    assert acc.calls >= 1  # the pass was asked for, and refused
    assert out["final"] == said and out["heard"] == said
    assert out["final_by"] == "moonshine"
    # Every word is committed between the chunks and /end: the client's
    # fast words end in the control phrase too.
    assert out["same"] is True


def test_the_hold_back_is_logged_once_per_episode(caplog):
    script = [[(1, "Ship it")], [(1, "Ship it over")]]
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        for _ in range(4):
            _phrase(script, Held())
    held = [r for r in caplog.records if "held back" in r.getMessage()]
    assert len(held) == 1
    assert "logged once until a pass runs again" in held[0].getMessage()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        out = _phrase(script, Ok("Ship it."))
        _phrase(script, Ok("Ship it."))
    assert out["final_by"] == "whisper" and out["final"] == "Ship it over"
    again = [r.getMessage() for r in caplog.records]
    assert again == [
        "voice stream: the accurate pass runs again (it was held back on 4"
        " phrases; their fast text stood)"
    ]
    # A new episode is logged again.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        _phrase(script, Held())
    assert sum("held back" in r.getMessage() for r in caplog.records) == 1


def test_any_other_failure_is_logged_every_time(caplog):
    class Broke:
        def __call__(self, audio):
            raise speech.SpeechError("whisper broke")

    script = [[(1, "Ship it")], [(1, "Ship it over")]]
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        for _ in range(3):
            assert _phrase(script, Broke())["final"] == "Ship it over"
    assert caplog.text.count("the accurate pass failed (whisper broke)") == 3
