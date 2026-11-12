"""``core.speech_moonshine``: the Moonshine streaming recognizer behind the
same interface as ``speech_stream.Stream`` — with the engine faked (a
scripted transcriber: no package, no model in CI). Interface parity, the
commit policy, keyterms, the wake-word join, the short/long final policy,
the fallback when unavailable, the thread cap and the shared registry.
"""

from __future__ import annotations

import inspect
import os
import time
import types
from array import array

import pytest

from backend.web.core import speech
from backend.web.core import speech_moonshine as sm
from backend.web.core import speech_stream as ss

RATE = ss.RATE


def loud(seconds: float = 0.3) -> array:
    return array("f", [0.3] * int(seconds * RATE))


def quiet(seconds: float = 0.3) -> array:
    return array("f", [0.0] * int(seconds * RATE))


class Line:
    def __init__(self, line_id: int, text: str, complete: bool = False):
        self.line_id = line_id
        self.text = text
        self.is_complete = complete


class Transcript:
    def __init__(self, lines):
        self.lines = [Line(*line) for line in lines]


class FakeLive:
    """A scripted engine stream: each ``update_transcription`` answers the
    next scripted transcript (the last one repeats); ``stop`` answers
    ``final`` (default: the last one, every line complete)."""

    def __init__(self, owner):
        self.owner = owner
        self.audio = 0
        self.updates = 0
        self.started = self.stopped = self.closed = False

    def start(self):
        self.started = True

    def add_audio(self, data, rate):
        assert rate == RATE
        if self.owner.fail:
            raise RuntimeError("engine broke")
        self.audio += len(data)

    def update_transcription(self, flags=0):
        if self.owner.fail:
            raise RuntimeError("engine broke")
        self.updates += 1
        script = self.owner.script
        got = script[min(self.updates - 1, len(script) - 1)] if script else []
        return Transcript(got)

    def stop(self):
        self.stopped = True
        if self.owner.final is not None:
            return Transcript(self.owner.final)
        last = self.owner.script[-1] if self.owner.script else []
        return Transcript([(i, t, True) for i, t, *_ in last])

    def close(self):
        self.closed = True


class FakeTranscriber:
    def __init__(self, script=None, final=None):
        self.script = list(script or [])
        self.final = final
        self.fail = False
        self.keyterms_calls: list = []
        self.lives: list = []

    def set_keyterms(self, terms):
        self.keyterms_calls.append(terms)

    def create_stream(self, update_interval=None, flags=0):
        assert update_interval and update_interval > 1000  # we drive the passes
        live = FakeLive(self)
        self.lives.append(live)
        return live


def make(script=None, final=None, accurate=None, hints=""):
    t = FakeTranscriber(script, final)
    eng = sm.MoonshineEngine(t, threads=1)
    return sm.MoonshineStream(eng, accurate, hints), t


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    ss.clear()
    speech.unload_all()
    yield
    ss.clear()
    speech.unload_all()


# --------------------------------------------------------------------------- #
# The interface is speech_stream.Stream's
# --------------------------------------------------------------------------- #
class TestInterfaceParity:
    def test_every_public_member_of_stream_is_there(self):
        st, _t = make()
        assert isinstance(st, ss.Stream)
        public = [n for n in dir(ss.Stream) if not n.startswith("_")]
        assert public  # feed, finish, turn, text, busy, …
        for name in public:
            assert hasattr(st, name), name
        for name in ("last_ms", "last_window_s", "touched", "client", "words"):
            assert hasattr(st, name), name

    def test_feed_and_finish_signatures_match(self):
        for name in ("feed", "finish", "turn"):
            ours = inspect.signature(getattr(sm.MoonshineStream, name))
            theirs = inspect.signature(getattr(ss.Stream, name))
            assert list(ours.parameters) == list(theirs.parameters), name

    def test_result_shapes(self):
        st, _t = make([[(1, "Fix the")], [(1, "Fix the failing test")]])
        got = st.feed(loud())
        assert isinstance(got, tuple) and len(got) == 2
        assert all(isinstance(x, str) for x in got)
        assert isinstance(st.turn(), dict) and "hold_ms" in st.turn()
        out = st.finish()
        # Every key a whisper Stream's finish has, when it heard words.
        for key in (
            "committed",
            "partial",
            "final",
            "final_ms",
            "final_wait_ms",
            "final_early",
            "same",
        ):
            assert key in out, key
        assert out["partial"] == "" and out["final_by"] == "moonshine"
        assert isinstance(out["final_ms"], int) and isinstance(out["same"], bool)

    def test_nothing_heard_has_no_final(self):
        st, _t = make([[]])
        st.feed(loud())
        assert st.finish() == {"committed": "", "partial": ""}


# --------------------------------------------------------------------------- #
# How words are committed
# --------------------------------------------------------------------------- #
class TestCommit:
    def test_agreement_of_two_different_hypotheses(self):
        st, _t = make(
            [
                [(1, "Nor is it.")],
                [(1, "Nor is it.")],  # the same answer again: not agreement
                [(1, "Nor is Mr. Crowell.")],
                [(1, "Nor is Mr. Quilter's manor,")],
            ]
        )
        assert st.feed(loud()) == ("", "Nor is it.")
        assert st.feed(loud()) == ("", "Nor is it.")
        assert st.feed(loud()) == ("Nor is", "Mr. Crowell.")
        assert st.feed(loud()) == ("Mr.", "Quilter's manor,")
        assert st.text == "Nor is Mr."

    def test_a_completed_line_is_committed_whole(self):
        st, _t = make(
            [
                [(1, "Over and")],
                [(1, "Over and out.", True)],
                [(1, "Over and out.", True), (2, "Stop")],
            ]
        )
        assert st.feed(loud()) == ("", "Over and")
        assert st.feed(loud()) == ("Over and out.", "")
        assert st.feed(loud()) == ("", "Stop")
        out = st.finish()
        assert out["committed"] == "Stop"
        assert out["final"] == "Over and out. Stop" and out["same"] is True

    def test_a_completed_line_continues_after_what_was_typed(self):
        st, _t = make(
            [
                [(1, "Nor is it")],
                [(1, "Nor is Mr. Crowell")],
                [
                    (
                        1,
                        "Nor is Mr. Quilter's manner less interesting than his matter.",
                        True,
                    )
                ],
            ]
        )
        st.feed(loud())
        assert st.feed(loud())[0] == "Nor is"
        got, tail = st.feed(loud())
        assert got == "Mr. Quilter's manner less interesting than his matter."
        assert tail == "" and st.held_seconds == 0.0

    def test_a_rewritten_line_is_corrected_by_the_final(self):
        st, _t = make(
            [[(1, "Just regard")], [(1, "Just regard that")]],
            final=[(1, "Disregard that.", True)],
        )
        st.feed(loud())
        assert st.feed(loud())[0] == "Just regard"
        out = st.finish()
        assert out["final"] == "Disregard that." and out["same"] is False

    def test_silence_with_nothing_pending_runs_no_pass(self):
        st, t = make([[(1, "Stop.", True)]])
        st.feed(loud())
        updates = t.lives[0].updates
        assert st.feed(quiet()) == ("", "")
        assert t.lives[0].updates == updates and st.last_ms == 0
        assert t.lives[0].audio == len(loud()) + len(quiet())  # the audio still goes in

    def test_silence_with_a_tail_still_passes(self):
        st, t = make([[(1, "Over and")], [(1, "Over and out.", True)]])
        st.feed(loud())
        assert st.feed(quiet()) == ("Over and out.", "")
        assert t.lives[0].updates == 2

    def test_window_and_turn(self):
        st, _t = make([[(1, "Over")]])
        st.feed(loud(0.5))
        assert st.last_window_s == pytest.approx(0.5)
        verdict = st.turn()
        assert set(verdict) >= {"p", "hold_ms", "reason"}

    def test_an_engine_failure_is_a_speech_error(self):
        st, t = make([[(1, "Over")]])
        t.fail = True
        with pytest.raises(speech.SpeechError):
            st.feed(loud())

    def test_finish_closes_the_engine_stream(self):
        st, t = make([[(1, "Over.", True)]])
        st.feed(loud())
        st.finish()
        assert t.lives[0].stopped and t.lives[0].closed


# --------------------------------------------------------------------------- #
# Keyterms
# --------------------------------------------------------------------------- #
class TestKeyterms:
    def test_hints_split_trimmed_deduplicated(self):
        assert sm.keyterms("mindflock, over,  sitecheck bot 7 ,Over,, ") == [
            "mindflock",
            "over",
            "sitecheck bot 7",
        ]
        assert sm.keyterms("") == []

    def test_the_stream_passes_them_to_the_model_once(self):
        t = FakeTranscriber([[(1, "Over")]])
        eng = sm.MoonshineEngine(t, threads=1)
        sm.MoonshineStream(eng, None, "mindflock, over and out, sitecheck bot 7")
        assert t.keyterms_calls == [["mindflock", "over and out", "sitecheck bot 7"]]
        sm.MoonshineStream(eng, None, "mindflock, over and out, sitecheck bot 7")
        assert len(t.keyterms_calls) == 1  # unchanged: not set again
        sm.MoonshineStream(eng, None, "mindflock, map ux")
        assert t.keyterms_calls[-1] == ["mindflock", "map ux"]
        assert eng.keyterms == ("mindflock", "map ux")

    def test_no_hints_leaves_the_models_list(self):
        t = FakeTranscriber()
        sm.MoonshineStream(sm.MoonshineEngine(t), None, "")
        assert t.keyterms_calls == []

    def test_the_production_hint_string_feeds_it(self):
        from backend.web.core import voice_words

        hints = speech.recognition_hints(voice_words.effective({}), ["sitecheck-bot7"])
        terms = sm.keyterms(hints)
        assert terms[0] == "mindflock"
        assert all("," not in t for t in terms)


# --------------------------------------------------------------------------- #
# The wake word
# --------------------------------------------------------------------------- #
class TestWakeJoin:
    @pytest.mark.parametrize(
        "said, want",
        [
            ("Mind flock, stop.", "Mindflock, stop."),
            ("mind flock what is a worktree", "mindflock what is a worktree"),
            ("hey Mind-Flock?", "hey Mindflock?"),
            ("mind f lock", "mindflock"),
            ("Mindflock, how do fences work?", "Mindflock, how do fences work?"),
            ("mind your step", "mind your step"),
            ("", ""),
        ],
    )
    def test_join(self, said, want):
        assert sm.join_wake(said, "mindflock") == want

    def test_a_custom_wake_word(self):
        assert sm.join_wake("hey Jar vis, over", "jarvis") == "hey Jarvis, over"
        assert sm.join_wake("hey jar vis", "hey jarvis") == "hey jar vis"

    def test_the_stream_joins_before_words_leave(self):
        st, _t = make([[(1, "Mind flock, which sessions")]])
        assert st.feed(loud()) == ("", "Mindflock, which sessions")

    def test_the_first_half_is_never_typed(self):
        st, _t = make(
            [
                [(1, "Okay mind")],
                [(1, "Okay mind")],
                # "mind" is agreed, and "flo" may yet make it the wake word.
                [(1, "Okay mind flo")],
                [(1, "Okay mind flock, stop")],
            ]
        )
        st.feed(loud())
        st.feed(loud())
        got, tail = st.feed(loud())
        assert got == "Okay" and tail == "mind flo"
        got, tail = st.feed(loud())
        assert got == "" and tail == "mindflock, stop"
        assert st.finish()["final"] == "Okay mindflock, stop"

    def test_a_held_word_at_the_end_of_the_hypothesis(self):
        st, _t = make(
            [[(1, "Hello mind now")], [(1, "Hello mind,")], [(1, "Hello mind there")]]
        )
        st.feed(loud())
        # "Hello mind" agreed, but the hypothesis ends at "mind": held.
        assert st.feed(loud()) == ("Hello", "mind,")
        assert st.feed(loud()) == ("mind", "there")

    def test_a_plain_word_is_released_when_the_next_one_breaks_it(self):
        st, _t = make([[(1, "mind your")], [(1, "mind your step")]])
        st.feed(loud())
        assert st.feed(loud())[0] == "mind your"

    def test_the_stream_uses_the_persons_wake_word(self):
        st, _t = make([[(1, "Jar vis, stop")]])
        st.words = {"wake": "jarvis"}
        assert st.feed(loud())[1] == "Jarvis, stop"


# --------------------------------------------------------------------------- #
# The final: whisper writes the text, Moonshine hears the control words
# --------------------------------------------------------------------------- #
class Accurate:
    def __init__(self, text="whisper text", fail=False, delay=0.0):
        self.text = text
        self.fail = fail
        self.delay = delay
        self.calls: list = []

    def __call__(self, audio):
        self.calls.append(len(audio))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise speech.SpeechError("whisper broke")
        return self.text


class TestFinalPolicy:
    def test_a_short_phrase_gets_the_whisper_final(self):
        acc = Accurate("Fix the login bug.")
        st, _t = make([[(1, "Fix the log in bog.", True)]], accurate=acc)
        st.feed(loud(1.0))
        st.feed(quiet(0.6))  # the quiet tail starts the early pass
        out = st.finish()
        assert len(acc.calls) == 1
        assert out["final"] == "Fix the login bug." and out["final_by"] == "whisper"
        assert out["heard"] == "Fix the log in bog."
        assert out["same"] is False and out["final_early"] is True

    def test_a_long_phrase_gets_the_whisper_final(self):
        acc = Accurate("Run the tests again and show me the failures.")
        st, _t = make(
            [[(1, "Run the test again and show me the failures.", True)]],
            accurate=acc,
        )
        for _ in range(3):
            st.feed(loud(1.5))
        st.feed(quiet(0.6))
        out = st.finish()
        assert len(acc.calls) == 1 and acc.calls[0] >= int(4.5 * RATE)
        assert out["final"] == "Run the tests again and show me the failures."
        assert out["final_by"] == "whisper" and out["same"] is False

    def test_finish_starts_a_fresh_pass_without_an_early_one(self):
        # Cut off mid-speech: no quiet tail, so no early pass — finish runs
        # the whole phrase itself.
        acc = Accurate("Long dictation, cut off.")
        st, _t = make([[(1, "Long dictation cut off.", True)]], accurate=acc)
        st.feed(loud(1.5))
        out = st.finish()
        assert len(acc.calls) == 1 and acc.calls[0] == int(1.5 * RATE)
        assert out["final"] == "Long dictation, cut off."
        assert out["final_by"] == "whisper" and out["final_early"] is False

    def test_a_stale_early_pass_is_replaced_by_a_fresh_one(self):
        acc = Accurate("First half and the second half.")
        st, _t = make(
            [
                [(1, "First half")],
                [(1, "First half and")],
                [(1, "First half and the second half.")],
            ],
            accurate=acc,
        )
        st.feed(loud(1.0))
        st.feed(quiet(0.6))  # early pass on the first half…
        st.feed(loud(1.0))  # …then more speech: it no longer covers the phrase
        st.feed(loud(0.5))
        out = st.finish()
        assert out["final"] == "First half and the second half."
        assert out["final_by"] == "whisper" and out["final_early"] is False
        assert acc.calls[-1] == int(3.1 * RATE)  # the whole phrase

    def test_a_slow_pass_is_waited_for_at_most_the_timeout(self, monkeypatch):
        monkeypatch.setattr(ss, "FINAL_TIMEOUT_S", 0.05)
        acc = Accurate("too late", delay=0.5)
        st, _t = make([[(1, "Some dictation here.", True)]], accurate=acc)
        st.feed(loud(1.5))
        st.feed(quiet(0.6))
        t0 = time.monotonic()
        out = st.finish()
        assert time.monotonic() - t0 < 0.4
        assert out["final"] == "Some dictation here." and out["final_by"] == "moonshine"
        assert out["heard"] == "Some dictation here."
        assert st.final_outcome == "timeout"

    def test_the_timeout_is_speech_streams(self):
        assert not hasattr(sm, "POLISH_WAIT_S") and not hasattr(
            sm, "POLISH_MIN_SECONDS"
        )
        assert ss.FINAL_TIMEOUT_S >= 1.5

    def test_a_failed_pass_leaves_moonshines_final(self):
        acc = Accurate(fail=True)
        st, _t = make([[(1, "Some dictation here.", True)]], accurate=acc)
        st.feed(loud(1.5))
        st.feed(quiet(0.6))
        out = st.finish()
        assert len(acc.calls) == 1
        assert out["final"] == "Some dictation here." and out["final_by"] == "moonshine"

    def test_without_whisper_moonshines_text_is_the_final(self):
        st, _t = make([[(1, "Some dictation here.", True)]])
        st.feed(loud(1.5))
        out = st.finish()
        assert out["final"] == out["heard"] == "Some dictation here."
        assert out["final_by"] == "moonshine"

    def test_a_click_gets_no_pass(self):
        acc = Accurate()
        st, _t = make([[(1, "Uh.", True)]], accurate=acc)
        st.feed(loud(0.1))  # under FINAL_MIN_SECONDS of speech
        st.feed(quiet(0.6))
        out = st.finish()
        assert acc.calls == [] and out["final_by"] == "moonshine"

    def test_whispers_text_gets_the_wake_join_too(self):
        acc = Accurate("Mind flock, summarise the long thread.")
        st, _t = make(
            [[(1, "Mindflock, summarise the long thread.", True)]], accurate=acc
        )
        st.feed(loud(1.5))
        st.feed(quiet(0.6))
        out = st.finish()
        assert out["final_by"] == "whisper"
        assert out["final"] == "Mindflock, summarise the long thread."

    def test_a_whisper_final_is_recorded_for_final_stats(self):
        before = ss.final_stats()["samples"]
        st, _t = make([[(1, "Fix the bug.", True)]], accurate=Accurate("Fix the bug."))
        st.feed(loud())
        st.finish()
        assert ss.final_stats()["samples"] == before + 1

    def test_a_moonshine_final_is_not_a_wait(self):
        # GET /api/voice's expect_ms sizes the clients' /end wait: a ~20 ms
        # Moonshine final must not pull it under whisper's real wait.
        before = ss.final_stats()["samples"]
        for st, _t in (
            make([[(1, "Fix the bug.", True)]]),  # no whisper
            make([[(1, "Stop.", True)]], accurate=Accurate("Stop.")),  # control only
        ):
            st.feed(loud())
            st.finish()
        assert ss.final_stats()["samples"] == before


class TestControlWords:
    """Moonshine hears the control words, whisper writes the text."""

    def test_the_radio_word_starts_the_early_pass(self):
        acc = Accurate("Send the report, over.")
        st, _t = make([[(1, "Send the report over.")]], accurate=acc)
        st.feed(loud(1.0))
        st.feed(quiet(0.1))  # one quiet frame: under EARLY_QUIET_SECONDS
        assert st._early is not None
        plain, _t2 = make([[(1, "Send the report")]], accurate=Accurate())
        plain.feed(loud(1.0))
        plain.feed(quiet(0.1))
        assert plain._early is None
        out = st.finish()
        assert out["final_early"] is True and out["final_by"] == "whisper"

    def test_a_plain_pause_starts_it_after_early_quiet_seconds(self):
        st, _t = make([[(1, "Fix the bug")]], accurate=Accurate())
        st.feed(loud(1.0))
        st.feed(quiet(0.3))  # a pause mid-phrase: no pass to go stale
        assert st._early is None
        st.feed(quiet(sm.EARLY_QUIET_SECONDS - 0.3))
        assert st._early is not None
        st.close()

    @pytest.mark.parametrize(
        "whisper, want",
        [
            ("Send the reports, okay.", "Send the reports over."),  # garbled
            ("Send the reports.", "Send the reports over."),  # dropped
            ("Send the reports, over and out.", "Send the reports over."),  # other kind
            ("Send the reports over.", "Send the reports over."),  # agrees
        ],
    )
    def test_a_garbled_over_still_sends_with_whispers_words(self, whisper, want):
        from backend.web.core import voice_words

        acc = Accurate(whisper)
        st, _t = make([[(1, "Send the report over.", True)]], accurate=acc)
        st.feed(loud(1.0))
        st.feed(quiet(0.1))
        out = st.finish()
        assert out["final_by"] == "whisper" and out["heard"] == "Send the report over."
        assert out["final"] == want
        assert voice_words.control_kind(out["final"]) == "send"
        assert out["final"].startswith("Send the reports")  # whisper's body

    def test_no_control_word_from_moonshine_leaves_whisper_alone(self):
        acc = Accurate("Note the hand over.")
        st, _t = make([[(1, "Note the handover.", True)]], accurate=acc)
        st.feed(loud(1.0))
        out = st.finish()
        assert out["final"] == "Note the hand over."

    @pytest.mark.parametrize(
        "said",
        [
            "Stop.",
            "Over.",
            "Mindflock, stop.",
            "Stop talking.",
            "Session three.",  # a switch to a NAME waits for whisper
            "Next one.",
        ],
    )
    def test_a_control_only_phrase_never_waits_for_whisper(self, said):
        acc = Accurate("whisper text", delay=1.0)
        st, _t = make([[(1, said, True)]], accurate=acc)
        st.feed(loud(0.5))
        st.feed(quiet(0.6))  # an early pass may have started: it is dropped
        t0 = time.monotonic()
        out = st.finish()
        assert time.monotonic() - t0 < 0.3
        assert out["final"] == out["heard"] == said and out["final_by"] == "moonshine"

    def test_a_switch_to_a_name_takes_whispers_words(self):
        """Moonshine heard "sitecheck bot seven" 0/16 on the bench, whisper
        14/16: a switch by NAME is no instant control phrase — ``final``
        is whisper's, ``heard`` Moonshine's beside it."""
        acc = Accurate("Switch to sitecheck bot seven.")
        st, _t = make([[(1, "Switch to sight check bot 7.", True)]], accurate=acc)
        st.feed(loud(1.0))
        out = st.finish()
        assert out["final"] == "Switch to sitecheck bot seven."
        assert out["final_by"] == "whisper"
        assert out["heard"] == "Switch to sight check bot 7."

    @pytest.mark.parametrize(
        "heard, only",
        [
            ("stop listening", True),
            ("Disregard that.", True),
            ("Send the report over.", False),
            ("Add a docstring to the new function, disregard.", True),
            ("Open a pull request for this branch, over.", False),  # no switch
            ("Switch to sitecheck.", False),  # a name waits for whisper
            ("Next session.", True),
            ("Run the tests, stop listening.", False),
            ("Fix the bug.", False),
            ("new session in app that fixes login", False),
            ("yes", False),
            ("", False),
            # The router's own no-text kinds act at once too.
            ("Fix the login bug, scratch that.", True),  # undo: nothing to send
            ("Scratch that.", True),
            ("Scroll up.", True),
            ("Page down.", True),
            ("Interrupt.", True),
            ("Go back.", True),
            ("Where am I?", True),
            ("What can I say?", True),
            ("Scroll down to the footer and fix the margin.", False),
            ("Go back and fix the test.", False),
        ],
    )
    def test_control_only(self, heard, only):
        assert sm.control_only(heard) is only

    def test_control_only_reads_the_persons_words(self):
        words = {"send": ["roger"]}
        assert sm.control_only("Roger.", words)
        assert not sm.control_only("Roger.", {"send": ["over"]})

    @pytest.mark.parametrize(
        "final, heard, want",
        [
            (
                "Fix it, stop listening.",
                "Fix it, stop listening.",
                "Fix it, stop listening.",
            ),
            ("Fix it.", "Fix it.", "Fix it."),
            ("Fix it, okay.", "Fix it, over and out.", "Fix it over and out."),
            (
                "Mind flock, run it.",
                "Mindflock, run it, over.",
                "Mind flock, run it over.",
            ),
            (
                "Send the report, ober.",
                "Send the report over.",
                "Send the report over.",
            ),
        ],
    )
    def test_with_control(self, final, heard, want):
        assert sm.with_control(final, heard) == want


# --------------------------------------------------------------------------- #
# Availability, loading, the thread cap
# --------------------------------------------------------------------------- #
def _model_dir(root) -> str:
    d = root / "download.moonshine.ai" / "model" / ("%s-en" % sm.ARCH) / "quantized_x"
    d.mkdir(parents=True)
    for name in sm.MODEL_FILES:
        (d / name).write_bytes(b"x")
    return str(d)


class FakeArch:
    TINY_STREAMING = 2


def _fake_module(record: dict):
    class Transcriber(FakeTranscriber):
        def __init__(self, path, arch, update_interval=0.5, options=None):
            super().__init__()
            record["path"], record["arch"] = path, arch
            record["env"] = os.environ.get("MOONSHINE_ORT_SINGLE_THREAD")

    mv = types.SimpleNamespace(ModelArch=FakeArch)
    tr = types.SimpleNamespace(Transcriber=Transcriber)
    return {"moonshine_voice": mv, "moonshine_voice.transcriber": tr}


class TestAvailability:
    def test_unavailable_without_the_package(self, monkeypatch, tmp_path):
        _model_dir(tmp_path / "voice" / "moonshine")
        monkeypatch.setattr(speech, "_installed", lambda m: False)
        assert sm.available() is False
        assert "moonshine-voice is not installed" in sm.unavailable_reason()

    def test_unavailable_without_the_model(self, monkeypatch):
        monkeypatch.setattr(speech, "_installed", lambda m: True)
        assert sm.model_path() is None
        assert sm.available() is False
        assert "not downloaded" in sm.unavailable_reason()

    def test_a_partial_download_is_not_a_model(self, monkeypatch, tmp_path):
        monkeypatch.setattr(speech, "_installed", lambda m: True)
        d = _model_dir(tmp_path / "voice" / "moonshine")
        os.remove(os.path.join(d, "encoder.ort"))
        assert sm.available() is False

    def test_available_with_both(self, monkeypatch, tmp_path):
        monkeypatch.setattr(speech, "_installed", lambda m: True)
        d = _model_dir(tmp_path / "voice" / "moonshine")
        assert sm.available() is True and sm.unavailable_reason() == ""
        assert str(sm.model_path()) == d

    def test_engine_without_a_model_raises_for_the_fallback(self, monkeypatch):
        with pytest.raises(speech.SpeechError):
            sm.engine()
        with pytest.raises(speech.SpeechError):
            sm.open_stream("mindflock")
        assert speech.loaded() == []

    def test_english_only(self):
        assert sm.supports("") and sm.supports("en-US") and sm.supports("en")
        assert not sm.supports("de-DE") and not sm.supports("fr")


class TestLoading:
    @pytest.fixture
    def fake(self, monkeypatch, tmp_path):
        record: dict = {}
        mods = _fake_module(record)
        monkeypatch.setattr(speech, "_import", lambda name: mods[name])
        monkeypatch.setattr(speech, "_installed", lambda m: True)
        record["dir"] = _model_dir(tmp_path / "voice" / "moonshine")
        monkeypatch.delenv("MOONSHINE_ORT_SINGLE_THREAD", raising=False)
        return record

    def test_loaded_once_through_the_speech_cache(self, fake):
        a = sm.engine()
        b = sm.engine()
        assert a is b and fake["path"] == fake["dir"] and fake["arch"] == 2
        assert sm.CACHE_KEY in speech.loaded()
        # The idle sweep / memory shedding see it like a whisper model.
        assert sm.CACHE_KEY in speech.sweep_idle(now=speech._now() + 10**6)

    def test_the_thread_cap_is_set_before_the_load(self, fake):
        eng = sm.engine()
        assert fake["env"] == "1" and eng.threads == 1

    def test_an_explicit_opt_out_is_respected(self, fake, monkeypatch):
        monkeypatch.setenv("MOONSHINE_ORT_SINGLE_THREAD", "0")
        eng = sm.engine()
        assert fake["env"] == "0" and eng.threads == 0

    def test_a_load_failure_is_a_speech_error(self, fake, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("bad model")

        mods = {
            "moonshine_voice": types.SimpleNamespace(ModelArch=FakeArch),
            "moonshine_voice.transcriber": types.SimpleNamespace(Transcriber=boom),
        }
        monkeypatch.setattr(speech, "_import", lambda name: mods[name])
        with pytest.raises(speech.SpeechError, match="bad model"):
            sm.engine()


# --------------------------------------------------------------------------- #
# The registry is speech_stream's
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_open_registers_a_moonshine_stream(self):
        eng = sm.MoonshineEngine(FakeTranscriber([[(1, "Over.", True)]]))
        sid = sm.open_stream("mindflock", None, client="page-1", engine_=eng)
        st = ss.get_stream(sid)
        assert isinstance(st, sm.MoonshineStream) and st.client == "page-1"
        assert ss.close_stream(sid) is st

    def test_the_stream_cap_applies(self):
        eng = sm.MoonshineEngine(FakeTranscriber())
        sids = [
            sm.open_stream("", None, client="c%d" % i, engine_=eng)
            for i in range(ss.MAX_STREAMS)
        ]
        assert all(sids)
        assert sm.open_stream("", None, client="late", engine_=eng) is None
        assert eng.transcriber.lives[-1].closed  # the refused one let go

    def test_a_clients_oldest_is_replaced(self):
        eng = sm.MoonshineEngine(FakeTranscriber())
        first = sm.open_stream("", None, client="page", engine_=eng)
        for _ in range(ss.CLIENT_STREAMS):
            sm.open_stream("", None, client="page", engine_=eng)
        assert ss.get_stream(first) is None

    @pytest.mark.parametrize("how", ["sweep", "delete", "replaced"])
    def test_a_dropped_stream_lets_its_engine_stream_go(self, how, monkeypatch):
        # A stream the registry forgets without its finish (the page locked,
        # the tab closed, a reload replaced it) frees Moonshine's engine
        # stream at once — not whenever the cycle collector next gets to it
        # (an early pass ties the stream into a cycle with its lane job).
        import gc

        t = FakeTranscriber([[(1, "Fix the log in bog.", False)]])
        eng = sm.MoonshineEngine(t, threads=1)
        acc = Accurate("Fix the login bug.")
        gc.disable()
        try:
            sid = sm.open_stream("", acc, client="page", engine_=eng)
            st = ss.get_stream(sid)
            st.feed(loud(1.0))
            st.feed(quiet(0.6))  # the quiet tail starts the early pass
            assert st._early is not None
            st._early.thread.join(2)
            del st
            if how == "sweep":
                later = ss._now() + ss.EXPIRE_SECONDS + 1
                monkeypatch.setattr(ss, "_now", lambda: later)
                assert ss.sweep() == [sid]
            elif how == "delete":  # DELETE /api/voice/stream/{sid}
                from backend.web.routes import speech as routes

                assert routes.voice_stream_abandon(sid).status_code == 204
                assert ss.get_stream(sid) is None
            else:
                for _ in range(ss.CLIENT_STREAMS):
                    sm.open_stream("", None, client="page", engine_=eng)
                assert ss.get_stream(sid) is None
            assert t.lives[0].closed
        finally:
            gc.enable()
