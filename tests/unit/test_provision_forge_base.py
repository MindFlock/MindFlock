"""A ticket session provisioned from the user's own checkout pushes to the
FORGE and forks from the forge's base, not the checkout's stale copy of it.

The case that motivated this: a ticket routed to ``~/…/sitecheck-bot7`` got
``origin`` = that checkout (``load_provision_settings`` never carried the
checkout's own origin), and its base was the checkout's ``staging`` — 339
commits behind GitHub. The agent caught up by merging GitHub's staging, which
made the branch read "committed" at once; fast-track pushed it into the laptop
and Open PR found no GitHub remote. The forge here is a local repo served by
a fake ``ssh`` (``GIT_SSH_COMMAND``), so nothing touches the network and every
URL the code sees is the real GitHub spelling.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from backend.config import settings as S
from backend.session import provisioned

FORGE_URL = "git@github.com:Org/app.git"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch, isolate_settings_store):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MINDFLOCK_BASE_BRANCH", raising=False)
    monkeypatch.delenv("MINDFLOCK_REPO_URL", raising=False)


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(path: Path, name: str) -> str:
    (path / name).write_text(name + "\n")
    _git(path, "add", name)
    _git(path, "commit", "-q", "-m", name)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def forge(tmp_path, monkeypatch):
    """A 'GitHub' repo on staging, reachable as FORGE_URL."""
    f = tmp_path / "forge"
    f.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "staging", str(f)], check=True)
    _commit(f, "one")
    ssh = tmp_path / "fake-ssh"
    # git runs `<ssh> [opts] git@github.com "git-upload-pack 'Org/app.git'"`:
    # run that last argument locally, against the forge directory.
    ssh.write_text(
        "#!/bin/sh\n"
        'for a; do cmd="$a"; done\n'
        'exec sh -c "$(printf %s "$cmd" | sed "s#\'Org/app.git\'#\'$FAKE_FORGE\'#")"\n'
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("FAKE_FORGE", str(f))
    monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh))
    return f


def _checkout_of(tmp_path: Path) -> Path:
    """The user's checkout: cloned from the forge, origin spelled as GitHub."""
    c = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", FORGE_URL, str(c)], check=True)
    return c


def _settings(checkout: Path, tmp_path: Path) -> provisioned.ProvisionSettings:
    S.update_settings(
        repository={
            "url": FORGE_URL,
            "workspace_dir": str(tmp_path / "ws"),
            "base_branch": "staging",
        }
    )
    s = provisioned.load_provision_settings(repo_url_override=str(checkout))
    assert s is not None
    return s


def test_a_local_ticket_source_carries_its_checkouts_origin(forge, tmp_path):
    checkout = _checkout_of(tmp_path)
    s = _settings(checkout, tmp_path)
    assert s.repo_url == str(checkout), "still cloned from the fast local source"
    assert s.origin_url == FORGE_URL
    assert provisioned.forge_origin(s) == FORGE_URL


def test_a_forge_url_source_needs_no_origin_url(forge, tmp_path):
    S.update_settings(repository={"url": FORGE_URL, "workspace_dir": str(tmp_path)})
    s = provisioned.load_provision_settings()
    assert s is not None and s.origin_url == ""
    assert provisioned.forge_origin(s) == FORGE_URL


def test_the_base_pushes_to_the_forge_and_forks_from_its_fresh_tip(forge, tmp_path):
    checkout = _checkout_of(tmp_path)
    fresh = [_commit(forge, "two"), _commit(forge, "three")][-1]  # checkout is behind
    base = Path(provisioned.ensure_base_repo(_settings(checkout, tmp_path)))
    assert _git(base, "remote", "get-url", "origin") == FORGE_URL
    assert _git(base, "rev-parse", "HEAD") == fresh
    assert _git(base, "rev-parse", "--abbrev-ref", "HEAD") == "staging"
    # A refresh later keeps following the forge.
    newer = _commit(forge, "four")
    provisioned.ensure_base_repo(_settings(checkout, tmp_path))
    assert _git(base, "rev-parse", "HEAD") == newer


def test_a_checkout_with_its_own_commits_keeps_them(forge, tmp_path):
    checkout = _checkout_of(tmp_path)
    _commit(forge, "two")
    mine = _commit(checkout, "local-only")  # diverged from the forge
    base = Path(provisioned.ensure_base_repo(_settings(checkout, tmp_path)))
    assert _git(base, "rev-parse", "HEAD") == mine


def test_an_unreachable_forge_leaves_the_checkouts_base(forge, tmp_path, monkeypatch):
    checkout = _checkout_of(tmp_path)
    start = _git(checkout, "rev-parse", "HEAD")
    _commit(forge, "two")
    # Offline: every ssh connection fails.
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")
    base = Path(provisioned.ensure_base_repo(_settings(checkout, tmp_path)))
    assert _git(base, "rev-parse", "HEAD") == start
    assert os.path.isdir(base / ".git")
