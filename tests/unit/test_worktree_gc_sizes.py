"""Disk cleanup's sizes (backend/web/core/worktree_gc.py, "sizes"): one walk
per item, kept in a size cache in the state dir until the folder changes,
and a headline that counts every hard-linked file ONCE — from the cache as
much as from a fresh walk (summing per-item sizes once reported 2,039 GB
for a 210 GB tree)."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from backend.web.core import worktree_gc as gc
from tests.unit.test_worktree_gc import (  # noqa: F401 — env is a fixture
    LATER,
    add_wt,
    env,
    git,
)

MB = 1024 * 1024


def blob(path, n=MB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(n))
    return path


def ignore(env, *pats):
    os.makedirs(os.path.join(env["repo"], ".git", "info"), exist_ok=True)
    with open(os.path.join(env["repo"], ".git", "info", "exclude"), "a") as f:
        f.write("".join(p + "\n" for p in pats))


def linked_tree(env):
    """Three worktrees whose ignored ``.venv`` hard-links one "cache"
    outside them all (what uv does), plus a file hard-linked between two
    of them and a file of each one's own."""
    ignore(env, ".venv/", "own.bin", "pair.bin")
    cache = os.path.join(env["tmp"], "uv-cache")
    shared = [blob(os.path.join(cache, "pkg%d.so" % k), 2 * MB) for k in range(3)]
    wts = [add_wt(env, n) for n in ("v1", "v2", "v3")]
    for w in wts:
        for s in shared:
            dst = os.path.join(w, ".venv", "lib", os.path.basename(s))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.link(s, dst)
        blob(os.path.join(w, "own.bin"), MB)
    os.link(
        blob(os.path.join(wts[0], "pair.bin"), 3 * MB), os.path.join(wts[1], "pair.bin")
    )
    return wts


def du_truth(paths):
    """``du``'s own answer for these folders together (each file once)."""
    cp = subprocess.run(
        ["du", "-sck", "-x", *paths], capture_output=True, text=True, check=True
    )
    return int(cp.stdout.strip().splitlines()[-1].split()[0]) * 1024


def test_the_headline_counts_every_hard_linked_file_once_from_walks_and_cache(
    env, monkeypatch
):
    wts = linked_tree(env)
    usage = gc.Usage(repos=[env["repo"]])
    its = gc.inventory(usage, root=env["root"], now=LATER)
    s = gc.summarize(its)
    naive = sum(i["size_bytes"] for i in its)
    # 6 MB of cache linked from each of 3 trees + a 3 MB pair: a per-item
    # sum counts 18 + 6 MB of it; the headline counts 6 + 3.
    assert naive >= 3 * 6 * MB + 2 * 3 * MB
    assert 6 * MB + 3 * MB + 3 * MB <= s["bytes"] < 6 * MB + 3 * MB + 3 * MB + MB
    if shutil.which("du"):
        assert abs(s["bytes"] - du_truth(wts)) <= 64 * 1024
    # Removing any one frees only its own file; the linked ones stay.
    for i in its:
        assert MB <= i["free_bytes"] < MB + 256 * 1024
    assert s["safe_bytes"] == sum(i["free_bytes"] for i in its)

    # A second listing walks nothing and says exactly the same.
    gc._caches.clear()  # a new process: from the file alone
    monkeypatch.setattr(gc, "walk_sizes", lambda *a, **k: pytest.fail("walked"))
    again = gc.inventory(usage, root=env["root"], now=LATER + 60)
    assert gc.summarize(again) == s
    assert {i["path"]: i["size_bytes"] for i in again} == {
        i["path"]: i["size_bytes"] for i in its
    }


def test_the_cache_lives_in_the_state_dir_and_drops_what_went(env):
    wts = linked_tree(env)
    usage = gc.Usage(repos=[env["repo"]])
    gc.inventory(usage, root=env["root"], now=LATER)
    f = os.path.join(env["home"], gc.SIZE_CACHE_FILE)
    assert os.path.isfile(f)
    assert set(gc.size_cache().entries) == {gc.norm(w) for w in wts}
    git(env["repo"], "worktree", "remove", "--force", wts[2])
    its = gc.inventory(usage, root=env["root"], now=LATER)
    assert set(gc.size_cache().entries) == {gc.norm(w) for w in wts[:2]}
    assert gc.summarize(its)["bytes"] < 6 * MB + 3 * MB + 2 * MB + MB


def test_a_change_in_the_folder_or_age_walks_it_again(env, monkeypatch):
    w = add_wt(env, "grows")
    ignore(env, "dist/")
    usage = gc.Usage(repos=[env["repo"]])
    walked = []
    real = gc.walk_sizes
    monkeypatch.setattr(
        gc, "walk_sizes", lambda p, *a, **k: walked.append(p) or real(p, *a, **k)
    )
    first = gc.inventory(usage, root=env["root"], now=LATER)[0]
    gc.inventory(usage, root=env["root"], now=LATER + 60)
    assert len(walked) == 1
    blob(os.path.join(w, "dist", "bundle.js"), 2 * MB)  # a new top-level folder
    grown = gc.inventory(usage, root=env["root"], now=LATER + 120)[0]
    assert len(walked) == 2 and grown["size_bytes"] >= first["size_bytes"] + 2 * MB
    gc.inventory(usage, root=env["root"], now=LATER + 120 + gc.SIZE_TTL_S)
    assert len(walked) == 3


def test_unmeasured_sizes_are_unknown_never_a_sum(env, monkeypatch):
    linked_tree(env)
    its = gc.inventory(
        gc.Usage(repos=[env["repo"]]), root=env["root"], now=LATER, measure=False
    )
    assert all(i["size_bytes"] is None for i in its)
    s = gc.summarize(its)
    assert s["bytes"] is None and s["safe_bytes"] is None
    assert s["count"] == 3 and s["safe_count"] == 3


@pytest.mark.skipif(not gc._find_has_printf(), reason="needs GNU find")
def test_the_python_walk_agrees_with_find(env, tmp_path, monkeypatch):
    top = tmp_path / "t"
    blob(str(top / "a" / "one.bin"), 3 * MB)
    blob(str(top / "b.bin"), MB)
    os.link(str(top / "b.bin"), str(top / "a" / "b-again.bin"))  # linked inside
    os.symlink("b.bin", str(top / "link"))
    inner = top / "nested[1]"  # a glob character in an excluded path
    blob(str(inner / "big.bin"), 4 * MB)
    ex = [gc.norm(str(inner))]
    by_find = gc.walk_sizes(gc.norm(str(top)), ex)
    monkeypatch.setattr(gc, "_find_ok", False)
    by_py = gc.walk_sizes(gc.norm(str(top)), ex)
    assert by_find is not None and by_py is not None
    for k in ("size", "free", "own"):
        assert by_find[k] == by_py[k], k
    assert by_find["shared"] == by_py["shared"] and len(by_find["shared"]) == 1
    # The excluded folder is left out; the file linked twice counts once.
    assert 4 * MB <= by_find["size"] < 5 * MB
    assert 3 * MB <= by_find["free"] < 4 * MB
