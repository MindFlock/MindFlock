"""Flow polish (fp-flight-api): the cookbook's lint knows each kind's options
and the ids the server gives a start's stops; its examples stay valid against
the strict check; Verify reads the way the vocabulary says."""

from __future__ import annotations

import pytest

from backend.web.core import flight_cookbook as C
from backend.web.core import flights as F

DTO = F.kinds_dto()
FIELDS = C.kind_fields(DTO)
LABELS = {k["kind"]: k["label"] for k in DTO["kinds"]}


def _lint(steps, **kw):
    return C.lint(steps, known=F.STEP_KINDS, fields=FIELDS, labels=LABELS, **kw)


def test_an_unknown_option_is_an_error_in_the_servers_words():
    (x,) = C.errors(_lint([{"kind": "checks", "config": {"timeout": 120}}]))
    assert x["text"] == (
        "unknown option 'timeout' on a CI checks stop — it has: timeout_minutes"
    )
    assert x["field"] == "steps[0].config.timeout"
    (y,) = C.errors(_lint([{"kind": "agent", "config": {"promt": "go"}}]))
    assert y["text"].startswith(
        "unknown option 'promt' on an Agent stop — it has: prompt"
    )


@pytest.mark.parametrize(
    "kind, legacy",
    [("wait", "minutes"), ("verify", "wait_live"), ("loop", "every_minutes")],
)
def test_legacy_option_names_still_read(kind, legacy):
    steps = [{"kind": "agent"}, {"kind": kind, "config": {legacy: 1}}]
    if kind == "loop":
        steps[1]["config"]["back_to"] = "s1"
    assert not [x for x in C.errors(_lint(steps)) if "unknown option" in x["text"]]


def test_without_fields_the_option_check_is_off():
    assert not C.errors(C.lint([{"kind": "checks", "config": {"timeout": 1}}]))


def test_a_loop_back_to_an_author_id_the_server_renumbers():
    steps = [
        {"id": "build", "kind": "agent", "config": {"prompt": "x"}},
        {"kind": "loop", "config": {"back_to": "build"}},
    ]
    # A saved route keeps its ids: fine.
    assert not C.errors(_lint(steps))
    # A start drops them and numbers s1, s2…: say which id it will be.
    (x,) = C.errors(_lint(steps, renumber=0))
    assert "use back_to='s1'" in x["text"] and x["stop"] == "s2"
    # After a saved route's stops, the next free sN.
    base = [{"id": "s1", "kind": "agent"}, {"id": "s4", "kind": "notify"}]
    (y,) = C.errors(_lint(base + steps, renumber=2))
    assert "use back_to='s5'" in y["text"]
    assert not C.errors(
        _lint(
            base + [steps[0], {"kind": "loop", "config": {"back_to": "s5"}}], renumber=2
        )
    )


def test_every_example_passes_the_strict_check_and_the_option_check():
    for r in C.RECIPES:
        F.normalize_steps([dict(s) for s in r["steps"]], strict=True)
        assert not C.errors(_lint(r["steps"], renumber=0)), r["id"]


def test_an_example_shows_verify_as_the_vocabulary_describes_it():
    verifies = [(r, s) for r in C.RECIPES for s in r["steps"] if s["kind"] == "verify"]
    assert verifies
    whos = {s["config"].get("who") for _, s in verifies}
    assert whos <= set(F.VERIFY_WHO) and len(whos) >= 2
    for r, s in verifies:
        kinds = [x["kind"] for x in r["steps"]]
        assert "agent" in kinds[: kinds.index("verify")], r["id"]
        assert "behavior" not in (s.get("title") or "").lower()
        assert "behavior" not in r["when"]
    text = " ".join(str(v) for v in C.STOPS["verify"].values())
    assert "behavior" not in text
    for words in (
        "both = agent, then you",
        "agent = agent only",
        "must give evidence",
        "you = agent, then your sign-off",
    ):
        assert words in text, words


def test_verify_sentence_says_sign_off_for_you():
    assert C.verify_sentence({"who": "you"}) == (
        "Verify on this branch · agent, then your sign-off · right away"
    )


def test_options_line_comes_from_the_kind_definition():
    ship = next(k for k in DTO["kinds"] if k["kind"] == "ship")
    assert C.options_line(ship) == (
        "lane=push (commit | push | pr | merge); ask_first=false; message"
    )
    verify = next(k for k in DTO["kinds"] if k["kind"] == "verify")
    line = C.options_line(verify)
    assert "who=both (both = Agent, then me | agent = Agent only | you = " in line
    assert "ready=now (now = " in line and "where=branch" in line
    assert C.options_line({"kind": "x", "fields": []}) == "none"
    loop = next(k for k in DTO["kinds"] if k["kind"] == "loop")
    assert "back_to (an earlier stop id, or agent)" in C.options_line(loop)


def test_stop_line_puts_the_served_options_first():
    line = C.stop_line("checks", "CI checks", "timeout_minutes=60")
    assert line.startswith("checks (CI checks) · options: timeout_minutes=60 · use:")
    assert " ·  · " not in line
    # The one-shot prompt (no options) keeps the cookbook's words.
    assert "timeout_minutes=60 · waits" in C.stop_line("checks")
