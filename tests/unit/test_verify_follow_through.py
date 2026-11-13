"""Verify follows a checklist through while its master switch is off.

``repository.verify_enabled`` off used to make every watcher pass skip every
plan, while a route's Verify stop and the Verify button went on creating and
walking checklists — so one owner's store held checklists stuck in
``generated`` for ever with ``merged_at`` 0. The switch now pauses only what
MindFlock does by itself (a push to a watched repo writing a plan): a checklist
that was ASKED for is watched until it ships. Plans record who wanted them
(``trigger``); a plan written before that field counts as asked.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from backend.web.core import test_plans as tp
from backend.web.core import test_plans_driver as tpd


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """An isolated plan store — autouse, so nothing here can touch a real one."""
    path = tmp_path / "test_plans.json"
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(path))
    tpd._TEST_PLAN_LIVE_CHECKED.clear()
    tpd._TEST_PLAN_LANDED_CHECKED.clear()
    return path


@pytest.fixture
def repo_settings():
    """Write ``[repository]`` into the conftest's isolated settings store."""
    from backend.config import settings as settings_mod

    path = Path(os.environ["MINDFLOCK_SETTINGS_FILE"])

    def write(**fields) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"repository": fields}), encoding="utf-8")
        settings_mod.invalidate()

    yield write
    settings_mod.invalidate()


_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="T",
    GIT_AUTHOR_EMAIL="t@example.com",
    GIT_COMMITTER_NAME="T",
    GIT_COMMITTER_EMAIL="t@example.com",
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_NOSYSTEM="1",
)


def _git(cwd, *args) -> str:
    cp = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_ENV,
    )
    return cp.stdout.decode().strip()


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
            {"id": "s1", "text": "Run it", "expect": "It runs", "actor": "agent"}
        ],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


# --------------------------------------------------------------------------- #
# Decision 1 — who asked for a checklist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "over, asked",
    [
        ({}, True),  # written before `trigger` existed: counts as asked
        ({"origin": "manual"}, True),  # e.g. pr-sitecheck-bot-3175
        ({"origin": ""}, True),  # e.g. sc-25247
        ({"trigger": "asked"}, True),
        ({"trigger": "push"}, False),
        ({"trigger": "push", "runs": [{"at": 1.0, "results": {}}]}, True),
    ],
)
def test_which_checklists_count_as_asked_for(over, asked):
    tp.upsert(_plan("p", **over))
    assert tp.is_asked_for(tp.get("p")) is asked


def test_an_unknown_trigger_reads_as_one_written_before_the_field(store):
    store.write_text(
        json.dumps({"version": 1, "plans": {"p": dict(_plan("p"), trigger="robot")}}),
        encoding="utf-8",
    )
    assert tp.get("p")["trigger"] == ""


def test_a_requested_plan_records_asked_and_a_push_records_push():
    assert tp.ensure_plan_for("a", "feat-a", "s", "/r", "main")["trigger"] == "asked"
    made = tp.ensure_plan_for("b", "feat-b", "s", "/r", "main", manual=True)
    assert made["trigger"] == "asked"
    pushed = tp.ensure_plan_for("c", "feat-c", "s", "/r", "main", trigger="push")
    assert pushed["trigger"] == "push"
    assert tp.get("c")["trigger"] == "push"  # survives the store round trip


def test_asking_for_a_push_made_plan_makes_it_asked_for():
    tp.ensure_plan_for("c", "feat-c", "s", "/r", "main", trigger="push")
    assert tp.ensure_plan_for("c", "feat-c", "s", "/r", "main", trigger="push") is None
    assert tp.get("c")["trigger"] == "push"
    # Another window asking for the same branch: the covering plan is the one.
    assert tp.ensure_plan_for("other", "feat-c", "s", "/r", "main") is None
    assert tp.get("c")["trigger"] == "asked"


def test_running_a_checklist_makes_it_asked_for():
    tp.upsert(_plan("p", trigger="push"))
    tp.start_run("p", "verify-p")
    assert tp.get("p")["trigger"] == "asked"


@pytest.fixture
def pushed(tmp_path, monkeypatch):
    """A registered session on a real repo whose push may write a plan."""
    from backend.web import server

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "f.txt").write_text("hello again\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "work")
    (repo / ".mindflock.toml").write_text(
        "[workspace]\nverify_on_push = true\n", encoding="utf-8"
    )

    class _Wt:
        def GetRepoPath(self):
            return str(repo)

    class _Inst:
        Program = "claude"
        Prompt = ""

        def __init__(self, open_session):
            self._open = open_session

        def GetWorktreePath(self):
            return str(repo)

        def GetGitWorktree(self):
            return _Wt()

        def Started(self):
            return self._open

        Status = None

    calls: list = []
    for mod in (server, tpd):
        monkeypatch.setattr(mod, "_generate_test_plan", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(tpd, "_test_plan_intent", lambda title: "")

    def register(title: str, open_session: bool) -> None:
        monkeypatch.setitem(server.ENGINE.instances, title, _Inst(open_session))

    return register, calls


@pytest.mark.parametrize("open_session", [True, False])
def test_a_push_to_a_watched_repo_records_its_plan_as_push(
    pushed, repo_settings, open_session
):
    register, _calls = pushed
    repo_settings(verify_enabled=True)
    register("sc-1", open_session)
    tpd._ensure_test_plan_blocking("sc-1")
    assert tp.get("sc-1")["trigger"] == "push"


def test_a_route_verify_stop_on_a_push_made_plan_makes_it_asked_for(
    pushed, repo_settings
):
    register, _calls = pushed
    repo_settings(verify_enabled=True)
    register("sc-1", True)
    tpd._ensure_test_plan_blocking("sc-1")
    assert tp.get("sc-1")["trigger"] == "push"
    assert tpd._ensure_verify_plan("sc-1") == ("sc-1", "")
    assert tp.get("sc-1")["trigger"] == "asked"


def test_with_the_switch_off_a_push_still_writes_nothing(pushed, repo_settings):
    register, calls = pushed
    repo_settings(verify_enabled=False)
    register("sc-1", False)
    tpd._ensure_test_plan_blocking("sc-1")
    assert tp.list_plans() == [] and calls == []


# --------------------------------------------------------------------------- #
# Decision 1 — each paused pass follows asked-for checklists
# --------------------------------------------------------------------------- #
def _owner_like_store() -> None:
    """The owner's shapes: route-stop / by-hand plans with no trigger, plus one
    a push made."""
    tp.upsert(_plan("sc-25247", origin=""))
    tp.upsert(_plan("pr-sitecheck-bot-3175", origin="manual", steps=[]))
    tp.upsert(_plan("pushed-1", trigger="push"))


def test_the_liveness_pass_follows_asked_for_checklists_with_the_switch_off(
    repo_settings, monkeypatch
):
    from backend.web import server

    repo_settings(verify_enabled=False, deploy_delay_minutes=0)
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(tpd, "_notify_test_plan_due", lambda plan: None)
    asked: list = []
    monkeypatch.setattr(
        tpd, "_test_plan_is_live", lambda plan: asked.append(plan["id"]) or True
    )
    _owner_like_store()
    tpd._check_test_plans_for_liveness(tp.list_plans())
    assert sorted(asked) == ["pr-sitecheck-bot-3175", "sc-25247"]
    assert tp.get("sc-25247")["state"] == "due"
    assert tp.get("pr-sitecheck-bot-3175")["state"] == "due"
    assert tp.get("pushed-1")["state"] == "generated"
    assert not tp.get("pushed-1")["merged_at"]


def test_the_liveness_pass_keeps_its_budget_with_the_switch_off(
    repo_settings, monkeypatch
):
    """Fewer plans, the same budget and rotation."""
    from backend.web import server

    repo_settings(verify_enabled=False)
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(tpd, "_TEST_PLAN_LIVE_BUDGET_S", 0.0)
    asked: list = []
    monkeypatch.setattr(
        tpd, "_test_plan_is_live", lambda plan: asked.append(plan["id"]) or False
    )
    _owner_like_store()
    tpd._check_test_plans_for_liveness(tp.list_plans())
    assert asked == []


def test_the_landing_pass_follows_asked_for_checklists_with_the_switch_off(
    repo_settings, monkeypatch
):
    from backend.web import server

    repo_settings(verify_enabled=False)
    monkeypatch.setattr(server, "git_available", lambda: True)
    asked: list = []
    monkeypatch.setattr(
        tpd, "_test_plan_merged_into", lambda plan: asked.append(plan["id"]) or {}
    )
    _owner_like_store()
    tpd._check_test_plan_landings(tp.list_plans())
    assert sorted(asked) == ["pr-sitecheck-bot-3175", "sc-25247"]


def test_a_stalled_asked_for_writing_is_recovered_with_the_switch_off(
    repo_settings, monkeypatch
):
    repo_settings(verify_enabled=False)
    started: list = []
    monkeypatch.setattr(
        tpd, "_start_test_plan_generation", lambda pid, *a, **k: started.append(pid)
    )
    old = time.time() - (tp.GENERATE_STALE_S + 1)
    tp.upsert(_plan("asked", state="generating", steps=[], gen_started=old))
    tp.upsert(
        _plan("pushed", state="generating", steps=[], gen_started=old, trigger="push")
    )
    tpd._recover_stalled_test_plans(tp.list_plans())
    assert started == ["asked"]


def test_writing_steps_for_closed_sessions_stays_paused_even_when_asked_for(
    repo_settings, monkeypatch
):
    """Automatic writing the driver starts by itself — what the switch pauses."""
    repo_settings(verify_enabled=False)
    monkeypatch.setattr(
        tpd, "_start_test_plan_generation", lambda *a, **k: pytest.fail("paused")
    )
    tp.upsert(_plan("asked", steps=[], repo_root="/"))
    tpd._generate_for_closed_test_plans(tp.list_plans())
