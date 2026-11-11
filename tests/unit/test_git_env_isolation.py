"""The suite must never touch the repo it is run FROM, even under a git hook.

A git hook runs with the committing repo exported: a pre-commit hook in a
linked worktree gets ``GIT_DIR=<repo>/.git/worktrees/<name>`` and an absolute
``GIT_INDEX_FILE``. The owner's local pre-commit chain runs the full suite from
that hook, and ``test_flight_triggers`` snapshotted ``os.environ`` at import —
so its ``git -C <tmp> init`` re-initialised the REAL repo (``core.bare = true``
in the shared ``.git/config``; every git command then failed with "must be run
in a work tree"), and its ``git add -A`` / ``commit`` replaced the committing
worktree's index with the tmp repo's single ``a.txt``.

Here a throwaway repo (with a linked worktree, like a MindFlock session) stands
in for "the real repo": the leaking module runs in a nested pytest with the
hook's variables pointing at the stand-in, and the stand-in's config, indexes
and refs must come out byte-identical. tests/conftest.py's import-time scrub
and test_flight_triggers' per-call env are the two fixes this pins.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The tests that leaked: every one that builds a tmp repo through the module's
# ``repos`` fixture / ``_git`` helper (named leaker: the default-branch test).
_LEAKERS = "tests/unit/test_flight_triggers.py"


def _git(folder, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(folder), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()


def _clean_env() -> dict:
    env = dict(os.environ)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(var, None)
    return env


@pytest.fixture
def standin(tmp_path):
    """``(main, linked)``: a repo with two commits and a linked worktree on its
    own branch — the shape of the owner's checkout plus one session."""
    main = tmp_path / "standin"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    for name in ("README.md", "backend.py", "docs.md"):
        (main / name).write_text(name + "\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    linked = tmp_path / "standin-wt"
    _git(main, "worktree", "add", "-q", "-b", "session", str(linked))
    (linked / "work.py").write_text("work\n")
    _git(linked, "add", "-A")
    _git(linked, "commit", "-q", "-m", "session work")
    return main, linked


def _snapshot(main: Path, linked: Path) -> dict:
    gitdir = main / ".git"
    return {
        "config": (gitdir / "config").read_bytes(),
        "main index": (gitdir / "index").read_bytes(),
        "linked index": (gitdir / "worktrees" / linked.name / "index").read_bytes(),
        "refs": _git(main, "for-each-ref", "--format=%(refname) %(objectname)"),
        "linked HEAD": _git(linked, "rev-parse", "HEAD"),
    }


def _hook_env(kind: str, main: Path, linked: Path) -> dict:
    gitdir = main / ".git"
    if kind == "linked-worktree":
        # Exactly what git 2.43 exports to a pre-commit hook in a worktree.
        return {
            "GIT_DIR": str(gitdir / "worktrees" / linked.name),
            "GIT_INDEX_FILE": str(gitdir / "worktrees" / linked.name / "index"),
            "GIT_PREFIX": "",
        }
    # A wrapper/tool exporting the main checkout's repo explicitly.
    return {
        "GIT_DIR": str(gitdir),
        "GIT_INDEX_FILE": str(gitdir / "index"),
    }


@pytest.mark.parametrize("kind", ["linked-worktree", "main-checkout"])
def test_suite_under_a_git_hook_leaves_the_repo_alone(standin, tmp_path, kind):
    main, linked = standin
    before = _snapshot(main, linked)

    env = _clean_env()
    # The nested run is exactly the files named here, serially.
    for var in list(env):
        if var.startswith(("PYTEST_ADDOPTS", "PYTEST_XDIST", "PYTEST_CURRENT_TEST")):
            env.pop(var)
    env.update(_hook_env(kind, main, linked))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            _LEAKERS,
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "nested"),
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    # The stand-in first: a leak shows up as the corruption itself, not just as
    # the nested run failing.
    gitdir = main / ".git"
    cfg = ["git", "config", "--file", str(gitdir / "config")]
    assert (
        subprocess.run(
            cfg + ["core.bare"], capture_output=True, text=True
        ).stdout.strip()
        == "false"
    )
    assert (
        subprocess.run(cfg + ["--get", "core.worktree"], capture_output=True).returncode
        == 1
    )
    after = _snapshot(main, linked)
    for key in before:
        assert after[key] == before[key], key
    assert _git(linked, "status", "--porcelain") == ""
    assert _git(main, "status", "--porcelain") == ""
    # And the nested run must actually have run the leakers (not died
    # collecting, which would leave the stand-in untouched vacuously).
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert " passed" in proc.stdout


def test_root_conftest_scrubs_git_redirects_at_import(tmp_path):
    """The import-time scrub on its own: anything that reads os.environ after
    the root conftest is imported (collection, test-module import, a session
    fixture) sees no redirecting variable, whatever the parent exported."""
    probe = (
        "import importlib.util, os, sys\n"
        "spec = importlib.util.spec_from_file_location('root_conftest', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print(sorted(k for k in os.environ if k in mod._GIT_REDIRECT_VARS\n"
        "             or k.startswith(('GIT_CONFIG_KEY', 'GIT_CONFIG_VALUE', 'GIT_CONFIG_COUNT'))))\n"
    )
    env = _clean_env()
    env.update(
        GIT_DIR=str(tmp_path / "real" / ".git"),
        GIT_WORK_TREE=str(tmp_path / "real"),
        GIT_INDEX_FILE=".git/index",
        GIT_COMMON_DIR=str(tmp_path / "real" / ".git"),
        GIT_OBJECT_DIRECTORY=str(tmp_path / "objects"),
        GIT_CONFIG_PARAMETERS="'core.bare'='true'",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.worktree",
        GIT_CONFIG_VALUE_0=str(tmp_path),
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(_REPO_ROOT / "tests" / "conftest.py")],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.strip()
    assert out == "[]"
