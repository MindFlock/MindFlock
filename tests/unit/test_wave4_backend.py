"""Wave-4 backend (roadmap section K + J3/J5 backend halves).

Covers:
  * K1 — per-session base branch: recorded on InstanceData at creation
    (dual-read: absent for old sessions), resolved at read time through the
    fallback chain (stored -> origin/HEAD -> main/master probe -> configured
    base only when the repo matches), and used by stage detection so a commit
    on a plain repo reaches the "committed" stage instead of snapping back to
    "agent" under the global config's base.
  * K2 — `_repo_name` prefers the session's own provision_repo/workspace path
    over the globally configured repo's name.
  * K3 — `_parse_failed_step` falls back to the raw hook's last output line
    when there are no pre-commit-framework ``name....Failed`` lines.
  * K4 — `/api/workspaces/delete` allows deleting an UNREFERENCED base clone
    and refuses (naming the holders) when sessions/worktrees still use it.
  * J3 — `diff_stat` on the instance JSON: shape, totals (committed-beyond-base
    + uncommitted), ~10s cache, null when unavailable.
  * J5 — `session.budget_exceeded` emitted once per session per budget value.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.config import settings as settings_store
from backend.session import provisioned
from backend.session.storage import InstanceData, Status
from backend.web import server
from backend.web.core import events as events_mod
from starlette.testclient import TestClient


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: str) -> str:
    cp = subprocess.run(
        ["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-q", "-m", "init", cwd=str(path))
    return path


class _FakeWorktree:
    def __init__(self, repo_path: str = "", base_sha: str = ""):
        self._repo_path = repo_path
        self._base_sha = base_sha

    def GetRepoPath(self):  # noqa: N802
        return self._repo_path

    def GetWorktreePath(self):  # noqa: N802
        return self._repo_path

    def GetBaseCommitSHA(self):  # noqa: N802
        return self._base_sha


class _FakeInst:
    Program = "bash"
    Path = ""

    def __init__(
        self,
        wt: str,
        *,
        base_branch: str = "",
        branch: str = "",
        started: bool = True,
        status: Status = Status.Running,
        repo_path: str = "",
        title: str = "t",
    ):
        self.Title = title
        self.Branch = branch
        self.BaseBranch = base_branch
        self.Status = status
        self._wt = wt
        self._started = started
        self._repo_path = repo_path or wt

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return self._wt

    def GetGitWorktree(self):  # noqa: N802
        return _FakeWorktree(self._repo_path)


@pytest.fixture(autouse=True)
def _clear_server_caches():
    server._BASE_BRANCH_CACHE.clear()
    server._DIFF_STAT_CACHE.clear()
    server._BUDGET_FIRED.clear()
    yield
    server._BASE_BRANCH_CACHE.clear()
    server._DIFF_STAT_CACHE.clear()
    server._BUDGET_FIRED.clear()


# --------------------------------------------------------------------------- #
# K1 — InstanceData round-trip (dual-read)
# --------------------------------------------------------------------------- #
def test_instance_data_base_branch_roundtrip():
    d = InstanceData(title="t", base_branch="main").to_dict()
    assert d["base_branch"] == "main"
    assert InstanceData.from_dict(d).base_branch == "main"


def test_instance_data_base_branch_absent_for_old_entries():
    # Old state.json entries (no key) parse to "" — and an unset field is not
    # emitted, keeping ordinary sessions byte-compatible with the Go wire form.
    assert InstanceData.from_dict({"title": "x"}).base_branch == ""
    assert "base_branch" not in InstanceData(title="t").to_dict()


def test_from_instance_data_restores_base_branch(tmp_path):
    from backend.session.instance import FromInstanceData

    repo = _init_repo(tmp_path / "r")
    data = InstanceData.from_dict(
        {
            "title": "t-restore",
            "path": str(repo),
            "status": int(Status.Paused),
            "base_branch": "main",
            "worktree": {"repo_path": str(repo), "worktree_path": str(repo)},
        }
    )
    inst = FromInstanceData(data, attach=False)
    assert inst.BaseBranch == "main"


# --------------------------------------------------------------------------- #
# K1 — per-session base resolution (fallback chain)
# --------------------------------------------------------------------------- #
def test_base_branch_prefers_stored_value(tmp_path):
    inst = _FakeInst(str(tmp_path), base_branch="dev-trunk")
    assert server._session_base_branch(inst) == "dev-trunk"


def test_base_branch_fallback_origin_head(tmp_path):
    # A clone records origin/HEAD -> the source's default branch wins even
    # when it's neither main nor master.
    src = _init_repo(tmp_path / "src", branch="trunk")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(src), str(clone)], check=True)
    inst = _FakeInst(str(clone))
    assert server._session_base_branch(inst) == "trunk"


def test_base_branch_fallback_main_master_probe(tmp_path):
    repo = _init_repo(tmp_path / "m", branch="master")  # no origin at all
    inst = _FakeInst(str(repo))
    assert server._session_base_branch(inst) == "master"


def test_base_branch_configured_only_when_repo_matches(tmp_path, monkeypatch):
    # No origin/HEAD, no main/master -> the configured base applies ONLY to
    # the configured repo; any other repo gets the neutral default.
    src = _init_repo(tmp_path / "cfg-src", branch="trunk")
    matching = _init_repo(tmp_path / "match", branch="trunk")
    _git("remote", "add", "origin", str(src), cwd=str(matching))
    other = _init_repo(tmp_path / "other", branch="trunk")
    _git("remote", "add", "origin", str(tmp_path / "elsewhere"), cwd=str(other))

    cfg = provisioned.ProvisionSettings(
        repo_url=str(src), workspace_dir=tmp_path / "ws", base_branch="staging"
    )
    monkeypatch.setattr(server.provisioning, "load_provision_settings", lambda **k: cfg)
    assert server._session_base_branch(_FakeInst(str(matching))) == "staging"
    assert server._session_base_branch(_FakeInst(str(other))) == "main"


def test_base_branch_fallback_is_cached(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "c", branch="master")
    inst = _FakeInst(str(repo))
    assert server._session_base_branch(inst) == "master"
    # A cached worktree never re-runs the git probes within the TTL.
    monkeypatch.setattr(
        server,
        "_resolve_fallback_base_branch",
        lambda wt: (_ for _ in ()).throw(AssertionError("probe re-ran")),
    )
    assert server._session_base_branch(inst) == "master"


# --------------------------------------------------------------------------- #
# K1 — stage detection uses the per-session base (the round's blocker)
# --------------------------------------------------------------------------- #
def _worktree_session(tmp_path):
    """A plain scratch repo (base `main`) + a session worktree off it."""
    src = _init_repo(tmp_path / "scratch", branch="main")
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "feat/x", str(wt), cwd=str(src))
    return src, wt


def test_stage_reaches_committed_on_plain_repo(tmp_path, monkeypatch):
    """Commit on a plain repo (base main) -> stage 'committed', even while the
    GLOBAL provision settings point at an unrelated repo with base 'staging'
    (the exact K1 failure: the guided flow used to snap back to 'agent')."""
    src, wt = _worktree_session(tmp_path)
    cfg = provisioned.ProvisionSettings(
        repo_url="git@github.com:org/example-bot.git",
        workspace_dir=tmp_path / "ws",
        base_branch="staging",
    )
    monkeypatch.setattr(server.provisioning, "load_provision_settings", lambda **k: cfg)
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
    inst = _FakeInst(str(wt), base_branch="main", branch="feat/x")

    # Dirty tree -> agent (next: Commit).
    (wt / "b.txt").write_text("hello\n")
    assert server._session_stage(inst)["stage"] == "agent"

    # Committed beyond main, not on origin -> committed (next: Push).
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "work", cwd=str(wt))
    assert server._session_stage(inst)["stage"] == "committed"


def test_stage_committed_for_pre_k1_session_via_fallback(tmp_path, monkeypatch):
    """An old session (no stored base_branch) resolves main via the probe and
    still reaches 'committed'."""
    src, wt = _worktree_session(tmp_path)
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
    inst = _FakeInst(str(wt), base_branch="", branch="feat/x")
    (wt / "b.txt").write_text("hello\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "work", cwd=str(wt))
    assert server._session_stage(inst)["stage"] == "committed"


# --------------------------------------------------------------------------- #
# K2 — repo name prefers the session's own repo
# --------------------------------------------------------------------------- #
def test_repo_name_prefers_provision_repo(tmp_path):
    inst = _FakeInst(str(tmp_path), started=False)
    inst.Provisioned = True
    inst._provision_repo = str(tmp_path / "projects" / "my-local-repo")
    assert server._repo_name(inst) == "my-local-repo"


def test_repo_name_prefers_workspace_path_when_set(tmp_path):
    inst = _FakeInst(str(tmp_path), started=False)
    inst.Provisioned = True
    inst._provision_repo = ""
    inst._workspace_path = str(tmp_path / "ws" / "pr-42")
    assert server._repo_name(inst) == "pr-42"


def test_repo_name_configured_repo_unchanged(tmp_path, monkeypatch):
    class _S:
        repo_url = "git@github.com:org/example-bot.git"

    monkeypatch.setattr(server.provisioning, "settings_for_workspace", lambda p: _S())
    inst = _FakeInst(str(tmp_path), started=True)
    inst.Provisioned = True
    assert server._repo_name(inst) == "example-bot"


# --------------------------------------------------------------------------- #
# K3 — failed-step parsing (framework names + raw-hook fallback)
# --------------------------------------------------------------------------- #
def test_parse_failed_step_framework_lines():
    text = (
        "black................................................Passed\n"
        "ruff.................................................Failed\n"
    )
    assert server._parse_failed_step(text) == "ruff"


def test_parse_failed_step_framework_multiple():
    text = (
        "ruff.................................................Failed\n"
        "black................................................Failed\n"
    )
    assert server._parse_failed_step(text) == "black (+1)"


def test_parse_failed_step_raw_hook_fallback():
    # Raw git hook: no `name....Failed` lines — surface its last output line,
    # skipping our own plumbing command echo and the shell prompt.
    text = (
        "user@host:~/wt$ touch .mindflock_precommit.lock; git add -A; "
        "git commit -F .mindflock_commit_msg\n"
        "running custom checks...\n"
        "ERROR: forbidden pattern found in src/app.py\n"
        "\n"
        "user@host:~/wt$\n"
    )
    assert (
        server._parse_failed_step(text)
        == "ERROR: forbidden pattern found in src/app.py"
    )


def test_parse_failed_step_truncates_long_lines():
    text = (
        "$ touch .mindflock_precommit.lock; git commit -F .mindflock_commit_msg\n"
        + "E" * 200
        + "\n"
    )
    out = server._parse_failed_step(text)
    assert out is not None and len(out) <= 80 and out.endswith("...")


def test_parse_failed_step_nothing_useful_is_none():
    text = "user@host:~/wt$ touch .mindflock_precommit.lock; git add -A\n\n$\n"
    assert server._parse_failed_step(text) is None


def test_parse_failed_step_prefers_error_line_over_trailing_noise():
    # A hook whose LAST line is a pointer/path echo must not become the badge —
    # the error line above it is the detail worth surfacing.
    text = (
        "$ touch .mindflock_precommit.lock; git commit -F .mindflock_commit_msg\n"
        "error: secret detected in config.py\n"
        "→ ~/MindFlock/app\n"
    )
    assert server._parse_failed_step(text) == "error: secret detected in config.py"


def test_parse_failed_step_only_noise_is_none():
    # Pointer echoes and bare paths name nothing — better the generic
    # "pre-commit ✗" badge than "✗ → ~/MindFlock/app".
    text = (
        "$ touch .mindflock_precommit.lock; git commit -F .mindflock_commit_msg\n"
        "→ ~/MindFlock/app\n"
        "~/MindFlock/app\n"
        "----\n"
    )
    assert server._parse_failed_step(text) is None


# --------------------------------------------------------------------------- #
# K4 — base-clone delete guard
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(
        server, "_workspace_roots", lambda: [os.path.realpath(str(root))]
    )
    monkeypatch.setattr(server, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(server, "_remove_trust_entry", lambda p: None)
    return root


def test_list_workspaces_skips_sizes_by_default(ws_root):
    (ws_root / "some-ws").mkdir()
    (ws_root / "some-ws" / "big.bin").write_bytes(b"x" * 4096)
    resp = asyncio.run(server.list_workspaces())
    body = json.loads(bytes(resp.body))
    (entry,) = body["workspaces"]
    assert entry["name"] == "some-ws"
    assert entry["size_bytes"] is None


def test_list_workspaces_sizes_on_request(ws_root):
    (ws_root / "some-ws").mkdir()
    (ws_root / "some-ws" / "big.bin").write_bytes(b"x" * 4096)
    resp = asyncio.run(server.list_workspaces(sizes=1))
    body = json.loads(bytes(resp.body))
    (entry,) = body["workspaces"]
    assert entry["size_bytes"] >= 4096


def test_delete_unreferenced_base_clone_allowed(ws_root):
    base = _init_repo(ws_root / "_base_myrepo")
    resp = asyncio.run(server.delete_workspace({"path": str(base)}))
    assert resp.status_code == 200
    assert not base.exists()


def test_delete_base_clone_with_attached_worktree_refused(ws_root):
    base = _init_repo(ws_root / "_base_held")
    wt = ws_root / "held-wt"
    _git("worktree", "add", "-q", "-b", "b1", str(wt), cwd=str(base))
    resp = asyncio.run(server.delete_workspace({"path": str(base)}))
    assert resp.status_code == 400
    body = bytes(resp.body).decode()
    assert "attached worktree" in body
    assert base.exists()


def test_delete_base_clone_with_active_session_refused(ws_root, monkeypatch):
    base = _init_repo(ws_root / "_base_active")
    inst = _FakeInst(str(ws_root / "some-wt"), repo_path=str(base), title="holder-1")
    monkeypatch.setitem(server.ENGINE.instances, "holder-1", inst)
    resp = asyncio.run(server.delete_workspace({"path": str(base)}))
    assert resp.status_code == 400
    body = bytes(resp.body).decode()
    assert "holder-1" in body
    assert base.exists()


def test_delete_refresher_dir_still_protected(ws_root):
    d = ws_root / "_testmon_refresher"
    d.mkdir()
    resp = asyncio.run(server.delete_workspace({"path": str(d)}))
    assert resp.status_code == 400
    assert "protected" in bytes(resp.body).decode()
    assert d.exists()


# --------------------------------------------------------------------------- #
# J3 — diff_stat: shape, totals, cache, null-when-unavailable
# --------------------------------------------------------------------------- #
def test_diff_stat_counts_committed_plus_uncommitted(tmp_path, monkeypatch):
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main", branch="feat/x")
    # Committed beyond main: new file, 2 lines.
    (wt / "b.txt").write_text("l1\nl2\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "add b", cwd=str(wt))
    # Uncommitted tracked change: +1 line in a.txt.
    (wt / "a.txt").write_text("one\ntwo\n")
    stat = server._session_diff_stat(inst)
    assert stat == {
        "files": 2,
        "additions": 3,
        "deletions": 0,
        "uncommitted": {"additions": 1, "deletions": 0},
    }


def test_diff_stat_counts_untracked_files(tmp_path):
    # New files the agent created but never staged count too (intent-to-add).
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main")
    (wt / "new.txt").write_text("n1\nn2\nn3\n")
    stat = server._session_diff_stat(inst)
    assert stat["files"] == 1 and stat["additions"] == 3
    assert stat["uncommitted"] == {"additions": 3, "deletions": 0}


def test_diff_stat_is_cached_about_ten_seconds(tmp_path):
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main")
    first = server._session_diff_stat(inst)
    assert first == {
        "files": 0,
        "additions": 0,
        "deletions": 0,
        "uncommitted": {"additions": 0, "deletions": 0},
    }
    # New change inside the TTL -> the cached answer is returned as-is.
    (wt / "a.txt").write_text("one\ntwo\n")
    assert server._session_diff_stat(inst) == first
    # Expired cache -> recomputed.
    server._DIFF_STAT_CACHE.clear()
    assert server._session_diff_stat(inst)["additions"] == 1


def test_diff_stat_fingerprint_skips_recompute_when_state_unchanged(
    tmp_path, monkeypatch
):
    # TTL lapsed but nothing changed in the worktree: the expensive shortstat
    # pair (seconds of CPU on a big session diff) must NOT re-run — the cached
    # numbers are re-armed off the cheap state fingerprint.
    from backend.web.core import snapshot as snapshot_mod

    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main")
    (wt / "a.txt").write_text("one\ntwo\n")
    first = server._session_diff_stat(inst)
    assert first["additions"] == 1
    exp, val, fp = server._DIFF_STAT_CACHE[str(wt)]
    assert fp is not None
    server._DIFF_STAT_CACHE[str(wt)] = (0.0, val, fp)  # expire the TTL only

    calls = []
    real_run = snapshot_mod.subprocess.run

    def spy(cmd, *a, **k):
        calls.append(list(cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(snapshot_mod.subprocess, "run", spy)
    assert server._session_diff_stat(inst) == first
    assert not any("--shortstat" in c for c in calls)  # no expensive diff ran
    assert not any("add" in c and "-N" in c for c in calls)  # no index mutation
    # And the TTL was re-armed so the next 10s are served from memory.
    assert server._DIFF_STAT_CACHE[str(wt)][0] > 0.0


def test_diff_stat_fingerprint_recomputes_on_content_change(tmp_path):
    # Same dirty path set, different content: `git status` output alone can't
    # tell — the (mtime_ns, size) stat pass in the fingerprint must force a
    # recompute after the TTL.
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main")
    (wt / "a.txt").write_text("one\ntwo\n")
    assert server._session_diff_stat(inst)["additions"] == 1
    (wt / "a.txt").write_text("one\ntwo\nthree\n")
    exp, val, fp = server._DIFF_STAT_CACHE[str(wt)]
    server._DIFF_STAT_CACHE[str(wt)] = (0.0, val, fp)  # expire the TTL only
    assert server._session_diff_stat(inst)["additions"] == 2


def test_diff_stat_fingerprint_recomputes_on_new_untracked_file(tmp_path):
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main")
    first = server._session_diff_stat(inst)
    assert first["additions"] == 0
    (wt / "new.txt").write_text("n1\nn2\n")
    exp, val, fp = server._DIFF_STAT_CACHE[str(wt)]
    server._DIFF_STAT_CACHE[str(wt)] = (0.0, val, fp)  # expire the TTL only
    stat = server._session_diff_stat(inst)
    assert stat["files"] == 1 and stat["additions"] == 2


def test_diff_stat_null_when_unavailable(tmp_path):
    assert server._session_diff_stat(_FakeInst(str(tmp_path), started=False)) is None
    paused = _FakeInst(str(tmp_path), status=Status.Paused)
    assert server._session_diff_stat(paused) is None
    gone = _FakeInst(str(tmp_path / "nope"))
    assert server._session_diff_stat(gone) is None


def test_instance_json_carries_diff_stat_key(tmp_path):
    inst = _FakeInst(str(tmp_path), started=False)
    inst.Path = str(tmp_path)
    d = server._instance_json(inst)
    assert "diff_stat" in d and d["diff_stat"] is None


# --------------------------------------------------------------------------- #
# Diff tab baselines: /diff and /file-diff honor base=fork (default) / head
# --------------------------------------------------------------------------- #
def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


def test_instance_diff_fork_mode_includes_committed_work(tmp_path, monkeypatch):
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main", title="d-fork")
    monkeypatch.setitem(server.ENGINE.instances, "d-fork", inst)
    (wt / "b.txt").write_text("l1\nl2\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "add b", cwd=str(wt))
    (wt / "a.txt").write_text("one\ntwo\n")
    body = _body(asyncio.run(server.instance_diff("d-fork")))
    assert body["base"] == "fork" and body["error"] is None
    # Committed (b.txt: 2 lines) + uncommitted (a.txt: 1 line) both present.
    assert body["added"] == 3 and body["removed"] == 0
    assert "b.txt" in body["content"] and "+two" in body["content"]


def test_instance_diff_head_mode_uses_worktree_diff(tmp_path, monkeypatch):
    class _Stats:
        Added, Removed, Content, Error = 2, 1, "diffdata", None

    class _DiffWorktree(_FakeWorktree):
        def Diff(self):  # noqa: N802
            return _Stats()

    inst = _FakeInst(str(tmp_path), title="d-head")
    inst.GetGitWorktree = lambda: _DiffWorktree(str(tmp_path))
    monkeypatch.setitem(server.ENGINE.instances, "d-head", inst)
    body = _body(asyncio.run(server.instance_diff("d-head", base="head")))
    assert body == {
        "added": 2,
        "removed": 1,
        "content": "diffdata",
        "error": None,
        "base": "head",
    }


def test_instance_file_diff_fork_shows_committed_head_does_not(tmp_path, monkeypatch):
    src, wt = _worktree_session(tmp_path)
    inst = _FakeInst(str(wt), base_branch="main", title="d-file")
    monkeypatch.setitem(server.ENGINE.instances, "d-file", inst)
    (wt / "a.txt").write_text("one\ntwo\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "grow a", cwd=str(wt))
    fork = _body(asyncio.run(server.instance_file_diff("d-file", path="a.txt")))
    head = _body(
        asyncio.run(server.instance_file_diff("d-file", path="a.txt", base="head"))
    )
    assert "+two" in fork["content"]
    assert head["content"] == ""


# --------------------------------------------------------------------------- #
# J5 — cost guardrail: settings field + one-shot event
# --------------------------------------------------------------------------- #
@pytest.fixture()
def budget_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    settings_store.invalidate()
    yield
    settings_store.invalidate()


def _set_budget(value):
    settings_store.update_settings(general={"session_budget_usd": value})


def test_general_settings_group_serializes(budget_env):
    _set_budget(1.5)
    assert settings_store.load_settings().general.session_budget_usd == 1.5
    # The generic settings-API group mechanism carries it with no addon edits.
    from backend.web.addons.settings import _masked_view

    assert _masked_view()["general"]["session_budget_usd"] == 1.5


def test_budget_event_emitted_once_per_session(budget_env):
    _set_budget(1.0)
    got = []
    unsub = events_mod.BUS.subscribe(
        lambda e: got.append(e) if e["event"] == "session.budget_exceeded" else None
    )
    try:
        server._check_session_budget("s1", 2.0)
        server._check_session_budget("s1", 2.5)  # still over: no re-fire
        assert len(got) == 1
        env = got[0]
        assert env["session"] == "s1"
        assert env["data"] == {"cost": 2.0, "budget": 1.0}
        # A second session fires independently.
        server._check_session_budget("s2", 3.0)
        assert len(got) == 2
    finally:
        unsub()


def test_budget_event_rearms_when_budget_raised(budget_env):
    _set_budget(1.0)
    got = []
    unsub = events_mod.BUS.subscribe(
        lambda e: got.append(e) if e["event"] == "session.budget_exceeded" else None
    )
    try:
        server._check_session_budget("s1", 2.0)
        assert len(got) == 1
        # Raised above the current cost -> silent again.
        _set_budget(5.0)
        server._check_session_budget("s1", 2.0)
        assert len(got) == 1
        # Cost crosses the RAISED budget -> announces once more.
        server._check_session_budget("s1", 6.0)
        assert len(got) == 2
        assert got[1]["data"] == {"cost": 6.0, "budget": 5.0}
    finally:
        unsub()


def test_budget_off_means_no_events(budget_env):
    got = []
    unsub = events_mod.BUS.subscribe(
        lambda e: got.append(e) if e["event"] == "session.budget_exceeded" else None
    )
    try:
        server._check_session_budget("s1", 100.0)  # absent = off
        _set_budget(0)
        server._check_session_budget("s1", 100.0)  # 0 = off
        assert got == []
    finally:
        unsub()


def test_budget_event_in_vocabulary():
    assert "session.budget_exceeded" in events_mod.EVENT_NAMES


# --------------------------------------------------------------------------- #
# make-pr route decision logic: the self-PR guard and the "PR already open"
# bounce vs. real-error path. These exercise the endpoint directly (previously
# only _pr_info / base-branch resolution had coverage).
# --------------------------------------------------------------------------- #
_MKPR_CLIENT = TestClient(server.app)


def _fake_cp(returncode: int, out: str):
    return subprocess.CompletedProcess(
        ["gh"], returncode, stdout=out.encode("utf-8"), stderr=b""
    )


def _prime_make_pr(monkeypatch, tmp_path, *, branch, base, title):
    """Register a fake session and stub the make-pr environment (git present,
    gh present, resolved base/branch) so only the endpoint's own branching runs.
    Returns the title. Caller pops it from ENGINE.instances in a finally."""
    inst = _FakeInst(str(tmp_path), branch=branch, title=title)
    server.ENGINE.instances[title] = inst
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(server, "_configured_pr_base", lambda: base)
    monkeypatch.setattr(server, "_current_branch", lambda wt: branch)
    return title


def test_make_pr_rejects_pr_into_base_branch(tmp_path, monkeypatch):
    # Session sits ON the base branch -> a branch can't be PR'd into itself.
    title = _prime_make_pr(
        monkeypatch, tmp_path, branch="main", base="main", title="mkpr-self"
    )
    called = []
    monkeypatch.setattr(
        server, "_run_capped", lambda *a, **k: called.append(a) or _fake_cp(0, "")
    )
    try:
        r = _MKPR_CLIENT.post(f"/api/instances/{title}/make-pr", json={})
        assert r.status_code == 409
        assert "base branch" in r.json()["error"]
        # gh was never invoked — the guard short-circuits before _do().
        assert called == []
    finally:
        server.ENGINE.instances.pop(title, None)


def test_make_pr_open_pr_bounce_returns_ok(tmp_path, monkeypatch):
    # gh create fails, but an OPEN PR already exists -> surface its URL, ok:True.
    title = _prime_make_pr(
        monkeypatch, tmp_path, branch="feat/x", base="main", title="mkpr-open"
    )
    monkeypatch.setattr(
        server,
        "_run_capped",
        lambda *a, **k: _fake_cp(1, "a pull request already exists"),
    )
    monkeypatch.setattr(
        server,
        "_pr_info",
        lambda *a, **k: {"state": "OPEN", "url": "https://example.test/pr/7"},
    )
    try:
        r = _MKPR_CLIENT.post(f"/api/instances/{title}/make-pr", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["note"] == "PR already open"
        assert body["url"] == "https://example.test/pr/7"
    finally:
        server.ENGINE.instances.pop(title, None)


def test_make_pr_no_commits_is_400(tmp_path, monkeypatch):
    # gh reports no commits and no OPEN PR exists -> 400 "nothing to PR".
    title = _prime_make_pr(
        monkeypatch, tmp_path, branch="feat/x", base="main", title="mkpr-empty"
    )
    monkeypatch.setattr(
        server,
        "_run_capped",
        lambda *a, **k: _fake_cp(1, "no commits between main and feat/x"),
    )
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
    try:
        r = _MKPR_CLIENT.post(f"/api/instances/{title}/make-pr", json={})
        assert r.status_code == 400
        assert "nothing to PR" in r.json()["error"]
    finally:
        server.ENGINE.instances.pop(title, None)
