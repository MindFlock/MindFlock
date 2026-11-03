"""The landing strip's data (``backend/web/core/landing.py``): the repos
in the sky, the git facts of a real tiny repo, "landed today", the checks
tone and the minute's cache."""

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest

from backend.web.core import landing as L


def _git(root, *args):
    subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return str(root)


@pytest.fixture(autouse=True)
def _fresh_cache():
    L.forget()
    yield
    L.forget()


def _inst(path, started=True):
    wt = SimpleNamespace(GetRepoPath=lambda: path)

    def gw():
        if not started:
            raise RuntimeError("not started")
        return wt

    return SimpleNamespace(Path=path, GetGitWorktree=gw, GetWorktreePath=lambda: path)


def test_repo_roots_are_distinct_local_and_existing(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    insts = {
        "one": _inst(str(a)),
        "two": _inst(str(a)),
        "three": _inst(str(b), started=False),
        "mini::four": _inst(str(b)),
        "gone": _inst(str(tmp_path / "nope")),
        "shared": SimpleNamespace(
            PeerShare="x",
            Path=str(b),
            GetGitWorktree=lambda: None,
            GetWorktreePath=lambda: str(b),
        ),
    }
    assert L.repo_roots(insts) == [str(a), str(b)]
    assert L.repo_roots({}) == []
    assert L.repo_roots(None) == []


def test_git_facts_of_a_repo_with_no_origin(repo):
    facts = L.repo_facts(repo)
    assert facts["root"] == repo and facts["name"] == "app"
    assert facts["branch"] == "main"
    assert len(facts["head"]) == 40
    # No merge commit yet: the last commit's time stands in.
    assert isinstance(facts["last_merge_at"], float) and facts["last_merge_at"] > 0
    # ...and says so, so the strip reads "last commit", never "merged".
    assert facts["last_merge_kind"] == "commit"
    # A merge commit is what the age reads.
    _git(repo, "checkout", "-q", "-b", "feat")
    (os.path.join(repo, "b.txt")) and open(os.path.join(repo, "b.txt"), "w").write("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "feat")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    merged = L.last_merge_at(repo, "main")
    assert merged is not None and merged >= facts["last_merge_at"]
    assert L.last_merge(repo, "main") == (merged, "merge")
    assert L.repo_facts(repo)["last_merge_kind"] == "merge"
    assert L.last_merge(repo, "") == (None, None)
    assert L.default_branch("/nonexistent/path") == ""
    assert L.head_of(repo, "") == ""


def _flight(
    fid,
    root,
    state="done",
    finished=1000.0,
    lane="merge",
    url="https://github.com/o/r/pull/1",
    ship_state="done",
):
    return {
        "id": fid,
        "session": "sess-" + fid,
        "repo_root": root,
        "state": state,
        "finished": finished,
        "steps": [
            {"id": "s1", "kind": "agent", "config": {}},
            {"id": "s2", "kind": "ship", "config": {"lane": lane}},
            {"id": "s3", "kind": "notify", "config": {}},
        ],
        "step_states": {
            "s1": {"state": "done", "detail": {}},
            "s2": {"state": ship_state, "detail": {"url": url}},
            "s3": {"state": "done", "detail": {}},
        },
    }


def test_landed_today_is_done_merged_recent_and_of_the_repo():
    now = 100_000.0
    flights = [
        _flight("ok", "/r", finished=now - 3600),
        _flight("old", "/r", finished=now - 2 * 86400),
        _flight("pushed", "/r", lane="push"),
        _flight("failed", "/r", state="failed", finished=now - 10),
        _flight("elsewhere", "/other", finished=now - 10),
        _flight("skipped-ship", "/r", finished=now - 20, ship_state="skipped"),
        _flight("newer", "/r", finished=now - 60, url=""),
        "junk",
    ]
    rows = L.landed(flights, "/r", now)
    assert [r["flight"] for r in rows] == ["newer", "ok"]
    assert rows[1] == {
        "flight": "ok",
        "title": "sess-ok",
        "pr_url": "https://github.com/o/r/pull/1",
        "at": now - 3600,
    }
    assert rows[0]["pr_url"] == ""
    # The last DONE ship step decides: a merge before a skipped push still landed.
    f = _flight("two-ships", "/r", finished=now - 5)
    f["steps"].append({"id": "s4", "kind": "ship", "config": {"lane": "push"}})
    f["step_states"]["s4"] = {"state": "skipped", "detail": {}}
    assert [r["flight"] for r in L.landed([f], "/r", now)] == ["two-ships"]


def test_checks_tone():
    assert [
        L.checks_tone(v) for v in ("ok", "failed", "pending", "none", "unknown", "")
    ] == [
        "green",
        "red",
        "pending",
        "none",
        "none",
        "none",
    ]


def test_landing_answers_and_caches(repo, monkeypatch):
    calls = []

    async def fake_checks(wt, sha):
        calls.append((wt, sha))
        return "ok"

    from backend.web.core import github_pr as gh

    monkeypatch.setattr(gh, "pr_checks", fake_checks)
    insts = {"one": _inst(repo)}
    flights = [_flight("f1", repo, finished=5000.0)]
    data = asyncio.run(L.landing(insts, flights, now=6000.0))
    assert data["at"] == 6000.0
    (row,) = data["repos"]
    assert row["name"] == "app" and row["branch"] == "main" and row["checks"] == "green"
    assert [r["flight"] for r in row["landed"]] == ["f1"]
    assert calls == [(repo, row["head"])]
    # Within the minute: the same answer, no second look.
    again = asyncio.run(L.landing(insts, [], now=6030.0))
    assert again["at"] == 6000.0 and [
        r["flight"] for r in again["repos"][0]["landed"]
    ] == ["f1"]
    assert len(calls) == 1
    # After it: asked again.
    later = asyncio.run(L.landing(insts, [], now=6100.0))
    assert later["repos"][0]["landed"] == [] and len(calls) == 2
    # A different set of repos is its own answer.
    assert asyncio.run(L.landing({}, [], now=6100.0)) == {"repos": [], "at": 6100.0}


def test_checks_failures_read_as_none(repo, monkeypatch):
    async def boom(wt, sha):
        raise RuntimeError("offline")

    from backend.web.core import github_pr as gh

    monkeypatch.setattr(gh, "pr_checks", boom)
    data = asyncio.run(L.landing({"one": _inst(repo)}, [], now=1.0))
    assert data["repos"][0]["checks"] == "none"
