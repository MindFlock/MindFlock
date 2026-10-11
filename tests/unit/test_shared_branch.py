"""Two people on ONE pushed branch (core.shared_branch).

* ``continue_branch`` on create: a worktree ON ``x`` tracking ``origin/x``
  (local branch made from the remote one, an existing local reused and
  fast-forwarded, 409 when ``x`` is checked out elsewhere, 400 for a ref that
  is not remote-tracking), and the ``shared_branch`` row flag.
* The push route's divergence check: 409 ``diverged`` with the count and the
  authors, never reaching the shell push.
* ``rebase-remote``: success, and a conflict that is aborted with the tree
  exactly as it was.
* The MCP's ship_session reports ``diverged`` as a step, never a crash.

Real throwaway repos under ``tmp_path`` (a bare ``origin``, the canonical
clone, and a teammate's clone that pushes), ``$HOME`` redirected.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend import client as mf_client
from backend.session.git.worktree import new_git_worktree_from_branch
from backend.session.storage import GitWorktreeData, InstanceData, Status
from backend.web import server
from backend.web.core import session_create
from backend.web.core import shared_branch as sb
from tests.unit.test_webui import _stub_create


def _git(cwd, *args, env=None):
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    assert res.returncode == 0, res.stdout.decode("utf-8", "replace")
    return res.stdout.decode("utf-8", "replace").strip()


def _identity(path, name="T"):
    _git(path, "config", "user.email", name.lower() + "@example.com")
    _git(path, "config", "user.name", name)
    _git(path, "config", "commit.gpgsign", "false")


def _commit(path, name, text, msg=None):
    with open(os.path.join(path, name), "w") as fh:
        fh.write(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", msg or ("edit " + name))
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sb, "_SHARED", None)
    monkeypatch.setattr(sb, "_BEHIND", {})
    monkeypatch.setattr(sb, "_BG_AT", {})
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "-q", "--bare", "-b", "main", origin)
    repo = str(tmp_path / "repo")
    _git(str(tmp_path), "clone", "-q", origin, repo)
    _identity(repo, "Me")
    _commit(repo, "a.txt", "one\n")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    mate = str(tmp_path / "mate")
    _git(str(tmp_path), "clone", "-q", origin, mate)
    _identity(mate, "Alice")
    # Alice's feature branch, pushed; the canonical repo has not fetched it.
    _git(mate, "switch", "-q", "-c", "feat")
    _commit(mate, "f.txt", "feature\n")
    _git(mate, "push", "-q", "-u", "origin", "feat")
    return {"origin": origin, "repo": repo, "mate": mate, "tmp": tmp_path}


# --------------------------------------------------------------------------- #
# prepare_continue / continue_branch on create
# --------------------------------------------------------------------------- #
def test_prepare_creates_local_branch_tracking_the_remote(world):
    repo = world["repo"]
    status, remote, err = sb.prepare_continue(repo, "origin/feat", "feat")
    assert (status, remote, err) == (0, "origin", "")
    assert _git(repo, "rev-parse", "feat") == _git(world["mate"], "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "--abbrev-ref", "feat@{u}") == "origin/feat"
    # …and the engine's existing-branch path puts a worktree ON it.
    wt = new_git_worktree_from_branch(repo, "feat", "s1")
    wt.Setup()
    assert _git(wt.GetWorktreePath(), "branch", "--show-current") == "feat"
    assert wt.IsExistingBranch() is True  # never deleted on cleanup


def test_prepare_reuses_and_fast_forwards_an_existing_local(world):
    repo, mate = world["repo"], world["mate"]
    _git(repo, "fetch", "-q", "origin")
    old = _git(repo, "rev-parse", "origin/feat")
    _git(repo, "branch", "--no-track", "feat", old)  # no upstream yet
    new = _commit(mate, "f.txt", "feature v2\n")
    _git(mate, "push", "-q", "origin", "feat")
    status, _remote, err = sb.prepare_continue(repo, "origin/feat", "feat")
    assert status == 0, err
    assert _git(repo, "rev-parse", "feat") == new  # fast-forwarded
    assert _git(repo, "rev-parse", "--abbrev-ref", "feat@{u}") == "origin/feat"


def test_prepare_refuses_a_branch_checked_out_elsewhere(world):
    repo = world["repo"]
    assert sb.prepare_continue(repo, "origin/feat", "feat")[0] == 0
    other = str(world["tmp"] / "elsewhere")
    _git(repo, "worktree", "add", "-q", other, "feat")
    status, _r, err = sb.prepare_continue(repo, "origin/feat", "feat")
    assert status == 409
    assert "feat is already checked out in" in err
    assert os.path.realpath(other) in err or other in err


@pytest.mark.parametrize(
    "base_ref,branch,needle",
    [
        ("main", "main", "remote-tracking branch"),
        ("origin/feat", "other", "does not match"),
        ("origin/nope", "nope", "does not exist on origin"),
        ("origin/feat", "-x", "must name a branch"),
    ],
)
def test_prepare_refuses_bad_refs(world, base_ref, branch, needle):
    status, _r, err = sb.prepare_continue(world["repo"], base_ref, branch)
    assert status == 400 and needle in err


@pytest.fixture
def reg(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    return instances


def test_create_with_continue_branch(world, reg, monkeypatch):
    captured: list = []
    _stub_create(monkeypatch, captured, git_enabled=True)
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: (world["repo"], True)
    )
    status, body = asyncio.run(
        session_create.create_result(
            {
                "title": "pair",
                "program": "bash",
                "repo_path": world["repo"],
                "base_ref": "origin/feat",
                "continue_branch": "feat",
            }
        )
    )
    assert status == 202, body
    (opts,) = captured
    # ON the branch (the engine's existing-branch path), no fork point.
    assert opts.branch == "feat" and opts.base_ref == "" and opts.base_branch == ""
    assert sb.shared("pair") == {"remote": "origin", "branch": "feat"}
    assert sb.row_fields("pair", "", "feat")["shared_branch"] is True
    # A reused title without continue_branch starts unshared.
    reg.pop("pair")
    captured.clear()
    status, _ = asyncio.run(
        session_create.create_result(
            {"title": "pair", "program": "bash", "repo_path": world["repo"]}
        )
    )
    assert status == 202 and sb.shared("pair") is None


@pytest.mark.parametrize(
    "extra,code,needle",
    [
        ({"base_ref": "main"}, 400, "remote-tracking"),
        ({"base_ref": "origin/feat", "in_place": True}, 400, "not in-place"),
        ({"base_ref": "origin/feat", "provisioned": True}, 400, "not provisioned"),
    ],
)
def test_create_refusals(world, reg, monkeypatch, extra, code, needle):
    _stub_create(monkeypatch, git_enabled=True)
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: (world["repo"], True)
    )
    payload = {
        "title": "pair",
        "program": "bash",
        "repo_path": world["repo"],
        "continue_branch": "feat" if extra.get("base_ref") != "main" else "main",
        **extra,
    }
    status, body = asyncio.run(session_create.create_result(payload))
    assert status == code and needle in body["error"], body
    assert "pair" not in reg


def test_create_409_when_checked_out(world, reg, monkeypatch):
    _stub_create(monkeypatch, git_enabled=True)
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: (world["repo"], True)
    )
    sb.prepare_continue(world["repo"], "origin/feat", "feat")
    _git(world["repo"], "worktree", "add", "-q", str(world["tmp"] / "wt2"), "feat")
    status, body = asyncio.run(
        session_create.create_result(
            {
                "title": "pair",
                "program": "bash",
                "repo_path": world["repo"],
                "base_ref": "origin/feat",
                "continue_branch": "feat",
            }
        )
    )
    assert status == 409 and "already checked out in" in body["error"]


# --------------------------------------------------------------------------- #
# Push divergence + rebase-remote (routes)
# --------------------------------------------------------------------------- #
def _mk_inst(title, wt, branch):
    from backend.session.instance import FromInstanceData

    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)
    data = InstanceData(
        title=title,
        path=wt,
        branch=branch,
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=wt,
            worktree_path=wt,
            session_name=title,
            branch_name=branch,
            is_existing_branch=True,
        ),
    )
    return FromInstanceData(data, attach=False)


@pytest.fixture
def on_feat(world, monkeypatch):
    """The canonical repo checked out ON feat (as a continue_branch session's
    worktree would be), registered as session ``s1``; shell push neutered."""
    repo = world["repo"]
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "switch", "-q", "-c", "feat", "--track", "origin/feat")
    inst = _mk_inst("s1", repo, "feat")
    monkeypatch.setattr(server.ENGINE, "instances", {"s1": inst})
    reached: list = []
    monkeypatch.setattr(server, "_ensure_shell_session", lambda t, w: ("sh", None))
    monkeypatch.setattr(server, "_send_to_shell", lambda n, c: reached.append(c))
    monkeypatch.setattr(server, "_republish_session", lambda t: {"title": t})
    world["reached"] = reached
    return world


def test_push_refused_when_the_remote_moved_on(on_feat):
    repo, mate = on_feat["repo"], on_feat["mate"]
    _commit(repo, "mine.txt", "mine\n")
    _commit(mate, "f.txt", "v2\n")
    _commit(mate, "g.txt", "v3\n")
    _git(mate, "push", "-q", "origin", "feat")
    c = TestClient(server.app)
    r = c.post("/api/instances/s1/push-branch", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["diverged"] is True
    assert body["behind"] == 2 and body["authors"] == ["Alice"]
    assert body["error"] == "feat moved on origin: 2 new commits by Alice"
    assert on_feat["reached"] == []  # never typed a push, let alone a force
    # The row's behind count comes from that fetch, local refs only.
    assert sb.row_fields("s1", repo, "feat")["behind"] == 2


def test_push_goes_ahead_when_not_behind(on_feat):
    _commit(on_feat["repo"], "mine.txt", "mine\n")
    c = TestClient(server.app)
    r = c.post("/api/instances/s1/push-branch", json={})
    assert r.status_code == 200, r.json()
    assert on_feat["reached"] and "git push" in on_feat["reached"][0]
    assert "--force" not in on_feat["reached"][0]


def test_rebase_remote_success(on_feat):
    repo, mate = on_feat["repo"], on_feat["mate"]
    mine = _commit(repo, "mine.txt", "mine\n")
    theirs = _commit(mate, "f.txt", "v2\n")
    _git(mate, "push", "-q", "origin", "feat")
    c = TestClient(server.app)
    r = c.post("/api/instances/s1/rebase-remote", json={})
    assert r.status_code == 200, r.json()
    assert r.json()["ok"] is True and r.json()["behind"] == 0
    # Their commit is now under mine; mine was replayed (new sha), not lost.
    assert _git(repo, "merge-base", "--is-ancestor", theirs, "HEAD") == ""
    assert _git(repo, "log", "-1", "--format=%s") == "edit mine.txt"
    assert _git(repo, "rev-parse", "HEAD") != mine
    # …and the push goes through now.
    assert c.post("/api/instances/s1/push-branch", json={}).status_code == 200


def test_rebase_remote_conflict_is_aborted(on_feat):
    repo, mate = on_feat["repo"], on_feat["mate"]
    mine = _commit(repo, "f.txt", "my version\n")
    _commit(mate, "f.txt", "their version\n")
    _git(mate, "push", "-q", "origin", "feat")
    c = TestClient(server.app)
    r = c.post("/api/instances/s1/rebase-remote", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["conflicts"] == ["f.txt"]
    assert "aborted" in body["error"]
    # Exactly as it was: same HEAD, clean tree, no rebase in progress.
    assert _git(repo, "rev-parse", "HEAD") == mine
    assert _git(repo, "status", "--porcelain") == ""
    assert sb._in_rebase(repo) is False


def test_rebase_remote_refuses_unknown_remote_and_dirty_tree(on_feat):
    c = TestClient(server.app)
    r = c.post("/api/instances/s1/rebase-remote", json={"remote": "nope"})
    assert r.status_code == 400
    with open(os.path.join(on_feat["repo"], "a.txt"), "w") as fh:
        fh.write("dirty\n")
    r = c.post("/api/instances/s1/rebase-remote", json={})
    assert r.status_code == 409 and r.json().get("dirty") is True


def test_rows_carry_the_flag_and_behind(on_feat):
    from backend.web.core import snapshot

    sb.set_shared("s1", "origin", "feat")
    row = snapshot._instance_json(server.ENGINE.instances["s1"], cheap=True)
    assert row["shared_branch"] is True and row["behind"] == 0
    sb.drop("s1")
    row = snapshot._instance_json(server.ENGINE.instances["s1"], cheap=True)
    assert row["shared_branch"] is False


def test_forget_session_state_drops_the_flag(world):
    sb.set_shared("gone", "origin", "feat")
    server._forget_session_state("gone")
    assert sb.shared("gone") is None


# --------------------------------------------------------------------------- #
# MCP: ship_session reports diverged as a step
# --------------------------------------------------------------------------- #
def test_ship_session_reports_diverged_as_a_step():
    from tests.unit.test_mcp_ship_tools import _ctx, _setup, _steps

    box, api, clock, sim = _setup()
    sim.dirty = False
    api.errors[("POST", "/api/instances/w1/push-branch")] = mf_client.ApiError(
        409,
        "mindflock/w1 moved on origin: 3 new commits by Alice, Bob",
        {
            "error": "mindflock/w1 moved on origin: 3 new commits by Alice, Bob",
            "diverged": True,
            "behind": 3,
            "authors": ["Alice", "Bob"],
            "remote": "origin",
            "branch": "mindflock/w1",
        },
    )
    out = box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
    assert out["ok"] is False
    assert _steps(out) == [("commit", "skipped"), ("push", "diverged")]
    step = out["steps"][-1]
    assert step["behind"] == 3 and step["authors"] == ["Alice", "Bob"]
    assert "branch moved on the remote — rebase first" in out["hint"]
    assert "make-pr" not in [p[0] for p in sim.posts]


def test_get_session_carries_shared_branch_and_behind():
    import inspect

    from backend.mcp import tools

    src = inspect.getsource(tools.Toolbox.get_session)
    assert 'out["shared_branch"]' in src and 'out["behind"]' in src
    assert "shared_branch" in tools._COMPACT_OPTIONAL
    assert "behind" in tools._COMPACT_OPTIONAL


# --------------------------------------------------------------------------- #
# Git-safety pins added after review
# --------------------------------------------------------------------------- #
def test_prepare_never_moves_a_local_branch_with_its_own_commits(world):
    """A local `feat` that is AHEAD of (or diverged from) origin/feat keeps its
    commits: only a strictly-behind branch is fast-forwarded."""
    repo, mate = world["repo"], world["mate"]
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "switch", "-q", "-c", "feat", "origin/feat")
    mine = _commit(repo, "mine.txt", "local work\n")
    _git(repo, "switch", "-q", "main")
    _commit(mate, "f.txt", "feature v2\n")  # the remote moves too: diverged
    _git(mate, "push", "-q", "origin", "feat")
    status, _remote, err = sb.prepare_continue(repo, "origin/feat", "feat")
    assert status == 0, err
    assert _git(repo, "rev-parse", "feat") == mine


def test_prepare_refuses_a_non_origin_remote(world):
    repo = world["repo"]
    _git(repo, "remote", "add", "upstream", world["origin"])
    _git(repo, "fetch", "-q", "upstream")
    status, _remote, err = sb.prepare_continue(repo, "upstream/feat", "feat")
    assert status == 400 and "origin" in err


def test_conflict_abort_restores_the_branch_and_leaves_no_stash(world):
    repo, mate = world["repo"], world["mate"]
    assert sb.prepare_continue(repo, "origin/feat", "feat")[0] == 0
    wt = new_git_worktree_from_branch(repo, "feat", "s-conf")
    wt.Setup()
    path = wt.GetWorktreePath()
    _identity(path, "Me")
    before = _commit(path, "f.txt", "mine\n")
    _commit(mate, "f.txt", "theirs\n")
    _git(mate, "push", "-q", "origin", "feat")
    status, body = sb.rebase_remote(path, "origin", "feat")
    assert status == 409 and body["conflicts"] == ["f.txt"]
    assert _git(path, "rev-parse", "HEAD") == before
    assert _git(path, "branch", "--show-current") == "feat"
    assert _git(path, "stash", "list") == ""
    assert _git(path, "status", "--porcelain") == ""


def test_own_rewritten_history_is_not_someone_elses_push(world):
    """After the session amends its own pushed commit, the remote holds the
    old patch — nothing new: behind counts 0 (git's own non-ff refusal
    still applies at push time)."""
    repo = world["repo"]
    assert sb.prepare_continue(repo, "origin/feat", "feat")[0] == 0
    wt = new_git_worktree_from_branch(repo, "feat", "s-rew")
    wt.Setup()
    path = wt.GetWorktreePath()
    _identity(path, "Me")
    _commit(path, "g.txt", "one\n", msg="mine")
    _git(path, "push", "-q", "origin", "feat")
    _git(path, "commit", "-q", "--amend", "-m", "mine, reworded")
    d = sb.divergence(path, "origin", "feat")
    assert d is not None and d["behind"] == 0


def test_cleanup_of_a_continued_session_keeps_the_branch(world):
    repo = world["repo"]
    assert sb.prepare_continue(repo, "origin/feat", "feat")[0] == 0
    wt = new_git_worktree_from_branch(repo, "feat", "s-keep")
    wt.Setup()
    wt.Cleanup()
    assert _git(repo, "rev-parse", "--verify", "refs/heads/feat")


def test_fetch_against_a_dead_remote_fails_open_quickly(world):
    repo = world["repo"]
    _git(repo, "remote", "set-url", "origin", str(world["tmp"] / "gone.git"))
    assert sb.divergence(repo, "origin", "main") is None


def test_rebase_refused_while_the_agent_works(world, monkeypatch):
    monkeypatch.setattr(server, "_snapshot_activity", lambda title: "working")

    class _Inst:
        def GetWorktreePath(self):
            return world["repo"]

    monkeypatch.setattr(server, "_inst_or_404", lambda title: (_Inst(), None))
    monkeypatch.setattr(server._peer_guard, "refusal", lambda inst, what: None)
    r = TestClient(server.app).post("/api/instances/x/rebase-remote", json={})
    assert r.status_code == 409 and r.json().get("busy") is True
