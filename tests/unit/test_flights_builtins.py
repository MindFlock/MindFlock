"""Round 15's built-in flyways and presets (docs/flights.md "Round 15", "Built-in
flyways and presets"): seeded after the original four, shaped as documented,
valid, resettable, and never in the way of the depth cut."""

import re

import pytest

from backend.web.core import flight_triggers as FT
from backend.web.core import flights as F
from backend.web.core import template_schedule as TS

NOW = 1_800_000_000.0

ORIGINAL = ["builtin:ticket", "builtin:pr", "builtin:schedule", "builtin:verify"]
FLYWAYS = [
    "builtin:botpr",
    "builtin:redmain",
    "builtin:nightly",
    "builtin:release",
    "builtin:bugreport",
]
PRESETS = {
    "builtin:preset-docs": "Update the docs",
    "builtin:preset-tests": "Write tests",
    "builtin:preset-security": "Security pass",
    "builtin:preset-selfreview": "Self-review",
    "builtin:preset-explain": "Explain the diff",
}


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    yield tmp_path / "flights"


def shape(pid):
    p = F.get_plan(pid)
    return [(s["kind"], s["title"]) for s in p["steps"]]


def lanes(pid):
    return [
        s["config"]["lane"] for s in F.get_plan(pid)["steps"] if s["kind"] == "ship"
    ]


def sentences(text):
    return [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]


def test_the_new_built_ins_come_after_the_original_four_in_order():
    assert list(F.BUILTIN_IDS) == ORIGINAL + FLYWAYS + list(PRESETS)
    plans = F.list_plans()
    assert [p["id"] for p in plans[: len(F.BUILTIN_IDS)]] == list(F.BUILTIN_IDS)
    assert all(p["builtin"] for p in plans[: len(F.BUILTIN_IDS)])


def test_the_depth_cut_still_starts_from_the_original_four():
    assert F.plan_for_depth("ticket", "pr") == "builtin:ticket@pr"
    assert F.plan_for_depth("issue", "merge") == "builtin:ticket@merge"
    assert F.plan_for_depth("pr", "push") == "builtin:pr@push"
    assert F.plan_for_depth("schedule", "commit") == "builtin:schedule@commit"
    cut = F.get_plan("builtin:ticket@pr")
    assert [s["kind"] for s in cut["steps"]] == ["agent", "ship", "ship", "ship"]
    assert cut["steps"][0]["title"] == "Agent works the ticket"


def test_bot_pr():
    p = F.get_plan("builtin:botpr")
    assert p["name"] == "Bot PR" and p["trigger"]["kind"] == "pr"
    assert shape("builtin:botpr") == [
        ("agent", "Agent reads it"),
        ("test", "Test"),
        ("sync", "Sync with main"),
        ("checks", "CI checks"),
        ("ship", "Merge"),
    ]
    assert lanes("builtin:botpr") == ["merge"]
    assert not any(s["kind"] == "you" for s in p["steps"])  # no perch


def test_red_main_waits_for_its_repo():
    p = F.get_plan("builtin:redmain")
    assert p["name"] == "Red main"
    assert p["trigger"]["kind"] == "main_red" and p["trigger"]["repo"] == ""
    assert shape("builtin:redmain") == [
        ("agent", "Agent investigates"),
        ("test", "Test"),
        ("ship", "Commit"),
        ("ship", "Push"),
        ("ship", "Open PR"),
        ("you", "Look at the fix"),
    ]
    # Seeded without a repo: the trigger loop leaves it alone …
    assert "builtin:redmain" not in [x["id"] for x in FT.watching_plans()]
    # … and a save must name one (the owner picks it in the inspector).
    with pytest.raises(F.PlanError) as err:
        F.normalize_plan(p, strict=True)
    assert err.value.field == "trigger.repo"


def test_red_main_with_a_repo_is_watched(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    p = F.get_plan("builtin:redmain")
    p["trigger"]["repo"] = str(repo)
    saved = F.save_plan(p, now=NOW)
    assert saved["trigger"]["repo"] == str(repo)
    assert "builtin:redmain" in [x["id"] for x in FT.watching_plans()]


def test_nightly_chores_is_seeded_paused_at_three():
    p = F.get_plan("builtin:nightly")
    assert p["name"] == "Nightly chores"
    trig = p["trigger"]
    assert trig["kind"] == "schedule" and trig["paused"] is True
    assert trig["schedule"]["at"] == "03:00" and trig["schedule"]["days"] == "daily"
    assert shape("builtin:nightly") == [
        ("agent", "Agent updates deps"),
        ("test", "Test"),
        ("ship", "Commit"),
        ("ship", "Push"),
        ("ship", "Open PR"),
    ]
    row = next(
        r for r in TS.flight_plan_rows(F.list_plans()) if r["name"] == "builtin:nightly"
    )
    assert row["schedule"]["paused"] is True  # fires nothing until switched on


def test_release_ends_with_land():
    p = F.get_plan("builtin:release")
    assert p["name"] == "Release" and p["trigger"]["kind"] == "manual"
    assert shape("builtin:release") == [
        ("agent", "Agent writes the notes"),
        ("ship", "Commit"),
        ("ship", "Push"),
        ("ship", "Open PR"),
        ("checks", "CI checks"),
        ("you", "Approve the release"),
        ("ship", "Merge"),
        ("agent", "Agent tags and publishes"),
        ("land", "Land"),
    ]
    assert lanes("builtin:release") == ["commit", "push", "pr", "merge"]


def test_bug_report_writes_the_failing_test_first():
    p = F.get_plan("builtin:bugreport")
    assert p["name"] == "Bug report" and p["trigger"]["kind"] == "ticket"
    assert [k for k, _ in shape("builtin:bugreport")] == [
        "agent",
        "test",
        "agent",
        "test",
        "ship",
        "ship",
        "verify",
        "ship",
        "reviews",
        "checks",
        "you",
        "ship",
        "ticket",
    ]
    s = p["steps"]
    assert s[0]["title"] == "Write a failing test that reproduces the report"
    assert s[1]["title"] == "Write the failing test first"
    assert s[1]["config"]["expect"] == "fail" and s[1]["on_fail"] == "agent"
    assert s[2]["title"] == "Make it pass"
    assert s[3]["config"]["expect"] == "pass"
    assert lanes("builtin:bugreport") == ["commit", "push", "pr", "merge"]


@pytest.mark.parametrize("pid", FLYWAYS + list(PRESETS))
def test_every_new_built_in_is_a_valid_plan_with_real_prompts(pid):
    p = F.get_plan(pid)
    if p["trigger"]["kind"] in F.MAIN_TRIGGERS:
        p["trigger"]["repo"] = ""  # checked in test_red_main_waits_for_its_repo
        p["trigger"]["kind"] = "manual"
    assert F.normalize_plan(p, strict=True)["steps"] == p["steps"]
    assert p["description"]
    for s in p["steps"]:
        if s["kind"] == "agent":
            assert len(sentences(s["config"]["prompt"])) >= 3, s["title"]
            assert s["config"]["join"] is True


@pytest.mark.parametrize("pid,name", list(PRESETS.items()))
def test_presets_are_one_agent_step(pid, name):
    p = F.get_plan(pid)
    assert p["name"] == name and p["builtin"]
    assert p["trigger"]["kind"] == "manual"
    assert [(s["kind"], s["title"]) for s in p["steps"]] == [("agent", name)]
    prompt = p["steps"][0]["config"]["prompt"]
    assert 3 <= len(sentences(prompt)) <= 5
    assert "{branch}" in prompt
    filled = F.fill_prompt(prompt, {"branch": "feat/x", "subject": {}})
    assert "feat/x" in filled and "{branch}" not in filled


@pytest.mark.parametrize("pid", FLYWAYS + list(PRESETS))
def test_every_new_built_in_resets(pid, tmp_path):
    p = F.get_plan(pid)
    if p["trigger"]["kind"] in F.MAIN_TRIGGERS:
        (tmp_path / "r" / ".git").mkdir(parents=True)
        p["trigger"]["repo"] = str(tmp_path / "r")
    F.save_plan(dict(p, name="Mine", steps=p["steps"][:1]), now=NOW)
    assert F.get_plan(pid)["name"] == "Mine"
    back = F.reset_plan(pid)
    assert back["name"] == p["name"] and back["steps"] == p["steps"]
    assert F.delete_plan(pid) == "reset"


def test_a_preset_drops_onto_a_line_as_its_one_stop():
    f = F.take_off(F.get_plan("builtin:verify"), session="s", now=NOW)
    out = F.op_add_plan(f, F.get_plan("builtin:preset-selfreview"), now=NOW)
    assert [s["title"] for s in out["steps"]][-1] == "Self-review"
