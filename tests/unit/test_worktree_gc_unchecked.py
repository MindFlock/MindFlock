"""Folders whose files can't be checked (backend/web/core/worktree_gc.py,
``orphan`` items): their own state, a look inside on request
(:func:`inspect_unchecked`), and removal only by an explicit choice
(``even_unchecked``) — never by collection — with the path checks that keep
the deletion inside MindFlock's own folders, off symlinks and on one
filesystem. Throwaway homes and repositories only."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from backend.session.git import created
from backend.web.core import worktree_gc as gc

DAY = 24 * 3600.0
LATER = time.time() + 30 * DAY


def git(cwd, *args):
    cp = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if cp.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), cp.stderr))
    return cp.stdout.strip()


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


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
    write(str(repo / "README"), "hello\n")
    write(str(repo / ".gitignore"), "node_modules/\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    root = home / "worktrees"
    root.mkdir()
    return {"tmp": tmp_path, "home": home, "repo": str(repo), "root": str(root)}


def forgotten_worktree(env, name, branch=None):
    """A worktree git has forgotten (its admin dir pruned away), its branch
    still in the repository: what ``git worktree prune`` after a crash, or
    a repository re-clone, leaves under the worktrees root."""
    branch = branch or "u/" + name
    path = os.path.join(env["root"], "u", name + "_18e7a6754a1a670a")
    git(env["repo"], "worktree", "add", "-q", "-b", branch, path, "main")
    write(os.path.join(path, "src", "app.py"), "print('hi')\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "work")
    admin = gc.gitdir_of(path)
    subprocess.run(["rm", "-rf", admin], check=True)
    return path


def listed(env, usage=None):
    usage = usage or gc.Usage(repos=[env["repo"]])
    return {
        os.path.basename(i["path"]): i
        for i in gc.inventory(usage, root=env["root"], now=LATER)
    }


def the(env, name):
    return next(i for k, i in listed(env).items() if k.startswith(name))


def look(env, it):
    return gc.inspect_unchecked(it, usage=gc.Usage(), root=env["root"])


def remove(env, it, **kw):
    kw.setdefault("confirmed", True)
    return gc.remove_one(it, root=env["root"], **kw)


# ------------------------------------------------------------ the class


def test_a_forgotten_worktree_is_its_own_class_with_the_plain_reason(env):
    forgotten_worktree(env, "feat")
    it = the(env, "feat")
    assert it["kind"] == gc.KIND_ORPHAN and it["state"] == gc.UNCHECKED
    assert it["unchecked_why"] == gc.GONE_UNREGISTERED and not it["safe"]
    assert "git no longer lists it as a worktree of" in it["kept"][-1]
    s = gc.summarize(list(listed(env).values()))
    assert s["unchecked_count"] == 1 and s["safe_count"] == 0


def test_each_gone_shape_has_its_own_reason(env, tmp_path):
    root = env["root"]
    no_git = os.path.join(root, "u", "left_18e2d220aa03c9e5")
    write(os.path.join(no_git, "notes.txt"))
    repo_gone = os.path.join(root, "u", "repo-gone_18e2d220aa03c9e6")
    write(
        os.path.join(repo_gone, ".git"),
        "gitdir: %s/.git/worktrees/x\n" % (tmp_path / "vanished"),
    )
    own = os.path.join(root, "u", "own_18e2d220aa03c9e7")
    os.makedirs(own)
    git(own, "init", "-q")
    items = listed(env)
    assert items[os.path.basename(no_git)]["unchecked_why"] == gc.GONE_NO_GIT
    assert items[os.path.basename(repo_gone)]["unchecked_why"] == gc.GONE_REPO
    assert "is gone" in items[os.path.basename(repo_gone)]["kept"][-1]
    assert items[os.path.basename(own)]["unchecked_why"] == gc.GONE_OWN_REPO
    assert all(i["state"] == gc.UNCHECKED for i in items.values())


# ------------------------------------------------------------ a look inside


def test_a_look_inside_counts_work_apart_from_dependencies(env):
    path = os.path.join(env["root"], "u", "left_18e2d220aa03c9e5")
    write(os.path.join(path, "notes.md"), "my idea\n")
    write(os.path.join(path, "src", "a.py"), "a\n")
    for i in range(5):
        write(os.path.join(path, "node_modules", "p%d" % i, "index.js"))
    write(os.path.join(path, "pkg", "__pycache__", "a.cpython-312.pyc"))
    facts = look(env, the(env, "left"))
    assert facts["files"] == 8 and facts["work_files"] == 2
    assert facts["dependency_files"] == 6
    assert {f["path"] for f in facts["work_sample"]} == {
        "notes.md",
        os.path.join("src", "a.py"),
    }
    tops = {t["name"]: t for t in facts["top"]}
    assert tops["node_modules"]["kind"] == "dependency"
    assert tops["node_modules"]["files"] == 5
    assert facts["newest"] and facts["complete"]
    assert facts["verdict"] == "unknown" and not facts["safe"]
    assert any("for good" in ln for ln in facts["summary"])


def test_a_dangling_gitdir_and_a_gone_repo_are_said(env, tmp_path):
    path = os.path.join(env["root"], "u", "repo-gone_18e2d220aa03c9e6")
    gd = tmp_path / "vanished" / ".git" / "worktrees" / "x"
    write(os.path.join(path, ".git"), "gitdir: %s\n" % gd)
    write(os.path.join(path, "a.txt"))
    facts = look(env, the(env, "repo-gone"))
    g = facts["git"]
    assert g["dot"] == "file" and not g["gitdir_exists"]
    assert not g["repo_exists"] and g["compare"] is None
    text = " ".join(facts["summary"])
    assert "which no longer exists" in text and "is gone too" in text
    assert not facts["safe"]


def test_files_matching_the_branch_exactly_are_safe_to_remove(env):
    path = forgotten_worktree(env, "feat")
    write(os.path.join(path, "node_modules", "dep", "x.js"))  # ignored
    facts = look(env, the(env, "feat"))
    assert facts["git"]["branch"] == "u/feat"
    assert facts["verdict"] == "matches" and facts["safe"]
    assert "Its files match branch u/feat exactly." in facts["summary"]


def test_the_comparison_writes_nothing_into_the_repository(env):
    path = forgotten_worktree(env, "feat")
    write(os.path.join(path, "src", "app.py"), "changed\n")
    gitdir = os.path.join(env["repo"], ".git")

    def snapshot():
        out = {}
        for d, _dirs, files in os.walk(gitdir):
            for f in files:
                p = os.path.join(d, f)
                out[p] = os.stat(p).st_mtime_ns
        return out

    before = snapshot()
    look(env, the(env, "feat"))
    assert snapshot() == before


def test_files_that_differ_from_the_branch_are_counted(env):
    path = forgotten_worktree(env, "feat")
    write(os.path.join(path, "src", "app.py"), "changed\n")
    write(os.path.join(path, "new.txt"), "fresh\n")
    os.remove(os.path.join(path, "README"))
    facts = look(env, the(env, "feat"))
    c = facts["git"]["compare"]
    assert (c["changed_count"], c["extra_count"], c["missing_count"]) == (1, 1, 1)
    assert c["changed"] == ["src/app.py"] and c["extra"] == ["new.txt"]
    assert facts["verdict"] == "differs" and not facts["safe"]
    assert "3 files differ from branch u/feat" in " ".join(facts["summary"])


def test_a_folder_lacking_some_of_the_branchs_files_loses_nothing(env):
    path = forgotten_worktree(env, "feat")
    os.remove(os.path.join(path, "README"))
    facts = look(env, the(env, "feat"))
    assert facts["verdict"] == "within" and facts["safe"]


def test_a_deleted_branch_leaves_nothing_to_compare(env):
    forgotten_worktree(env, "feat")
    git(env["repo"], "branch", "-D", "u/feat")
    facts = look(env, the(env, "feat"))
    assert facts["git"]["compare"] is None and not facts["safe"]
    assert "no branch it was on any more" in " ".join(facts["summary"])


def test_the_recorded_branch_wins_over_the_folder_name(env):
    path = forgotten_worktree(env, "feat", branch="other/name")
    created.record_created(path, env["repo"], "other/name", title="feat")
    facts = look(env, the(env, "feat"))
    assert facts["git"]["branch"] == "other/name" and facts["safe"]


def test_a_folder_of_only_caches_is_safe_and_its_own_repo_never_is(env):
    caches = os.path.join(env["root"], "u", "ctx_18e2d220aa03c9e5")
    write(os.path.join(caches, "web", ".vite", "deps", "a.js"))
    own = os.path.join(env["root"], "u", "own_18e2d220aa03c9e7")
    os.makedirs(own)
    git(own, "init", "-q")
    assert look(env, the(env, "ctx"))["verdict"] == "no_work"
    facts = look(env, the(env, "own"))
    assert facts["verdict"] == "own_repo" and not facts["safe"]


# ------------------------------------------------------------- removing


def test_without_even_unchecked_it_stays_and_the_facts_come_back(env):
    path = os.path.join(env["root"], "u", "left_18e2d220aa03c9e5")
    write(os.path.join(path, "notes.md"), "my idea\n")
    res = remove(env, the(env, "left"))
    assert res["needs_confirm"] and res["unchecked"] and not res["ok"]
    assert res["facts"]["work_files"] == 1 and os.path.isdir(path)
    assert "permanently" in res["error"]
    res = remove(env, the(env, "left"), even_unchecked=True)
    assert res["ok"] and res["permanent"] and not os.path.exists(path)
    assert res["deleted"]["work_files"] == 1


def test_one_that_loses_nothing_goes_without_even_unchecked(env):
    path = forgotten_worktree(env, "feat")
    res = remove(env, the(env, "feat"), confirmed=False)
    assert res["ok"] and res["verdict"] == "matches" and not os.path.exists(path)
    assert "u/feat" in git(env["repo"], "branch", "--list", "u/feat")


def test_collection_never_touches_one_even_when_it_matches(env):
    path = forgotten_worktree(env, "feat")
    items = list(listed(env).values())
    assert look(env, the(env, "feat"))["safe"]

    def recheck(it):
        return gc.classify(
            gc._record(it["path"], it["kind"], it["repo"]),
            gc.Usage(repos=[env["repo"]]),
            now=LATER,
            root=env["root"],
            measure=False,
        )

    res = gc.collect(items, recheck, root=env["root"])
    assert not res["removed"] and os.path.isdir(path)


def test_in_use_is_never_removed_even_unchecked(env):
    path = os.path.join(env["root"], "u", "left_18e2d220aa03c9e5")
    write(os.path.join(path, "a.txt"))
    usage = gc.Usage(in_use={path: "session 'x' (running)"})
    it = next(iter(listed(env, usage).values()))
    res = remove(env, it, even_unchecked=True, usage=usage)
    assert not res["ok"] and "in use" in res["error"] and os.path.isdir(path)


def test_a_symlink_inside_is_removed_as_a_link_never_followed(env, tmp_path):
    outside = tmp_path / "precious"
    write(str(outside / "keep.txt"), "mine\n")
    path = os.path.join(env["root"], "u", "left_18e2d220aa03c9e5")
    write(os.path.join(path, "a.txt"))
    os.symlink(str(outside), os.path.join(path, "link-dir"))
    os.symlink(str(outside / "keep.txt"), os.path.join(path, "link-file"))
    res = remove(env, the(env, "left"), even_unchecked=True)
    assert res["ok"] and not os.path.lexists(path)
    assert (outside / "keep.txt").read_text() == "mine\n"


def test_a_read_only_folder_inside_is_still_removed(env):
    path = os.path.join(env["root"], "u", "left_18e2d220aa03c9e5")
    ro = os.path.join(path, "go", "mod")
    write(os.path.join(ro, "a.go"))
    os.chmod(ro, 0o555)
    os.chmod(os.path.join(path, "go"), 0o555)
    assert gc._delete_tree(path) == "" and not os.path.exists(path)


def test_the_deletion_never_crosses_into_another_filesystem(env, monkeypatch):
    path = os.path.join(env["root"], "u", "left_18e2d220aa03c9e5")
    write(os.path.join(path, "mnt", "data.bin"))
    write(os.path.join(path, "a.txt"))
    real_stat = os.stat

    class Other:
        def __init__(self, st):
            self.st_mode, self.st_ino = st.st_mode, st.st_ino
            self.st_dev = st.st_dev + 1

    def fake_stat(name, *a, **kw):
        st = real_stat(name, *a, **kw)
        return Other(st) if name == "mnt" else st

    monkeypatch.setattr(os, "stat", fake_stat)
    err = gc._delete_tree(path)
    monkeypatch.undo()
    assert "another filesystem" in err
    assert os.path.isfile(os.path.join(path, "mnt", "data.bin"))
    assert not os.path.exists(os.path.join(path, "a.txt"))


@pytest.mark.parametrize("shape", ["outside", "symlink", "not_orphan"])
def test_the_path_checks_refuse(env, tmp_path, shape):
    usage, records = gc.Usage(), {}
    if shape == "outside":
        path = str(tmp_path / "elsewhere")
        os.makedirs(path)
        item = gc._record(path, gc.KIND_ORPHAN)
        want = "outside MindFlock's own folders"
    elif shape == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        os.makedirs(os.path.join(env["root"], "u"))
        link = os.path.join(env["root"], "u", "link_18e2d220aa03c9e5")
        os.symlink(str(target), link)
        item = {"path": link, "kind": gc.KIND_ORPHAN}
        want = "symbolic link"
    else:
        path = os.path.join(env["root"], "u", "wt_18e2d220aa03c9e5")
        os.makedirs(path)
        item = gc._record(path, gc.KIND_WORKTREE)
        want = "can't be checked is removed this way"
    assert want in gc.unchecked_refusal(
        item, root=env["root"], usage=usage, records=records
    )
    if shape == "symlink":
        assert os.path.isdir(str(tmp_path / "target"))


def test_a_recorded_folder_outside_every_root_is_still_refused(env, tmp_path):
    """The creation record makes a folder "ours" for the listing, but an
    unchecked deletion also needs it inside MindFlock's own folders."""
    path = str(tmp_path / "far" / "x_18e2d220aa03c9e5")
    write(os.path.join(path, "a.txt"))
    item = gc._record(path, gc.KIND_ORPHAN)
    records = {gc.norm(path): {"path": gc.norm(path)}}
    why = gc.unchecked_refusal(
        item, root=env["root"], usage=gc.Usage(), records=records
    )
    assert "outside" in why and os.path.isdir(path)


def test_a_live_worktree_is_never_deleted_as_unchecked(env):
    path = os.path.join(env["root"], "u", "live_18e2d220aa03c9e5")
    git(env["repo"], "worktree", "add", "-q", "-b", "u/live", path, "main")
    item = gc._record(gc.norm(path), gc.KIND_ORPHAN)
    why = gc.unchecked_refusal(item, root=env["root"], usage=gc.Usage(), records={})
    assert why == "git still knows it as a worktree" and os.path.isdir(path)


def test_a_folder_holding_other_worktree_folders_is_never_deleted_whole(env):
    """A stray file in a branch-slug level makes the whole level one item;
    deleting it would take every worktree folder in it."""
    level = os.path.join(env["root"], "someone")
    write(os.path.join(level, ".DS_Store"))
    write(os.path.join(level, "dead_18e2d220aa03c9e5", "a.txt"))
    it = listed(env)["someone"]
    assert it["state"] == gc.UNCHECKED
    res = remove(env, it, even_unchecked=True)
    assert not res["ok"] and "other worktree folders" in res["error"]
    assert os.path.isfile(os.path.join(level, "dead_18e2d220aa03c9e5", "a.txt"))
