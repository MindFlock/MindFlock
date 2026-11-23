"""A development checkout updates itself from its own branch (core.dev_update).

Everything here runs against throwaway git repos under ``tmp_path``: a bare
``origin.git``, a ``seed`` clone that publishes new commits to it, and the
``work`` clone that plays the server's checkout. The installer refresh is a
stub script; nothing touches the real checkout, its venv or a real server.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from backend.web.core import dev_update
from backend.web.core import self_update

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(cwd, *args) -> str:
    cp = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ, **_ENV),
    )
    return cp.stdout.strip()


def _pyproject(version: str) -> str:
    return '[project]\nname = "mindflock"\nversion = "%s"\n' % version


class Repos:
    def __init__(self, root: Path):
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.work = root / "work"
        _git(root, "init", "--bare", "-b", "main", str(self.origin))
        _git(root, "clone", str(self.origin), str(self.seed))
        (self.seed / "pyproject.toml").write_text(_pyproject("0.1.0"))
        (self.seed / "a.txt").write_text("one\n")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "one")
        _git(self.seed, "push", "origin", "main")
        _git(root, "clone", str(self.origin), str(self.work))

    def publish(self, version: str = "", text: str = "more\n") -> str:
        """A new commit on origin/main (optionally a version bump)."""
        with open(self.seed / "a.txt", "a") as fh:
            fh.write(text)
        if version:
            (self.seed / "pyproject.toml").write_text(_pyproject(version))
        _git(self.seed, "commit", "-am", "next")
        _git(self.seed, "push", "origin", "main")
        return _git(self.seed, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git(self.work, "rev-parse", "HEAD")


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_update, "_REMOTE_AT", {})
    return Repos(tmp_path)


# --------------------------------------------------------------------------- #
# Where a checkout stands
# --------------------------------------------------------------------------- #
def test_an_up_to_date_checkout_on_main_can_update_and_has_nothing_new(repos):
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["branch"] == "main" and st["default_branch"] == "main"
    assert st["behind"] == 0 and st["ahead"] == 0 and st["dirty"] is False
    assert st["can_update"] is True and st["why_not"] == ""
    assert st["upstream"] == "origin/main"
    assert set(dev_update.public(st)) == set(dev_update.PUBLIC_KEYS)


def test_new_commits_on_the_remote_are_found_and_fetched(repos):
    tip = repos.publish(version="0.2.0")
    # Without a remote look only local refs are read: nothing new yet.
    assert dev_update.status(str(repos.work))["behind"] == 0
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["behind"] == 1 and st["target"] == tip
    assert st["target_version"] == "0.2.0"
    assert st["can_update"] is True


def test_the_remote_look_is_throttled_unless_forced(repos):
    dev_update.status(str(repos.work), refresh=True, force=True)
    repos.publish()
    assert dev_update.status(str(repos.work), refresh=True)["behind"] == 0
    assert dev_update.status(str(repos.work), refresh=True, force=True)["behind"] == 1


def test_another_branch_is_quiet_with_one_plain_sentence(repos):
    _git(repos.work, "checkout", "-b", "foo")
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is False
    assert st["why_not"] == (
        "This is a development checkout on branch foo. Updates apply when it "
        "is on main with nothing uncommitted."
    )


def test_uncommitted_changes_block_the_update(repos):
    (repos.work / "a.txt").write_text("edited\n")
    st = dev_update.status(str(repos.work))
    assert st["dirty"] is True and st["can_update"] is False
    assert "uncommitted changes" in st["why_not"]
    # An untracked file is not "uncommitted changes to tracked files".
    _git(repos.work, "checkout", "--", "a.txt")
    (repos.work / "scratch.txt").write_text("x")
    assert dev_update.status(str(repos.work))["can_update"] is True


def test_a_diverged_checkout_cannot_move_forward_by_itself(repos):
    repos.publish()
    (repos.work / "b.txt").write_text("mine\n")
    _git(repos.work, "add", "b.txt")
    _git(repos.work, "commit", "-m", "local")
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["ahead"] == 1 and st["behind"] == 1
    assert st["can_update"] is False and "cannot move forward" in st["why_not"]


# --------------------------------------------------------------------------- #
# No branch checked out (a detached HEAD)
# --------------------------------------------------------------------------- #
def _detach_at(repos, rev="HEAD", drop_main=False):
    _git(repos.work, "checkout", "-q", "--detach", rev)
    if drop_main:
        _git(repos.work, "branch", "-D", "main")
    return _git(repos.work, "rev-parse", "HEAD")


def _branch_of(repos) -> str:
    cp = subprocess.run(
        ["git", "symbolic-ref", "-q", "--short", "HEAD"],
        cwd=str(repos.work),
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip()


def _local_main(repos) -> str:
    cp = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "refs/heads/main"],
        cwd=str(repos.work),
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip()


def test_detached_at_the_tip_can_update_and_has_nothing_new(repos):
    _detach_at(repos)
    st = dev_update.status(str(repos.work))
    assert st["branch"] == "" and st["can_update"] is True and st["why_not"] == ""
    assert st["attach"] == {"branch": "main", "create": False, "from": st["head"]}


def test_detached_at_an_ancestor_with_no_local_main_can_update(repos):
    """The ML-Rig-2023 case: detached exactly at a release commit that is an
    ancestor of origin/main, tracked files clean, no local main."""
    old = _detach_at(repos, drop_main=True)
    tip = repos.publish(version="0.2.0")
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is True and st["why_not"] == ""
    assert st["behind"] == 1 and st["target"] == tip and st["head"] == old
    assert st["attach"] == {"branch": "main", "create": True, "from": ""}
    # Looking changes nothing.
    assert _branch_of(repos) == "" and _local_main(repos) == ""


def test_detached_at_a_commit_not_on_main_is_refused_with_what_to_do(repos):
    _detach_at(repos)
    (repos.work / "b.txt").write_text("mine\n")
    _git(repos.work, "add", "b.txt")
    _git(repos.work, "commit", "-m", "a local commit on no branch")
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is False and st["attach"] is None
    assert st["why_not"] == (
        "This development checkout has no branch checked out and is at a "
        "commit that is not on main; switch it to main (git switch main) to "
        "get updates."
    )


def test_detached_with_uncommitted_tracked_changes_is_refused(repos):
    _detach_at(repos, drop_main=True)
    repos.publish()
    (repos.work / "a.txt").write_text("edited\n")
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is False and st["attach"] is None
    assert "uncommitted changes" in st["why_not"]
    assert "git switch main" in st["why_not"]
    # Untracked files are not "uncommitted changes to tracked files".
    _git(repos.work, "checkout", "--", "a.txt")
    (repos.work / "scratch.txt").write_text("x")
    assert dev_update.status(str(repos.work))["can_update"] is True


def test_detached_with_a_local_main_that_is_ahead_is_refused(repos):
    first = repos.head()
    (repos.work / "b.txt").write_text("mine\n")
    _git(repos.work, "add", "b.txt")
    _git(repos.work, "commit", "-m", "only on local main")
    _detach_at(repos, first)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is False and st["attach"] is None
    assert "its own main has commits that are not here" in st["why_not"]


def test_detached_ahead_of_a_local_main_that_is_behind_can_update(repos):
    c2 = repos.publish()
    tip = repos.publish()
    _git(repos.work, "fetch", "-q", "origin")
    first = _local_main(repos)
    _detach_at(repos, c2)
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is True
    assert st["attach"] == {"branch": "main", "create": False, "from": first}
    assert st["target"] == tip


def test_the_script_attaches_then_fast_forwards(repos, tmp_path, monkeypatch):
    old = _detach_at(repos, drop_main=True)
    tip = repos.publish(version="0.2.0")
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    before = {"app": "mindflock", "version": "0.1.0", "commit": old}
    after = {"app": "mindflock", "version": "0.2.0", "commit": tip}
    cp, state, refreshes = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[before, after]
    )
    assert cp.returncode == 0, cp.stderr
    assert state["state"] == "done" and state["healthy"] is True
    assert _branch_of(repos) == "main" and repos.head() == tip
    upstream = _git(repos.work, "rev-parse", "--abbrev-ref", "main@{upstream}")
    assert upstream == "origin/main"
    assert len(refreshes) == 1


def test_the_script_rollback_restores_no_branch_at_the_old_commit(
    repos, tmp_path, monkeypatch
):
    old = _detach_at(repos, drop_main=True)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    before = {"app": "mindflock", "version": "0.1.0", "commit": old}
    cp, state, refreshes = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[before, None]
    )
    assert state["state"] == "rolled_back", (cp.stdout, cp.stderr)
    assert repos.head() == old and _branch_of(repos) == ""
    # The branch the update created is gone again.
    assert _local_main(repos) == ""
    assert len(refreshes) == 2


def test_the_script_rollback_puts_an_existing_local_main_back(
    repos, tmp_path, monkeypatch
):
    c2 = repos.publish()
    repos.publish()
    _git(repos.work, "fetch", "-q", "origin")
    first = _local_main(repos)
    _detach_at(repos, c2)
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    before = {"app": "mindflock", "version": "0.1.0", "commit": c2}
    cp, state, _ = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[before, None]
    )
    assert state["state"] == "rolled_back"
    assert repos.head() == c2 and _branch_of(repos) == ""
    assert _local_main(repos) == first


def test_a_failed_refresh_after_the_attach_restores_no_branch(
    repos, tmp_path, monkeypatch
):
    old = _detach_at(repos, drop_main=True)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    cp, state, _ = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[None], refresh_rc=5
    )
    assert state["state"] == "failed" and "moved back" in state["error"]
    assert repos.head() == old and _branch_of(repos) == ""
    assert _local_main(repos) == ""


def test_a_branch_that_appeared_while_it_waited_stops_it(repos, tmp_path, monkeypatch):
    old = _detach_at(repos, drop_main=True)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    _git(repos.work, "branch", "main", old)  # someone made it meanwhile
    cp, state, refreshes = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[None]
    )
    assert state["state"] == "failed" and "nothing was changed" in state["error"]
    assert repos.head() == old and _branch_of(repos) == "" and refreshes == []


def test_switching_to_a_branch_while_it_waited_stops_it(repos, tmp_path, monkeypatch):
    _detach_at(repos)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    _git(repos.work, "checkout", "-q", "main")
    cp, state, _ = _run_dev_script(tmp_path, monkeypatch, st, curl_bodies=[None])
    assert state["state"] == "failed" and "on a branch now" in state["error"]


def test_apply_here_attaches_and_move_back_detaches(repos, monkeypatch, capsys):
    old = _detach_at(repos, drop_main=True)
    tip = repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    monkeypatch.setattr(dev_update, "refresh_command", lambda checkout, python="": None)
    assert dev_update.apply_here(st) == (True, "")
    assert repos.head() == tip and _branch_of(repos) == "main"
    assert dev_update.move_back(str(repos.work), old, attach=st["attach"]) is True
    assert repos.head() == old and _branch_of(repos) == "" and _local_main(repos) == ""


def test_apply_here_refuses_when_the_checkout_moved(repos, monkeypatch):
    _detach_at(repos, drop_main=True)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    _git(repos.work, "checkout", "-q", "-b", "elsewhere")
    ok, why = dev_update.apply_here(st)
    assert ok is False and "nothing was changed" in why
    assert _local_main(repos) == ""


def test_the_default_branch_comes_from_origin_head(repos):
    assert dev_update.default_branch(str(repos.work)) == "main"


def test_not_a_checkout_answers_none(monkeypatch):
    monkeypatch.setattr(self_update, "install_checkout", lambda: "")
    assert dev_update.status() is None and dev_update.public(None) is None


# --------------------------------------------------------------------------- #
# The refresh never removes packages
# --------------------------------------------------------------------------- #
def test_the_refresh_adds_and_never_syncs(monkeypatch):
    monkeypatch.setattr(dev_update.shutil, "which", lambda *a, **k: "/x/uv")
    argv = dev_update.refresh_command("/src/mf", python="/venv/bin/python")
    assert argv[:3] == ["/x/uv", "pip", "install"]
    assert "sync" not in argv and "--reinstall-package" in argv
    assert argv[-2:] == ["-e", "/src/mf[web]"]
    assert "/venv/bin/python" in argv


# --------------------------------------------------------------------------- #
# The update script (the server's detached path)
# --------------------------------------------------------------------------- #
def _run_dev_script(tmp_path, monkeypatch, st, *, curl_bodies, refresh_rc=0):
    """Generate self_update's script for ``st`` and run it with a stub
    refresh (records its calls, exits ``refresh_rc``) and a stub ``curl``
    answering ``curl_bodies`` in turn (None = down; the last one repeats)."""
    statedir = tmp_path / "state"
    statedir.mkdir()
    monkeypatch.setattr(self_update, "_state_dir", lambda: statedir)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "refresh-calls"
    refresh = bindir / "refresh"
    refresh.write_text(
        '#!/bin/sh\necho "$*" >> %s\nexit %d\n' % (calls, refresh_rc), encoding="utf-8"
    )
    refresh.chmod(0o755)
    monkeypatch.setattr(
        dev_update, "refresh_command", lambda checkout, python="": [str(refresh), "go"]
    )
    for i, body in enumerate(curl_bodies):
        (tmp_path / ("curl.%d" % i)).write_text(
            "" if body is None else json.dumps(body, separators=(",", ":"))
        )
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "n=$(cat {d}/curl.n 2>/dev/null || echo 0)\n"
        '[ "$n" -gt {last} ] && n={last}\n'
        "echo $((n + 1)) > {d}/curl.n\n"
        "f={d}/curl.$n\n"
        '[ -s "$f" ] || exit 7\n'
        'cat "$f"\n'.format(d=tmp_path, last=len(curl_bodies) - 1)
    )
    curl.chmod(0o755)
    script_text = self_update._script(
        st["branch"],
        st["target"],
        from_version="0.1.0",
        prev_commit=st["head"],
        started_at=123.0,
        health_url="http://127.0.0.1:1/api/remote/hello",
        health_timeout=3,
        health_poll=1,
        down_probes=2,
        relaunch="",
        auto=True,
        version=st["target_version"] or "0.1.0",
        dev=st,
    )
    assert "uv sync" not in script_text and "--hard" not in script_text
    script = tmp_path / "gen.sh"
    script.write_text(script_text)
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), PATH="%s:/usr/bin:/bin" % bindir, **_ENV)
    cp = subprocess.run(
        ["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    state = json.loads(self_update.state_path().read_text())
    refreshes = calls.read_text().splitlines() if calls.exists() else []
    return cp, state, refreshes


def test_the_script_fast_forwards_refreshes_and_is_healthy(
    repos, tmp_path, monkeypatch
):
    tip = repos.publish(version="0.2.0")
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    old = {"app": "mindflock", "version": "0.1.0", "commit": st["head"]}
    new = {"app": "mindflock", "version": "0.2.0", "commit": tip}
    cp, state, refreshes = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[old, new]
    )
    assert cp.returncode == 0, cp.stderr
    assert repos.head() == tip
    assert state["state"] == "done" and state["healthy"] is True
    assert state["auto"] is True and state["dev"] is True
    assert state["version"] == "0.2.0" and state["commit"] == tip
    assert len(refreshes) == 1


def test_a_checkout_that_never_comes_back_is_moved_back(repos, tmp_path, monkeypatch):
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    old = {"app": "mindflock", "version": "0.1.0", "commit": st["head"]}
    cp, state, refreshes = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[old, None]
    )
    assert state["state"] == "rolled_back"
    assert "moved back to %s" % st["head"][:7] in state["error"]
    assert repos.head() == st["head"]
    # Refreshed onto the new code, then again onto the old.
    assert len(refreshes) == 2


def test_a_failed_refresh_moves_the_checkout_back(repos, tmp_path, monkeypatch):
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    cp, state, _ = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[None], refresh_rc=5
    )
    assert state["state"] == "failed" and state["code"] == 5
    assert "moved back" in state["error"]
    assert repos.head() == st["head"]


def test_edits_made_while_it_waited_stop_the_update(repos, tmp_path, monkeypatch):
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    (repos.work / "a.txt").write_text("someone is editing\n")
    cp, state, refreshes = _run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[None]
    )
    assert state["state"] == "failed"
    assert state["error"] == "the checkout has uncommitted changes now"
    assert repos.head() == st["head"] and refreshes == []
    assert (repos.work / "a.txt").read_text() == "someone is editing\n"


def test_a_branch_switch_while_it_waited_stops_the_update(repos, tmp_path, monkeypatch):
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    _git(repos.work, "checkout", "-b", "elsewhere")
    cp, state, _ = _run_dev_script(tmp_path, monkeypatch, st, curl_bodies=[None])
    assert state["state"] == "failed" and "no longer on its branch" in state["error"]


# --------------------------------------------------------------------------- #
# start_update on a checkout
# --------------------------------------------------------------------------- #
def test_start_update_on_a_checkout_spawns_the_git_path(repos, tmp_path, monkeypatch):
    repos.publish(version="0.2.0")
    statedir = tmp_path / "state"
    statedir.mkdir()
    monkeypatch.setattr(self_update, "_state_dir", lambda: statedir)
    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    monkeypatch.setattr(self_update, "install_checkout", lambda: str(repos.work))
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.1.0")
    monkeypatch.setattr(dev_update, "_sync_remote", lambda *a, **k: None)
    _git(repos.work, "fetch", "origin")
    spawned = []
    monkeypatch.setattr(
        self_update,
        "_spawn",
        lambda path, fields: spawned.append(fields) or {"ok": True},
    )
    assert self_update.start_update("v9.9.9", auto=True) == {"ok": True}
    fields = spawned[0]
    assert fields["dev"] is True and fields["auto"] is True
    assert fields["ref"] == "main" and fields["version"] == "0.2.0"
    assert fields["prev_commit"] == repos.head()
    script = (statedir / "update.sh").read_text()
    assert "merge --ff-only" in script and "reset --quiet --keep" in script


def test_start_update_on_an_up_to_date_checkout_is_a_quiet_no(repos, monkeypatch):
    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    monkeypatch.setattr(self_update, "install_checkout", lambda: str(repos.work))
    out = self_update.start_update("")
    assert out["ok"] is False and out["quiet"] is True


# --------------------------------------------------------------------------- #
# The foreground path (mindflock update)
# --------------------------------------------------------------------------- #
def test_apply_here_moves_forward_and_move_back_returns(repos, monkeypatch, capsys):
    tip = repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    monkeypatch.setattr(dev_update, "refresh_command", lambda checkout, python="": None)
    assert dev_update.apply_here(st) == (True, "")
    assert repos.head() == tip
    assert dev_update.move_back(str(repos.work), st["head"]) is True
    assert repos.head() == st["head"]


def test_move_back_refuses_a_dirty_tree(repos, monkeypatch):
    tip = repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    monkeypatch.setattr(dev_update, "refresh_command", lambda checkout, python="": None)
    dev_update.apply_here(st)
    (repos.work / "a.txt").write_text("edited after\n")
    assert dev_update.move_back(str(repos.work), st["head"]) is False
    assert repos.head() == tip


# --------------------------------------------------------------------------- #
# The version a checkout reports
# --------------------------------------------------------------------------- #
def test_a_checkout_reports_its_own_pyproject_version(tmp_path):
    import backend

    pkg = tmp_path / "backend"
    pkg.mkdir()
    (tmp_path / "pyproject.toml").write_text(_pyproject("7.8.9"))
    assert backend._checkout_version(str(pkg)) == "7.8.9"
    # Not ours, or no pyproject at all (a release install): no answer.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "other"\nversion = "1"\n'
    )
    assert backend._checkout_version(str(pkg)) == ""
    (tmp_path / "pyproject.toml").unlink()
    assert backend._checkout_version(str(pkg)) == ""
