"""``tests/bench/voice_bench.py``, the voice recognition bench: pytest never
collects it (no ``test_`` prefix, no ``__init__``) and CI has none of the
local models' packages, so a syntax error or a broken scorer would land
unseen. The module is loaded with a stand-in ``numpy`` when the real one is
absent (only its synthesis needs it); just the pure scoring helpers run.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.web.core import voice_words

BENCH = Path(__file__).resolve().parents[1] / "bench" / "voice_bench.py"


def test_the_bench_compiles():
    tree = ast.parse(BENCH.read_text(), filename=str(BENCH))
    compile(tree, str(BENCH), "exec")
    names = {
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
    }
    assert {"main", "run_config", "phrases", "_norm", "_wer", "_control"} <= names


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """The bench module, loaded without touching the owner's settings file
    and without numpy when the venv has none."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    if importlib.util.find_spec("numpy") is None:
        monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(ndarray=object))
    spec = importlib.util.spec_from_file_location("voice_bench", BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestScoring:
    def test_norm_folds_numbers_case_and_punctuation(self, bench):
        assert bench._norm("Switch to Sitecheck Bot Seven, over.") == [
            "switch",
            "to",
            "sitecheck",
            "bot",
            "7",
            "over",
        ]
        assert bench._norm("shortcut-21129") == ["shortcut", "2", "1", "1", "2", "9"]
        assert bench._norm("7") == bench._norm("seven") == ["7"]
        assert bench._norm("ten") == ["1", "0"]
        assert bench._norm("") == []

    def test_wer_is_word_edits_over_the_reference(self, bench):
        assert bench._wer(["a", "b", "c"], ["a", "b", "c"]) == (0, 3)
        assert bench._wer(["a", "b", "c"], ["a", "c"]) == (1, 3)  # a deletion
        assert bench._wer(["a", "b"], ["a", "x", "b", "y"]) == (2, 2)  # insertions
        assert bench._wer(["a", "b"], ["a", "c"]) == (1, 2)  # a substitution
        assert bench._wer([], ["x"]) == (1, 0)
        ref = bench._norm("Run the tests, over.")
        assert bench._wer(ref, bench._norm("run the tests over")) == (0, 4)

    def test_a_trailing_control_word_is_judged_like_the_clients(self, bench):
        words = voice_words.effective({})
        send, end, clear, stop = (
            ("end", "send"),
            ("end", "end"),
            ("end", "clear"),
            ("end", "stop"),
        )
        assert bench._control("Fix the failing test, over.", send, words) == (
            True,
            "fix the failing test",
        )
        # "submit" counts as a send word (the clients' table has it).
        assert bench._control("fix the test submit", send, words) == (
            True,
            "fix the test",
        )
        assert bench._control("Over and out!", end, words) == (True, "")
        assert bench._control("over, out", end, words) == (True, "")
        assert bench._control("Disregard.", clear, words) == (True, "")
        assert bench._control("Stop listening", stop, words) == (True, "")
        # Not at the end: not heard where it belongs.
        assert bench._control("over the top", send, words) == (False, "over the top")
        assert bench._control("I disregard that", clear, words) == (
            False,
            "i disregard that",
        )
        # The person's own words, as the clients would read them.
        own = voice_words.effective({"send": ["go go"]})
        assert bench._control("do it, go go", send, own) == (True, "do it")
        assert bench._control("do it, over", send, own) == (False, "do it over")

    def test_the_wake_word_leads_with_two_letters_of_slack(self, bench):
        words = voice_words.effective({})
        wake = ("start", "wake")
        assert bench._control("MindFlock, how do fences work?", wake, words) == (
            True,
            "how do fences work",
        )
        assert bench._control("Mindflok how do fences work", wake, words) == (
            True,
            "how do fences work",
        )
        assert bench._control("mindflock", wake, words) == (True, "")
        assert bench._control("Mind flock how", wake, words) == (
            False,
            "mind flock how",
        )
        assert bench._control("how do fences work", wake, words) == (
            False,
            "how do fences work",
        )

    def test_pct(self, bench):
        assert bench._pct([], 0.5) == 0.0
        assert bench._pct([7.0], 0.95) == 7.0
        vals = [float(v) for v in range(10, 210, 10)]
        assert bench._pct(vals, 0.5) == 110.0
        assert bench._pct(vals, 0.95) == 190.0

    def test_the_scorers_need_no_model(self, bench):
        # Nothing in the scoring reaches numpy, piper or faster-whisper.
        for name in ("_norm", "_wer", "_control", "_close", "_pct"):
            src = inspect.getsource(getattr(bench, name))
            assert "np." not in src and "piper" not in src and "speech." not in src
