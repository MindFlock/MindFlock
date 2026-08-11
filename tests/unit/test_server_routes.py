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

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Loading, Status
from backend.web import server
from backend.web.core import github_pr
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


# The message of a blocked commit is offered back rather than retyped. It always
# survived on disk (it is the file `git commit -F` reads); what was missing was a
# way for the dialog to see it after a reload emptied its in-memory copy.
def _commit_msg_session(registered, monkeypatch, tmp_path, name, status, msg):
    wt = tmp_path / name
    wt.mkdir()
    if msg is not None:
        (wt / ".mindflock_commit_msg").write_text(msg)
    if status is not None:
        (wt / ".mindflock_commit_status").write_text(status)
    registered(name, wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    return client.get("/api/instances/" + name + "/commit-message")


def test_commit_message_returned_after_a_blocked_commit(
    registered, monkeypatch, tmp_path
):
    r = _commit_msg_session(
        registered, monkeypatch, tmp_path, "gt-cm1", "1", "hooks ate this\n"
    )
    assert r.status_code == 200
    assert r.json()["message"] == "hooks ate this"


def test_commit_message_withheld_after_a_successful_commit(
    registered, monkeypatch, tmp_path
):
    """A committed message is history — pre-filling the NEXT commit with it would
    be worse than an empty box."""
    r = _commit_msg_session(
        registered, monkeypatch, tmp_path, "gt-cm2", "0", "already committed\n"
    )
    assert r.status_code == 200
    assert r.json()["message"] == ""


def test_commit_message_empty_when_no_attempt_was_recorded(
    registered, monkeypatch, tmp_path
):
    """No status file at all: a stale message file from some earlier flow must not
    resurface as this commit's prefill."""
    r = _commit_msg_session(
        registered, monkeypatch, tmp_path, "gt-cm3", None, "stale\n"
    )
    assert r.status_code == 200
    assert r.json()["message"] == ""


def test_commit_message_survives_a_missing_message_file(
    registered, monkeypatch, tmp_path
):
    """A failure recorded with no message file is an empty box, not a 500 — this
    endpoint must never be what stops someone committing."""
    r = _commit_msg_session(registered, monkeypatch, tmp_path, "gt-cm4", "1", None)
    assert r.status_code == 200
    assert r.json()["message"] == ""


def test_commit_message_409_when_worktree_missing(registered, monkeypatch):
    registered("gt-cm5", wt="")
    monkeypatch.setattr(server, "git_available", lambda: True)
    r = client.get("/api/instances/gt-cm5/commit-message")
    assert r.status_code == 409


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


# --------------------------------------------------------------------------- #
# make-pr / merge-pr: gh is PREFERRED but OPTIONAL                             #
#                                                                              #
# Each behaviour is asserted BOTH ways round. "gh present" exercises the CLI   #
# rung; "gh absent" exercises REST and then the browser handoff — the path an  #
# SSH-remote contributor who never installed gh actually takes. Nothing here   #
# may 400 merely because gh is missing.                                        #
# --------------------------------------------------------------------------- #
def _use_gh(monkeypatch, present: bool) -> None:
    """Decide the gh rung without touching PATH (or running `gh auth status`)."""
    monkeypatch.setattr(server, "gh_available", lambda: present)


def _stub_gh(monkeypatch, returncode: int, out: bytes):
    """One canned `gh` result; returns the argv list every call recorded."""
    calls: list = []

    def _fake_run(args, **kw):
        calls.append(list(args))
        return SimpleNamespace(returncode=returncode, stdout=out, stderr=b"")

    monkeypatch.setattr(server, "_run_capped", _fake_run)
    return calls


def _stub_rest(
    monkeypatch,
    *,
    token: str = "ghp_test",
    origin: str = "git@github.com:o/r.git",  # SSH on purpose: the reported case
    commits=("Add a thing\n\nthe why",),
    responses=(),
):
    """Wire github_pr's four seams: auth, origin, `git log`, HTTP.

    Everything above them — the --fill semantics, status handling, the
    open/merged/closed folding — stays real code, so these tests cover the rung
    rather than aiohttp.
    """
    calls: list = []
    queued = list(responses)

    async def _token():
        return token

    async def _request(method, path, *, token, params=None, body=None):
        calls.append((method, path, params, body))
        return queued.pop(0) if queued else (0, "no stubbed response")

    monkeypatch.setattr(github_pr, "_token", _token)
    monkeypatch.setattr(github_pr, "origin_url", lambda wt: origin)
    monkeypatch.setattr(
        github_pr, "_commit_messages", lambda wt, base, head: list(commits)
    )
    monkeypatch.setattr(github_pr, "_request", _request)
    return calls


def _prime(registered, monkeypatch, tmp_path, title, *, branch="feat/x", base="main"):
    """Register a session and resolve git/base/branch for the PR routes."""
    wt = tmp_path / "ws"
    wt.mkdir()
    registered(title, wt=str(wt))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_configured_pr_base", lambda: base)
    monkeypatch.setattr(server, "_current_branch", lambda w: branch)
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    return str(wt)


def test_make_pr_uses_rest_when_gh_absent(registered, monkeypatch, tmp_path):
    # THE regression: no gh, but a token resolves -> the PR is opened over REST
    # and the route answers 200 with its URL, never "gh is not installed".
    _prime(registered, monkeypatch, tmp_path, "gt-9a")
    _use_gh(monkeypatch, False)
    url = "https://github.com/o/r/pull/7"
    calls = _stub_rest(monkeypatch, responses=[(201, {"html_url": url, "number": 7})])
    r = client.post("/api/instances/gt-9a/make-pr", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "url": url}
    method, path, _params, body = calls[0]
    assert (method, path) == ("POST", "/repos/o/r/pulls")
    # --fill parity: title is the first commit's subject, body is everything
    # else the branch's commits say (including that commit's own body).
    assert body["title"] == "Add a thing"
    assert body["body"] == "the why"
    assert body["base"] == "main" and body["head"] == "feat/x"


def test_make_pr_browser_fallback_without_gh_or_token(
    registered, monkeypatch, tmp_path
):
    # No gh AND no token: still a 200, carrying the prefilled compare page and
    # a message naming BOTH ways out. A wall here is what blocked the reporter.
    _prime(registered, monkeypatch, tmp_path, "gt-9b")
    _use_gh(monkeypatch, False)
    _stub_rest(monkeypatch, token="")
    r = client.post("/api/instances/gt-9b/make-pr", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["compare_url"] == (
        "https://github.com/o/r/compare/main...feat/x?expand=1"
    )
    assert "token" in body["message"] and "GitHub CLI" in body["message"]


@pytest.mark.parametrize("with_gh", [True, False])
def test_make_pr_409_when_on_base_branch(registered, monkeypatch, tmp_path, with_gh):
    # The self-PR guard fires BEFORE any transport is chosen, so it behaves
    # identically with and without gh — and neither rung is touched.
    _prime(registered, monkeypatch, tmp_path, "gt-10-%s" % with_gh, branch="main")
    _use_gh(monkeypatch, with_gh)
    gh_calls = _stub_gh(monkeypatch, 0, b"")
    rest_calls = _stub_rest(monkeypatch)
    r = client.post("/api/instances/gt-10-%s/make-pr" % with_gh, json={})
    assert r.status_code == 409
    assert "base branch" in r.json()["error"]
    assert gh_calls == [] and rest_calls == []


@pytest.mark.parametrize("with_gh", [True, False])
def test_make_pr_success_returns_url(registered, monkeypatch, tmp_path, with_gh):
    _prime(registered, monkeypatch, tmp_path, "gt-11-%s" % with_gh)
    _use_gh(monkeypatch, with_gh)
    url = "https://github.com/o/r/pull/7"
    if with_gh:
        _stub_gh(monkeypatch, 0, (url + "\n").encode())
    else:
        _stub_rest(monkeypatch, responses=[(201, {"html_url": url, "number": 7})])
    r = client.post("/api/instances/gt-11-%s/make-pr" % with_gh, json={"base": "main"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "url": url}


@pytest.mark.parametrize("with_gh", [True, False])
def test_make_pr_no_commits_message(registered, monkeypatch, tmp_path, with_gh):
    # gh says "no commits between", the API says "No commits between" — the
    # friendly message must not depend on which rung ran.
    _prime(registered, monkeypatch, tmp_path, "gt-12-%s" % with_gh)
    _use_gh(monkeypatch, with_gh)
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
    if with_gh:
        _stub_gh(monkeypatch, 1, b"no commits between main and feat/x")
    else:
        _stub_rest(
            monkeypatch,
            responses=[
                (
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [{"message": "No commits between main and feat/x"}],
                    },
                )
            ],
        )
    r = client.post("/api/instances/gt-12-%s/make-pr" % with_gh, json={})
    assert r.status_code == 400
    assert "nothing to PR" in r.json()["error"]


@pytest.mark.parametrize("with_gh", [True, False])
def test_make_pr_bounces_to_existing_open_pr(
    registered, monkeypatch, tmp_path, with_gh
):
    _prime(registered, monkeypatch, tmp_path, "gt-13-%s" % with_gh)
    _use_gh(monkeypatch, with_gh)
    monkeypatch.setattr(
        server,
        "_pr_info",
        lambda *a, **k: {"state": "OPEN", "url": "https://x/pull/1"},
    )
    if with_gh:
        _stub_gh(monkeypatch, 1, b"a pull request already exists")
    else:
        _stub_rest(
            monkeypatch,
            responses=[
                (
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [
                            {"message": "A pull request already exists for o:feat/x."}
                        ],
                    },
                )
            ],
        )
    r = client.post("/api/instances/gt-13-%s/make-pr" % with_gh, json={})
    assert r.status_code == 200
    assert r.json()["note"] == "PR already open"
    assert r.json()["url"] == "https://x/pull/1"


@pytest.mark.parametrize("with_gh", [True, False])
def test_merge_pr_failure_surfaces_the_refusal(
    registered, monkeypatch, tmp_path, with_gh
):
    # The repo's refusal reaches the user verbatim; WHICH tool was refused is
    # not part of the contract, so the assertion is on the message alone.
    _prime(registered, monkeypatch, tmp_path, "gt-14-%s" % with_gh)
    _use_gh(monkeypatch, with_gh)
    if with_gh:
        _stub_gh(monkeypatch, 1, b"required reviews missing")
    else:
        _stub_rest(
            monkeypatch,
            responses=[
                (200, [{"html_url": "https://x/pull/1", "state": "open", "number": 1}]),
                (405, {"message": "required reviews missing"}),
            ],
        )
    r = client.post("/api/instances/gt-14-%s/merge-pr" % with_gh)
    assert r.status_code == 400
    assert "required reviews missing" in r.json()["error"]


def test_merge_pr_links_out_without_gh_or_token(registered, monkeypatch, tmp_path):
    # No gh and no token: 200 + a link the user can merge from, not a 400.
    _prime(registered, monkeypatch, tmp_path, "gt-14c")
    _use_gh(monkeypatch, False)
    _stub_rest(monkeypatch, token="")
    r = client.post("/api/instances/gt-14c/merge-pr")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["pr_url"].startswith("https://github.com/o/r/pulls?q=")
    assert "token" in body["message"] and "GitHub CLI" in body["message"]


def test_capabilities_github_true_with_token_and_no_gh(monkeypatch):
    # The UI learns PR create/merge is possible from `caps`, not from a 400.
    monkeypatch.setattr(server, "gh_available", lambda: False)
    monkeypatch.setattr(github_pr, "has_token_sync", lambda: True)
    server._GITHUB_CAP_CACHE[0] = 0.0  # the probe is cached 60s
    assert server._capabilities()["github"] is True
    monkeypatch.setattr(github_pr, "has_token_sync", lambda: False)
    server._GITHUB_CAP_CACHE[0] = 0.0
    assert server._capabilities()["github"] is False


# --------------------------------------------------------------------------- #
# github_pr: the gh-free client itself                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:o/r.git",  # scp-style SSH — the reported contributor's
        "ssh://git@github.com:22/o/r.git",
        "https://github.com/o/r.git",
    ],
)
def test_repo_ref_is_transport_independent(monkeypatch, origin):
    # Every spelling of the same remote resolves to the same API slug, so the
    # REST rung works without ever rewriting the user's remote.
    monkeypatch.setattr(github_pr, "origin_url", lambda wt: origin)
    assert github_pr.repo_ref("/ws").slug == "o/r"


def test_repo_ref_none_for_a_local_clone(monkeypatch):
    # Provisioning clones from the user's own checkout: a local path is a
    # normal remote, just one with no forge to call.
    monkeypatch.setattr(github_pr, "origin_url", lambda wt: "/home/me/app")
    assert github_pr.repo_ref("/ws") is None


def test_find_pr_folds_a_merged_pr_into_ghs_vocabulary(monkeypatch):
    # REST reports a merged PR as state "closed" + merged_at; the stage machine
    # only understands gh's OPEN/MERGED/CLOSED, so the fold must happen here.
    _stub_rest(
        monkeypatch,
        responses=[
            (
                200,
                [
                    {
                        "html_url": "https://github.com/o/r/pull/4",
                        "state": "closed",
                        "merged_at": "2026-07-30T00:00:00Z",
                        "number": 4,
                    }
                ],
            )
        ],
    )
    got = asyncio.run(github_pr.find_pr("/ws", "feat/x"))
    assert got == {
        "url": "https://github.com/o/r/pull/4",
        "state": "MERGED",
        "number": 4,
    }


def test_create_pr_without_commits_reports_no_commits(monkeypatch):
    # An empty branch never reaches the API: the message is pre-shaped so the
    # route's "nothing to PR" mapping fires on it.
    calls = _stub_rest(monkeypatch, commits=())
    res = asyncio.run(github_pr.create_pr("/ws", "main", "feat/x"))
    assert res.ok is False and res.unavailable is False
    assert "No commits between main and feat/x" in res.error
    assert calls == []


def test_client_degrades_instead_of_raising_when_offline(monkeypatch):
    # A dead network is a routine outcome for these routes, not a 500.
    _stub_rest(monkeypatch, responses=[(0, "connection refused")])
    res = asyncio.run(github_pr.create_pr("/ws", "main", "feat/x"))
    assert res.ok is False and res.unavailable is False
    assert "api.github.com" in res.error
    _stub_rest(monkeypatch, responses=[(0, "connection refused")])
    assert asyncio.run(github_pr.find_pr("/ws", "feat/x")) is None


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
    seen = []
    monkeypatch.setattr(
        server,
        "_agent_transcript_text",
        lambda wt, name="": (seen.append((wt, name)), "recovered transcript")[1],
    )
    # Agent pane, session gone -> the on-disk transcript is served instead of 404.
    r = client.get("/api/instances/ph-2/history")
    assert r.status_code == 200
    assert r.text == "recovered transcript"
    # Looked up by THIS window's tmux session, not by the worktree alone —
    # siblings sharing a directory each have their own conversation.
    assert seen == [("/tmp/x", server.tmux.to_mindflock_tmux_name("ph-2"))]


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
    server._pending_add("sc-99", "tix")  # a start already in flight
    monkeypatch.setattr(
        server._ticket_start,
        "list_assigned_tickets",
        lambda: _aval({"tickets": [{"id": "sc-99", "session": "sc-99"}]}),
    )
    try:
        r = client.get("/api/tickets")
        assert r.status_code == 200
        # a ticket whose start is in-flight (in core.pending) reads as claimed
        assert r.json()["tickets"][0]["has_session"] is True
    finally:
        server._ASSIGNED_TICKETS_CACHE.pop("v", None)
        server._pending_drop("sc-99")


def test_config_reports_the_default_program_as_a_provider_name(monkeypatch):
    """A config.toml written by an older first run stores `which claude` output.
    Served verbatim, the New Session dialog didn't recognise it and listed it as
    an extra agent-dropdown entry — a mystery "/opt/homebrew/bin/claude" above
    the real agents on a Homebrew Mac. Normalized on the way out, so an existing
    install is fixed without editing any file."""
    monkeypatch.setattr(
        server.ENGINE, "default_program", lambda: "/opt/homebrew/bin/claude"
    )
    assert client.get("/api/config").json()["default_program"] == "claude"


def test_config_keeps_a_custom_program_verbatim(monkeypatch):
    """A program no provider claims is the launch command itself — untouched."""
    monkeypatch.setattr(
        server.ENGINE, "default_program", lambda: "/opt/bin/my-own-agent"
    )
    assert (
        client.get("/api/config").json()["default_program"] == "/opt/bin/my-own-agent"
    )


def test_pending_start_shows_as_a_provisioning_row():
    """An accepted force-start is visible in the sidebar before its session
    exists — the whole point of core.pending (a PR clone runs far longer than
    the instances poll, and an empty sidebar reads as a failed start)."""
    server._pending_add("pr-app-42", "pr", branch="fix/login-crash", repo="o/app")
    try:
        rows = client.get("/api/instances").json()
        row = next(r for r in rows if r["title"] == "pr-app-42")
        assert row["pending"] is True
        assert row["status"] == "loading"
        assert row["stage"] == "provisioning"
        # The branch rides along so the row can name the work, not just the slug.
        assert row["branch"] == "fix/login-crash"
    finally:
        server._pending_drop("pr-app-42")
    assert not [
        r for r in client.get("/api/instances").json() if r["title"] == "pr-app-42"
    ]


def test_pending_row_yields_to_the_real_session(monkeypatch):
    """Once the instance registers, the real entry is the only one — a stale
    pending marker must not double the row."""
    monkeypatch.setitem(server.ENGINE.instances, "sc-77", _FakeInst("sc-77"))
    server._pending_add("sc-77", "tix", branch="feature/sc-77/x")
    try:
        titles = [r["title"] for r in client.get("/api/instances").json()]
        assert titles.count("sc-77") == 1
    finally:
        server._pending_drop("sc-77")


def test_force_review_row_appears_before_the_github_lookup(monkeypatch):
    """The provisioning row must not wait on find_pr: the panel's cached list
    already carries the session title, so the row goes up first and the lookup
    (a GitHub round trip — the last click-to-row delay) happens after."""
    server._OPEN_PRS_CACHE["v"] = (
        time.monotonic() + 60,
        {"prs": [{"repo": "o/app", "number": 42, "session": "pr-app-42"}]},
    )
    seen: list = []

    async def _slow_find(repo, number):
        # Whatever the row state is at lookup time is what the user sees while
        # waiting on GitHub.
        seen.append(
            [
                r["title"]
                for r in client.get("/api/instances").json()
                if r.get("pending")
            ]
        )
        raise LookupError("no such PR")

    monkeypatch.setattr(server._pr_review, "find_pr", _slow_find)
    monkeypatch.setattr(server, "git_available", lambda: True)
    try:
        r = client.post("/api/github/prs/review", json={"repo": "o/app", "number": 42})
        assert r.status_code == 404  # the lookup failed, as staged
        assert seen == [["pr-app-42"]]  # ...but the row was already up
        # A failed lookup takes its row back down again.
        assert not [x for x in client.get("/api/instances").json() if x.get("pending")]
    finally:
        server._OPEN_PRS_CACHE.pop("v", None)
        server._pending_drop("pr-app-42")


def test_force_review_409s_from_the_cached_title(monkeypatch):
    """The double-click guard works off the early title too — otherwise the
    second click would sail past it and provision the same workspace twice."""
    server._OPEN_PRS_CACHE["v"] = (
        time.monotonic() + 60,
        {"prs": [{"repo": "o/app", "number": 7, "session": "pr-app-7"}]},
    )
    server._pending_add("pr-app-7", "pr")
    monkeypatch.setattr(server, "git_available", lambda: True)
    try:
        r = client.post("/api/github/prs/review", json={"repo": "o/app", "number": 7})
        assert r.status_code == 409
        assert r.json()["title"] == "pr-app-7"
    finally:
        server._OPEN_PRS_CACHE.pop("v", None)
        server._pending_drop("pr-app-7")


def test_ingestion_status_greens_for_a_locally_forced_start():
    """The pipeline's beacon only knows the pipeline's own queue, so a ticket
    forced from the UI has to green the dot through core.pending — otherwise the
    light reads idle for the whole provisioning (what it did before)."""
    r = client.get("/api/mindflock/status")
    assert r.status_code == 200
    assert r.json()["tickets_active"] is False
    server._pending_add("sc-5", "tix")
    try:
        assert client.get("/api/mindflock/status").json()["tickets_active"] is True
        # The other two dots are unaffected by a ticket start.
        assert client.get("/api/mindflock/status").json()["pr_active"] is False
    finally:
        server._pending_drop("sc-5")
    assert client.get("/api/mindflock/status").json()["tickets_active"] is False


def test_provisioning_kinds_classifies_a_loading_session(monkeypatch):
    """A session that exists but is still cloning counts as active work too —
    the pending marker is dropped the moment the instance registers, so without
    this the light would flick back to gold mid-provisioning."""
    from backend.web.core import pending

    inst = _FakeInst("sc-9", started=False, status=Loading)
    inst.Branch = "feature/sc-9/add-thing"
    monkeypatch.setitem(server.ENGINE.instances, "sc-9", inst)
    assert "tix" in pending.provisioning_kinds()
    # A running session is not "being brought in" — that's the idle state.
    inst._started = True
    assert "tix" not in pending.provisioning_kinds()


def test_session_kind_matches_the_sidebar_label():
    """Backend kind classification and the frontend's sessionLabel must agree
    on what a PR / issue / ticket session looks like."""
    from backend.web.core import pending

    assert pending.session_kind("pr-app-42", "fix/x") == "pr"
    assert pending.session_kind("issue-app-77", "feature/issue-app-77/x") == "iss"
    assert pending.session_kind("sc-12345", "feature/sc-12345/add-dark-mode") == "tix"
    assert pending.session_kind("my-refactor", "feature/my-refactor") == ""


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


# --------------------------------------------------------------------------- #
# _cached_fanout — the settings panels' stale-while-revalidate cache           #
# --------------------------------------------------------------------------- #
# The tickets / open-PRs / open-issues panels each fan out to a slow upstream
# (measured at ~3s for the ticket sources). They unmount when the settings
# dialog closes, so every visit used to pay that sweep; these tests pin the
# behaviour that keeps the wait off the request path.
#
# The window boundaries are driven through an explicit ``ttl`` / ``max_stale``
# plus a hand-cranked clock rather than the module defaults, so a change to
# _FANOUT_TTL / _FANOUT_MAX_STALE fails the constant's own test instead of
# silently sliding every window test with it.
_PANELS = [
    ("/api/github/prs", "_OPEN_PRS_CACHE", "_pr_review", "list_open_prs", "prs"),
    (
        "/api/tickets",
        "_ASSIGNED_TICKETS_CACHE",
        "_ticket_start",
        "list_assigned_tickets",
        "tickets",
    ),
    (
        "/api/github/issues",
        "_OPEN_ISSUES_CACHE",
        "_issue_start",
        "list_open_issues",
        "issues",
    ),
]


@pytest.fixture(autouse=True)
def _clear_fanout_caches():
    """Never let one test's cached payload (or in-flight sweep) reach another.

    All three panel caches are module-level dicts, and a stale entry left
    behind changes whether the *next* test's route call sweeps at all — the
    kind of ordering dependency that only shows up under ``-p no:randomly`` or
    a single-file run. Also drops any leaked ``"task"``: a pending refresh
    would otherwise keep writing into a cache a later test is reading.
    """
    caches = (
        server._OPEN_PRS_CACHE,
        server._ASSIGNED_TICKETS_CACHE,
        server._OPEN_ISSUES_CACHE,
    )

    def _clear():
        for cache in caches:
            cache.pop("v", None)
            task = cache.pop("task", None)
            if task is not None and not task.done():
                try:
                    task.cancel()
                except RuntimeError:  # its event loop is already closed
                    pass

    _clear()
    yield
    _clear()


class _CapLog:
    """A stand-in for ``log.ErrorLog`` so the failure branches are assertable."""

    def __init__(self):
        self.msgs: list = []

    def Printf(self, fmt, *args):  # noqa: N802 — Go-style logger API
        # The loggers use Go verbs (%v) that Python's % rejects; keep the args
        # alongside the format so substring assertions still work.
        self.msgs.append(fmt + " " + " ".join(str(a) for a in args))

    def Print(self, *args):  # noqa: N802
        self.msgs.append(" ".join(str(a) for a in args))

    def Println(self, *args):  # noqa: N802
        self.msgs.append(" ".join(str(a) for a in args))


@pytest.fixture
def caplog_errorlog(monkeypatch):
    cap = _CapLog()
    monkeypatch.setattr(server.log, "ErrorLog", cap)
    return cap


class _FakeClock:
    """A hand-cranked ``time.monotonic`` for exact window boundaries."""

    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def fanout_clock(monkeypatch):
    """Freeze ``time.monotonic`` so "exactly at the expiry" is testable.

    Relative offsets can only probe *near* a boundary; the comparisons here are
    strict (``cached[0] > now``), so the equal case needs a stopped clock.
    """
    clock = _FakeClock()
    monkeypatch.setattr(server.time, "monotonic", clock)
    return clock


async def test_cached_fanout_fresh_hit_skips_the_loader():
    cache = {"v": (time.monotonic() + 10.0, {"prs": ["cached"]})}

    async def _loader():
        raise AssertionError("a fresh entry must not hit the upstream")

    data, stale = await server._cached_fanout(cache, _loader)
    assert data == {"prs": ["cached"]}
    assert stale is False


async def test_cached_fanout_serves_stale_then_refreshes_behind_it():
    # Just past the TTL: still inside the stale window.
    cache = {"v": (time.monotonic() - 1.0, {"prs": ["old"]})}
    calls = []

    async def _loader():
        calls.append(1)
        return {"prs": ["new"]}

    data, stale = await server._cached_fanout(cache, _loader)
    # The request returns the old list rather than waiting for the sweep.
    assert data == {"prs": ["old"]}
    assert stale is True
    await cache["task"]  # let the background refresh land
    assert cache["v"][1] == {"prs": ["new"]}
    assert len(calls) == 1
    # The slot is released on success, so the panel can sweep again later.
    assert "task" not in cache


async def test_cached_fanout_stale_read_returns_before_the_loader_finishes():
    """The whole point: a stale hit must not be blocked by the sweep it starts."""
    cache = {"v": (time.monotonic() - 1.0, {"prs": ["old"]})}
    release = asyncio.Event()

    async def _loader():
        await release.wait()  # a slow ls-remote / GitHub call
        return {"prs": ["new"]}

    data, stale = await server._cached_fanout(cache, _loader)
    assert (data, stale) == ({"prs": ["old"]}, True)
    assert not cache["task"].done()  # returned with the sweep still running
    release.set()
    await cache["task"]
    assert cache["v"][1] == {"prs": ["new"]}


async def test_cached_fanout_waits_when_past_the_stale_window():
    cache = {"v": (time.monotonic() - 10_000.0, {"prs": ["ancient"]})}

    async def _loader():
        return {"prs": ["new"]}

    data, stale = await server._cached_fanout(cache, _loader)
    # Too old to show: this request waits for real data instead.
    assert data == {"prs": ["new"]}
    assert stale is False
    assert "task" not in cache


async def test_cached_fanout_at_the_ttl_expiry_is_stale_not_fresh(fanout_clock):
    """The TTL boundary itself: expiry reached = serve stale + sweep behind it."""

    async def _loader():
        return {"prs": ["new"]}

    # A hair before expiry is still a fresh hit…
    cache = {"v": (fanout_clock.now + 0.001, {"prs": ["old"]})}
    assert await server._cached_fanout(cache, _loader, ttl=20.0, max_stale=300.0) == (
        {"prs": ["old"]},
        False,
    )
    assert "task" not in cache
    # …and exactly at it the entry has flipped to stale-but-servable.
    cache = {"v": (fanout_clock.now, {"prs": ["old"]})}
    assert await server._cached_fanout(cache, _loader, ttl=20.0, max_stale=300.0) == (
        {"prs": ["old"]},
        True,
    )
    await cache["task"]


async def test_cached_fanout_at_the_stale_window_edge_waits_for_the_loader(
    fanout_clock,
):
    """The far boundary: once ``max_stale`` is used up, the payload is not shown."""

    async def _loader():
        return {"prs": ["new"]}

    # One tick inside the window: the old list is still worth showing.
    cache = {"v": (fanout_clock.now - 300.0 + 0.001, {"prs": ["old"]})}
    assert await server._cached_fanout(cache, _loader, ttl=20.0, max_stale=300.0) == (
        {"prs": ["old"]},
        True,
    )
    await cache["task"]
    # Exactly at the edge: wait for a real sweep rather than show it.
    cache = {"v": (fanout_clock.now - 300.0, {"prs": ["old"]})}
    assert await server._cached_fanout(cache, _loader, ttl=20.0, max_stale=300.0) == (
        {"prs": ["new"]},
        False,
    )
    assert "task" not in cache


async def test_fanout_window_constants_match_the_documented_contract():
    """The windows the client (and docs) are written against: 20s / 5min."""
    assert server._FANOUT_TTL == 20.0
    assert server._FANOUT_MAX_STALE == 300.0


async def test_cached_fanout_fresh_flag_bypasses_a_valid_entry(fanout_clock):
    cache = {"v": (fanout_clock.now + 10.0, {"prs": ["cached"]})}
    calls = []

    async def _loader():
        calls.append(1)
        return {"prs": ["swept"]}

    data, stale = await server._cached_fanout(cache, _loader, fresh=True, ttl=20.0)
    assert data == {"prs": ["swept"]}  # the Refresh button means what it says
    assert stale is False
    # The sweep repopulates the cache, so the poll right behind the Refresh
    # click is a fresh hit instead of a second trip upstream.
    assert cache["v"] == (fanout_clock.now + 20.0, {"prs": ["swept"]})
    assert await server._cached_fanout(cache, _loader, ttl=20.0) == (
        {"prs": ["swept"]},
        False,
    )
    assert len(calls) == 1


async def test_cached_fanout_keeps_stale_data_when_the_refresh_fails(caplog_errorlog):
    cache = {"v": (time.monotonic() - 1.0, {"prs": ["old"]})}

    async def _loader():
        raise RuntimeError("github down")

    data, stale = await server._cached_fanout(cache, _loader)
    assert data == {"prs": ["old"]}
    assert stale is True
    task = cache["task"]
    await task  # the failure is swallowed, not raised at the awaiting caller
    assert task.exception() is None
    # A failed sweep must not empty the panel or poison the cache…
    assert cache["v"][1] == {"prs": ["old"]}
    # …but it is not silent either, and it releases the single-flight slot so
    # the panel isn't wedged until a restart.
    assert any("background fan-out refresh failed" in m for m in caplog_errorlog.msgs)
    assert any("github down" in m for m in caplog_errorlog.msgs)
    assert "task" not in cache


async def test_cached_fanout_refresh_is_single_flight():
    cache = {"v": (time.monotonic() - 1.0, {"prs": ["old"]})}
    calls = []
    release = asyncio.Event()

    async def _loader():
        calls.append(1)
        await release.wait()
        return {"prs": ["new"]}

    await server._cached_fanout(cache, _loader)
    task = cache["task"]
    # A second read while the refresh is in flight must not start another.
    await server._cached_fanout(cache, _loader)
    assert cache["task"] is task
    release.set()
    await task
    assert len(calls) == 1


async def test_cached_fanout_backs_off_after_a_failed_sweep(fanout_clock):
    """A failing upstream must not be polled harder than a healthy one.

    The client returns every 2s for as long as the server answers ``stale``,
    and the entry stays stale precisely because the sweeps keep failing — so
    without a backoff each of those polls would arm another sweep against the
    thing that is already down, for the whole five-minute stale window.
    """
    cache = {"v": (fanout_clock.now - 1.0, {"prs": ["old"]})}
    calls = []

    async def _loader():
        calls.append(1)
        raise RuntimeError("still down")  # keeps the entry stale

    await server._cached_fanout(cache, _loader)
    await cache["task"]
    assert len(calls) == 1

    # Right after the failure: still honestly stale, but no new sweep armed.
    data, stale = await server._cached_fanout(cache, _loader)
    assert (data, stale) == ({"prs": ["old"]}, True)
    assert "task" not in cache
    assert len(calls) == 1

    # Once the backoff has elapsed, exactly one more sweep is attempted.
    fanout_clock.advance(server._FANOUT_ERROR_BACKOFF + 1.0)
    await server._cached_fanout(cache, _loader)
    await cache["task"]
    assert len(calls) == 2


async def test_cached_fanout_sweeps_again_once_a_good_one_lands(fanout_clock):
    """Single-flight is per sweep, not per stale window: a successful refresh
    clears the backoff so the panel keeps updating normally."""
    cache = {"v": (fanout_clock.now - 1.0, {"prs": ["old"]})}
    cache["retry_after"] = fanout_clock.now - 1.0  # a previous failure, expired

    async def _loader():
        return {"prs": ["new"]}

    await server._cached_fanout(cache, _loader)
    await cache["task"]
    assert cache["v"][1] == {"prs": ["new"]}
    assert "retry_after" not in cache


async def test_cached_fanout_hung_sweep_frees_the_slot(monkeypatch):
    """A sweep that never returns must not wedge the panel.

    Single-flight means one stuck ls-remote would otherwise block every later
    refresh for that panel — the list would freeze at whatever it last had.
    """
    monkeypatch.setattr(server, "_FANOUT_SWEEP_TIMEOUT", 0.01)
    cache = {"v": (time.monotonic() - 1.0, {"prs": ["old"]})}

    async def _loader():
        await asyncio.sleep(30)  # never finishes within the timeout
        return {"prs": ["never"]}

    data, stale = await server._cached_fanout(cache, _loader)
    assert (data, stale) == ({"prs": ["old"]}, True)
    await cache["task"]
    assert "task" not in cache  # the slot is free for the next attempt
    assert cache["v"][1] == {"prs": ["old"]}  # and the last good list survives


async def test_schedule_fanout_refresh_ignores_a_second_caller_in_flight():
    """Two requests racing on the same panel share one sweep, whoever asks."""
    cache = {"v": (time.monotonic() - 1.0, {"prs": ["old"]})}
    release = asyncio.Event()
    first_calls, second_calls = [], []

    async def _first():
        first_calls.append(1)
        await release.wait()
        return {"prs": ["first"]}

    async def _second():
        second_calls.append(1)
        return {"prs": ["second"]}

    server._schedule_fanout_refresh(cache, _first, 20.0)
    task = cache["task"]
    await asyncio.sleep(0)  # let the first sweep get as far as the loader
    server._schedule_fanout_refresh(cache, _second, 20.0)
    assert cache["task"] is task
    release.set()
    await task
    assert first_calls == [1]
    assert second_calls == []  # the second loader never ran
    assert cache["v"][1] == {"prs": ["first"]}


# --------------------------------------------------------------------------- #
# the three panel routes over that cache (?fresh + the "stale" flag)           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,cache_name,mod,loader,key",
    _PANELS,
    ids=[p[0] for p in _PANELS],
)
def test_settings_panel_fresh_hit_answers_with_stale_false(
    monkeypatch, path, cache_name, mod, loader, key
):
    """Every panel payload carries ``stale`` — the client's poll decision."""
    cache = getattr(server, cache_name)
    cache["v"] = (time.monotonic() + 60.0, {key: [{"id": "cached"}]})
    monkeypatch.setattr(
        getattr(server, mod),
        loader,
        lambda: _aval({key: [{"id": "swept"}]}),
    )
    r = client.get(path)
    assert r.status_code == 200
    assert r.json()["stale"] is False
    assert [row["id"] for row in r.json()[key]] == ["cached"]


@pytest.mark.parametrize(
    "path,cache_name,mod,loader,key",
    _PANELS,
    ids=[p[0] for p in _PANELS],
)
def test_settings_panel_serves_a_stale_payload_marked_stale(
    monkeypatch, path, cache_name, mod, loader, key
):
    """Past the TTL the panel answers from the stale entry and sweeps behind it."""
    cache = getattr(server, cache_name)
    cache["v"] = (time.monotonic() - 1.0, {key: [{"id": "old"}]})
    calls = []

    async def _load():
        calls.append(1)
        return {key: [{"id": "swept"}]}

    monkeypatch.setattr(getattr(server, mod), loader, _load)
    r = client.get(path)
    assert r.status_code == 200
    assert r.json()["stale"] is True  # tells the client to come back for more
    assert [row["id"] for row in r.json()[key]] == ["old"]
    assert calls == [1]  # …and the refresh did run, off the request path
    assert cache["v"][1] == {key: [{"id": "swept"}]}


@pytest.mark.parametrize(
    "path,cache_name,mod,loader,key",
    _PANELS,
    ids=[p[0] for p in _PANELS],
)
def test_settings_panel_fresh_param_forces_a_sweep(
    monkeypatch, path, cache_name, mod, loader, key
):
    cache = getattr(server, cache_name)
    cache["v"] = (time.monotonic() + 60.0, {key: [{"id": "cached"}]})
    monkeypatch.setattr(
        getattr(server, mod), loader, lambda: _aval({key: [{"id": "swept"}]})
    )
    # Without ?fresh the fresh cache entry answers…
    assert [row["id"] for row in client.get(path).json()[key]] == ["cached"]
    # …and with it the handler goes back upstream.
    r = client.get(path + "?fresh=1")
    assert [row["id"] for row in r.json()[key]] == ["swept"]
    assert r.json()["stale"] is False


@pytest.mark.parametrize(
    "path,cache_name,mod,loader,key",
    _PANELS,
    ids=[p[0] for p in _PANELS],
)
def test_settings_panel_serves_stale_instead_of_502_when_upstream_is_down(
    monkeypatch, path, cache_name, mod, loader, key
):
    """A usable stale entry outranks an upstream outage: no 502, no empty panel."""
    cache = getattr(server, cache_name)
    cache["v"] = (time.monotonic() - 1.0, {key: [{"id": "old"}]})

    async def _boom():
        raise RuntimeError("upstream down")

    monkeypatch.setattr(getattr(server, mod), loader, _boom)
    r = client.get(path)
    assert r.status_code == 200
    assert r.json()["stale"] is True
    assert [row["id"] for row in r.json()[key]] == ["old"]
    # The failed sweep left the last known list in place to serve again.
    assert cache["v"][1] == {key: [{"id": "old"}]}


@pytest.mark.parametrize(
    "path,cache_name,mod,loader,key",
    _PANELS,
    ids=[p[0] for p in _PANELS],
)
def test_settings_panel_fresh_sweep_502s_but_keeps_the_good_payload(
    monkeypatch, path, cache_name, mod, loader, key
):
    """An explicit Refresh reports the failure — without discarding what we have."""
    cache = getattr(server, cache_name)
    good = {key: [{"id": "cached"}]}
    cache["v"] = (time.monotonic() + 60.0, good)

    async def _boom():
        raise RuntimeError("no gh token")

    monkeypatch.setattr(getattr(server, mod), loader, _boom)
    r = client.get(path + "?fresh=1")
    assert r.status_code == 502
    assert "no gh token" in r.json()["error"]
    assert cache["v"][1] == good  # the panel still has rows to show
    assert client.get(path).json()[key][0]["id"] == "cached"


def test_github_open_prs_stale_hit_annotates_without_touching_the_cache(
    registered, monkeypatch
):
    """``has_session`` is per-request even off a stale entry, and the annotated
    copy must not be written back — a cached payload carrying a stale
    ``has_session``/``stale`` would show dead sessions as live."""
    cached = {"prs": [{"number": 3, "session": "owner/repo#3"}]}
    server._OPEN_PRS_CACHE["v"] = (time.monotonic() - 1.0, cached)
    # Freeze the sweep so the served payload is unambiguously the stale one.
    monkeypatch.setattr(server, "_schedule_fanout_refresh", lambda *a, **k: None)

    first = client.get("/api/github/prs").json()
    assert first["stale"] is True
    assert first["prs"][0]["has_session"] is False  # no session yet

    registered("owner/repo#3", wt="/tmp/x")  # session appears mid-stale-window
    second = client.get("/api/github/prs").json()
    assert second["prs"][0]["has_session"] is True  # re-annotated, not remembered
    # The cached payload itself stayed clean.
    assert server._OPEN_PRS_CACHE["v"][1] == {
        "prs": [{"number": 3, "session": "owner/repo#3"}]
    }
    assert "stale" not in server._OPEN_PRS_CACHE["v"][1]
