"""Provisioned-workspace generalization: universality guarantees.

* ``.mindflock_*`` marker files drive stage detection and launcher resume;
* the guided commit endpoint works for PLAIN (non-provisioned) sessions and
  never imports the provisioned module for them;
* per-repo base clone dirs (``_base_<slug>``);
* ``local_settings_for`` makes provisioning available for ANY local repo.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from backend import workspace_setup
from backend.session import provisioned


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    return path


# --------------------------------------------------------------------------- #
# per-repo base clone dirname
# --------------------------------------------------------------------------- #
def test_base_repo_dirname_is_per_repo():
    assert (
        provisioned.base_repo_dirname("git@github.com:org/example-repo.git")
        == "_base_example-repo"
    )
    assert provisioned.base_repo_dirname("/home/me/My Repo") == "_base_my-repo"
    assert provisioned.base_repo_dirname("") == "_base_repo"
    # Distinct repos -> distinct base dirs.
    assert provisioned.base_repo_dirname(
        "x/alpha.git"
    ) != provisioned.base_repo_dirname("x/beta.git")


def test_is_base_repo_dirname():
    assert provisioned.is_base_repo_dirname("_base_example-repo")
    assert not provisioned.is_base_repo_dirname("pr-12")
    assert not provisioned.is_base_repo_dirname("feature-x")


def test_resolve_base_repo_dir_is_per_repo(tmp_path):
    ws = tmp_path / "workspaces"
    settings = provisioned.ProvisionSettings(
        repo_url="git@github.com:org/some-repo.git", workspace_dir=ws
    )
    assert provisioned.resolve_base_repo_dir(settings) == ws / "_base_some-repo"


def test_classify_workspace_marks_base_layout():
    from backend.web import server

    assert server._classify_workspace("_base_example-repo", "/ws") == "base"
    assert server._classify_workspace("_testmon_refresher", "/ws") == "refresher"
    assert server._classify_workspace("pr-7", "/ws") == "pr"


# --------------------------------------------------------------------------- #
# universal provisioning: any local repo
# --------------------------------------------------------------------------- #
def test_local_settings_for_any_repo(tmp_path):
    repo = _init_repo(tmp_path / "myproj")
    s = provisioned.local_settings_for(repo)
    assert s is not None
    assert s.repo_url == str(repo.resolve())
    # base_branch tracks the repo's current branch, whatever it is named.
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert s.base_branch == head
    # Local repos get auto-detected setup and NO shared cache seeds.
    assert s.setup_commands is None
    assert s.caches == []
    # A non-repo dir is refused.
    assert provisioned.local_settings_for(tmp_path / "not-a-repo") is None


# --------------------------------------------------------------------------- #
# marker files
# --------------------------------------------------------------------------- #
class _FakeInst:
    Title = "t"
    Branch = ""

    def __init__(self, wt: str):
        self._wt = wt
        from backend.session.storage import Status

        self.Status = Status.Running

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


class _RaisingInst:
    """A sibling whose engine accessors blow up mid-scan — the shared-worktree
    liveness probe must step over it, not abort."""

    Title = "boom"

    def GetWorktreePath(self):  # noqa: N802
        raise RuntimeError("engine snapshot went sideways")


def test_stage_detects_precommit_lock(tmp_path, monkeypatch):
    from backend.web import server

    (tmp_path / ".mindflock_precommit.lock").write_text("")
    # Lock present AND the commit is genuinely running in the shell -> precommit.
    monkeypatch.setattr(server, "_precommit_lock_is_live", lambda title: True)
    res = server._session_stage(_FakeInst(str(tmp_path)))
    assert res["stage"] == "precommit"


def test_stage_self_heals_stale_precommit_lock(tmp_path, monkeypatch):
    """A cancelled/killed commit leaves the lock behind (its own `rm` never
    fired). Once the shell has been idle CONTINUOUSLY past the debounce window the
    stage self-heals: the stale lock is cleared and detection falls through to a
    re-committable stage. But a SINGLE not-live reading must NOT heal — the probe
    reads False on any transient tmux/ps hiccup, and healing on one blip stranded
    a live commit's pill on "idle" (the regression this guards)."""
    import time
    from backend.web import server
    from backend.web.core import agent_state as A

    A._LOCK_STALE_SINCE.clear()
    lock_path = tmp_path / ".mindflock_precommit.lock"
    lock_path.write_text("")
    # Age the lock past the startup grace so the liveness probe is consulted (a
    # fresh lock is always treated as live — see the race-fix test below).
    old = time.time() - (server._PRECOMMIT_LOCK_GRACE_S + 5.0)
    os.utime(lock_path, (old, old))
    # Shell idle -> commit no longer running -> lock reads stale.
    monkeypatch.setattr(server, "_precommit_lock_is_live", lambda title: False)
    # Drive the debounce clock explicitly (no real sleeping).
    now = {"t": time.time()}
    monkeypatch.setattr(A.time, "time", lambda: now["t"])

    # First not-live read: debounce starts, pill STAYS precommit, lock kept.
    res = server._session_stage(_FakeInst(str(tmp_path)))
    assert res["stage"] == "precommit"
    assert lock_path.exists()

    # Still not-live past the heal window -> now genuinely abandoned -> self-heal.
    now["t"] += A._PRECOMMIT_LOCK_STALE_HEAL_S + 1.0
    os.utime(  # keep it older than grace relative to the advanced clock
        lock_path, (now["t"] - (server._PRECOMMIT_LOCK_GRACE_S + 5.0),) * 2
    )
    res = server._session_stage(_FakeInst(str(tmp_path)))
    assert res["stage"] != "precommit"
    assert not lock_path.exists()  # self-healed so it can't re-wedge later
    A._LOCK_STALE_SINCE.clear()


def test_stage_precommit_survives_sibling_committing(tmp_path, monkeypatch):
    """Sessions sharing a worktree share ONE git-dir lock. While one is
    committing, the OTHER (idle) session's stage probe must not self-heal the lock
    out from under it: the committing sibling's live shell keeps the pill up. This
    guards the multi-session-per-worktree drop-to-idle."""
    import time
    from backend.web import server
    from backend.web.core import agent_state as A

    A._LOCK_STALE_SINCE.clear()
    lock_path = tmp_path / ".mindflock_precommit.lock"
    lock_path.write_text("")
    old = time.time() - (server._PRECOMMIT_LOCK_GRACE_S + 5.0)
    os.utime(lock_path, (old, old))  # stale -> liveness consulted

    idle = _FakeInst(str(tmp_path))
    idle.Title = "idle"
    busy = _FakeInst(str(tmp_path))  # SAME worktree -> shares the lock
    busy.Title = "busy"
    monkeypatch.setitem(server.ENGINE.instances, "idle", idle)
    monkeypatch.setitem(server.ENGINE.instances, "busy", busy)
    # Only 'busy' shows a live commit shell.
    monkeypatch.setattr(
        server, "_precommit_lock_is_live", lambda title: title == "busy"
    )

    res = server._session_stage(idle)
    assert res["stage"] == "precommit"  # NOT healed away by the idle sibling
    assert lock_path.exists()
    A._LOCK_STALE_SINCE.clear()


def test_stage_debounce_resets_when_lock_reads_live_again(tmp_path, monkeypatch):
    """The stale-debounce timer is armed by the FIRST not-live read and torn
    down the moment the lock reads live again. A later not-live read must then
    restart the FULL heal window from scratch rather than inheriting the earlier
    timer and healing immediately — otherwise a commit that briefly blipped
    not-live would be one blip away from a premature self-heal forever."""
    import time
    from backend.web import server
    from backend.web.core import agent_state as A

    A._LOCK_STALE_SINCE.clear()
    inst = _FakeInst(str(tmp_path))
    inst.Title = "flapper"
    lock_path = tmp_path / ".mindflock_precommit.lock"
    lock_path.write_text("")
    now = {"t": time.time()}
    monkeypatch.setattr(A.time, "time", lambda: now["t"])
    # Keep the lock permanently older than the startup grace relative to the
    # (only-ever-advancing) mocked clock, so the liveness probe is consulted.
    old = now["t"] - (server._PRECOMMIT_LOCK_GRACE_S + 5.0)
    os.utime(lock_path, (old, old))

    live = {"v": False}
    monkeypatch.setattr(server, "_precommit_lock_is_live", lambda title: live["v"])

    # First not-live read arms the debounce timer.
    res = server._session_stage(inst)
    assert res["stage"] == "precommit"
    assert inst.Title in A._LOCK_STALE_SINCE

    # A LIVE read must pop the title — the timer resets the instant it reads live.
    live["v"] = True
    res = server._session_stage(inst)
    assert res["stage"] == "precommit"
    assert inst.Title not in A._LOCK_STALE_SINCE

    # Now go not-live again, PAST what the original window would have been. The
    # window must restart (fresh timer at "now"), NOT heal on this read.
    now["t"] += A._PRECOMMIT_LOCK_STALE_HEAL_S + 100.0
    live["v"] = False
    res = server._session_stage(inst)
    assert res["stage"] == "precommit"  # fresh window, not an immediate heal
    assert lock_path.exists()
    assert A._LOCK_STALE_SINCE[inst.Title] == now["t"]  # timer restarted here
    A._LOCK_STALE_SINCE.clear()


def test_stage_stale_lock_stays_precommit_within_heal_window(tmp_path, monkeypatch):
    """A SECOND-or-later not-live read still INSIDE the heal window keeps the pill
    at "pre-commit" without re-arming the debounce timer or healing the lock.

    The first not-live read arms the timer; subsequent reads before the window
    elapses must simply hold the pill (this is the middle debounce branch — only
    a >=2nd consecutive not-live read within the window exercises it, distinct
    from the first-read arm and the post-window self-heal)."""
    import time
    from backend.web import server
    from backend.web.core import agent_state as A

    A._LOCK_STALE_SINCE.clear()
    inst = _FakeInst(str(tmp_path))
    inst.Title = "slowcommit"
    lock_path = tmp_path / ".mindflock_precommit.lock"
    lock_path.write_text("")
    now = {"t": time.time()}
    monkeypatch.setattr(A.time, "time", lambda: now["t"])
    # Keep the lock older than the startup grace relative to the mocked clock so
    # the liveness probe (always not-live here) is what decides.
    old = now["t"] - (server._PRECOMMIT_LOCK_GRACE_S + 5.0)
    os.utime(lock_path, (old, old))
    monkeypatch.setattr(server, "_precommit_lock_is_live", lambda title: False)

    # First not-live read arms the debounce timer.
    assert server._session_stage(inst)["stage"] == "precommit"
    armed = A._LOCK_STALE_SINCE[inst.Title]

    # A later not-live read still inside the window -> stays precommit; the timer
    # stays exactly where the first read set it (no re-arm) and the lock is kept.
    now["t"] += A._PRECOMMIT_LOCK_STALE_HEAL_S - 1.0
    os.utime(lock_path, (now["t"] - (server._PRECOMMIT_LOCK_GRACE_S + 5.0),) * 2)
    assert server._session_stage(inst)["stage"] == "precommit"
    assert lock_path.exists()
    assert A._LOCK_STALE_SINCE[inst.Title] == armed  # timer unchanged, not healed
    A._LOCK_STALE_SINCE.clear()


def test_stage_missing_lock_clears_stale_timer(tmp_path, monkeypatch):
    """When the lock file is gone there is nothing to debounce, so a lingering
    stale timer for that title must be dropped (the `else` branch) — otherwise a
    future commit's first not-live read would inherit an ancient timestamp and
    self-heal a live lock on the very first poll."""
    import time
    from backend.web import server
    from backend.web.core import agent_state as A

    A._LOCK_STALE_SINCE.clear()
    inst = _FakeInst(str(tmp_path))
    inst.Title = "cleaner"
    # No lock file on disk, but a stale timer lingers from a prior commit.
    assert not (tmp_path / ".mindflock_precommit.lock").exists()
    A._LOCK_STALE_SINCE[inst.Title] = time.time() - 10_000.0

    server._session_stage(inst)
    assert inst.Title not in A._LOCK_STALE_SINCE  # forgotten -> next commit clean
    A._LOCK_STALE_SINCE.clear()


def test_commit_shell_live_ignores_sibling_on_other_worktree(tmp_path, monkeypatch):
    """`_commit_shell_live` only counts siblings pointing at the SAME worktree
    path. A busy commit in an unrelated worktree must not keep this worktree's
    lock alive — the complement to the shared-worktree survival case."""
    from backend.web import server
    from backend.web.core import agent_state as A

    wt_a = tmp_path / "a"
    wt_b = tmp_path / "b"
    wt_a.mkdir()
    wt_b.mkdir()
    own = _FakeInst(str(wt_a))
    own.Title = "self"
    busy = _FakeInst(str(wt_b))  # DIFFERENT worktree
    busy.Title = "busy"
    monkeypatch.setitem(server.ENGINE.instances, "self", own)
    monkeypatch.setitem(server.ENGINE.instances, "busy", busy)
    monkeypatch.setattr(
        server, "_precommit_lock_is_live", lambda title: title == "busy"
    )

    # Own shell is quiet and the only live commit is in an unrelated worktree.
    assert A._commit_shell_live(own, str(wt_a)) is False


def test_commit_shell_live_skips_broken_sibling(tmp_path, monkeypatch):
    """One instance whose engine accessors raise must be stepped over, not abort
    the whole scan: the polling session's own shell is still probed first and a
    healthy same-worktree sibling further down the list is still consulted."""
    from backend.web import server
    from backend.web.core import agent_state as A

    own = _FakeInst(str(tmp_path))
    own.Title = "self"
    good = _FakeInst(str(tmp_path))  # SAME worktree, live commit
    good.Title = "good"
    monkeypatch.setitem(server.ENGINE.instances, "self", own)
    monkeypatch.setitem(server.ENGINE.instances, "boom", _RaisingInst())
    monkeypatch.setitem(server.ENGINE.instances, "good", good)

    probed = []
    monkeypatch.setattr(
        server,
        "_precommit_lock_is_live",
        lambda title: probed.append(title) or (title == "good"),
    )

    # The broken sibling doesn't crash the scan; the live 'good' sibling wins.
    assert A._commit_shell_live(own, str(tmp_path)) is True
    assert probed[0] == "self"  # own shell probed first
    assert "good" in probed  # scan continued past the broken instance
    assert "boom" not in probed  # broken sibling never contributed a title


def test_commit_shell_live_short_circuits_on_own_shell(tmp_path, monkeypatch):
    """The single-session common case stays a single probe: when the polling
    session's own shell reads live, `_commit_shell_live` returns True without
    probing any sibling."""
    from backend.web import server
    from backend.web.core import agent_state as A

    own = _FakeInst(str(tmp_path))
    own.Title = "self"
    sib = _FakeInst(str(tmp_path))  # same worktree, would also be live
    sib.Title = "sib"
    monkeypatch.setitem(server.ENGINE.instances, "self", own)
    monkeypatch.setitem(server.ENGINE.instances, "sib", sib)

    probed = []
    monkeypatch.setattr(
        server,
        "_precommit_lock_is_live",
        lambda title: probed.append(title) or True,
    )

    assert A._commit_shell_live(own, str(tmp_path)) is True
    assert probed == ["self"]  # returned on the own-shell probe, sibling untouched


def test_stage_precommit_survives_commit_startup_race(tmp_path, monkeypatch):
    """A just-touched lock reads "pre-commit" even when the liveness probe says
    "not live" — the commit chain forks git a beat after the lock appears, and the
    first poll must not delete the lock and drop the stage to idle (the regression
    this guards against)."""
    from backend.web import server

    lock_path = tmp_path / ".mindflock_precommit.lock"
    lock_path.write_text("")  # mtime = now -> inside the grace window
    # Probe would say "not live" (git hasn't forked yet), but grace must win.
    monkeypatch.setattr(server, "_precommit_lock_is_live", lambda title: False)
    res = server._session_stage(_FakeInst(str(tmp_path)))
    assert res["stage"] == "precommit"
    assert lock_path.exists()  # NOT deleted — the commit is still starting


def test_precommit_lock_liveness(monkeypatch):
    """The liveness probe: a non-shell foreground (or a non-shell descendant of
    an idle shell) means the commit is running; a bare-shell prompt with nothing
    underneath, or a missing shell session, means it's stale."""
    from backend.web import server

    monkeypatch.setattr(server, "_shell_tmux_name", lambda title: "sh")

    # No shell session -> stale.
    monkeypatch.setattr(server, "_live_session_name", lambda name: None)
    assert server._precommit_lock_is_live("t") is False

    monkeypatch.setattr(server, "_live_session_name", lambda name: name)

    # git in the foreground -> live commit.
    monkeypatch.setattr(server, "_pane_meta", lambda name: ("git", 0.0, "42", "80x24"))
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: False)
    assert server._precommit_lock_is_live("t") is True

    # Bare shell foreground but a hook child is alive -> still live.
    monkeypatch.setattr(server, "_pane_meta", lambda name: ("bash", 0.0, "42", "80x24"))
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: True)
    assert server._precommit_lock_is_live("t") is True

    # Bare shell prompt, nothing underneath -> stale.
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: False)
    assert server._precommit_lock_is_live("t") is False


def test_precommit_lock_path_resolves_into_git_dir(tmp_path):
    """The lock lives inside the worktree's private git dir so in-tree tooling
    (``git add -A`` / ``git clean -fdx`` / nested agents) can't sweep it while a
    commit's hooks run — the regression that dropped the stage pill to "idle"."""
    from backend.web import server

    repo = _init_repo(tmp_path / "wt")
    lock = server._precommit_lock_path(str(repo))
    # Sits directly in a .git dir (path canonicalization / symlinks aside), and
    # is NOT the old worktree-root scratch path.
    assert os.path.basename(lock) == "mindflock_precommit.lock"
    assert os.path.basename(os.path.dirname(lock)) == ".git"
    assert lock != os.path.join(str(repo), ".mindflock_precommit.lock")


def test_precommit_lock_path_falls_back_when_not_a_repo(tmp_path):
    """A non-repo path can't resolve a git dir — fall back to the legacy
    worktree-root location rather than raising."""
    from backend.web import server

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert server._precommit_lock_path(str(plain)) == os.path.join(
        str(plain), ".mindflock_precommit.lock"
    )


def test_stage_detects_commit_status(tmp_path, monkeypatch):
    from backend.web import server

    (tmp_path / ".mindflock_commit_status").write_text("1")
    monkeypatch.setattr(server, "_is_dirty", lambda wt: True)
    monkeypatch.setattr(server, "_failed_precommit_step", lambda title: "ruff")
    res = server._session_stage(_FakeInst(str(tmp_path)))
    assert res["stage"] == "interrupt"


def test_launcher_resume_gated_on_started_marker(tmp_path):
    """The generated launcher script resumes (not re-seeds) once the
    .mindflock_started marker exists."""
    path = provisioned.write_launcher(str(tmp_path), "prompt", program="claude")
    txt = Path(path).read_text()
    assert os.path.basename(path) == ".mindflock_launch.sh"
    assert "[ -f .mindflock_started ]" in txt
    assert ": > .mindflock_started" in txt
    assert (tmp_path / ".mindflock_prompt.md").read_text() == "prompt"


def test_workspace_artifacts_are_git_excluded(tmp_path):
    repo = _init_repo(tmp_path / "r")
    workspace_setup.exclude_artifacts(repo)
    excl = (repo / ".git" / "info" / "exclude").read_text()
    for name in (
        ".mindflock_precommit.lock",
        ".mindflock_commit_status",
        ".mindflock_commit_msg",
        ".mindflock_launch.sh",
        ".testmondata",
    ):
        assert name in excl


# --------------------------------------------------------------------------- #
# universal pre-commit: the commit endpoint works for a PLAIN session
# --------------------------------------------------------------------------- #
def test_commit_endpoint_universal_for_plain_sessions(tmp_path, monkeypatch):
    from backend.web import server

    repo = _init_repo(tmp_path / "plain")
    inst = _FakeInst(str(repo))
    sent = {}
    monkeypatch.setitem(server.ENGINE.instances, "plain-t", inst)
    monkeypatch.setattr(server, "_ensure_shell_session", lambda t, wt: ("sh", None))
    monkeypatch.setattr(
        server, "_send_to_shell", lambda name, cmd: sent.update(cmd=cmd)
    )

    resp = asyncio.run(server.instance_commit("plain-t", {"message": "msg x"}))
    assert resp.status_code == 200

    cmd = sent["cmd"]
    # The lock lives in the PRIVATE git dir (not the worktree root) so nothing
    # running in the tree can sweep it while the hooks run.
    assert 'L="$(git rev-parse --absolute-git-dir)/mindflock_precommit.lock"' in cmd
    assert 'touch "$L"' in cmd
    assert "echo $rc > .mindflock_commit_status" in cmd
    assert 'rm -f "$L"' in cmd
    assert "git commit -F .mindflock_commit_msg" in cmd
    # The message file was persisted for re-commit reuse.
    assert (repo / ".mindflock_commit_msg").read_text() == "msg x"
    # The scratch artifacts were git-excluded on this PLAIN repo (no
    # provisioning involved).
    excl = (repo / ".git" / "info" / "exclude").read_text()
    assert ".mindflock_commit_msg" in excl


def test_commit_endpoint_reuses_prior_message_file(tmp_path, monkeypatch):
    from backend.web import server

    repo = _init_repo(tmp_path / "old-ws")
    (repo / ".mindflock_commit_msg").write_text("old message")
    inst = _FakeInst(str(repo))
    sent = {}
    monkeypatch.setitem(server.ENGINE.instances, "old-t", inst)
    monkeypatch.setattr(server, "_ensure_shell_session", lambda t, wt: ("sh", None))
    monkeypatch.setattr(
        server, "_send_to_shell", lambda name, cmd: sent.update(cmd=cmd)
    )

    # No message in the payload -> the prior message file is reused.
    resp = asyncio.run(server.instance_commit("old-t", {}))
    assert resp.status_code == 200
    assert (repo / ".mindflock_commit_msg").read_text() == "old message"


# --------------------------------------------------------------------------- #
# ensure_base_repo self-heals a base clone that got flipped to bare
# --------------------------------------------------------------------------- #
def _init_origin_on(path: Path, branch: str) -> Path:
    """A local 'origin' repo with one commit on ``branch`` to clone from."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "add",
            "README.md",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    return path


def test_ensure_base_repo_heals_bare_base(tmp_path):
    """A canonical base clone that got `core.bare=true` (breaks worktree-add and
    `git rev-parse --show-toplevel`, wedging provisioning) is restored to a
    normal working clone on the next ensure_base_repo call."""
    origin = _init_origin_on(tmp_path / "origin", "main")
    settings = provisioned.ProvisionSettings(
        repo_url=str(origin),
        workspace_dir=tmp_path / "workspaces",
        base_branch="main",
    )

    # First call clones the base as a normal (non-bare) working clone.
    base = Path(provisioned.ensure_base_repo(settings))
    assert (
        subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--is-bare-repository"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "false"
    )

    # Simulate the corruption we saw in the field.
    subprocess.run(["git", "-C", str(base), "config", "core.bare", "true"], check=True)
    assert (
        subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )

    # ensure_base_repo self-heals: back to non-bare and resolvable again, so
    # resolve_worktree_paths (find_git_repo_root -> --show-toplevel) works.
    healed = Path(provisioned.ensure_base_repo(settings))
    assert healed == base
    assert (
        subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--is-bare-repository"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "false"
    )
    from backend.session.git.util import find_git_repo_root

    assert find_git_repo_root(str(base)) == str(base.resolve())


def test_ensure_base_repo_falls_back_when_base_branch_missing(tmp_path):
    """When the configured base_branch is absent on origin the fast
    ``--branch`` clone fails, and ensure_base_repo falls back to a plain full
    clone, leaving the base repo on its default branch (never raising)."""
    origin = _init_origin_on(tmp_path / "origin", "main")
    settings = provisioned.ProvisionSettings(
        repo_url=str(origin),
        workspace_dir=tmp_path / "workspaces",
        base_branch="does-not-exist",
    )
    base = Path(provisioned.ensure_base_repo(settings))
    assert (base / ".git").is_dir()
    head = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "main"  # fell back to origin's default branch


def test_ensure_base_repo_refresh_tolerates_missing_base_branch(tmp_path):
    """A refresh whose base_branch no longer resolves on origin (fetch +
    checkout both fail) leaves the existing base clone on its current HEAD
    rather than raising — the best-effort refresh path."""
    origin = _init_origin_on(tmp_path / "origin", "main")
    ws = tmp_path / "workspaces"
    base = Path(
        provisioned.ensure_base_repo(
            provisioned.ProvisionSettings(
                repo_url=str(origin), workspace_dir=ws, base_branch="main"
            )
        )
    )
    # Second call takes the refresh path (base/.git exists) but the branch is
    # absent on origin: it must not raise and must keep the same base dir.
    healed = Path(
        provisioned.ensure_base_repo(
            provisioned.ProvisionSettings(
                repo_url=str(origin), workspace_dir=ws, base_branch="ghost"
            )
        )
    )
    assert healed == base
    assert (base / ".git").is_dir()


def test_load_provision_settings_tolerates_malformed_toml(tmp_path):
    """A config.toml that fails to parse is logged and treated as empty; with a
    repo_url override supplied, provisioning settings still resolve."""
    bad = tmp_path / "config.toml"
    bad.write_text("this is = = not valid toml [[[\n")
    s = provisioned.load_provision_settings(
        config_path=bad, repo_url_override="git@github.com:org/x.git"
    )
    assert s is not None
    assert s.repo_url == "git@github.com:org/x.git"


# --------------------------------------------------------------------------- #
# Deterministic branch naming (slugify / branch_name_for)
#
# Branch names become durable git refs and appear in state.json, so the
# Shortcut scheme, the mindflock/<title> fallback, story-id sanitization, and
# the 'story' collapse are pinned as pure-function contracts.
# --------------------------------------------------------------------------- #
def test_branch_name_for_shortcut_scheme():
    assert (
        provisioned.branch_name_for(20005, "Test Story")
        == "feature/sc-20005/test-story"
    )


def test_branch_name_for_collapses_slug_equal_to_sc_id():
    # A slug that just repeats 'sc-<id>' falls back to 'story'.
    assert (
        provisioned.branch_name_for("20005", "Ignored", name="sc-20005")
        == "feature/sc-20005/story"
    )


def test_branch_name_for_collapses_bare_id_slug():
    # A slug equal to the bare id also collapses to 'story'.
    assert provisioned.branch_name_for(20005, "20005") == "feature/sc-20005/story"


def test_branch_name_for_without_story_id_uses_mindflock_scheme():
    assert provisioned.branch_name_for(None, "My Thing") == "mindflock/my-thing"


def test_slugify_basic_lowercase_dash_collapse():
    assert provisioned.slugify("Test Story") == "test-story"
    assert provisioned.slugify("A  B---C") == "a-b-c"


def test_slugify_empty_defaults_to_story():
    assert provisioned.slugify("") == "story"
    assert provisioned.slugify("!!!") == "story"


def test_slugify_truncates_and_strips_trailing_dash():
    # Truncation at max_len must never leave a trailing dash.
    s = provisioned.slugify("a" * 30 + " " + "b" * 30, max_len=31)
    assert len(s) <= 31
    assert not s.endswith("-")
    assert s == "a" * 30
