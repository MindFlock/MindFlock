"""Spoken text (``core.spoken_text``): the fixture
``tests/unit/data/spoken_text.json`` — one rule table, three implementations
— through the Python module, with every case idempotent; the table pinned
identical in the Python, the TypeScript (``frontend/src/lib/spokenText.ts``)
and the static twin (``backend/web/static/spoken-text.js``, run under node
when node is here); the twin's answers for the whole fixture; the
``voice.pronounce`` normaliser's bounds; and the 2 ms budget.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from backend.web.core import spoken_text as st

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "unit" / "data" / "spoken_text.json"
PY = ROOT / "backend" / "web" / "core" / "spoken_text.py"
TS = ROOT / "frontend" / "src" / "lib" / "spokenText.ts"
TWIN = ROOT / "backend" / "web" / "static" / "spoken-text.js"

CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _ids(cases):
    return ["[%s] %s" % (c["family"], c["name"]) for c in cases]


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_fixture(case):
    got = st.shape(case["in"], case.get("overrides"))
    assert got == case["out"]
    # Idempotent: shaping shaped text changes nothing.
    assert st.shape(got, case.get("overrides")) == got


def test_fixture_covers_every_rule_family():
    families = {c["family"] for c in CASES}
    assert {
        "heteronyms",
        "tech",
        "numbers",
        "identifiers",
        "paths",
        "markup",
        "overrides",
        "bounds",
        "say",
    } <= families
    # The owner's exact complaint is the first case.
    assert CASES[0]["in"] == "Once it is done everything goes live."
    assert CASES[0]["out"] == "Once it is done everything goes lyve."


# --------------------------------------------------------------------------- #
# One table, three copies
# --------------------------------------------------------------------------- #
def _between(text: str, start: str, end: str) -> str:
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.S)
    assert m, "markers %r / %r missing" % (start, end)
    return m.group(1)


def _python_tables() -> dict:
    body = _between(PY.read_text(encoding="utf-8"), "# @@tables\n", "# @@end")
    body = body.strip()
    assert body.startswith("TABLES = ")
    return json.loads(body[len("TABLES = ") :])


def _ts_tables() -> dict:
    body = _between(
        TS.read_text(encoding="utf-8"), "/* @@tables */", "/* @@end */"
    ).strip()
    head = "export const TABLES: Tables = "
    assert body.startswith(head) and body.endswith(";")
    return json.loads(body[len(head) : -1])


def test_the_python_literal_is_the_live_table():
    assert _python_tables() == st.TABLES


def test_the_typescript_carries_the_same_table():
    assert _ts_tables() == st.TABLES


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def _run_twin(script: str) -> str:
    out = subprocess.run(
        [_node(), "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
        env={**os.environ, "TWIN": str(TWIN), "FIXTURE": str(FIXTURE)},
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


_TWIN_LOAD = (
    "const fs=require('fs');const win={};"
    "new Function('window',fs.readFileSync(process.env.TWIN,'utf8'))(win);"
    "const T=win.MindFlockSpokenText;"
)


def test_the_static_twin_carries_the_same_table_and_the_window_name():
    got = json.loads(
        _run_twin(_TWIN_LOAD + "process.stdout.write(JSON.stringify(T.TABLES));")
    )
    assert got == st.TABLES


def test_the_static_twin_answers_the_whole_fixture():
    script = _TWIN_LOAD + (
        "const cases=JSON.parse(fs.readFileSync(process.env.FIXTURE,'utf8')).cases;"
        "process.stdout.write(JSON.stringify(cases.map(c=>T.shape(c.in,c.overrides))));"
    )
    got = json.loads(_run_twin(script))
    for case, out in zip(CASES, got):
        assert out == case["out"], case["name"]


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #
def test_odd_input_is_empty():
    assert st.shape("") == ""
    assert st.shape(None) == ""
    assert st.shape(42) == ""
    assert st.shape("   ") == ""


def test_shaping_never_touches_the_caption():
    line = "Everything goes live."
    assert st.shape(line) == "Everything goes lyve."
    assert line == "Everything goes live."


def test_output_is_bounded_and_cut_at_a_space():
    out = st.shape("word " * 2000)
    assert len(out) <= st.MAX_OUT
    assert out.endswith("word")
    assert st.shape("x" * 5000) == "x" * st.MAX_OUT


def test_a_500_char_line_shapes_in_under_2ms():
    line = (
        "Once it is done everything goes live; see backend/web/core/speech.py:404 and PR #275. "
        * 6
    )[:500]
    st.shape(line)
    t0 = time.perf_counter()
    for _ in range(50):
        st.shape(line)
    assert (time.perf_counter() - t0) / 50 < 0.002


class TestNormalizePronounce:
    def test_shape_lower_case_say_trimmed_as(self):
        assert st.normalize_pronounce([{"say": " Live ", "as": " lyve "}]) == [
            {"say": "live", "as": "lyve"}
        ]

    def test_junk_is_skipped(self):
        assert st.normalize_pronounce(None) == []
        assert st.normalize_pronounce("live") == []
        assert (
            st.normalize_pronounce(
                [None, "x", {"say": 1, "as": "y"}, {"say": "a", "as": ""}]
            )
            == []
        )

    def test_bounds_and_duplicates(self):
        rows = [{"say": "w%d" % i, "as": "x"} for i in range(50)]
        assert len(st.normalize_pronounce(rows)) == st.PRONOUNCE_MAX
        assert st.normalize_pronounce([{"say": "x" * 41, "as": "y"}]) == []
        assert st.normalize_pronounce([{"say": "x", "as": "y" * 41}]) == []
        assert st.normalize_pronounce(
            [{"say": "sql", "as": "S Q L"}, {"say": "SQL", "as": "again"}]
        ) == [{"say": "sql", "as": "S Q L"}]

    def test_markup_and_control_characters_are_refused(self):
        assert st.normalize_pronounce([{"say": "a<b", "as": "x"}]) == []
        assert st.normalize_pronounce([{"say": "ok", "as": "<b>x</b>"}]) == []
        assert st.normalize_pronounce([{"say": "ok", "as": "x\x00y"}]) == []

    def test_phrases_and_the_odd_characters_a_word_may_hold(self):
        assert st.normalize_pronounce([{"say": "pull   request", "as": "P R"}]) == [
            {"say": "pull request", "as": "P R"}
        ]
        assert st.normalize_pronounce(
            [{"say": "c#", "as": "C sharp"}, {"say": "node.js", "as": "node J S"}]
        ) == [
            {"say": "c#", "as": "C sharp"},
            {"say": "node.js", "as": "node J S"},
        ]

    def test_overrides_win_over_every_rule(self):
        rules = [{"say": "live", "as": "liv"}, {"say": "json", "as": "jason"}]
        assert st.shape("goes live with JSON", rules) == "goes liv with jason"
