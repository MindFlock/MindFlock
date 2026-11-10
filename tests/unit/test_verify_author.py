"""The AUTHOR half of Verify-as-a-walkthrough: the step schema and the prompt.

A plan is now a walkthrough an agent performs in order. Each step is
``{id, text, actor, expect?, needs?}``: ``text`` is what the walker DOES,
``expect`` what it must SEE, and ``needs`` the ids of EARLIER steps whose action
it builds on. These tests pin the schema rules (only earlier ids survive, old
plans read back unchanged, a model's ids are renumbered and its references
translated) and the prompt's FOURTH FAILURE CLASS — steps that wait on somebody
else's action ("after someone has run /sbx-ask in the last hour, search Loki").
"""

from __future__ import annotations

import json
import subprocess

import pytest

from backend.web.core import commit_message as cm
from backend.web.core import test_plans as tp


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Never the developer's real ``~/.mindflock/test_plans.json``."""
    path = tmp_path / "test_plans.json"
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(path))
    monkeypatch.setattr(tp, "_default_program", lambda: "claude")
    return path


def _plan(plan_id="sc-1", **over) -> dict:
    plan = {
        "id": plan_id,
        "title": plan_id,
        "repo_root": "/tmp/repo",
        "branch": "feature/%s" % plan_id,
        "sha": "a" * 40,
        "live_branch": "main",
        "state": "generated",
        "error": "",
        "generated_at": 100.0,
        "live_at": 0.0,
        "steps": [
            {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"},
            {"id": "s2", "text": "Look at it", "expect": "Right", "actor": "human"},
        ],
        "runs": [],
    }
    plan.update(over)
    return plan


def _block(steps, summary="It works.") -> str:
    return "<testplan>%s</testplan>" % json.dumps({"summary": summary, "steps": steps})


def _step(text, needs=None, sid=None, actor="agent", expect="seen") -> dict:
    out = {"text": text, "expect": expect, "actor": actor}
    if sid is not None:
        out["id"] = sid
    if needs is not None:
        out["needs"] = needs
    return out


# --------------------------------------------------------------------------- #
# Normalization — only EARLIER ids survive, and old plans are untouched
# --------------------------------------------------------------------------- #
def test_needs_keeps_earlier_ids_and_drops_forward_self_and_unknown_refs():
    steps = tp._normalize_steps(
        [
            {"id": "s1", "text": "a", "actor": "agent", "needs": ["s2", "s1"]},
            {"id": "s2", "text": "b", "actor": "agent", "needs": ["s1", "s1"]},
            {"id": "s3", "text": "c", "actor": "agent", "needs": ["s9", "s2", "s4"]},
            {"id": "s4", "text": "d", "actor": "agent", "needs": ["s1", "s3"]},
        ]
    )
    # s1: forward + self → nothing, and no key at all.
    assert "needs" not in steps[0]
    # s2: de-duplicated.
    assert steps[1]["needs"] == ["s1"]
    # s3: unknown s9 and forward s4 dropped.
    assert steps[2]["needs"] == ["s2"]
    assert steps[3]["needs"] == ["s1", "s3"]


@pytest.mark.parametrize("raw", ["s1", {"s1": 1}, None, 3, [None, {}, [], True]])
def test_a_needs_of_the_wrong_shape_is_no_needs(raw):
    steps = tp._normalize_steps(
        [
            {"id": "s1", "text": "a", "actor": "agent"},
            {"id": "s2", "text": "b", "actor": "agent", "needs": raw},
        ]
    )
    assert "needs" not in steps[1]


def test_a_numeric_ref_is_read_as_its_string():
    steps = tp._normalize_steps(
        [
            {"id": "1", "text": "a", "actor": "agent"},
            {"id": "2", "text": "b", "actor": "agent", "needs": [1]},
        ]
    )
    assert steps[1]["needs"] == ["1"]


def test_an_old_plan_reads_back_byte_for_byte(store):
    """Plans written before walkthroughs have no ``needs`` (and some no
    ``expect``): loading one must not change a single key of its steps."""
    old = [
        {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"},
        {"id": "s2", "text": "Look at it", "expect": "", "actor": "human"},
    ]
    tp.upsert(_plan("sc-old", steps=old))
    back = tp.get("sc-old")["steps"]
    assert [dict(s, manual=False) for s in old] == back
    assert all("needs" not in s for s in back)


def test_needs_survives_the_store_and_is_reread_through_the_earlier_rule(store):
    tp.upsert(
        _plan(
            "sc-1",
            steps=[
                {"id": "s1", "text": "Do", "expect": "x", "actor": "agent"},
                {
                    "id": "s2",
                    "text": "See",
                    "expect": "y",
                    "actor": "agent",
                    "needs": ["s1", "s3"],
                },
                {"id": "s3", "text": "More", "expect": "z", "actor": "agent"},
            ],
        )
    )
    steps = tp.get("sc-1")["steps"]
    assert steps[1]["needs"] == ["s1"]
    # Raw file agrees with the read: the forward ref is gone on disk too.
    raw = json.loads(store.read_text())["plans"]["sc-1"]["steps"]
    assert raw[1]["needs"] == ["s1"]


def test_unknown_step_keys_are_still_dropped():
    step = tp._normalize_step(
        {"text": "a", "actor": "agent", "needs": [], "bogus": 1}, 0
    )
    assert set(step) == {"id", "text", "expect", "actor", "manual"}


def test_expect_is_clamped_like_text():
    step = tp._normalize_step({"text": "a", "expect": "y" * 5000}, 0)
    assert len(step["expect"]) == tp.MAX_TEXT


# --------------------------------------------------------------------------- #
# Parsing a model's answer — ids renumbered, references translated
# --------------------------------------------------------------------------- #
def test_parse_reads_expect_and_needs_from_the_models_json():
    summary, steps = tp.parse_answer(
        _block(
            [
                _step("POST /x", sid="s1", expect="201"),
                _step("Search logs for x.created", sid="s2", needs=["s1"]),
            ]
        )
    )
    assert summary == "It works."
    assert steps[0] == {
        "id": "s1",
        "text": "POST /x",
        "expect": "201",
        "actor": "agent",
        "manual": False,
    }
    assert steps[1]["needs"] == ["s1"] and steps[1]["expect"] == "seen"


def test_a_models_own_ids_are_renumbered_and_its_refs_follow():
    """Ids key every recorded result and the regenerate merge relies on a fresh
    answer living in ``s*``, so the model's naming is translated, not kept."""
    steps = tp.parse_plan(
        _block(
            [
                _step("first", sid="trigger"),
                _step("second", sid="trigger"),  # a duplicate id
                _step("third", sid="look", needs=["trigger", "nope"]),
            ]
        )
    )
    assert [s["id"] for s in steps] == ["s1", "s2", "s3"]
    # "trigger" means the FIRST step that carried it; "nope" names nothing.
    assert steps[2]["needs"] == ["s1"]


def test_a_dropped_step_takes_its_references_with_it_and_ids_close_up():
    steps = tp.parse_plan(
        _block(
            [
                _step("setup"),  # s1 by position
                {"text": "", "actor": "agent"},  # s2 by position — dropped
                _step("check", needs=["s1", "s2"]),  # s3 by position
            ]
        )
    )
    assert [s["id"] for s in steps] == ["s1", "s2"]
    assert steps[1]["text"] == "check" and steps[1]["needs"] == ["s1"]


def test_a_model_that_numbers_needs_by_position_is_understood():
    steps = tp.parse_plan(_block([_step("a"), _step("b", needs=[1])]))
    assert steps[1]["needs"] == ["s1"]


def test_parse_drops_forward_and_self_refs_from_the_model():
    steps = tp.parse_plan(
        _block(
            [
                _step("a", sid="s1", needs=["s2"]),
                _step("b", sid="s2", needs=["s2", "s1"]),
            ]
        )
    )
    assert "needs" not in steps[0]
    assert steps[1]["needs"] == ["s1"]


def test_a_bare_array_without_ids_still_parses():
    """The pre-contract shape: no summary, no ids, no needs."""
    steps = tp.parse_plan("<testplan>%s</testplan>" % json.dumps([_step("only")]))
    assert steps == [
        {
            "id": "s1",
            "text": "only",
            "expect": "seen",
            "actor": "agent",
            "manual": False,
        }
    ]


# --------------------------------------------------------------------------- #
# Vetting — needs follows the steps it names
# --------------------------------------------------------------------------- #
def test_vetting_redirects_a_ref_to_a_dropped_duplicate_onto_the_kept_copy():
    got = tp._vet_generated(
        [
            {"id": "s1", "text": "POST /x", "expect": "201", "actor": "agent"},
            {"id": "s2", "text": "POST /x", "expect": "201", "actor": "agent"},
            {
                "id": "s3",
                "text": "Look",
                "expect": "row",
                "actor": "agent",
                "needs": ["s2"],
            },
        ]
    )
    assert [s["id"] for s in got] == ["s1", "s3"]
    assert got[1]["needs"] == ["s1"]


def test_vetting_drops_a_ref_to_a_placeholder():
    got = tp._vet_generated(
        [
            {"id": "s1", "text": "TBD", "expect": "", "actor": "agent"},
            {
                "id": "s2",
                "text": "Look",
                "expect": "row",
                "actor": "agent",
                "needs": ["s1"],
            },
        ]
    )
    assert [s["id"] for s in got] == ["s2"] and "needs" not in got[0]


def test_vetting_keeps_needs_on_a_step_it_hands_to_a_person():
    got = tp._vet_generated(
        [
            {"id": "s1", "text": "POST /x", "expect": "201", "actor": "agent"},
            {
                "id": "s2",
                "text": "Look",
                "expect": "",
                "actor": "agent",
                "needs": ["s1"],
            },
        ]
    )
    assert got[1]["actor"] == "human" and got[1]["needs"] == ["s1"]


# --------------------------------------------------------------------------- #
# add_step / edit_step / remove_step
# --------------------------------------------------------------------------- #
def test_add_step_without_needs_is_unchanged(store):
    tp.upsert(_plan())
    plan = tp.add_step("sc-1", "Mine", "seen", "agent")
    assert plan["steps"][-1] == {
        "id": "s3",
        "text": "Mine",
        "expect": "seen",
        "actor": "agent",
        "manual": True,
    }


def test_add_step_takes_needs_on_earlier_steps(store):
    tp.upsert(_plan())
    plan = tp.add_step("sc-1", "Mine", "seen", "agent", needs=["s1", "s1"])
    assert plan["steps"][-1]["needs"] == ["s1"]
    assert tp.get("sc-1")["steps"][-1]["needs"] == ["s1"]


@pytest.mark.parametrize("needs", [["s9"], "s1", [{"id": "s1"}]])
def test_add_step_refuses_needs_that_name_no_earlier_step(store, needs):
    tp.upsert(_plan())
    with pytest.raises(ValueError):
        tp.add_step("sc-1", "Mine", "seen", "agent", needs=needs)
    assert len(tp.get("sc-1")["steps"]) == 2  # nothing was appended


def test_edit_step_sets_and_clears_needs_and_keeps_the_answers(store):
    tp.upsert(
        _plan(
            runs=[
                {
                    "at": 1.0,
                    "results": {"s2": {"result": "pass", "note": "ok", "by": "human"}},
                }
            ]
        )
    )
    plan = tp.edit_step("sc-1", "s2", needs=["s1"])
    assert plan["steps"][1]["needs"] == ["s1"]
    # Needs changes what the walker does FIRST, not what is asked.
    assert plan["runs"][-1]["results"]["s2"]["result"] == "pass"
    plan = tp.edit_step("sc-1", "s2", needs=[])
    assert "needs" not in plan["steps"][1]


def test_edit_step_refuses_a_need_on_a_later_step_or_itself(store):
    tp.upsert(_plan())
    for bad in (["s2"], ["s1"]):
        with pytest.raises(ValueError):
            tp.edit_step("sc-1", "s1", needs=bad)
    assert "needs" not in tp.get("sc-1")["steps"][0]


def test_edit_step_with_a_bad_need_changes_nothing_else(store):
    tp.upsert(_plan())
    with pytest.raises(ValueError):
        tp.edit_step("sc-1", "s1", text="Changed", needs=["s7"])
    assert tp.get("sc-1")["steps"][0]["text"] == "Run it"


def test_removing_a_step_unhooks_the_steps_that_needed_it(store):
    tp.upsert(_plan())
    tp.add_step("sc-1", "Setup", "done", "agent")
    plan = tp.add_step("sc-1", "Check", "row", "agent", needs=["s1", "s3"])
    assert plan["steps"][-1]["needs"] == ["s1", "s3"]
    plan = tp.remove_step("sc-1", "s3")
    assert plan["steps"][-1]["needs"] == ["s1"]


# --------------------------------------------------------------------------- #
# Regenerating — a kept manual step's needs never point at a rewritten step
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_regenerating_keeps_needs_between_kept_steps_and_drops_the_rest(
    tmp_path, monkeypatch
):
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    (repo / "app.py").write_text("print('x')\n")
    answer = _block([_step("POST /fresh"), _step("Look fresh", needs=["s1"])])
    real = subprocess.run

    def fake(argv, **kw):
        if list(argv)[:1] == ["git"]:
            return real(argv, **kw)
        return subprocess.CompletedProcess(argv, 0, answer.encode(), b"")

    monkeypatch.setattr(cm.subprocess, "run", fake)
    tp.upsert(
        _plan(
            "sc-1",
            repo_root=str(repo),
            state="generating",
            generated_at=0.0,
            steps=[
                {"id": "s1", "text": "Old generated", "expect": "x", "actor": "agent"},
                {
                    "id": "s2",
                    "text": "Mine A",
                    "expect": "a",
                    "actor": "agent",
                    "manual": True,
                    "needs": ["s1"],
                },
                {
                    "id": "s3",
                    "text": "Mine B",
                    "expect": "b",
                    "actor": "agent",
                    "manual": True,
                    "needs": ["s1", "s2"],
                },
            ],
        )
    )
    plan = tp.generate("sc-1", "claude", str(repo))
    by_text = {s["text"]: s for s in plan["steps"]}
    assert by_text["Look fresh"]["needs"] == ["s1"]
    mine_a, mine_b = by_text["Mine A"], by_text["Mine B"]
    # A's only need was a GENERATED step that has just been rewritten.
    assert "needs" not in mine_a
    # B keeps its need on A, under A's new id.
    assert mine_a["id"].startswith("m") and mine_b["needs"] == [mine_a["id"]]


# --------------------------------------------------------------------------- #
# The prompt — the fourth failure class
# --------------------------------------------------------------------------- #
def _prompt() -> str:
    return tp.build_generation_prompt("", "app.py | 4 +-", "@@", "feature/x")


def test_the_prompt_asks_for_a_walkthrough_with_ids_and_needs():
    prompt = _prompt()
    assert "The plan is a WALKTHROUGH" in prompt
    assert '"id", "text", "expect", "actor" and "needs"' in prompt
    assert '"needs" lists the ids of EARLIER steps' in prompt
    assert "Never a later step, never the step itself" in prompt


def test_the_prompt_forbids_steps_that_wait_on_someone_elses_action():
    """The observed failure: "On the sandbox deployment, after someone has run
    /sbx-ask in the last hour, search Loki for …" — an agent step whose input
    is a stranger's behaviour, so the checker waited on a person."""
    prompt = _prompt()
    assert "THE WALKER TRIGGERS IT ITSELF" in prompt
    assert "NEVER write a step that waits on someone else's action" in prompt
    assert "after someone has run" in prompt
    assert "Trigger it in an earlier step and name that step" in prompt
    # The on-demand rule survives, but reading the last run's evidence is NOW,
    # never phrased as a wait.
    assert "the evidence the last real run already left" in prompt
    assert "read on demand, NOW" in prompt


def test_the_prompt_never_hands_a_person_what_a_tool_can_do():
    """The second observed failure: "type the slash command /sbx-ask … in the
    sandbox Slack channel", marked human — one Slack API call."""
    prompt = _prompt()
    assert "A STEP A TOOL CAN DO IS NEVER A PERSON'S" in prompt
    assert "posting it through the Slack API" in prompt
    # A trigger only a person can perform is the human step, and what follows
    # it is the agent's — tied together with needs.
    assert "that trigger is the human step" in prompt
    assert 'agent step whose "needs" names it' in prompt
    # ...and the old rules still stand beside it.
    assert "At most 2 in a plan" in prompt


def test_the_docstring_records_the_fourth_failure_class():
    doc = tp.build_generation_prompt.__doc__
    assert "THE FOURTH FAILURE CLASS" in doc
    assert "/sbx-ask" in doc
    assert "WALKTHROUGH" in doc


def test_the_example_shows_do_expect_and_needs():
    steps = tp.parse_plan(_prompt())
    assert [s["id"] for s in steps] == ["s1", "s2", "s3", "s4"]
    assert all(s["text"] and s["expect"] for s in steps)
    # The log search and the person's look both build on the walker's own
    # request, instead of waiting for somebody to have made one.
    assert steps[2]["needs"] == ["s1"] and steps[3]["needs"] == ["s1"]
    assert "needs" not in steps[0] and "needs" not in steps[1]
    for step in steps:
        low = step["text"].lower()
        assert "after someone" not in low and "within the last hour" not in low
        assert tp._norm_example(step["text"]) in tp._EXAMPLE_TEXTS


# --------------------------------------------------------------------------- #
# results_markdown — the evidence a ✅ was judged against
# --------------------------------------------------------------------------- #
def test_results_markdown_shows_expect_under_its_step():
    md = tp.results_markdown(
        _plan(
            steps=[
                {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"},
                {"id": "s2", "text": "Look", "expect": "", "actor": "human"},
            ],
            runs=[{"at": 1.0, "results": {"s1": {"result": "pass", "note": "ok"}}}],
        )
    )
    lines = md.splitlines()
    at = lines.index("- ✅ pass · agent · Run it — ok")
    assert lines[at + 1] == "  - expect: It runs"
    # No expect, no sub-line.
    assert lines[-1] == "- ⬜ open · human · Look"
