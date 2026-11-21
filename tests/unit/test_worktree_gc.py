"""The worktree garbage collector's git half (backend/web/core/worktree_gc.py),
against throwaway repositories only: what counts as merged (ancestor, squash,
a squashed history, a merged PR), what keeps an item (in use, uncommitted,
unpushed, recent, not ours, orphaned), that collection re-checks and removes
only the safe set, deletes only merged branches MindFlock cut, and that a
removal on request saves the work first."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from backend.session.git import created
from backend.web.core import worktree_gc as gc

DAY = 24 * 3600.0
LATER = time.time() + 30 * DAY  # "now" a month on: everything is old


def git(cwd, *args, check=True):
    cp = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and cp.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), cp.stderr))
    return cp.stdout.strip()


def commit(cwd, name, text="x\n", msg=None):
    p = os.path.join(str(cwd), name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", msg or ("add " + name))
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "README", "hello\n")
    root = home / "worktrees"
    root.mkdir()
    return {"tmp": tmp_path, "home": home, "repo": str(repo), "root": str(root)}


def add_wt(env, name, branch=None, *, record=True, existing=False, start="main"):
    path = os.path.join(env["root"], "u", name)
    branch = branch or ("u/" + name)
    git(env["repo"], "worktree", "add", "-q", "-b", branch, path, start)
    if record:
        created.record_created(
            path, env["repo"], branch, title=name, existing_branch=existing
        )
    return path


def items(env, usage=None, **kw):
    usage = usage or gc.Usage(repos=[env["repo"]])
    kw.setdefault("now", LATER)
    return {
        os.path.basename(i["path"]): i
        for i in gc.inventory(usage, root=env["root"], **kw)
    }


def recheck_with(env, usage=None):
    usage = usage or gc.Usage(repos=[env["repo"]])

    def recheck(it):
        rec = gc._record(
            it["path"],
            it["kind"],
            it["repo"],
            branch=it["branch"],
            head=(
                git(it["path"], "rev-parse", "HEAD")
                if os.path.isdir(it["path"])
                else ""
            ),
            detached=it.get("detached", False),
        )
        return gc.classify(rec, usage, now=LATER, root=env["root"], measure=False)

    return recheck


def branches(repo):
    return set(
        git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    )


# ------------------------------------------------------------- merged-ness


def test_a_merged_clean_old_worktree_is_safe_and_collected_with_its_branch(env):
    wt = add_wt(env, "done")
    commit(wt, "a.txt")
    git(env["repo"], "merge", "-q", "--no-ff", "-m", "merge", "u/done")
    it = items(env)["done"]
    assert it["safe"] and it["state"] == gc.SAFE and it["merged"] == "ancestor"
    assert it["size_bytes"] and it["size_bytes"] > 0
    res = gc.collect([it], recheck_with(env), root=env["root"])
    assert [r["path"] for r in res["removed"]] == [wt]
    assert not os.path.exists(wt)
    assert "u/done" not in branches(env["repo"])
    assert res["branches_deleted"] == ["u/done"]
    assert gc.norm(wt) not in created.load_records()


def test_a_squash_merged_branch_counts_as_merged(env):
    wt = add_wt(env, "squashed")
    commit(wt, "a.txt", "one\n")
    commit(wt, "b.txt", "two\n")
    git(env["repo"], "merge", "-q", "--squash", "u/squashed")
    git(env["repo"], "commit", "-q", "-m", "squash")
    commit(env["repo"], "later.txt")  # main moved on since
    it = items(env)["squashed"]
    assert it["merged"] == "content" and it["safe"]
    gc.collect([it], recheck_with(env), root=env["root"])
    assert "u/squashed" not in branches(env["repo"])


def test_the_merge_probe_writes_nothing_into_the_repository(env):
    """merge-tree --write-tree writes objects: a scan must not leave any in
    the person's repository (they go to a throwaway object dir)."""
    wt = add_wt(env, "probe")
    commit(wt, "README", "mine\n")
    commit(env["repo"], "other.txt", "theirs\n")

    def loose():
        objs = os.path.join(env["repo"], ".git", "objects")
        return sorted(
            os.path.join(d, f)
            for d, _, fs in os.walk(objs)
            if os.path.basename(d) not in ("info", "pack")
            for f in fs
        )

    before = loose()
    it = items(env)["probe"]
    assert it["merged"] == ""  # the probe ran (and found real work)
    assert loose() == before


def test_a_main_squashed_to_one_commit_still_counts_the_branch_merged(env):
    """The owner squashes main to a single 'initial commit' now and then:
    afterwards no branch shares history with it at all."""
    wt = add_wt(env, "old")
    commit(wt, "feature.txt", "f\n")
    git(env["repo"], "merge", "-q", "u/old")
    tree = git(env["repo"], "rev-parse", "HEAD^{tree}")
    root_commit = git(env["repo"], "commit-tree", tree, "-m", "initial commit")
    git(env["repo"], "update-ref", "refs/heads/main", root_commit)
    git(env["repo"], "reset", "-q", "--hard", "main")
    it = items(env)["old"]
    assert it["merged"] == "content" and it["safe"]


def test_a_conflicting_branch_is_not_merged_and_is_kept(env):
    wt = add_wt(env, "clash")
    commit(wt, "README", "mine\n")
    commit(env["repo"], "README", "theirs\n")
    it = items(env)["clash"]
    assert it["merged"] == "" and not it["safe"]
    assert it["state"] == gc.HOLDS_WORK
    assert any("1 commit in no base branch and on no remote" == r for r in it["kept"])


def test_pushed_but_unmerged_is_removed_and_its_branch_kept(env, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(env["repo"], "remote", "add", "origin", str(origin))
    git(env["repo"], "push", "-q", "origin", "main")
    wt = add_wt(env, "pushed")
    commit(wt, "README", "different\n")
    commit(env["repo"], "README", "conflict\n")
    git(wt, "push", "-q", "origin", "u/pushed")
    it = items(env)["pushed"]
    assert it["merged"] == "" and it["unsaved_commits"] == 0 and it["safe"]
    res = gc.collect([it], recheck_with(env), root=env["root"])
    assert res["removed"] and not os.path.exists(wt)
    assert "u/pushed" in branches(env["repo"])  # pushed is not merged
    assert res["branches_deleted"] == []


def test_a_merged_pr_at_this_head_is_merged(env):
    wt = add_wt(env, "pr")
    commit(wt, "README", "pr version\n")
    commit(env["repo"], "README", "edited after\n")
    head = git(wt, "rev-parse", "HEAD")
    asked = []

    def pr_merged(item):
        asked.append(item["branch"])
        return item["head"] == head

    usage = gc.Usage(repos=[env["repo"]], pr_merged=pr_merged)
    it = items(env, usage)["pr"]
    assert asked == ["u/pr"]
    assert it["merged"] == "pr" and it["safe"]


def test_the_pr_rung_is_not_asked_when_something_else_keeps_it(env):
    wt = add_wt(env, "dirtypr")
    commit(wt, "README", "pr version\n")
    commit(env["repo"], "README", "edited after\n")
    (open(os.path.join(wt, "new.txt"), "w")).write("wip")
    asked = []
    usage = gc.Usage(repos=[env["repo"]], pr_merged=lambda i: asked.append(1) or True)
    it = items(env, usage)["dirtypr"]
    assert asked == [] and not it["safe"]


# -------------------------------------------------------------- what keeps


def test_uncommitted_and_untracked_keep_it_but_ignored_files_do_not(env):
    a = add_wt(env, "tracked")
    with open(os.path.join(a, "README"), "a") as f:
        f.write("more\n")
    b = add_wt(env, "untracked")
    open(os.path.join(b, "new.txt"), "w").write("wip")
    c = add_wt(env, "ignored")
    open(os.path.join(c, ".gitignore"), "w").write("node_modules/\n.gitignore\n")
    os.makedirs(os.path.join(c, "node_modules"))
    open(os.path.join(c, "node_modules", "x.js"), "w").write("1")
    got = items(env)
    assert got["tracked"]["kept"] == ["1 uncommitted change"]
    assert got["untracked"]["kept"] == ["1 untracked file"]
    assert got["tracked"]["state"] == got["untracked"]["state"] == gc.HOLDS_WORK
    assert got["ignored"]["safe"]


def test_the_scan_never_rewrites_the_index(env):
    wt = add_wt(env, "idx")
    gd = gc.gitdir_of(wt)
    idx = os.path.join(gd, "index")
    os.utime(idx, (1_000_000, 1_000_000))
    items(env)
    assert os.stat(idx).st_mtime == 1_000_000


def test_recently_used_is_kept(env):
    add_wt(env, "fresh")
    it = items(env, now=time.time())["fresh"]
    assert it["state"] == gc.RECENT and not it["safe"]
    assert it["kept"][-1].startswith("used ")


def test_the_threshold_is_a_setting(env):
    add_wt(env, "week")
    now = time.time() + 7 * DAY
    assert items(env, now=now)["week"]["state"] == gc.RECENT
    assert items(env, now=now, after_days=3)["week"]["safe"]


def test_in_use_is_kept_and_never_removed_even_on_request(env):
    wt = add_wt(env, "busy")
    usage = gc.Usage(repos=[env["repo"]], in_use={wt: "session 'busy' (paused)"})
    it = items(env, usage)["busy"]
    assert it["state"] == gc.IN_USE and it["kept"] == ["session 'busy' (paused)"]
    res = gc.remove_one(it, confirmed=True, usage=usage, root=env["root"])
    assert not res["ok"] and os.path.isdir(wt)


def test_a_worktree_mindflock_did_not_make_is_listed_never_collected(env, tmp_path):
    mine = tmp_path / "repo" / ".claude" / "worktrees" / "agent-x"
    git(env["repo"], "worktree", "add", "-q", "-b", "agent-x", str(mine))
    it = items(env)["agent-x"]
    assert it["state"] == gc.NOT_OURS and not it["safe"]
    assert "not created by MindFlock" in it["kept"]
    gc.collect([dict(it, safe=True)], recheck_with(env), root=env["root"])
    assert mine.is_dir()


def test_a_recorded_worktree_outside_the_root_is_ours(env, tmp_path):
    other = tmp_path / "elsewhere" / "wt"
    git(env["repo"], "worktree", "add", "-q", "-b", "elsewhere", str(other))
    created.record_created(str(other), env["repo"], "elsewhere")
    assert items(env)["wt"]["ours"]


def test_the_main_checkout_is_never_an_item(env):
    add_wt(env, "one")
    paths = {
        i["path"]
        for i in gc.inventory(
            gc.Usage(repos=[env["repo"]]), root=env["root"], now=LATER
        )
    }
    assert gc.norm(env["repo"]) not in paths


def test_an_orphan_folder_is_listed_never_collected(env):
    orphan = os.path.join(env["root"], "u", "gone_abc")
    os.makedirs(orphan)
    open(os.path.join(orphan, ".git"), "w").write(
        "gitdir: /nonexistent/.git/worktrees/gone\n"
    )
    open(os.path.join(orphan, "notes.txt"), "w").write("?")
    it = items(env)["gone_abc"]
    assert it["kind"] == gc.KIND_ORPHAN and not it["safe"]
    assert "can't be checked" in it["kept"][-1]
    res = gc.remove_one(it, confirmed=False, root=env["root"])
    assert res.get("needs_confirm") and os.path.isdir(orphan)
    res = gc.remove_one(it, confirmed=True, root=env["root"])
    assert res["ok"] and not os.path.exists(orphan)


def test_a_locked_worktree_is_kept(env):
    wt = add_wt(env, "locked")
    git(env["repo"], "worktree", "lock", wt)
    it = items(env)["locked"]
    assert not it["safe"] and "locked in git" in it["kept"]


def test_a_missing_folder_is_reported_not_counted(env):
    import shutil

    wt = add_wt(env, "vanished")
    shutil.rmtree(wt)
    it = items(env)["vanished"]
    assert it["state"] == gc.MISSING and not it["safe"]
    # Forgetting it drops THIS record only, not every stale one in the repo.
    other = add_wt(env, "alsogone")
    shutil.rmtree(other)
    assert gc.remove_one(it, confirmed=False, root=env["root"])["ok"]
    listed = git(env["repo"], "worktree", "list", "--porcelain")
    assert wt not in listed and other in listed


# ----------------------------------------------------------- the removal


def test_collect_rechecks_each_item_right_before(env):
    wt = add_wt(env, "claimed")
    it = items(env)["claimed"]
    assert it["safe"]
    usage = gc.Usage(repos=[env["repo"]], in_use={wt: "session 'new' (running)"})
    res = gc.collect([it], recheck_with(env, usage), root=env["root"])
    assert res["removed"] == [] and os.path.isdir(wt)
    assert "session 'new'" in res["skipped"][0]["reason"]


def test_collect_honours_its_limit(env):
    for n in ("a1", "a2", "a3"):
        add_wt(env, n)
    its = [i for i in items(env).values() if i["safe"]]
    res = gc.collect(its, recheck_with(env), root=env["root"], limit=2)
    assert len(res["removed"]) == 2


def test_a_branch_the_person_brought_is_never_deleted(env):
    git(env["repo"], "branch", "theirs")
    path = os.path.join(env["root"], "u", "theirs")
    git(env["repo"], "worktree", "add", "-q", path, "theirs")
    created.record_created(path, env["repo"], "theirs", existing_branch=True)
    it = items(env)["theirs"]
    assert it["safe"] and it["merged"] == "ancestor"
    gc.collect([it], recheck_with(env), root=env["root"])
    assert not os.path.exists(path) and "theirs" in branches(env["repo"])


def test_without_any_record_the_branch_is_kept(env):
    wt = add_wt(env, "norecord", record=False)
    it = items(env)["norecord"]
    assert it["safe"]
    gc.collect([it], recheck_with(env), root=env["root"])
    assert not os.path.exists(wt) and "u/norecord" in branches(env["repo"])


def test_remove_on_request_saves_uncommitted_work_first(env, tmp_path):
    wt = add_wt(env, "wip")
    commit(wt, "done.txt")
    with open(os.path.join(wt, "README"), "a") as f:
        f.write("edited\n")
    open(os.path.join(wt, "brand_new.txt"), "w").write("new file\n")
    with open(os.path.join(wt, "bin.dat"), "wb") as f:
        f.write(b"\x00\x01\x02")
    it = items(env)["wip"]
    assert not it["safe"]
    res = gc.remove_one(it, confirmed=False, root=env["root"])
    assert res["needs_confirm"] and os.path.isdir(wt)
    res = gc.remove_one(it, confirmed=True, root=env["root"])
    assert res["ok"], res
    assert not os.path.exists(wt)
    saved = res["saved"]
    assert saved["dir"].startswith(gc.kept_root())
    assert os.path.isfile(os.path.join(saved["dir"], "info.json"))
    # The unmerged branch survives; the patch re-applies onto it.
    assert "u/wip" in branches(env["repo"]) and res["branch_deleted"] == ""
    check = tmp_path / "check"
    git(env["repo"], "worktree", "add", "-q", str(check), "u/wip")
    git(check, "apply", "--index", saved["patch"])
    assert (check / "brand_new.txt").read_text() == "new file\n"
    assert (check / "README").read_text().endswith("edited\n")
    assert (check / "bin.dat").read_bytes() == b"\x00\x01\x02"


def test_a_detached_head_with_unmerged_commits_gets_a_kept_branch(env):
    path = os.path.join(env["root"], "u", "detached")
    git(env["repo"], "worktree", "add", "-q", "--detach", path, "main")
    head = commit(path, "lonely.txt")
    it = items(env)["detached"]
    assert it["unsaved_commits"] == 1 and not it["safe"]
    res = gc.remove_one(it, confirmed=True, root=env["root"])
    assert res["ok"]
    ref = res["saved"]["kept_branch"]
    assert ref.startswith("mindflock-kept/")
    assert git(env["repo"], "rev-parse", ref) == head


def test_nothing_is_removed_when_the_work_cannot_be_saved(env, monkeypatch):
    wt = add_wt(env, "cantsave")
    open(os.path.join(wt, "x.txt"), "w").write("wip")
    it = items(env)["cantsave"]

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(gc, "save_work", boom)
    res = gc.remove_one(it, confirmed=True, root=env["root"])
    assert not res["ok"] and "Nothing was removed" in res["error"]
    assert os.path.isdir(wt)


# ---------------------------------------------------------------- clones


def make_clone(env, name, *, push=True):
    origin = os.path.join(str(env["tmp"]), "origin-%s.git" % name)
    git(env["tmp"], "clone", "-q", "--bare", env["repo"], origin)
    croot = os.path.join(str(env["tmp"]), "workspaces")
    os.makedirs(croot, exist_ok=True)
    path = os.path.join(croot, name)
    git(env["tmp"], "clone", "-q", origin, path)
    return croot, path


def test_a_clean_pushed_clone_is_safe_and_an_unpushed_one_is_kept(env):
    croot, ok = make_clone(env, "feature-ok")
    _, bad = make_clone(env, "feature-bad")
    commit(bad, "local.txt")
    _, stashy = make_clone(env, "feature-stash")
    with open(os.path.join(stashy, "README"), "a") as f:
        f.write("s\n")
    git(stashy, "stash", "-q")
    os.makedirs(os.path.join(croot, "_base_repo", ".git"))  # infrastructure
    usage = gc.Usage(repos=[env["repo"]], clone_roots=[croot])
    got = items(env, usage)
    assert "_base_repo" not in got
    assert got["feature-ok"]["kind"] == gc.KIND_CLONE and got["feature-ok"]["safe"]
    assert got["feature-bad"]["unsaved_commits"] == 1 and not got["feature-bad"]["safe"]
    assert got["feature-stash"]["stashes"] == 1 and not got["feature-stash"]["safe"]
    res = gc.collect(
        [got["feature-ok"]],
        lambda it: gc.classify(
            gc._record(it["path"], gc.KIND_CLONE, it["path"]),
            usage,
            now=LATER,
            root=env["root"],
        ),
        usage=usage,
        root=env["root"],
    )
    assert res["removed"] and not os.path.exists(ok)
    res = gc.remove_one(
        got["feature-bad"], confirmed=True, usage=usage, root=env["root"]
    )
    assert res["ok"] and os.path.isfile(res["saved"]["bundle"])
    assert not os.path.exists(bad)
    git(env["repo"], "bundle", "verify", res["saved"]["bundle"])  # needs its base


def test_a_folder_outside_the_roots_is_never_rmtreed(env, tmp_path):
    stray = tmp_path / "stray"
    git(tmp_path, "clone", "-q", env["repo"], str(stray))
    item = {"path": str(stray), "kind": gc.KIND_CLONE, "safe": True}
    assert gc._remove_folder(item, env["root"], gc.Usage(), allow_orphan=False)
    assert stray.is_dir()


# -------------------------------------------------------------- records


def test_creation_records_round_trip_and_dedupe(env):
    p = os.path.join(env["root"], "x")
    created.record_created(p, env["repo"], "b1", title="x")
    created.record_created(p, env["repo"], "b1", title="x-again")
    lines = open(created.records_path()).read().splitlines()
    assert len(lines) == 1
    assert created.load_records()[gc.norm(p)]["title"] == "x"
    created.forget_records([p])
    assert created.load_records() == {}


def test_worktree_setup_writes_a_creation_record(env):
    from backend.session.git.worktree import GitWorktree

    path = os.path.join(env["root"], "made_abc")
    wt = GitWorktree(
        repoPath=env["repo"], worktreePath=path, sessionName="made", branchName="u/made"
    )
    wt.Setup()
    rec = created.load_records()[gc.norm(path)]
    assert rec["branch"] == "u/made" and rec["existing_branch"] is False
    assert rec["title"] == "made"


def test_summary_counts_the_safe_bytes(env):
    add_wt(env, "s1")
    w2 = add_wt(env, "s2")
    open(os.path.join(w2, "u.txt"), "w").write("x")
    its = gc.inventory(gc.Usage(repos=[env["repo"]]), root=env["root"], now=LATER)
    s = gc.summarize(its)
    assert s["count"] == 2 and s["safe_count"] == 1
    assert s["safe_bytes"] == next(i["free_bytes"] for i in its if i["safe"])


# ---------------------------------------- the clock a read cannot move


def test_last_used_is_not_moved_by_a_scan_or_anyone_elses_git_status(env):
    """A real machine read "used 18 hours ago" on worktrees closed weeks
    before: someone's survey (and the server's diff probe) ran `git status`,
    which rewrites the index. The clock is the HEAD reflog now; reads, a
    rewritten index and a touched folder all leave it alone."""
    stamp = "@%d +0000" % int(time.time() - 40 * DAY)
    os.environ["GIT_COMMITTER_DATE"] = stamp
    try:
        wt = add_wt(env, "quiet", record=False)  # its reflog entry is 40 days old
    finally:
        del os.environ["GIT_COMMITTER_DATE"]
    now = time.time()
    first = items(env, now=now)["quiet"]
    assert now - first["last_used"] > 39 * DAY
    assert first["state"] == gc.SAFE
    second = items(env, now=now)["quiet"]
    assert second["last_used"] == first["last_used"]
    # Someone else's plain `git status` (optional locks ON) refreshes the
    # index; a touch moves the folder and index mtimes. Neither counts.
    subprocess.run(["git", "-C", wt, "status"], capture_output=True, check=True)
    os.utime(wt, None)
    os.utime(os.path.join(gc.gitdir_of(wt), "index"), None)
    third = items(env, now=now)["quiet"]
    assert third["last_used"] == first["last_used"] and third["safe"]
    # A commit is use.
    commit(wt, "b.txt")
    after = items(env, now=time.time())["quiet"]
    assert time.time() - after["last_used"] < 60
    assert after["kept"][-1].startswith("used ")


def test_every_probe_runs_with_optional_locks_off(env, monkeypatch):
    seen = []
    real = gc.gitutil.git

    def spy(args, cwd=None, **kw):
        seen.append((list(args), dict(kw.get("env") or {})))
        return real(args, cwd, **kw)

    monkeypatch.setattr(gc.gitutil, "git", spy)
    add_wt(env, "locks")
    items(env)
    probes = [(a, e) for a, e in seen if a and a[0] == "--no-optional-locks"]
    assert probes and all(e.get("GIT_OPTIONAL_LOCKS") == "0" for _, e in probes)


# ------------------------------------------------- sizes, nesting


def test_a_worktree_inside_another_is_left_out_of_its_size_and_keeps_it(env):
    outer = add_wt(env, "outer")
    # An agent's own worktree under an ignored .claude/worktrees/.
    os.makedirs(os.path.join(env["repo"], ".git", "info"), exist_ok=True)
    with open(os.path.join(env["repo"], ".git", "info", "exclude"), "a") as f:
        f.write(".claude/\n")
    inner = os.path.join(outer, ".claude", "worktrees", "agent-1")
    git(env["repo"], "worktree", "add", "-q", "-b", "agent-1", inner)
    with open(os.path.join(inner, "blob.bin"), "wb") as f:
        f.write(os.urandom(2 * 1024 * 1024))
    got = items(env)
    o, i = got["outer"], got["agent-1"]
    assert not o["safe"] and o["nested"] == [gc.norm(inner)]
    assert any(k.startswith("contains 1 other worktree") for k in o["kept"])
    assert o["size_bytes"] < 1024 * 1024 <= i["size_bytes"]
    # Removal refuses to take a checkout inside along with it, even forced.
    res = gc.remove_one(o, confirmed=True, root=env["root"])
    assert not res["ok"] and "another git checkout inside" in res["error"]
    assert os.path.isdir(inner)


def test_the_totals_count_a_hard_linked_file_once(env, tmp_path):
    a = add_wt(env, "hl-a")
    b = add_wt(env, "hl-b")
    big = os.path.join(a, "cache.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(4 * 1024 * 1024))
    os.link(big, os.path.join(b, "cache.bin"))  # what uv does to every .venv
    os.makedirs(os.path.join(env["repo"], ".git", "info"), exist_ok=True)
    with open(os.path.join(env["repo"], ".git", "info", "exclude"), "a") as f:
        f.write("cache.bin\n")  # ignored, like a .venv: not "work"
    its = gc.inventory(gc.Usage(repos=[env["repo"]]), root=env["root"], now=LATER)
    naive = sum(i["size_bytes"] for i in its)
    s = gc.summarize(its, gc.measure_totals(its))
    assert naive >= 8 * 1024 * 1024  # each item's du counts the shared file
    assert s["safe_count"] == 2
    # On disk: once. Freed by removing them: none of it — another path (here
    # each other; on a real machine uv's cache) still links the blocks.
    assert 4 * 1024 * 1024 <= s["bytes"] < 6 * 1024 * 1024
    assert s["safe_bytes"] < 1024 * 1024
    a_item = next(i for i in its if i["path"] == gc.norm(a))
    assert a_item["free_bytes"] < 1024 * 1024 <= a_item["size_bytes"]


def test_worktrees_mindflock_did_not_make_stay_out_of_the_headline(env, tmp_path):
    add_wt(env, "mine")
    theirs = tmp_path / "theirs"
    git(env["repo"], "worktree", "add", "-q", "-b", "theirs", str(theirs))
    with open(theirs / "big.bin", "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024))
    its = gc.inventory(gc.Usage(repos=[env["repo"]]), root=env["root"], now=LATER)
    s = gc.summarize(its, gc.measure_totals(its))
    assert s["count"] == 1 and s["other_count"] == 1
    assert s["bytes"] < 1024 * 1024 <= s["other_bytes"]


def test_what_is_left_of_a_worktree_folder_is_one_item_not_its_insides(env):
    left = os.path.join(env["root"], "u", "gone_0123456789abcdef")
    os.makedirs(os.path.join(left, ".claude"))
    open(os.path.join(left, ".claude", "settings.json"), "w").write("{}")
    deep = os.path.join(
        env["root"], "u", "ctx_fedcba9876543210", "web", ".vite", "deps"
    )
    os.makedirs(deep)
    open(os.path.join(deep, "x.js"), "w").write("1")
    paths = {i["path"] for i in gc.inventory(gc.Usage(), root=env["root"], now=LATER)}
    assert gc.norm(left) in paths
    assert gc.norm(os.path.join(env["root"], "u", "ctx_fedcba9876543210")) in paths
    assert not any(p.endswith(".claude") or p.endswith("deps") for p in paths)


def test_a_branch_squash_merged_into_staging_counts_as_merged(env):
    git(env["repo"], "branch", "staging")
    wt = add_wt(env, "ticket")
    commit(wt, "fix.txt")
    git(env["repo"], "checkout", "-q", "staging")
    git(env["repo"], "merge", "-q", "--squash", "u/ticket")
    git(env["repo"], "commit", "-q", "-m", "squash into staging")
    git(env["repo"], "checkout", "-q", "main")
    commit(env["repo"], "README", "main moved\n")
    it = items(env)["ticket"]
    assert it["merged"] == "content" and it["safe"]
