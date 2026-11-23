"""Verify follows a checklist through: the master switch, squash merges, "it's merged".

Two causes kept every checklist in one owner's store in ``generated`` for ever
with ``merged_at`` 0:

1. **The master switch.** ``repository.verify_enabled`` off made every watcher
   pass skip every plan, while a route's Verify stop and the Verify button went
   on creating and walking checklists. The switch now pauses only what MindFlock
   does by itself (a push to a watched repo writing a plan): a checklist that
   was ASKED for is watched until it ships. Plans record who wanted them
   (``trigger``); a plan written before that field counts as asked.
2. **Squash merges through another PR.** Ancestry never says yes to a squash,
   and the branch's own PR is not the one that merged. The content check asks
   whether the branch's changes are on the live branch (reverse-apply against a
   throwaway index), and a route's land stop or a person can say "it's merged"
   — through the same path ancestry takes.

The content check is tested against REAL git repositories (a bare "origin", a
working clone that pushes and squashes, and a bare base clone like the ones
these plans live in): what a reverse apply accepts is exactly what a mock would
get wrong.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.web.core import test_plans as tp
from backend.web.core import test_plans_driver as tpd


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """An isolated plan store — autouse, so nothing here can touch a real one."""
    path = tmp_path / "test_plans.json"
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(path))
    tpd._TEST_PLAN_LIVE_CHECKED.clear()
    tpd._TEST_PLAN_LANDED_CHECKED.clear()
    tpd._TEST_PLAN_CONTENT_CHECKED.clear()
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
# Real git worlds
# --------------------------------------------------------------------------- #
def _lines(n: int, **swap) -> str:
    rows = [str(i) for i in range(1, n + 1)]
    for k, v in swap.items():
        rows[int(k.lstrip("L")) - 1] = v
    return "\n".join(rows) + "\n"


class World:
    """origin (bare) + a working clone that pushes + a bare base clone."""

    def __init__(self, root: Path):
        self.root = root
        self.remote = root / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
            env=_ENV,
        )
        self.work = root / "work"
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(self.work)],
            check=True,
            env=_ENV,
            stderr=subprocess.DEVNULL,
        )
        _git(self.work, "checkout", "-q", "-b", "main")
        self.write({"a.txt": _lines(30), "b.txt": _lines(30)})
        self.commit("init")
        _git(self.work, "push", "-q", "origin", "main")
        self.base = root / "base.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.remote), str(self.base)],
            check=True,
            env=_ENV,
        )

    def write(self, files: dict) -> None:
        for name, text in files.items():
            path = self.work / name
            if text is None:
                path.unlink()
            else:
                path.write_text(text, encoding="utf-8")

    def commit(self, msg: str, allow_empty: bool = False) -> str:
        _git(self.work, "add", "-A")
        _git(
            self.work,
            "commit",
            "-q",
            "-m",
            msg,
            *(["--allow-empty"] if allow_empty else []),
        )
        return _git(self.work, "rev-parse", "HEAD")

    def branch(self, name: str, files: dict, start: str = "main") -> str:
        """A pushed feature branch with one commit; returns its sha."""
        _git(self.work, "checkout", "-q", "-B", name, start)
        self.write(files)
        sha = self.commit(name)
        _git(self.work, "push", "-q", "-f", "origin", name)
        _git(self.work, "checkout", "-q", "main")
        return sha

    def squash_into_main(self, *branches: str) -> str:
        """Squash-merge ``branches`` (together) into main and push it."""
        _git(self.work, "checkout", "-q", "main")
        for b in branches:
            _git(self.work, "merge", "-q", "--squash", b)
        sha = self.commit("squash " + " ".join(branches))
        _git(self.work, "push", "-q", "origin", "main")
        return sha

    def on_main(self, files: dict) -> str:
        _git(self.work, "checkout", "-q", "main")
        self.write(files)
        sha = self.commit("main moves on")
        _git(self.work, "push", "-q", "origin", "main")
        return sha

    def fetch_base(self) -> None:
        """What the plans' base clone holds: every branch, origin/main fetched
        the way ``probe_live`` fetches it."""
        _git(
            self.base, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/heads/*"
        )
        _git(
            self.base,
            "fetch",
            "-q",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )

    def shipped(self, sha: str) -> bool:
        self.fetch_base()
        return tp.probe_content_shipped(str(self.base), sha, "main")


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# --------------------------------------------------------------------------- #
# Decision 2a — the content check, on real repositories
# --------------------------------------------------------------------------- #
def test_a_plain_squash_merge_is_seen_as_shipped(world):
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten"), "n.txt": "new\n"})
    assert not world.shipped(sha)  # not merged yet
    world.squash_into_main("feat")
    assert world.shipped(sha)
    # ...and ancestry, the history half, still cannot see it.
    assert tp.probe_live(str(world.base), sha, "main", fetch=False) == "waiting"


def test_a_squash_that_bundles_two_branches_ships_both(world):
    """The owner's sitecheck shape: a scratch-branch PR carrying two tickets."""
    one = world.branch("t-1", {"a.txt": _lines(30, L5="five")})
    two = world.branch("t-2", {"b.txt": _lines(30, L20="twenty")})
    _git(world.work, "checkout", "-q", "-b", "scratch", "main")
    _git(world.work, "merge", "-q", "--no-edit", "t-1")
    _git(world.work, "merge", "-q", "--no-edit", "t-2")
    _git(world.work, "checkout", "-q", "main")
    world.squash_into_main("scratch")
    assert world.shipped(one) and world.shipped(two)


def test_a_live_branch_that_moved_on_with_unrelated_changes_still_counts(world):
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    world.squash_into_main("feat")
    world.on_main({"b.txt": _lines(30, L25="later"), "c.txt": "other work\n"})
    # Lines NEXT to the change move too: offset tolerated, content still there.
    world.on_main({"a.txt": "header\n" + _lines(30, L10="ten", L1="one")})
    assert world.shipped(sha)


def test_a_later_commit_on_the_same_lines_is_a_miss_never_a_false_shipped(world):
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    world.squash_into_main("feat")
    world.on_main({"a.txt": _lines(30, L10="TEN")})
    assert not world.shipped(sha)


def test_changes_that_are_not_on_the_live_branch_are_not_shipped(world):
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten"), "n.txt": "new\n"})
    world.on_main({"b.txt": _lines(30, L3="three"), "c.txt": "c\n"})
    assert not world.shipped(sha)


def test_half_the_branch_on_the_live_branch_is_not_shipped(world):
    sha = world.branch(
        "feat", {"a.txt": _lines(30, L10="ten"), "b.txt": _lines(30, L20="x")}
    )
    world.on_main({"a.txt": _lines(30, L10="ten")})  # only one of its two changes
    assert not world.shipped(sha)


def test_an_empty_branch_has_shipped_nothing(world):
    _git(world.work, "checkout", "-q", "-b", "empty", "main")
    sha = world.commit("nothing", allow_empty=True)
    _git(world.work, "push", "-q", "origin", "empty")
    _git(world.work, "checkout", "-q", "main")
    assert not world.shipped(sha)


def test_a_deleted_remote_branch_still_answers_from_the_sha(world):
    """The land stop deletes the remote branch; the plan keeps its sha, and
    the base clone keeps the objects."""
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    world.fetch_base()
    world.squash_into_main("feat")
    _git(world.work, "push", "-q", "origin", "--delete", "feat")
    assert world.shipped(sha)


def test_a_sha_the_clone_does_not_have_is_not_shipped(world):
    world.on_main({"c.txt": "c\n"})
    assert not world.shipped("1" * 40)


def test_a_missing_live_branch_is_not_shipped(world):
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    world.fetch_base()
    assert not tp.probe_content_shipped(str(world.base), sha, "release")


def test_a_deletion_whose_context_repeats_is_not_a_false_shipped(world):
    """THE AMBIGUITY THE FORWARD TEST EXISTS FOR. A hunk that only deletes a
    line reverse-applies wherever its context lines sit together — and in a
    repetitive file they sit together in the UNMERGED file too."""
    text = "a\nb\nc\na\nb\nc\nX\na\nb\nc\n"
    world.on_main({"r.txt": text})
    sha = world.branch("feat", {"r.txt": text.replace("X\n", "")})
    world.fetch_base()
    # The trap is real: the reverse patch alone fits the unmerged file.
    base = _git(world.base, "merge-base", sha, "refs/remotes/origin/main")
    patch = subprocess.run(
        ["git", "-C", str(world.base), "diff", base, sha],
        check=True,
        stdout=subprocess.PIPE,
        env=_ENV,
    ).stdout
    idx = world.root / "probe-index"
    env = dict(_ENV, GIT_INDEX_FILE=str(idx))
    subprocess.run(
        ["git", "-C", str(world.base), "read-tree", "refs/remotes/origin/main"],
        check=True,
        env=env,
    )
    reverse = subprocess.run(
        ["git", "-C", str(world.base), "apply", "--cached", "--check", "-R"],
        input=patch,
        env=env,
        stderr=subprocess.DEVNULL,
    )
    assert reverse.returncode == 0
    # ...and the check still says no.
    assert not tp.probe_content_shipped(str(world.base), sha, "main")
    world.squash_into_main("feat")
    assert world.shipped(sha)


def test_the_check_touches_nothing_in_a_working_clone(world):
    """Run against a NON-bare checkout: its index and worktree are untouched."""
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    world.squash_into_main("feat")
    _git(
        world.work, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"
    )
    _git(world.work, "checkout", "-q", "main")
    (world.work / "dirty.txt").write_text("mine\n", encoding="utf-8")
    index = world.work / ".git" / "index"
    before = (index.read_bytes(), _git(world.work, "status", "--porcelain"))
    assert tp.probe_content_shipped(str(world.work), sha, "main")
    assert (index.read_bytes(), _git(world.work, "status", "--porcelain")) == before


def test_a_diff_over_the_cap_is_not_checked(world, monkeypatch):
    sha = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    world.squash_into_main("feat")
    monkeypatch.setattr(tp, "MAX_CONTENT_DIFF_BYTES", 10)
    assert not world.shipped(sha)


# --------------------------------------------------------------------------- #
# Decision 2a in the due loop — its own interval, after history says no
# --------------------------------------------------------------------------- #
def test_the_content_check_answers_when_history_says_no(monkeypatch):
    calls = []
    monkeypatch.setattr(tpd, "_test_plan_live_by_history", lambda plan: False)
    monkeypatch.setattr(
        tp, "probe_content_shipped", lambda *a, **k: calls.append(a) or True
    )
    tp.upsert(_plan("sc-1", live_problem="Its pull request merged into staging"))
    assert tpd._test_plan_is_live(tp.get("sc-1")) is True
    assert calls and tp.get("sc-1")["live_problem"] == ""


def test_the_content_check_runs_on_its_own_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(tpd, "_test_plan_live_by_history", lambda plan: False)
    monkeypatch.setattr(
        tp, "probe_content_shipped", lambda *a, **k: calls.append(a) or False
    )
    plan = _plan("sc-1")
    tp.upsert(plan)
    assert tpd._test_plan_is_live(plan) is False
    assert tpd._test_plan_is_live(plan) is False  # the next pass, a minute on
    assert len(calls) == 1
    later = time.time() + tpd._TEST_PLAN_CONTENT_INTERVAL_S + 1
    assert tpd._test_plan_content_shipped(plan, now=later) is False
    assert len(calls) == 2


def test_the_content_check_asks_about_the_newest_push_not_the_first(world, monkeypatch):
    """The first push's changes shipped, a later push's did not: not shipped."""
    monkeypatch.setattr(tpd, "_test_plan_live_by_history", lambda plan: False)
    first = world.branch("feat", {"a.txt": _lines(30, L10="ten")})
    _git(world.work, "checkout", "-q", "-b", "part", "feat")
    _git(world.work, "push", "-q", "origin", "part")
    _git(world.work, "checkout", "-q", "main")
    world.squash_into_main("part")
    tip = world.branch("feat", {"b.txt": _lines(30, L20="later")}, start="feat")
    world.fetch_base()
    plan = _plan("sc-1", repo_root=str(world.base), sha=first, tip_sha=tip)
    tp.upsert(plan)
    assert tp.probe_content_shipped(str(world.base), first, "main")  # the trap
    assert tpd._test_plan_is_live(tp.get("sc-1")) is False


def test_history_saying_yes_never_spends_a_content_check(monkeypatch):
    monkeypatch.setattr(tpd, "_test_plan_live_by_history", lambda plan: True)
    monkeypatch.setattr(
        tp, "probe_content_shipped", lambda *a, **k: pytest.fail("not needed")
    )
    assert tpd._test_plan_is_live(_plan("sc-1")) is True


def test_a_squashed_route_checklist_comes_due_with_the_switch_off(
    world, repo_settings, monkeypatch
):
    """End to end, the owner's case: Verify switched off, a checklist a route
    stop made (origin "", no trigger — written before the field), its branch
    squashed into the live branch through another PR. One pass of the real
    liveness phase brings it due — fetch, ancestry no, PR none, content yes."""
    from backend.web import server

    repo_settings(verify_enabled=False, deploy_delay_minutes=0)
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_pr_info", lambda root, branch: None)
    monkeypatch.setattr(tpd, "_notify_test_plan_due", lambda plan: None)
    monkeypatch.setattr(tp, "verify_block", lambda root: {})
    sha = world.branch("sc-25247", {"a.txt": _lines(30, L10="ten")})
    world.fetch_base()
    _git(world.work, "checkout", "-q", "-b", "scratch", "sc-25247")
    _git(world.work, "push", "-q", "origin", "scratch")
    world.squash_into_main("scratch")
    tp.upsert(_plan("sc-25247", repo_root=str(world.base), branch="sc-25247", sha=sha))
    tpd._check_test_plans_for_liveness(tp.list_plans())
    plan = tp.get("sc-25247")
    assert plan["state"] == "due" and plan["merged_at"] > 0 and plan["live_at"] > 0


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


# --------------------------------------------------------------------------- #
# Decision 2b — "it's merged", from a person or the route's land stop
# --------------------------------------------------------------------------- #
@pytest.fixture
def emitted(monkeypatch):
    seen: list = []
    notified: list = []
    monkeypatch.setattr(
        tpd._events.BUS, "emit", lambda event, **kw: seen.append((event, kw))
    )
    monkeypatch.setattr(
        tpd, "_notify_test_plan_due", lambda plan: notified.append(plan["id"])
    )
    monkeypatch.setattr(tp, "verify_block", lambda root: {})
    return seen, notified


def test_marking_a_plan_shipped_goes_the_way_ancestry_does(repo_settings, emitted):
    seen, notified = emitted
    repo_settings(verify_enabled=False, deploy_delay_minutes=0)
    tp.upsert(_plan("sc-1", trigger="push", live_problem="waiting"))
    plan = tpd._ship_test_plan("sc-1")
    assert plan["state"] == "due" and plan["merged_at"] > 0 and plan["live_at"] > 0
    assert plan["trigger"] == "asked" and plan["live_problem"] == ""
    assert [e for e, _ in seen] == ["session.test_plan_due"]
    assert notified == ["sc-1"]


def test_a_deploy_wait_still_runs_after_a_person_says_merged(
    repo_settings, emitted, monkeypatch
):
    """The same path, so the same wait: merged now, due once it deploys — and
    the paused loop finishes the wait, because it is now asked for."""
    from backend.web import server

    seen, notified = emitted
    repo_settings(verify_enabled=False, deploy_delay_minutes=10)
    tp.upsert(_plan("sc-1", trigger="push"))
    plan = tpd._ship_test_plan("sc-1")
    assert plan["state"] == "generated" and plan["merged_at"] > 0
    assert seen == [] and notified == []
    plan["merged_at"] = time.time() - 601
    tp.upsert(plan)
    monkeypatch.setattr(server, "git_available", lambda: True)
    tpd._check_test_plans_for_liveness(tp.list_plans())
    assert tp.get("sc-1")["state"] == "due" and notified == ["sc-1"]


@pytest.mark.parametrize(
    "over",
    [
        {"merged_at": 5.0},
        {"state": "due", "live_at": 6.0, "merged_at": 5.0},
        {"state": "done", "merged_at": 5.0},
        {"state": "running", "run_session": "verify-sc-1"},
        {"state": "generating"},
    ],
)
def test_only_a_plan_waiting_for_its_merge_is_moved(emitted, over):
    seen, _ = emitted
    tp.upsert(_plan("sc-1", **over))
    before = tp.get("sc-1")
    assert tpd._ship_test_plan("sc-1") == before
    assert seen == []


@pytest.fixture
def client(monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server, "git_available", lambda: True)
    return TestClient(server.app)


def test_the_shipped_route(client, repo_settings, emitted):
    repo_settings(deploy_delay_minutes=0)
    tp.upsert(_plan("sc-1"))
    r = client.post("/api/test-plans/sc-1/shipped")
    assert r.status_code == 200
    assert r.json()["plan"]["state"] == "due"
    assert client.post("/api/test-plans/sc-1/shipped").status_code == 409
    assert client.post("/api/test-plans/nope/shipped").status_code == 404


def test_the_shipped_route_takes_a_plan_id_with_a_slash(client, repo_settings, emitted):
    repo_settings(deploy_delay_minutes=0)
    tp.upsert(_plan("repo@feat/x"))
    r = client.post("/api/test-plans/" + "repo%40feat%2Fx" + "/shipped")
    assert r.status_code == 200 and tp.get("repo@feat/x")["state"] == "due"


def test_the_land_stop_marks_the_branchs_checklist_shipped(repo_settings, emitted):
    from backend.web.core.flight_kinds import land

    repo_settings(deploy_delay_minutes=0)
    tp.upsert(_plan("sc-1", repo_root="/r", branch="feat", live_branch="main"))
    out = land._ship_checklist(
        {"title": "sc-1", "repo": "/r", "branch": "feat", "main": "main"}
    )
    assert out == "sc-1" and tp.get("sc-1")["state"] == "due"


def test_the_land_stop_finds_a_checklist_another_window_owns(repo_settings, emitted):
    from backend.web.core.flight_kinds import land

    repo_settings(deploy_delay_minutes=0)
    tp.upsert(_plan("first", repo_root="/r", branch="feat", live_branch="main"))
    out = land._ship_checklist(
        {"title": "second", "repo": "/r", "branch": "feat", "main": "main"}
    )
    assert out == "first" and tp.get("first")["state"] == "due"


def test_the_land_stop_never_ships_a_checklist_waiting_for_another_branch(emitted):
    """Landed on main, checklist waits for release: telling it "shipped" would
    be a false shipped."""
    from backend.web.core.flight_kinds import land

    tp.upsert(_plan("sc-1", repo_root="/r", branch="feat", live_branch="release"))
    assert (
        land._ship_checklist(
            {"title": "sc-1", "repo": "/r", "branch": "feat", "main": "main"}
        )
        == ""
    )
    assert tp.get("sc-1")["state"] == "generated" and not tp.get("sc-1")["merged_at"]


def test_the_land_stop_with_no_checklist_does_nothing(emitted):
    from backend.web.core.flight_kinds import land

    assert (
        land._ship_checklist(
            {"title": "x", "repo": "/r", "branch": "b", "main": "main"}
        )
        == ""
    )
