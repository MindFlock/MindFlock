"""A ticketing source that spans several repos: the stored shape (and its
back-compat with the one-``repo_url`` shape), the routing order (labels →
title → the agent CLI → fallback), the recorded decision, and the prompt a
multi-repo ticket launches with. The source card's repo list is pinned in
frontend/src/__tests__/repoListField.test.tsx."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.config import settings as S
from backend.ticket_ingestion import repo_routing as R
from backend.ticket_ingestion.config import (
    PipelineConfig,
    RepoTarget,
    TicketProviderConfig,
    load_config,
)
from backend.ticket_ingestion.state import load_ticket_routes, record_ticket_route
from tests._factories import make_ticket
from tests.unit.test_ticket_launch import launch_env  # noqa: F401 — fixture

BACK = "git@github.com:acme/vnext-backend.git"
FRONT = "git@github.com:acme/vnext-frontend.git"
SITE = "https://github.com/acme/sitecheck"


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    monkeypatch.delenv("MINDFLOCK_REPO_URL", raising=False)


def _targets(*pairs):
    return [RepoTarget(url=u, hint=h) for u, h in pairs]


THREE = _targets((BACK, "api, django"), (FRONT, ""), (SITE, "scanner"))


def _src(targets=THREE, **kw):
    return TicketProviderConfig(
        provider="shortcut",
        repo_url=targets[0].url if targets else "",
        repos=list(targets) if len(targets) > 1 else [],
        **kw,
    )


def _never(prompt):  # an ask that must not be reached
    raise AssertionError("the model must not be asked")


# --------------------------------------------------------------------------- #
# Stored shape: settings.json back-compat
# --------------------------------------------------------------------------- #
def test_old_single_repo_source_reads_as_a_one_element_set_and_saves_unchanged():
    old = {"id": "sc", "provider": "shortcut", "repo_url": BACK}
    src = S.TicketingSource.from_dict(old)
    assert src.repo_entries() == [{"url": BACK}]
    # Byte-identical on the way back out: no `repos` key for one repo.
    assert src.to_dict() == old


def test_several_repos_roundtrip_with_repo_url_mirroring_the_first():
    d = S.TicketingSource(
        provider="shortcut",
        repos=[{"url": BACK, "hint": "api"}, {"url": FRONT}],
    ).to_dict()
    assert d["repo_url"] == BACK
    assert d["repos"] == [{"url": BACK, "hint": "api"}, {"url": FRONT}]
    back = S.TicketingSource.from_dict(d)
    assert back.repo_entries() == d["repos"]


def test_repo_entries_are_coerced_tolerantly():
    src = S.TicketingSource.from_dict(
        {
            "provider": "shortcut",
            "repos": [BACK, {"url": " "}, {"url": FRONT, "hint": "  "}, BACK, 7],
        }
    )
    assert src.repo_entries() == [{"url": BACK}, {"url": FRONT}]


def test_a_single_repos_entry_collapses_to_repo_url():
    d = S.TicketingSource(provider="shortcut", repos=[{"url": FRONT}]).to_dict()
    assert d == {"provider": "shortcut", "repo_url": FRONT}


def test_repos_reach_the_pipeline_config_and_old_sources_still_load():
    S.set_ticketing_sources(
        [
            {
                "id": "multi",
                "provider": "shortcut",
                "api_token": "t1",
                "member_id": "m1",
                "repos": [{"url": BACK, "hint": "api"}, {"url": FRONT}],
            },
            {
                "id": "single",
                "provider": "shortcut",
                "api_token": "t2",
                "member_id": "m2",
                "repo_url": SITE,
            },
        ]
    )
    cfg = load_config()
    by_id = {s.id: s for s in cfg.ticketing_sources}
    multi, single = by_id["multi"], by_id["single"]
    assert multi.repo_url == BACK
    assert multi.repo_targets() == [RepoTarget(BACK, "api"), RepoTarget(FRONT, "")]
    assert single.repos == []
    assert single.repo_targets() == [RepoTarget(SITE, "")]


def test_config_toml_may_list_repos_as_bare_urls(tmp_path):
    from backend.ticket_ingestion.config import _parse_source

    cfg, problems = _parse_source(
        {
            "provider": "shortcut",
            "api_token": "t",
            "member_id": "m",
            "repos": [BACK, FRONT],
        },
        tmp_path / "config.toml",
        0,
    )
    assert not problems
    assert cfg.repo_url == BACK
    assert cfg.repo_urls() == [BACK, FRONT]


# --------------------------------------------------------------------------- #
# Routing order
# --------------------------------------------------------------------------- #
def test_one_repo_is_never_routed():
    d = R.route(make_ticket(labels=["frontend"]), _targets((BACK, "")), ask=_never)
    assert d.repos == [BACK] and d.method == "single"


def test_a_label_naming_a_repo_wins_without_asking():
    d = R.route(make_ticket(labels=["vnext-frontend"]), THREE, ask=_never)
    assert d.repos == [FRONT]
    assert d.method == "label"
    assert d.summary() == "vnext-frontend: label 'vnext-frontend'"


def test_a_label_matching_a_word_only_one_repo_has():
    d = R.route(make_ticket(labels=["Frontend"]), THREE, ask=_never)
    assert d.repos == [FRONT] and d.reason == "label 'Frontend'"


def test_a_word_every_repo_shares_says_nothing():
    # "vnext" names two repos, so it is not a signal; the fallback applies.
    d = R.route(make_ticket(name="Tidy up", labels=["vnext"]), THREE, ask=None)
    assert d.method == "fallback" and d.repos == [BACK]


def test_a_label_matching_a_hint():
    d = R.route(make_ticket(labels=["scanner"]), THREE, ask=_never)
    assert d.repos == [SITE]


def test_labels_for_two_repos_route_to_both_in_source_order():
    d = R.route(make_ticket(labels=["frontend", "api"]), THREE, ask=_never)
    assert d.repos == [BACK, FRONT]
    assert d.reason == "labels 'api', 'frontend'"


def test_a_title_naming_exactly_one_repo():
    d = R.route(make_ticket(name="Scanner times out on login pages"), THREE, ask=_never)
    assert d.repos == [SITE] and d.method == "title"
    assert d.reason == "title mentions 'scanner'"


def test_an_ambiguous_title_asks_the_model():
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "<repos>vnext-backend, vnext-frontend</repos><reason>new endpoint plus UI</reason>"

    story = make_ticket(
        name="Add frontend form backed by a new API endpoint",
        description="The settings page needs a toggle; persist it server side.",
        labels=["Q3"],
    )
    d = R.route(story, THREE, ask=ask, blurbs=["Django API", "React app", ""])
    assert d.repos == [BACK, FRONT] and d.method == "llm"
    assert d.reason == "model: new endpoint plus UI"
    prompt = asked[0]
    for needle in (
        "1. vnext-backend — hint: api, django — README: Django API",
        "2. vnext-frontend — README: React app",
        "3. sitecheck — hint: scanner",
        "Ticket title: Add frontend form",
        "Labels: Q3",
        "persist it server side",
        "<repos></repos>",
    ):
        assert needle in prompt


def test_no_signal_asks_the_model_and_its_failure_falls_back_to_the_first_repo():
    def boom(prompt):
        raise RuntimeError("claude is not installed")

    d = R.route(make_ticket(name="Improve things"), THREE, ask=boom, blurbs=[])
    assert d.repos == [BACK] and d.method == "fallback"
    assert d.reason == "no signal — first repo"


def test_a_model_naming_nothing_on_the_list_falls_back_to_the_first_title_hit():
    d = R.route(
        make_ticket(name="frontend calls the api wrong"),
        THREE,
        ask=lambda p: "<repos>mobile</repos>",
        blurbs=[],
    )
    assert d.repos == [BACK] and d.method == "fallback"
    assert "first of several" in d.reason


def test_a_stored_model_decision_is_reused_but_a_label_overrides_it():
    stored = R.RouteDecision(repos=[SITE], reason="model: x", method="llm").to_dict()
    story = make_ticket(name="Improve things")
    assert R.route(story, THREE, ask=_never, stored=stored).repos == [SITE]
    story.labels = ["frontend"]
    assert R.route(story, THREE, ask=_never, stored=stored).repos == [FRONT]
    # …and a decision naming a repo the source no longer has is not reused.
    gone = R.RouteDecision(repos=["git@x:y/z.git"], reason="", method="llm").to_dict()
    assert R.route(
        make_ticket(name="Improve things"), THREE, ask=None, stored=gone
    ).repos == [BACK]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<repos>2</repos>", [FRONT]),
        ("Sure!\n<repos>\n`vnext-frontend`\n1. vnext-backend\n</repos>", [FRONT, BACK]),
        ("<REPOS>SiteCheck, sitecheck</REPOS>", [SITE]),
        ("vnext-backend", None),
        ("<repos>nothing-here</repos>", None),
    ],
)
def test_parse_llm_answer(raw, expected):
    d = R.parse_llm_answer(raw, THREE)
    assert (d.repos if d else None) == expected


def test_the_model_question_uses_the_shared_one_shot_path(monkeypatch):
    from backend.web.core import commit_message

    seen = {}

    def fake_pick(prompt, program, fallback, purpose=""):
        seen["program"], seen["purpose"] = program, purpose
        return ["claude", "-p", prompt]

    def fake_run(argv, cwd, timeout):
        seen["cwd"] = cwd
        return "<repos>sitecheck</repos>"

    monkeypatch.setattr(commit_message, "pick_argv", fake_pick)
    monkeypatch.setattr(commit_message, "_run", fake_run)
    monkeypatch.setattr(commit_message, "neutral_cwd", lambda: "/neutral")
    assert R.ask_cli("q", "codex") == "<repos>sitecheck</repos>"
    assert seen == {
        "program": "codex",
        "purpose": "the repo a ticket needs",
        "cwd": "/neutral",
    }


def test_readme_blurb_skips_badges_and_headings(tmp_path):
    (tmp_path / "README.md").write_text(
        "# vnext-backend\n\n[![ci](x)](y)\n<p>logo</p>\n\nThe Django API behind VNext.\n"
    )
    assert R.readme_blurb(str(tmp_path)) == "vnext-backend The Django API behind VNext."
    assert R.readme_blurb(str(tmp_path / "missing")) == ""


# --------------------------------------------------------------------------- #
# apply_route: stamps the story, resolves paths, records the decision
# --------------------------------------------------------------------------- #
def _local_repos(tmp_path):
    paths = []
    for name in ("vnext-backend", "vnext-frontend", "sitecheck"):
        p = tmp_path / "code" / name
        p.mkdir(parents=True)
        paths.append(str(p))
    return paths


def test_apply_route_stamps_primary_extras_and_records(tmp_path):
    back, front, site = _local_repos(tmp_path)
    src = _src(_targets((back, ""), (front, ""), (site, "")))
    story = make_ticket(id=7, slug="sc-7", labels=["frontend", "backend"])
    d = R.apply_route(story, src, state_dir=tmp_path, ask=_never)
    assert d.repos == [back, front]
    assert story.repo_url == back
    assert story.route["extra"] == [
        {"name": "vnext-frontend", "url": front, "path": front}
    ]
    assert story.route["others"] == ["sitecheck"]
    assert load_ticket_routes(tmp_path)["sc-7"]["repos"] == [back, front]


def test_apply_route_leaves_a_one_repo_source_alone(tmp_path):
    story = make_ticket(slug="sc-8", repo_url=BACK)
    assert R.apply_route(story, _src(_targets((BACK, ""))), state_dir=tmp_path) is None
    assert story.repo_url == BACK and story.route == {}
    assert load_ticket_routes(tmp_path) == {}


def test_recorded_routes_are_capped(tmp_path, monkeypatch):
    from backend.ticket_ingestion import state

    monkeypatch.setattr(state, "_MAX_TICKET_ROUTES", 3)
    for i in range(5):
        record_ticket_route(
            tmp_path, f"sc-{i}", {"repos": [BACK], "at": f"2026-01-0{i + 1}"}
        )
    assert sorted(load_ticket_routes(tmp_path)) == ["sc-2", "sc-3", "sc-4"]


def test_preview_never_asks_and_says_pending_when_ambiguous(tmp_path):
    src = _src()
    assert R.preview(make_ticket(labels=["api"]), src, {})["summary"] == (
        "vnext-backend: label 'api'"
    )
    pending = R.preview(make_ticket(name="Improve things", slug="sc-9"), src, {})
    assert pending["method"] == "pending" and pending["repo_url"] == ""
    stored = {"sc-9": R.RouteDecision([SITE], "model: scans", "llm").to_dict()}
    got = R.preview(make_ticket(name="Improve things", slug="sc-9"), src, stored)
    assert got["repos"] == ["sitecheck"] and got["repo_url"] == SITE
    assert R.preview(make_ticket(), _src(_targets((BACK, ""))), {}) is None


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #
def _prompt(story):
    from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner

    return ClaudeCodeRunner(PipelineConfig())._build_prompt(story, None, None)


def test_multi_repo_prompt_names_each_other_repo_by_absolute_path(tmp_path):
    back, front, site = _local_repos(tmp_path)
    src = _src(_targets((back, ""), (front, ""), (site, "")))
    story = make_ticket(
        id=12, slug="sc-12", name="Add export", labels=["backend", "frontend"]
    )
    R.apply_route(story, src, state_dir=tmp_path, ask=_never)
    text = _prompt(story)
    branch = "feature/sc-12/add-export"
    assert "## Other repositories this ticket needs" in text
    assert "MindFlock routed this ticket to `vnext-backend` (this workspace)" in text
    assert "the repositories below — labels 'backend', 'frontend'. Make" in text
    assert "### vnext-frontend" in text
    assert f"Local checkout: `{front}`" in text
    worktree = str(tmp_path / "code" / "vnext-frontend-sc-12")
    assert f"git -C {front} worktree add {worktree} -b {branch} origin/HEAD" in text
    assert f"git -C {worktree} push -u origin {branch}" in text
    assert "Open a pull request for it in that repository." in text
    assert "its own branch, its own commits, its own pull request" in text
    # The section sits between the description and the acceptance criteria.
    assert (
        text.index("## Description")
        < text.index("## Other repositories")
        < text.index("## Acceptance Criteria")
    )
    assert "sitecheck" not in text.split("## Other repositories")[1]


def test_an_extra_repo_without_a_checkout_says_to_clone_it():
    story = make_ticket(slug="sc-3", name="Fix", repo_url=BACK)
    story.route = {
        "reason": "model: both",
        "method": "llm",
        "extra": [{"name": "vnext-frontend", "url": FRONT, "path": ""}],
        "others": [],
    }
    text = R.other_repos_section(story, "feature/sc-3/fix")
    assert f"Repository: `{FRONT}` (no local checkout yet)" in text
    assert "create the branch `feature/sc-3/fix` there" in text


def test_a_ticket_routed_to_one_of_several_repos_is_told_where_and_why(tmp_path):
    story = make_ticket(slug="sc-4", name="Scanner fix", repo_url=SITE)
    R.apply_route(story, _src(), state_dir=tmp_path, ask=_never)
    text = _prompt(story)
    assert "## Repository" in text
    assert (
        "routed it to `sitecheck` (this workspace) — title mentions 'scanner'." in text
    )
    assert "The others are `vnext-backend`, `vnext-frontend`." in text
    assert "## Other repositories" not in text


def test_a_one_repo_ticket_prompt_is_unchanged():
    story = make_ticket(repo_url=BACK)
    text = _prompt(story)
    assert "## Repository" not in text and "## Other repositories" not in text


# --------------------------------------------------------------------------- #
# The pipeline: the branch check spans every repo, and process_story routes
# --------------------------------------------------------------------------- #
async def test_backfill_checks_branches_in_every_repo_of_the_source(
    tmp_path, monkeypatch
):
    from backend.ticket_ingestion import backfill

    monkeypatch.setattr(backfill, "_STATE_DIR", tmp_path)
    import asyncio

    cfg = PipelineConfig(ticketing=_src(), repo_url="", workspace_dir=tmp_path)
    scanner = backfill.BackfillScanner(cfg, asyncio.Queue(), source=cfg.ticketing)
    seen = []

    async def branches(url):
        seen.append(url)
        return {"feature/sc-1/test-story"} if url == FRONT else set()

    with (
        patch.object(scanner, "_fetch_stories", new_callable=AsyncMock) as fetch,
        patch.object(backfill, "_get_existing_branches", side_effect=branches),
    ):
        fetch.return_value = [make_ticket(id=1), make_ticket(id=2, name="Other")]
        assert await scanner.scan() == 1  # sc-1's branch exists in the frontend repo
    assert seen == [BACK, FRONT, SITE]


async def test_process_story_routes_before_launching(tmp_path, monkeypatch):
    from backend.ticket_ingestion import orchestrator as orch

    monkeypatch.setattr(orch, "_STATE_DIR", tmp_path)
    o = orch.PipelineOrchestrator.__new__(orch.PipelineOrchestrator)
    o.config = PipelineConfig(ticketing=_src())

    class _Scanner:
        _source = o.config.ticketing
        _source_key = "shortcut"

    o._scanners = [_Scanner()]
    story = make_ticket(slug="sc-5", labels=["frontend"], repo_url=BACK)
    story.source_key = "shortcut"
    await o._route_story(story)
    assert story.repo_url == FRONT
    assert story.route["reason"] == "label 'frontend'"
    assert load_ticket_routes(tmp_path)["sc-5"]["method"] == "label"


async def test_process_story_routing_failure_keeps_the_first_repo(
    tmp_path, monkeypatch
):
    from backend.ticket_ingestion import orchestrator as orch

    monkeypatch.setattr(orch, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        R, "route", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    o = orch.PipelineOrchestrator.__new__(orch.PipelineOrchestrator)
    o.config = PipelineConfig(ticketing=_src())

    class _Scanner:
        _source = o.config.ticketing
        _source_key = "shortcut"

    o._scanners = [_Scanner()]
    story = make_ticket(slug="sc-6", repo_url=BACK)
    await o._route_story(story)  # must not raise
    assert story.repo_url == BACK and story.route == {}


def test_route_story_for_a_forced_start(tmp_path, monkeypatch):
    from backend.web.core import ticket_start

    cfg = PipelineConfig(ticketing_sources=[_src(id="sc")])
    monkeypatch.setattr(ticket_start, "_load_config", lambda: cfg)
    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    story = make_ticket(slug="sc-11", labels=["scanner"], repo_url=BACK)
    d = ticket_start.route_story(story, "sc", "claude")
    assert d is not None and story.repo_url == SITE
    # Unknown source / one-repo source: untouched.
    assert ticket_start.route_story(make_ticket(), "nope") is None


def test_route_decision_timestamps_are_utc_iso():
    d = R.RouteDecision([BACK], "", "label")
    assert datetime.fromisoformat(d.at).tzinfo == timezone.utc


# --------------------------------------------------------------------------- #
# The sources API carries the list (what the Intake card PUTs)
# --------------------------------------------------------------------------- #
def test_sources_put_stores_and_returns_the_repo_list(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    S.invalidate()
    from backend.web.server import app

    base = {"id": "sc", "provider": "shortcut", "api_token": "t", "member_id": "m"}
    with TestClient(app) as c:
        many = {
            **base,
            "repo_url": BACK,
            "repos": [{"url": BACK, "hint": "api"}, {"url": FRONT}],
        }
        got = c.put("/api/settings/ticketing/sources", json={"sources": [many]}).json()
        assert got["sources"][0]["repos"] == [
            {"url": BACK, "hint": "api"},
            {"url": FRONT},
        ]
        assert got["sources"][0]["repo_url"] == BACK
        # Back down to one repo: the card sends an empty list; the old shape returns.
        one = {**base, "repo_url": FRONT, "repos": []}
        got = c.put("/api/settings/ticketing/sources", json={"sources": [one]}).json()
        assert "repos" not in got["sources"][0]
        assert got["sources"][0]["repo_url"] == FRONT
    S.invalidate()


def test_paths_with_spaces_are_quoted_in_the_commands(tmp_path):
    front = str(tmp_path / "My Code" / "vnext-frontend")
    story = make_ticket(slug="sc-3", name="Fix", repo_url=BACK)
    story.route = {
        "reason": "",
        "method": "label",
        "extra": [{"name": "vnext-frontend", "url": front, "path": front}],
        "others": [],
    }
    text = R.other_repos_section(story, "feature/sc-3/fix")
    assert f"git -C '{front}' fetch origin" in text
    assert f"worktree add '{tmp_path / 'My Code' / 'vnext-frontend-sc-3'}' -b" in text


# --------------------------------------------------------------------------- #
# A repo picked on the Intake row (POST /api/tickets/start ``repo``)
# --------------------------------------------------------------------------- #
def test_pick_target_takes_a_url_or_a_short_name_of_the_sources_repos():
    src = _src()
    assert R.pick_target(src, FRONT) == FRONT
    assert R.pick_target(src, "sitecheck") == SITE
    assert R.pick_target(src, "git@github.com:acme/other.git") is None
    assert R.pick_target(src, "") is None


def test_a_manual_pick_skips_routing_and_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "route", lambda *a, **k: pytest.fail("must not route"))
    # A label says frontend; the row said sitecheck — the person wins.
    story = make_ticket(slug="sc-21", labels=["frontend"], repo_url=BACK)
    d = R.apply_manual_route(story, _src(), SITE, state_dir=tmp_path)
    assert story.repo_url == SITE
    assert (d.repos, d.reason, d.method) == ([SITE], "picked on the row", "manual")
    assert story.route["method"] == "manual" and story.route["extra"] == []
    assert story.route["others"] == ["vnext-backend", "vnext-frontend"]
    rec = load_ticket_routes(tmp_path)["sc-21"]
    assert (rec["repos"], rec["reason"], rec["method"]) == (
        [SITE],
        "picked on the row",
        "manual",
    )


def test_a_recorded_manual_pick_outranks_labels_in_route_and_preview():
    story = make_ticket(slug="sc-22", labels=["frontend"])
    stored = R.RouteDecision([SITE], "picked on the row", "manual").to_dict()
    assert R.route(story, THREE, ask=_never, stored=stored).repos == [SITE]
    got = R.preview(story, _src(), {"sc-22": stored})
    assert got["repo_url"] == SITE and got["method"] == "manual"
    # A pick of a repo the source no longer has falls back to routing.
    gone = R.RouteDecision(["git@x:o/gone.git"], "picked on the row", "manual")
    assert R.preview(story, _src(), {"sc-22": gone.to_dict()})["repo_url"] == FRONT


def test_preview_lists_every_repo_of_the_source_for_the_row_picker():
    want = [
        {"name": "vnext-backend", "url": BACK},
        {"name": "vnext-frontend", "url": FRONT},
        {"name": "sitecheck", "url": SITE},
    ]
    assert R.preview(make_ticket(labels=["api"]), _src(), {})["choices"] == want
    pending = R.preview(make_ticket(name="Improve things"), _src(), {})
    assert pending["method"] == "pending" and pending["choices"] == want


def _multi_repo_config(monkeypatch, tmp_path, targets=THREE):
    from backend.web.core import ticket_start

    cfg = PipelineConfig(ticketing_sources=[_src(targets, id="sc")])
    monkeypatch.setattr(ticket_start, "_load_config", lambda: cfg)
    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    return ticket_start


def test_route_story_pins_a_picked_repo(tmp_path, monkeypatch):
    ticket_start = _multi_repo_config(monkeypatch, tmp_path)
    monkeypatch.setattr(R, "route", lambda *a, **k: pytest.fail("must not route"))
    story = make_ticket(slug="sc-23", labels=["scanner"], repo_url=BACK)
    d = ticket_start.route_story(story, "sc", "claude", FRONT)
    assert d.method == "manual" and story.repo_url == FRONT


def test_resolve_repo_pick_refuses_what_the_source_does_not_have(tmp_path, monkeypatch):
    ticket_start = _multi_repo_config(monkeypatch, tmp_path)
    assert ticket_start.resolve_repo_pick("sc", "") == ""
    assert ticket_start.resolve_repo_pick("sc", "vnext-frontend") == FRONT
    with pytest.raises(ticket_start.LaunchError) as err:
        ticket_start.resolve_repo_pick("sc", "git@x:o/elsewhere.git")
    assert err.value.status == 400
    assert "vnext-backend, vnext-frontend, sitecheck" in err.value.body["error"]
    # A one-repo source has nothing to pick.
    _multi_repo_config(monkeypatch, tmp_path, _targets((BACK, "")))
    with pytest.raises(ticket_start.LaunchError) as err:
        ticket_start.resolve_repo_pick("sc", BACK)
    assert err.value.status == 400


def test_a_start_with_a_picked_repo_launches_there(tmp_path, monkeypatch, launch_env):
    import asyncio

    from tests.unit.test_ticket_launch import _drain

    ticket_start = _multi_repo_config(monkeypatch, tmp_path)
    story = asyncio.run(ticket_start.find_ticket("sc", "23588"))
    body = asyncio.run(ticket_start.launch("sc", "23588", repo="sitecheck"))
    assert body["title"] == "sc-23588"
    _drain(launch_env.tasks)
    assert story.repo_url == SITE
    assert story.route["reason"] == "picked on the row"
    assert load_ticket_routes(tmp_path)["23588"]["method"] == "manual"


def test_a_bad_repo_pick_claims_nothing(tmp_path, monkeypatch, launch_env):
    import asyncio

    from backend.web.core import pending as pending_mod

    ticket_start = _multi_repo_config(monkeypatch, tmp_path)
    with pytest.raises(ticket_start.LaunchError) as err:
        asyncio.run(ticket_start.launch("sc", "23588", repo="nope"))
    assert err.value.status == 400
    assert not pending_mod.has("sc-23588") and launch_env.tasks == []


@pytest.mark.parametrize("repo", ["nope", 7])
def test_the_start_route_answers_400_for_a_bad_repo(tmp_path, monkeypatch, repo):
    from fastapi.testclient import TestClient

    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    _multi_repo_config(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "git_available", lambda: True)
    r = TestClient(server.app).post(
        "/api/tickets/start", json={"source": "sc", "id": "1", "repo": repo}
    )
    assert r.status_code == 400
    assert "repo" in r.json()["error"]
