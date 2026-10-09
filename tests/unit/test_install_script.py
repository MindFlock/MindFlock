"""install.sh: the newest release by default, and a running server restarted.

The README's one-liner used to install ``main`` (unreleased code sharing a
version string with the last release) while the desktop app and both
updaters track releases. Now an unset ``MINDFLOCK_INSTALL_REF`` resolves the
newest release; and re-running the script — the documented way to update —
restarts a server already running here instead of leaving it on old code.
The resolver is exercised with stub ``curl``/``git`` (no network).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2] / "install.sh").read_text(
    encoding="utf-8"
)


def test_install_script_parses():
    assert subprocess.run(["/bin/sh", "-n", "-c", _SCRIPT]).returncode == 0


@pytest.mark.skipif(not shutil.which("dash"), reason="dash not installed")
def test_install_script_parses_as_posix_sh():
    assert subprocess.run(["dash", "-n", "-c", _SCRIPT]).returncode == 0


def test_the_default_ref_is_the_newest_release_not_main():
    assert 'REF="${MINDFLOCK_INSTALL_REF:-}"' in _SCRIPT
    assert 'REF="${MINDFLOCK_INSTALL_REF:-main}"' not in _SCRIPT
    assert 'REF="$(latest_release_tag)"' in _SCRIPT


def test_a_running_server_is_restarted_after_the_install():
    tail = _SCRIPT[_SCRIPT.index("# --- 5.") :]
    assert "/api/remote/hello" in tail and '"$MF" restart' in tail
    # …unless the caller restarts it itself (the desktop app does).
    assert '"${MINDFLOCK_INSTALL_NO_RESTART:-}" != "1"' in tail
    main_js = (Path(__file__).resolve().parents[2] / "electron" / "main.js").read_text(
        encoding="utf-8"
    )
    assert main_js.count("MINDFLOCK_INSTALL_NO_RESTART") >= 2  # both transports


def _resolver(tmp_path: Path, *, curl: str, git: str, repo: str) -> str:
    fn = re.search(r"^latest_release_tag\(\) \{.*?^\}", _SCRIPT, re.S | re.M).group(0)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", curl), ("git", git)):
        exe = bindir / name
        exe.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        exe.chmod(0o755)
    script = "REPO=%s\n%s\nlatest_release_tag\n" % (repo, fn)
    env = dict(os.environ, PATH="%s:/usr/bin:/bin" % bindir)
    return subprocess.run(
        ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, timeout=30
    ).stdout


@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/MindFlock/MindFlock",
        "https://github.com/MindFlock/MindFlock.git",
    ],
)
def test_the_resolver_reads_the_releases_api(tmp_path, repo):
    curl = (
        'case "$*" in *api.github.com/repos/MindFlock/MindFlock/releases/latest*) '
        'printf \'{\\n  "tag_name": "v0.8.1",\\n  "name": "x"\\n}\\n\' ;; *) exit 22 ;; esac'
    )
    assert _resolver(tmp_path, curl=curl, git="exit 1", repo=repo) == "v0.8.1"


def test_the_resolver_falls_back_to_the_highest_remote_tag(tmp_path):
    git = 'printf "abc\\trefs/tags/v0.9.0\\ndef\\trefs/tags/v0.8.0\\n"'
    assert (
        _resolver(tmp_path, curl="exit 6", git=git, repo="https://example.com/x.git")
        == "v0.9.0"
    )


def test_the_resolver_answers_nothing_rather_than_a_non_release(tmp_path):
    git = 'printf "abc\\trefs/tags/nightly\\n"'
    assert (
        _resolver(tmp_path, curl="exit 6", git=git, repo="https://example.com/x") == ""
    )


def test_the_resolver_skips_pre_releases_and_stray_v_tags(tmp_path):
    git = (
        'printf "a\\trefs/tags/vnext\\nb\\trefs/tags/v1.0.0-rc1\\n'
        'c\\trefs/tags/v0.9.0\\n"'
    )
    assert (
        _resolver(tmp_path, curl="exit 6", git=git, repo="https://example.com/x.git")
        == "v0.9.0"
    )


def _restart_step(tmp_path: Path, *, restart_exit: int) -> subprocess.CompletedProcess:
    """Step 5 alone, with a server answering its hello and a `mindflock
    restart` that exits ``restart_exit``."""
    step = _SCRIPT[_SCRIPT.index("# --- 5.") : _SCRIPT.index("# --- 6.")]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    mf = bindir / "mindflock"
    mf.write_text("#!/bin/sh\nexit %d\n" % restart_exit, encoding="utf-8")
    for exe in bindir.iterdir():
        exe.chmod(0o755)
    script = (
        'set -eu\nsay() { printf \'%%s\\n\' "$*"; }\nJOIN=""\nMF=%s\n%s\n'
        "say AFTER-STEP-5\n" % (mf, step)
    )
    env = dict(os.environ, PATH="%s:/usr/bin:/bin" % bindir)
    env.pop("MINDFLOCK_INSTALL_NO_RESTART", None)
    return subprocess.run(
        ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )


def test_a_restarted_server_is_reported_as_running_the_new_version(tmp_path):
    cp = _restart_step(tmp_path, restart_exit=0)
    assert cp.returncode == 0 and "runs the new version" in cp.stdout


def test_a_failed_restart_never_claims_the_new_version_runs(tmp_path):
    cp = _restart_step(tmp_path, restart_exit=1)
    assert "runs the new version" not in cp.stdout
    assert "still runs the previous version" in cp.stdout
    assert cp.returncode == 1
