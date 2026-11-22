"""Preparing a new session worktree (``backend.worktree_prepare`` and its
runner in ``backend.web.core.worktree_setup``).

Covers: the default pin carry (and that secrets never ride by default), the
lockfile plan, the node_modules clone (hard-link tree, the private metadata
copy, the caches left behind, deleting a clone never touching the source),
when a clone is refused, the install fallback, and the runner's ``hold``
contract (the automatic part never holds prompts; declared commands do).
No real package manager runs: installs are fake executables on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time

import pytest

from backend import worktree_prepare as prep
from backend.web.core import worktree_setup as ws


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def deps_on(monkeypatch):
    monkeypatch.delenv(prep.DEPS_ENV, raising=False)


@pytest.fixture
def source(tmp_path):
    """A repo with a root uv project and an npm frontend, its node_modules
    "installed", plus a worktree of it."""
    r = tmp_path / "src"
    (r / "frontend").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text("node_modules/\n.venv/\n.python-version\n.env\n")
    (r / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    (r / "uv.lock").write_text("version = 1\n")
    (r / "frontend" / "package.json").write_text("{}")
    (r / "frontend" / "package-lock.json").write_text('{"lockfileVersion": 3}')
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    nm = r / "frontend" / "node_modules"
    (nm / "pkg" / "lib").mkdir(parents=True)
    (nm / "pkg" / "lib" / "index.js").write_text("module.exports = 1\n")
    (nm / ".bin").mkdir()
    os.symlink("../pkg/lib/index.js", nm / ".bin" / "pkg")
    (nm / ".cache" / "babel").mkdir(parents=True)
    (nm / ".cache" / "babel" / "x.json").write_text("{}")
    (nm / ".package-lock.json").write_text('{"hidden": true}')
    # npm writes its hidden lockfile after the lockfile it installed from.
    later = time.time() + 5
    os.utime(nm / ".package-lock.json", (later, later))
    (r / ".venv").mkdir()
    (r / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (r / ".python-version").write_text("3.12\n")
    (r / ".env").write_text("SECRET=1\n")
    wt = tmp_path / "wt"
    _git(r, "worktree", "add", "-q", "-b", "feat", str(wt))
    return r, wt


# --------------------------------------------------------------------------- #
# carry
# --------------------------------------------------------------------------- #
def test_default_carry_brings_pins_never_secrets(source):
    src, wt = source
    copied = prep.carry_files(str(src), str(wt))
    assert copied == [".python-version"]
    assert (wt / ".python-version").read_text() == "3.12\n"
    assert not (wt / ".env").exists()


def test_explicit_carry_may_name_a_secret(source):
    src, wt = source
    assert ".env" in prep.carry_files(str(src), str(wt), [".env"])
    assert (wt / ".env").exists()


def test_default_carry_skips_symlinks_big_files_and_existing(source):
    src, wt = source
    (src / ".nvmrc").symlink_to(src / ".python-version")
    (src / ".tool-versions").write_text("x" * (prep.CARRY_MAX_BYTES + 1))
    (wt / ".python-version").write_text("3.11\n")
    assert prep.carry_files(str(src), str(wt)) == []
    assert (wt / ".python-version").read_text() == "3.11\n"  # never overwritten


def test_looks_secret():
    for name in (".env", ".env.local", "prod.env", ".envrc", "id_rsa", "x.pem",
                 ".npmrc", "aws_credentials", "gh-token.txt"):  # fmt: skip
        assert prep.looks_secret(name), name
    for name in DEFAULTS_SAFE:
        assert not prep.looks_secret(name), name


DEFAULTS_SAFE = prep.DEFAULT_CARRY


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_finds_tracked_lockfiles(source):
    _src, wt = source
    steps = prep.plan_dependencies(str(wt))
    assert [(s.manager, s.dir) for s in steps] == [("uv", ""), ("npm", "frontend")]


def test_plan_ignores_untracked_and_lockfiles_without_a_manifest(source):
    _src, wt = source
    (wt / "other").mkdir()
    (wt / "other" / "yarn.lock").write_text("")  # untracked
    os.unlink(wt / "pyproject.toml")
    steps = prep.plan_dependencies(str(wt))
    assert [(s.manager, s.dir) for s in steps] == [("npm", "frontend")]


def test_auto_plan_mirrors_what_the_source_installed(source, deps_on):
    src, wt = source
    shutil.rmtree(src / "frontend" / "node_modules")
    cfg = ws.WorkspaceConfig()
    assert [s.manager for s in ws.dependency_plan(str(wt), cfg, str(src))] == ["uv"]
    # "install" installs every lockfile's, whatever the source has.
    cfg = ws.WorkspaceConfig(dependencies="install")
    got = [s.manager for s in ws.dependency_plan(str(wt), cfg, str(src))]
    assert got == ["uv", "npm"]


def test_plan_off_switches(source, monkeypatch, deps_on):
    src, _wt = source
    assert ws.dependency_plan(str(src), ws.WorkspaceConfig(dependencies="off")) == []
    monkeypatch.setenv(prep.DEPS_ENV, "0")
    assert ws.dependency_plan(str(src), ws.WorkspaceConfig()) == []


# --------------------------------------------------------------------------- #
# node_modules clone
# --------------------------------------------------------------------------- #
def _npm_step():
    return prep.DepStep("npm", "frontend", "package-lock.json")


def test_reuse_clones_as_hard_links_and_unshares_metadata(source, tmp_path):
    src, wt = source
    log = open(tmp_path / "log", "w+")
    res = prep.run_dependency_step(_npm_step(), str(src), str(wt), "auto", log)
    assert res["state"] == "ok" and res["how"] in ("hardlink", "reflink", "clonefile")
    s_nm, w_nm = src / "frontend" / "node_modules", wt / "frontend" / "node_modules"
    js = "pkg/lib/index.js"
    if res["how"] == "hardlink":
        assert os.stat(w_nm / js).st_ino == os.stat(s_nm / js).st_ino
    # The bin link stays a RELATIVE link inside the worktree (never a link
    # back into the source, never a symlinked node_modules).
    assert os.readlink(w_nm / ".bin" / "pkg") == "../pkg/lib/index.js"
    assert not os.path.islink(w_nm)
    # npm rewrites its hidden lockfile in place: a private copy.
    assert (
        os.stat(w_nm / ".package-lock.json").st_ino
        != os.stat(s_nm / ".package-lock.json").st_ino
    )
    assert not (w_nm / ".cache").exists()  # tool caches are not shared
    assert (s_nm / ".cache" / "babel" / "x.json").exists()
    # Removing the worktree's copy never touches the source's files.
    shutil.rmtree(w_nm)
    assert (s_nm / js).read_text() == "module.exports = 1\n"


def test_reuse_refused_when_lockfile_differs(source, tmp_path, monkeypatch):
    src, wt = source
    (wt / "frontend" / "package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}')
    assert "differs" in prep.reusable_node_modules(_npm_step(), str(src), str(wt))


def test_reuse_refused_for_stale_or_symlinked_source(source, tmp_path):
    src, wt = source
    nm = src / "frontend" / "node_modules"
    os.utime(nm / ".package-lock.json", (1, 1))
    assert "older" in prep.reusable_node_modules(_npm_step(), str(src), str(wt))
    shutil.move(str(nm), str(tmp_path / "real_nm"))
    os.symlink(tmp_path / "real_nm", nm)
    assert "no installed" in prep.reusable_node_modules(_npm_step(), str(src), str(wt))


def _fake_bin(tmp_path, monkeypatch, name, body):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    exe = b / name
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(b) + os.pathsep + os.environ.get("PATH", ""))


def test_install_fallback_runs_the_package_manager(source, tmp_path, monkeypatch):
    src, wt = source
    (wt / "frontend" / "package-lock.json").write_text('{"changed": true}')
    _fake_bin(tmp_path, monkeypatch, "npm", 'echo "npm $*"; mkdir node_modules')
    log = open(tmp_path / "log", "w+")
    res = prep.run_dependency_step(_npm_step(), str(src), str(wt), "auto", log)
    assert res["state"] == "ok" and res["how"] == "install"
    log.seek(0)
    text = log.read()
    assert "lockfile differs" in text and "npm ci --prefer-offline" in text


def test_install_failure_says_why(source, tmp_path, monkeypatch):
    src, wt = source
    _fake_bin(tmp_path, monkeypatch, "uv", "exit 2")
    log = open(tmp_path / "log", "w+")
    step = prep.DepStep("uv", "", "uv.lock")
    res = prep.run_dependency_step(step, str(src), str(wt), "auto", log)
    assert res["state"] == "failed" and res["note"] == "uv sync in . exited 2"


def test_uv_step_never_targets_the_servers_venv(source, tmp_path, monkeypatch):
    src, wt = source
    _fake_bin(tmp_path, monkeypatch, "uv", 'echo "VENV=${VIRTUAL_ENV:-none}"')
    monkeypatch.setenv("VIRTUAL_ENV", "/the/server/venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/the/server/venv")
    log = open(tmp_path / "log", "w+")
    prep.run_dependency_step(
        prep.DepStep("uv", "", "uv.lock"), str(src), str(wt), "auto", log
    )
    log.seek(0)
    assert "VENV=none" in log.read()


# --------------------------------------------------------------------------- #
# runner: hold contract
# --------------------------------------------------------------------------- #
def test_automatic_prepare_never_holds_prompts(source, tmp_path, monkeypatch, deps_on):
    src, wt = source
    _fake_bin(tmp_path, monkeypatch, "uv", "sleep 0.3")
    assert ws.start_setup("t", str(src), str(wt), ws.WorkspaceConfig())
    st = ws.setup_status(str(wt))
    assert st["state"] == "running" and st["hold"] is False
    assert not ws.holds_prompts(st)
    assert (wt / ".python-version").exists()  # carried before start returns
    assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "ok")
    st = ws.setup_status(str(wt))
    assert [s["name"] for s in st["steps"]] == ["uv (.)", "npm (frontend)"]
    assert st["steps"][1]["how"] in ("hardlink", "reflink", "clonefile")


def test_failed_automatic_prepare_reports_why_and_still_does_not_hold(
    source, tmp_path, monkeypatch, deps_on
):
    src, wt = source
    _fake_bin(tmp_path, monkeypatch, "uv", "exit 1")
    assert ws.start_setup("t", str(src), str(wt), ws.WorkspaceConfig())
    assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "failed")
    st = ws.setup_status(str(wt))
    assert st["error"] == "uv sync in . exited 1"
    assert not ws.holds_prompts(st)
    # The later step still ran: one failure doesn't strand the rest.
    assert st["steps"][1]["state"] == "ok"


def test_declared_commands_hold_prompts(source, deps_on, monkeypatch):
    src, wt = source
    monkeypatch.setenv(prep.DEPS_ENV, "0")
    cfg = ws.WorkspaceConfig(setup_commands=["exit 4"])
    assert ws.start_setup("t", str(src), str(wt), cfg)
    assert _wait(lambda: (ws.setup_status(str(wt)) or {}).get("state") == "failed")
    st = ws.setup_status(str(wt))
    assert st["hold"] is True and ws.holds_prompts(st)
    assert st["error"] == "a setup command exited 4"


def test_status_without_hold_key_holds_as_before():
    assert ws.holds_prompts({"state": "running"})
    assert ws.holds_prompts({"state": "failed"})
    assert not ws.holds_prompts({"state": "ok"})
    assert not ws.holds_prompts(None)


def test_prompt_note_names_the_steps_and_the_status_file():
    note = ws.prompt_note([prep.DepStep("uv", "", "uv.lock")])
    assert "uv (.)" in note and ws.SETUP_STATUS in note
    assert ws.prompt_note([]) == ""


# --------------------------------------------------------------------------- #
# config: the repo's file + the user's block
# --------------------------------------------------------------------------- #
def test_dependencies_key_parsed_and_bad_value_is_auto(tmp_path):
    (tmp_path / ws.CONFIG_NAME).write_text('[workspace]\ndependencies = "install"\n')
    assert ws.load_config(str(tmp_path)).dependencies == "install"
    (tmp_path / ws.CONFIG_NAME).write_text('[workspace]\ndependencies = "yes"\n')
    assert ws.load_config(str(tmp_path)).dependencies == "auto"


def test_user_block_merges_after_the_repos(tmp_path, monkeypatch):
    from backend.config import settings as _settings

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ws.CONFIG_NAME).write_text(
        '[workspace]\nsetup_commands = ["make a"]\ncopy_untracked = [".x"]\n'
    )
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "repository": {
                    "worktree_setup": {
                        str(repo): {
                            "setup_commands": ["make b"],
                            "copy_untracked": [".env"],
                            "dependencies": "off",
                            "junk": 1,
                        }
                    }
                }
            }
        )
    )
    _settings.invalidate()
    st = _settings.load_settings()
    assert st.repository.worktree_setup == {
        str(repo): {
            "setup_commands": ["make b"],
            "copy_untracked": [".env"],
            "dependencies": "off",
        }
    }
    monkeypatch.setattr(_settings, "load_settings", lambda: st)
    cfg = ws.load_setup_config(str(repo))
    assert cfg.setup_commands == ["make a", "make b"]
    assert cfg.copy_untracked == [".x", ".env"]
    assert cfg.dependencies == "off"
