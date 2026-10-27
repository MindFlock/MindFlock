"""``core.speech_stream``: the committed-words policy (LocalAgreement-2)
against a transcriber scripted per pass, the prune, the WAV decoder and the
registry. Nothing here loads a model.
"""

from __future__ import annotations

import io
import wave
from array import array

import pytest

from backend.web.core import speech
from backend.web.core import speech_stream as ss

RATE = ss.RATE


def _words(text, start=0.0, step=0.5):
    """``[(word, start, end)]`` for ``text``, one word every ``step`` s."""
    return [
        (w, start + i * step, start + i * step + 0.4)
        for i, w in enumerate(text.split())
    ]


class Script:
    """A transcriber that answers pass ``n`` with ``passes[n]`` and records
    what it was given."""

    def __init__(self, *passes):
        self.passes = list(passes)
        self.calls: list = []

    def __call__(self, audio, prompt):
        self.calls.append((len(audio), prompt))
        out = self.passes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _secs(s, level=0.1):
    """``s`` seconds of audio: a steady ``level`` (speech-loud by default,
    0.0 for silence)."""
    return array("f", [level] * int(s * RATE))


@pytest.fixture(autouse=True)
def _registry():
    ss.clear()
    yield
    ss.clear()


class TestCommit:
    def test_words_commit_only_after_two_agreeing_passes(self):
        fake = Script(_words("hello world"), _words("Hello world how"))
        st = ss.Stream(fake)
        assert st.feed(_secs(1)) == ("", "hello world")
        assert st.feed(_secs(0.5)) == ("Hello world", "how")
        assert st.text == "Hello world"

    def test_a_changed_tail_is_never_committed(self):
        fake = Script(
            _words("I want to go"),
            _words("I want two"),
            _words("I want to go home"),
            _words("I want to go home"),
        )
        st = ss.Stream(fake)
        st.feed(_secs(1))
        assert st.feed(_secs(1)) == ("I want", "two")
        # "two" changed to "to": nothing new agrees with the old tail.
        assert st.feed(_secs(1)) == ("", "to go home")
        assert st.feed(_secs(1)) == ("to go home", "")
        assert st.text == "I want to go home"

    def test_casing_and_punctuation_come_from_the_current_pass(self):
        fake = Script(_words("hello, WORLD"), _words("Hello world. Next"))
        st = ss.Stream(fake)
        st.feed(_secs(1))
        assert st.feed(_secs(1)) == ("Hello world.", "Next")

    def test_a_shrinking_hypothesis(self):
        fake = Script(
            _words("the quick brown fox"),
            _words("the quick"),
            _words("the quick brown fox jumps"),
            _words("the quick brown fox jumps"),
        )
        st = ss.Stream(fake)
        st.feed(_secs(1))
        assert st.feed(_secs(1)) == ("the quick", "")
        assert st.feed(_secs(1)) == ("", "brown fox jumps")
        assert st.feed(_secs(1)) == ("brown fox jumps", "")

    def test_a_reheard_committed_word_with_a_late_timestamp_is_dropped(self):
        fake = Script(
            _words("hello world"),
            _words("hello world"),
            [("hello", 0.0, 0.4), ("world", 0.85, 1.2), ("again", 1.3, 1.6)],
            [("world", 0.85, 1.2), ("again", 1.3, 1.6)],
        )
        st = ss.Stream(fake)
        st.feed(_secs(1))
        assert st.feed(_secs(1)) == ("hello world", "")
        assert st.feed(_secs(1)) == ("", "again")
        assert st.feed(_secs(1)) == ("again", "")
        assert st.text == "hello world again"

    def test_the_flush_commits_the_rest(self):
        fake = Script(
            _words("Hello world"),
            _words("Hello world how"),
            _words("Hello world how are you?"),
        )
        st = ss.Stream(fake)
        st.feed(_secs(1))
        st.feed(_secs(1))
        assert st.finish() == {"committed": "how are you?", "partial": ""}
        assert st.text == "Hello world how are you?"
        assert st.held_seconds == 0

    def test_initial_prompt_carries_the_committed_text(self):
        fake = Script(_words("one two"), _words("one two three"), [])
        st = ss.Stream(fake)
        st.feed(_secs(1))
        st.feed(_secs(1))
        st.feed(_secs(1))
        assert [p for _n, p in fake.calls] == ["", "", "one two"]

    def test_the_prompt_is_the_last_200_characters_on_a_word_boundary(self):
        st = ss.Stream(Script([]))
        st._committed = [("word%03d" % i, i, i + 0.5) for i in range(60)]
        prompt = st._prompt()
        assert len(prompt) <= ss.PROMPT_CHARS
        assert prompt.split()[0].startswith("word") and len(prompt.split()[0]) == 7
        assert prompt.endswith("word059")

    def test_empty_audio_runs_no_pass(self):
        fake = Script()
        st = ss.Stream(fake)
        assert st.feed(array("f")) == ("", "")
        assert st.finish() == {"committed": "", "partial": ""}
        assert fake.calls == []

    def test_a_failed_pass_keeps_the_chunk(self):
        fake = Script(speech.SpeechError("faster-whisper failed: boom"), [])
        st = ss.Stream(fake)
        with pytest.raises(speech.SpeechError):
            st.feed(_secs(1))
        st.feed(_secs(1))
        assert fake.calls[1][0] == 2 * RATE

    def test_ms_and_held_seconds(self):
        st = ss.Stream(Script([]))
        st.feed(_secs(1.5))
        assert st.held_seconds == 1.5
        assert isinstance(st.last_ms, int) and st.last_ms >= 0


class TestPrune:
    def test_audio_before_the_last_committed_word_is_dropped(self):
        fake = Script(
            [("one", 0.0, 0.5), ("two", 0.6, 1.0), ("three", 2.5, 2.9)],
            [
                ("one", 0.0, 0.5),
                ("two", 0.6, 1.0),
                ("three", 2.5, 2.9),
                ("four", 5.0, 5.5),
            ],
            # The window now starts at 2.6 s: times are relative to it.
            [("three", 0.0, 0.3), ("four", 2.4, 2.9), ("five", 3.1, 3.5)],
        )
        st = ss.Stream(fake)
        st.feed(_secs(3))
        assert st.feed(_secs(3)) == ("one two three", "four")
        assert st.held_seconds == pytest.approx(6 - 2.6, abs=1e-3)
        assert st.feed(_secs(0.5)) == ("four", "five")
        assert fake.calls[2][0] == int(round((6.5 - 2.6) * RATE))

    def test_uncommitted_words_keep_their_audio(self):
        fake = Script(_words("um so"), _words("well then"))
        st = ss.Stream(fake)
        st.feed(_secs(3))
        assert st.feed(_secs(3)) == ("", "well then")
        assert st.held_seconds == 6  # nothing committed: nothing dropped

    def test_a_tail_after_commits_keeps_its_audio(self):
        fake = Script(
            [("one", 0.0, 0.5), ("two", 0.6, 1.0)],
            [("one", 0.0, 0.5), ("two", 0.6, 1.0), ("three", 1.1, 1.5)],
        )
        st = ss.Stream(fake)
        st.feed(_secs(3))
        assert st.feed(_secs(3)) == ("one two", "three")
        # cut at min(two's end - 0.3, three's start - 0.3) = 0.7 s
        assert st.held_seconds == pytest.approx(6 - 0.7, abs=1e-3)

    def test_a_window_with_no_words_keeps_its_last_second(self):
        st = ss.Stream(Script([], []))
        st.feed(_secs(3))
        st.feed(_secs(3))
        assert st.held_seconds == pytest.approx(ss.SILENT_KEEP_SECONDS)


class TestWindow:
    """No pass decodes more than WINDOW_SECONDS; what leaves the window
    uncommitted is committed on the way out."""

    def test_a_pass_never_decodes_more_than_the_window(self):
        assert ss.WINDOW_SECONDS == 8.0 and ss.MAX_HELD_SECONDS == 8.0
        fake = Script(
            # Nothing agrees yet: "a" straddles the edge-to-be (2 s), "b" not.
            [("a", 1.5, 2.5), ("b", 5.0, 5.5)],
            # The window starts at 2.0 s now: times are relative to it.
            [("b", 3.0, 3.5), ("c", 7.0, 7.5)],
        )
        st = ss.Stream(fake)
        assert st.feed(_secs(6)) == ("", "a b")
        assert st.feed(_secs(4)) == ("a b", "c")
        assert fake.calls[1][0] == 8 * RATE
        assert st.last_window_s == 8.0
        # Then the usual prune: cut at b's end - 0.3 = 5.2 s.
        assert st.held_seconds == pytest.approx(10 - 5.2, abs=1e-3)
        assert st.text == "a b"

    def test_a_tail_that_never_settles_is_committed_at_the_edge(self):
        st = ss.Stream(Script(_words("x y"), _words("p q"), _words("p q")))
        st.feed(_secs(3))
        assert st.feed(_secs(3)) == ("", "p q")
        # 9 s held: the first second leaves, and the tail that starts in it
        # ("p" at 0.0 s, "q" at 0.5 s) is committed as it goes.
        assert st.feed(_secs(3)) == ("p q", "")
        assert st.text == "p q"

    def test_words_forced_out_before_a_failed_pass_are_returned_later(self):
        fake = Script(
            [("a", 1.0, 1.5), ("b", 5.0, 5.5)],
            speech.SpeechError("faster-whisper failed: boom"),
            [("b", 3.0, 3.5)],
        )
        st = ss.Stream(fake)
        st.feed(_secs(6))
        with pytest.raises(speech.SpeechError):
            st.feed(_secs(4))
        assert st.text == "a"  # committed when it left the window ...
        assert st.feed(_secs(0)) == ("a b", "")  # ... and returned with the next

    def test_the_flush_respects_the_window(self):
        fake = Script(speech.SpeechError("boom"), [("late", 7.0, 7.5)])
        st = ss.Stream(fake)
        with pytest.raises(speech.SpeechError):
            st.feed(_secs(9))
        assert st.finish()["committed"] == "late"
        assert fake.calls[1][0] == 8 * RATE


class TestSilence:
    """A quiet chunk with nothing pending runs no pass."""

    def test_quiet(self):
        assert ss._quiet(array("f")) is True
        assert ss._quiet(array("f", [0.005, -0.005] * 100)) is True
        assert ss._quiet(array("f", [0.02, -0.02] * 100)) is False

    def test_silence_with_nothing_pending_runs_no_pass(self):
        fake = Script(_words("hello"))
        st = ss.Stream(fake)
        assert st.feed(_secs(1, level=0.0)) == ("", "")
        assert fake.calls == []
        assert (st.last_ms, st.last_window_s) == (0, 0.0)
        # The quiet audio is kept: a soft start is decoded with the next chunk.
        assert st.feed(_secs(0.5)) == ("", "hello")
        assert fake.calls[0][0] == int(1.5 * RATE)
        assert st.last_window_s == 1.5

    def test_silence_still_decodes_while_a_tail_waits(self):
        fake = Script(_words("hello"), _words("hello"))
        st = ss.Stream(fake)
        st.feed(_secs(1))
        assert st.feed(_secs(0.5, level=0.0)) == ("hello", "")
        assert len(fake.calls) == 2

    def test_silence_after_commits_runs_no_pass(self):
        fake = Script(_words("hi there"), _words("hi there"))
        st = ss.Stream(fake)
        st.feed(_secs(1))
        assert st.feed(_secs(1)) == ("hi there", "")
        assert st.feed(_secs(1, level=0.0)) == ("", "")
        assert len(fake.calls) == 2

    def test_a_long_silence_is_held_to_the_window(self):
        st = ss.Stream(Script())
        assert st.feed(_secs(12, level=0.0)) == ("", "")
        assert st.held_seconds == ss.WINDOW_SECONDS


class TestDecodeWav:
    @staticmethod
    def _wav(samples, rate=RATE, channels=1, width=2):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(array("h", samples).tobytes())
        return buf.getvalue()

    def test_pcm16_to_float(self):
        out = ss.decode_wav(self._wav([0, 16384, -32768, 32767]))
        assert out.typecode == "f"
        assert list(out)[:3] == [0.0, 0.5, -1.0]
        assert out[3] == pytest.approx(1.0, abs=1e-4)

    def test_other_formats_are_refused(self):
        with pytest.raises(ValueError, match="not 48000 Hz, 1 channel"):
            ss.decode_wav(self._wav([0, 0], rate=48000))
        with pytest.raises(ValueError, match="2 channel"):
            ss.decode_wav(self._wav([0, 0], channels=2))
        with pytest.raises(ValueError, match="as a WAV file"):
            ss.decode_wav(b"\x1aE\xdf\xa3webm")
        with pytest.raises(ValueError):
            ss.decode_wav(b"")


class Accurate:
    """An accurate pass that answers ``text`` (or raises it, or waits for
    ``gate``) and records the audio lengths it was given."""

    def __init__(self, text="", gate=None):
        self.text, self.gate = text, gate
        self.calls: list = []

    def __call__(self, audio):
        self.calls.append(len(audio))
        if self.gate is not None:
            self.gate.wait(5)
        if isinstance(self.text, BaseException):
            raise self.text
        return self.text


def _spoken(fast, accurate, chunks=2, secs=1.0):
    """A stream that heard ``chunks`` loud chunks: the fast tier commits
    ``fast`` (two agreeing passes, then the flush)."""
    st = ss.Stream(Script(*([_words(fast)] * (chunks + 1))), accurate)
    for _ in range(chunks):
        st.feed(_secs(secs))
    return st


class TestFinal:
    """The second tier: the accurate pass over the whole phrase at the end."""

    def test_finish_returns_the_accurate_text(self):
        acc = Accurate(" run the tests ")
        st = _spoken("run the tick", acc)
        out = st.finish()
        assert out["committed"] == "" and out["partial"] == ""
        assert out["final"] == "run the tests"
        assert out["same"] is False
        assert isinstance(out["final_ms"], int) and out["final_ms"] >= 0
        assert set(out) == {"committed", "partial", "final", "final_ms", "same"}
        # The whole phrase, not the pruned window.
        assert acc.calls == [2 * RATE]
        assert st.final_ms == out["final_ms"] and st.phrase_seconds == 0

    def test_the_phrase_outlives_the_window(self):
        acc = Accurate("all of it")
        st = _spoken("a b c", acc, chunks=12)
        assert st.held_seconds <= ss.WINDOW_SECONDS
        assert st.phrase_seconds == 12
        st.finish()
        assert acc.calls == [12 * RATE]

    def test_same_when_only_case_and_punctuation_differ(self):
        out = _spoken("hello world", Accurate("Hello, world.")).finish()
        assert out["final"] == "Hello, world." and out["same"] is True

    @pytest.mark.parametrize(
        "a,b,same",
        [
            ("Hello, world.", "hello world", True),
            ("  run   the tests ", "Run the tests!", True),
            ("don't stop", "Dont stop", True),
            ("run the tests", "run the tick", False),
            ("one two", "one two three", False),
            ("", "", True),
        ],
    )
    def test_same_text(self, a, b, same):
        assert ss.same_text(a, b) is same

    def test_a_failed_accurate_pass_leaves_the_fast_text(self, caplog):
        st = _spoken("hello there", Accurate(speech.SpeechError("boom")))
        with caplog.at_level("WARNING", logger=ss.__name__):
            assert st.finish() == {"committed": "", "partial": ""}
        assert "accurate pass failed (boom)" in caplog.text
        assert st.text == "hello there"

    def test_an_accurate_pass_that_hears_nothing_leaves_the_fast_text(self):
        assert _spoken("hello", Accurate("  ")).finish() == {
            "committed": "",
            "partial": "",
        }

    def test_the_timeout_leaves_the_fast_text(self, monkeypatch, caplog):
        import threading

        monkeypatch.setattr(ss, "FINAL_TIMEOUT_S", 0.05)
        gate = threading.Event()
        acc = Accurate("too late", gate=gate)
        st = _spoken("hello there", acc)
        with caplog.at_level("WARNING", logger=ss.__name__):
            out = st.finish()
        gate.set()
        assert out == {"committed": "", "partial": ""}
        assert caplog.text.count("took over 0.1s") == 1
        assert acc.calls == [2 * RATE]  # it ran; its late result is dropped

    def test_a_short_phrase_skips_the_accurate_pass(self):
        acc = Accurate("hi")
        st = ss.Stream(Script(_words("hi"), _words("hi")), acc)
        st.feed(_secs(0.5))
        assert st.voiced_s == 0.5 < ss.FINAL_MIN_SECONDS
        assert st.finish() == {"committed": "hi", "partial": ""}
        assert acc.calls == []

    def test_silence_does_not_count_as_speech(self):
        acc = Accurate("hi")
        st = ss.Stream(Script(_words("hi"), _words("hi"), []), acc)
        st.feed(_secs(0.4))
        st.feed(_secs(3, level=0.0))  # 3.4 s of audio, 0.4 s of it speech
        assert st.voiced_s == pytest.approx(0.4)
        st.finish()
        assert acc.calls == []

    def test_speech_is_counted_in_frames(self):
        chunk = array("f", [0.0] * RATE)
        chunk[: RATE // 4] = array("f", [0.1] * (RATE // 4))  # 250 ms loud
        assert ss._voiced(chunk) == pytest.approx(0.3)  # three 100 ms frames
        assert ss._voiced(array("f")) == 0

    def test_the_phrase_buffer_is_capped(self, monkeypatch, caplog):
        monkeypatch.setattr(ss, "PHRASE_MAX_SECONDS", 3.0)
        acc = Accurate("never")
        st = ss.Stream(Script(*([[]] * 5)), acc)
        with caplog.at_level("INFO", logger=ss.__name__):
            for _ in range(4):
                st.feed(_secs(1))
        assert st.phrase_seconds == 3.0 and st.phrase_trimmed is True
        assert caplog.text.count("phrase over 3s") == 1  # noted once
        assert st.finish() == {"committed": "", "partial": ""}
        assert acc.calls == []  # it would not cover the typed phrase

    def test_without_an_accurate_pass_no_phrase_is_kept(self):
        st = ss.Stream(Script([], []))
        st.feed(_secs(2))
        assert st.phrase_seconds == 0
        assert st.finish() == {"committed": "", "partial": ""}

    def test_a_failed_fast_flush_still_raises(self):
        acc = Accurate("x")
        st = ss.Stream(Script([], speech.SpeechError("boom")), acc)
        st.feed(_secs(1))
        with pytest.raises(speech.SpeechError):
            st.finish()

    def test_open_stream_passes_the_accurate_pass_on(self):
        acc = Accurate("ok then")
        sid = ss.open_stream(Script(*([_words("ok")] * 3)), acc)
        stream = ss.get_stream(sid)
        stream.feed(_secs(1))
        assert ss.close_stream(sid).finish()["final"] == "ok then"


class TestRegistry:
    def test_cap(self):
        ids = [ss.open_stream(Script()) for _ in range(ss.MAX_STREAMS)]
        assert all(ids) and len(set(ids)) == ss.MAX_STREAMS
        assert ss.open_stream(Script()) is None
        assert ss.close_stream(ids[0]) is not None
        assert ss.close_stream(ids[0]) is None
        assert ss.open_stream(Script()) is not None

    def test_expiry_after_the_last_chunk(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        sid = ss.open_stream(Script([]))
        clock[0] += ss.EXPIRE_SECONDS - 1
        ss.get_stream(sid).feed(_secs(0.5))  # a chunk restarts the clock
        clock[0] += ss.EXPIRE_SECONDS - 1
        assert ss.get_stream(sid) is not None
        clock[0] += 1
        assert ss.get_stream(sid) is None

    def test_a_stream_mid_pass_is_not_swept(self, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        sid = ss.open_stream(Script())
        stream = ss.get_stream(sid)
        clock[0] = ss.EXPIRE_SECONDS * 2
        with stream._lock:
            assert ss.sweep() == []
        assert ss.sweep() == [sid]

    def test_the_sweep_timer_runs_while_streams_are_open(self, monkeypatch):
        armed: list = []

        class _Timer:
            def __init__(self, delay, fn):
                armed.append((delay, fn))
                self.daemon = False

            def start(self):
                pass

            def is_alive(self):
                return False

            def cancel(self):
                pass

        monkeypatch.setattr(ss.threading, "Timer", _Timer)
        clock = [0.0]
        monkeypatch.setattr(ss, "_now", lambda: clock[0])
        sid = ss.open_stream(Script())
        assert armed and armed[0][0] == ss.EXPIRE_SECONDS / 2
        clock[0] = ss.EXPIRE_SECONDS
        armed[0][1]()
        assert ss.get_stream(sid) is None
        assert len(armed) == 1  # nothing open: not re-armed
