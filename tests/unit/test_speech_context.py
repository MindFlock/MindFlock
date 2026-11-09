"""The dictation context: ``POST /api/voice/stream``'s ``context`` (the text
already dictated in this message before this phrase) reaching the accurate
pass as whisper's ``initial_prompt`` — sanitized and bounded
(``core.speech.dictation_context``), beside the ``hotwords`` vocabulary,
inside Whisper's token budget (``core.speech._prompt_room``), on every
accurate pass of the stream (the early one and ``/end``'s), on both the
whisper and the Moonshine stream paths; no context is today's call exactly.
faster-whisper and Moonshine are faked.
"""

from __future__ import annotations

from array import array
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import memory_pressure
from backend.web import server
from backend.web.core import speech
from backend.web.core import speech_stream as ss
from backend.web.routes import speech as routes

client = TestClient(server.app)
RATE = ss.RATE


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_VOICE_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(speech, "_arm_timer", lambda: None)
    monkeypatch.setattr(memory_pressure, "is_low", lambda **kw: False)
    monkeypatch.setattr(speech, "_spawn", lambda target, name: target())
    speech.unload_all()
    ss.clear()
    yield
    speech.unload_all()
    ss.clear()


# --------------------------------------------------------------------------- #
# The sanitizer
# --------------------------------------------------------------------------- #
class TestDictationContext:
    @pytest.mark.parametrize(
        "raw", [None, 12, 1.5, ["a", "b"], {"t": "x"}, b"bytes", True]
    )
    def test_only_a_string(self, raw):
        assert speech.dictation_context(raw) == ""

    def test_blank_is_nothing(self):
        assert speech.dictation_context("") == ""
        assert speech.dictation_context(" \n\t ") == ""

    def test_whitespace_collapses(self):
        got = speech.dictation_context("  Fix the\n\nfailing   test,\tthen ")
        assert got == "Fix the failing test, then"

    def test_control_and_invisible_characters_go(self):
        raw = "run\x00 the\x1b[31m tests​ now ok\x7f﻿"
        got = speech.dictation_context(raw)
        assert got == "run the [31m tests now ok"
        assert got.isprintable()

    def test_a_short_context_is_kept_whole(self):
        text = "So the accurate pass runs once at the end."
        assert speech.dictation_context(text) == text

    def test_keeps_the_end_cut_at_a_word_boundary(self):
        words = ["word%03d" % i for i in range(100)]  # 799 characters
        got = speech.dictation_context(" ".join(words))
        assert len(got) <= speech.CONTEXT_CHARS == 300
        assert got.endswith("word099")  # the words this phrase follows
        # Whole words only: the first one is complete, not a fragment.
        assert got.split()[0] in words
        assert all(w in words for w in got.split())
        assert len(got) > speech.CONTEXT_CHARS - len("word000 ")

    def test_a_cut_on_a_space_keeps_the_word_after_it(self):
        text = "x" * 50 + " " + "y" * 299
        assert speech.dictation_context(text) == "y" * 299

    def test_one_huge_word_is_cut_as_is(self):
        assert speech.dictation_context("z" * 1000) == "z" * 300

    def test_the_limit_is_a_parameter(self):
        assert speech.dictation_context("alpha beta gamma", limit=10) == "beta gamma"
        assert speech.dictation_context("alpha beta gamma", limit=9) == "gamma"
        assert speech.dictation_context("alpha", limit=0) == ""


# --------------------------------------------------------------------------- #
# LocalSTT.final_transcriber
# --------------------------------------------------------------------------- #
@pytest.fixture
def model(monkeypatch):
    """A recording faster-whisper whose ``transcribe`` takes the new
    arguments (``hotwords``, ``temperature``, ``max_new_tokens``)."""
    seen: dict = {"calls": [], "names": []}

    class _Model:
        def __init__(self, name, **kw):
            seen["names"].append(name)

        def transcribe(
            self, audio, hotwords=None, temperature=None, max_new_tokens=None, **kw
        ):
            call = dict(kw)
            for key, val in (
                ("hotwords", hotwords),
                ("temperature", temperature),
                ("max_new_tokens", max_new_tokens),
            ):
                if val is not None:
                    call[key] = val
            seen["calls"].append(call)
            return iter([SimpleNamespace(text=" Then open a pull request.")]), None

    fw = SimpleNamespace(WhisperModel=_Model)

    def frombuffer(raw, dtype):
        out = array("f")
        out.frombytes(raw)
        return out

    np = SimpleNamespace(float32="float32", frombuffer=frombuffer)
    monkeypatch.setattr(speech, "_installed", lambda name: name == "faster_whisper")
    monkeypatch.setattr(
        speech, "_import", lambda name: {"faster_whisper": fw, "numpy": np}[name]
    )
    return seen


class TestFinalPrompt:
    def test_the_context_is_the_initial_prompt(self, model):
        run = speech.LocalSTT().final_transcriber(
            "en", "MindFlock, worktree", context="Rebase this branch onto main,"
        )
        assert run(array("f", [0.0] * 160)) == "Then open a pull request."
        (kw,) = model["calls"]
        assert kw["initial_prompt"] == "Rebase this branch onto main,"
        assert kw["hotwords"] == "MindFlock, worktree"  # the hints still pass
        assert kw["temperature"] == 0.0 and kw["beam_size"] == speech.FINAL_BEAM
        assert kw["condition_on_previous_text"] is False and kw["vad_filter"] is True

    def test_the_context_is_sanitized_and_bounded(self, model):
        raw = "\x00ignored  " + " ".join("w%d" % i for i in range(200)) + "\n"
        speech.LocalSTT().final_transcriber("en", context=raw)(array("f", [0.0]))
        prompt = model["calls"][0]["initial_prompt"]
        assert prompt == speech.dictation_context(raw)
        assert len(prompt) <= speech.CONTEXT_CHARS and prompt.endswith("w199")
        assert "\x00" not in prompt and "\n" not in prompt

    def test_no_context_is_unchanged(self, model):
        speech.LocalSTT().final_transcriber("en", "MindFlock")(array("f", [0.0]))
        speech.LocalSTT().final_transcriber("en", "MindFlock", context="")(
            array("f", [0.0])
        )
        speech.LocalSTT().final_transcriber("en", "MindFlock", context=None)(  # type: ignore[arg-type]
            array("f", [0.0])
        )
        a, b, c = model["calls"]
        assert a == b == c
        assert "initial_prompt" not in a and a["hotwords"] == "MindFlock"

    def test_the_token_cap_fits_beside_the_prompt(self, model):
        hints = ", ".join("hint%d" % i for i in range(60))[: speech.HINT_CHARS]
        context = " ".join("ctx%d" % i for i in range(120))
        seconds = 30
        speech.LocalSTT().final_transcriber("en", hints, context=context)(
            array("f", [0.0] * (RATE * seconds))
        )
        kw = model["calls"][0]
        used = (
            speech._PROMPT_OVERHEAD
            + speech._prompt_tokens(None, kw["hotwords"])
            + speech._prompt_tokens(None, kw["initial_prompt"])
        )
        assert used + kw["max_new_tokens"] <= speech.WHISPER_MAX_LENGTH
        assert kw["max_new_tokens"] < speech.final_tokens(seconds)  # it did cut

    def test_a_short_phrase_keeps_its_own_cap(self, model):
        speech.LocalSTT().final_transcriber("en", "x", context="a b c")(
            array("f", [0.0] * RATE)
        )
        assert model["calls"][0]["max_new_tokens"] == speech.final_tokens(1.0)


class TestPromptTokens:
    def test_counts_through_the_models_tokenizer(self):
        class _Tok:
            def encode(self, text, add_special_tokens=True):
                assert add_special_tokens is False and text.startswith(" ")
                return SimpleNamespace(ids=list(range(len(text.split()))))

        m = SimpleNamespace(hf_tokenizer=_Tok())
        assert speech._prompt_tokens(m, "one two three") == 3
        assert speech._prompt_tokens(m, "") == 0

    def test_estimates_without_one_and_caps_at_half(self):
        assert speech._prompt_tokens(object(), "abcd") == 2
        assert (
            speech._prompt_tokens(object(), "x" * 2000)
            == speech.WHISPER_MAX_LENGTH // 2 - 1
        )

    def test_room_is_at_least_one(self):
        huge = "x" * 2000
        assert speech._prompt_room(object(), huge, huge) == 1


# --------------------------------------------------------------------------- #
# Every accurate pass of a stream carries it
# --------------------------------------------------------------------------- #
def _secs(s, level=0.1):
    return array("f", [level] * int(s * RATE))


class _Fast:
    """The fast tier: the same words every pass."""

    def __init__(self, text):
        self.words = [(w, i * 0.3, i * 0.3 + 0.2) for i, w in enumerate(text.split())]

    def __call__(self, audio, prompt):
        return list(self.words)


class TestStreamPasses:
    def test_the_early_and_the_final_pass_both_get_the_context(self, model):
        accurate = speech.LocalSTT().final_transcriber(
            "en", "MindFlock", context="Rebase this branch onto main,"
        )
        st = ss.Stream(_Fast("then open a pull"), accurate)
        st.feed(_secs(1.5))
        st.feed(_secs(0.4, level=0.0))  # the quiet that starts the early pass
        st.feed(_secs(1.0))  # more speech: the early pass no longer covers it
        out = st.finish()
        assert out["final"] == "Then open a pull request."
        assert len(model["calls"]) >= 1
        assert all(
            c["initial_prompt"] == "Rebase this branch onto main,"
            for c in model["calls"]
        )


# --------------------------------------------------------------------------- #
# POST /api/voice/stream
# --------------------------------------------------------------------------- #
class _Local(speech.Backend):
    """A ready streaming backend recording what its accurate pass was built
    with; ``old`` drops the ``context`` parameter (a backend from before)."""

    id, kind, label, streams = "local", "stt", "local", True

    def __init__(self):
        self.engine = "whisper"
        self.finals: list = []

    def ready(self):
        return True, "ready"

    def stream_engine(self, lang=""):
        return self.engine

    def stream_transcriber(self, lang="", hints=""):
        return lambda audio, prompt: []

    def final_transcriber(self, lang="", hints="", **kw):
        self.finals.append((lang, hints, kw))

        def accurate(audio):
            return "whisper text"

        accurate.context = kw.get("context", "")  # type: ignore[attr-defined]
        return accurate


@pytest.fixture
def local(monkeypatch):
    backend = _Local()
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [backend], "tts": []})
    monkeypatch.setattr(speech, "stt_loaded", lambda: True)
    opened: list = []

    def open_stream(hints="", accurate=None, client="", engine_=None):
        opened.append(accurate)
        return ss.open_stream(lambda a, p: [], None, client)

    monkeypatch.setattr(routes._moonshine, "open_stream", open_stream)
    return SimpleNamespace(backend=backend, opened=opened)


class TestStreamOpen:
    def test_the_context_reaches_the_accurate_pass(self, local):
        r = client.post(
            "/api/voice/stream",
            json={"lang": "en-US", "client": "d-1", "context": "Rebase this  branch\n"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["context"] == len("Rebase this branch")
        ((lang, hints, kw),) = local.backend.finals
        assert lang == "en-US" and hints
        assert kw == {"context": "Rebase this branch"}

    def test_moonshine_gets_the_same_accurate_pass(self, local):
        local.backend.engine = speech.ENGINE_MOONSHINE
        r = client.post(
            "/api/voice/stream", json={"lang": "en", "context": "So the pass"}
        )
        assert r.json()["engine"] == "moonshine"
        (accurate,) = local.opened
        assert accurate.context == "So the pass"

    def test_a_long_context_is_bounded(self, local):
        raw = " ".join("w%d" % i for i in range(400))
        body = client.post("/api/voice/stream", json={"context": raw}).json()
        got = local.backend.finals[0][2]["context"]
        assert body["context"] == len(got) <= speech.CONTEXT_CHARS
        assert got == speech.dictation_context(raw) and got.endswith("w399")

    @pytest.mark.parametrize("raw", [None, 5, ["a"], {"x": 1}, "", "   ", "\x00\x01"])
    def test_no_usable_context_is_todays_call(self, local, raw):
        body = {"lang": "en"} if raw is None else {"lang": "en", "context": raw}
        r = client.post("/api/voice/stream", json=body)
        assert r.status_code == 200 and r.json()["context"] == 0
        # Called exactly as before: no context keyword at all.
        assert local.backend.finals == [("en", local.backend.finals[0][1], {})]

    def test_no_body_still_opens(self, local):
        r = client.post("/api/voice/stream")
        assert r.status_code == 200 and r.json()["context"] == 0
