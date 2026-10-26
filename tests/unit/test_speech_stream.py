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


def _secs(s):
    return array("f", [0.0] * int(s * RATE))


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
        assert st.finish() == "how are you?"
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
        assert st.finish() == ""
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
            [("one", 0.0, 0.5), ("two", 0.6, 1.0), ("three", 5.0, 5.5)],
            [
                ("one", 0.0, 0.5),
                ("two", 0.6, 1.0),
                ("three", 5.0, 5.5),
                ("four", 11.0, 11.5),
            ],
            # The window now starts at 5.2 s: times are relative to it.
            [("three", 0.0, 0.3), ("four", 5.8, 6.3), ("five", 6.5, 6.9)],
        )
        st = ss.Stream(fake)
        st.feed(_secs(6))
        assert st.feed(_secs(6)) == ("one two three", "four")
        assert st.held_seconds == pytest.approx(12 - 5.2, abs=1e-3)
        assert st.feed(_secs(0.5)) == ("four", "five")
        assert fake.calls[2][0] == int(round((12.5 - 5.2) * RATE))

    def test_uncommitted_words_keep_their_audio(self):
        fake = Script(_words("um so"), _words("well then"))
        st = ss.Stream(fake)
        st.feed(_secs(6))
        assert st.feed(_secs(6)) == ("", "well then")
        assert st.held_seconds == 12  # nothing committed: nothing dropped

    def test_a_tail_after_commits_keeps_its_audio(self):
        fake = Script(
            [("one", 0.0, 0.5), ("two", 0.6, 1.0)],
            [("one", 0.0, 0.5), ("two", 0.6, 1.0), ("three", 1.1, 1.5)],
        )
        st = ss.Stream(fake)
        st.feed(_secs(6))
        assert st.feed(_secs(6)) == ("one two", "three")
        # cut at min(two's end - 0.3, three's start - 0.3) = 0.7 s
        assert st.held_seconds == pytest.approx(12 - 0.7, abs=1e-3)

    def test_a_window_with_no_words_keeps_its_last_second(self):
        st = ss.Stream(Script([], []))
        st.feed(_secs(6))
        st.feed(_secs(6))
        assert st.held_seconds == pytest.approx(ss.SILENT_KEEP_SECONDS)

    def test_a_tail_that_never_settles_is_committed_past_30_seconds(self):
        # Nothing agrees yet, but "a" ended more than 10 s before the end of
        # 31 s of audio: committed anyway; "b" is recent and waits.
        st = ss.Stream(Script([("a", 1.0, 1.5), ("b", 25.0, 25.5)]))
        assert st.feed(_secs(31)) == ("a", "b")
        assert st.held_seconds == pytest.approx(31 - 1.2, abs=1e-3)


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
