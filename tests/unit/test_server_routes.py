"""Route-level tests for ``backend/web/server.py`` handlers.

These drive the FastAPI app through ``TestClient`` and monkeypatch the
engine / git / tmux / IDE seams, so no test ever spawns a real session, tmux
server, ``gh``/``git`` subprocess, or IDE. Each test asserts a handler's OWN
logic: status codes, JSON serialization, the guard branches (404 / 409 / 400),
and the event + side-effect wiring — the meaningful uncovered surface that the
existing ``test_webui`` / ``test_wave4_backend`` / ``test_prompt_queue`` files
leave untested.

Complements (does not duplicate):

* ``test_prompt_queue`` — queue store + the send / queue CRUD endpoints.
* ``test_wave4_backend`` — diff-stat, base-branch resolution, workspace list.
* ``test_webui`` — index/static serving + create_instance branching.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Loading, Status
from backend.web import server
from backend.web.server import app

client = TestClient(app)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
class _FakeInst:
    """A registry stand-in exposing only what the routes under test touch."""

    Program = "bash"
    InPlace = False

    def __init__(
        self,
        title: str,
        *,
        wt: str = "",
        started: bool = True,
        status: Status = Status.Running,
        program: str = "bash",
    ):
        self.Title = title
        self.Branch = "feat/x"
        self.Path = wt
        self.Status = status
        self.Program = program
        self._wt = wt
        self._started = started
        self.ExtraEnv: dict = {}
        self.calls: list = []

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return self._wt

    def SetStatus(self, status):  # noqa: N802
        self.Status = status

    def Pause(self):  # noqa: N802
        self.calls.append("pause")
        self.Status = Status.Paused

    def Resume(self):  # noqa: N802
        self.calls.append("resume")
        self.Status = Status.Running

    def Kill(self):  # noqa: N802
        self.calls.append("kill")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Never touch the real ``~/.mindflock`` state or fire background tasks.

    ENGINE.save is replaced with a recording no-op (routes call it
    synchronously), and _register_task closes any coroutine it is handed so a
    scheduled ``_bg_start`` never actually runs (nor warns about not being
    awaited).
    """
    saves: list = []
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: saves.append(kw))

    def _close(coro):
        try:
            coro.close()
        except AttributeError:
            pass
        return None

    monkeypatch.setattr(server, "_register_task", _close)
    return SimpleNamespace(saves=saves)


@pytest.fixture
def registered(monkeypatch):
    """Register/cleanup a fake instance under a unique title."""
    created: list = []

    def _make(title: str, **kw) -> _FakeInst:
        inst = _FakeInst(title, **kw)
        monkeypatch.setitem(server.ENGINE.instances, title, inst)
        created.append(title)
        return inst

    yield _make
    for t in created:
        server.ENGINE.instances.pop(t, None)
        server._EVENT_SNAPSHOT.pop(t, None)


# --------------------------------------------------------------------------- #
# budget routes                                                                #
# --------------------------------------------------------------------------- #
def test_get_budget_404_for_unknown():
    r = client.get("/api/instances/nope-budget/budget")
    assert r.status_code == 404
    assert "instance not found" in r.json()["error"]


def test_get_budget_serializes_cost(registered, monkeypatch):
    registered("bud-1", wt="/tmp/x")
    monkeypatch.setattr(server, "_session_tokens", lambda inst: {"cost": 1.25})
    monkeypatch.setattr(
        server, "_budget_status_for", lambda t, c: {"cost": c, "limit": 5.0}
    )
    r = client.get("/api/instances/bud-1/budget")
    assert r.status_code == 200
    assert r.json() == {"cost": 1.25, "limit": 5.0}


def test_get_budget_swallows_token_error(registered, monkeypatch):
    registered("bud-2", wt="/tmp/x")

    def _boom(inst):
        raise RuntimeError("no tokens")

    monkeypatch.setattr(server, "_session_tokens", _boom)
    captured = {}
    monkeypatch.setattr(
        server, "_budget_status_for", lambda t, c: captured.setdefault("cost", c) or {}
    )
    assert client.get("/api/instances/bud-2/budget").status_code == 200
    # A failed cost read is treated as 0.0, not a 500.
    assert captured["cost"] == 0.0


def test_raise_budget_rejects_nonpositive_limit(registered):
    registered("bud-3", wt="/tmp/x")
    r = client.post("/api/instances/bud-3/budget/raise", json={"limit": 0})
    assert r.status_code == 400
    assert "positive dollar amount" in r.json()["error"]


def test_raise_budget_forever_sets_no_expiry(registered, monkeypatch):
    registered("bud-4", wt="/tmp/x")
    monkeypatch.setattr(server, "_session_tokens", lambda inst: {"cost": 0.0})
    seen = {}

    def _set(title, limit, expires):
        seen["title"], seen["limit"], seen["expires"] = title, limit, expires

    monkeypatch.setattr(server, "_set_budget_override", _set)
    monkeypatch.setattr(server, "_budget_status_for", lambda t, c: {"ok": True})
    events = []
    monkeypatch.setattr(
        server._events.BUS,
        "emit",
        lambda name, **kw: events.append((name, kw)),
    )
    # hours omitted -> forever (expires is None).
    r = client.post("/api/instances/bud-4/budget/raise", json={"limit": 10})
    assert r.status_code == 200
    assert seen == {"title": "bud-4", "limit": 10.0, "expires": None}
    assert any(n == "session.budget_raised" for n, _ in events)


def test_raise_budget_hours_sets_future_expiry(registered, monkeypatch):
    registered("bud-5", wt="/tmp/x")
    monkeypatch.setattr(server, "_session_tokens", lambda inst: {"cost": 0.0})
    seen = {}
    monkeypatch.setattr(
        server,
        "_set_budget_override",
        lambda title, limit, expires: seen.update(expires=expires),
    )
    monkeypatch.setattr(server, "_budget_status_for", lambda t, c: {})
    monkeypatch.setattr(server._events.BUS, "emit", lambda *a, **k: None)
    import time as _t

    before = _t.time()
    client.post("/api/instances/bud-5/budget/raise", json={"limit": 3, "hours": 2})
    # 2h in the future (allow scheduling slack).
    assert seen["expires"] > before + 2 * 3600 - 5


def test_raise_budget_bad_limit_string_is_400(registered):
    registered("bud-6", wt="/tmp/x")
    r = client.post("/api/instances/bud-6/budget/raise", json={"limit": "not-a-number"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# /api/mkdir                                                                   #
# --------------------------------------------------------------------------- #
def test_mkdir_requires_parent():
    r = client.post("/api/mkdir", json={"name": "x"})
    assert r.status_code == 400
    assert "parent folder is required" in r.json()["error"]


def test_mkdir_requires_name(tmp_path):
    r = client.post("/api/mkdir", json={"path": str(tmp_path)})
    assert r.status_code == 400
    assert "folder name is required" in r.json()["error"]


@pytest.mark.parametrize("bad", ["a/b", "a\\b", ".", ".."])
def test_mkdir_rejects_path_in_name(tmp_path, bad):
    r = client.post("/api/mkdir", json={"path": str(tmp_path), "name": bad})
    assert r.status_code == 400
    assert "must not contain a path" in r.json()["error"]


def test_mkdir_rejects_missing_parent_dir(tmp_path):
    r = client.post(
        "/api/mkdir", json={"path": str(tmp_path / "nope"), "name": "child"}
    )
    assert r.status_code == 400
    assert "not a directory" in r.json()["error"]


def test_mkdir_creates_and_returns_abs_path(tmp_path):
    r = client.post("/api/mkdir", json={"path": str(tmp_path), "name": "made"})
    assert r.status_code == 200
    made = r.json()["path"]
    assert made == str(tmp_path / "made")
    assert os.path.isdir(made)


def test_mkdir_rejects_existing(tmp_path):
    (tmp_path / "dup").mkdir()
    r = client.post("/api/mkdir", json={"path": str(tmp_path), "name": "dup"})
    assert r.status_code == 400
    assert "already exists" in r.json()["error"]


# --------------------------------------------------------------------------- #
# /api/browse                                                                  #
# --------------------------------------------------------------------------- #
def test_browse_rejects_non_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    r = client.get("/api/browse", params={"path": str(f)})
    assert r.status_code == 400
    assert "not a directory" in r.json()["error"]


def test_browse_lists_subdirs_and_hides_dotfolders(tmp_path, monkeypatch):
    (tmp_path / "visible").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "afile").write_text("x")
    monkeypatch.setattr(server, "_is_git_repo", lambda p: False)
    r = client.get("/api/browse", params={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert names == ["visible"]  # dotfolder + plain file excluded
    assert body["path"] == str(tmp_path)
    assert body["parent"] == os.path.dirname(str(tmp_path))
    assert body["is_git"] is False


def test_browse_flags_git_repos(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(server, "_is_git_repo", lambda p: os.path.basename(p) == "repo")
    r = client.get("/api/browse", params={"path": str(tmp_path)})
    entry = next(e for e in r.json()["entries"] if e["name"] == "repo")
    assert entry["is_git"] is True


# --------------------------------------------------------------------------- #
# pause / resume                                                               #
# --------------------------------------------------------------------------- #
def test_pause_calls_pause_and_emits(registered, monkeypatch):
    inst = registered("pr-1", wt="/tmp/x")
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
    events = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda n, **k: events.append((n, k))
    )
    r = client.post("/api/instances/pr-1/pause")
    assert r.status_code == 200
    assert "pause" in inst.calls
    assert any(n == "session.paused" for n, _ in events)


def test_pause_surfaces_error_as_400(registered, monkeypatch):
    inst = registered("pr-2", wt="/tmp/x")
    monkeypatch.setattr(
        inst, "Pause", lambda: (_ for _ in ()).throw(RuntimeError("cant pause"))
    )
    r = client.post("/api/instances/pr-2/pause")
    assert r.status_code == 400
    assert "cant pause" in r.json()["error"]


def test_resume_rederives_ports_and_emits(registered, monkeypatch):
    inst = registered("pr-3", wt="/tmp/x")
    monkeypatch.setattr(server._ports, "env_for", lambda t: {"PORT": "9000"})
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
    events = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda n, **k: events.append((n, k))
    )
    r = client.post("/api/instances/pr-3/resume")
    assert r.status_code == 200
    assert "resume" in inst.calls
    assert inst.ExtraEnv == {"PORT": "9000"}
    assert any(n == "session.resumed" for n, _ in events)


def test_resume_surfaces_error_as_400(registered, monkeypatch):
    inst = registered("pr-4", wt="/tmp/x")
    monkeypatch.setattr(server._ports, "env_for", lambda t: {})
    monkeypatch.setattr(
        inst, "Resume", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert client.post("/api/instances/pr-4/resume").status_code == 400


# --------------------------------------------------------------------------- #
# cleanup / close / copy                                                       #
# --------------------------------------------------------------------------- #
@pytest.fixture
def _stub_lifecycle(monkeypatch):
    """Neutralize the destructive helpers cleanup/close/copy call."""
    monkeypatch.setattr(server, "_kill_shell_session", lambda t: None)
    monkeypatch.setattr(server, "_kill_agent_session", lambda t: None)
    monkeypatch.setattr(server, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(server, "_remove_trust_entry", lambda p: None)
    monkeypatch.setattr(server, "_worktree_in_use_by_other", lambda wt, t: False)
    monkeypatch.setattr(server._events.BUS, "emit", lambda *a, **k: None)


def test_cleanup_rmtrees_worktree_and_drops_instance(
    registered, _stub_lifecycle, monkeypatch, tmp_path
):
    wt = tmp_path / "ws"
    wt.mkdir()
    inst = registered("cl-1", wt=str(wt))
    removed = []
    monkeypatch.setattr(server.shutil, "rmtree", lambda p, **k: removed.append(p))
    r = client.post("/api/instances/cl-1/cleanup")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "kill" in inst.calls
    assert removed == [str(wt)]
    assert "cl-1" not in server.ENGINE.instances


def test_cleanup_never_rmtrees_in_place_repo(
    registered, _stub_lifecycle, monkeypatch, tmp_path
):
    wt = tmp_path / "own-repo"
    wt.mkdir()
    inst = registered("cl-2", wt=str(wt))
    inst.InPlace = True
    removed = []
    monkeypatch.setattr(server.shutil, "rmtree", lambda p, **k: removed.append(p))
    assert client.post("/api/instances/cl-2/cleanup").status_code == 200
    # In-place sessions run in the user's own repo — it must survive cleanup.
    assert removed == []


def test_close_records_closed_and_keeps_worktree(
    registered, _stub_lifecycle, monkeypatch, tmp_path
):
    wt = tmp_path / "keep"
    wt.mkdir()
    registered("cl-3", wt=str(wt))
    recorded = []
    monkeypatch.setattr(server, "_record_closed", lambda i: recorded.append(i.Title))
    r = client.post("/api/instances/cl-3/close")
    assert r.status_code == 200
    assert recorded == ["cl-3"]
    assert os.path.isdir(wt)  # worktree kept
    assert "cl-3" not in server.ENGINE.instances


def test_copy_409_when_source_workspace_missing(registered):
    registered("cp-1", wt="/tmp/does-not-exist-xyz")
    r = client.post("/api/instances/cp-1/copy")
    assert r.status_code == 409
    assert "source workspace not ready" in r.json()["error"]


def test_copy_registers_in_place_child(registered, monkeypatch, tmp_path):
    wt = tmp_path / "src"
    wt.mkdir()
    registered("cp-2", wt=str(wt))
    captured = {}

    def _new_instance(opts):
        captured["opts"] = opts
        return _FakeInst(opts.title, wt=str(wt))

    monkeypatch.setattr(server.session, "NewInstance", _new_instance)
    monkeypatch.setattr(server, "_unique_title", lambda base: base)
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
    monkeypatch.setattr(server._events.BUS, "emit", lambda *a, **k: None)
    r = client.post("/api/instances/cp-2/copy")
    assert r.status_code == 202
    assert r.json()["title"] == "cp-2-copy"
    # The copy is always in-place (shares the source's worktree, never deletes it).
    assert captured["opts"].in_place is True
    assert captured["opts"].path == str(wt)
    assert "cp-2-copy" in server.ENGINE.instances
    server.ENGINE.instances.pop("cp-2-copy", None)


# --------------------------------------------------------------------------- #
# setup / check (verification gate) routes                                     #
# --------------------------------------------------------------------------- #
def test_setup_status_404_for_unknown():
    assert client.get("/api/instances/nope/setup").status_code == 404


def test_setup_status_409_when_worktree_missing(registered):
    registered("st-1", wt="/tmp/gone-xyz")
    r = client.get("/api/instances/st-1/setup")
    assert r.status_code == 409
    assert "workspace not ready" in r.json()["error"]


def test_setup_rerun_400_when_no_setup_configured(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("st-2", wt=str(wt))
    monkeypatch.setattr(
        server._wt_setup, "load_config", lambda w: SimpleNamespace(has_setup=False)
    )
    r = client.post("/api/instances/st-2/setup/rerun")
    assert r.status_code == 400
    assert "no [workspace] setup" in r.json()["error"]


def test_setup_rerun_409_when_already_running(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    inst = registered("st-3", wt=str(wt))
    inst.Path = str(wt)
    monkeypatch.setattr(
        server._wt_setup, "load_config", lambda w: SimpleNamespace(has_setup=True)
    )
    monkeypatch.setattr(server._wt_setup, "start_setup", lambda *a, **k: False)
    r = client.post("/api/instances/st-3/setup/rerun")
    assert r.status_code == 409
    assert "already running" in r.json()["error"]


def test_setup_rerun_202_on_start(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    inst = registered("st-4", wt=str(wt))
    inst.Path = str(wt)
    monkeypatch.setattr(
        server._wt_setup, "load_config", lambda w: SimpleNamespace(has_setup=True)
    )
    monkeypatch.setattr(server._wt_setup, "start_setup", lambda *a, **k: True)
    monkeypatch.setattr(server._wt_setup, "setup_summary", lambda w: {"state": "run"})
    r = client.post("/api/instances/st-4/setup/rerun")
    assert r.status_code == 202
    assert r.json() == {"ok": True, "status": {"state": "run"}}


def test_check_run_400_when_no_check_command(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("ck-1", wt=str(wt))
    monkeypatch.setattr(
        server._wt_setup, "load_config", lambda w: SimpleNamespace(check_command="")
    )
    r = client.post("/api/instances/ck-1/check")
    assert r.status_code == 400
    assert "no [workspace] check_command" in r.json()["error"]


def test_check_run_202_on_start(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("ck-2", wt=str(wt))
    monkeypatch.setattr(
        server._wt_setup,
        "load_config",
        lambda w: SimpleNamespace(check_command="pytest -q"),
    )
    monkeypatch.setattr(server._wt_setup, "start_check", lambda *a, **k: True)
    monkeypatch.setattr(server._wt_setup, "check_summary", lambda w: {"state": "run"})
    r = client.post("/api/instances/ck-2/check")
    assert r.status_code == 202


# --------------------------------------------------------------------------- #
# guided git flow: commit / push-branch / branches / make-pr / merge-pr        #
# early-return guards (no real git/gh subprocess involved)                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def _no_git(monkeypatch):
    monkeypatch.setattr(server, "git_available", lambda: False)


def test_commit_no_git_uses_uniform_response(registered, _no_git):
    registered("gt-1", wt="/tmp/x")
    r = client.post("/api/instances/gt-1/commit", json={"message": "m"})
    # _no_git_response() is the uniform 409 for git-only endpoints.
    assert r.status_code == 409
    assert "git is not installed" in r.json()["error"]


def test_commit_400_without_message(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-2", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    r = client.post("/api/instances/gt-2/commit", json={})
    assert r.status_code == 400
    assert "commit message required" in r.json()["error"]


def test_commit_409_when_worktree_missing(registered, monkeypatch):
    registered("gt-3", wt="")
    monkeypatch.setattr(server, "git_available", lambda: True)
    r = client.post("/api/instances/gt-3/commit", json={"message": "m"})
    assert r.status_code == 409
    assert "workspace not ready" in r.json()["error"]


def test_commit_reuses_saved_message_file(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    (wt / ".mindflock_commit_msg").write_text("previous message\n")
    registered("gt-4", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_exclude_artifacts", lambda p: None)
    sent = {}
    monkeypatch.setattr(
        server, "_ensure_shell_session", lambda t, w: ("shell_" + t, None)
    )
    monkeypatch.setattr(
        server, "_send_to_shell", lambda name, cmd: sent.update(name=name, cmd=cmd)
    )
    monkeypatch.setattr(server, "_shell_tmux_name", lambda t: "shell_" + t)
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    # Empty message + an existing msg file -> the commit proceeds (message reused).
    r = client.post("/api/instances/gt-4/commit", json={"message": ""})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "git commit" in sent["cmd"]


def test_push_branch_400_without_origin(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-5", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(
        server._wt_setup, "load_config", lambda w: SimpleNamespace(check_command="")
    )
    monkeypatch.setattr(server, "_has_origin", lambda wt, fresh=False: False)
    r = client.post("/api/instances/gt-5/push-branch", json={"force": True})
    assert r.status_code == 400
    assert "no origin remote" in r.json()["error"]


def test_push_branch_409_when_check_required(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-6", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(
        server._wt_setup,
        "load_config",
        lambda w: SimpleNamespace(check_command="pytest"),
    )
    # No passing check for this commit -> soft gate 409.
    monkeypatch.setattr(server._wt_setup, "check_summary", lambda w: None)
    r = client.post("/api/instances/gt-6/push-branch", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["check_required"] is True


def test_push_branch_success_marks_pending(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-7", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_has_origin", lambda wt, fresh=False: True)
    monkeypatch.setattr(
        server, "_ensure_shell_session", lambda t, w: ("shell_" + t, None)
    )
    pushed = {}
    monkeypatch.setattr(
        server, "_send_to_shell", lambda name, cmd: pushed.update(cmd=cmd)
    )
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    marked = {}
    monkeypatch.setattr(
        server,
        "mark_origin_push_pending",
        lambda wt, br: marked.update(wt=wt, br=br),
    )
    monkeypatch.setattr(server, "_shell_tmux_name", lambda t: "shell_" + t)
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    r = client.post("/api/instances/gt-7/push-branch", json={"force": True})
    assert r.status_code == 200
    assert "git push" in pushed["cmd"]
    assert marked == {"wt": str(wt), "br": "feat/x"}


def test_branches_lists_remote_heads(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-8", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)

    def _fake_run(args, **kw):
        # ls-remote --heads origin
        out = b"sha1\trefs/heads/main\nsha2\trefs/heads/dev\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    monkeypatch.setattr(server, "_run_capped", _fake_run)
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "main")
    r = client.get("/api/instances/gt-8/branches")
    assert r.status_code == 200
    body = r.json()
    assert body["branches"] == ["dev", "main"]  # sorted, de-prefixed
    assert body["current"] == "feat/x"
    assert body["default"] == "main"


def test_make_pr_400_without_gh(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-9", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    r = client.post("/api/instances/gt-9/make-pr", json={})
    assert r.status_code == 400
    assert "gh" in r.json()["error"].lower()


def test_make_pr_409_when_on_base_branch(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-10", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "main")
    monkeypatch.setattr(server, "_current_branch", lambda w: "main")
    r = client.post("/api/instances/gt-10/make-pr", json={})
    assert r.status_code == 409
    assert "base branch" in r.json()["error"]


def test_make_pr_success_returns_url(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-11", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "main")
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    url = "https://github.com/o/r/pull/7"

    def _fake_run(args, **kw):
        return SimpleNamespace(returncode=0, stdout=(url + "\n").encode(), stderr=b"")

    monkeypatch.setattr(server, "_run_capped", _fake_run)
    r = client.post("/api/instances/gt-11/make-pr", json={"base": "main"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "url": url}


def test_make_pr_no_commits_message(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-12", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "main")
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)

    def _fake_run(args, **kw):
        return SimpleNamespace(
            returncode=1, stdout=b"no commits between main and feat/x", stderr=b""
        )

    monkeypatch.setattr(server, "_run_capped", _fake_run)
    r = client.post("/api/instances/gt-12/make-pr", json={})
    assert r.status_code == 400
    assert "nothing to PR" in r.json()["error"]


def test_make_pr_bounces_to_existing_open_pr(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-13", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "main")
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    monkeypatch.setattr(
        server,
        "_pr_info",
        lambda *a, **k: {"state": "OPEN", "url": "https://x/pull/1"},
    )
    monkeypatch.setattr(
        server,
        "_run_capped",
        lambda args, **kw: SimpleNamespace(
            returncode=1, stdout=b"a pull request already exists", stderr=b""
        ),
    )
    r = client.post("/api/instances/gt-13/make-pr", json={})
    assert r.status_code == 200
    assert r.json()["note"] == "PR already open"
    assert r.json()["url"] == "https://x/pull/1"


def test_merge_pr_failure_surfaces_gh_error(registered, monkeypatch, tmp_path):
    wt = tmp_path / "ws"
    wt.mkdir()
    registered("gt-14", wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(
        server,
        "_run_capped",
        lambda args, **kw: SimpleNamespace(
            returncode=1, stdout=b"required reviews missing", stderr=b""
        ),
    )
    r = client.post("/api/instances/gt-14/merge-pr")
    assert r.status_code == 400
    assert "required reviews missing" in r.json()["error"]


# --------------------------------------------------------------------------- #
# recently-closed: list / reopen / forget                                      #
# --------------------------------------------------------------------------- #
def test_recently_closed_list_shape(monkeypatch, tmp_path):
    folder = str(tmp_path / "exists")
    os.makedirs(folder)
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "e1",
                "title": "t1",
                "branch": "b1",
                "folder": folder,
                "in_place": False,
                "provisioned": True,
                "closed_at": 123,
            },
            {"id": "e2", "title": "t2", "branch": "b2", "folder": "/gone-xyz"},
        ],
    )
    r = client.get("/api/recently-closed")
    assert r.status_code == 200
    items = r.json()
    assert items[0]["exists"] is True and items[0]["provisioned"] is True
    assert items[1]["exists"] is False  # folder gone -> exists false


def test_reopen_404_for_unknown_entry(monkeypatch):
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    r = client.post("/api/recently-closed/nope/reopen")
    assert r.status_code == 404
    assert "entry not found" in r.json()["error"]


def test_reopen_410_when_worktree_gone(monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "e1", "title": "t", "folder": "/gone-xyz-410", "data": {}}],
    )
    r = client.post("/api/recently-closed/e1/reopen")
    assert r.status_code == 410
    assert "no longer exists" in r.json()["error"]


def test_forget_404_for_unknown_entry(monkeypatch):
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    r = client.post("/api/recently-closed/nope/forget", json={})
    assert r.status_code == 404


def test_forget_drops_entry_and_saves(monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "e1", "title": "t"}, {"id": "e2", "title": "u"}],
    )
    saved = {}
    monkeypatch.setattr(
        server, "_save_recently_closed", lambda items: saved.setdefault("items", items)
    )
    r = client.post("/api/recently-closed/e1/forget", json={})
    assert r.status_code == 200
    # e1 removed, e2 kept.
    assert [e["id"] for e in saved["items"]] == ["e2"]


def test_forget_wipe_removes_worktree(monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [
            {
                "id": "e1",
                "title": "t",
                "in_place": False,
                "folder": "/ws/e1",
                "data": {"worktree": {"repo_path": "/base"}},
            }
        ],
    )
    monkeypatch.setattr(server, "_save_recently_closed", lambda items: None)
    wiped = {}
    monkeypatch.setattr(
        server,
        "_remove_worktree_path",
        lambda folder, repo: wiped.update(folder=folder, repo=repo),
    )
    r = client.post("/api/recently-closed/e1/forget", json={"wipe": True})
    assert r.status_code == 200
    assert wiped == {"folder": "/ws/e1", "repo": "/base"}


def test_forget_wipe_skipped_for_in_place(monkeypatch):
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"id": "e1", "title": "t", "in_place": True, "folder": "/own"}],
    )
    monkeypatch.setattr(server, "_save_recently_closed", lambda items: None)
    called = []
    monkeypatch.setattr(server, "_remove_worktree_path", lambda *a: called.append(a))
    r = client.post("/api/recently-closed/e1/forget", json={"wipe": True})
    assert r.status_code == 200
    # In-place sessions live in the user's own dir — wipe must never rmtree it.
    assert called == []


# --------------------------------------------------------------------------- #
# IDE routes                                                                   #
# --------------------------------------------------------------------------- #
def test_list_ides_flags_installed(monkeypatch):
    from backend.web.core import ide_launch as _ide_launch

    spec_a = SimpleNamespace(command="cursor", name="Cursor", kind="gui")
    spec_b = SimpleNamespace(command="nvim", name="Neovim", kind="terminal")
    monkeypatch.setattr(_ide_launch, "detect_ides", lambda: [spec_a])
    monkeypatch.setattr(server.ide_cfg, "known_ide_specs", lambda: [spec_a, spec_b])
    monkeypatch.setattr(server.ide_cfg, "ide_command", lambda: "cursor")
    monkeypatch.setattr(server.ide_cfg, "ide_name", lambda: "Cursor")
    r = client.get("/api/ides")
    assert r.status_code == 200
    body = r.json()
    by_cmd = {e["command"]: e for e in body["ides"]}
    assert by_cmd["cursor"]["installed"] is True
    assert by_cmd["nvim"]["installed"] is False
    assert body["current"] == "cursor"


def test_instance_ide_409_when_workspace_missing(registered):
    registered("ide-1", wt="")
    r = client.post("/api/instances/ide-1/ide")
    assert r.status_code == 409
    assert "workspace not ready" in r.json()["error"]


def test_instance_ide_maps_launch_error_to_400(registered, monkeypatch, tmp_path):
    from backend.web.core import ide_launch as _ide_launch

    wt = tmp_path / "ws"
    wt.mkdir()
    registered("ide-2", wt=str(wt))
    monkeypatch.setattr(server.ide_cfg, "ide_kind", lambda: "terminal")
    monkeypatch.setattr(server.ide_cfg, "ide_name", lambda: "Neovim")

    def _boom(path):
        raise _ide_launch.IdeLaunchError("no display")

    monkeypatch.setattr(_ide_launch, "launch_ide", _boom)
    r = client.post("/api/instances/ide-2/ide")
    assert r.status_code == 400
    assert "no display" in r.json()["error"]


def test_instance_ide_terminal_opens_new(registered, monkeypatch, tmp_path):
    from backend.web.core import ide_launch as _ide_launch

    wt = tmp_path / "ws"
    wt.mkdir()
    registered("ide-3", wt=str(wt))
    monkeypatch.setattr(server.ide_cfg, "ide_kind", lambda: "terminal")
    launched = []
    monkeypatch.setattr(_ide_launch, "launch_ide", lambda p: launched.append(p))
    r = client.post("/api/instances/ide-3/ide")
    assert r.status_code == 200
    # Terminal editors are never "already open": always a fresh window.
    assert r.json() == {"ok": True, "opened_new": True}
    assert launched == [str(wt)]


# --------------------------------------------------------------------------- #
# small config/toggle routes                                                   #
# --------------------------------------------------------------------------- #
def test_list_providers_excludes_generic():
    r = client.get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    names = {p["name"] for p in body["providers"]}
    assert "generic" not in names
    assert body["default"]  # a default program is always named
    # each entry carries the usage-window descriptor
    assert all("usage_window" in p for p in body["providers"])


def test_cursor_autoadopt_roundtrip():
    original = server._CURSOR_AUTOADOPT_ENABLED
    try:
        assert client.get("/api/cursor/autoadopt").json()["enabled"] == bool(original)
        assert (
            client.post("/api/cursor/autoadopt", json={"enabled": True}).json()[
                "enabled"
            ]
            is True
        )
        assert server._CURSOR_AUTOADOPT_ENABLED is True
        assert (
            client.post("/api/cursor/autoadopt", json={"enabled": False}).json()[
                "enabled"
            ]
            is False
        )
    finally:
        server._CURSOR_AUTOADOPT_ENABLED = original


def test_scroll_speed_get(monkeypatch):
    monkeypatch.setattr(server, "load_scroll_speed", lambda: 4)
    assert client.get("/api/scroll-speed").json() == {"speed": 4}


def test_scroll_speed_set_applies_and_swallows_apply_error(monkeypatch):
    monkeypatch.setattr(server, "save_scroll_speed", lambda raw: 7)

    def _boom(speed):
        raise RuntimeError("no tmux server")

    monkeypatch.setattr(server, "apply_scroll_speed", _boom)
    # A live-apply failure must not fail the request — the value still persists.
    r = client.post("/api/scroll-speed", json={"speed": 7})
    assert r.status_code == 200
    assert r.json() == {"speed": 7}


def test_window_refresh_get_serialization(monkeypatch):
    monkeypatch.setattr(
        server._window_refresh,
        "get_config",
        lambda: {
            "enabled": True,
            "interval_hours": 5.0,
            "anchor_time": "09:00",
            "providers": ["claude"],
            "last_fired": {"claude": 111.0},
        },
    )
    monkeypatch.setattr(server._window_refresh, "next_fire_at", lambda name, cfg: 222.0)
    r = client.get("/api/window-refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["interval_hours"] == 5.0
    assert body["anchor_time"] == "09:00"
    # options carry per-provider window + schedule info
    opt = next(o for o in body["options"] if o["name"] == "claude")
    assert opt["next_fire"] == 222.0 and opt["last_fired"] == 111.0


def test_window_refresh_set_passes_through(monkeypatch):
    seen = {}

    def _set_config(**kw):
        seen.update(kw)
        return {
            "enabled": False,
            "interval_hours": 8.0,
            "anchor_time": "",
            "providers": [],
        }

    monkeypatch.setattr(server._window_refresh, "set_config", _set_config)
    r = client.post(
        "/api/window-refresh",
        json={"enabled": False, "interval_hours": 8, "providers": []},
    )
    assert r.status_code == 200
    assert r.json()["interval_hours"] == 8.0
    # only the body's keys are forwarded to set_config
    assert seen["enabled"] is False and seen["interval_hours"] == 8


# --------------------------------------------------------------------------- #
# devices (remote pairing) + mobile + logs                                     #
# --------------------------------------------------------------------------- #
def test_connect_device_400_on_failure(monkeypatch):
    async def _connect(device, token):
        return False, "bad token"

    monkeypatch.setattr(server._remote, "connect_device", _connect)
    r = client.post("/api/devices/dev1/connect", json={"token": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad token"


def test_connect_device_success_returns_devices(monkeypatch):
    async def _connect(device, token):
        return True, None

    monkeypatch.setattr(server._remote, "connect_device", _connect)
    monkeypatch.setattr(server._remote, "devices_json", lambda: {"devices": ["dev1"]})
    r = client.post("/api/devices/dev1/connect", json={"token": "tok"})
    assert r.status_code == 200
    assert r.json() == {"devices": ["dev1"]}


def test_disconnect_device_forgets_and_returns_devices(monkeypatch):
    forgotten = []
    monkeypatch.setattr(server._remote, "forget_device", lambda d: forgotten.append(d))
    monkeypatch.setattr(server._remote, "devices_json", lambda: {"devices": []})
    r = client.post("/api/devices/dev9/disconnect")
    assert r.status_code == 200
    assert forgotten == ["dev9"]


def test_mobile_falls_back_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("no tailscale")

    monkeypatch.setattr(server, "_mobile_info", _boom)
    r = client.get("/api/mobile")
    assert r.status_code == 200
    # Never 500s the settings screen — degrades to an "unavailable" note.
    assert r.json()["note"] == "unavailable"
    assert r.json()["urls"] == []


def test_logs_falls_back_to_first_source_on_unknown_name(monkeypatch, tmp_path):
    logf = tmp_path / "server.log"
    logf.write_text("hello log\n")
    monkeypatch.setattr(
        server, "_log_sources", lambda: [{"name": "server", "path": str(logf)}]
    )
    monkeypatch.setattr(server, "_read_log_tail", lambda p: ("hello log\n", 10))
    r = client.get("/api/logs", params={"name": "does-not-exist"})
    assert r.status_code == 200
    body = r.json()
    assert body["selected"] == "server"  # unknown name falls back to first source
    assert body["exists"] is True
    assert body["text"] == "hello log\n"


def test_logs_never_500s_on_internal_error(monkeypatch):
    def _boom():
        raise RuntimeError("log discovery broke")

    monkeypatch.setattr(server, "_log_sources", _boom)
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert r.json()["sources"] == []


# --------------------------------------------------------------------------- #
# /api/usage                                                                   #
# --------------------------------------------------------------------------- #
def test_usage_windows_has_period_and_provider_keys():
    r = client.get("/api/usage")
    assert r.status_code == 200
    body = r.json()
    # The endpoint always carries the provider list + default even when idle.
    assert "providers" in body and isinstance(body["providers"], list)
    assert "default" in body


# --------------------------------------------------------------------------- #
# pane history                                                                 #
# --------------------------------------------------------------------------- #
def test_pane_history_404_for_unknown():
    r = client.get("/api/instances/nope/history")
    assert r.status_code == 404
    assert r.text == "instance not found"


def test_pane_history_no_live_shell_session(registered, monkeypatch):
    registered("ph-1", wt="/tmp/x")
    monkeypatch.setattr(server, "_shell_tmux_name", lambda t: "shell_" + t)
    monkeypatch.setattr(server, "_live_session_name", lambda base: None)
    r = client.get("/api/instances/ph-1/history", params={"pane": "shell"})
    assert r.status_code == 404
    assert r.text == "no live session"


def test_pane_history_falls_back_to_transcript(registered, monkeypatch):
    registered("ph-2", wt="/tmp/x")
    monkeypatch.setattr(server, "_live_session_name", lambda base: None)
    monkeypatch.setattr(
        server, "_agent_transcript_text", lambda wt: "recovered transcript"
    )
    # Agent pane, session gone -> the on-disk transcript is served instead of 404.
    r = client.get("/api/instances/ph-2/history")
    assert r.status_code == 200
    assert r.text == "recovered transcript"


# --------------------------------------------------------------------------- #
# review/ingest LIST endpoints (the force-START endpoints are deferred)        #
# --------------------------------------------------------------------------- #
async def _aval(v):
    return v


def test_github_open_prs_annotates_has_session(registered, monkeypatch):
    registered("owner/repo#3", wt="/tmp/x")  # a live session matching a PR
    server._OPEN_PRS_CACHE.pop("v", None)
    monkeypatch.setattr(
        server._pr_review,
        "list_open_prs",
        lambda: _aval(
            {
                "prs": [
                    {"number": 3, "session": "owner/repo#3"},
                    {"number": 4, "session": "owner/repo#4"},
                ]
            }
        ),
    )
    try:
        r = client.get("/api/github/prs")
        assert r.status_code == 200
        by_num = {p["number"]: p for p in r.json()["prs"]}
        assert by_num[3]["has_session"] is True  # a session is live for it
        assert by_num[4]["has_session"] is False
    finally:
        server._OPEN_PRS_CACHE.pop("v", None)


def test_github_open_prs_502_on_error(monkeypatch):
    server._OPEN_PRS_CACHE.pop("v", None)

    async def _boom():
        raise RuntimeError("no gh token")

    monkeypatch.setattr(server._pr_review, "list_open_prs", _boom)
    try:
        r = client.get("/api/github/prs")
        assert r.status_code == 502
        assert "no gh token" in r.json()["error"]
    finally:
        server._OPEN_PRS_CACHE.pop("v", None)


def test_assigned_tickets_annotates_has_session(monkeypatch):
    server._ASSIGNED_TICKETS_CACHE.pop("v", None)
    server._TICKET_STARTS.add("sc-99")  # a start already in flight
    monkeypatch.setattr(
        server._ticket_start,
        "list_assigned_tickets",
        lambda: _aval({"tickets": [{"id": "sc-99", "session": "sc-99"}]}),
    )
    try:
        r = client.get("/api/tickets")
        assert r.status_code == 200
        # a ticket whose start is in-flight (in _TICKET_STARTS) reads as claimed
        assert r.json()["tickets"][0]["has_session"] is True
    finally:
        server._ASSIGNED_TICKETS_CACHE.pop("v", None)
        server._TICKET_STARTS.discard("sc-99")


def test_github_open_issues_502_on_error(monkeypatch):
    server._OPEN_ISSUES_CACHE.pop("v", None)

    async def _boom():
        raise RuntimeError("issues unconfigured")

    monkeypatch.setattr(server._issue_start, "list_open_issues", _boom)
    try:
        r = client.get("/api/github/issues")
        assert r.status_code == 502
        assert "issues unconfigured" in r.json()["error"]
    finally:
        server._OPEN_ISSUES_CACHE.pop("v", None)


# --------------------------------------------------------------------------- #
# clear_workspaces (bulk reclaim)                                              #
# --------------------------------------------------------------------------- #
def test_clear_workspaces_removes_idle_keeps_active_and_protected(
    registered, monkeypatch, tmp_path
):
    root = tmp_path / "workspaces"
    root.mkdir()
    idle = root / "idle-ws"
    idle.mkdir()
    active = root / "active-ws"
    active.mkdir()
    base = root / "_base_repo"  # protected base clone
    base.mkdir()
    monkeypatch.setattr(
        server, "_workspace_roots", lambda: [os.path.realpath(str(root))]
    )
    monkeypatch.setattr(server, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(server, "_remove_trust_entry", lambda p: None)
    # A live session owns active-ws.
    registered("holder", wt=str(active))
    r = client.post("/api/workspaces/clear", json={})
    assert r.status_code == 200
    body = r.json()
    assert "idle-ws" in body["removed"]
    assert "holder" in body["kept_active"]
    assert not idle.exists()  # idle reclaimed
    assert active.exists()  # live session's dir left alone
    assert base.exists()  # protected base clone never bulk-deleted


# --------------------------------------------------------------------------- #
# recently-closed reopen (success path)                                        #
# --------------------------------------------------------------------------- #
def test_reopen_recreates_running_instance(monkeypatch, tmp_path):
    wt = tmp_path / "preserved"
    wt.mkdir()
    entry = {
        "id": "e1",
        "title": "revived",
        "folder": str(wt),
        "data": {"title": "revived", "worktree": {"worktree_path": str(wt)}},
    }
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [entry])
    saved_remaining = {}
    monkeypatch.setattr(
        server,
        "_save_recently_closed",
        lambda items: saved_remaining.setdefault("items", items),
    )
    monkeypatch.setattr(server, "_unique_title", lambda base: base)
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda inst, title: ("agent_" + title, None)
    )
    # Neutralize the heavy rehydration; assert the wiring, not FromInstanceData.
    from backend.session import instance as _inst_mod

    monkeypatch.setattr(
        _inst_mod, "FromInstanceData", lambda data, attach=False: _FakeInst("revived")
    )
    monkeypatch.setattr(server._events.BUS, "emit", lambda *a, **k: None)
    try:
        r = client.post("/api/recently-closed/e1/reopen")
        assert r.status_code == 200
        assert r.json()["title"] == "revived"
        assert "revived" in server.ENGINE.instances
        # The reopened entry is dropped from the recently-closed store.
        assert saved_remaining["items"] == []
    finally:
        server.ENGINE.instances.pop("revived", None)
