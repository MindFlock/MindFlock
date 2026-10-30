"""Moving a session's workspace (docs/flights.md "Moving a session"): the
engine's ``MoveWorkspace`` against real temp git repos (a linked worktree, a
main clone with linked worktrees outside and inside it, a plain folder), its
refusals and failure paths, the providers' ``carry_workdir`` hook, and the
``POST /api/instances/{title}/move`` route codes.

tmux is a fake (no agent ever launches) and ``_configure_launch_command`` is
replaced by a recorder, so nothing writes the real ~/.mindflock or
~/.claude.json; HOME points at tmp for the whole module.
"""

from __future__ import annotations

import os
import subprocess
import time

import anyio
import pytest
from fastapi.testclient import TestClient

from backend import providers
from backend.providers import claude as claude_mod
from backend.session import git
from backend.session import instance as inst_mod
from backend.session.instance import (
    Instance,
    MoveError,
    MoveWorkspace,
    _InPlaceWorktree,
)
from backend.session.storage import Status
from backend.web import server
from backend.web.core import events as _events
from backend.web.core import move_session


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------
class FakeTmux:
    """Go-style tmux double: close/start/restore return an Exception or None."""

    def __init__(self):
        self.sanitized_name = "mindflock_s"
        self.launch_command = None
        self.extra_env = {}
        self.alive = True
        self.start_err = None
        self.calls = []

    def close(self):
        self.calls.append("close")
        self.alive = False
        return None

    def restore(self):
        self.calls.append("restore")
        return None

    def start(self, work_dir):
        self.calls.append(("start", work_dir))
        if self.start_err is None:
            self.alive = True
        return self.start_err

    def does_session_exist(self):
        return self.alive


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    launches = []
    monkeypatch.setattr(
        Instance,
        "_configure_launch_command",
        lambda self, resume=False: launches.append(resume),
    )
    monkeypatch.setattr(inst_mod, "_clipboard_write", lambda text: None)
    yield launches


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


def _repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _worktree_paths(repo):
    out = _git(repo, "worktree", "list", "--porcelain")
    return [
        os.path.realpath(line[len("worktree ") :])
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _inst(title, repo, wt, *, in_place=False, status=Status.Running, program="fakecli"):
    inst = Instance()
    inst.Title = title
    inst.Path = str(repo)
    inst.Program = program
    inst.Status = status
    inst.InPlace = in_place
    if in_place:
        inst._git_worktree = _InPlaceWorktree(
            repoPath=str(repo), worktreePath=str(wt), sessionName=title, branchName=""
        )
    else:
        inst._git_worktree = git.NewGitWorktreeFromStorage(
            str(repo), str(wt), title, "feat", "", False
        )
    inst._tmux_session = FakeTmux()
    inst._started = True
    return inst


class _Saves:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def test_move_linked_worktree(tmp_path, _isolated):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    (wt / "dirty.txt").write_text("uncommitted\n")
    inst = _inst("s", repo, wt)
    dest = tmp_path / "moved" / "wt2"
    dest.parent.mkdir()
    save = _Saves()

    assert MoveWorkspace({"s": inst}, "s", str(dest), save=save) is None

    assert not wt.exists() and (dest / "dirty.txt").read_text() == "uncommitted\n"
    assert inst.GetWorktreePath() == str(dest)
    assert inst._git_worktree.GetRepoPath() == str(repo) and inst.Path == str(repo)
    assert os.path.realpath(dest) in _worktree_paths(repo)
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feat"
    # Stopped like Pause, relaunched through Resume's path, in the new folder.
    assert inst._tmux_session.calls == ["close", ("start", str(dest))]
    assert _isolated == [True]
    assert inst.Status == Status.Running and save.n == 1


def test_move_main_clone_repairs_its_worktrees(tmp_path, _isolated):
    repo = _repo(tmp_path / "repo")
    outside = tmp_path / "wt-out"
    inside = repo / ".worktrees" / "wt-in"
    _git(repo, "worktree", "add", "-q", "-b", "out", str(outside))
    _git(repo, "worktree", "add", "-q", "-b", "in", str(inside))
    me = _inst("main", repo, repo, in_place=True)
    twin = _inst("main-copy", repo, repo, in_place=True)  # a window copy, same folder
    child = _inst("child", repo, outside)
    nested = _inst("nested", repo, inside)
    instances = {i.Title: i for i in (me, twin, child, nested)}
    dest = tmp_path / "elsewhere" / "repo"
    dest.parent.mkdir()

    assert MoveWorkspace(instances, "main", str(dest), save=None) is None

    assert not repo.exists()
    for inst in (me, twin):
        assert inst.Path == str(dest) and inst.GetWorktreePath() == str(dest)
    assert child.Path == str(dest) and child._git_worktree.GetRepoPath() == str(dest)
    assert child.GetWorktreePath() == str(outside)
    new_inside = dest / ".worktrees" / "wt-in"
    assert nested.GetWorktreePath() == str(new_inside)
    # git still resolves every worktree from the new place, and they find it.
    listed = _worktree_paths(dest)
    assert os.path.realpath(outside) in listed
    assert os.path.realpath(new_inside) in listed
    assert _git(outside, "rev-parse", "--abbrev-ref", "HEAD").strip() == "out"
    assert _git(new_inside, "rev-parse", "--abbrev-ref", "HEAD").strip() == "in"
    assert os.path.realpath(
        _git(outside, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    ) == os.path.realpath(dest / ".git")


def test_move_plain_folder_keeps_a_paused_session_paused(tmp_path, _isolated):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "notes.md").write_text("x")
    inst = _inst("p", folder, folder, in_place=True, status=Status.Paused)
    dest = tmp_path / "plain2"

    assert MoveWorkspace({"p": inst}, "p", str(dest)) is None

    assert (dest / "notes.md").exists() and not folder.exists()
    assert inst.GetWorktreePath() == str(dest) and inst.Path == str(dest)
    assert inst.Status == Status.Paused
    assert inst._tmux_session.calls == [] and _isolated == []


@pytest.mark.parametrize(
    "dest, code",
    [
        ("relative/path", "bad_dest"),
        ("", "bad_dest"),
        ("{wt}/sub", "bad_dest"),
        ("{wt}", "bad_dest"),
        ("{tmp}/taken", "exists"),
        ("{tmp}/no/such/parent", "bad_dest"),
    ],
)
def test_refusals_touch_nothing(tmp_path, dest, code):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    (tmp_path / "taken").mkdir()
    inst = _inst("s", repo, wt)

    err = MoveWorkspace(
        {"s": inst}, "s", dest.format(wt=wt, tmp=tmp_path), save=_Saves()
    )

    assert isinstance(err, MoveError) and err.code == code
    assert inst.GetWorktreePath() == str(wt) and wt.is_dir()
    assert inst._tmux_session.calls == [] and inst.Status == Status.Running


def test_unknown_title_and_repo_subfolder(tmp_path):
    err = MoveWorkspace({}, "ghost", "/tmp/x")
    assert isinstance(err, MoveError) and err.code == "unknown_session"
    repo = _repo(tmp_path / "repo")
    sub = repo / "pkg"
    sub.mkdir()
    inst = _inst("sub", sub, sub, in_place=True)
    err = MoveWorkspace({"sub": inst}, "sub", str(tmp_path / "pkg2"))
    assert isinstance(err, MoveError) and err.code == "bad_source"
    assert sub.is_dir() and inst._tmux_session.calls == []


def test_failed_git_move_relaunches_where_the_folder_is(tmp_path, _isolated):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    _git(repo, "worktree", "lock", str(wt))  # git refuses to move a locked one
    inst = _inst("s", repo, wt)
    save = _Saves()

    err = MoveWorkspace({"s": inst}, "s", str(tmp_path / "wt2"), save=save)

    assert err is not None and "failed to move workspace" in str(err)
    assert wt.is_dir() and not (tmp_path / "wt2").exists()
    assert inst.GetWorktreePath() == str(wt)
    assert inst._tmux_session.calls == ["close", ("start", str(wt))]
    assert inst.Status == Status.Running and save.n == 1


def test_failed_relaunch_leaves_it_paused_in_the_new_folder(tmp_path):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    inst = _inst("s", repo, wt)
    inst._tmux_session.start_err = RuntimeError("no tmux")
    dest = tmp_path / "wt2"
    save = _Saves()

    err = MoveWorkspace({"s": inst}, "s", str(dest), save=save)

    assert err is not None and "failed to relaunch the agent" in str(err)
    assert inst.GetWorktreePath() == str(dest) and dest.is_dir()
    assert inst.Status == Status.Paused and save.n == 1


def test_peer_share_session_is_refused(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    inst = _inst("peer", folder, folder, in_place=True)
    inst.PeerShare = "abc"
    err = MoveWorkspace({"peer": inst}, "peer", str(tmp_path / "g"))
    assert isinstance(err, MoveError) and err.code == "peer_share"


# ---------------------------------------------------------------------------
# Provider hook
# ---------------------------------------------------------------------------
def _slug(path):
    return claude_mod._project_slug(str(path))


def test_claude_carry_copies_the_transcripts(tmp_path):
    projects = tmp_path / "home" / ".claude" / "projects"
    old, new = tmp_path / "old", tmp_path / "new"
    src = projects / _slug(old)
    src.mkdir(parents=True)
    (src / "abc.jsonl").write_text('{"type":"user"}\n')

    providers.resolve("claude").carry_workdir(str(old), str(new))

    assert (projects / _slug(new) / "abc.jsonl").read_text() == '{"type":"user"}\n'
    assert (src / "abc.jsonl").exists()  # copied, not moved


def test_engine_move_runs_the_claude_hook(tmp_path):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    projects = tmp_path / "home" / ".claude" / "projects"
    (projects / _slug(wt)).mkdir(parents=True)
    (projects / _slug(wt) / "t1.jsonl").write_text("{}\n")
    inst = _inst("s", repo, wt, program="claude")
    dest = tmp_path / "wt2"

    assert MoveWorkspace({"s": inst}, "s", str(dest)) is None
    assert (projects / _slug(dest) / "t1.jsonl").exists()


@pytest.mark.parametrize("program", ["codex", "aider"])
def test_other_providers_carry_nothing(tmp_path, program):
    before = sorted(p for p in (tmp_path / "home").rglob("*"))
    assert providers.resolve(program).carry_workdir("/a/old", "/a/new") is None
    assert sorted(p for p in (tmp_path / "home").rglob("*")) == before


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
client = TestClient(server.app)


@pytest.fixture
def routed(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    inst = _inst("s", repo, wt)
    # A copy of the registry, never the live state.json's dict.
    monkeypatch.setattr(server.ENGINE, "instances", {"s": inst})
    monkeypatch.setattr(server.ENGINE, "save", lambda *a, **k: None)
    monkeypatch.setattr(
        server._ports, "env_for", lambda title, port=None: {"PORT": "1"}
    )
    activity = {"value": "idle"}
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: activity["value"])
    # One event loop for every request (no lifespan: the loops never start),
    # so the move's background task outlives the 202 that started it.
    with anyio.from_thread.start_blocking_portal() as portal:
        monkeypatch.setattr(client, "portal", portal)
        yield {"inst": inst, "wt": wt, "tmp": tmp_path, "activity": activity}


def _move(title, **body):
    return client.post("/api/instances/%s/move" % title, json=body)


def _wait_event(name, title, since):
    deadline = time.time() + 10
    while time.time() < deadline:
        for e in _events.BUS.backlog(since):
            if e["event"] == name and e["session"] == title:
                return e
        time.sleep(0.05)
    raise AssertionError("no %s event" % name)


def test_route_refusals(routed):
    tmp = routed["tmp"]
    (tmp / "taken").mkdir()
    r = _move("ghost", dest=str(tmp / "x"))
    assert r.status_code == 404 and r.json()["code"] == "unknown_session"
    # A device row's route is proxied to that device before it gets here; the
    # gate refuses one all the same.
    refusal, _ = move_session.check({}, "mac::s", {"dest": str(tmp / "x")}, str)
    assert refusal[0] == 409 and refusal[1]["code"] == "remote"
    r = _move("s", dest="not/absolute")
    assert r.status_code == 400 and r.json()["code"] == "bad_dest"
    r = _move("s", dest=str(routed["wt"] / "inner"))
    assert r.status_code == 400 and r.json()["code"] == "bad_dest"
    r = _move("s", dest=str(tmp / "taken"))
    assert r.status_code == 409 and r.json()["code"] == "exists"
    sub = routed["wt"] / "pkg"
    sub.mkdir()
    refusal, _ = move_session.check(
        {"sub": _inst("sub", sub, sub, in_place=True)},
        "sub",
        {"dest": str(tmp / "y")},
        str,
    )
    assert refusal == (400, {"error": refusal[1]["error"], "code": "bad_source"})
    routed["activity"]["value"] = "working"
    r = _move("s", dest=str(tmp / "x"))
    assert r.status_code == 409 and r.json()["code"] == "working"
    assert routed["inst"]._tmux_session.calls == []


def test_route_in_flight(routed, monkeypatch):
    from backend.web.core import flights as F

    monkeypatch.setattr(
        F, "for_session", lambda title, unfinished_only=True: {"state": "running"}
    )
    r = _move("s", dest=str(routed["tmp"] / "x"))
    assert r.status_code == 409 and r.json()["code"] == "in_flight"


def test_route_moves_and_emits(routed, monkeypatch):
    from backend.web.core import flights as F

    refreshed = []
    monkeypatch.setattr(
        F, "list_flights", lambda include_finished=True: refreshed.append(1) or []
    )
    routed["activity"]["value"] = "working"  # force overrides mid-turn
    dest = routed["tmp"] / "moved"
    since = _events.BUS.backlog()[-1]["seq"] if _events.BUS.backlog() else 0

    r = _move("s", dest=str(dest), force=True)

    assert r.status_code == 202
    assert r.json() == {"title": "s", "from": str(routed["wt"]), "to": str(dest)}
    e = _wait_event("session.moved", "s", since)
    assert e["data"] == {"title": "s", "from": str(routed["wt"]), "to": str(dest)}
    assert routed["inst"].GetWorktreePath() == str(dest) and dest.is_dir()
    assert routed["inst"].ExtraEnv.get("PORT") == "1"
    assert move_session.moving("s") is None


def test_route_emits_move_failed(routed):
    _git(routed["inst"].Path, "worktree", "lock", str(routed["wt"]))
    since = _events.BUS.backlog()[-1]["seq"] if _events.BUS.backlog() else 0

    r = _move("s", dest=str(routed["tmp"] / "moved"))

    assert r.status_code == 202
    e = _wait_event("session.move_failed", "s", since)
    assert "failed to move workspace" in e["data"]["error"]
    assert e["data"]["path"] == str(routed["wt"])
