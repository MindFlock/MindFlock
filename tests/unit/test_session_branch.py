"""Branch session: ``POST /api/instances/{title}/branch`` and its engine pieces.

A branch is a NEW session in its own worktree, on a new branch cut from the
source's current HEAD, carrying a copy of the source's uncommitted changes
(``backend.session.git.carry``) — the source untouched. Everything runs
against throwaway git repos under ``tmp_path`` with ``$HOME`` redirected (the
worktrees live in tmp). No agent and no tmux ever start: the route's
background Start is closed unrun, and the tests drive the engine's worktree
steps themselves. ``ENGINE.instances`` is replaced wholesale (the live engine
reads the real state.json).
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session import git as sgit
from backend.session.instance import InstanceOptions, new_instance
from backend.session.storage import Status
from backend.web import server
from backend.web.core import remote as remote_mod
from backend.web.core import session_branch
from backend.web.core import snapshot as snapshot_mod

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    res = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert res.returncode == 0, res.stdout.decode("utf-8", "replace")
    return res.stdout.decode("utf-8", "replace").strip()


def _status(path):
    """Porcelain status, untracked files listed one by one (lines unstripped:
    the leading column is meaningful)."""
    res = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=path,
        stdout=subprocess.PIPE,
        check=True,
    )
    return res.stdout.decode("utf-8", "replace").rstrip("\n")


def _write(path, name, text):
    full = os.path.join(path, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(text)


def _read(path, name):
    with open(os.path.join(path, name)) as fh:
        return fh.read()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on ``main`` (with a .gitignore) and a linked worktree on branch
    ``me/src`` one commit ahead of main — the source session's worktree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = str(tmp_path / "repo")
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    _write(path, "a.txt", "a\n")
    _write(path, "gone.txt", "doomed\n")
    _write(path, ".gitignore", "*.log\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    main = _git(path, "rev-parse", "HEAD")
    wt = str(tmp_path / "src-wt")
    _git(path, "worktree", "add", "-q", "-b", "me/src", wt, "main")
    _write(wt, "feature.txt", "committed on the branch\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "feature")
    tip = _git(wt, "rev-parse", "HEAD")
    return SimpleNamespace(path=path, wt=wt, main=main, tip=tip)


def _dirty(wt):
    """Every kind of uncommitted change: modified, deleted, staged-new,
    untracked (nested), and an ignored file that must stay behind."""
    _write(wt, "a.txt", "a\nedited\n")
    os.remove(os.path.join(wt, "gone.txt"))
    _write(wt, "staged.txt", "staged\n")
    _git(wt, "add", "staged.txt")
    _write(wt, "notes/new.md", "untracked\n")
    _write(wt, "debug.log", "ignored\n")


class _Src:
    """A registry stand-in for the source session."""

    def __init__(self, title, wt, path="", in_place=False, **kw):
        self.Title = title
        self.Path = path or wt
        self.Program = kw.get("program", "claude")
        self.Branch = kw.get("branch", "me/src")
        self.Status = kw.get("status", Status.Running)
        self.InPlace = in_place
        self.Provisioned = kw.get("provisioned", False)
        self.WorkspaceStrategy = kw.get("strategy", "worktree")
        self.LaunchArgs = tuple(kw.get("launch_args", ("--verbose",)))
        self.ProfileId = kw.get("profile_id", "")
        self.ProfileModel = kw.get("profile_model", "")
        self.PeerShare = kw.get("peer_share", "")
        self.Parent = ""
        self.Spawned = False
        self._wt = wt
        self._started = kw.get("started", True)

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def reg(monkeypatch):
    """A private, empty registry; every create side effect that would reach
    beyond tmp is stubbed, and the background Start is closed unrun."""
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    events: list = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda name, **kw: events.append((name, kw))
    )
    monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
    monkeypatch.setattr(server, "_mark_onboarded", lambda: None)
    monkeypatch.setattr(server._ports, "env_for", lambda t: {})
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
    monkeypatch.setattr(server, "_profile_id_error", lambda p: None)

    def add(title, wt, **kw):
        inst = _Src(title, wt, **kw)
        instances[title] = inst
        return inst

    return SimpleNamespace(instances=instances, events=events, add=add)


def _build(inst):
    """The engine half of the background Start, minus tmux: cut the worktree,
    then carry the changes over."""
    inst._create_first_time_worktree()
    inst._git_worktree.Setup()
    inst._apply_carry()
    return inst._git_worktree.GetWorktreePath()


# --------------------------------------------------------------------------- #
# engine: backend.session.git.carry                                            #
# --------------------------------------------------------------------------- #
def test_snapshot_of_a_clean_worktree_is_empty(repo):
    assert sgit.snapshot_changes(repo.wt) == ""
    # Only an ignored file: still nothing to carry.
    _write(repo.wt, "x.log", "noise\n")
    assert sgit.snapshot_changes(repo.wt) == ""


def test_snapshot_holds_every_change_and_leaves_the_source_alone(repo):
    _dirty(repo.wt)
    before = _status(repo.wt)
    staged_before = _git(repo.wt, "diff", "--cached", "--name-status")
    snap = sgit.snapshot_changes(repo.wt)
    assert snap
    # On top of HEAD, never on a branch.
    assert _git(repo.wt, "rev-parse", snap + "^") == repo.tip
    assert snap not in _git(repo.path, "for-each-ref", "--format=%(objectname)")
    files = set(_git(repo.wt, "ls-tree", "-r", "--name-only", snap).splitlines())
    assert {"a.txt", "staged.txt", "notes/new.md", "feature.txt"} <= files
    assert "gone.txt" not in files and "debug.log" not in files
    assert _git(repo.wt, "show", snap + ":a.txt") == "a\nedited"
    # The source: same files, same index, same branch.
    assert _status(repo.wt) == before
    assert _git(repo.wt, "diff", "--cached", "--name-status") == staged_before
    assert _git(repo.wt, "rev-parse", "HEAD") == repo.tip


def test_apply_snapshot_reproduces_the_tree_unstaged(repo, tmp_path):
    _dirty(repo.wt)
    snap = sgit.snapshot_changes(repo.wt)
    other = str(tmp_path / "other")
    _git(repo.path, "worktree", "add", "-q", "-b", "other", other, repo.tip)
    sgit.apply_snapshot(other, snap)
    assert _read(other, "a.txt") == "a\nedited\n"
    assert not os.path.exists(os.path.join(other, "gone.txt"))
    assert _read(other, "notes/new.md") == "untracked\n"
    assert _read(other, "staged.txt") == "staged\n"
    assert not os.path.exists(os.path.join(other, "debug.log"))
    # Nothing staged, HEAD unmoved.
    assert _git(other, "diff", "--cached", "--name-only") == ""
    assert _git(other, "rev-parse", "HEAD") == repo.tip
    assert set(_status(other).splitlines()) == {
        " M a.txt",
        " D gone.txt",
        "?? notes/new.md",
        "?? staged.txt",
    }


def test_apply_snapshot_refuses_an_option_shaped_ref(repo):
    with pytest.raises(RuntimeError):
        sgit.apply_snapshot(repo.wt, "--force")
    with pytest.raises(RuntimeError):
        sgit.apply_snapshot(repo.wt, "")


def test_snapshot_outside_a_repo_raises(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError):
        sgit.snapshot_changes(str(plain))


def test_instance_carries_the_snapshot_into_its_new_worktree(repo):
    _dirty(repo.wt)
    snap = sgit.snapshot_changes(repo.wt)
    inst = new_instance(
        InstanceOptions(
            title="b1",
            path=repo.path,
            program="bash",
            base_ref=repo.tip,
            base_branch="me/src",
            carry_ref=snap,
        )
    )
    assert inst._carry_ref == snap
    wt = _build(inst)
    assert _git(wt, "rev-parse", "HEAD") == repo.tip
    assert inst.BaseBranch == "me/src"
    assert _read(wt, "a.txt") == "a\nedited\n"
    assert inst._carry_ref == ""  # one-shot
    # The diff base is the fork commit, so the carried edits read as changes.
    assert inst._git_worktree.GetBaseCommitSHA() == repo.tip


def test_carry_ref_needs_a_fork_point(repo):
    inst = new_instance(
        InstanceOptions(title="b2", path=repo.path, program="bash", carry_ref="abc")
    )
    assert inst._carry_ref == ""


def test_a_carry_that_fails_fails_the_step(repo):
    inst = new_instance(
        InstanceOptions(
            title="b3",
            path=repo.path,
            program="bash",
            base_ref=repo.tip,
            carry_ref="0" * 40,
        )
    )
    inst._create_first_time_worktree()
    inst._git_worktree.Setup()
    with pytest.raises(RuntimeError, match="failed to carry uncommitted changes"):
        inst._apply_carry()


# --------------------------------------------------------------------------- #
# the route                                                                    #
# --------------------------------------------------------------------------- #
def test_branch_forks_from_the_tip_and_carries_the_changes(reg, repo):
    _dirty(repo.wt)
    before = _status(repo.wt)
    reg.add("src", repo.wt, path=repo.path, profile_id="work", profile_model="m1")
    r = client.post("/api/instances/src/branch")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["title"] == "src-branch"
    assert body["branched_from"] == "src"
    assert body["base_branch"] == "me/src"
    assert body["carried"] is True
    new = reg.instances["src-branch"]
    # Mirrors Duplicate: same CLI, profile and model; no parent; no prompt.
    assert new.Program == "claude"
    assert new.ProfileId == "work" and new.ProfileModel == "m1"
    assert new.LaunchArgs == ("--verbose",)
    assert new.Parent == "" and new.Spawned is False and new.Prompt == ""
    # Its own worktree of the canonical repo, never the source's folder.
    assert new.InPlace is False
    assert new.Path == os.path.realpath(repo.path)
    created = [kw for name, kw in reg.events if name == "session.created"]
    assert created[-1]["data"]["branched_from"] == "src"

    wt = _build(new)
    assert wt != repo.wt
    assert _git(wt, "rev-parse", "HEAD") == repo.tip  # the branch tip, not main
    assert _git(wt, "symbolic-ref", "--short", "HEAD") == server._session_branch_name(
        "src-branch"
    )
    assert new.BaseBranch == "me/src"
    assert _read(wt, "a.txt") == "a\nedited\n"
    assert _read(wt, "notes/new.md") == "untracked\n"
    assert not os.path.exists(os.path.join(wt, "gone.txt"))
    assert not os.path.exists(os.path.join(wt, "debug.log"))
    # The source is exactly as it was.
    assert _status(repo.wt) == before
    assert _git(repo.wt, "rev-parse", "HEAD") == repo.tip


def test_branch_of_a_clean_session_carries_nothing(reg, repo):
    reg.add("src", repo.wt, path=repo.path)
    r = client.post("/api/instances/src/branch")
    assert r.status_code == 202, r.text
    assert r.json()["carried"] is False
    new = reg.instances["src-branch"]
    assert new._carry_ref == ""
    wt = _build(new)
    assert _status(wt) == ""
    assert _git(wt, "rev-parse", "HEAD") == repo.tip


def test_branch_titles_skip_taken_names_and_branches(reg, repo):
    reg.add("src", repo.wt, path=repo.path)
    reg.add("src-branch", repo.wt, path=repo.path)  # a live namesake
    # A closed session's branch still holds the -2 name.
    _git(repo.path, "branch", server._session_branch_name("src-branch-2"), "main")
    r = client.post("/api/instances/src/branch")
    assert r.status_code == 202, r.text
    assert r.json()["title"] == "src-branch-3"
    # The closed namesake's branch is untouched.
    assert (
        _git(repo.path, "rev-parse", server._session_branch_name("src-branch-2"))
        == repo.main
    )


def test_branch_of_an_in_place_session_in_a_repo(reg, repo):
    reg.add("home", repo.path, in_place=True, branch="main")
    _write(repo.path, "a.txt", "a\nlocal\n")
    r = client.post("/api/instances/home/branch")
    assert r.status_code == 202, r.text
    assert r.json()["base_branch"] == "main"
    wt = _build(reg.instances["home-branch"])
    assert _git(wt, "rev-parse", "HEAD") == repo.main
    assert _read(wt, "a.txt") == "a\nlocal\n"
    assert _read(repo.path, "a.txt") == "a\nlocal\n"


def test_branch_refuses_a_folder_outside_git(reg, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    reg.add("loose", str(plain), in_place=True, branch="")
    r = client.post("/api/instances/loose/branch")
    assert r.status_code == 409
    assert r.json()["error"] == (
        "Branch needs a git repository — %s isn't one" % str(plain)
    )
    assert list(reg.instances) == ["loose"]


def test_branch_refuses_a_repo_with_no_commit(reg, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(str(fresh), "init", "-q")
    reg.add("fresh", str(fresh), in_place=True, branch="")
    r = client.post("/api/instances/fresh/branch")
    assert r.status_code == 409
    assert "has none yet" in r.json()["error"]


def test_branch_refuses_remote_rows_and_relayed_requests(reg, repo):
    r = client.post("/api/instances/rig::src/branch")
    # The proxy middleware forwards `dev::title` to the device when it is
    # connected; unconnected (as here) it answers itself — either way nothing
    # is created on this device.
    assert r.status_code in (409, 502)
    assert reg.instances == {}
    reg.add("src", repo.wt, path=repo.path)
    r = client.post(
        "/api/instances/src/branch", headers={remote_mod.REMOTE_HEADER: "rig"}
    )
    assert r.status_code in (403, 409)
    assert list(reg.instances) == ["src"]


@pytest.mark.asyncio
async def test_branch_core_refuses_a_remote_title(reg):
    resp = await session_branch.branch("rig::src")
    assert resp.status_code == 409
    assert b"open MindFlock on rig" in resp.body
    resp = await session_branch.branch("src", relayed=True)
    assert resp.status_code == 409


def test_branch_refuses_a_shared_folder_session(reg, repo):
    reg.add("peer", repo.wt, path=repo.path, peer_share="abc123")
    r = client.post("/api/instances/peer/branch")
    assert r.status_code == 409
    assert r.json()["peer_share"] is True
    assert "Branch is not available" in r.json()["error"]


def test_branch_refuses_a_session_still_starting(reg, repo):
    reg.add("new", repo.wt, path=repo.path, status=Status.Loading)
    r = client.post("/api/instances/new/branch")
    assert r.status_code == 409
    assert "still starting" in r.json()["error"]
    reg.add("new2", repo.wt, path=repo.path, started=False)
    assert client.post("/api/instances/new2/branch").status_code == 409


def test_branch_refuses_a_clone_workspace(reg, repo, tmp_path):
    clone = str(tmp_path / "clone")
    _git(repo.path, "clone", "-q", repo.path, clone)
    reg.add("cl", clone, path=clone, provisioned=True, strategy="clone")
    r = client.post("/api/instances/cl/branch")
    assert r.status_code == 409
    assert "separate clone" in r.json()["error"]


def test_branch_404_for_an_unknown_session(reg):
    assert client.post("/api/instances/nope/branch").status_code == 404


def test_branch_refuses_a_missing_workspace(reg, tmp_path):
    reg.add("ghost", str(tmp_path / "gone"))
    r = client.post("/api/instances/ghost/branch")
    assert r.status_code == 409
    assert r.json()["error"] == "source workspace not ready"


# --------------------------------------------------------------------------- #
# the row's git_repo                                                           #
# --------------------------------------------------------------------------- #
def test_row_git_repo(repo, tmp_path):
    plain = tmp_path / "plain" / "deep"
    plain.mkdir(parents=True)
    worktree = SimpleNamespace(InPlace=False)
    in_place = SimpleNamespace(InPlace=True)
    assert snapshot_mod._in_git_repo(worktree, "") is True
    assert snapshot_mod._in_git_repo(in_place, repo.path) is True
    sub = os.path.join(repo.path, "sub")
    os.makedirs(sub)
    assert snapshot_mod._in_git_repo(in_place, sub) is True
    assert snapshot_mod._in_git_repo(in_place, repo.wt) is True  # .git file
    assert snapshot_mod._in_git_repo(in_place, str(plain)) is False
    assert snapshot_mod._in_git_repo(in_place, "") is False
