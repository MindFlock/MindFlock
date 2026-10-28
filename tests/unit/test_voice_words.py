"""core.voice_words: the voice keywords' defaults, how a saved set is cleaned,
the overlay every reader uses, the regex contract the clients rely on, and
the sound-alike tolerance (HEARD, sound_key, sounds_like, control_kind) —
pinned by tests/unit/data/voice_sounds.json, which vitest runs through the
desktop's matchers and test_mobile_voice_words.py through the phone's, so
the three can never drift."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

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
    assert set(got) == set(vw.KINDS) | {"wake", "heard"}
    # heard: the known forms of the phrases present — none for the person's.
    assert got["heard"]["stop"] == [] and got["heard"]["wake"] == []
    assert got["heard"]["send"] == list(vw.HEARD["over"])
    # An effective dict (heard and all) reads back the same.
    assert vw.effective(got) == got


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


# --- what whisper writes (HEARD) --------------------------------------------------
FIXTURE = Path(__file__).resolve().parent / "data" / "voice_sounds.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_every_heard_form_is_a_valid_phrase_and_not_a_default():
    every = [
        p for ps in vw.DEFAULTS.values() for p in (ps if isinstance(ps, list) else [ps])
    ]
    for phrase, forms in vw.HEARD.items():
        assert phrase in every, phrase  # forms belong to default phrases only
        for f in forms:
            assert vw._phrases([f]) == [f], (phrase, f)  # clean, as served
            assert f not in every, f  # never a phrase in its own right
        assert len(forms) == len(set(forms)), phrase


def test_heard_of_follows_the_phrases_present():
    assert vw.heard_of(["over"]) == list(vw.HEARD["over"])
    assert vw.heard_of(["roger"]) == []
    # A phrase moved to another kind keeps its forms; one dropped loses them.
    assert vw.heard_of(["quiet", "roger"]) == list(vw.HEARD["quiet"])
    # A form that is itself one of the phrases is not repeated.
    assert "stock" not in vw.heard_of(["stop", "stock"])
    # Bounded.
    assert len(vw.heard_of([p for p in vw.HEARD])) <= vw.HEARD_MAX
    assert vw.HEARD_MAX == 24


def test_heard_is_served_for_every_kind_and_the_wake_word():
    got = vw.heard(vw.effective({}))
    assert set(got) == set(vw.KINDS) | {"wake"}
    assert got["wake"] == list(vw.HEARD["mindflock"])
    assert got["end"][:2] == ["over an out", "over in out"]
    # "over, out" brings its own; the two end phrases' forms are both there.
    assert "ova out" in got["end"]
    assert vw.heard(vw.effective({"wake": "jarvis"}))["wake"] == []


def test_heard_is_never_stored():
    assert vw.normalize(vw.effective({})) == {}
    assert "heard" not in vw.normalize({"send": ["roger"], "heard": {"send": ["x"]}})


# --- the sound of a phrase --------------------------------------------------------
def test_sound_key_pins():
    for text, key in _fixture()["keys"].items():
        assert vw.sound_key(text) == key, text


def test_sound_key_ignores_case_punctuation_and_spaces():
    assert (
        vw.sound_key("Over, out!")
        == vw.sound_key("over out")
        == vw.sound_key("overout")
    )
    assert vw.sound_key("Don’t") == vw.sound_key("dont")
    assert vw.sound_key("") == "" and vw.sound_key("...") == ""


def test_sounds_like_pins():
    for a, b, want in _fixture()["alike"]:
        assert vw.sounds_like(a, b) is want, (a, b)


@pytest.mark.parametrize(
    "said, phrase, want",
    [
        # The margin shrinks with the phrase: none below five letters.
        ("ovr", "over", False),
        ("stpo", "stop", False),
        ("halt", "halt", True),
        ("rodger", "roger", True),  # five letters: one
        ("rodjer", "roger", False),  # two is too many
        ("disregards", "disregard", True),  # eight up: two
        ("disregardes", "disregard", True),
        ("disregardess", "disregard", False),
        # Same letters, other sound: never.
        ("oven", "over", False),
        ("quilt", "quiet", False),
        # The whole utterance, run together: a split or a join is free.
        ("rod ger", "roger", True),
        ("overandout", "over and out", True),
        # A homophone respelt, about as long, from eight letters; never a
        # phrase cut short.
        ("sight check", "sitecheck", True),
        ("over and", "over and out", False),
        ("roger and", "roger and out", False),
    ],
)
def test_sounds_like_margin(said, phrase, want):
    assert vw.sounds_like(said, phrase) is want


def test_sound_tail_reads_the_last_words_only():
    assert vw.sound_tail("fix the bug rodger", ["roger"]) == ("fix the bug", "roger")
    assert vw.sound_tail("fix it, Rojer.", ["roger"]) == ("fix it", "roger")
    assert vw.sound_tail("fix it rod ger", ["roger"]) == ("fix it", "roger")
    assert vw.sound_tail("Rodger", ["roger"]) == ("", "roger")
    # Longest phrase first: "roger an out" is the end phrase, not "roger".
    assert vw.sound_tail("ship it roger an out", ["roger and out", "roger"]) == (
        "ship it",
        "roger and out",
    )
    # A tail word after it, when allowed.
    assert vw.sound_tail("toss it scrap dat that", ["scrap that"], ("that",)) == (
        "toss it",
        "scrap that",
    )
    assert vw.sound_tail("toss it scrap dat that", ["scrap that"]) is None
    # Mid-sentence never; nothing never.
    assert vw.sound_tail("rodger that is a flag", ["roger"]) is None
    assert vw.sound_tail("", ["roger"]) is None
    assert vw.sound_tail("fix it", []) is None


# --- one finished phrase, as every client reads it --------------------------------
def test_the_fixture_is_what_the_server_serves():
    f = _fixture()
    assert f["words"] == vw.effective({})
    custom = {k: v for k, v in f["custom"].items() if k != "heard"}
    assert f["custom"] == vw.effective(custom)
    assert f["custom"]["heard"] == {k: [] for k in list(vw.KINDS) + ["wake"]}
    assert len(f["cases"]) >= 80
    kinds = {c["kind"] for c in f["cases"]}
    assert kinds == {"send", "end", "clear", "stop", "hush", None}


def test_control_kind_reads_every_fixture_case():
    f = _fixture()
    for c in f["cases"]:
        words = f["custom"] if c["words"] == "custom" else f["words"]
        assert vw.control_kind(c["text"], words) == c["kind"], c


def test_control_kind_with_no_words_is_the_defaults():
    assert vw.control_kind("fix it ova") == "send"
    assert vw.control_kind("fix it ova", None) == vw.control_kind("fix it ova", {})
    assert vw.control_kind("") is None and vw.control_kind("...") is None


def test_control_kind_order_hush_then_stop_then_radio():
    # The hush is the whole phrase; a stop phrase ends anything; the end
    # phrase before a stop phrase is the stop's business (the clients send).
    assert vw.control_kind("be quiet") == "hush"
    assert vw.control_kind("fix it, be quiet") is None
    assert vw.control_kind("over and out, that's all") == "stop"
    assert vw.control_kind("fix it stop") is None  # one word: the router's
    assert vw.control_kind("stop listening") == "stop"
