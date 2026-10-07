"""The engine's ordinary git calls (status polling, diff stats) on a peer
shared folder: hardened argv, no submodule handling, no staging.

Host git only treats a path as a submodule when the trusted index lists a
gitlink for it, so the invariant is twofold: nothing the engine runs can add
one (``add`` is refused, diff stats skip ``add -N``), and every status/diff
ignores submodules outright, whatever a planted ``.gitmodules`` says."""

from __future__ import annotations

import subprocess

import pytest

from backend.peer import paths
from backend.session.git import diff as diff_mod
from backend.session.git import worktree_git as wg


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


@pytest.fixture
def share_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    work = tmp_path / "peer" / "shares" / ("ab" * 16) / "work"
    work.mkdir(parents=True)
    return str(work)


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(wg.subprocess, "run", r)
    return r


def _run(path, *args):
    return wg.GitWorktreeGitMixin.run_git_command(object(), path, *args)


def test_peer_share_status_and_diff_ignore_submodules(share_path, rec):
    _run(share_path, "status", "--porcelain")
    _run(share_path, "diff", "--numstat", "HEAD")
    for argv in rec.calls:
        sub = argv.index("-C") + 2
        assert argv[sub + 1] == "--ignore-submodules=all", argv
        cfg = " ".join(argv[: argv.index("-C")])
        for kv in (
            "core.fsmonitor=false",
            "core.hooksPath=/dev/null",
            "submodule.recurse=false",
            "diff.ignoreSubmodules=all",
        ):
            assert kv in cfg, (kv, argv)


def test_peer_share_staging_refused(share_path, rec):
    for args in (("add", "-N", "."), ("add", "-A")):
        with pytest.raises(RuntimeError, match="staging is not available"):
            _run(share_path, *args)
    assert rec.calls == []


def test_ordinary_worktree_unchanged(tmp_path, monkeypatch, rec):
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    wt = tmp_path / "wt"
    wt.mkdir()
    _run(str(wt), "add", "-N", ".")
    _run(str(wt), "status", "--porcelain")
    assert rec.calls == [
        ["git", "-C", str(wt), "add", "-N", "."],
        ["git", "-C", str(wt), "status", "--porcelain"],
    ]


def test_symlink_into_share_is_still_a_share(share_path, tmp_path, rec):
    link = tmp_path / "innocent"
    link.symlink_to(share_path)
    with pytest.raises(RuntimeError):
        _run(str(link), "add", "-N", ".")


def test_diff_stats_skip_intent_to_add_in_share(share_path, monkeypatch):
    seen = []

    class W(diff_mod.GitWorktreeDiffMixin):
        worktreePath = share_path

        def run_git_command(self, path, *args, **kw):
            seen.append(args)
            return ""

        def GetBaseCommitSHA(self):
            return "0" * 40

    for meth in ("_diff_uncached", "DiffNumstat"):
        fn = getattr(W(), meth, None)
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001 — only the argv matters here
                pass
    assert seen, "diff code did not run"
    assert not any(a[:1] == ("add",) for a in seen), seen
    assert paths.is_inside_peer_root(share_path)
