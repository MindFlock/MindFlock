"""Disk cleanup judges a worktree against FRESH base refs
(backend/web/core/worktree_gc.py, "fresh bases"): before classifying, each
repository's ``origin/<base>`` refs are fetched once (throttled), so work
merged on the remote since the last fetch counts as merged. Real temp repos
with a local directory as ``origin`` — the suite never fetches anything
else (tests/conftest.py)."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from backend.session.git import created
from backend.web.core import worktree_gc as gc
from tests.unit.test_worktree_gc import LATER, commit, git

DAY = 24 * 3600.0


@pytest.fixture
def fenv(tmp_path, monkeypatch):
    """``upstream`` (the "remote"), ``repo`` = a clone of it (origin =
    upstream), and a MindFlock worktree of ``repo`` holding one commit that
    is on no remote."""
    home = tmp_path / "home"
    home.mkdir()
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("")
    monkeypatch.setenv("MINDFLOCK_HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    gc._caches.clear()
    up = tmp_path / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    commit(up, "README", "hello\n")
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(up), str(repo)], check=True, capture_output=True
    )
    root = home / "worktrees"
    wt = os.path.join(str(root), "u", "feat")
    git(repo, "worktree", "add", "-q", "-b", "u/feat", wt, "origin/main")
    created.record_created(wt, str(repo), "u/feat", title="feat")
    commit(wt, "feat.txt", "the feature\n", msg="feature")
    return {
        "up": str(up),
        "repo": str(repo),
        "wt": wt,
        "root": str(root),
        "home": str(home),
    }


def squash_upstream(fenv) -> None:
    """The feature lands on the remote as a squash commit, and the remote's
    main moves on after it — none of which the clone has fetched."""
    commit(fenv["up"], "feat.txt", "the feature\n", msg="feat (#1)")
    commit(fenv["up"], "README", "moved on\n", msg="later")


def the_item(fenv, **kw):
    kw.setdefault("now", LATER)
    its = gc.inventory(gc.Usage(repos=[fenv["repo"]]), root=fenv["root"], **kw)
    return next(i for i in its if i["path"] == gc.norm(fenv["wt"]))


def snapshot(fenv) -> dict:
    """Everything the fetch must leave alone: local branches, HEADs, both
    indexes (bytes and mtime), the worktree's files."""

    def index(path):
        p = git(path, "rev-parse", "--git-path", "index")
        p = p if os.path.isabs(p) else os.path.join(path, p)
        with open(p, "rb") as f:
            return f.read(), os.stat(p).st_mtime_ns

    return {
        "heads": git(
            fenv["repo"],
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
        ),
        "head": git(fenv["repo"], "rev-parse", "HEAD"),
        "wt_head": git(fenv["wt"], "rev-parse", "HEAD"),
        "index": index(fenv["repo"]),
        "wt_index": index(fenv["wt"]),
        # --no-optional-locks: a plain status would refresh the index itself.
        "status": git(fenv["wt"], "--no-optional-locks", "status", "--porcelain"),
        "files": sorted(os.listdir(fenv["wt"])),
    }


def test_a_stale_base_keeps_it_and_a_fresh_one_finds_it_merged(fenv):
    squash_upstream(fenv)
    before = snapshot(fenv)

    stale = the_item(fenv)  # no fetch at all (the library default)
    assert not stale["safe"]
    assert stale["kept"] == ["1 commit in no base branch and on no remote"]

    fresh = the_item(fenv, fetch=True)
    assert fresh["safe"], fresh["kept"]
    assert fresh["merged"] == "content" and fresh["merged_into"] == "main"
    # The only write: refs/remotes/origin/main moved to the remote's main.
    assert git(fenv["repo"], "rev-parse", "origin/main") == git(
        fenv["up"], "rev-parse", "main"
    )
    assert snapshot(fenv) == before


def test_without_fetch_the_reason_says_the_base_may_be_stale(fenv):
    squash_upstream(fenv)
    it = the_item(fenv, fetch=False)  # --no-fetch: checked, said, not fetched
    assert not it["safe"] and it["base_stale"]
    (why,) = it["kept"]
    assert why == (
        "1 commit in no base branch and on no remote (origin/main was not "
        "refreshed (--no-fetch) and when it was last fetched is unknown, so it "
        "may be merged since)"
    )
    # A fetch a day before "now" (LATER is a month on): its age is said.
    fh = os.path.join(fenv["repo"], ".git", "FETCH_HEAD")
    open(fh, "w").close()
    t = time.time() - DAY
    os.utime(fh, (t, t))
    (why,) = the_item(fenv, fetch=False)["kept"]
    assert (
        "(origin/main was last fetched 31 days ago and not refreshed (--no-fetch)"
        in why
    )


def test_a_failed_fetch_keeps_it_never_crashes_and_says_so(fenv):
    squash_upstream(fenv)
    git(fenv["repo"], "remote", "set-url", "origin", os.path.join(fenv["up"], "gone"))
    before = snapshot(fenv)
    it = the_item(fenv, fetch=True)
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
    (why,) = it["kept"]
    assert "couldn't be refreshed" in why and "may be merged since" in why
    assert snapshot(fenv) == before


def test_fresher_refs_never_turn_unmerged_work_safe(fenv):
    # The remote moves on, but without the feature.
    commit(fenv["up"], "other.txt", "someone else's\n", msg="other")
    it = the_item(fenv, fetch=True)
    assert not it["safe"]
    assert it["kept"] == ["1 commit in no base branch and on no remote"]


def test_only_the_base_branches_are_fetched_no_tags_no_fetch_head(fenv):
    squash_upstream(fenv)
    git(fenv["up"], "branch", "someone-elses")
    git(fenv["up"], "tag", "v9")
    git(fenv["repo"], "branch", "-r", "-d", "origin/HEAD")  # nothing else names it
    common = os.path.join(fenv["repo"], ".git")
    res = gc.refresh_bases({fenv["repo"]: {""}}, now=LATER)[fenv["repo"]]
    assert res["status"] == "fetched" and res["names"] == ["main"]
    remotes = git(fenv["repo"], "for-each-ref", "--format=%(refname)", "refs/remotes")
    assert remotes.split() == ["refs/remotes/origin/main"]
    assert git(fenv["repo"], "tag", "--list") == ""
    assert not os.path.exists(os.path.join(common, "FETCH_HEAD"))
    # The throttle stamp is MindFlock's own file, not the repository's.
    assert os.path.isfile(os.path.join(fenv["home"], gc.FETCH_STATE_FILE))


def test_the_fetch_is_throttled_per_repository(fenv, monkeypatch):
    calls = []
    real = gc._fetch_bases

    def counting(repo, names, *a, **kw):
        calls.append((repo, tuple(names)))
        return real(repo, names, *a, **kw)

    monkeypatch.setattr(gc, "_fetch_bases", counting)
    hints = {fenv["repo"]: {"", "main"}}
    first = gc.refresh_bases(hints, now=LATER)[fenv["repo"]]
    again = gc.refresh_bases(hints, now=LATER + 60)[fenv["repo"]]
    assert first["status"] == "fetched" and again["status"] == "recent"
    assert not again["stale"]
    assert len(calls) == 1
    later = gc.refresh_bases(hints, now=LATER + gc.FETCH_TTL_S + 1)[fenv["repo"]]
    assert later["status"] == "fetched" and len(calls) == 2
    # Many worktrees of one repository: still one fetch per scan.
    for name in ("a", "b", "c"):
        git(fenv["repo"], "worktree", "add", "-q", "-b", "u/" + name,
            os.path.join(fenv["root"], "u", name), "origin/main")  # fmt: skip
    gc.inventory(
        gc.Usage(repos=[fenv["repo"]]),
        root=fenv["root"],
        now=LATER + 3 * gc.FETCH_TTL_S,
        fetch=True,
    )
    assert len(calls) == 3


def test_a_failed_fetch_is_throttled_too_and_stays_stale(fenv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        gc,
        "_fetch_bases",
        lambda repo, names, *a, **k: calls.append(1) or (False, "offline"),
    )
    hints = {fenv["repo"]: {""}}
    a = gc.refresh_bases(hints, now=LATER)[fenv["repo"]]
    b = gc.refresh_bases(hints, now=LATER + 60)[fenv["repo"]]
    assert a["status"] == "failed" and a["error"] == "offline" and a["stale"]
    assert b["status"] == "recent" and b["error"] == "offline" and b["stale"]
    assert len(calls) == 1


def test_a_repository_without_a_remote_is_neither_fetched_nor_stale(fenv, monkeypatch):
    git(fenv["repo"], "remote", "remove", "origin")
    monkeypatch.setattr(
        gc, "_fetch_bases", lambda *a, **k: pytest.fail("no remote: no fetch")
    )
    res = gc.refresh_bases({fenv["repo"]: {""}}, now=LATER)[fenv["repo"]]
    assert res["status"] in ("no_remote", "no_base") and not res["stale"]
    assert gc.stale_note(res) == ""


def test_a_local_base_behind_its_remote_tracking_ref_does_not_hide_merged_work(fenv):
    """The worktree's repo has a LOCAL main that lags origin/main (nobody
    pulled): what is in origin/main is merged all the same, and the local
    branch is left exactly where it was."""
    squash_upstream(fenv)
    local_main = git(fenv["repo"], "rev-parse", "main")
    it = the_item(fenv, fetch=True)
    assert it["safe"]
    assert git(fenv["repo"], "rev-parse", "main") == local_main
    assert gc.base_refs(fenv["repo"]) == ["refs/heads/main", "refs/remotes/origin/main"]


def test_the_fetch_cannot_prompt(fenv, monkeypatch):
    seen = {}
    real = gc.gitutil.git

    def spy(args, cwd=None, **kw):
        if "fetch" in args:
            seen.update(kw.get("env") or {})
            seen["new_session"] = kw.get("new_session")
            seen["args"] = list(args)
        return real(args, cwd, **kw)

    monkeypatch.setattr(gc.gitutil, "git", spy)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    gc.refresh_bases({fenv["repo"]: {""}}, now=LATER)
    assert seen["GIT_TERMINAL_PROMPT"] == "0" and seen["GIT_ASKPASS"] == ""
    assert "BatchMode=yes" in seen["GIT_SSH_COMMAND"]
    assert seen["new_session"] is True
    for flag in ("--no-tags", "--no-prune", "--no-write-fetch-head"):
        assert flag in seen["args"]
