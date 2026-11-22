"""A batch of ``spawn_session`` calls: every accepted spawn becomes a session
or a failure its orchestrator sees — never nothing.

Observed on the owner's machine: an orchestrator that spawned several workers
in one batch (parallel tool calls, so near-simultaneous ``POST
/api/instances``) lost some of them. Two things made that possible:

* their Starts ran ``git worktree add`` in one repository at the same moment,
  which git can fail (``test_worktree_concurrent_add``); and
* a spawn the launch gate let through at once (not queued) whose Start then
  failed only dropped its row: no ``failed_to_start``, no note to the parent,
  and the ``/api/create_failures`` record expired ten minutes later. A queued
  spawn's failure already stayed visible; now both do.

In-process: the registry is a fresh dict, the create's side effects are
stubbed except the worktree itself, which is real git under ``tmp_path``.
Nothing touches tmux, the live engine or the owner's settings.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time

import pytest

from backend.config import settings as _settings
from backend import session
from backend.session.git.worktree import GitWorktree
from backend.web import server
from backend.web.core import launch_gate as gate
from backend.web.core import pending
from backend.web.core import session_create as sc
from tests.unit.test_create_queue import _GO, env  # noqa: F401 — the fixture
from tests.unit.test_webui import _FakeInst, _stub_create


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        stdout=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = str(tmp_path / "repo")
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    for i in range(200):
        with open(os.path.join(path, "f%03d.txt" % i), "w") as fh:
            fh.write("x" * 2000)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "files")
    return path


class _WorktreeInst(_FakeInst):
    """A session whose Start makes its real worktree (and nothing else)."""

    fail_with = ""

    def __init__(self, opts, repo, root):
        super().__init__(opts)
        self._tree = GitWorktree(
            repoPath=repo,
            worktreePath=os.path.join(root, self.Title),
            sessionName=self.Title,
            branchName="mf/" + self.Title,
            baseCommitSHA="",
            isExistingBranch=False,
        )

    def Start(self, *a, **k):  # noqa: N802
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self._tree.Setup()
        self.Status = "running"

    def GetWorktreePath(self):  # noqa: N802
        return self._tree.worktreePath


@pytest.fixture
def batch(env, repo, tmp_path, monkeypatch):  # noqa: F811 — the fixture
    """Creates go through at once (the gate says go), their background Starts
    are collected so a test runs them together."""
    _stub_create(monkeypatch)
    root = str(tmp_path / "wts")
    os.makedirs(root)
    monkeypatch.setattr(
        session, "NewInstance", lambda opts: _WorktreeInst(opts, repo, root)
    )
    monkeypatch.setattr(server, "_prepare_plain_repo", lambda r, i, **k: (repo, True))
    monkeypatch.setattr(gate, "decide", lambda *a, **k: dict(_GO))
    monkeypatch.setattr(gate, "spacing_for", lambda *a, **k: 0.0)
    monkeypatch.setattr(server.ENGINE, "save", lambda *a, **k: None)
    monkeypatch.setattr(_settings, "update_settings", lambda *a, **k: None)
    starts: list = []
    monkeypatch.setattr(server, "_register_task", starts.append)
    env.starts = starts
    env.repo = repo
    return env


def _spawn(title, repo):
    return {
        "program": "bash",
        "repo_path": repo,
        "parent": "orch",
        "spawned": True,
        "title": title,
        "prompt": "task for " + title,
    }


async def _fire(h, titles):
    """N spawns at once, then their N Starts at once (as the server's loop
    would run them: each Start in its own thread)."""
    results = await asyncio.gather(
        *(sc.create_requested(_spawn(t, h.repo)) for t in titles)
    )
    await asyncio.gather(*h.starts)
    return [r.status_code for r in results]


def test_a_batch_of_spawns_all_become_sessions(batch, monkeypatch):
    """Eight spawns at once from one repository: eight sessions, and their
    ``git worktree add`` runs never overlapped (each is slowed down so they
    would, were nothing keeping them apart)."""
    active = {"now": 0, "max": 0}
    count = threading.Lock()
    real = GitWorktree.run_git_command

    def slow(self, path, *args, **kw):
        if args[:2] != ("worktree", "add"):
            return real(self, path, *args, **kw)
        with count:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            time.sleep(0.05)
            return real(self, path, *args, **kw)
        finally:
            with count:
                active["now"] -= 1

    monkeypatch.setattr(GitWorktree, "run_git_command", slow)
    titles = ["orch-w%d" % i for i in range(1, 9)]
    statuses = asyncio.run(_fire(batch, titles))
    assert active["max"] == 1
    assert statuses == [202] * len(titles)
    failed = {t: pending.failed(t) for t in titles if pending.failed(t)}
    assert failed == {}
    started = [
        t for t in titles if getattr(batch.instances.get(t), "Status", "") == "running"
    ]
    assert started == titles


def test_an_admitted_spawn_whose_start_fails_stays_a_visible_failure(
    batch, monkeypatch
):
    """Let through at once (not queued), then its Start fails: the row stays
    as ``start_failed`` (what wait_for_session reads as failed_to_start), the
    failure is noted and announced, and the orchestrator is told."""
    monkeypatch.setattr(
        _WorktreeInst, "fail_with", "git command failed: fatal: failed to read"
    )
    statuses = asyncio.run(_fire(batch, ["orch-w1"]))
    assert statuses == [202]
    assert "orch-w1" not in batch.instances
    rec = pending.failed("orch-w1")
    assert rec is not None and "failed to read" in rec["error"]
    assert list(pending.failed_rows("orch")) == ["orch-w1"]
    assert "task for orch-w1" in (rec.get("prompt_preview") or "")
    assert "orch-w1" in batch.notes
    assert [(p, t) for p, t, _e in batch.told] == [("orch", "orch-w1")]
    assert any(
        e == "session.create_failed" and s == "orch-w1" for e, s, _d in batch.events
    )
    # The row the listing shows carries it, so MCP wait_for_session answers
    # failed_to_start with the reason.
    rows = {r["title"]: r for r in pending.rows(server.ENGINE)}
    assert rows["orch-w1"].get("start_failed", {}).get("error")


def test_a_persons_failed_create_still_just_reports(batch, monkeypatch):
    """Not a spawn (a person's New session): unchanged — the failure is noted
    and announced, no placeholder row is kept, no parent is told."""
    monkeypatch.setattr(_WorktreeInst, "fail_with", "boom")
    payload = _spawn("mine", batch.repo)
    payload.pop("spawned")
    payload.pop("parent")

    async def go():
        resp = await sc.create_requested(payload)
        await asyncio.gather(*batch.starts)
        return resp.status_code

    assert asyncio.run(go()) == 202
    assert pending.failed("mine") is None
    assert "mine" in batch.notes and batch.told == []
