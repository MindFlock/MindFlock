"""``core.speech_moonshine``: the Moonshine streaming recognizer behind the
same interface as ``speech_stream.Stream`` — with the engine faked (a
scripted transcriber: no package, no model in CI). Interface parity, the
commit policy, keyterms, the wake-word join, the short/long final policy,
the fallback when unavailable, the thread cap and the shared registry.
"""

from __future__ import annotations

import inspect
import os
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
# The final: Moonshine's for a short phrase, whisper's for a long one
# --------------------------------------------------------------------------- #
class Accurate:
    def __init__(self, text="whisper text", fail=False):
        self.text = text
        self.fail = fail
        self.calls: list = []

    def __call__(self, audio):
        self.calls.append(len(audio))
        if self.fail:
            raise speech.SpeechError("whisper broke")
        return self.text


class TestFinalPolicy:
    def test_a_short_phrase_never_runs_the_accurate_pass(self):
        acc = Accurate()
        st, _t = make([[(1, "Over and out.", True)]], accurate=acc)
        st.feed(loud(1.0))
        st.feed(quiet(0.6))  # the quiet tail would start an early whisper pass
        assert not st.wants_polish
        out = st.finish()
        assert acc.calls == []
        assert out["final"] == "Over and out." and out["final_by"] == "moonshine"
        assert out["same"] is True and out["final_early"] is False

    def test_a_long_phrase_gets_whisper_polish(self):
        acc = Accurate("Run the tests again and show me the failures.")
        st, _t = make(
            [[(1, "Run the test again and show me the failures.", True)]],
            accurate=acc,
        )
        for _ in range(3):
            st.feed(loud(1.5))
        assert st.wants_polish
        out = st.finish()
        assert len(acc.calls) == 1 and acc.calls[0] == int(4.5 * RATE)
        assert out["final"] == "Run the tests again and show me the failures."
        assert out["final_by"] == "whisper" and out["same"] is False

    def test_the_polish_starts_early_on_the_quiet_tail(self):
        acc = Accurate("Long dictation.")
        st, _t = make([[(1, "Long dictation.", True)]], accurate=acc)
        for _ in range(3):
            st.feed(loud(1.5))
        st.feed(quiet(0.6))
        out = st.finish()
        assert len(acc.calls) == 1
        assert out["final_early"] is True and out["final_by"] == "whisper"

    def test_a_failed_polish_leaves_moonshines_final(self):
        acc = Accurate(fail=True)
        st, _t = make([[(1, "Long dictation here.", True)]], accurate=acc)
        for _ in range(3):
            st.feed(loud(1.5))
        out = st.finish()
        assert out["final"] == "Long dictation here." and out["final_by"] == "moonshine"

    def test_whispers_text_gets_the_wake_join_too(self):
        acc = Accurate("Mind flock, summarise the long thread.")
        st, _t = make(
            [[(1, "Mindflock, summarise the long thread.", True)]], accurate=acc
        )
        for _ in range(3):
            st.feed(loud(1.5))
        assert st.finish()["final"] == "Mindflock, summarise the long thread."

    def test_the_wait_is_recorded_for_final_stats(self):
        before = ss.final_stats()["samples"]
        st, _t = make([[(1, "Over.", True)]])
        st.feed(loud())
        st.finish()
        assert ss.final_stats()["samples"] == before + 1


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
