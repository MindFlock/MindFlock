"""core.voice_words: the voice keywords' defaults, how a saved set is cleaned,
the overlay every reader uses, and the regex contract the clients rely on."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from backend.config import settings as S
from backend.web.core import voice_words as vw


# --- normalize -------------------------------------------------------------------
def test_empty_and_junk_mean_all_defaults():
    for raw in (None, {}, [], "over", 3, {"send": None, "nope": ["x"]}):
        assert vw.normalize(raw) == {}


def test_phrases_are_cleaned_and_deduplicated():
    got = vw.normalize({"send": ["  Send  It ", "send it", "SEND   it", "Go’s"]})
    assert got == {"send": ["send it", "go's"]}


def test_a_lone_string_is_one_phrase():
    assert vw.normalize({"clear": "Scrap That"}) == {"clear": ["scrap that"]}


def test_invalid_phrases_are_dropped():
    got = vw.normalize(
        {
            "stop": [
                "halt!",  # punctuation
                "stop.",  # a full stop
                "a" * 41,  # too long
                "a" * 40,  # the limit
                "",  # empty
                " , ",  # nothing to say
                "(stop)",  # regex characters
                "arrête",  # not ASCII
                7,  # not a string
                "over, out",  # commas are fine
                "don't",  # so are apostrophes
                "plan 9",  # and digits
            ]
        }
    )
    assert got == {"stop": ["a" * 40, "over, out", "don't", "plan 9"]}


def test_at_most_six_phrases_per_kind():
    got = vw.normalize({"hush": ["w%d" % i for i in range(10)]})
    assert got == {"hush": ["w0", "w1", "w2", "w3", "w4", "w5"]}
    assert vw.MAX_PHRASES == 6


def test_an_empty_or_all_invalid_kind_falls_back_to_the_default():
    assert vw.normalize({"send": [], "end": ["!!!"], "clear": "disregard"}) == {}


def test_the_defaults_are_not_stored():
    assert vw.normalize(vw.DEFAULTS) == {}
    assert vw.normalize({"send": ["Over"], "wake": "MindFlock"}) == {}


@pytest.mark.parametrize(
    "raw, got",
    [
        ("Jarvis", "jarvis"),
        ("  Hey   Jarvis ", "hey jarvis"),
        ("max", "max"),
        ("ab", ""),  # too short
        ("a" * 21, ""),  # too long
        ("hey there jarvis", ""),  # three words
        ("r2d2", ""),  # digits
        ("mind-flock", ""),  # punctuation
        (["jarvis"], ""),  # not a string
        ("mindflock", ""),  # ours: not stored
    ],
)
def test_the_wake_word(raw, got):
    out = vw.normalize({"wake": raw})
    assert out.get("wake", "") == got


def test_every_default_passes_its_own_rules():
    for kind in vw.KINDS:
        assert vw._phrases(vw.DEFAULTS[kind]) == vw.DEFAULTS[kind]
        assert 1 <= len(vw.DEFAULTS[kind]) <= vw.MAX_PHRASES
    assert vw._wake(vw.DEFAULTS["wake"]) == "mindflock"


# --- effective -------------------------------------------------------------------
def test_effective_overlays_the_saved_kinds_on_the_defaults():
    got = vw.effective({"stop": ["Halt"], "wake": "jarvis", "bogus": ["x"]})
    assert got["stop"] == ["halt"] and got["wake"] == "jarvis"
    assert got["send"] == ["over"] and got["hush"] == vw.DEFAULTS["hush"]
    assert set(got) == set(vw.KINDS) | {"wake"}


def test_effective_reads_settings_or_the_voice_group():
    s = S.Settings.from_dict({"voice": {"words": {"send": ["go go"]}}})
    assert vw.effective(s)["send"] == ["go go"]
    assert vw.effective(s.voice)["send"] == ["go go"]
    assert vw.effective(S.Settings()) == vw.effective({})


def test_effective_never_hands_out_the_defaults_themselves():
    got = vw.effective({})
    got["send"].append("mutated")
    assert vw.DEFAULTS["send"] == ["over"]


# --- the setting -----------------------------------------------------------------
def test_the_setting_is_stored_normalised():
    v = S.VoiceSettings.from_dict({"words": {"send": [" GO "], "stop": []}})
    assert v.words == {"send": ["go"]}
    assert v.to_dict() == {"words": {"send": ["go"]}}
    assert S.VoiceSettings.from_dict({"words": "junk"}).words == {}
    assert S.VoiceSettings().to_dict() == {}


# --- phrase_regex ----------------------------------------------------------------
def test_longest_first_and_whole_alternation():
    src = vw.phrase_regex(["over", "over and out", "over, out"])
    assert src == "(?:over and out|over,? out|over)"
    rx = re.compile(r"^%s$" % src)
    assert rx.match("over and out") and rx.match("over, out")
    assert rx.match("over out") and rx.match("over")
    assert not rx.match("overt")


def test_specials_are_escaped():
    src = vw.phrase_regex(["a.b", "c|d", "(e)", "f*", "g/h", "x\\y"])
    rx = re.compile(r"^%s$" % src)
    assert rx.match("a.b") and not rx.match("axb")
    assert rx.match("c|d") and not rx.match("c")
    assert rx.match("(e)") and rx.match("f*") and rx.match("g/h")
    assert rx.match("x\\y")


def test_no_phrases_never_match():
    assert vw.phrase_regex([]) == "(?!)"
    assert not re.search(vw.phrase_regex([]), "anything")


def test_duplicates_and_non_strings_are_skipped():
    assert vw.phrase_regex(["hush", "hush", None, 3, ""]) == "(?:hush)"


# What JavaScript's RegExp (with or without the ``u`` flag) would refuse or
# read differently from Python's ``re``.
_NOT_JS = [
    r"\(\?<[=!]",  # lookbehind
    r"\(\?P",  # Python named groups
    r"\(\?<\w",  # named groups
    r"\(\?[aiLmsux]",  # inline flags
    r"\\[AZ]",  # Python-only anchors
    r"\\-",  # an escape the u flag refuses
    r"\\ ",  # likewise
]


def test_the_defaults_regexes_are_js_safe():
    for kind in vw.KINDS:
        src = vw.phrase_regex(vw.DEFAULTS[kind])
        for bad in _NOT_JS:
            assert not re.search(bad, src), (kind, src, bad)
        re.compile(src)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_regexes_compile_and_match_in_javascript():
    cases = {kind: vw.phrase_regex(vw.DEFAULTS[kind]) for kind in vw.KINDS}
    cases["odd"] = vw.phrase_regex(["a.b", "(e)", "g/h", "don't", "over, out"])
    script = (
        "const c = JSON.parse(process.argv[1]); const out = {};"
        "for (const k in c) { const a = new RegExp('^' + c[k] + '$', 'i');"
        " const b = new RegExp('^' + c[k] + '$', 'u');"
        " out[k] = [a.test(process.argv[2]), b.source.length > 0]; }"
        "console.log(JSON.stringify(out));"
    )
    res = subprocess.run(
        ["node", "-e", script, json.dumps(cases), "over out"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert got["end"] == [True, True]  # "over, out" with the comma dropped
    assert got["odd"] == [True, True]
    assert got["send"] == [False, True]
