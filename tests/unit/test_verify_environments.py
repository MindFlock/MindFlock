"""Verify "where": a repo's environments, and a run pointed at one.

A route's Verify stop chooses WHERE the behaviors are checked — this branch's
own code, or one of the repo's environments (the implicit ``live`` built from
the repo's card, plus any it lists under ``verify_repo_settings[slug]
["environments"]``). These pin the backend half of that: the settings
sanitizer, ``test_plans.environments`` / ``environment`` / ``env_reached_at`` /
``results_markdown``, the run route's ``environment`` + ``notes`` body, the
``GET /api/test-plans/environments`` list, and the generation steer
``_ensure_test_plan(environment=, notes=)``.

Isolation: the root conftest points ``$MINDFLOCK_SETTINGS_FILE`` and
``$MINDFLOCK_TEST_PLANS_FILE`` at tmp for every test, and anything that touches
``server.ENGINE.instances`` does it through ``monkeypatch`` so the real engine
is never written.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.config import settings as settings_mod
from backend.web.core import test_plans as tp
from backend.web.core import test_plans_driver as tpd


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo_settings():
    """Write ``[repository]`` into the (conftest-isolated) settings file and
    drop the parse cache, so the real ``load_settings`` reads it."""
    path = Path(os.environ["MINDFLOCK_SETTINGS_FILE"])

    def write(**fields) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"repository": fields}), encoding="utf-8")
        settings_mod.invalidate()

    yield write
    settings_mod.invalidate()


@pytest.fixture
def acme(monkeypatch, repo_settings):
    """``/repo`` is ``acme/app`` on the Verify list, with a target, a live
    branch, a deploy delay, standing notes and one sandbox environment."""
    monkeypatch.setattr(tp, "repo_slug", lambda root: "acme/app" if root else "")
    repo_settings(
        verify_repos=["acme/app"],
        verify_repo_settings={
            "acme/app": {
                "target": "https://app.example.com",
                "live_branch": "staging",
                "deploy_delay_minutes": "7",
                "prompt": "The UI runs on :3000.",
                "environments": [
                    {
                        "name": "sandbox",
                        "reach": "https://sandbox.example.com",
                        "branch": "sandbox",
                        "deploy_minutes": 3,
                    }
                ],
            }
        },
    )
    return "/repo"


def _plan(plan_id="sc-1", **over) -> dict:
    plan = {
        "id": plan_id,
        "title": plan_id,
        "repo_root": "/repo",
        "branch": "feature/%s" % plan_id,
        "sha": "a" * 40,
        "tip_sha": "b" * 40,
        "live_branch": "staging",
        "state": "due",
        "live_at": 1.0,
        "generated_at": 100.0,
        "steps": [
            {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"},
            {"id": "s2", "text": "Look at it", "expect": "Right", "actor": "human"},
        ],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


@pytest.fixture
def client(monkeypatch):
    """The real app, with the run route's environment probes pinned and session
    creation captured instead of performed."""
    from backend.web import server

    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_is_verify_repo_usable", lambda root: True)
    monkeypatch.setattr(server, "_kill_orphan_plan_tmux", lambda t: "")
    monkeypatch.setattr(server, "_free_stale_verify_worktree", lambda r, t: "")
    monkeypatch.setattr(server, "_clear_verify_results", lambda t: None)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    made: list = []

    async def _create(payload, request=None):
        made.append(payload)
        return JSONResponse({"ok": True}, status_code=202)

    monkeypatch.setattr(server, "create_instance", _create)
    c = TestClient(server.app)
    c.made = made
    return c


# --------------------------------------------------------------------------- #
# Settings: sanitizing a repo's environments
# --------------------------------------------------------------------------- #
def test_environments_are_sanitized_on_load():
    got = settings_mod._verify_repo_settings(
        {
            "acme/app": {
                "environments": [
                    {"name": "Sandbox", "reach": " #sandbox ", "branch": "sandbox"},
                    {"name": "sandbox", "reach": "a duplicate — first wins"},
                    {"name": "branch", "reach": "reserved"},
                    {"name": "BRANCH"},
                    {"name": "has space"},
                    {"name": ""},
                    {"name": "x" * 41},
                    "not a map",
                    {"name": "qa_1", "deploy_minutes": -5, "branch": "a..b"},
                    {"name": "slow", "deploy_minutes": 99999, "branch": "-oops"},
                    {"name": "junk", "deploy_minutes": "abc", "branch": "x;rm -rf"},
                    {"name": "ok", "deploy_minutes": "30", "reach": "y" * 900},
                ]
            }
        }
    )
    envs = got["acme/app"]["environments"]
    assert [e["name"] for e in envs] == ["sandbox", "qa_1", "slow", "junk", "ok"]
    by = {e["name"]: e for e in envs}
    assert by["sandbox"] == {
        "name": "sandbox",
        "reach": "#sandbox",
        "branch": "sandbox",
        "deploy_minutes": 0,
    }
    # Clamped, garbage-tolerant, and a branch git/the shell should not see reads
    # as no branch at all.
    assert by["qa_1"]["deploy_minutes"] == 0 and by["qa_1"]["branch"] == ""
    assert by["slow"]["deploy_minutes"] == 1440 and by["slow"]["branch"] == ""
    assert by["junk"]["deploy_minutes"] == 0 and by["junk"]["branch"] == ""
    assert by["ok"]["deploy_minutes"] == 30
    assert len(by["ok"]["reach"]) == settings_mod.VERIFY_TARGET_MAX


def test_environments_alone_keep_a_block_and_an_empty_list_drops_it():
    got = settings_mod._verify_repo_settings(
        {
            "acme/app": {"environments": [{"name": "sandbox"}]},
            "acme/empty": {"environments": []},
            "acme/bad": {"environments": "not a list"},
        }
    )
    assert list(got) == ["acme/app"]
    assert got["acme/app"]["environments"][0]["reach"] == ""


def test_environments_are_capped():
    many = [{"name": "e%d" % i} for i in range(50)]
    got = settings_mod._verify_repo_settings({"a/b": {"environments": many}})
    assert len(got["a/b"]["environments"]) == settings_mod.VERIFY_ENVIRONMENTS_MAX


def test_environments_round_trip_through_the_settings_object():
    block = {"environments": [{"name": "sandbox", "branch": "sandbox"}]}
    repo = settings_mod.RepositorySettings.from_dict(
        {"verify_repo_settings": {"acme/app": block}}
    )
    out = repo.to_dict()["verify_repo_settings"]["acme/app"]["environments"]
    assert out == [
        {"name": "sandbox", "reach": "", "branch": "sandbox", "deploy_minutes": 0}
    ]
    # A copy, not the loaded settings' own list.
    out[0]["name"] = "changed"
    assert repo.verify_repo_settings["acme/app"]["environments"][0]["name"] == (
        "sandbox"
    )


def test_settings_sync_carries_the_whole_block():
    """Environments ride inside ``verify_repo_settings``, which sync already
    treats as one field — nothing per-key to enumerate."""
    from backend.web.core import settings_sync

    assert "verify_repo_settings" in settings_sync.SYNCED["repository"]


# --------------------------------------------------------------------------- #
# environments() / environment()
# --------------------------------------------------------------------------- #
def test_the_repos_own_fields_are_an_implicit_live_environment(acme):
    envs = tp.environments(acme)
    assert envs == [
        {
            "name": "live",
            "reach": "https://app.example.com",
            "branch": "staging",
            "deploy_minutes": 7,
            "implicit": True,
        },
        {
            "name": "sandbox",
            "reach": "https://sandbox.example.com",
            "branch": "sandbox",
            "deploy_minutes": 3,
            "implicit": False,
        },
    ]


def test_a_configured_live_overrides_the_implicit_one(monkeypatch, repo_settings):
    monkeypatch.setattr(tp, "repo_slug", lambda root: "acme/app")
    repo_settings(
        verify_repos=["acme/app"],
        verify_repo_settings={
            "acme/app": {
                "target": "https://old.example.com",
                "environments": [
                    {"name": "sandbox", "branch": "sandbox"},
                    {"name": "live", "reach": "#prod-alerts", "branch": "prod"},
                ],
            }
        },
    )
    envs = tp.environments("/repo")
    assert [e["name"] for e in envs] == ["live", "sandbox"]
    assert envs[0]["reach"] == "#prod-alerts" and envs[0]["branch"] == "prod"
    assert envs[0]["implicit"] is False


def test_an_unconfigured_repo_still_has_live(monkeypatch, repo_settings):
    monkeypatch.setattr(tp, "repo_slug", lambda root: "acme/app")
    repo_settings(verify_repos=[], deploy_delay_minutes=0)
    assert tp.environments("/repo") == [
        {
            "name": "live",
            "reach": "",
            "branch": "main",
            "deploy_minutes": 0,
            "implicit": True,
        }
    ]


def test_environment_by_name(acme):
    assert tp.environment(acme, "Sandbox")["reach"] == "https://sandbox.example.com"
    assert tp.environment(acme, "live")["implicit"] is True
    assert tp.environment(acme, "branch") is None
    assert tp.environment(acme, "nope") is None
    assert tp.environment(acme, "") is None


# --------------------------------------------------------------------------- #
# env_reached_at — a real origin, a real environment branch
# --------------------------------------------------------------------------- #
def _git(cwd, *args) -> str:
    out = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return out.stdout.decode().strip()


@pytest.fixture
def origin_and_clone(tmp_path, monkeypatch):
    """``origin.git`` with ``main`` and ``sandbox``; ``work`` cloned from it
    with a pushed ``feature`` commit; ``other``, a second clone, to move origin
    without touching ``work``'s remote-tracking refs."""
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    (seed / "a.txt").write_text("a\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "a")
    _git(seed, "push", "-q", "origin", "HEAD:main", "HEAD:sandbox")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "checkout", "-qb", "feature")
    (work / "b.txt").write_text("b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "b")
    _git(work, "push", "-q", "origin", "feature")
    sha = _git(work, "rev-parse", "HEAD")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    monkeypatch.setattr(tp, "_ENV_FETCHED", {})
    return SimpleNamespace(work=work, other=other, sha=sha)


SANDBOX = {"name": "sandbox", "branch": "sandbox", "reach": "", "deploy_minutes": 0}


def test_env_reached_at_waits_for_ancestry_then_keeps_the_first_sighting(
    origin_and_clone, monkeypatch
):
    o = origin_and_clone
    tp.upsert(_plan("sc-1", repo_root=str(o.work), sha=o.sha, tip_sha=o.sha))
    assert tp.env_reached_at("sc-1", SANDBOX) == 0.0

    # The work reaches origin's sandbox — from ANOTHER clone, so `work` only
    # learns of it by fetching.
    _git(o.other, "push", "-q", "origin", "origin/feature:refs/heads/sandbox")

    # Throttled: within the TTL nothing is fetched, so it is still "not yet".
    assert tp.env_reached_at("sc-1", SANDBOX) == 0.0

    monkeypatch.setattr(tp, "_ENV_FETCHED", {})
    monkeypatch.setattr(tp.time, "time", lambda: 5000.0)
    assert tp.env_reached_at("sc-1", SANDBOX) == 5000.0
    assert tp.get("sc-1")["env_seen"] == {"sandbox": 5000.0}

    # The first sighting is the answer from now on — the deploy clock does not
    # restart on every poll.
    monkeypatch.setattr(tp.time, "time", lambda: 9000.0)
    monkeypatch.setattr(tp, "_ENV_FETCHED", {})
    assert tp.env_reached_at("sc-1", SANDBOX) == 5000.0


def test_env_reached_at_is_zero_for_a_commit_that_is_not_there(origin_and_clone):
    o = origin_and_clone
    tp.upsert(_plan("sc-1", repo_root=str(o.work), sha=o.sha, tip_sha=o.sha))
    main = {"name": "prod", "branch": "main"}
    assert tp.env_reached_at("sc-1", main) == 0.0
    assert tp.get("sc-1")["env_seen"] == {}


def test_env_reached_at_without_a_branch_or_a_plan_is_zero(origin_and_clone):
    o = origin_and_clone
    tp.upsert(_plan("sc-1", repo_root=str(o.work), sha=o.sha))
    assert tp.env_reached_at("sc-1", {"name": "sandbox", "branch": ""}) == 0.0
    assert tp.env_reached_at("nope", SANDBOX) == 0.0
    assert tp.env_reached_at("sc-1", {}) == 0.0


def test_env_seen_survives_the_store_and_garbage_is_dropped():
    tp.upsert(_plan("sc-1", env_seen={"sandbox": 12.0, "": 3.0, "bad": "x"}))
    assert tp.get("sc-1")["env_seen"] == {"sandbox": 12.0}


# --------------------------------------------------------------------------- #
# POST /api/test-plans/{id}/run — environment + notes
# --------------------------------------------------------------------------- #
def test_no_environment_is_todays_run_exactly(client, acme):
    tp.upsert(_plan("sc-1"))
    plan = tp.get("sc-1")
    expected = tp.build_run_prompt(
        plan,
        None,
        live=True,
        repo_notes=tp.repo_notes(acme),
        target=tp.verify_target(acme),
    )
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 202, r.text
    assert client.made[0]["prompt"] == expected
    run = tp.get("sc-1")["runs"][-1]
    assert run["where"] == "" and run["checkout"] == ""


def test_branch_checks_the_plans_own_commit_with_no_deployment(client, acme):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "branch"})
    assert r.status_code == 202, r.text
    prompt = client.made[0]["prompt"]
    assert "THIS BRANCH'S OWN CODE" in prompt
    assert "git checkout --detach %s" % ("b" * 40) in prompt
    # The repo's deployment is NOT what is under test.
    assert "https://app.example.com" not in prompt
    assert "NOTHING IS RUNNING HERE" in prompt
    run = tp.get("sc-1")["runs"][-1]
    assert run["where"] == "branch"
    assert run["target"] == ""
    assert run["checkout"] == "b" * 40


def test_an_environment_puts_its_deployment_under_test(client, acme):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "sandbox"})
    assert r.status_code == 202, r.text
    prompt = client.made[0]["prompt"]
    assert "git fetch origin sandbox && git checkout --detach origin/sandbox" in prompt
    assert "ONLY TO READ CODE" in prompt
    assert "THE SANDBOX ENVIRONMENT" in prompt.upper()
    assert "https://sandbox.example.com" in prompt
    assert "https://app.example.com" not in prompt
    run = tp.get("sc-1")["runs"][-1]
    assert run["where"] == "sandbox"
    assert run["target"] == "https://sandbox.example.com"
    assert run["checkout"] == "origin/sandbox"
    # ...and the poller will stamp the run with the sandbox, not the repo's
    # own target, when it finishes.
    assert tp.run_target(tp.get("sc-1")) == "https://sandbox.example.com"


def test_an_environment_with_no_branch_checks_out_the_plans_commit(
    client, monkeypatch, repo_settings
):
    monkeypatch.setattr(tp, "repo_slug", lambda root: "acme/app")
    repo_settings(
        verify_repos=["acme/app"],
        verify_repo_settings={"acme/app": {"environments": [{"name": "canary"}]}},
    )
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "canary"})
    assert r.status_code == 202, r.text
    prompt = client.made[0]["prompt"]
    assert "git checkout --detach %s" % ("b" * 40) in prompt
    assert "Nobody wrote down how to reach it" in prompt
    assert tp.get("sc-1")["runs"][-1]["checkout"] == "b" * 40


def test_an_unknown_environment_is_a_400_and_starts_nothing(client, acme):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "nope"})
    assert r.status_code == 400
    assert r.json() == {"error": "no such environment: nope"}
    assert client.made == []
    assert tp.get("sc-1")["state"] == "due"


def test_notes_reach_the_checker_after_the_standing_notes(client, acme):
    tp.upsert(_plan("sc-1"))
    r = client.post(
        "/api/test-plans/sc-1/run",
        json={"environment": "sandbox", "notes": "Mind the migration."},
    )
    assert r.status_code == 202, r.text
    prompt = client.made[0]["prompt"]
    standing = prompt.index("The UI runs on :3000.")
    mine = prompt.index("Notes for this check: Mind the migration.")
    assert standing < mine < prompt.index("Steps:")


def test_notes_without_an_environment_still_arrive(client, acme):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"notes": "x" * 3000})
    assert r.status_code == 202, r.text
    assert "Notes for this check: " + "x" * 2000 in client.made[0]["prompt"]
    assert "x" * 2001 not in client.made[0]["prompt"]


@pytest.mark.parametrize(
    "body", [{"environment": 3}, {"notes": ["a"]}, {"environment": {"x": 1}}]
)
def test_a_malformed_body_is_a_400(client, acme, body):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json=body)
    assert r.status_code == 400
    assert client.made == []


def test_the_in_process_call_shape_still_works(client, acme):
    """The route driver calls ``run_test_plan(plan_id, None, {...})``."""
    import asyncio

    from backend.web import server

    tp.upsert(_plan("sc-1"))
    resp = asyncio.run(
        server.run_test_plan("sc-1", None, {"environment": "live", "notes": "n"})
    )
    assert resp.status_code == 202
    run = tp.get("sc-1")["runs"][-1]
    assert run["where"] == "live" and run["checkout"] == "origin/staging"
    assert run["target"] == "https://app.example.com"


# --------------------------------------------------------------------------- #
# GET /api/test-plans/environments
# --------------------------------------------------------------------------- #
def test_the_environments_list_covers_every_verify_repo(
    client, monkeypatch, repo_settings
):
    repo_settings(
        live_branch="release",
        verify_repos=["Acme/App", "acme/lib"],
        verify_repo_settings={
            "acme/app": {
                "live_branch": "staging",
                "environments": [
                    {"name": "sandbox", "branch": "sandbox", "deploy_minutes": 2},
                    {"name": "qa"},
                ],
            }
        },
    )
    r = client.get("/api/test-plans/environments")
    assert r.status_code == 200
    body = r.json()
    assert body["names"] == ["live", "qa", "sandbox"]
    assert list(body["repos"]) == ["Acme/App", "acme/lib"]
    app = body["repos"]["Acme/App"]
    assert [e["name"] for e in app] == ["live", "sandbox", "qa"]
    assert app[0]["branch"] == "staging" and app[0]["implicit"] is True
    assert app[1]["deploy_minutes"] == 2
    # A repo with no block inherits the flock-wide chain.
    assert body["repos"]["acme/lib"][0]["branch"] == "release"


def test_the_environments_route_is_not_a_plan_id(client, repo_settings):
    repo_settings(verify_repos=[])
    r = client.get("/api/test-plans/environments")
    assert r.status_code == 200
    assert r.json() == {"repos": {}, "names": []}


# --------------------------------------------------------------------------- #
# results_markdown
# --------------------------------------------------------------------------- #
def test_results_markdown_names_where_and_every_behavior():
    plan = _plan(
        "sc-1",
        summary="Badges count unread.",
        steps=[
            {"id": "s1", "text": "Open it", "expect": "", "actor": "agent"},
            {"id": "s2", "text": "Look at\nit", "expect": "", "actor": "human"},
            {"id": "s3", "text": "Poke it", "expect": "", "actor": "agent"},
            {"id": "s4", "text": "Wait", "expect": "", "actor": "agent"},
        ],
        runs=[
            {
                "at": 1.0,
                "where": "sandbox",
                "target": "https://sandbox.example.com",
                "results": {
                    "s1": {"result": "pass", "note": ""},
                    "s2": {"result": "fail", "note": "badge\nreads 0"},
                    "s3": {"result": "blocked", "note": "no access"},
                },
            }
        ],
    )
    md = tp.results_markdown(plan)
    lines = md.splitlines()
    assert lines[0] == (
        "### Verify: checked on the **sandbox** environment "
        "(https://sandbox.example.com)"
    )
    assert "_Badges count unread._" in lines
    assert "- ✅ pass · agent · Open it" in lines
    assert "- ❌ fail · human · Look at it — badge reads 0" in lines
    assert "- ⏸ blocked · agent · Poke it — no access" in lines
    assert "- ⬜ open · agent · Wait" in lines


def test_results_markdown_for_branch_and_for_no_run():
    branch = _plan(runs=[{"at": 1.0, "where": "branch", "results": {}}])
    assert tp.results_markdown(branch).startswith(
        "### Verify: checked on this branch's own code (`bbbbbbb`)"
    )
    fresh = tp.results_markdown(_plan())
    assert fresh.startswith("### Verify: not checked yet")
    assert "- ⬜ open · agent · Run it" in fresh
    assert "- ⬜ open · human · Look at it" in fresh


def test_results_markdown_for_a_legacy_run_names_the_repo_target():
    plan = _plan(runs=[{"at": 1.0, "target": "https://app.example.com"}])
    assert tp.results_markdown(plan).startswith(
        "### Verify: checked on https://app.example.com"
    )
    assert tp.results_markdown({}).startswith("### Verify: not checked yet")


# --------------------------------------------------------------------------- #
# _ensure_test_plan(environment=, notes=) steers the writing
# --------------------------------------------------------------------------- #
class _Now:
    """threading.Thread, run on the spot."""

    def __init__(self, target, args=(), **_kw):
        self._run = lambda: target(*args)

    def start(self):
        self._run()


@pytest.mark.parametrize(
    "environment, want",
    [
        ("sandbox", "https://sandbox.example.com"),
        ("branch", ""),
        ("", None),
        ("nope", None),
    ],
)
def test_ensure_test_plan_steers_generation(
    acme, tmp_path, monkeypatch, environment, want
):
    from backend.web import server

    wt = tmp_path / "wt"
    wt.mkdir()
    inst = SimpleNamespace(
        Program="claude",
        GetWorktreePath=lambda: str(wt),
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: acme),
    )
    monkeypatch.setitem(server.ENGINE.instances, "sess", inst)
    monkeypatch.setattr(server, "_current_branch", lambda w: "feature/x")
    monkeypatch.setattr(server, "_git_head_sha", lambda w: "c" * 40)
    monkeypatch.setattr(tpd, "_test_plan_intent", lambda *a: "")
    monkeypatch.setattr(tpd, "threading", SimpleNamespace(Thread=_Now))
    calls: list = []
    monkeypatch.setattr(
        tpd, "_generate_test_plan", lambda *a, **k: calls.append((a, k))
    )

    server._ensure_test_plan(
        "sess", manual=True, environment=environment, notes="Mind the migration."
    )

    assert len(calls) == 1
    assert calls[0][1]["target"] == want
    assert calls[0][1]["focus"] == "Mind the migration."


def test_generation_target_and_focus_reach_the_prompt(acme, tmp_path, monkeypatch):
    """``generate(target=, focus=)`` replaces the repo's target and adds to the
    plan's own focus — for this generation only, nothing stored."""
    seen: dict = {}

    def capture(*a, **k):
        seen.update(k)
        raise tp.TestPlanError("captured")

    monkeypatch.setattr(tp, "_range_diff", lambda cwd, plan: ("1 file", "diff"))
    monkeypatch.setattr(tp, "build_generation_prompt", capture)
    wt = tmp_path / "wt"
    wt.mkdir()
    tp.upsert(_plan("sc-1", state="generated", focus="ignore the refactor"))

    tp.generate(
        "sc-1", "claude", str(wt), target="https://sandbox.example.com", focus="n"
    )

    assert seen["target"] == "https://sandbox.example.com"
    assert seen["focus"] == "ignore the refactor\nn"
    assert tp.get("sc-1")["focus"] == "ignore the refactor"

    seen.clear()
    tp.generate("sc-1", "claude", str(wt))
    assert seen["target"] == "https://app.example.com"
    assert seen["focus"] == "ignore the refactor"


# --------------------------------------------------------------------------- #
# Settings: the sanitizer's edges
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [None, "sandbox", {"name": "sandbox"}, 3])
def test_environments_that_are_not_a_list_read_as_none(raw):
    assert settings_mod._verify_environments(raw) == []


def test_environment_names_are_folded_and_must_be_plain():
    got = settings_mod._verify_environments(
        [
            {"name": "  Staging "},
            {"name": "a b"},
            {"name": "é"},
            {"name": "x" * 40},
            {"name": "Sandbox", "reach": "first"},
            {"name": "sandbox", "reach": "second"},
            None,
            ["sandbox"],
        ]
    )
    assert [e["name"] for e in got] == ["staging", "x" * 40, "sandbox"]
    assert got[2]["reach"] == "first"


@pytest.mark.parametrize(
    "branch, kept",
    [
        ("release/2026.11", "release/2026.11"),
        ("sandbox", "sandbox"),
        ("-evil", ""),
        ("../x", ""),
        ("has space", ""),
        ("/abs", ""),
        ("b" * 200, "b" * 200),
        ("b" * 201, ""),
    ],
)
def test_an_implausible_branch_is_blanked_not_dropped(branch, kept):
    got = settings_mod._verify_environments([{"name": "qa", "branch": branch}])
    assert len(got) == 1  # the entry survives either way
    assert got[0]["branch"] == kept


@pytest.mark.parametrize(
    "raw, want",
    [
        (None, 0),
        (-5, 0),
        ("abc", 0),
        (True, 0),
        (2.5, 0),
        ("12", 12),
        (1440, 1440),
        (1441, 1440),
    ],
)
def test_deploy_minutes_are_clamped_and_garbage_reads_as_zero(raw, want):
    got = settings_mod._verify_environments([{"name": "qa", "deploy_minutes": raw}])
    assert got[0]["deploy_minutes"] == want


def test_reach_is_cut_and_a_blank_reach_is_kept_blank():
    got = settings_mod._verify_environments(
        [{"name": "a", "reach": "r" * 501}, {"name": "b", "reach": "   "}]
    )
    assert got[0]["reach"] == "r" * settings_mod.VERIFY_TARGET_MAX
    assert got[1]["reach"] == ""


def test_to_dict_is_a_deep_enough_copy():
    repo = settings_mod.RepositorySettings.from_dict(
        {
            "verify_repo_settings": {
                "acme/app": {"target": "t", "environments": [{"name": "sandbox"}]}
            }
        }
    )
    d = repo.to_dict()
    d["verify_repo_settings"]["acme/app"]["environments"][0]["name"] = "changed"
    d["verify_repo_settings"]["acme/app"]["environments"].append({"name": "x"})
    d["verify_repo_settings"]["acme/app"]["target"] = "changed"
    block = repo.verify_repo_settings["acme/app"]
    assert block["environments"] == [
        {"name": "sandbox", "reach": "", "branch": "", "deploy_minutes": 0}
    ]
    assert block["target"] == "t"


def test_a_settings_file_with_bad_environments_loads_sanitized(repo_settings):
    """Sync and import both land as a settings file that is then loaded, and the
    load is where the sanitizer runs — so a malformed entry from another device
    is cleaned the same way as a hand-edited one."""
    repo_settings(
        verify_repo_settings={
            "acme/app": {
                "environments": [
                    {"name": "Sandbox", "branch": "../evil", "deploy_minutes": -1},
                    {"name": "branch"},
                    "junk",
                ]
            }
        }
    )
    block = settings_mod.load_settings().repository.verify_repo_settings
    assert block["acme/app"]["environments"] == [
        {"name": "sandbox", "reach": "", "branch": "", "deploy_minutes": 0}
    ]


# --------------------------------------------------------------------------- #
# environments() / environments_by_slug() edges
# --------------------------------------------------------------------------- #
def test_a_configured_live_with_no_branch_keeps_no_branch(monkeypatch, repo_settings):
    """A listed ``live`` replaces the implicit one outright: its blank branch is
    "does not deploy from git", not "fall back to the resolved live branch"."""
    monkeypatch.setattr(tp, "repo_slug", lambda root: "acme/app")
    repo_settings(
        live_branch="release",
        verify_repos=["acme/app"],
        verify_repo_settings={
            "acme/app": {"environments": [{"name": "live", "reach": "#prod"}]}
        },
    )
    live = tp.environments("/repo")[0]
    assert live == {
        "name": "live",
        "reach": "#prod",
        "branch": "",
        "deploy_minutes": 0,
        "implicit": False,
    }


def test_environments_by_slug_skips_blank_and_repeated_slugs(repo_settings):
    repo_settings(verify_repos=["", "acme/app", "acme/app", "acme/lib"])
    got = tp.environments_by_slug()
    assert list(got) == ["acme/app", "acme/lib"]
    # Neither has a block: each is just the implicit live.
    assert [[e["name"] for e in v] for v in got.values()] == [["live"], ["live"]]


# --------------------------------------------------------------------------- #
# probe_live(fetch=False) and env_reached_at's fetch throttle
# --------------------------------------------------------------------------- #
@pytest.fixture
def fetches(monkeypatch):
    """Count (and optionally forbid) the ``git fetch`` calls test_plans makes."""
    real = tp.gitutil.git
    seen: list = []
    state = SimpleNamespace(forbid=False, seen=seen)

    def git(args, *a, **k):
        if args and args[0] == "fetch":
            assert not state.forbid, "fetched with fetch=False"
            seen.append(tuple(args))
        return real(args, *a, **k)

    monkeypatch.setattr(tp.gitutil, "git", git)
    return state


def test_probe_live_without_fetch_answers_from_the_local_ref(origin_and_clone, fetches):
    o = origin_and_clone
    fetches.forbid = True
    # origin moved, but `work` was not told: the local ref still says "not yet".
    _git(o.other, "push", "-q", "origin", "origin/feature:refs/heads/sandbox")
    assert tp.probe_live(str(o.work), o.sha, "sandbox", fetch=False) == "waiting"
    # No local ref at all is "missing" — nothing was asked of the network.
    assert tp.probe_live(str(o.work), o.sha, "nowhere", fetch=False) == "missing"
    assert fetches.seen == []

    fetches.forbid = False
    assert tp.probe_live(str(o.work), o.sha, "sandbox") == "live"
    assert len(fetches.seen) == 1
    # ...and once fetched, the local half alone knows.
    fetches.forbid = True
    assert tp.probe_live(str(o.work), o.sha, "sandbox", fetch=False) == "live"
    fetches.forbid = False
    assert tp.probe_live(str(o.work), o.sha, "nowhere") == "missing"


def test_env_reached_at_fetches_once_per_repo_and_branch_per_ttl(
    origin_and_clone, fetches, monkeypatch
):
    o = origin_and_clone
    tp.upsert(_plan("sc-1", repo_root=str(o.work), sha=o.sha, tip_sha=o.sha))
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(tp.time, "time", lambda: clock.now)

    assert tp.env_reached_at("sc-1", SANDBOX) == 0.0
    assert tp.env_reached_at("sc-1", SANDBOX) == 0.0
    assert len(fetches.seen) == 1

    # Another branch on the same repo is its own key.
    assert tp.env_reached_at("sc-1", {"name": "prod", "branch": "main"}) == 0.0
    assert len(fetches.seen) == 2

    clock.now += tp.ENV_FETCH_TTL - 1
    tp.env_reached_at("sc-1", SANDBOX)
    assert len(fetches.seen) == 2

    clock.now += 1
    tp.env_reached_at("sc-1", SANDBOX)
    assert len(fetches.seen) == 3


def test_env_reached_at_falls_back_to_the_tip(monkeypatch):
    asked: list = []

    def probe(root, sha, branch, fetch=True):
        asked.append(sha)
        return "live"

    monkeypatch.setattr(tp, "probe_live", probe)
    monkeypatch.setattr(tp, "_ENV_FETCHED", {})
    monkeypatch.setattr(tp.time, "time", lambda: 42.0)
    tp.upsert(_plan("sc-1", sha="", tip_sha="c" * 40))
    assert tp.env_reached_at("sc-1", SANDBOX) == 42.0
    assert asked == ["c" * 40]


def test_env_reached_at_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(tp, "probe_live", boom)
    monkeypatch.setattr(tp, "_ENV_FETCHED", {})
    tp.upsert(_plan("sc-1"))
    assert tp.env_reached_at("sc-1", SANDBOX) == 0.0
    assert tp.get("sc-1")["env_seen"] == {}


def test_a_racing_first_sighting_is_kept(monkeypatch):
    """Two callers both see the work arrive; whichever stamps first is the
    answer, and the later one adopts it rather than restarting the clock."""
    monkeypatch.setattr(tp, "_ENV_FETCHED", {})
    monkeypatch.setattr(tp.time, "time", lambda: 900.0)
    tp.upsert(_plan("sc-1"))

    def probe(*a, **k):
        # The other caller stamps between this one's read and its write.
        plan = tp.get("sc-1")
        plan["env_seen"] = {"sandbox": 100.0}
        tp.upsert(plan)
        return "live"

    monkeypatch.setattr(tp, "probe_live", probe)
    assert tp.env_reached_at("sc-1", SANDBOX) == 100.0
    assert tp.get("sc-1")["env_seen"] == {"sandbox": 100.0}


def test_env_seen_is_capped_and_cleaned():
    seen = {"e%d" % i: float(i + 1) for i in range(60)}
    seen.update({"neg": -3.0, "zero": 0, "nan": "x"})
    tp.upsert(_plan("sc-1", env_seen=seen))
    got = tp.get("sc-1")["env_seen"]
    assert len(got) == tp.MAX_ENV_SEEN
    assert all(v > 0 for v in got.values())
    tp.upsert(_plan("sc-2", env_seen=["sandbox"]))
    assert tp.get("sc-2")["env_seen"] == {}


def test_env_seen_survives_a_rewrite(acme, tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(
        tp,
        "_generate_steps",
        lambda *a, **k: (
            "New summary.",
            [{"id": "n1", "text": "New", "expect": "", "actor": "agent"}],
            "",
        ),
    )
    tp.upsert(_plan("sc-1", state="generating", env_seen={"sandbox": 5.0}))
    plan = tp.generate("sc-1", "claude", str(wt), refresh=True)
    assert [s["text"] for s in plan["steps"]] == ["New"]
    assert tp.get("sc-1")["env_seen"] == {"sandbox": 5.0}


# --------------------------------------------------------------------------- #
# Run records: where / checkout / target
# --------------------------------------------------------------------------- #
def test_a_legacy_run_has_no_where():
    tp.upsert(_plan("sc-1", runs=[{"at": 1.0, "results": {}}]))
    run = tp.get("sc-1")["runs"][-1]
    assert run["where"] == "" and run["checkout"] == ""


def test_where_and_checkout_are_capped():
    tp.upsert(
        _plan("sc-1", runs=[{"at": 1.0, "where": "w" * 99, "checkout": "c" * 999}])
    )
    run = tp.get("sc-1")["runs"][-1]
    assert len(run["where"]) <= 40 and len(run["checkout"]) <= 250


def test_start_run_stamps_only_what_it_is_given():
    tp.upsert(_plan("sc-1"))
    tp.start_run("sc-1", "verify-sc-1")
    run = tp.get("sc-1")["runs"][-1]
    # target=None: the finish stamps the repo's target, as before.
    assert run["where"] == "" and run["checkout"] == "" and run["target"] == ""
    assert tp.run_target(tp.get("sc-1")) == tp.verify_target("/repo")

    tp.upsert(_plan("sc-2"))
    tp.start_run("sc-2", "v", where="branch", target="", checkout="b" * 40)
    run = tp.get("sc-2")["runs"][-1]
    assert (run["where"], run["target"], run["checkout"]) == ("branch", "", "b" * 40)

    # A checkout without a where is not recorded: it means nothing on its own.
    tp.upsert(_plan("sc-3"))
    tp.start_run("sc-3", "v", where="", checkout="origin/x")
    assert tp.get("sc-3")["runs"][-1]["checkout"] == ""


def test_run_target_prefers_the_runs_own_stamp(acme):
    sandbox = _plan(runs=[{"where": "sandbox", "target": "https://sb"}])
    assert tp.run_target(sandbox) == "https://sb"
    branch = _plan(runs=[{"where": "branch", "target": ""}])
    assert tp.run_target(branch) == ""
    # A where-run never falls back to the repo's target, even unstamped.
    assert tp.run_target(_plan(runs=[{"where": "qa"}])) == ""
    assert tp.run_target(_plan(runs=[{"at": 1.0}])) == "https://app.example.com"
    assert tp.run_target(_plan(runs=[])) == "https://app.example.com"


# --------------------------------------------------------------------------- #
# build_run_prompt with a where
# --------------------------------------------------------------------------- #
def test_a_branch_prompt_ignores_any_target():
    prompt = tp.build_run_prompt(
        _plan(), None, target="https://app.example.com", where="BRANCH"
    )
    assert "https://app.example.com" not in prompt
    assert "NOTHING IS RUNNING HERE" in prompt
    assert "git checkout --detach %s" % ("b" * 40) in prompt


def test_an_environment_prompt_says_the_checkout_is_for_reading():
    prompt = tp.build_run_prompt(
        _plan(),
        None,
        target="https://sb.example.com",
        where="sandbox",
        checkout_branch="sandbox",
    )
    assert "https://sb.example.com" in prompt
    assert "ONLY TO READ CODE" in prompt
    assert "origin/sandbox" in prompt


def test_no_where_and_no_notes_is_the_same_prompt():
    plan = _plan()
    kw = dict(live=True, repo_notes="Standing.", target="https://app.example.com")
    base = tp.build_run_prompt(plan, None, **kw)
    assert tp.build_run_prompt(plan, None, where="", notes="", **kw) == base
    assert "Notes for this check" not in base


def test_notes_follow_the_standing_notes_even_without_any():
    prompt = tp.build_run_prompt(_plan(), None, notes="Just this once.")
    assert "Notes for this check: Just this once." in prompt


# --------------------------------------------------------------------------- #
# generate(target="") — the one-shot says "no deployment"
# --------------------------------------------------------------------------- #
def test_a_blank_generation_target_is_no_deployment(acme, tmp_path, monkeypatch):
    seen: dict = {}

    def capture(*a, **k):
        seen.update(k)
        raise tp.TestPlanError("captured")

    monkeypatch.setattr(tp, "_range_diff", lambda cwd, plan: ("1 file", "diff"))
    monkeypatch.setattr(tp, "build_generation_prompt", capture)
    wt = tmp_path / "wt"
    wt.mkdir()
    tp.upsert(_plan("sc-1", state="generated"))
    before = tp.get("sc-1")
    tp.generate("sc-1", "claude", str(wt), target="")
    assert seen["target"] == ""
    after = tp.get("sc-1")
    for k in ("live_branch", "focus", "repo_root"):
        assert after[k] == before[k]


# --------------------------------------------------------------------------- #
# results_markdown edges
# --------------------------------------------------------------------------- #
def test_results_markdown_with_no_steps():
    md = tp.results_markdown(_plan(steps=[], runs=[]))
    assert md.splitlines() == [
        "### Verify: not checked yet",
        "",
        "No behaviors written yet.",
    ]


def test_results_markdown_keeps_every_line_one_line():
    plan = _plan(
        steps=[{"id": "s1", "text": "a\n\nb", "expect": "", "actor": "agent"}],
        runs=[
            {
                "at": 1.0,
                "where": "qa",
                "target": "line\none",
                "results": {
                    "s1": {"result": "fail", "note": "n" * 400 + "\n- not a bullet"}
                },
            }
        ],
    )
    lines = tp.results_markdown(plan).splitlines()
    assert lines[0] == "### Verify: checked on the **qa** environment (line one)"
    step = [ln for ln in lines if ln.startswith("- ")]
    assert len(step) == 1
    assert step[0].startswith("- ❌ fail · agent · a b — ")
    assert step[0].endswith("…")
    assert len(step[0].split(" — ", 1)[1]) == 300


def test_results_markdown_never_raises_on_junk():
    for junk in (None, [], "x", {"runs": ["x"], "steps": ["y", None]}):
        assert tp.results_markdown(junk).startswith("### Verify:")


# --------------------------------------------------------------------------- #
# _generation_steer and the refresh path
# --------------------------------------------------------------------------- #
def test_generation_steer(acme, monkeypatch, repo_settings):
    assert tpd._generation_steer(acme, "") is None
    assert tpd._generation_steer(acme, "   ") is None
    assert tpd._generation_steer(acme, "BRANCH") == ""
    assert tpd._generation_steer(acme, "Sandbox") == "https://sandbox.example.com"
    assert tpd._generation_steer(acme, "live") == "https://app.example.com"
    assert tpd._generation_steer(acme, "nope") is None
    repo_settings(
        verify_repos=["acme/app"],
        verify_repo_settings={"acme/app": {"environments": [{"name": "canary"}]}},
    )
    assert tpd._generation_steer(acme, "canary") == ""


def _steer_session(monkeypatch, tmp_path, repo_root):
    from backend.web import server

    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    inst = SimpleNamespace(
        Program="claude",
        GetWorktreePath=lambda: str(wt),
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: repo_root),
    )
    monkeypatch.setitem(server.ENGINE.instances, "sess", inst)
    monkeypatch.setattr(server, "_current_branch", lambda w: "feature/x")
    monkeypatch.setattr(server, "_git_head_sha", lambda w: "c" * 40)
    monkeypatch.setattr(tpd, "_test_plan_intent", lambda *a: "")
    monkeypatch.setattr(tpd, "threading", SimpleNamespace(Thread=_Now))
    calls: list = []
    monkeypatch.setattr(
        tpd, "_generate_test_plan", lambda *a, **k: calls.append((a, k))
    )
    return calls


def test_the_two_argument_callers_still_write_for_the_repo(acme, tmp_path, monkeypatch):
    from backend.web import server

    calls = _steer_session(monkeypatch, tmp_path, acme)
    server._ensure_test_plan("sess", True)
    assert calls[0][1]["target"] is None and calls[0][1]["focus"] == ""


def test_a_refresh_is_steered_too(acme, tmp_path, monkeypatch):
    from backend.web import server

    calls = _steer_session(monkeypatch, tmp_path, acme)
    monkeypatch.setattr(tp, "ensure_plan_for", lambda *a, **k: None)
    monkeypatch.setattr(tp, "owner_for_branch", lambda *a: "owner")
    monkeypatch.setattr(tp, "refresh_for_push", lambda *a, **k: {"id": "owner"})

    server._ensure_test_plan(
        "sess", manual=True, environment="sandbox", notes="Mind the migration."
    )

    assert len(calls) == 1
    args, kw = calls[0]
    assert args[0] == "owner" and kw["refresh"] is True
    assert kw["target"] == "https://sandbox.example.com"
    assert kw["focus"] == "Mind the migration."


# --------------------------------------------------------------------------- #
# _verify_run_trees for a run with a where
# --------------------------------------------------------------------------- #
@pytest.fixture
def run_session(monkeypatch, tmp_path):
    """A verify session record for ``verify-sc-1`` whose HEAD is ``t…t``."""
    from backend.web import server

    wt = tmp_path / "verify-wt"
    wt.mkdir()
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"verify-sc-1": SimpleNamespace(GetWorktreePath=lambda: str(wt))},
    )
    monkeypatch.setattr(server, "_git_head_sha", lambda w: "t" * 40)
    return "verify-sc-1"


def _where_plan(root, checkout, where="sandbox", **over):
    return _plan(
        "sc-1",
        repo_root=str(root),
        run_session="verify-sc-1",
        runs=[{"at": 1.0, "where": where, "checkout": checkout}],
        **over,
    )


def test_run_trees_resolve_an_environment_checkout(origin_and_clone, run_session):
    o = origin_and_clone
    want = _git(o.work, "rev-parse", "origin/sandbox")
    plan = _where_plan(o.work, "origin/sandbox")
    assert tpd._verify_run_trees(plan) == ("t" * 40, want)


def test_run_trees_an_unresolvable_checkout_is_unknown(origin_and_clone, run_session):
    o = origin_and_clone
    plan = _where_plan(o.work, "origin/never-pushed")
    assert tpd._verify_run_trees(plan) == ("t" * 40, "")


def test_run_trees_a_sha_checkout_is_its_own_answer(run_session):
    plan = _where_plan("/repo", "d" * 40, where="branch")
    assert tpd._verify_run_trees(plan) == ("t" * 40, "d" * 40)
    blank = _where_plan("/repo", "", where="canary")
    assert tpd._verify_run_trees(blank) == ("t" * 40, "b" * 40)
    no_tip = _where_plan("/repo", "", where="canary", tip_sha="")
    assert tpd._verify_run_trees(no_tip) == ("t" * 40, "a" * 40)


def test_run_trees_without_a_where_are_unchanged(origin_and_clone, run_session):
    """A legacy run on a shipped plan expects origin/<live>, as before."""
    o = origin_and_clone
    want = _git(o.work, "rev-parse", "origin/main")
    plan = _plan(
        "sc-1",
        repo_root=str(o.work),
        run_session="verify-sc-1",
        live_branch="main",
        runs=[{"at": 1.0, "checkout": "origin/sandbox"}],
    )
    assert tpd._verify_run_trees(plan) == ("t" * 40, want)
    early = dict(plan, live_at=0.0)
    assert tpd._verify_run_trees(early) == ("t" * 40, "b" * 40)


# --------------------------------------------------------------------------- #
# The poller stamps the run's own target
# --------------------------------------------------------------------------- #
def _finish_with(monkeypatch, tmp_path, start_kw: dict, trees=("", "")):
    from backend.web import server

    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    (wt / tp.RESULT_FILE).write_text(
        json.dumps(
            {
                "plan": "sc-1",
                "finished": True,
                "results": [{"id": "s1", "result": "pass", "note": "ok"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"verify-sc-1": SimpleNamespace(GetWorktreePath=lambda: str(wt))},
    )
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    monkeypatch.setattr(tpd, "_announce_test_plan_checked", lambda plan: None)
    if trees is not None:
        monkeypatch.setattr(tpd, "_verify_run_trees", lambda plan: trees)
    tp.upsert(_plan("sc-1", state="due", live_at=50.0))
    tp.start_run("sc-1", "verify-sc-1", **start_kw)
    server._poll_running_test_plans(tp.list_plans())
    return tp.get("sc-1")


@pytest.mark.parametrize(
    "start_kw, want",
    [
        ({}, "https://app.example.com"),
        ({"where": "branch", "target": "", "checkout": "b" * 40}, ""),
        (
            {"where": "sandbox", "target": "https://sb", "checkout": "origin/sandbox"},
            "https://sb",
        ),
    ],
)
def test_the_poller_stamps_the_runs_own_target(
    acme, tmp_path, monkeypatch, start_kw, want
):
    plan = _finish_with(monkeypatch, tmp_path, start_kw)
    assert plan["state"] == "done"
    assert plan["runs"][-1]["target"] == want


def test_a_rev_parse_timeout_does_not_stop_the_poller(acme, tmp_path, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0] if a else "git", 15)

    monkeypatch.setattr(tpd.subprocess, "run", slow)
    from backend.web import server

    monkeypatch.setattr(server, "_git_head_sha", lambda w: "t" * 40)
    plan = _finish_with(
        monkeypatch,
        tmp_path,
        {"where": "sandbox", "target": "https://sb", "checkout": "origin/sandbox"},
        trees=None,
    )
    # Not folded in this pass — the run is still open, and the next pass retries.
    assert plan["state"] == "running"
    assert (tmp_path / "wt" / tp.RESULT_FILE).exists()


# --------------------------------------------------------------------------- #
# The run route's edges
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["Sandbox", "  sandbox  ", "SANDBOX"])
def test_the_environment_name_is_folded(client, acme, name):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": name})
    assert r.status_code == 202, r.text
    assert tp.get("sc-1")["runs"][-1]["where"] == "sandbox"


def test_branch_needs_no_environments(client, monkeypatch, repo_settings):
    monkeypatch.setattr(tp, "repo_slug", lambda root: "other/repo")
    repo_settings(verify_repos=[])
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "branch"})
    assert r.status_code == 202, r.text
    assert tp.get("sc-1")["runs"][-1]["where"] == "branch"


def test_an_unknown_environment_touches_nothing(client, acme, monkeypatch):
    from backend.web import server

    started: list = []
    monkeypatch.setattr(tp, "start_run", lambda *a, **k: started.append(a))
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "nope"})
    assert r.status_code == 400
    assert started == [] and client.made == []
    assert server.ENGINE.instances == {}
    plan = tp.get("sc-1")
    assert plan["run_session"] == "" and plan["runs"] == []


@pytest.mark.parametrize("body", [{"notes": 3}, {"notes": {"a": 1}}])
def test_non_string_notes_are_a_400(client, acme, body):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json=body)
    assert r.status_code == 400
    assert r.json() == {"error": "notes must be a string"}


def test_a_non_string_environment_says_so(client, acme):
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": ["sandbox"]})
    assert r.status_code == 400
    assert r.json() == {"error": "environment must be a string"}


def test_a_running_plan_is_a_409_before_any_environment_lookup(
    client, acme, monkeypatch
):
    from backend.web import server

    looked: list = []
    monkeypatch.setattr(tp, "environment", lambda *a, **k: looked.append(a) or None)
    monkeypatch.setattr(server, "_verify_session_usable", lambda t: True)
    tp.upsert(_plan("sc-1", state="running", run_session="verify-sc-1"))
    r = client.post("/api/test-plans/sc-1/run", json={"environment": "nope"})
    assert r.status_code == 409
    assert looked == [] and client.made == []


def test_environment_names_are_deduplicated_across_repos(client, repo_settings):
    repo_settings(
        verify_repos=["acme/app", "acme/lib"],
        verify_repo_settings={
            "acme/app": {"environments": [{"name": "sandbox"}, {"name": "zeta"}]},
            "acme/lib": {"environments": [{"name": "sandbox"}, {"name": "alpha"}]},
        },
    )
    r = client.get("/api/test-plans/environments")
    assert r.json()["names"] == ["alpha", "live", "sandbox", "zeta"]


def test_the_environments_route_is_registered_before_plan_id_routes():
    from starlette.routing import Route

    from backend.web import server

    # Every method, not just GET: none of the greedy routes is a GET today, and
    # a GET-only filter would make this pass by comparing against nothing.
    paths = [r.path for r in server.app.routes if isinstance(r, Route)]
    mine = paths.index("/api/test-plans/environments")
    greedy = [
        i for i, p in enumerate(paths) if p.startswith("/api/test-plans/{plan_id")
    ]
    assert greedy, "no {plan_id:path} routes found — the check would be vacuous"
    assert all(mine < i for i in greedy)


# --------------------------------------------------------------------------- #
# End to end: a sandbox run, its results, its comment
# --------------------------------------------------------------------------- #
def test_a_sandbox_run_from_route_to_results_comment(
    client, acme, tmp_path, monkeypatch
):
    from backend.web import server

    tp.upsert(_plan("sc-1"))
    r = client.post(
        "/api/test-plans/sc-1/run",
        json={"environment": "sandbox", "notes": "Mind the migration."},
    )
    assert r.status_code == 202, r.text
    assert "Notes for this check: Mind the migration." in client.made[0]["prompt"]
    plan = tp.get("sc-1")
    run_session = plan["run_session"]
    assert run_session

    wt = tmp_path / "e2e-wt"
    wt.mkdir()
    (wt / tp.RESULT_FILE).write_text(
        json.dumps(
            {
                "plan": "sc-1",
                "finished": True,
                "results": [{"id": "s1", "result": "pass", "note": "it ran"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(
        server.ENGINE.instances,
        run_session,
        SimpleNamespace(GetWorktreePath=lambda: str(wt)),
    )
    monkeypatch.setattr(server, "_live_session_name", lambda name: name)
    monkeypatch.setattr(tpd, "_announce_test_plan_checked", lambda plan: None)
    monkeypatch.setattr(tpd, "_verify_run_trees", lambda plan: ("", ""))
    server._poll_running_test_plans(tp.list_plans())

    plan = tp.get("sc-1")
    run = plan["runs"][-1]
    assert (run["where"], run["target"], run["checkout"]) == (
        "sandbox",
        "https://sandbox.example.com",
        "origin/sandbox",
    )
    md = tp.results_markdown(plan)
    assert md.splitlines()[0] == (
        "### Verify: checked on the **sandbox** environment "
        "(https://sandbox.example.com)"
    )
    assert "- ✅ pass · agent · Run it — it ran" in md
    assert "- ⬜ open · human · Look at it" in md
