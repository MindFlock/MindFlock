"""flow-polish (fp-flight-driver): what stops a route is said BEFORE it
creates anything (``flight_driver.preflight``, used by ``start`` / ``attach``
and a dry run) — a mistyped Verify environment, a branch a live session
already holds, a Ship stop on a session sharing its folder — ad-hoc step ids
reach ``start_at``, and a Checks stop that can't read the checks says why.
Against fakes only."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import github_pr as GH
from backend.web.core import test_plans as T

NOW = time.time()
ENVS = [
    {"name": "live", "implicit": True},
    {"name": "staging", "implicit": False},
]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_CHECKS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(T, "environments", lambda repo: list(ENVS))
    monkeypatch.setattr(
        T,
        "environment",
        lambda repo, name: next(
            (e for e in ENVS if e["name"] == str(name).lower()), None
        ),
    )


class FakeInst:
    def __init__(self, path="/repo", branch="feat"):
        self.Path = path
        self.Branch = branch
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Path)


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


@pytest.fixture
def created(monkeypatch):
    import backend.web.core.session_create as sc

    made = []

    async def fake_create(payload, **kw):
        made.append(payload)
        return 202, {"title": "new-1"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    return made


def run(coro):
    return asyncio.run(coro)


VERIFY_TYPO = [
    {"kind": "agent", "config": {"prompt": "build it"}},
    {"kind": "ship", "config": {"lane": "pr"}},
    {"kind": "verify", "config": {"where": "stagin"}},
]


# --------------------------------------------------------------------------- #
# preflight itself (the dry run's contract)
# --------------------------------------------------------------------------- #
def test_preflight_names_a_mistyped_environment_and_the_ones_there_are(srv):
    got = D.preflight(VERIFY_TYPO, repo="/repo")
    assert got == [
        {
            "field": "steps[2].config.where",
            "detail": "Verify: no environment named stagin in this repo — it has "
            "live, staging",
        }
    ]


def test_preflight_is_clear_for_a_good_route(srv):
    steps = [dict(s) for s in VERIFY_TYPO]
    steps[2] = {"kind": "verify", "config": {"where": "Staging"}}
    assert D.preflight(steps, repo="/repo") == []
    assert D.preflight({"steps": [{"kind": "verify"}]}, repo="/repo") == []
    # "live" always exists; an unknown repo is not checked.
    assert (
        D.preflight([{"kind": "verify", "config": {"where": "live"}}], repo="/r") == []
    )
    assert D.preflight(VERIFY_TYPO, repo="") == []


def test_preflight_skips_stops_before_the_start(srv):
    plan = F.save_plan({"name": "P", "steps": list(VERIFY_TYPO) + [{"kind": "notify"}]})
    assert D.preflight(plan, repo="/repo", start_at="s4") == []
    assert len(D.preflight(plan, repo="/repo", start_at="s3")) == 1


def test_preflight_reports_a_bad_start_and_bad_steps(srv):
    assert D.preflight([{"kind": "notify"}], repo="/r", start_at="nope") == [
        {"field": "start_at", "detail": "no stop nope on this route"}
    ]
    bad = D.preflight([{"kind": "nonsense"}], repo="/r")
    assert len(bad) == 1 and bad[0]["field"].startswith("steps[0]")


def test_preflight_names_the_session_holding_the_branch(srv):
    srv.ENGINE.instances["sc-12"] = FakeInst(branch="feature/sc-12/login")
    got = D.preflight([{"kind": "agent"}], repo="/repo", branch="feature/sc-12/login")
    assert len(got) == 1
    assert got[0]["field"] == "branch" and got[0]["session"] == "sc-12"
    assert got[0]["detail"] == (
        "sc-12 already has this branch — run the route on that session instead"
    )
    # Another repo's session on a same-named branch is not in the way.
    srv.ENGINE.instances["sc-12"] = FakeInst(
        path="/other", branch="feature/sc-12/login"
    )
    assert (
        D.preflight([{"kind": "agent"}], repo="/repo", branch="feature/sc-12/login")
        == []
    )


def _shared(title, lane):
    return SimpleNamespace(
        body=json.dumps(
            {
                "error": "s-copy works in this same folder — shipping by itself "
                "here would commit both sessions' work as one",
                "shared_with": "s-copy",
            }
        ).encode()
    )


def test_preflight_says_a_ship_stop_on_a_shared_folder_up_front(srv):
    srv._lane_refusal = _shared
    got = D.preflight(VERIFY_TYPO[:2], repo="/repo", session="s", attach=True)
    assert got == [
        {
            "field": "steps[1]",
            "code": "shared_folder",
            "detail": "s-copy works in this same folder — shipping by itself "
            "here would commit both sessions' work as one",
            "session": "s-copy",
        }
    ]
    # No Ship stop: nothing to refuse.
    assert D.preflight([{"kind": "notify"}], repo="/r", session="s", attach=True) == []


# --------------------------------------------------------------------------- #
# start / attach refuse BEFORE creating anything
# --------------------------------------------------------------------------- #
def test_start_refuses_a_mistyped_environment_before_creating_a_session(srv, created):
    with pytest.raises(F.PlanError) as err:
        run(D.start({"steps": VERIFY_TYPO, "repo_path": "/repo"}))
    assert created == []
    assert err.value.field == "steps[2].config.where"
    assert "no environment named stagin" in err.value.message
    assert isinstance(err.value, D.PreflightError)
    assert err.value.problems[0]["field"] == "steps[2].config.where"
    assert F.list_flights() == []


def test_start_on_a_session_refuses_a_ship_stop_that_would_be_refused(srv, created):
    srv._lane_refusal = _shared
    with pytest.raises(D.PreflightError) as err:
        run(D.start({"steps": VERIFY_TYPO[:2], "title": "s"}))
    assert err.value.session == "s-copy" and err.value.code == "shared_folder"
    assert F.list_flights() == []


def test_attach_refuses_before_creating_a_session(srv, created):
    g = run(D.ground({"steps": VERIFY_TYPO}))
    with pytest.raises(D.PreflightError):
        run(D.attach(g["id"], {"title": "", "repo_path": "/repo"}))
    assert created == []
    with pytest.raises(D.PreflightError):
        run(D.attach(g["id"], {"title": "s"}))
    assert F.get(g["id"])["state"] == F.GROUNDED


def test_a_good_route_still_starts(srv, created):
    steps = [{"kind": "agent", "config": {"prompt": "x"}}, {"kind": "verify"}]
    f = run(D.start({"steps": steps, "repo_path": "/repo"}))
    assert f["session"] == "new-1" and len(created) == 1


# --------------------------------------------------------------------------- #
# 6. Ad-hoc ids reach start_at
# --------------------------------------------------------------------------- #
def test_start_at_an_authors_own_step_id(srv, monkeypatch):
    real = F.adhoc_plan

    def with_map(raw, base=None, name="", id_map=None):
        plan = real(raw, base, name)
        if id_map is not None:
            for given, made in zip(raw, plan["steps"][-len(raw) :]):
                if given.get("id"):
                    id_map[given["id"]] = made["id"]
        return plan

    monkeypatch.setattr(F, "adhoc_plan", with_map)
    steps = [
        {"id": "plan", "kind": "agent", "config": {"prompt": "x"}},
        {"id": "build", "kind": "notify"},
        {"id": "ship", "kind": "notify"},
    ]
    f = run(D.start({"steps": steps, "title": "s", "start_at": "build"}))
    assert f["cursor"] == 1 and f["steps"][1]["id"] == "s2"


def test_an_older_adhoc_plan_without_id_map_still_works(srv, monkeypatch):
    real = F.adhoc_plan
    monkeypatch.setattr(
        F, "adhoc_plan", lambda raw, base=None, name="": real(raw, base, name)
    )
    f = run(D.start({"steps": [{"kind": "notify"}], "title": "s", "start_at": "s1"}))
    assert f["cursor"] == 0


# --------------------------------------------------------------------------- #
# 8. A Checks stop that can't read the checks says why
# --------------------------------------------------------------------------- #
def test_checks_unknown_says_why(srv, monkeypatch):
    async def why(wt, branch):
        return "unknown", "no GitHub token is set up"

    monkeypatch.setattr(GH, "pr_checks_why", why)
    f = {"id": "f1"}
    out = run(D._checks_obs(f, {"folder": "/repo", "branch": "feat"}, NOW))
    assert out["verdict"] == "unknown" and out["why"] == "no GitHub token is set up"
    D._CHECKS.clear()
    out = run(D._checks_obs(f, {"folder": "/repo", "branch": ""}, NOW))
    assert out["why"] == "the session is not on a branch"


def test_checks_known_carry_no_why(srv, monkeypatch):
    async def ok(wt, branch):
        return "ok", ""

    monkeypatch.setattr(GH, "pr_checks_why", ok)
    out = run(D._checks_obs({"id": "f1"}, {"folder": "/r", "branch": "b"}, NOW))
    assert out["verdict"] == "ok" and "why" not in out


def test_pr_checks_why_names_the_missing_pieces(monkeypatch):
    monkeypatch.setattr(GH, "repo_ref", lambda wt: None)
    assert run(GH.pr_checks_why("/r", "b")) == (
        "unknown",
        "this repo has no GitHub remote",
    )
    monkeypatch.setattr(GH, "repo_ref", lambda wt: SimpleNamespace(slug="o/r"))

    async def no_token():
        return ""

    monkeypatch.setattr(GH, "api_token", no_token)
    assert run(GH.pr_checks_why("/r", "b")) == ("unknown", "no GitHub token is set up")
    assert run(GH.pr_checks("/r", "b")) == "unknown"

    async def token():
        return "t"

    async def refuse(method, path, **kw):
        return 403, {"message": "Resource not accessible by integration"}

    monkeypatch.setattr(GH, "api_token", token)
    monkeypatch.setattr(GH, "_request", refuse)
    verdict, why = run(GH.pr_checks_why("/r", "b"))
    assert verdict == "unknown" and why.startswith("GitHub answered: ")
    assert "Resource not accessible" in why
