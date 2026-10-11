"""``GET /api/repos/branches`` — the New dialog's Base branch menu.

Local branches first with the checked-out one on top, then remote-tracking
refs (origin/HEAD left out), capped at 200, memoised 10 s per folder; a
folder that is not a repo answers an empty menu rather than an error.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import repo_branches

client = TestClient(server.app)


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def repo(tmp_path):
    repo_branches.clear_cache()
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    (d / "f").write_text("1")
    _git(d, "add", "f")
    _git(d, "commit", "-q", "-m", "one")
    _git(d, "branch", "feature/x")
    _git(d, "branch", "old")
    # a remote-tracking ref and an origin/HEAD pointer, without a network
    _git(d, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(d, "update-ref", "refs/remotes/origin/feature/y", "HEAD")
    _git(d, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(d, "checkout", "-q", "feature/x")
    yield d
    repo_branches.clear_cache()


@pytest.fixture(autouse=True)
def _any_path_allowed(monkeypatch):
    """These repos live under pytest's tmp dir, outside the roots the route
    lets git run in (home tree, recent repos, live sessions)."""
    from backend.web import server as _server

    monkeypatch.setattr(_server, "_branches_path_allowed", lambda p: True)


def test_current_first_then_locals_then_remotes(repo):
    r = client.get("/api/repos/branches", params={"path": str(repo)})
    assert r.status_code == 200
    body = r.json()
    assert body["is_git"] is True
    assert body["current"] == "feature/x"
    # what the server cuts from when nothing is chosen: origin's HEAD
    assert body["default"] == "main"
    names = [b["name"] for b in body["branches"]]
    assert names[0] == "feature/x"
    assert body["branches"][0]["current"] is True
    locals_ = [b["name"] for b in body["branches"] if not b["remote"]]
    remotes = [b["name"] for b in body["branches"] if b["remote"]]
    assert set(locals_) == {"feature/x", "main", "old"}
    assert set(remotes) == {"origin/main", "origin/feature/y"}
    assert "origin/HEAD" not in names
    # locals before remotes
    assert names.index("old") < names.index("origin/main")
    assert body["truncated"] is False


def test_plain_folder_and_blank_path(tmp_path):
    repo_branches.clear_cache()
    r = client.get("/api/repos/branches", params={"path": str(tmp_path)})
    assert r.status_code == 200
    assert r.json()["is_git"] is False and r.json()["branches"] == []
    assert client.get("/api/repos/branches").status_code == 400
    assert client.get("/api/repos/branches", params={"path": " "}).status_code == 400


def test_answer_is_memoised_ten_seconds(repo):
    first = repo_branches.list_branches(str(repo), now=100.0)
    _git(repo, "branch", "later")
    # same folder, inside the window: the memo answers, the new branch is unseen
    again = repo_branches.list_branches(str(repo), now=105.0)
    assert again is first
    # past the window: a fresh listing
    fresh = repo_branches.list_branches(str(repo), now=111.0)
    assert "later" in [b["name"] for b in fresh["branches"]]


def test_cap_at_two_hundred(repo, monkeypatch):
    monkeypatch.setattr(repo_branches, "MAX_BRANCHES", 3)
    repo_branches.clear_cache()
    body = repo_branches.list_branches(str(repo))
    assert len(body["branches"]) == 3
    assert body["truncated"] is True
    # the cut is from the remote end: the checked-out branch always survives
    assert body["branches"][0]["name"] == "feature/x"


def test_forwarded_for_sessions_on_another_device():
    from backend.web.core import remote

    assert ("GET", "/api/repos/branches") in remote._FWD_ALLOWED
