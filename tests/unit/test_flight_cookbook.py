"""The stop cookbook (``backend.web.core.flight_cookbook``): one entry per
kind, recipes the real validator accepts, a lint that teaches, and a read-back
sentence — the one source both describe paths read."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from backend.web.core import flight_cookbook as C
from backend.web.core import flights as F


def test_every_kind_has_a_cookbook_entry_and_nothing_else():
    """A kind landing without an entry fails here: say when it fits."""
    assert set(C.STOPS) == set(F.STEP_KINDS)
    for kind, s in C.STOPS.items():
        for key in ("use", "avoid", "fields", "waits"):
            assert s[key].strip(), (kind, key)
        assert isinstance(s["fixable"], bool) and isinstance(s["rare"], bool)


@pytest.mark.parametrize(
    "kind", ["screenshot", "listen", "call", "budget", "checkpoint"]
)
def test_the_rare_kinds_say_plainly_when_not_to_use_them(kind):
    s = C.STOPS[kind]
    assert s["rare"]
    assert s["avoid"].startswith("almost always") or "never" in s["avoid"]
    assert "RARE" in C.stop_line(kind)


@pytest.mark.parametrize("recipe", C.RECIPES, ids=lambda r: r["id"])
def test_every_recipe_validates_and_lints_clean(recipe):
    steps = F.normalize_steps(json.loads(json.dumps(recipe["steps"])), strict=True)
    trig = recipe.get("trigger") or {}
    issues = C.lint(steps, known=F.STEP_KINDS, has_pr=trig.get("kind") == "pr")
    assert issues == [], issues
    if trig:
        F.normalize_plan(
            {"name": recipe["id"], "steps": recipe["steps"], "trigger": trig}
        )


def test_the_recipes_cover_the_common_situations():
    ids = {r["id"] for r in C.RECIPES}
    assert len(ids) >= 8
    for want in (
        "ticket_pr_tests",
        "ticket_merge_after_ok",
        "pr_review_coderabbit",
        "nightly_chores",
        "split_and_join",
        "loop_until_green",
        "verify_before_pr",
        "bot_pr_merge_green",
        "release",
    ):
        assert want in ids


def test_the_module_never_imports_flights_or_fastapi():
    """The MCP stdio server imports it: no FastAPI per session."""
    code = (
        "import sys; import backend.web.core.flight_cookbook; "
        "bad = [m for m in sys.modules if m.startswith(('fastapi', 'starlette', "
        "'backend.web.core.flights'))]; print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "[]", out.stderr


# --------------------------------------------------------------------------- #
# lint — every refusal says what to do instead
# --------------------------------------------------------------------------- #
def _texts(steps, **kw):
    return [(x["level"], x["text"]) for x in C.lint(steps, **kw)]


def _one(steps, level="error", **kw):
    got = [t for lv, t in _texts(steps, **kw) if lv == level]
    assert got, _texts(steps, **kw)
    return got[0]


def test_join_without_split():
    msg = _one([{"kind": "agent"}, {"kind": "join", "config": {"of": "spawned"}}])
    assert "add a split stop before it" in msg
    warn = _one([{"kind": "agent"}, {"kind": "join"}], level="warn")
    assert "agent stop already waits" in warn


def test_merge_before_open_pr():
    msg = _one(
        [
            {"kind": "ship", "config": {"lane": "merge"}},
            {"kind": "ship", "config": {"lane": "pr"}},
        ]
    )
    assert "goes backwards" in msg and "drop this stop or move it" in msg


def test_a_bad_lane_and_an_unknown_kind_say_what_to_use():
    assert "commit, push, pr or merge" in _one(
        [{"kind": "ship", "config": {"lane": "prod"}}]
    )
    assert "use you" in _one([{"kind": "approve"}])
    assert "use ship (lane pr)" in _one([{"kind": "pr"}])
    assert "use checks" in _one([{"kind": "chekcs"}])


def test_verify_with_no_agent_before_it():
    steps = [{"kind": "verify"}, {"kind": "ship", "config": {"lane": "pr"}}]
    assert "put the agent stop first" in _one(steps, level="warn", on_session=False)
    # On an existing session the work so far counts as the agent.
    assert not [t for t in _texts(steps) if "agent stop first" in t[1]]


def test_a_wait_for_me_that_never_says_what_to_do_and_a_loop_that_never_ends():
    assert "config.text" in _one([{"kind": "agent"}, {"kind": "you"}], level="warn")
    msg = _one(
        [{"kind": "agent"}, {"kind": "loop", "config": {"times": 0}}], level="warn"
    )
    assert "loops forever" in msg


def test_loops_go_back_to_earlier_stops():
    assert "nothing to repeat" in _one([{"kind": "loop"}])
    later = [
        {"id": "a", "kind": "agent"},
        {"id": "l", "kind": "loop", "config": {"back_to": "t"}},
        {"id": "t", "kind": "test"},
    ]
    assert "move the Loop after t" in _one(later)
    assert "stops are numbered" in _one(
        [{"kind": "agent"}, {"kind": "loop", "config": {"back_to": "s5"}}]
    )
    ok = [
        {"kind": "agent"},
        {"kind": "test"},
        {"kind": "loop", "config": {"back_to": "s1"}},
    ]
    assert not [x for x in C.lint(ok) if x["level"] == "error"]


def test_finish_is_last_and_needs_a_merge():
    assert "move it to the end" in _one([{"kind": "land"}, {"kind": "notify"}])
    assert "add ship lane=merge" in _one(
        [{"kind": "agent"}, {"kind": "land"}], level="warn"
    )


def test_reviews_and_checks_need_a_pr_unless_the_route_is_on_one():
    steps = [{"kind": "agent"}, {"kind": "reviews"}]
    assert "put ship lane=pr before it" in _one(steps, level="warn")
    assert C.lint(steps, has_pr=True) == []


def test_commands_and_urls_are_never_invented():
    assert "never invent one" in _one([{"kind": "command"}])
    assert "never invent one" in _one([{"kind": "preview"}])
    assert "ask the person" in _one([{"kind": "call", "config": {"url": ""}}])


def test_split_needs_prompts_and_something_to_wait_for_it():
    assert "one worker's task per line" in _one([{"kind": "split"}, {"kind": "join"}])
    warn = _one(
        [{"kind": "split", "config": {"prompts": "a\nb"}}, {"kind": "test"}],
        level="warn",
    )
    assert "put a join" in warn


def test_judge_conditions_need_a_judge():
    msg = _one(
        [
            {"kind": "agent"},
            {"kind": "ship", "config": {"lane": "pr"}, "when": "judge is yes"},
        ]
    )
    assert "add a judge stop" in msg


def test_on_fail_agent_where_the_agent_cannot_help():
    assert "leave on_fail as halt" in _one(
        [{"kind": "wait", "on_fail": "agent"}], level="warn"
    )
    assert C.lint([{"kind": "test", "on_fail": "agent"}]) == []


def test_field_hints_teach_the_server_refusals():
    assert "judge is" in C.field_hint("steps[2].when", "bad condition")
    assert "use you" in C.field_hint(
        "steps[0].kind", "unknown step kind 'approve' — one of …"
    )
    assert "end" in C.field_hint("steps[1].kind", "a Finish stop is the last stop")
    assert "config.lane" in C.field_hint("steps[3].config.lane", "x")
    assert C.field_hint("name", "x") == ""


# --------------------------------------------------------------------------- #
# the read-back sentence
# --------------------------------------------------------------------------- #
def test_the_sentence_reads_like_the_owner_wrote_it():
    steps = F.normalize_steps(
        [
            {"kind": "agent", "config": {"prompt": "fix the login bug"}},
            {"kind": "test", "on_fail": "agent"},
            {"kind": "ship", "config": {"lane": "pr"}},
            {"kind": "you", "config": {"text": "approve the merge"}},
            {"kind": "ship", "config": {"lane": "merge"}},
        ]
    )
    assert C.sentence(steps) == (
        "Agent: fix the login bug → Test (agent fixes failures ×2) → Open PR → "
        "waits for you: approve the merge → Merge when green"
    )


def test_the_sentence_names_loops_waits_splits_and_conditions():
    steps = [
        {"kind": "split", "config": {"prompts": "a\nb\nc"}},
        {"kind": "join", "config": {"of": "spawned"}},
        {"kind": "wait", "config": {"until": "18:00"}},
        {"kind": "loop", "config": {"back_to": "agent", "times": 2}},
        {
            "kind": "ship",
            "config": {"lane": "pr"},
            "when": "diff lines > 0",
            "halt": True,
        },
    ]
    s = C.sentence(steps)
    assert "Split into 3 workers → Join (waits for the workers)" in s
    assert "Wait until 18:00 → Loop back to the agent ×2" in s
    assert "[halts here] Open PR — only when diff lines > 0" in s
    assert C.sentence([]) == "(no stops)"


def test_the_notation_is_exact_and_short():
    line = C.notation(C.RECIPES[0]["steps"])
    assert line == 'agent(prompt="{ticket}") → test!agent → ship(lane="pr")'


def test_the_prompt_text_carries_stops_rules_and_recipes():
    p = C.prompt_text()
    for kind in F.STEP_KINDS:
        assert "- %s " % kind in p
    for r in C.RULES:
        assert r in p
    assert '"kind":"ship"' in p
