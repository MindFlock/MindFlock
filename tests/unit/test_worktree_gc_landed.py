"""Disk cleanup's landed rung (backend/web/core/worktree_gc.py ``landed``):
work that reached a base branch through a squash or rebase merge, as other
commits, and the base moved on since — touching the same lines (a CHANGELOG
entry), so the whole-branch merge test against the base's tip conflicts.

The rule under test: the worktree is safe only when a commit on the base's
first-parent history, after the fork, already holds every change the branch
makes byte for byte (merging the branch into it merges cleanly and gives
back its own tree) — and nothing else keeps it. Anything git can't answer,
or answers oddly, keeps it. Real throwaway repositories throughout."""

from __future__ import annotations

import os
import subprocess

from backend.web.core import worktree_gc as gc
from tests.unit.test_worktree_gc import (  # noqa: F401 — env is a fixture
    LATER,
    add_wt,
    branches,
    commit,
    env,
    git,
    items,
    recheck_with,
)

CL_HEAD = "# Changelog\n\n## Unreleased\n\n"


def write(cwd, name, text):
    with open(os.path.join(str(cwd), name), "w") as f:
        f.write(text)


def changelog(env):
    """A CHANGELOG.md on main that every branch adds an entry to."""
    write(env["repo"], "CHANGELOG.md", CL_HEAD + "- older entry\n")
    git(env["repo"], "add", "CHANGELOG.md")
    git(env["repo"], "commit", "-q", "-m", "changelog")


def feature(env, name):
    """A worktree with two commits: a CHANGELOG entry and a new file."""
    wt = add_wt(env, name)
    write(wt, "CHANGELOG.md", CL_HEAD + "- %s entry\n- older entry\n" % name)
    git(wt, "commit", "-q", "-am", "%s: changelog" % name)
    commit(wt, name + ".py", "print('%s')\n" % name)
    return wt


def squash_into_main(env, branch, msg="squash"):
    git(env["repo"], "merge", "-q", "--squash", branch)
    git(env["repo"], "commit", "-q", "-m", msg)
    return git(env["repo"], "rev-parse", "HEAD")


def release(env, name="1.0"):
    """main moves on and rewrites the very lines the branch touched."""
    path = os.path.join(env["repo"], "CHANGELOG.md")
    text = open(path).read()
    write(
        env["repo"],
        "CHANGELOG.md",
        text.replace(
            "## Unreleased\n\n", "## Unreleased\n\n- next entry\n\n## %s\n\n" % name
        ),
    )
    git(env["repo"], "commit", "-q", "-am", "release " + name)


def tip_test(env, wt):
    """What the whole-branch test against the base's tip says (the old rung)."""
    head = git(wt, "rev-parse", "HEAD")
    return gc.merged_into(env["repo"], head, gc.base_refs(env["repo"]))


# ------------------------------------------------------------------- safe


def test_squash_merged_then_the_base_rewrote_the_same_lines_is_safe(env):
    """The owner's case: squash-merged (GitHub squash-only), and every later
    commit on main edits the CHANGELOG lines the branch added. The tip test
    conflicts; the commit the squash made holds all of it."""
    changelog(env)
    wt = feature(env, "feat")
    landed_at = squash_into_main(env, "u/feat")
    release(env)
    assert tip_test(env, wt) == ""  # what kept it before
    it = items(env)["feat"]
    assert it["safe"] and it["state"] == gc.SAFE, it["kept"]
    assert it["merged"] == "content" and it["merged_how"] == "squash"
    assert it["merged_at"] == landed_at and it["merged_into"] == "main"
    assert it["unsaved_commits"] == 0
    assert it["safe_reason"] == "its changes are already in main (squash-merged)"
    res = gc.collect([it], recheck_with(env), root=env["root"])
    assert [r["path"] for r in res["removed"]] == [wt]
    assert "u/feat" not in branches(env["repo"])


def test_rebase_merged_then_the_base_moved_on_is_safe(env):
    changelog(env)
    wt = feature(env, "rb")
    commit(env["repo"], "other.txt", "o\n")  # main moved: new hashes
    for sha in git(wt, "rev-list", "--reverse", "main..HEAD").split():
        git(env["repo"], "cherry-pick", sha)
    release(env)
    assert tip_test(env, wt) == ""
    it = items(env)["rb"]
    assert it["safe"], it["kept"]
    assert it["merged_how"] == "rebase"
    assert it["safe_reason"] == "its changes are already in main (rebase-merged)"


def test_a_worker_merged_into_an_integration_branch_that_was_squashed(env, monkeypatch):
    """Worker → integration branch (a real merge) → squash into main → main
    moves on: the worker's commits exist nowhere in main by hash."""
    changelog(env)
    git(env["repo"], "branch", "integ", "main")
    feature(env, "worker")
    # A second worker adds its own CHANGELOG entry a few lines further down,
    # so the squash commit holds a THIRD version of that file: neither
    # worker's blob, both workers' lines (the 3-way merge-file path).
    w2 = add_wt(env, "worker2")
    write(w2, "CHANGELOG.md", CL_HEAD + "- older entry\n- worker2 entry\n")
    git(w2, "commit", "-q", "-am", "worker2: changelog")
    git(env["repo"], "checkout", "-q", "integ")
    git(env["repo"], "merge", "-q", "--no-ff", "-m", "merge worker", "u/worker")
    git(env["repo"], "merge", "-q", "--no-ff", "-m", "merge w2", "u/worker2")
    commit(env["repo"], "integ-only.txt", "glue\n")
    git(env["repo"], "checkout", "-q", "main")
    squash_into_main(env, "integ", "integration (#7)")
    git(env["repo"], "branch", "-D", "integ")
    release(env)
    merges = []
    real = gc._merge_is_noop
    monkeypatch.setattr(gc, "_merge_is_noop", lambda *a: merges.append(1) or real(*a))
    got = items(env)
    for name in ("worker", "worker2"):
        assert got[name]["safe"], got[name]["kept"]
        assert got[name]["merged"] == "content"
    assert merges  # the third-version file went through merge-file


def test_a_third_version_that_rewrote_the_branch_line_holds_work(env):
    """The squash commit has the file, but with the branch's line changed
    (a reviewer edited it on merge): the branch's text is in no base commit."""
    changelog(env)
    wt = feature(env, "edited")
    git(env["repo"], "merge", "-q", "--squash", "u/edited")
    write(
        env["repo"],
        "CHANGELOG.md",
        CL_HEAD + "- edited entry, reworded on merge\n- older entry\n",
    )
    git(env["repo"], "add", "CHANGELOG.md")
    git(env["repo"], "commit", "-q", "-m", "squash, reworded")
    release(env)
    it = items(env)["edited"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
    assert wt


def test_a_later_revert_in_the_base_does_not_turn_it_back_into_work(env):
    """Same as an ancestor-merged branch whose change main later reverted:
    the work is in main's history (the landing commit), main chose to drop
    it. Removing the worktree loses nothing main doesn't still hold."""
    changelog(env)
    wt = feature(env, "rev")
    landed_at = squash_into_main(env, "u/rev")
    git(env["repo"], "revert", "--no-edit", landed_at)
    it = items(env)["rev"]
    assert it["safe"] and it["merged_at"] == landed_at, it["kept"]
    assert wt


def test_squash_merged_into_staging_after_staging_moved_on(env):
    changelog(env)
    git(env["repo"], "branch", "staging")
    feature(env, "tk")
    git(env["repo"], "checkout", "-q", "staging")
    squash_into_main(env, "u/tk")
    release(env, "staging-1")
    git(env["repo"], "checkout", "-q", "main")
    it = items(env)["tk"]
    assert it["safe"] and it["merged_into"] == "staging", it["kept"]


# ------------------------------------------------------------ holds work


def test_one_more_commit_after_the_squash_holds_work(env):
    changelog(env)
    wt = feature(env, "more")
    squash_into_main(env, "u/more")
    commit(wt, "after.txt", "new work\n")
    release(env)
    it = items(env)["more"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
    assert it["merged"] == "" and it["unsaved_commits"] == 3
    assert it["kept"] == ["3 commits in no base branch and on no remote"]


def test_an_amended_line_after_the_squash_holds_work(env):
    """The branch changed a file the squash already carried: the newer text
    is in no base commit."""
    changelog(env)
    wt = feature(env, "amend")
    squash_into_main(env, "u/amend")
    commit(wt, "amend.py", "print('changed after merge')\n")
    it = items(env)["amend"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_only_part_of_the_branch_landed_holds_work(env):
    changelog(env)
    wt = feature(env, "half")
    # main took the CHANGELOG commit only, not the new file.
    first = git(wt, "rev-list", "--reverse", "main..HEAD").split()[0]
    commit(env["repo"], "other.txt", "o\n")
    git(env["repo"], "cherry-pick", first)
    release(env)
    it = items(env)["half"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_squash_merged_with_an_uncommitted_change_holds_work_unprobed(env, monkeypatch):
    changelog(env)
    wt = feature(env, "dirty")
    squash_into_main(env, "u/dirty")
    release(env)
    with open(os.path.join(wt, "dirty.py"), "a") as f:
        f.write("# wip\n")
    asked = []
    real = gc.landed
    monkeypatch.setattr(gc, "landed", lambda *a: asked.append(a) or real(*a))
    it = items(env)["dirty"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
    assert "1 uncommitted change" in it["kept"]
    assert asked == []  # the probe runs only where it can change the verdict


def test_squash_merged_with_an_untracked_file_holds_work(env):
    changelog(env)
    wt = feature(env, "untr")
    squash_into_main(env, "u/untr")
    write(wt, "notes.txt", "mine\n")
    it = items(env)["untr"]
    assert not it["safe"] and "1 untracked file" in it["kept"]


def test_a_stash_made_on_the_branch_holds_work(env):
    changelog(env)
    wt = feature(env, "st")
    squash_into_main(env, "u/st")
    release(env)
    write(wt, "st.py", "print('stashed')\n")
    git(wt, "stash", "push", "-q", "-m", "keep me")
    it = items(env)["st"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
    assert "1 stash entry made on it" in it["kept"]


def test_recent_squash_merged_work_is_kept_and_not_probed(env, monkeypatch):
    changelog(env)
    feature(env, "young")
    squash_into_main(env, "u/young")
    release(env)
    monkeypatch.setattr(gc, "landed", lambda *a: (_ for _ in ()).throw(AssertionError))
    it = items(env, now=None)["young"]
    assert it["state"] == gc.HOLDS_WORK and not it["safe"]


def test_never_merged_holds_work(env):
    changelog(env)
    feature(env, "wip")
    release(env)
    it = items(env)["wip"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
    assert it["kept"] == ["2 commits in no base branch and on no remote"]


def test_a_branch_longer_than_the_cap_holds_work(env, monkeypatch):
    changelog(env)
    feature(env, "long")
    squash_into_main(env, "u/long")
    release(env)
    monkeypatch.setattr(gc, "LANDED_MAX_COMMITS", 1)
    it = items(env)["long"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_the_tries_are_bounded(env, monkeypatch):
    """The landing commit sits behind more candidates than the budget: kept
    (unsure), never an unbounded walk of history."""
    changelog(env)
    wt = feature(env, "deep")
    squash_into_main(env, "u/deep")
    for n in range(4):  # later commits touching both of the branch's files
        write(env["repo"], "deep.py", "print(%d)\n" % n)
        release(env, "r%d" % n)
        git(env["repo"], "commit", "-q", "-am", "edit %d" % n, "--allow-empty")
    monkeypatch.setattr(gc, "LANDED_MAX_TRIES", 2)
    calls = []
    real = gc._contains_changes
    monkeypatch.setattr(
        gc, "_contains_changes", lambda *a, **k: calls.append(a[2]) or real(*a, **k)
    )
    assert (
        gc.landed(env["repo"], git(wt, "rev-parse", "HEAD"), ["refs/heads/main"])
        is None
    )
    assert len(calls) == 2


# ----------------------------------------------------- odd git, no writes


def test_odd_git_output_holds_work_and_never_crashes(env, monkeypatch):
    changelog(env)
    feature(env, "odd")
    squash_into_main(env, "u/odd")
    release(env)
    real = gc._git

    def garbled(cwd, *args, **kw):
        cp = real(cwd, *args, **kw)
        if args and args[0] in ("diff", "ls-tree", "log", "rev-list", "merge-base"):
            return subprocess.CompletedProcess(cp.args, 0, "not a sha\n\x00?", "")
        if args and args[0] == "cat-file":
            return subprocess.CompletedProcess(cp.args, 0, b"garbage", b"")
        return cp

    monkeypatch.setattr(gc, "_git", garbled)
    it = items(env)["odd"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_failing_git_holds_work_and_never_crashes(env, monkeypatch):
    changelog(env)
    feature(env, "fail")
    squash_into_main(env, "u/fail")
    release(env)
    real = gc._git

    def failing(cwd, *args, **kw):
        if args and args[0] in ("diff", "ls-tree", "cat-file", "stash"):
            return subprocess.CompletedProcess(args, 129, "", "unknown option")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(gc, "_git", failing)
    it = items(env)["fail"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_an_exception_in_the_probe_holds_work(env, monkeypatch):
    changelog(env)
    feature(env, "boom")
    squash_into_main(env, "u/boom")
    release(env)
    monkeypatch.setattr(gc, "landed", lambda *a: 1 / 0)
    it = items(env)["boom"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_the_landed_probe_writes_nothing_into_the_repository(env):
    changelog(env)
    feature(env, "clean")
    squash_into_main(env, "u/clean")
    release(env)
    gitdir = os.path.join(env["repo"], ".git")

    def snapshot():
        out = {}
        for d, _, fs in os.walk(gitdir):
            for f in fs:
                p = os.path.join(d, f)
                out[p] = os.stat(p).st_mtime_ns
        return out

    before = snapshot()
    it = items(env)["clean"]
    assert it["merged_how"] == "squash"  # the probe ran and found it
    assert snapshot() == before


def test_a_patch_equal_commit_that_differs_in_whitespace_holds_work(env):
    """Patch ids ignore whitespace; the merge test does not. main carries
    the branch's change re-indented — in Python that is other code."""
    wt = add_wt(env, "ws")
    write(wt, "code.py", "def f():\n    if x:\n        return 1\n")
    git(wt, "add", "code.py")
    git(wt, "commit", "-q", "-m", "add f")
    commit(env["repo"], "other.txt", "o\n")
    write(env["repo"], "code.py", "def f():\n    if x:\n    return 1\n")
    git(env["repo"], "add", "code.py")
    git(env["repo"], "commit", "-q", "-m", "add f")
    marked = git(env["repo"], "rev-list", "--cherry-mark", "--left-only", "main...u/ws")
    assert marked.startswith("=")  # git calls them the same patch
    it = items(env)["ws"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK


def test_the_listing_says_why_in_plain_words(env, capsys):
    from backend import cli_clean

    changelog(env)
    feature(env, "said")
    squash_into_main(env, "u/said")
    release(env)
    its = gc.inventory(gc.Usage(repos=[env["repo"]]), root=env["root"], now=LATER)
    out = cli_clean.render({"items": its, "worktrees_root": env["root"]})
    assert "its changes are already in main (squash-merged)" in out


def test_a_probe_past_its_time_budget_holds_work(env, monkeypatch):
    changelog(env)
    feature(env, "slow")
    squash_into_main(env, "u/slow")
    release(env)
    monkeypatch.setattr(gc, "LANDED_BUDGET_S", 1e-9)
    it = items(env)["slow"]
    assert not it["safe"] and it["state"] == gc.HOLDS_WORK
