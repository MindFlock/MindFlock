"""Behavioural tests for :class:`backend.session.instance.Instance`.

The instance orchestrates a tmux session and a git worktree. Both collaborators
are replaced with in-process fakes (the tmux fake returns ``Optional[Exception]``
Go-style; the worktree fake exposes the getter/predicate surface), so NO real
tmux server, PTY, git worktree, or subprocess is ever created. We pin the
lifecycle state machine (Start/Kill/Pause/Resume), the query methods, the
error-combination format, serialization, and the module helpers.

INTENTIONALLY UNCOVERED (noted, not faked):
  * the provisioned-workspace launch branch of ``_configure_launch_command``
    (needs a provisioned worktree + workspace_setup wiring),
  * ``Attach`` beyond the not-started guard (returns a live detach channel from
    a real PTY attach).
"""

from __future__ import annotations

import datetime as dt

import pytest

from backend.session import instance as inst_mod
from backend.session.instance import (
    Instance,
    InstanceOptions,
    _current_branch_of,
    _remove_all,
    from_instance_data,
    new_instance,
)
from backend.session.storage import (
    DiffStatsData,
    GitWorktreeData,
    InstanceData,
    Status,
)

# Captured before the autouse fixture replaces the module symbol with a no-op,
# so the "swallows clipboard errors" test can exercise the real implementation.
_ORIG_CLIPBOARD_WRITE = inst_mod._clipboard_write


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeTmux:
    """Go-style tmux double: action methods return an Exception or None."""

    def __init__(self):
        self.sanitized_name = "mindflock_demo"
        self.launch_command = None
        self.extra_env = {}
        # Injectable outcomes.
        self.capture = ("pane content", None)
        self.updated = (True, False)
        self.trust = True
        self.tap_err = None
        self.send_err = None
        self.attach_ret = ("CH", None)
        self.detach_err = None
        self.close_err = None
        self.restore_err = None
        self.start_err = None
        self.exists = True
        # Call log.
        self.calls = []

    def capture_pane_content(self):
        self.calls.append("capture")
        return self.capture

    def has_updated(self):
        self.calls.append("has_updated")
        return self.updated

    def check_and_handle_trust_prompt(self):
        self.calls.append("trust")
        return self.trust

    def tap_enter(self):
        self.calls.append("tap_enter")
        return self.tap_err

    def send_keys(self, keys):
        self.calls.append(("send_keys", keys))
        return self.send_err

    def attach(self):
        self.calls.append("attach")
        return self.attach_ret

    def detach_safely(self):
        self.calls.append("detach_safely")
        return self.detach_err

    def close(self):
        self.calls.append("close")
        return self.close_err

    def restore(self):
        self.calls.append("restore")
        return self.restore_err

    def start(self, work_dir):
        self.calls.append(("start", work_dir))
        return self.start_err

    def does_session_exist(self):
        return self.exists


class FakeWorktree:
    """Worktree double exposing the getter/predicate surface Instance needs."""

    def __init__(self, **kw):
        self.repo = kw.get("repo", "/repo")
        self.wt = kw.get("wt", "/wt")
        self.branch = kw.get("branch", "feature-x")
        self.base_sha = kw.get("base_sha", "abc123")
        self.existing = kw.get("existing", False)
        self.dirty = kw.get("dirty", False)
        self.valid = kw.get("valid", True)
        self.checked_out = kw.get("checked_out", False)
        self.keeps_dir = kw.get("keeps_dir", None)
        self.calls = []
        # Injectable failures (set to an Exception to raise).
        self.setup_exc = None
        self.cleanup_exc = None
        self.remove_exc = None
        self.prune_exc = None
        self.commit_exc = None
        self.valid_exc = None
        self.dirty_exc = None
        self.checked_out_exc = None

    def GetWorktreePath(self):
        return self.wt

    def GetRepoPath(self):
        return self.repo

    def GetBranchName(self):
        return self.branch

    def GetBaseCommitSHA(self):
        return self.base_sha

    def IsExistingBranch(self):
        return self.existing

    def Setup(self):
        self.calls.append("Setup")
        if self.setup_exc:
            raise self.setup_exc

    def Cleanup(self):
        self.calls.append("Cleanup")
        if self.cleanup_exc:
            raise self.cleanup_exc

    def Remove(self):
        self.calls.append("Remove")
        if self.remove_exc:
            raise self.remove_exc

    def Prune(self):
        self.calls.append("Prune")
        if self.prune_exc:
            raise self.prune_exc

    def IsDirty(self):
        if self.dirty_exc:
            raise self.dirty_exc
        return self.dirty

    def IsValidWorktree(self):
        if self.valid_exc:
            raise self.valid_exc
        return self.valid

    def IsBranchCheckedOut(self):
        if self.checked_out_exc:
            raise self.checked_out_exc
        return self.checked_out

    def CommitChanges(self, msg):
        self.calls.append(("CommitChanges", msg))
        if self.commit_exc:
            raise self.commit_exc


def _started_instance(program="claude"):
    """An Instance that already looks started, with fakes wired in."""
    inst = Instance()
    inst.Title = "demo"
    inst.Program = program
    inst.Status = Status.Running
    inst._started = True
    inst._tmux_session = FakeTmux()
    inst._git_worktree = FakeWorktree()
    return inst


@pytest.fixture(autouse=True)
def _no_clipboard(monkeypatch):
    # Never touch the real system clipboard from a test.
    monkeypatch.setattr(inst_mod, "_clipboard_write", lambda text: None)


# ---------------------------------------------------------------------------
# ToInstanceData
# ---------------------------------------------------------------------------
def test_to_instance_data_fills_worktree_and_diff():
    inst = _started_instance()
    inst.Title = "sc-1"
    inst.Path = "/repo"
    inst.Branch = "feature-x"
    inst.BaseBranch = "main"
    inst.LaunchArgs = ("--verbose",)
    inst._diff_stats = inst_mod.git.DiffStats(added=3, removed=1, content="D")
    data = inst.ToInstanceData()
    assert data.title == "sc-1"
    assert data.branch == "feature-x"
    assert data.base_branch == "main"
    assert data.launch_args == ("--verbose",)
    # Worktree block populated from the worktree getters.
    assert data.worktree.repo_path == "/repo"
    assert data.worktree.branch_name == "feature-x"
    assert data.worktree.base_commit_sha == "abc123"
    # Diff stats block populated.
    assert (data.diff_stats.added, data.diff_stats.removed) == (3, 1)
    # updated_at is set to now (a real tz-aware datetime).
    assert data.updated_at.tzinfo is not None


def test_to_instance_data_zero_value_when_no_worktree():
    inst = Instance()
    inst.Title = "x"
    inst._git_worktree = None
    inst._diff_stats = None
    data = inst.ToInstanceData()
    # Zero-value objects are still present.
    assert data.worktree == GitWorktreeData()
    assert data.diff_stats == DiffStatsData()


# ---------------------------------------------------------------------------
# Query methods + guards
# ---------------------------------------------------------------------------
def test_set_status():
    inst = Instance()
    inst.SetStatus(Status.Loading)
    assert inst.Status == Status.Loading


def test_preview_empty_when_not_started():
    assert Instance().Preview() == ""


def test_preview_empty_when_paused():
    inst = _started_instance()
    inst.Status = Status.Paused
    assert inst.Preview() == ""


def test_preview_returns_content():
    inst = _started_instance()
    inst._tmux_session.capture = ("hello pane", None)
    assert inst.Preview() == "hello pane"


def test_preview_raises_on_capture_error():
    inst = _started_instance()
    inst._tmux_session.capture = ("", Exception("cap boom"))
    with pytest.raises(RuntimeError) as ei:
        inst.Preview()
    assert "cap boom" in str(ei.value)


def test_has_updated_false_when_not_started():
    assert Instance().HasUpdated() == (False, False)


def test_has_updated_delegates_to_tmux():
    inst = _started_instance()
    inst._tmux_session.updated = (True, True)
    assert inst.HasUpdated() == (True, True)


def test_trust_prompt_false_when_not_started():
    assert Instance().CheckAndHandleTrustPrompt() is False


def test_trust_prompt_false_for_unsupported_program():
    inst = _started_instance(program="codex")
    assert inst.CheckAndHandleTrustPrompt() is False


def test_trust_prompt_delegates_for_claude():
    inst = _started_instance(program="/usr/bin/claude")
    inst._tmux_session.trust = True
    assert inst.CheckAndHandleTrustPrompt() is True
    assert "trust" in inst._tmux_session.calls


def test_trust_prompt_delegates_for_aider():
    inst = _started_instance(program="aider")
    inst._tmux_session.trust = False
    assert inst.CheckAndHandleTrustPrompt() is False


def test_tap_enter_noop_when_autoyes_off():
    inst = _started_instance()
    inst.AutoYes = False
    inst.TapEnter()
    assert "tap_enter" not in inst._tmux_session.calls


def test_tap_enter_fires_when_autoyes_on():
    inst = _started_instance()
    inst.AutoYes = True
    inst.TapEnter()
    assert "tap_enter" in inst._tmux_session.calls


def test_tap_enter_swallows_error():
    inst = _started_instance()
    inst.AutoYes = True
    inst._tmux_session.tap_err = Exception("boom")
    # Fire-and-forget: no raise.
    inst.TapEnter()


def test_attach_raises_when_not_started():
    with pytest.raises(RuntimeError) as ei:
        Instance().Attach()
    assert "cannot attach instance that has not been started" in str(ei.value)


def test_attach_returns_channel():
    inst = _started_instance()
    inst._tmux_session.attach_ret = ("CHAN", None)
    assert inst.Attach() == "CHAN"


def test_attach_raises_on_error():
    inst = _started_instance()
    inst._tmux_session.attach_ret = (None, Exception("attach boom"))
    with pytest.raises(RuntimeError) as ei:
        inst.Attach()
    assert "attach boom" in str(ei.value)


def test_get_git_worktree_guard_and_success():
    with pytest.raises(RuntimeError):
        Instance().GetGitWorktree()
    inst = _started_instance()
    assert inst.GetGitWorktree() is inst._git_worktree


def test_get_worktree_path():
    assert Instance().GetWorktreePath() == ""
    inst = _started_instance()
    assert inst.GetWorktreePath() == "/wt"


def test_started_and_paused_flags():
    inst = Instance()
    assert inst.Started() is False
    assert inst.Paused() is False
    inst._started = True
    inst.Status = Status.Paused
    assert inst.Started() is True
    assert inst.Paused() is True


# ---------------------------------------------------------------------------
# SendPrompt / SendKeys
# ---------------------------------------------------------------------------
def test_send_prompt_sends_keys_then_enter(monkeypatch):
    monkeypatch.setattr(inst_mod.time, "sleep", lambda _s: None)
    inst = _started_instance()
    inst.SendPrompt("hi there")
    assert ("send_keys", "hi there") in inst._tmux_session.calls
    assert "tap_enter" in inst._tmux_session.calls


def test_send_prompt_raises_when_not_started():
    with pytest.raises(RuntimeError) as ei:
        Instance().SendPrompt("x")
    assert "instance not started" in str(ei.value)


def test_send_prompt_raises_when_tmux_missing():
    inst = Instance()
    inst._started = True
    inst._tmux_session = None
    with pytest.raises(RuntimeError) as ei:
        inst.SendPrompt("x")
    assert "tmux session not initialized" in str(ei.value)


def test_send_prompt_raises_on_send_error(monkeypatch):
    monkeypatch.setattr(inst_mod.time, "sleep", lambda _s: None)
    inst = _started_instance()
    inst._tmux_session.send_err = Exception("send boom")
    with pytest.raises(RuntimeError) as ei:
        inst.SendPrompt("x")
    assert "error sending keys to tmux session: send boom" in str(ei.value)


def test_send_prompt_raises_on_enter_error(monkeypatch):
    monkeypatch.setattr(inst_mod.time, "sleep", lambda _s: None)
    inst = _started_instance()
    inst._tmux_session.tap_err = Exception("enter boom")
    with pytest.raises(RuntimeError) as ei:
        inst.SendPrompt("x")
    assert "error tapping enter: enter boom" in str(ei.value)


def test_send_keys_success():
    inst = _started_instance()
    inst.SendKeys("abc")
    assert ("send_keys", "abc") in inst._tmux_session.calls


def test_send_keys_raises_when_paused():
    inst = _started_instance()
    inst.Status = Status.Paused
    with pytest.raises(RuntimeError) as ei:
        inst.SendKeys("x")
    assert "has not been started or is paused" in str(ei.value)


def test_send_keys_raises_on_error():
    inst = _started_instance()
    inst._tmux_session.send_err = Exception("sk boom")
    with pytest.raises(RuntimeError) as ei:
        inst.SendKeys("x")
    assert "sk boom" in str(ei.value)


# ---------------------------------------------------------------------------
# Kill + _combine_errors
# ---------------------------------------------------------------------------
def test_kill_noop_when_not_started():
    inst = Instance()
    inst._tmux_session = FakeTmux()
    inst.Kill()  # no raise, no calls
    assert inst._tmux_session.calls == []


def test_kill_closes_tmux_and_cleans_worktree():
    inst = _started_instance()
    inst.Kill()
    assert "close" in inst._tmux_session.calls
    assert "Cleanup" in inst._git_worktree.calls


def test_kill_collects_single_error():
    inst = _started_instance()
    inst._tmux_session.close_err = Exception("close boom")
    with pytest.raises(RuntimeError) as ei:
        inst.Kill()
    assert "failed to close tmux session: close boom" in str(ei.value)


def test_kill_combines_multiple_errors():
    inst = _started_instance()
    inst._tmux_session.close_err = Exception("close boom")
    inst._git_worktree.cleanup_exc = RuntimeError("cleanup boom")
    with pytest.raises(RuntimeError) as ei:
        inst.Kill()
    msg = str(ei.value)
    assert "multiple cleanup errors occurred:" in msg
    assert "failed to close tmux session: close boom" in msg
    assert "failed to cleanup git worktree: cleanup boom" in msg


def test_combine_errors_empty_and_single():
    inst = Instance()
    assert inst._combine_errors([]) is None
    one = RuntimeError("only")
    assert inst._combine_errors([one]) is one


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def test_start_raises_on_empty_title():
    inst = Instance()
    inst.Title = ""
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "instance title cannot be empty" in str(ei.value)


def test_start_restore_path_success():
    # first_time_setup=False restores an existing tmux session.
    inst = Instance()
    inst.Title = "demo"
    inst._tmux_session = FakeTmux()
    inst._tmux_session.restore_err = None
    inst.Start(False)
    assert inst.Started() is True
    assert inst.Status == Status.Running
    assert "restore" in inst._tmux_session.calls


def test_start_restore_failure_cleans_up_and_raises():
    inst = Instance()
    inst.Title = "demo"
    tmuxs = FakeTmux()
    tmuxs.restore_err = Exception("restore boom")
    inst._tmux_session = tmuxs
    with pytest.raises(RuntimeError) as ei:
        inst.Start(False)
    assert "failed to restore existing session: restore boom" in str(ei.value)
    assert inst.Started() is False


def test_start_in_place_first_time_success(tmp_path):
    # in_place uses the real _InPlaceWorktree over a real dir (no git needed:
    # _detect swallows the non-repo error), then starts the fake tmux session.
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst.InPlace = True
    inst._tmux_session = FakeTmux()
    inst.Start(True)
    assert inst.Started() is True
    assert inst.Status == Status.Running
    # In-place: the base branch is the (empty) detected branch.
    assert isinstance(inst._git_worktree, inst_mod._InPlaceWorktree)


def test_start_setup_failure_wrapped(tmp_path):
    # in_place Setup() raises when the repo dir no longer exists.
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path / "gone")
    inst.InPlace = True
    inst._tmux_session = FakeTmux()
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to setup git worktree" in str(ei.value)
    assert inst.Started() is False


def test_start_tmux_failure_cleans_up_and_raises(tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst.InPlace = True
    tmuxs = FakeTmux()
    tmuxs.start_err = Exception("tmux start boom")
    inst._tmux_session = tmuxs
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to start new session: tmux start boom" in str(ei.value)
    assert inst.Started() is False
    # _cleanup_partial closed the tmux session.
    assert "close" in tmuxs.calls


def test_start_plain_branch_uses_new_git_worktree(monkeypatch, tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst._tmux_session = FakeTmux()

    fake_wt = FakeWorktree(branch="mindflock-demo")
    monkeypatch.setattr(
        inst_mod.git, "NewGitWorktree", lambda path, title: (fake_wt, "mindflock-demo")
    )
    monkeypatch.setattr(inst_mod, "_current_branch_of", lambda p: "main")

    inst.Start(True)
    assert inst.Started() is True
    assert inst.Branch == "mindflock-demo"
    # BaseBranch recorded from the source repo's current branch.
    assert inst.BaseBranch == "main"
    assert "Setup" in fake_wt.calls


def test_start_selected_branch_uses_from_branch(monkeypatch, tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst._selected_branch = "existing-b"
    inst._tmux_session = FakeTmux()

    fake_wt = FakeWorktree(branch="existing-b", existing=True)
    monkeypatch.setattr(
        inst_mod.git,
        "NewGitWorktreeFromBranch",
        lambda path, branch, title: fake_wt,
    )
    inst.Start(True)
    assert inst.Branch == "existing-b"
    # An existing branch is its own base.
    assert inst.BaseBranch == "existing-b"


def test_start_worktree_creation_failure_wrapped(monkeypatch, tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst._tmux_session = FakeTmux()

    def _boom(path, title):
        raise RuntimeError("no repo here")

    monkeypatch.setattr(inst_mod.git, "NewGitWorktree", _boom)
    monkeypatch.setattr(inst_mod, "_current_branch_of", lambda p: "")
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to create git worktree: no repo here" in str(ei.value)


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------
def test_pause_raises_when_not_started():
    with pytest.raises(RuntimeError) as ei:
        Instance().Pause()
    assert "cannot pause instance that has not been started" in str(ei.value)


def test_pause_raises_when_already_paused():
    inst = _started_instance()
    inst.Status = Status.Paused
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "instance is already paused" in str(ei.value)


def test_pause_clean_worktree_removes_and_prunes(monkeypatch):
    inst = _started_instance()
    inst._git_worktree = FakeWorktree(valid=True, dirty=False)
    # Path exists so Remove is attempted.
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    inst.Pause()
    assert inst.Status == Status.Paused
    assert "Remove" in inst._git_worktree.calls
    assert "Prune" in inst._git_worktree.calls
    assert "detach_safely" in inst._tmux_session.calls


def test_pause_dirty_worktree_commits_first(monkeypatch):
    inst = _started_instance()
    inst.Title = "demo"
    inst._git_worktree = FakeWorktree(valid=True, dirty=True)
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    inst.Pause()
    assert inst.Status == Status.Paused
    # A commit was made before removal, with the [mindflock] ... (paused) msg.
    commit_calls = [c for c in inst._git_worktree.calls if isinstance(c, tuple)]
    assert commit_calls and commit_calls[0][0] == "CommitChanges"
    assert "[mindflock] update from 'demo'" in commit_calls[0][1]
    assert "(paused)" in commit_calls[0][1]


def test_pause_commit_failure_returns_early(monkeypatch):
    inst = _started_instance()
    wt = FakeWorktree(valid=True, dirty=True)
    wt.commit_exc = RuntimeError("commit boom")
    inst._git_worktree = wt
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to commit changes: commit boom" in str(ei.value)
    # Removal never attempted because commit failed.
    assert "Remove" not in wt.calls


def test_pause_orphaned_worktree_skips_dirty_and_removes_dir(monkeypatch):
    inst = _started_instance()
    wt = FakeWorktree(valid=False)
    inst._git_worktree = wt
    removed = {}
    monkeypatch.setattr(
        inst_mod, "_remove_all", lambda p: removed.__setitem__("path", p)
    )
    inst.Pause()
    assert inst.Status == Status.Paused
    # Orphaned path: detach + prune, and the leftover dir is removed.
    assert "detach_safely" in inst._tmux_session.calls
    assert "Prune" in wt.calls
    assert removed["path"] == "/wt"
    # IsDirty / Remove were skipped for the orphaned worktree.
    assert "Remove" not in wt.calls


def test_pause_orphaned_worktree_keeps_dir_when_flagged(monkeypatch):
    inst = _started_instance()
    wt = FakeWorktree(valid=False)
    wt.keeps_dir_across_pause = lambda: True
    inst._git_worktree = wt
    calls = {"removed": False}
    monkeypatch.setattr(
        inst_mod, "_remove_all", lambda p: calls.__setitem__("removed", True)
    )
    inst.Pause()
    assert inst.Status == Status.Paused
    # keeps_dir_across_pause() True -> the directory is preserved.
    assert calls["removed"] is False


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------
def test_resume_raises_when_not_started():
    with pytest.raises(RuntimeError) as ei:
        Instance().Resume()
    assert "cannot resume instance that has not been started" in str(ei.value)


def test_resume_raises_when_not_paused():
    inst = _started_instance()
    inst.Status = Status.Running
    with pytest.raises(RuntimeError) as ei:
        inst.Resume()
    assert "can only resume paused instances" in str(ei.value)


def test_resume_raises_when_branch_checked_out():
    inst = _started_instance()
    inst.Status = Status.Paused
    inst._git_worktree = FakeWorktree(checked_out=True)
    with pytest.raises(RuntimeError) as ei:
        inst.Resume()
    assert "cannot resume: branch is checked out" in str(ei.value)


def test_resume_restore_success():
    inst = _started_instance()
    inst.Status = Status.Paused
    inst._git_worktree = FakeWorktree(checked_out=False)
    tmuxs = inst._tmux_session
    tmuxs.exists = True
    tmuxs.restore_err = None
    inst.Resume()
    assert inst.Status == Status.Running
    assert "Setup" in inst._git_worktree.calls
    assert "restore" in tmuxs.calls


def test_resume_starts_new_session_when_absent():
    inst = _started_instance()
    inst.Status = Status.Paused
    inst.ExtraEnv = {"PORT": "9001"}
    inst._git_worktree = FakeWorktree(checked_out=False)
    tmuxs = inst._tmux_session
    tmuxs.exists = False  # no existing session -> start a new one
    tmuxs.start_err = None
    inst.Resume()
    assert inst.Status == Status.Running
    assert any(isinstance(c, tuple) and c[0] == "start" for c in tmuxs.calls)
    # ExtraEnv is re-applied to the tmux session on the new-session path.
    assert tmuxs.extra_env == {"PORT": "9001"}


def test_resume_setup_failure_raises(_errorlog):
    inst = _started_instance()
    inst.Status = Status.Paused
    wt = FakeWorktree(checked_out=False)
    wt.setup_exc = RuntimeError("setup boom")
    inst._git_worktree = wt
    with pytest.raises(RuntimeError) as ei:
        inst.Resume()
    assert "failed to setup git worktree: setup boom" in str(ei.value)
    assert _errorlog.msgs  # the error was logged before raising


def test_resume_new_session_start_failure_cleans_up():
    inst = _started_instance()
    inst.Status = Status.Paused
    wt = FakeWorktree(checked_out=False)
    inst._git_worktree = wt
    tmuxs = inst._tmux_session
    tmuxs.exists = False
    tmuxs.start_err = Exception("start boom")
    with pytest.raises(RuntimeError) as ei:
        inst.Resume()
    assert "failed to start new session: start boom" in str(ei.value)
    assert "Cleanup" in wt.calls


# ---------------------------------------------------------------------------
# new_instance
# ---------------------------------------------------------------------------
def test_new_instance_defaults_and_abspath(monkeypatch):
    monkeypatch.setattr(
        inst_mod, "_provider_default_launch_args", lambda program: ("--default",)
    )
    opts = InstanceOptions(title="t", path="rel/path", program="claude")
    inst = new_instance(opts)
    assert inst.Title == "t"
    assert inst.Status == Status.Ready
    assert inst.AutoYes is False  # opts.auto_yes intentionally ignored
    assert inst.Path.endswith("rel/path") and inst.Path.startswith("/")
    # launch_args None -> inherits the global provider default.
    assert inst.LaunchArgs == ("--default",)


def test_new_instance_explicit_launch_args_verbatim(monkeypatch):
    monkeypatch.setattr(
        inst_mod, "_provider_default_launch_args", lambda program: ("--default",)
    )
    # An explicit (even empty) list is used verbatim; the default is NOT applied.
    opts = InstanceOptions(title="t", path="/x", launch_args=["--only"])
    inst = new_instance(opts)
    assert inst.LaunchArgs == ("--only",)

    opts_empty = InstanceOptions(title="t", path="/x", launch_args=[])
    assert new_instance(opts_empty).LaunchArgs == ()


def test_new_instance_abspath_error_wrapped(monkeypatch):
    def _boom(_p):
        raise ValueError("bad path")

    monkeypatch.setattr(inst_mod.os.path, "abspath", _boom)
    with pytest.raises(RuntimeError) as ei:
        new_instance(InstanceOptions(title="t", path="x"))
    assert "failed to get absolute path: bad path" in str(ei.value)


def test_provider_default_launch_args_swallows_failure(monkeypatch):
    # Force the settings load to blow up; the helper must degrade to ().
    import backend.config.settings as settings_mod

    def _boom():
        raise RuntimeError("no settings")

    monkeypatch.setattr(settings_mod, "load_settings", _boom)
    assert inst_mod._provider_default_launch_args("claude") == ()


# ---------------------------------------------------------------------------
# from_instance_data
# ---------------------------------------------------------------------------
def _instance_data(**kw):
    return InstanceData(
        title=kw.get("title", "demo"),
        path=kw.get("path", "/repo"),
        branch=kw.get("branch", "feature-x"),
        status=kw.get("status", Status.Running),
        program=kw.get("program", "claude"),
        worktree=GitWorktreeData(
            repo_path="/repo",
            worktree_path="/wt",
            session_name="demo",
            branch_name="feature-x",
        ),
        diff_stats=DiffStatsData(added=1, removed=2, content="c"),
        in_place=kw.get("in_place", False),
        provisioned=kw.get("provisioned", False),
        base_branch=kw.get("base_branch", "main"),
    )


def test_from_instance_data_paused_marks_started_without_attach(monkeypatch):
    started = {"n": 0}
    monkeypatch.setattr(
        inst_mod.tmux, "NewTmuxSession", lambda title, program: FakeTmux()
    )
    inst = from_instance_data(_instance_data(status=Status.Paused))
    assert inst.Started() is True
    assert inst.Paused() is True
    assert inst._tmux_session is not None
    # diff stats + fields rehydrated.
    assert inst._diff_stats.Added == 1
    assert inst.BaseBranch == "main"


def test_from_instance_data_attach_false_no_start(monkeypatch):
    monkeypatch.setattr(
        inst_mod.tmux, "NewTmuxSession", lambda title, program: FakeTmux()
    )
    inst = from_instance_data(_instance_data(status=Status.Running), attach=False)
    assert inst.Started() is True
    assert inst._tmux_session is not None


def test_from_instance_data_running_attach_calls_start(monkeypatch):
    calls = {"start": 0}

    def fake_start(self, first_time):
        calls["first_time"] = first_time
        calls["start"] += 1
        self._started = True

    monkeypatch.setattr(inst_mod.Instance, "Start", fake_start)
    monkeypatch.setattr(inst_mod, "_worktree_from_data", lambda data: FakeWorktree())
    inst = from_instance_data(_instance_data(status=Status.Running), attach=True)
    assert calls["start"] == 1
    # Running instances are restored via Start(False).
    assert calls["first_time"] is False
    assert inst is not None


def test_from_instance_data_in_place_rebuilds_inplace_worktree(monkeypatch):
    monkeypatch.setattr(
        inst_mod.tmux, "NewTmuxSession", lambda title, program: FakeTmux()
    )
    inst = from_instance_data(_instance_data(status=Status.Paused, in_place=True))
    assert isinstance(inst._git_worktree, inst_mod._InPlaceWorktree)


# ---------------------------------------------------------------------------
# _InPlaceWorktree
# ---------------------------------------------------------------------------
def test_in_place_worktree_noops_and_flags(tmp_path):
    wt = inst_mod._InPlaceWorktree(
        repoPath=str(tmp_path),
        worktreePath=str(tmp_path),
        sessionName="s",
        branchName="",
    )
    # No-op teardown methods return None and never delete the dir.
    assert wt.Remove() is None
    assert wt.Prune() is None
    assert wt.Cleanup() is None
    assert wt.IsBranchCheckedOut() is False
    assert wt.keeps_dir_across_pause() is True
    assert tmp_path.exists()


def test_in_place_worktree_setup_raises_when_dir_gone(tmp_path):
    missing = tmp_path / "nope"
    wt = inst_mod._InPlaceWorktree(
        repoPath=str(missing),
        worktreePath=str(missing),
        sessionName="s",
        branchName="",
    )
    with pytest.raises(RuntimeError) as ei:
        wt.Setup()
    assert "in-place repo no longer exists" in str(ei.value)


def test_in_place_worktree_detects_branch_and_sha(tmp_path):
    # A real throwaway git repo so _detect populates branch + base SHA.
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_TERMINAL_PROMPT": "0"}

    def g(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    subprocess.run(
        ["git", "init", "-b", "trunk", str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    g("config", "user.email", "t@e.com")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("x\n")
    g("add", ".")
    g("commit", "-m", "init")

    wt = inst_mod._InPlaceWorktree(
        repoPath=str(repo),
        worktreePath=str(repo),
        sessionName="s",
        branchName="",
    )
    assert wt.branchName == "trunk"
    assert len(wt.baseCommitSHA) == 40


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------
def test_current_branch_of_returns_empty_on_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(inst_mod.subprocess, "run", _boom)
    assert _current_branch_of("/x") == ""


def test_current_branch_of_returns_branch(monkeypatch):
    class _R:
        returncode = 0
        stdout = "main\n"

    monkeypatch.setattr(inst_mod.subprocess, "run", lambda *a, **k: _R())
    assert _current_branch_of("/x") == "main"


def test_current_branch_of_empty_on_nonzero(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(inst_mod.subprocess, "run", lambda *a, **k: _R())
    assert _current_branch_of("/x") == ""


def test_remove_all_file_dir_and_absent(tmp_path):
    # File.
    f = tmp_path / "f.txt"
    f.write_text("x")
    _remove_all(str(f))
    assert not f.exists()

    # Directory tree.
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "g.txt").write_text("y")
    _remove_all(str(d))
    assert not d.exists()

    # Absent path is a silent no-op.
    _remove_all(str(tmp_path / "never"))


def test_remove_all_symlink(tmp_path):
    import os

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(str(target), str(link))
    _remove_all(str(link))
    assert not link.exists()
    # The symlink target survives (only the link was removed).
    assert target.exists()


def test_clipboard_write_swallows_errors(monkeypatch):
    def _boom(_text):
        raise RuntimeError("no clipboard")

    monkeypatch.setattr(inst_mod.pyperclip, "copy", _boom)
    # Call the module's real implementation (captured before the autouse fixture
    # swapped the module symbol for a no-op): a clipboard failure must not raise.
    _ORIG_CLIPBOARD_WRITE("text")


# ---------------------------------------------------------------------------
# A capturing logger for the ErrorLog/WarningLog branches
# ---------------------------------------------------------------------------
class _CapLog:
    def __init__(self):
        self.msgs = []

    def Print(self, *args):
        self.msgs.append(" ".join(str(a) for a in args))

    def Printf(self, fmt, *args):
        # Loggers use Go-style verbs (%v) that Python's % rejects; just append
        # the args so message-substring assertions still work.
        self.msgs.append(fmt + " " + " ".join(str(a) for a in args))

    def Println(self, *args):
        self.msgs.append(" ".join(str(a) for a in args))


# ---------------------------------------------------------------------------
# Start — creating the tmux session + extra_env; provisioned branch
# ---------------------------------------------------------------------------
def test_start_creates_tmux_when_none_and_sets_extra_env(monkeypatch, tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst.InPlace = True
    inst.ExtraEnv = {"PORT": "9000"}
    made = FakeTmux()
    monkeypatch.setattr(inst_mod.tmux, "NewTmuxSession", lambda title, program: made)
    inst.Start(True)
    # The instance built its own tmux session (none was pre-injected) ...
    assert inst._tmux_session is made
    # ... and copied ExtraEnv onto it before starting.
    assert made.extra_env == {"PORT": "9000"}


def test_start_provisioned_happy_path(monkeypatch, tmp_path):
    import types

    from backend.session import provisioned as prov

    inst = Instance()
    inst.Title = "sc-1"
    inst.Path = str(tmp_path)
    inst.Provisioned = True
    inst._tmux_session = FakeTmux()
    fake_wt = FakeWorktree(branch="feature/sc-1", wt=str(tmp_path))
    settings = types.SimpleNamespace(base_branch="staging")
    monkeypatch.setattr(
        prov, "load_provision_settings", lambda repo_url_override=None: settings
    )
    monkeypatch.setattr(prov, "branch_name_for", lambda a, b: "feature/sc-1")
    monkeypatch.setattr(prov, "build_provisioned_worktree", lambda *a, **k: fake_wt)

    inst.Start(True)
    assert inst.Started() is True
    assert inst.Branch == "feature/sc-1"
    # K1: base branch recorded from the provision settings.
    assert inst.BaseBranch == "staging"
    # The provisioned launch path writes a wrapper launcher and sets it on tmux.
    assert inst._tmux_session.launch_command is not None


def test_start_provisioned_no_repo_configured_raises(monkeypatch, tmp_path):
    from backend.session import provisioned as prov

    inst = Instance()
    inst.Title = "x"
    inst.Path = str(tmp_path)
    inst.Provisioned = True
    inst._tmux_session = FakeTmux()
    monkeypatch.setattr(
        prov, "load_provision_settings", lambda repo_url_override=None: None
    )
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "provisioned session requested but no repo is" in str(ei.value)


def test_start_provisioned_local_repo_not_git_raises(monkeypatch, tmp_path):
    from backend.session import provisioned as prov

    inst = Instance()
    inst.Title = "x"
    inst.Path = str(tmp_path)
    inst.Provisioned = True
    inst._provision_repo = "/some/local/dir"
    inst._tmux_session = FakeTmux()
    monkeypatch.setattr(prov, "local_settings_for", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "not a git repository" in str(ei.value)


def test_start_provisioned_build_failure_wrapped(monkeypatch, tmp_path):
    import types

    from backend.session import provisioned as prov

    inst = Instance()
    inst.Title = "x"
    inst.Path = str(tmp_path)
    inst.Provisioned = True
    inst._tmux_session = FakeTmux()
    monkeypatch.setattr(
        prov,
        "load_provision_settings",
        lambda repo_url_override=None: types.SimpleNamespace(base_branch="s"),
    )
    monkeypatch.setattr(prov, "branch_name_for", lambda a, b: "b")

    def _boom(*a, **k):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(prov, "build_provisioned_worktree", _boom)
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to create provisioned worktree: clone failed" in str(ei.value)


def test_start_selected_branch_failure_wrapped(monkeypatch, tmp_path):
    inst = Instance()
    inst.Title = "x"
    inst.Path = str(tmp_path)
    inst._selected_branch = "b"
    inst._tmux_session = FakeTmux()

    def _boom(path, branch, title):
        raise RuntimeError("no such branch")

    monkeypatch.setattr(inst_mod.git, "NewGitWorktreeFromBranch", _boom)
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to create git worktree from branch: no such branch" in str(ei.value)


def test_start_tmux_failure_swallows_close_error(tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst.InPlace = True
    tmuxs = FakeTmux()
    tmuxs.start_err = Exception("start boom")

    def _raise_close():
        raise RuntimeError("close raised")

    tmuxs.close = _raise_close
    inst._tmux_session = tmuxs
    # _cleanup_partial must swallow the close() error and still raise the
    # original start failure.
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to start new session: start boom" in str(ei.value)


def test_start_tmux_failure_chains_worktree_cleanup_error(monkeypatch, tmp_path):
    inst = Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst._tmux_session = FakeTmux()
    inst._tmux_session.start_err = Exception("start boom")

    fake_wt = FakeWorktree(branch="b")
    fake_wt.cleanup_exc = RuntimeError("cleanup boom")
    monkeypatch.setattr(
        inst_mod.git, "NewGitWorktree", lambda path, title: (fake_wt, "b")
    )
    monkeypatch.setattr(inst_mod, "_current_branch_of", lambda p: "main")
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    msg = str(ei.value)
    # The tmux start error is wrapped, and the worktree Cleanup error is chained.
    assert "failed to start new session: start boom" in msg
    assert "cleanup error: cleanup boom" in msg


def test_restart_after_started_setup_failure_triggers_kill(tmp_path):
    # A second Start(True) on an already-started instance whose in-place repo
    # has vanished: Setup fails and, because _started is True, the except path
    # goes through Kill() rather than _cleanup_partial.
    inst = _started_instance()
    inst.InPlace = True
    inst.Path = str(tmp_path / "gone")
    with pytest.raises(RuntimeError) as ei:
        inst.Start(True)
    assert "failed to setup git worktree" in str(ei.value)
    # Kill() closed the tmux session.
    assert "close" in inst._tmux_session.calls


def test_tap_enter_logs_error(monkeypatch):
    cap = _CapLog()
    monkeypatch.setattr(inst_mod.log, "ErrorLog", cap)
    inst = _started_instance()
    inst.AutoYes = True
    inst._tmux_session.tap_err = Exception("tap boom")
    inst.TapEnter()  # no raise
    assert any("tap boom" in m for m in cap.msgs)


# ---------------------------------------------------------------------------
# Pause — error-collection branches
# ---------------------------------------------------------------------------
@pytest.fixture
def _errorlog(monkeypatch):
    cap = _CapLog()
    monkeypatch.setattr(inst_mod.log, "ErrorLog", cap)
    monkeypatch.setattr(inst_mod.log, "WarningLog", cap)
    return cap


def test_pause_validate_failure_is_collected(monkeypatch, _errorlog):
    inst = _started_instance()
    wt = FakeWorktree(valid=True, dirty=False)
    wt.valid_exc = RuntimeError("validate boom")
    inst._git_worktree = wt
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to validate worktree: validate boom" in str(ei.value)


def test_pause_orphaned_detach_failure_collected(monkeypatch, _errorlog):
    inst = _started_instance()
    inst._git_worktree = FakeWorktree(valid=False)
    inst._tmux_session.detach_err = Exception("detach boom")
    monkeypatch.setattr(inst_mod, "_remove_all", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to detach tmux session: detach boom" in str(ei.value)


def test_pause_orphaned_remove_dir_failure_collected(monkeypatch, _errorlog):
    inst = _started_instance()
    inst._git_worktree = FakeWorktree(valid=False)

    def _boom(_p):
        raise OSError("rm boom")

    monkeypatch.setattr(inst_mod, "_remove_all", _boom)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to remove orphaned worktree directory" in str(ei.value)


def test_pause_orphaned_prune_failure_collected(monkeypatch, _errorlog):
    inst = _started_instance()
    wt = FakeWorktree(valid=False)
    wt.prune_exc = RuntimeError("prune boom")
    inst._git_worktree = wt
    monkeypatch.setattr(inst_mod, "_remove_all", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to prune git worktrees: prune boom" in str(ei.value)


def test_pause_dirty_check_failure_collected(monkeypatch, _errorlog):
    inst = _started_instance()
    wt = FakeWorktree(valid=True)
    wt.dirty_exc = RuntimeError("dirty boom")
    inst._git_worktree = wt
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to check if worktree is dirty: dirty boom" in str(ei.value)


def test_pause_remove_failure_returns_early(monkeypatch, _errorlog):
    inst = _started_instance()
    wt = FakeWorktree(valid=True, dirty=False)
    wt.remove_exc = RuntimeError("remove boom")
    inst._git_worktree = wt
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to remove git worktree: remove boom" in str(ei.value)
    # Prune not attempted after a failed Remove.
    assert "Prune" not in wt.calls


def test_pause_prune_failure_returns_early(monkeypatch, _errorlog):
    inst = _started_instance()
    wt = FakeWorktree(valid=True, dirty=False)
    wt.prune_exc = RuntimeError("prune boom")
    inst._git_worktree = wt
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    assert "failed to prune git worktrees: prune boom" in str(ei.value)


def test_pause_normal_detach_failure_still_pauses(monkeypatch, _errorlog):
    inst = _started_instance()
    wt = FakeWorktree(valid=True, dirty=False)
    inst._git_worktree = wt
    inst._tmux_session.detach_err = Exception("detach boom")
    monkeypatch.setattr(inst_mod.os, "stat", lambda p: None)
    with pytest.raises(RuntimeError) as ei:
        inst.Pause()
    # The detach error is surfaced, but the pause otherwise proceeds
    # (remove+prune ran) and the status was set to Paused before raising.
    assert "failed to detach tmux session: detach boom" in str(ei.value)
    assert "Remove" in wt.calls and "Prune" in wt.calls
    assert inst.Status == Status.Paused


def test_pause_missing_path_skips_remove(monkeypatch):
    inst = _started_instance()
    wt = FakeWorktree(valid=True, dirty=False)
    inst._git_worktree = wt

    def _stat(_p):
        raise OSError("gone")

    monkeypatch.setattr(inst_mod.os, "stat", _stat)
    inst.Pause()
    assert inst.Status == Status.Paused
    # Path did not exist -> Remove/Prune skipped.
    assert "Remove" not in wt.calls


# ---------------------------------------------------------------------------
# Resume — error branches
# ---------------------------------------------------------------------------
def test_resume_branch_check_failure_wrapped(_errorlog):
    inst = _started_instance()
    inst.Status = Status.Paused
    wt = FakeWorktree()
    wt.checked_out_exc = RuntimeError("check boom")
    inst._git_worktree = wt
    with pytest.raises(RuntimeError) as ei:
        inst.Resume()
    assert "failed to check if branch is checked out: check boom" in str(ei.value)


def test_resume_restore_failure_falls_back_to_new_session(_errorlog):
    inst = _started_instance()
    inst.Status = Status.Paused
    inst._git_worktree = FakeWorktree(checked_out=False)
    tmuxs = inst._tmux_session
    tmuxs.exists = True
    tmuxs.restore_err = Exception("restore boom")
    tmuxs.start_err = None
    inst.Resume()
    # Restore failed -> a new tmux session was started instead.
    assert inst.Status == Status.Running
    assert "restore" in tmuxs.calls
    assert any(isinstance(c, tuple) and c[0] == "start" for c in tmuxs.calls)


def test_resume_start_failure_cleanup_error_is_chained(_errorlog):
    inst = _started_instance()
    inst.Status = Status.Paused
    wt = FakeWorktree(checked_out=False)
    wt.cleanup_exc = RuntimeError("cleanup boom")
    inst._git_worktree = wt
    tmuxs = inst._tmux_session
    tmuxs.exists = False
    tmuxs.start_err = Exception("start boom")
    with pytest.raises(RuntimeError) as ei:
        inst.Resume()
    msg = str(ei.value)
    assert "failed to start new session" in msg
    assert "cleanup error: cleanup boom" in msg


# ---------------------------------------------------------------------------
# _provider_default_launch_args — success path
# ---------------------------------------------------------------------------
def test_provider_default_launch_args_success(monkeypatch):
    import types

    import backend.config.settings as settings_mod

    class _CC:
        def launch_args_for(self, provider):
            return "--foo --bar"

    monkeypatch.setattr(
        settings_mod, "load_settings", lambda: types.SimpleNamespace(coding_cli=_CC())
    )
    assert inst_mod._provider_default_launch_args("claude") == ("--foo", "--bar")


def test_provider_default_launch_args_empty_string_yields_empty(monkeypatch):
    import types

    import backend.config.settings as settings_mod

    class _CC:
        def launch_args_for(self, provider):
            return "   "

    monkeypatch.setattr(
        settings_mod, "load_settings", lambda: types.SimpleNamespace(coding_cli=_CC())
    )
    assert inst_mod._provider_default_launch_args("claude") == ()


# ---------------------------------------------------------------------------
# _InPlaceWorktree._detect — subprocess errors are swallowed
# ---------------------------------------------------------------------------
def test_in_place_detect_swallows_subprocess_errors(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise OSError("no git binary")

    monkeypatch.setattr(inst_mod.subprocess, "run", _boom)
    wt = inst_mod._InPlaceWorktree(
        repoPath=str(tmp_path),
        worktreePath=str(tmp_path),
        sessionName="s",
        branchName="",
    )
    # Detection failed silently: fields stay at their defaults.
    assert wt.branchName == ""
    assert wt.baseCommitSHA == ""


# ---------------------------------------------------------------------------
# _worktree_from_data — provisioned reconstruction
# ---------------------------------------------------------------------------
def test_worktree_from_data_rebuilds_provisioned_worktree(monkeypatch):
    from backend.session import provisioned as prov

    data = _instance_data(provisioned=True)
    data.workspace_strategy = "worktree"
    monkeypatch.setattr(prov, "settings_for_workspace", lambda repo: "SETTINGS")

    def _make(settings, **fields):
        return ("worktree-obj", settings, fields.get("branchName"))

    monkeypatch.setattr(prov, "ProvisionedWorktree", _make)
    got = inst_mod._worktree_from_data(data)
    assert got[0] == "worktree-obj"
    assert got[1] == "SETTINGS"
    assert got[2] == "feature-x"


def test_worktree_from_data_rebuilds_clone_worktree(monkeypatch):
    from backend.session import provisioned as prov

    data = _instance_data(provisioned=True)
    data.workspace_strategy = "clone"
    monkeypatch.setattr(prov, "settings_for_workspace", lambda repo: "S")
    monkeypatch.setattr(
        prov, "ProvisionedCloneWorktree", lambda settings, **f: ("clone-obj", settings)
    )
    got = inst_mod._worktree_from_data(data)
    assert got[0] == "clone-obj"


def test_worktree_from_data_provisioned_falls_back_on_error(monkeypatch, _errorlog):
    from backend.session import provisioned as prov

    data = _instance_data(provisioned=True)

    def _boom(repo):
        raise RuntimeError("config moved")

    monkeypatch.setattr(prov, "settings_for_workspace", _boom)
    # Falls back to a plain GitWorktree built from storage.
    got = inst_mod._worktree_from_data(data)
    from backend.session.git.worktree import GitWorktree

    assert isinstance(got, GitWorktree)
    assert got.GetBranchName() == "feature-x"


def test_worktree_from_data_plain_worktree():
    data = _instance_data(provisioned=False, in_place=False)
    got = inst_mod._worktree_from_data(data)
    from backend.session.git.worktree import GitWorktree

    assert isinstance(got, GitWorktree)
    assert got.GetRepoPath() == "/repo"
