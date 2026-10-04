"""Code Map analysis (``backend.web.core.code_map``): file list, change set,
committed change set, import graph, blast radius, tool feed and plans.

Every git-facing test runs against a REAL throwaway repo — the whole point of
these helpers is agreeing with git (numstat after ``add -N``, the committed
range the push gate reads), so a mocked git would only test the mock. The
server is replaced by a tiny namespace carrying the three attributes the
module reads (fork point, fingerprint, diff-stat cache): the fork point is
the only session-specific input, and pinning it keeps these tests off the
live ``server.ENGINE``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.web.core import code_map as cm
from backend.web.core import snapshot


def _git(*args: str, cwd) -> str:
    cp = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _write(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _write(path, "a.txt", "one\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


class _FakeSrv:
    """The slice of ``backend.web.server`` code_map reads."""

    def __init__(self, fork: str):
        self.fork = fork
        self._DIFF_STAT_CACHE: dict = {}
        self.fp_calls = 0

    def _session_fork_point(self, inst, wt):
        return self.fork

    def _worktree_fingerprint(self, wt, base):
        self.fp_calls += 1
        return snapshot._worktree_fingerprint(wt, base)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Fresh module caches + a private feed dir for every test."""
    monkeypatch.setenv("MINDFLOCK_TOOL_FEED_DIR", str(tmp_path / "feed"))
    for name in (
        "_LIST_CACHE",
        "_CHANGED_CACHE",
        "_GRAPH_CACHE",
        "_SCAN_MEMO",
        "_JSON_MEMO",
        "_PLAN_LATCH",
        "_TP_LATCH",
    ):
        getattr(cm, name).clear()
    yield


def test_server_exposes_what_code_map_reads():
    """The lazy accessor's contract: these names must exist on the server."""
    from backend.web import server

    assert callable(server._session_fork_point)
    assert isinstance(server._DIFF_STAT_CACHE, dict)
    # _worktree_fingerprint: the server's re-export when present, else snapshot's
    srv = SimpleNamespace()
    assert cm._wt_fingerprint(srv, "/no/such/dir", "HEAD") is None
    srv._worktree_fingerprint = lambda wt, fork: "patched"
    assert cm._wt_fingerprint(srv, "/no/such/dir", "HEAD") == "patched"


# --------------------------------------------------------------------------- #
# list_files
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel,is_test",
    [
        ("tests/unit/test_x.py", True),
        ("pkg/test/helpers.py", True),
        ("web/__tests__/a.ts", True),
        ("spec/models/user_spec.rb", True),
        ("Tests/Thing.cs", True),
        ("pkg/test_mod.py", True),
        ("pkg/mod_test.py", True),
        ("src/app.test.tsx", True),
        ("src/app.spec.js", True),
        ("src/app.ts", False),
        ("pkg/testing.py", False),
        ("pkg/contest.py", False),
        ("docs/latest.md", False),
        ("src/app.test.", False),
        # v3: test trees and per-language test files
        ("testsv2/x.py", True),
        ("integration_tests/a.py", True),
        ("e2e-tests/a.ts", True),
        ("pkg/fixtures/a.json", True),
        ("testdata/a.bin", True),
        ("cmd/app_test.go", True),
        ("src/FooTest.java", True),
        ("src/FooTests.kt", True),
        ("src/FooSpec.kt", True),
        ("src/lib_test.rs", True),
        ("src/test_io.c", True),
        ("src/Latest.java", False),
        ("src/contest.go", False),
        ("pkg/attestation.py", False),
    ],
)
def test_is_test_path(rel, is_test):
    assert cm._is_test_path(rel) is is_test


def test_list_files_tracked_untracked_ignored_and_flags(tmp_path):
    repo = _init_repo(tmp_path / "r")
    _write(repo, ".gitignore", "config.toml\nbuild/\n")
    _write(repo, "src/app.py", "x = 1\n")
    _write(repo, "tests/test_app.py", "def test(): pass\n")
    _write(repo, "gone.txt", "bye\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "more", cwd=repo)
    (repo / "gone.txt").unlink()  # tracked but deleted in the worktree
    _write(repo, "new_untracked.py", "print(1)\n")
    _write(repo, "config.toml", "secret = 1\n")  # ignored
    _write(repo, "build/out.js", "x\n")  # ignored

    rows, truncated = cm.list_files(str(repo))
    by = {r[0]: r for r in rows}
    assert truncated is False
    assert [r[0] for r in rows] == sorted(by)
    assert "new_untracked.py" in by
    assert "gone.txt" not in by  # vanished → skipped
    assert "config.toml" not in by and "build/out.js" not in by
    assert by["src/app.py"][1] == len("x = 1\n")
    assert by["src/app.py"][2] == 0
    assert by["tests/test_app.py"][2] == cm.FLAG_TEST

    rows, _ = cm.list_files(str(repo), extra_ignored=["./config.toml", "nope.txt"])
    by = {r[0]: r for r in rows}
    assert by["config.toml"][2] == cm.FLAG_IGNORED
    assert "nope.txt" not in by  # doesn't exist → skipped like any vanished file


def test_list_files_truncation_cap_keeps_zone_ignored_files(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "r")
    _write(repo, ".gitignore", "secret.env\n")
    for i in range(5):
        _write(repo, f"f{i}.txt", "x")
    _write(repo, "secret.env", "k=v")
    monkeypatch.setattr(cm, "MAX_FILES", 3)
    rows, truncated = cm.list_files(str(repo), extra_ignored=["secret.env"])
    assert truncated is True
    assert len(rows) == 3
    assert "secret.env" in [r[0] for r in rows]

    monkeypatch.setattr(cm, "MAX_FILES", 100)
    cm._LIST_CACHE.clear()
    rows, truncated = cm.list_files(str(repo))
    assert truncated is False and len(rows) == 7  # a.txt .gitignore f0..f4


def test_list_files_caches_by_fingerprint(tmp_path):
    repo = _init_repo(tmp_path / "r")
    rows1, _ = cm.list_files(str(repo), fp="fp1")
    _write(repo, "later.txt", "x")
    rows2, _ = cm.list_files(str(repo), fp="fp1")
    assert [r[0] for r in rows2] == [r[0] for r in rows1]  # same fp → cached
    rows3, _ = cm.list_files(str(repo), fp="fp2")
    assert "later.txt" in [r[0] for r in rows3]


def test_list_files_never_raises_on_non_repo(tmp_path):
    assert cm.list_files(str(tmp_path / "missing")) == ([], False)


# --------------------------------------------------------------------------- #
# changed_files / committed_changed
# --------------------------------------------------------------------------- #


def _feature_repo(tmp_path):
    repo = _init_repo(tmp_path / "r")
    _write(repo, "keep.py", "a\nb\nc\n")
    _write(repo, "del.py", "x\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    fork = _git("rev-parse", "HEAD", cwd=repo)
    _git("checkout", "-q", "-b", "feat", cwd=repo)
    return repo, fork


def test_changed_files_match_git_numstat_after_add_n(tmp_path, monkeypatch):
    repo, fork = _feature_repo(tmp_path)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    _write(repo, "keep.py", "a\nB\nc\nd\n")  # 2 added, 1 removed
    (repo / "del.py").unlink()
    _write(repo, "new/file.ts", "1\n2\n3\n")  # untracked → A via add -N
    (repo / "bin.dat").write_bytes(b"\x00\x09\x09\x09")
    _git("add", "keep.py", cwd=repo)
    _git("commit", "-q", "-m", "wip", cwd=repo)  # committed part counts too

    got = cm.changed_files(object(), str(repo))
    by = {c["path"]: c for c in got}

    # The oracle: git itself, after the same add -N.
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--numstat", "--no-renames", fork],
        capture_output=True,
        text=True,
    ).stdout
    expect = {}
    for line in out.strip().splitlines():
        a, r, p = line.split("\t")
        expect[p] = (0 if a == "-" else int(a), 0 if r == "-" else int(r))
    assert {p: (c["added"], c["removed"]) for p, c in by.items()} == expect
    assert by["keep.py"]["status"] == "M"
    assert by["del.py"]["status"] == "D"
    assert by["new/file.ts"]["status"] == "A"
    assert by["bin.dat"]["added"] == 0 and by["bin.dat"]["removed"] == 0
    assert [c["path"] for c in got] == sorted(by)


def test_changed_files_cache_follows_fingerprint(tmp_path, monkeypatch):
    repo, fork = _feature_repo(tmp_path)
    srv = _FakeSrv(fork)
    monkeypatch.setattr(cm, "_server", lambda: srv)
    _write(repo, "keep.py", "a\nb\nc\nd\n")
    first = cm.changed_files(object(), str(repo))
    assert [c["path"] for c in first] == ["keep.py"]

    calls = []
    real_git = cm._git
    monkeypatch.setattr(
        cm, "_git", lambda *a, **k: calls.append(a) or real_git(*a, **k)
    )
    again = cm.changed_files(object(), str(repo))
    assert again == first
    assert not any("diff" in a for a in calls)  # served from the cache

    _write(repo, "keep.py", "a\nb\nc\nd\ne\n")  # content change → new fingerprint
    third = cm.changed_files(object(), str(repo))
    assert third[0]["added"] == 2


def test_committed_changed_sees_a_commit_reverted_in_the_worktree(
    tmp_path, monkeypatch
):
    """The push gate's case: commit a change, then revert it in the working
    tree. The worktree diff is clean, but the commit still ships."""
    repo, fork = _feature_repo(tmp_path)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    _write(repo, "keep.py", "changed\n")
    _git("commit", "-q", "-am", "touch keep", cwd=repo)
    _write(repo, "keep.py", "a\nb\nc\n")  # back to the fork's content, uncommitted

    assert cm.changed_files(object(), str(repo)) == []
    assert cm.committed_changed(object(), str(repo)) == ["keep.py"]
    assert cm.committed_changed(object(), str(repo), target=fork) == []
    assert cm.committed_changed(object(), str(repo), target="no/such/ref") == []


def test_decoy_files_named_like_revisions_cannot_blind_the_diffs(tmp_path, monkeypatch):
    """F1: `touch HEAD` (or a file named after the fork sha) in the worktree
    made `git diff <fork> HEAD` die "ambiguous argument", which read as "no
    committed changes" — the push gate and breach detection failed open."""
    repo, fork = _feature_repo(tmp_path)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    _write(repo, "keep.py", "changed\n")
    _git("commit", "-q", "-am", "zone edit", cwd=repo)
    for decoy in ("HEAD", fork):
        _write(repo, decoy, "decoy\n")
        cm._CHANGED_CACHE.clear()
        assert cm.committed_changed(object(), str(repo)) == ["keep.py"], decoy
        changed = [c["path"] for c in cm.changed_files(object(), str(repo))]
        assert "keep.py" in changed and decoy in changed, (decoy, changed)


def test_committed_changed_pr_target_cannot_be_shadowed(tmp_path, monkeypatch):
    """F1: the PR/merge gate diffs against `origin/<branch>`. Neither a FILE at
    that path nor a same-named tag/branch pointed at the fork (git's DWIM
    order puts refs/tags and refs/heads BEFORE refs/remotes) may stand in."""
    repo, fork = _feature_repo(tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _write(repo, "keep.py", "changed\n")
    _git("commit", "-q", "-am", "zone edit", cwd=repo)
    _git("push", "-q", "origin", "feat", cwd=repo)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    assert cm.committed_changed(object(), str(repo), "origin/feat") == ["keep.py"]

    _git("tag", "origin/feat", fork, cwd=repo)
    assert cm.committed_changed(object(), str(repo), "origin/feat") == ["keep.py"]
    _write(repo, "origin/feat", "decoy\n")
    assert cm.committed_changed(object(), str(repo), "origin/feat") == ["keep.py"]

    # A "revision" git would parse as an OPTION is refused, never run.
    out = tmp_path / "written-by-git.txt"
    assert cm.committed_changed(object(), str(repo), f"--output={out}") == []
    assert not out.exists()
    assert cm._rev("HEAD") == "HEAD" and cm._rev(fork) == fork
    assert cm._rev("origin/main") == "refs/remotes/origin/main"
    assert cm._rev("") is None and cm._rev(None) is None and cm._rev("-x") is None


def test_changed_files_write_during_the_diff_is_not_cached_away(tmp_path, monkeypatch):
    """F2: the change set was cached under a fingerprint taken AFTER the
    diffs, so a zone edit landing between the diff and that fingerprint was
    pinned as "already seen" until some other file moved."""
    repo, fork = _feature_repo(tmp_path)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    _write(repo, "keep.py", "a\nb\nc\nd\n")
    real_git = cm._git
    fired = []

    def racing_git(wt, *args, **kw):
        out = real_git(wt, *args, **kw)
        if "--name-status" in args and not fired:
            fired.append(True)
            _write(repo, "del.py", "edited while the diff ran\n")
        return out

    monkeypatch.setattr(cm, "_git", racing_git)
    first = [c["path"] for c in cm.changed_files(object(), str(repo))]
    assert fired and first == ["keep.py"]  # the diff predates the write
    monkeypatch.setattr(cm, "_git", real_git)
    for _ in range(3):  # every later poll sees it — not just the next one
        got = [c["path"] for c in cm.changed_files(object(), str(repo))]
        assert got == ["del.py", "keep.py"]


def test_fingerprint_reuses_fresh_diff_stat_cache(tmp_path, monkeypatch):
    repo, fork = _feature_repo(tmp_path)
    srv = _FakeSrv(fork)
    monkeypatch.setattr(cm, "_server", lambda: srv)
    wt = str(repo)
    srv._DIFF_STAT_CACHE[wt] = (time.time() + 60, {}, "cached-fp")
    assert cm.fingerprint(object(), wt) == "cached-fp"
    assert srv.fp_calls == 0
    srv._DIFF_STAT_CACHE[wt] = (time.time() - 1, {}, "stale-fp")
    fp = cm.fingerprint(object(), wt)
    assert fp and fp != "stale-fp" and srv.fp_calls == 1


# --------------------------------------------------------------------------- #
# Import graph
# --------------------------------------------------------------------------- #

_TSCONFIG = """{
  // comments and trailing commas are fine in tsconfig
  "compilerOptions": {
    "baseUrl": ".",
    /* block comment */
    "paths": {
      "@/*": ["src/*"],
      "~cfg": ["src/config.ts",],
    },
  },
}
"""


def _graph_repo(root: Path) -> Path:
    files = {
        # Python — absolute, relative, package __init__
        "pkg/__init__.py": "from .core import thing\n",
        "pkg/core.py": "thing = 1\n",
        "pkg/util.py": "from . import core\nimport os, sys\n",
        "pkg/sub/__init__.py": "",
        "pkg/sub/deep.py": "from ..util import helper\nfrom .. import core\n",
        "app.py": (
            "import pkg.core\n"
            "from pkg import util\n"
            "from pkg.sub import deep\n"
            "from pkg.core import thing as t  # a name, not a module\n"
            '"""\n    import core/engine helpers directly (prose, not code)\n"""\n'
        ),
        "src/lib2/__init__.py": "",
        "src/lib2/mod.py": "",
        "tests/test_mod.py": "import lib2.mod\nimport json\n",
        "tools/json.py": "",  # must NOT capture every `import json`
        "scripts/helper.py": "",
        "scripts/run.py": "import helper\nimport json\n",
        # JS/TS — relative, index, ESM .js→.ts, require, dynamic, aliases
        "web/tsconfig.json": _TSCONFIG,
        "web/src/main.ts": (
            "import React from 'react'\n"
            "import App from './App'\n"
            "import { util } from '@/lib/util'\n"
            "import cfg from '~cfg'\n"
            "import './styles.css'\n"
            "import * as C from './components'\n"
            "const lazy = () => import('./lazy')\n"
            "const cjs = require('./legacy')\n"
            "import raw from './data.json?raw'\n"
            "obj.import('./not-an-import')\n"
        ),
        "web/src/App.tsx": "export default function App() { return null }\n",
        "web/src/config.ts": "export default {}\n",
        "web/src/lib/util.ts": "export * from './other.js'\nexport const util = 1\n",
        "web/src/lib/other.ts": "export const o = 1\n",
        "web/src/components/index.ts": ("export {\n  Button,\n} from './Button'\n"),
        "web/src/components/Button.tsx": "export const Button = 1\n",
        "web/src/lazy.tsx": "",
        "web/src/legacy.cjs": "",
        "web/src/data.json": "{}\n",
        "web/src/not-an-import.ts": "",
        # CSS
        "web/src/styles.css": '@import "./base.css";\n@import url("theme.scss");\n',
        "web/src/base.css": "",
        "web/src/theme.scss": '@use "sass:math";\n@use "vars";\n',
        "web/src/_vars.scss": "",
    }
    for rel, text in files.items():
        _write(root, rel, text)
    return root


def _edge_set(rows, graph):
    rels = [r[0] if not isinstance(r, str) else r for r in rows]
    return {(rels[s], rels[d]) for s, d in graph["edges"]}


def test_build_graph_resolvers(tmp_path):
    root = _graph_repo(tmp_path / "g")
    rels = sorted(
        str(p.relative_to(root)).replace(os.sep, "/")
        for p in root.rglob("*")
        if p.is_file()
    )
    g = cm.build_graph(str(root), rels)
    edges = _edge_set(rels, g)
    assert g["partial"] is False

    expected = {
        ("pkg/__init__.py", "pkg/core.py"),
        ("pkg/util.py", "pkg/core.py"),
        ("pkg/sub/deep.py", "pkg/util.py"),
        ("pkg/sub/deep.py", "pkg/core.py"),
        ("app.py", "pkg/core.py"),
        ("app.py", "pkg/util.py"),
        ("app.py", "pkg/sub/deep.py"),
        ("tests/test_mod.py", "src/lib2/mod.py"),
        ("scripts/run.py", "scripts/helper.py"),
        ("web/src/main.ts", "web/src/App.tsx"),
        ("web/src/main.ts", "web/src/lib/util.ts"),
        ("web/src/main.ts", "web/src/config.ts"),
        ("web/src/main.ts", "web/src/styles.css"),
        ("web/src/main.ts", "web/src/components/index.ts"),
        ("web/src/main.ts", "web/src/lazy.tsx"),
        ("web/src/main.ts", "web/src/legacy.cjs"),
        ("web/src/main.ts", "web/src/data.json"),
        ("web/src/lib/util.ts", "web/src/lib/other.ts"),
        ("web/src/components/index.ts", "web/src/components/Button.tsx"),
        ("web/src/styles.css", "web/src/base.css"),
        ("web/src/styles.css", "web/src/theme.scss"),
        ("web/src/theme.scss", "web/src/_vars.scss"),
    }
    assert expected <= edges, sorted(expected - edges)
    # The false positives the resolvers are built to avoid:
    assert ("tests/test_mod.py", "tools/json.py") not in edges
    assert ("scripts/run.py", "tools/json.py") not in edges
    assert ("web/src/main.ts", "web/src/not-an-import.ts") not in edges
    assert not any(s == "app.py" and d.endswith("__init__.py") for s, d in edges)
    assert not any(s == d for s, d in edges)
    assert g["langs"]["py"] == 12
    assert g["langs"]["css"] == 2 and g["langs"]["scss"] == 2
    assert g["langs"]["ts"] >= 5


def test_build_graph_budget_goes_partial_then_resumes_from_memo(tmp_path, monkeypatch):
    root = _graph_repo(tmp_path / "g")
    rels = sorted(
        str(p.relative_to(root)).replace(os.sep, "/")
        for p in root.rglob("*")
        if p.is_file()
    )
    monkeypatch.setattr(cm, "GRAPH_BUDGET_S", -1.0)  # over budget from the start
    g = cm.build_graph(str(root), rels, fp="fp")
    assert g["partial"] is True and g["edges"] == []
    assert not cm._GRAPH_CACHE  # a partial graph is never cached

    monkeypatch.setattr(cm, "GRAPH_BUDGET_S", 6.0)
    full = cm.build_graph(str(root), rels, fp="fp")
    assert full["partial"] is False and full["edges"]

    # Everything is memoized now: even a zero budget completes.
    cm._GRAPH_CACHE.clear()
    monkeypatch.setattr(cm, "GRAPH_BUDGET_S", -1.0)
    again = cm.build_graph(str(root), rels, fp="fp2")
    assert again == full


def test_build_graph_rescans_a_changed_file(tmp_path):
    root = tmp_path / "g"
    _write(root, "a.py", "")
    _write(root, "b.py", "")
    rels = ["a.py", "b.py"]
    assert cm.build_graph(str(root), rels)["edges"] == []
    _write(root, "a.py", "import b\n# grown\n")
    assert cm.build_graph(str(root), rels)["edges"] == [[0, 1]]


def test_build_graph_accepts_rows_and_skips_big_file_tail(tmp_path, monkeypatch):
    root = tmp_path / "g"
    _write(root, "b.py", "")
    _write(root, "c.py", "")
    _write(root, "a.py", "import b\n" + "#" * 200 + "\nimport c\n")
    monkeypatch.setattr(cm, "_MAX_SCAN_BYTES", 50)
    rows = [["a.py", 1, 0], ["b.py", 0, 0], ["c.py", 0, 0]]
    assert cm.build_graph(str(root), rows)["edges"] == [[0, 1]]


def test_build_graph_partial_is_reported_and_always_converges(tmp_path, monkeypatch):
    """F26/F32: a partial graph must be visible to the route (which may not
    answer "unchanged" over it) and repeated builds must converge even when
    the deadline is gone before any new file is read."""
    root = tmp_path / "g"
    n = 40
    for i in range(n):
        _write(root, f"m{i:02d}.py", f"import m{i + 1:02d}\n" if i + 1 < n else "")
    rels = [f"m{i:02d}.py" for i in range(n)]
    wt = str(root)
    assert cm.last_graph_partial(wt) is False  # no build yet

    monkeypatch.setattr(cm, "GRAPH_BUDGET_S", -1.0)  # non-positive: read nothing
    assert cm.build_graph(wt, rels, fp="fp")["partial"] is True
    assert cm.last_graph_partial(wt) is True

    # A deadline that is always already past: only the progress floor reads.
    monkeypatch.setattr(cm, "GRAPH_BUDGET_S", 1e-9)
    monkeypatch.setattr(cm, "_MIN_FRESH_READS", 7, raising=False)
    builds = 0
    while True:
        g = cm.build_graph(wt, rels, fp="fp")
        builds += 1
        assert cm.last_graph_partial(wt) is g["partial"]
        if not g["partial"]:
            break
        assert builds < 20, "a partial graph never converged"
    assert builds == -(-n // 7)  # ceil: 7 new files per build
    assert len(g["edges"]) == n - 1
    # A cache hit is a complete build too.
    assert cm.build_graph(wt, rels, fp="fp")["partial"] is False
    assert cm.last_graph_partial(wt) is False
    assert cm.last_graph_partial("/never/built") is False


def test_build_graph_resolution_time_never_eats_the_read_budget(tmp_path, monkeypatch):
    """F26: reading and resolving were interleaved, so a big memoized prefix
    whose RESOLUTION took the whole budget left no time to read anything new
    — the graph stayed partial on every call. Reading now comes first."""
    root = tmp_path / "g"
    for i in range(6):
        _write(root, f"m{i}.py", f"import m{(i + 1) % 6}\n")
    rels = [f"m{i}.py" for i in range(6)]
    cm.build_graph(str(root), rels[:5])  # memoize all but the last file
    real = cm._py_resolve

    def slow(*a, **k):
        time.sleep(0.1)
        return real(*a, **k)

    monkeypatch.setattr(cm, "_py_resolve", slow)
    monkeypatch.setattr(cm, "GRAPH_BUDGET_S", 0.25)  # < 5 resolutions x 0.1 s
    monkeypatch.setattr(cm, "_MIN_FRESH_READS", 0, raising=False)
    g = cm.build_graph(str(root), rels, fp="fp")
    assert g["partial"] is False and len(g["edges"]) == 6


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_build_graph_and_feed_never_open_a_fifo(tmp_path, monkeypatch):
    """F5: git lists symlinks, so `lnk.js -> some.fifo` is a normal row; an
    open() on it blocked forever and parked a worker thread per Map fetch."""
    import threading

    fifo = tmp_path / "outside.fifo"
    os.mkfifo(fifo)
    root = tmp_path / "g"
    _write(root, "a.py", "import b\n")
    _write(root, "b.py", "")
    _write(root, "c.ts", "import x from '@/x'\n")  # forces a tsconfig lookup
    (root / "lnk.js").symlink_to(fifo)
    (root / "tsconfig.json").symlink_to(fifo)
    rels = ["a.py", "b.py", "c.ts", "lnk.js", "tsconfig.json"]
    feed = _feed_file("mf_fifo")
    feed.symlink_to(fifo)

    result = {}

    def run():
        result["graph"] = cm.build_graph(str(root), rels)
        result["feed"] = cm.read_feed("mf_fifo")
        result["trim"] = cm.trim_feed("mf_fifo", max_bytes=-1)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)
    if t.is_alive():  # release the blocked open() so the thread can finish
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        pytest.fail("a FIFO behind a symlink blocked the Map")
    assert result["graph"]["edges"] == [[0, 1]]
    assert result["feed"] == [] and result["trim"] is False
    assert cm._open_regular(str(fifo)) is None
    with pytest.raises(OSError):
        cm._open_regular(str(tmp_path / "missing"))


def test_js_import_scan_is_linear_on_semicolon_free_exports():
    """F30: the lazy span between `export` and `from` crossed newlines, so a
    semicolon-free file of quote-free exports with no `from` anywhere was
    quadratic (~7 s at 8000 lines, minutes at the 2 MB cap) — inside one
    file's scan, holding the GIL."""
    text = "".join(f"export const K_{i} = {i}\n" for i in range(8000))
    t0 = time.monotonic()
    assert cm._js_specs(text) == []
    assert time.monotonic() - t0 < 1.5
    # The tempered span still spans lines within ONE statement.
    assert cm._js_specs(
        "export const a = 1\nimport {\n  x,\n  y,\n} from './late'\n"
        'export type { T } from "./types"\n'
    ) == ["./late", "./types"]


def test_lenient_json():
    assert cm._lenient_json('{"a": "x//y", /* c */ "b": [1,2,],}') == {
        "a": "x//y",
        "b": [1, 2],
    }


# --------------------------------------------------------------------------- #
# blast
# --------------------------------------------------------------------------- #


def test_blast_depth_semantics():
    # 1 imports 0, 2 imports 1, 3 imports 2, 4 imports 0; 0 imports 3 (cycle)
    edges = [[1, 0], [2, 1], [3, 2], [4, 0], [0, 3]]
    assert cm.blast(edges, 5, [0], 1) == {1: 1, 4: 1}
    assert cm.blast(edges, 5, [0], 2) == {1: 1, 4: 1, 2: 2}
    assert cm.blast(edges, 5, [0], 3) == {1: 1, 4: 1, 2: 2, 3: 3}
    assert cm.blast(edges, 5, [0], 9) == cm.blast(edges, 5, [0], 3)  # clamped
    assert cm.blast(edges, 5, [0], 0) == cm.blast(edges, 5, [0], 1)
    assert cm.blast(edges, 5, [0, 1], 1) == {4: 1, 2: 1}  # seeds excluded
    assert cm.blast(edges, 5, [99], 2) == {}
    assert cm.blast([[7, 0]], 2, [0], 1) == {}  # out-of-range edge ignored


# --------------------------------------------------------------------------- #
# Tool feed
# --------------------------------------------------------------------------- #


def _feed_file(name="mf_sess") -> Path:
    p = Path(cm._feed_path(name))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_feed_path_is_sanitized_under_the_env_dir(tmp_path):
    p = cm._feed_path("mindflock_a b/c:d")
    assert p == str(tmp_path / "feed" / "mindflock_a_b_c_d.jsonl")


def test_read_feed_since_limit_and_torn_lines():
    p = _feed_file()
    lines = [
        json.dumps({"v": 1, "ts": 100.0 + i, "ev": "pre", "tool": "Edit"})
        for i in range(5)
    ]
    lines.insert(2, "not json")
    lines.insert(3, json.dumps([1, 2]))
    lines.insert(4, json.dumps({"ts": "nope"}))
    p.write_text("\n".join(lines) + "\n" + '{"v":1,"ts":999.0,"ev":"po')  # torn tail
    recs = cm.read_feed("mf_sess")
    assert [r["ts"] for r in recs] == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert [r["ts"] for r in cm.read_feed("mf_sess", since=102.0)] == [103.0, 104.0]
    assert [r["ts"] for r in cm.read_feed("mf_sess", limit=2)] == [103.0, 104.0]
    assert cm.read_feed("no_such_session") == []


def test_read_feed_tail_skips_partial_first_line_and_sorts(monkeypatch):
    p = _feed_file()
    recs = [{"v": 1, "ts": float(t), "pad": "x" * 40} for t in (1, 2, 4, 3, 5)]
    p.write_text("".join(json.dumps(r) + "\n" for r in recs))
    line_len = len(json.dumps(recs[0])) + 1
    # Tail = the last 2.5 lines: the first is cut mid-record and must be dropped.
    monkeypatch.setattr(cm, "_FEED_TAIL_BYTES", int(line_len * 2.5))
    assert [r["ts"] for r in cm.read_feed("mf_sess")] == [3.0, 5.0]
    monkeypatch.setattr(cm, "_FEED_TAIL_BYTES", 10 * line_len)
    assert [r["ts"] for r in cm.read_feed("mf_sess")] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_trim_feed_keeps_whole_newest_lines():
    p = _feed_file()
    p.write_text(
        "".join(
            json.dumps({"ts": float(i), "pad": "y" * 50}) + "\n" for i in range(200)
        )
    )
    size = p.stat().st_size
    assert cm.trim_feed("mf_sess", max_bytes=size + 1, keep_bytes=1000) is False
    assert cm.trim_feed("mf_sess", max_bytes=1000, keep_bytes=1000) is True
    assert p.stat().st_size <= 1000
    assert (p.stat().st_mode & 0o777) == 0o600
    text = p.read_text()
    assert text.endswith("\n")
    parsed = [json.loads(line) for line in text.splitlines()]
    assert parsed[-1]["ts"] == 199.0
    assert [r["ts"] for r in parsed] == sorted(r["ts"] for r in parsed)
    assert cm.trim_feed("missing") is False


def test_trim_feed_carries_over_records_appended_around_the_replace(monkeypatch):
    """F28: the hook appends by PATH with no lock. A deny record written after
    trim's catch-up read (during fsync/rename) went to the OLD inode and died
    with it — and deny records are the monitor's only source of
    session.red_zone_blocked."""
    p = _feed_file()
    p.write_text(
        "".join(
            json.dumps({"ts": float(i), "id": f"r{i}", "pad": "y" * 50}) + "\n"
            for i in range(200)
        )
    )
    real_replace = os.replace

    def racing_replace(src, dst):
        fd = os.open(dst, os.O_WRONLY | os.O_APPEND)  # a hook opens by path...
        try:
            os.write(fd, (json.dumps({"ts": 500.0, "id": "deny1"}) + "\n").encode())
            real_replace(src, dst)
            # ...and one that opened before the rename writes just after it
            os.write(fd, (json.dumps({"ts": 501.0, "id": "deny2"}) + "\n").encode())
        finally:
            os.close(fd)

    monkeypatch.setattr(os, "replace", racing_replace)
    assert cm.trim_feed("mf_sess", max_bytes=1000, keep_bytes=1000) is True
    monkeypatch.setattr(os, "replace", real_replace)
    ids = [r.get("id") for r in cm.read_feed("mf_sess")]
    assert ids[-2:] == ["deny1", "deny2"]
    assert "r199" in ids and "r0" not in ids  # it still trimmed
    assert (p.stat().st_mode & 0o777) == 0o600


def test_read_feed_one_bad_line_never_costs_its_neighbours():
    """F29: the hook cuts a >16 KB record mid-JSON, and a hostile line can be
    nested deep enough that json raises RecursionError (not ValueError) —
    which escaped the per-line skip and emptied the WHOLE feed."""
    p = _feed_file()
    big = json.dumps({"ts": 2.0, "id": "big", "breach": ["x" * 40] * 600})
    lines = [
        json.dumps({"ts": 1.0, "id": "a"}).encode(),
        big[:16384].encode(),  # the hook's cut: invalid JSON
        b"[" * 100_000,  # RecursionError in the decoder
        b'{"ts": 3.0, "id": "\xff\xfe"}',  # invalid UTF-8
        json.dumps({"ts": 4.0, "id": "b"}).encode(),
    ]
    p.write_bytes(b"\n".join(lines) + b"\n")
    assert [r["id"] for r in cm.read_feed("mf_sess")] == ["a", "b"]


def test_gc_snaps_removes_only_old_snapshots(tmp_path):
    snap = tmp_path / "feed" / ".snap"
    snap.mkdir(parents=True)
    old, new, other = snap / "toolu_old.json", snap / "toolu_new.json", snap / "x.txt"
    for f in (old, new, other):
        f.write_text("{}")
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert cm.gc_snaps(max_age_s=3600) == 1
    assert not old.exists() and new.exists() and other.exists()
    assert cm.gc_snaps() == 0


def test_gc_snaps_without_dir_is_zero():
    assert cm.gc_snaps() == 0


def test_feed_transcript_path_is_the_newest_tp():
    recs = [
        {"ts": 1.0, "tp": "/t/a.jsonl"},
        {"ts": 3.0, "ev": "post"},
        {"ts": 2.0, "tp": "/t/b.jsonl"},
        {"ts": 0.5, "tp": ""},
    ]
    assert cm.feed_transcript_path(recs) == "/t/b.jsonl"
    assert cm.feed_transcript_path([]) is None


# --------------------------------------------------------------------------- #
# Plans — declared block parsing
# --------------------------------------------------------------------------- #


@pytest.fixture
def plan_wt(tmp_path):
    wt = tmp_path / "wt"
    for rel in (
        "backend/a.py",
        "backend/web/server.py",
        "pkg/__init__.py",
        "Makefile",
        "README.md",
    ):
        _write(wt, rel, "x\n")
    return wt


def _block(*lines: str) -> str:
    return (
        "Here is my plan:\n\n```mindflock-plan\n"
        + "\n".join(lines)
        + "\n```\n\nWaiting."
    )


@pytest.mark.parametrize(
    "line,path,intent",
    [
        ("backend/a.py — add the thing", "backend/a.py", "add the thing"),
        ("backend/a.py – en dash", "backend/a.py", "en dash"),
        ("backend/a.py - add the thing", "backend/a.py", "add the thing"),
        ("backend/a.py -- two dashes", "backend/a.py", "two dashes"),
        ("backend/a.py: add the thing", "backend/a.py", "add the thing"),
        ("- backend/a.py (add the thing)", "backend/a.py", "add the thing"),
        ("backend/a.py", "backend/a.py", ""),
        ("backend/a.py:", "backend/a.py", ""),
        ("**backend/a.py** — bold path", "backend/a.py", "bold path"),
        ("`backend/a.py` — ticked path", "backend/a.py", "ticked path"),
        ("1. `backend/a.py`: numbered", "backend/a.py", "numbered"),
        ("* [ ] backend/a.py — checkbox", "backend/a.py", "checkbox"),
        ("backend/a.py—glued dash", "backend/a.py", "glued dash"),
        ("./backend/a.py — dot slash", "backend/a.py", "dot slash"),
        ("backend/a.py:12 — line number", "backend/a.py", "line number"),
        ("backend\\a.py — backslashes", "backend/a.py", "backslashes"),
        ("backend/web/ — a whole dir", "backend/web", "a whole dir"),
        ("pkg/__init__.py — dunder kept", "pkg/__init__.py", "dunder kept"),
        ("Makefile — exists, no dot", "Makefile", "exists, no dot"),
        ("- backend/a.py — rename `f` to **g**", "backend/a.py", "rename f to g"),
        # F27: shapes agents actually emit that used to mangle the path
        ("backend/a.py:40-55 — line range", "backend/a.py", "line range"),
        ("backend/a.py:40-55:3 — range + col", "backend/a.py", "range + col"),
        ("backend/a.py#L40 — anchor", "backend/a.py", "anchor"),
        ("backend/a.py#L40-L55 — anchor range", "backend/a.py", "anchor range"),
        ("README.md. — sentence period", "README.md", "sentence period"),
        ("✅ backend/a.py — done", "backend/a.py", "done"),
        ("NEW: backend/a.py — tagged", "backend/a.py", "tagged"),
        ("(new) backend/a.py — paren tag", "backend/a.py", "paren tag"),
        ("Update `backend/a.py` — verb first", "backend/a.py", "verb first"),
        ("- [x] ✅ backend/a.py — checked + emoji", "backend/a.py", "checked + emoji"),
        ("| backend/a.py | table row |", "backend/a.py", "table row"),
        ("| `backend/a.py` | ticked | cell |", "backend/a.py", "ticked | cell"),
        ("Makefile: build target", "Makefile", "build target"),
    ],
)
def test_plan_line_forms(plan_wt, line, path, intent):
    items = cm.parse_plan_block(_block(line), None, str(plan_wt))
    assert items == [{"path": path, "intent": intent, "new": False}]


@pytest.mark.parametrize(
    "line",
    [
        "# Files",
        "",
        "// comment",
        "Nothing else needed",
        "/elsewhere/x.py — outside the worktree",
        "~/.claude/plans/p.md — home path",
        "https://example.com/a.py — a url",
        "../escape.py — climbs out",
        "| --- | :---: |",
        "| File | Change |",
        "Then run the tests",
    ],
)
def test_plan_lines_that_are_not_items(plan_wt, line):
    assert cm.parse_plan_block(_block(line), None, str(plan_wt)) == []


def test_plan_line_with_several_paths_shares_one_intent(plan_wt):
    items = cm.parse_plan_block(
        _block(
            "backend/a.py, backend/web/server.py — both",
            "`pkg/__init__.py`, `README.md`: ticked pair",
            "backend/a.py, then the rest — prose after a comma",
        ),
        None,
        str(plan_wt),
    )
    assert [(i["path"], i["intent"]) for i in items] == [
        ("backend/a.py", "both"),
        ("backend/web/server.py", "both"),
        ("pkg/__init__.py", "ticked pair"),
        ("README.md", "ticked pair"),
    ]


def test_plan_absolute_path_under_wt_is_relativized(plan_wt):
    items = cm.parse_plan_block(
        _block(
            f"{plan_wt}/backend/a.py — abs", f"{os.path.realpath(plan_wt)}/new/x.py"
        ),
        None,
        str(plan_wt),
    )
    assert items == [
        {"path": "backend/a.py", "intent": "abs", "new": False},
        {"path": "new/x.py", "intent": "", "new": True},
    ]


def test_plan_new_flag_uses_the_file_set_when_given(plan_wt):
    text = _block("backend/a.py — edit", "backend/b.py — create", "backend/web — dir")
    by_disk = cm.parse_plan_block(text, None, str(plan_wt))
    assert [i["new"] for i in by_disk] == [False, True, False]
    rows = [["backend/b.py", 1, 0]]  # file set says b exists, a doesn't
    by_set = cm.parse_plan_block(text, rows, str(plan_wt))
    assert [i["new"] for i in by_set] == [True, False, False]


def test_plan_block_last_fence_wins_dedupes_and_handles_unterminated(plan_wt):
    text = (
        _block("backend/old.py — first plan")
        + "\nRevised:\n~~~mindflock-plan\nbackend/a.py — one\nbackend/a.py — dup\n"
        "backend/c.py — unterminated fence runs to the end"
    )
    items = cm.parse_plan_block(text, None, str(plan_wt))
    assert [(i["path"], i["intent"]) for i in items] == [
        ("backend/a.py", "one"),
        ("backend/c.py", "unterminated fence runs to the end"),
    ]
    assert cm.parse_plan_block("no plan here", None, str(plan_wt)) is None
    assert cm.parse_plan_block("see mindflock-plan below", None, str(plan_wt)) is None
    assert cm.parse_plan_block("```mindflock-plan\n```", None, str(plan_wt)) == []


# --------------------------------------------------------------------------- #
# Plans — ExitPlanMode prose
# --------------------------------------------------------------------------- #

# Verbatim `tool_input.plan` from a real Claude Code 2.1.284 ExitPlanMode
# PreToolUse payload (scratchpad critique-claude/payloads.jsonl).
_EXITPLAN_SAMPLE = (
    "# Plan: Rename function f to g\n\n## Context\nRename function `f` to `g` in "
    "`src/mod.py` and update references in `a.txt`.\n\n## Changes\n1. "
    "**src/mod.py**: Rename function definition `def f(...)` to `def g(...)`\n2. "
    "**a.txt**: Update all mentions of `f` to `g`\n\n## Verification\n- Grep to "
    "confirm no remaining references to function `f` in both files\n- Run tests "
    "if present to verify the rename doesn't break anything\n"
)


def test_exitplan_items_from_a_real_plan(tmp_path):
    wt = tmp_path / "wt"
    _write(wt, "src/mod.py", "def f(): pass\n")
    _write(wt, "a.txt", "f\n")
    items = cm.exitplan_items(_EXITPLAN_SAMPLE, None, str(wt))
    assert [i["path"] for i in items] == ["src/mod.py", "a.txt"]
    assert all(i["new"] is False for i in items)
    # The file set, when given, decides "exists": a.txt is then a planned NEW
    # file (root-level, but set off in backticks/bold with an extension).
    items = cm.exitplan_items(_EXITPLAN_SAMPLE, [["src/mod.py", 1, 0]], str(wt))
    assert [(i["path"], i["new"]) for i in items] == [
        ("src/mod.py", False),
        ("a.txt", True),
    ]


def test_exitplan_items_new_files_and_prose_noise(tmp_path):
    wt = tmp_path / "wt"
    _write(wt, "src/mod.py", "")
    _write(wt, "docs/x.md", "")
    plan = (
        "## Changes\n"
        "1. **src/helpers/new.py** — nope, parent dir doesn't exist\n"
        "2. Create src/new_helper.py: shared helper\n"
        "3. Add `CHANGELOG.md` entry\n"
        "4. Mention NOTES.md in passing, e.g. like Node.js does\n"
        "5. See https://github.com/org/repo/blob/main/src/mod.py for context\n"
        "6. Update the whole src/ dir and and/or the docs/ folder\n"
        f"7. Edit {wt}/docs/x.md: absolute path\n"
        "8. Save to ~/.claude/plans/p.md\n"
        "9. Touch src/mod.py again\n"
    )
    items = cm.exitplan_items(plan, None, str(wt))
    assert [(i["path"], i["new"]) for i in items] == [
        ("src/new_helper.py", True),
        ("CHANGELOG.md", True),
        ("docs/x.md", False),
        ("src/mod.py", False),
    ]
    assert items[0]["intent"] == "shared helper"
    assert items[2]["intent"] == "absolute path"


def test_exitplan_items_keep_existing_extensionless_files(tmp_path):
    """F27: the `/`-or-`.` requirement ran BEFORE the file-set check, so an
    ExitPlanMode plan naming `Dockerfile` / `Makefile` lost them, and editing
    them was then flagged off-plan."""
    wt = tmp_path / "wt"
    for rel in ("Dockerfile", "Makefile", "build", "src/app/views.py"):
        _write(wt, rel, "x\n")
    plan = (
        "1. **`Dockerfile`**: bump base image\n"
        "2. `src/app/views.py`: use the new env var\n"
        "3. Update the Makefile build target\n"
        "4. Then build and run it\n"
    )
    for fs in (None, ["Dockerfile", "Makefile", "build", "src/app/views.py"]):
        items = cm.exitplan_items(plan, fs, str(wt))
        assert [i["path"] for i in items] == [
            "Dockerfile",
            "src/app/views.py",
            "Makefile",
        ], fs
        assert all(not i["new"] for i in items)
    # Not existing → not a file, however it is written.
    assert cm.exitplan_items("Edit the `Procfile`", None, str(wt)) == []


# --------------------------------------------------------------------------- #
# Plans — current_plan (declared vs exitplan, latch, thread)
# --------------------------------------------------------------------------- #


class _FakeProvider:
    def __init__(self, text=None, thread="T1"):
        self.text = text
        self.thread = thread
        self.calls = []

    def resume_thread_id(self, session_name):
        return self.thread

    def last_assistant_text(
        self, session_name, workdir, contains=None, transcript_path=None
    ):
        self.calls.append((session_name, workdir, contains, transcript_path))
        return self.text


def _plan_rec(ts, plan, tp="/t/T1.jsonl"):
    return {
        "v": 1,
        "ts": ts,
        "ev": "pre",
        "tool": "ExitPlanMode",
        "kind": "plan",
        "plan": plan,
        "tp": tp,
    }


def test_current_plan_declared_latch_and_exitplan(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    _write(wt, "src/mod.py", "")
    _write(wt, "a.txt", "")
    prov = _FakeProvider(text=_block("src/mod.py — rename", "src/new.py — create"))
    monkeypatch.setattr(cm, "_provider_for", lambda inst: prov)
    recs = [{"v": 1, "ts": 10.0, "ev": "pre", "tool": "Read", "tp": "/t/T1.jsonl"}]

    before = time.time()
    p = cm.current_plan(object(), "mf_s", str(wt), recs)
    assert p["source"] == "declared" and p["thread"] == "T1"
    assert p["ts"] >= before
    assert p["items"] == [
        {"path": "src/mod.py", "intent": "rename", "new": False},
        {"path": "src/new.py", "intent": "create", "new": True},
    ]
    assert prov.calls[-1] == ("mf_s", str(wt), "mindflock-plan", "/t/T1.jsonl")

    # Latched: the transcript no longer yields it and the records are gone.
    prov.text = None
    p2 = cm.current_plan(object(), "mf_s", str(wt), [])
    assert p2["source"] == "declared" and p2["ts"] == p["ts"]
    assert prov.calls[-1][3] == "/t/T1.jsonl"  # remembered transcript path
    # `new` is live: the planned file now exists
    _write(wt, "src/new.py", "")
    assert all(
        not i["new"] for i in cm.current_plan(object(), "mf_s", str(wt), [])["items"]
    )

    # An ExitPlanMode record newer than the declared block wins…
    time.sleep(0.01)
    ex_ts = time.time()
    p3 = cm.current_plan(
        object(), "mf_s", str(wt), [_plan_rec(ex_ts, _EXITPLAN_SAMPLE)]
    )
    assert p3["source"] == "exitplan" and p3["ts"] == ex_ts
    assert [i["path"] for i in p3["items"]] == ["src/mod.py", "a.txt"]
    # …and stays latched once the record scrolls out of the window.
    assert cm.current_plan(object(), "mf_s", str(wt), [])["source"] == "exitplan"
    # An OLDER record never displaces it.
    old = _plan_rec(ex_ts - 1, "- **a.txt**: only")
    assert cm.current_plan(object(), "mf_s", str(wt), [old])["items"] == p3["items"]

    # A new declared block (different text) is newer than everything.
    time.sleep(0.01)
    prov.text = _block("a.txt — just this")
    p4 = cm.current_plan(object(), "mf_s", str(wt), [])
    assert p4["source"] == "declared" and [i["path"] for i in p4["items"]] == ["a.txt"]
    assert p4["ts"] > ex_ts

    # A new thread (/clear, relaunch) starts from nothing.
    prov.text, prov.thread = None, "T2"
    p5 = cm.current_plan(object(), "mf_s", str(wt), [])
    assert p5 == {"source": None, "ts": None, "items": [], "thread": "T2"}
    # …and an old thread's plan record doesn't resurface in it.
    p6 = cm.current_plan(
        object(), "mf_s", str(wt), [_plan_rec(time.time() + 100, _EXITPLAN_SAMPLE)]
    )
    assert p6["source"] is None

    # The newest hook tp is still the OLD thread's: not handed to the provider.
    cm.current_plan(object(), "mf_s", str(wt), [_plan_rec(1.0, "x", tp="/t/T1.jsonl")])
    assert prov.calls[-1][3] is None

    cm.forget_plan("mf_s")
    prov.thread = "T1"
    assert cm.current_plan(object(), "mf_s", str(wt), [])["source"] is None


def test_current_plan_same_block_keeps_its_ts(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    prov = _FakeProvider(text=_block("x/y.py — a"))
    monkeypatch.setattr(cm, "_provider_for", lambda inst: prov)
    t1 = cm.current_plan(object(), "mf_s", str(wt), [])["ts"]
    time.sleep(0.01)
    assert cm.current_plan(object(), "mf_s", str(wt), [])["ts"] == t1


def test_current_plan_tolerates_providers_without_the_api(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    _write(wt, "src/mod.py", "")
    _write(wt, "a.txt", "")
    monkeypatch.setattr(cm, "_provider_for", lambda inst: object())
    recs = [_plan_rec(5.0, _EXITPLAN_SAMPLE, tp="/t/abc.jsonl")]
    p = cm.current_plan(object(), "mf_s", str(wt), recs)
    assert p["source"] == "exitplan" and p["thread"] == "abc"

    class _Boom:
        def resume_thread_id(self, n):
            raise RuntimeError("x")

        def last_assistant_text(self, *a, **k):
            raise RuntimeError("y")

    monkeypatch.setattr(cm, "_provider_for", lambda inst: _Boom())
    cm.forget_plan("mf_s")
    assert cm.current_plan(object(), "mf_s", str(wt), recs)["source"] == "exitplan"

    def _explode(inst):
        raise RuntimeError("no provider")

    monkeypatch.setattr(cm, "_provider_for", _explode)
    assert cm.current_plan(object(), "mf_s", str(wt), recs)["source"] is None


def test_old_conversations_approved_plan_does_not_resurface_after_clear(
    tmp_path, monkeypatch
):
    """F4: the hook stamps `tp` on `pre` only, so the `post` of conversation
    A's approved ExitPlanMode plan carried no conversation, passed the thread
    filter, and — being newest — was latched as conversation B's plan."""
    wt = tmp_path / "wt"
    _write(wt, "src/mod.py", "")
    _write(wt, "a.txt", "")
    prov = _FakeProvider(text=None, thread="NEW")
    monkeypatch.setattr(cm, "_provider_for", lambda inst: prov)
    pre = dict(_plan_rec(100.0, _EXITPLAN_SAMPLE, tp="/t/OLD.jsonl"), id="toolu_1")
    post = {k: v for k, v in pre.items() if k != "tp"}
    post.update(ev="post", ts=101.0)
    edit = {"v": 1, "ts": 200.0, "ev": "pre", "tool": "Edit", "tp": "/t/NEW.jsonl"}
    p = cm.current_plan(object(), "mf_s", str(wt), [pre, post, edit])
    assert p["source"] is None and p["thread"] == "NEW"

    # The same pair in THIS conversation: the post inherits its pre's tp.
    cm.forget_plan("mf_s")
    pre_new = dict(pre, tp="/t/NEW.jsonl")
    p = cm.current_plan(object(), "mf_s", str(wt), [pre_new, post])
    assert p["source"] == "exitplan" and p["ts"] == 101.0

    # A rejected plan (`fail`) is never the plan — the proposal stands.
    cm.forget_plan("mf_s")
    fail = dict(post, ev="fail", ts=102.0, plan="- **a.txt**: rejected")
    p = cm.current_plan(object(), "mf_s", str(wt), [pre_new, fail])
    assert p["source"] == "exitplan" and p["ts"] == 100.0


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _assistant(ts: float, text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": _iso(ts),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def test_declared_plan_ts_is_when_the_agent_wrote_it(tmp_path, monkeypatch):
    """F7/F25: the declared plan was stamped when THIS process first saw it,
    so a restart or a late first look at the Map re-stamped it to "now" —
    after every edit: off-plan emptied, and an older declared plan beat a
    newer ExitPlanMode plan. The transcript entry's own timestamp is used."""
    wt = tmp_path / "wt"
    _write(wt, "lib.py", "")
    _write(wt, "app.py", "")
    now = time.time()
    wrote = now - 600
    text = _block("lib.py — add Y")
    tp = tmp_path / "T1.jsonl"
    tp.write_text(
        json.dumps({"type": "user", "timestamp": _iso(wrote - 5), "message": {}})
        + "\n"
        + _assistant(wrote, text)
        + "\n"
    )
    prov = _FakeProvider(text=text, thread="T1")
    monkeypatch.setattr(cm, "_provider_for", lambda inst: prov)
    recs = [
        {"v": 1, "ts": now - 300, "ev": "pre", "tool": "Edit", "id": "e1",
         "tp": str(tp), "writes": [f"{wt}/app.py"]},
        {"v": 1, "ts": now - 299, "ev": "post", "tool": "Edit", "id": "e1",
         "writes": [f"{wt}/app.py"]},
    ]  # fmt: skip
    p = cm.current_plan(object(), "mf_s", str(wt), recs)  # first look, 10 min late
    assert p["source"] == "declared"
    assert p["ts"] == pytest.approx(wrote, abs=1e-3)
    assert cm.off_plan(p, [], recs, wt=str(wt)) == ["app.py"]

    cm._PLAN_LATCH.clear()  # a server restart
    cm._TP_LATCH.clear()
    assert cm.current_plan(object(), "mf_s", str(wt), recs)["ts"] == p["ts"]

    # Newest wins on REAL times: a later ExitPlanMode plan beats the block.
    cm._PLAN_LATCH.clear()
    ex = dict(_plan_rec(now - 120, _EXITPLAN_SAMPLE, tp=str(tp)), id="x1")
    assert cm.current_plan(object(), "mf_s", str(wt), recs + [ex])["source"] == (
        "exitplan"
    )


def test_declared_ts_is_where_the_latched_run_began(tmp_path):
    """Mirrors the latch: an identical re-emission keeps the first time, a
    different block in between starts a new run."""
    a, b = _block("x.py — a"), _block("y.py — b")
    ba, bb = cm.last_plan_block(a), cm.last_plan_block(b)
    tp = tmp_path / "T.jsonl"
    tp.write_text(
        "\n".join(
            [
                _assistant(100.0, b),  # an older run of b
                _assistant(200.0, a),
                _assistant(300.0, "no fence, just mentions mindflock-plan"),
                _assistant(400.0, b),
                _assistant(500.0, b),  # re-emitted: same run
                "not json mindflock-plan assistant",
            ]
        )
        + "\n"
    )
    assert cm._declared_ts(str(tp), bb) == 400.0
    assert cm._declared_ts(str(tp), ba) == 200.0
    assert cm._declared_ts(str(tp), cm.last_plan_block(_block("z.py"))) is None
    assert cm._declared_ts(None, bb) is None
    assert cm._declared_ts(str(tmp_path / "missing.jsonl"), bb) is None


# --------------------------------------------------------------------------- #
# off_plan
# --------------------------------------------------------------------------- #


def test_off_plan(tmp_path):
    wt = tmp_path / "wt"
    for rel in ("planned.py", "early.py", "late.py", "dir/inside.py"):
        _write(wt, rel, "")
    t0 = time.time()
    past = t0 - 100
    os.utime(wt / "early.py", (past, past))
    plan = {
        "source": "declared",
        "ts": t0 - 10,
        "items": [
            {"path": "planned.py", "intent": "", "new": False},
            {"path": "dir", "intent": "whole dir", "new": False},
        ],
        "thread": "T",
    }
    recs = [
        {"ts": t0 - 50, "ev": "post", "writes": [f"{wt}/before.py"]},  # before the plan
        {"ts": t0, "ev": "post", "writes": [f"{wt}/planned.py"]},
        {"ts": t0, "ev": "post", "writes": [f"{wt}/edit_off.py"]},
        {
            "ts": t0,
            "ev": "pre",
            "id": "t1",
            "writes": [f"{wt}/denied.py"],
            "deny": {"path": "denied.py"},
        },
        {"ts": t0, "ev": "pre", "id": "t2", "writes": [f"{wt}/failed.py"]},
        {"ts": t0 + 1, "ev": "fail", "id": "t2"},
        {"ts": t0, "ev": "post", "writes": ["/elsewhere/x.py", f"{wt}/dir/inside.py"]},
        {
            "ts": t0,
            "ev": "post",
            "writes": [str(wt / "dir")],
        },  # a dir (mkdir) — skipped
        {"ts": t0, "ev": "pre", "id": "t3", "writes": ["rel_already.py"]},
        # EnterWorktree sandbox: same logical file as planned.py
        {"ts": t0, "ev": "post", "writes": [f"{wt}/.claude/worktrees/w1/planned.py"]},
    ]
    changed = [
        {"path": "late.py", "status": "M", "added": 1, "removed": 0},
        {
            "path": "early.py",
            "status": "M",
            "added": 1,
            "removed": 0,
        },  # mtime before plan
        {
            "path": "removed_now.py",
            "status": "D",
            "added": 0,
            "removed": 1,
        },  # parent mtime
        "planned.py",
    ]
    os.utime(wt, None)  # the worktree root changed after the plan (removed_now.py)
    got = cm.off_plan(plan, changed, recs, wt=str(wt))
    assert got == ["edit_off.py", "late.py", "rel_already.py", "removed_now.py"]

    # Without wt: changed files count regardless of time; absolute writes can't be placed.
    got = cm.off_plan(plan, changed, recs)
    assert got == ["early.py", "late.py", "rel_already.py", "removed_now.py"]

    assert cm.off_plan(None, changed, recs, wt=str(wt)) == []
    assert cm.off_plan({"source": None, "items": []}, changed, recs) == []
    assert cm.off_plan(dict(plan, items=[]), changed, recs, wt=str(wt)) == []


# --------------------------------------------------------------------------- #
# Zone dry-run
# --------------------------------------------------------------------------- #


def test_preview_finds_a_gitignored_zone_file_behind_a_big_ignored_dir(
    tmp_path, monkeypatch
):
    """F3 (code_map side): preview handed zone_files a rule with only `re`, so
    the git-ignored listing couldn't be scoped by the pattern and walked the
    whole ignored tree — where `.venv/` sorts first, fills the scan bound, and
    a gitignored `config/local.toml` previewed as 0 files."""
    from backend.config import red_zones

    repo = _init_repo(tmp_path / "r")
    _write(repo, ".gitignore", ".venv/\nconfig/local.toml\n")
    for i in range(60):
        _write(repo, f".venv/lib/pkg{i:03d}.py", "")
    _write(repo, "config/local.toml", "secret = 1\n")
    _write(repo, "config/example.toml", "x = 1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "cfg", cwd=repo)
    # Stand-in for a 60k-entry .venv: a scan bound smaller than the ignored tree.
    monkeypatch.setattr(red_zones, "_IGNORED_SCAN_MAX", 50, raising=False)
    rules_seen = []
    real = red_zones.zone_files

    def spy(root, rules, *a, **k):
        rules_seen.append([dict(r) for r in rules])
        return real(root, rules, *a, **k)

    monkeypatch.setattr(red_zones, "zone_files", spy)
    res = cm.preview(str(repo), "config/local.toml")
    assert rules_seen and rules_seen[0][0].get("pattern") == "config/local.toml"
    assert res["count"] == 1 and res["sample"] == ["config/local.toml"]
    assert res["ignored_count"] == 1 and res["truncated"] is False


# --------------------------------------------------------------------------- #
# v3: edge names, entry points and the per-language resolvers
# --------------------------------------------------------------------------- #


def _detail(root: Path, files: dict) -> tuple:
    """Write ``files`` under ``root`` and return ``(rels, named_edges,
    entries)`` from :func:`cm.graph_detail` — edges as ``{(src, dst):
    names}`` over rel paths, entries as ``{rel: [entry]}``."""
    for rel, text in files.items():
        _write(root, rel, text)
    rels = sorted(files)
    g = cm.graph_detail(str(root), rels)
    assert g["partial"] is False
    named = {(rels[a], rels[b]): set(g["names"].get((a, b), ())) for a, b in g["edges"]}
    entries = {rels[i]: v for i, v in g["entry"].items()}
    return rels, named, entries


def test_build_graph_keeps_its_v2_shape(tmp_path):
    root = _graph_repo(tmp_path / "g")
    rels = sorted(
        str(p.relative_to(root)).replace(os.sep, "/")
        for p in root.rglob("*")
        if p.is_file()
    )
    g = cm.build_graph(str(root), rels)
    assert set(g) == {"edges", "partial", "langs"}
    d = cm.graph_detail(str(root), rels)
    assert d["edges"] == g["edges"]
    assert set(d) >= {"names", "entry", "api_edges"}


def test_python_edge_names_from_names_and_module_attrs(tmp_path):
    _rels, named, _e = _detail(
        tmp_path / "g",
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def foo(): pass\ndef baz(): pass\n",
            "pkg/util.py": "def bar(): pass\n",
            "pkg/core.py": "class Thing: pass\n",
            "a.py": (
                "from pkg import mod\n"
                "import pkg.util as u\n"
                "from pkg.core import Thing\n"
                "import pkg.mod\n"
                "mod.foo()\nu.bar()\npkg.mod.baz()\n"
            ),
        },
    )
    assert named[("a.py", "pkg/mod.py")] >= {"foo", "baz"}
    assert named[("a.py", "pkg/util.py")] == {"bar"}
    assert named[("a.py", "pkg/core.py")] == {"Thing"}


def test_js_edge_names_named_default_namespace_reexport(tmp_path):
    _rels, named, _e = _detail(
        tmp_path / "g",
        {
            "web/a.ts": (
                "import App from './App'\n"
                "import { x, y as z } from './lib'\n"
                "import * as U from './util'\n"
                "U.helper()\n"
                "export { w } from './w'\n"
            ),
            "web/App.tsx": "export default function App() { return null }\n",
            "web/lib.ts": "export const x = 1\nexport const y = 2\n",
            "web/util.ts": "export function helper() {}\n",
            "web/w.ts": "export const w = 1\n",
        },
    )
    assert named[("web/a.ts", "web/App.tsx")] == {"App"}
    assert named[("web/a.ts", "web/lib.ts")] == {"x", "y"}
    assert named[("web/a.ts", "web/util.ts")] == {"helper"}
    assert named[("web/a.ts", "web/w.ts")] == {"w"}


def test_go_resolver_links_only_files_defining_used_names(tmp_path):
    _rels, named, entries = _detail(
        tmp_path / "g",
        {
            "go.mod": "module example.com/app\n\ngo 1.22\n",
            "cmd/app/main.go": (
                "package main\n\nimport (\n"
                '\t"fmt"\n\tst "example.com/app/pkg/store"\n)\n\n'
                "func main() {\n\tfmt.Println(st.Open())\n}\n"
            ),
            "pkg/store/store.go": "package store\n\nfunc Open() int { return 1 }\n",
            "pkg/store/other.go": "package store\n\nfunc Other() int { return 2 }\n",
            "pkg/store/store_test.go": "package store\n\nfunc TestOpen() {}\n",
        },
    )
    assert named[("cmd/app/main.go", "pkg/store/store.go")] == {"Open"}
    assert ("cmd/app/main.go", "pkg/store/other.go") not in named
    assert ("cmd/app/main.go", "pkg/store/store_test.go") not in named
    assert [e["kind"] for e in entries["cmd/app/main.go"]] == ["main"]


def test_java_imports_same_package_and_never_main_to_test(tmp_path):
    base = "svc/src/main/java/com/acme/"
    tbase = "svc/src/test/java/com/acme/"
    _rels, named, _e = _detail(
        tmp_path / "g",
        {
            base
            + "model/Widget.java": (
                "package com.acme.model;\n\npublic class Widget {\n"
                "  public static class Part {}\n}\n"
            ),
            # same package, no import: Java never writes one
            base
            + "model/Gadget.java": (
                "package com.acme.model;\n\n"
                "// Mentions Fixture in a comment only.\n"
                'public class Gadget { Widget w; String s = "Helper"; }\n'
            ),
            base + "model/Helper.java": "package com.acme.model;\n\nclass Helper {}\n",
            base
            + "api/Ctl.java": (
                "package com.acme.api;\n\n"
                "import com.acme.model.Widget;\n"
                "import com.acme.model.Widget.Part;\n"
                "import com.acme.model.*;\n"
                "import static com.acme.util.Strings.slug;\n\n"
                "public class Ctl { Widget w; Gadget g; Fixture f; }\n"
            ),
            base
            + "util/Strings.java": (
                "package com.acme.util;\n\npublic class Strings {\n"
                "  public static String slug(String s) { return s; }\n}\n"
            ),
            tbase + "api/Fixture.java": "package com.acme.api;\n\nclass Fixture {}\n",
            tbase
            + "api/CtlTest.java": "package com.acme.api;\n\nclass CtlTest { Ctl c; }\n",
        },
    )
    assert named[(base + "api/Ctl.java", base + "model/Widget.java")] == {"Widget"}
    assert named[(base + "api/Ctl.java", base + "model/Gadget.java")] == {"Gadget"}
    assert named[(base + "api/Ctl.java", base + "util/Strings.java")] == {"Strings"}
    assert named[(base + "model/Gadget.java", base + "model/Widget.java")] == {"Widget"}
    # A class named only inside a string literal / comment is not a reference.
    assert (base + "model/Gadget.java", base + "model/Helper.java") not in named
    # main never depends on test code, even in the same package …
    assert (base + "api/Ctl.java", tbase + "api/Fixture.java") not in named
    # … while a test may use main code without an import.
    assert named[(tbase + "api/CtlTest.java", base + "api/Ctl.java")] == {"Ctl"}


def test_kotlin_top_level_function_import(tmp_path):
    k = "src/main/kotlin/com/x/"
    _rels, named, _e = _detail(
        tmp_path / "g",
        {
            k + "util/Strings.kt": "package com.x.util\n\nfun slugify(s: String) = s\n",
            k
            + "App.kt": (
                "package com.x\n\nimport com.x.util.slugify\n\n"
                'fun main() { println(slugify("a")) }\n'
            ),
        },
    )
    assert named[(k + "App.kt", k + "util/Strings.kt")] == {"slugify"}


def test_rust_mod_and_use_crate_super(tmp_path):
    _rels, named, _e = _detail(
        tmp_path / "g",
        {
            "src/lib.rs": "pub mod a;\n",
            "src/a.rs": "pub mod b;\npub mod c;\n",
            "src/a/b.rs": (
                "use crate::a::c::{Thing, Other};\nuse super::c::Thing as T;\n"
                "pub fn f() {}\n"
            ),
            "src/a/c.rs": "pub struct Thing;\npub struct Other;\n",
        },
    )
    assert ("src/lib.rs", "src/a.rs") in named
    assert ("src/a.rs", "src/a/b.rs") in named
    assert named[("src/a/b.rs", "src/a/c.rs")] >= {"Thing", "Other"}


def test_c_includes_follow_umbrella_headers_with_names(tmp_path):
    _rels, named, entries = _detail(
        tmp_path / "g",
        {
            "lib/lib.h": '#include "res.h"\n#include <stdio.h>\n',
            "lib/res.h": "int res_open(void);\nint res_close(void);\n",
            "lib/res.c": '#include "res.h"\nint res_open(void) { return 0; }\n',
            "src/main.c": (
                "#include <lib.h>\n\nint\nmain(void)\n{\n  return res_open();\n}\n"
            ),
            "src/lookup.c": (
                "int f(void) { return 1; }\n#ifdef SELF_TEST\n"
                "int main(void) { return 0; }\n#endif\n"
            ),
        },
    )
    assert ("src/main.c", "lib/lib.h") in named
    assert named[("src/main.c", "lib/res.h")] == {"res_open"}
    assert named[("lib/res.c", "lib/res.h")] >= {"res_open"}
    assert [e["kind"] for e in entries["src/main.c"]] == ["main"]
    assert "src/lookup.c" not in entries  # a main behind #ifdef is not an entry


def test_hcl_module_sources_name_variables_and_outputs(tmp_path):
    _rels, named, _e = _detail(
        tmp_path / "g",
        {
            "envs/prod/main.tf": (
                'module "vpc" {\n  source = "../../modules/vpc"\n  cidr = var.cidr\n}\n'
                'output "id" { value = module.vpc.vpc_id }\n'
            ),
            "modules/vpc/variables.tf": 'variable "cidr" {}\nvariable "unused" {}\n',
            "modules/vpc/outputs.tf": 'output "vpc_id" { value = "x" }\n',
        },
    )
    assert named[("envs/prod/main.tf", "modules/vpc/variables.tf")] == {"cidr"}
    assert named[("envs/prod/main.tf", "modules/vpc/outputs.tf")] == {"vpc_id"}


def test_api_edges_join_client_literals_to_routes(tmp_path):
    _rels, named, entries = _detail(
        tmp_path / "g",
        {
            "api/routes.py": (
                "from fastapi import APIRouter\n"
                'router = APIRouter(prefix="/api/items")\n\n'
                '@router.get("/{item_id}")\ndef get_item(item_id: int):\n    return 1\n\n'
                '@router.get("/")\ndef list_items():\n    return []\n\n'
                "def build():\n"
                '    inner = APIRouter(prefix="/api/nested")\n\n'
                '    @inner.post("/make")\n    async def make():\n        return 1\n'
                "    return inner\n"
            ),
            "web/src/client.ts": (
                "export const get = (id: string) => fetch(`/api/items/${id}`)\n"
                "export const make = () => fetch('/api/nested/make', { method: 'POST' })\n"
                "export const health = () => fetch('/health')\n"
            ),
            "web/src/client.test.ts": "fetch('/api/items/1')\n",
        },
    )
    got = named[("web/src/client.ts", "api/routes.py")]
    assert "GET /api/items/{item_id}" in got
    assert "POST /api/nested/make" in got
    # A route with fewer than 2 fixed segments is not API-edge material, and
    # test files are neither clients nor servers.
    assert ("web/src/client.test.ts", "api/routes.py") not in named
    routes = {(e["method"], e["route"]) for e in entries["api/routes.py"]}
    assert ("POST", "/api/nested/make") in routes  # nested router, prefixed
    assert ("GET", "/api/items/") in routes


def test_entry_points_across_frameworks(tmp_path):
    _rels, _named, entries = _detail(
        tmp_path / "g",
        {
            "web/flask_app.py": (
                "from flask import Blueprint\n"
                'bp = Blueprint("x", __name__, url_prefix="/v1")\n\n'
                '@bp.route("/things", methods=["GET", "POST"])\ndef things():\n    return 1\n'
            ),
            "server.js": (
                "app.get('/health', (req, res) => res.send('ok'))\n"
                'router.post("/api/users/:id", handler)\n'
                "client.get('/not/a/route')\n"
            ),
            "svc/src/main/java/com/x/Ctl.java": (
                "package com.x;\n\nimport a.b.C;\n\n"
                '@RestController\n@RequestMapping("/api/v1/widgets")\n'
                '@Timed(value = "t", extraTags = {"controller", "Ctl"})\n'
                "public class Ctl {\n"
                '  @GetMapping("/{id}")\n  public Widget getWidget(\n      @PathVariable Long id) {\n'
                "    return null;\n  }\n\n"
                "  @PostMapping\n  public Widget create(@RequestBody Widget w) { return w; }\n\n"
                '  @Scheduled(cron = "0 0 * * *")\n  public void nightly() {}\n\n'
                "  public static void main(String[] args) {}\n}\n"
            ),
            "main.go": (
                "package main\n\nfunc main() {\n"
                '\tr.GET("/api/ping", ping)\n\thttp.HandleFunc("/healthz", health)\n}\n'
            ),
            "cli.py": (
                "import click, argparse\n\n"
                "@cli.command()\ndef run():\n    pass\n\n"
                '@app.command("deploy")\ndef deploy_cmd():\n    pass\n\n'
                'sub.add_parser("serve")\n\n'
                'if __name__ == "__main__":\n    run()\n'
            ),
            "bot.py": (
                'SCAN = "/scan-now"\n\n'
                '@app.command("/scan")\ndef scan(ack, body):\n    ack()\n\n'
                "@app.command(SCAN)\ndef scan_now(ack, command):\n    ack()\n\n"
                '@app.event("app_mention")\ndef mention(event, say):\n    pass\n\n'
                "@shared_task\ndef job():\n    pass\n\n"
                "@celery_app.task(bind=True)\ndef job2(self):\n    pass\n"
            ),
        },
    )

    def kinds(rel):
        return sorted((e["kind"], e["method"], e["route"]) for e in entries[rel])

    assert kinds("web/flask_app.py") == [("http", "GET/POST", "/v1/things")]
    assert kinds("server.js") == [
        ("http", "GET", "/health"),
        ("http", "POST", "/api/users/:id"),
    ]
    java = {
        (e["kind"], e["method"], e["route"], e["handler"])
        for e in entries["svc/src/main/java/com/x/Ctl.java"]
    }
    assert ("http", "GET", "/api/v1/widgets/{id}", "getWidget") in java
    assert ("http", "POST", "/api/v1/widgets", "create") in java
    assert ("event", "SCHEDULED", "0 0 * * *", "nightly") in java
    assert ("main", "MAIN", "Ctl.main", "main") in java
    assert kinds("main.go") == [
        ("http", "ANY", "/healthz"),
        ("http", "GET", "/api/ping"),
        ("main", "MAIN", "main()"),
    ]
    assert kinds("cli.py") == [
        ("cli", "CLI", "deploy"),
        ("cli", "CLI", "run"),
        ("cli", "CLI", "serve"),
        ("main", "MAIN", "__main__"),
    ]
    assert kinds("bot.py") == [
        ("event", "EVENT", "app_mention"),
        ("event", "SLACK", "/scan"),
        ("event", "SLACK", "SCAN"),
        ("event", "TASK", "job"),
        ("event", "TASK", "job2"),
    ]


_PATHOLOGICAL = {
    "java-unclosed-comments": ("java", "A.java", "class A { int x; } /* " * 20000),
    "java-mappings": ("java", "B.java", "class B {\n" + "  @GetMapping(\n" * 20000),
    "c-unclosed-comments": ("c", "a.c", "int a; /* x " * 20000),
    "c-mains-in-ifdefs": ("c", "m.c", "#if X\nint main(void) {}\n" * 5000),
    "js-unclosed-braces": ("js", "a.ts", "import { a, b " * 20000),
    "js-literals": ("js", "b.ts", "fetch('/api/" + "x" * 50 + "' " * 20000),
    "py-open-decorators": ("py", "a.py", "@app.get(\n" * 20000),
    "py-routes-no-defs": ("py", "b.py", "@app.get('/api/x/y')\n" * 20000),
    "py-routers": ("py", "c.py", "r = APIRouter(\n" * 20000),
    "rust-open-braces": ("rust", "a.rs", "use crate::{a, b" * 20000),
    "rust-routes-no-fn": ("rust", "b.rs", '#[get("/x")]\n' * 20000),
    "go-unclosed-import-blocks": ("go", "a.go", 'import (\n "x"\n' * 20000),
    "go-unclosed-const-blocks": ("go", "b.go", "const (\n X = 1\n" * 20000),
}


@pytest.mark.parametrize("case", sorted(_PATHOLOGICAL))
def test_scanners_stay_linear_and_never_raise_on_pathological_text(case):
    """Every scanner runs inside one file's read, holding the GIL: a
    quadratic regex here stalls the whole server (see the _JS_FROM_RE note)."""
    lang, rel, text = _PATHOLOGICAL[case]
    t0 = time.monotonic()
    rec = cm._scan_text(text, lang, rel)
    assert isinstance(rec, dict)
    assert time.monotonic() - t0 < 2.0


def test_attr_uses_ignore_file_names_in_prose():
    """``server.py`` in a docstring is a file name, not ``server.py`` read
    off the ``server`` alias; a real call/index/deeper chain still counts."""
    text = (
        '"""Keeps ``server.py`` routes-only; see server.json too."""\n'
        "from backend.web import server\n"
        "server.app\n"
        "server.json()\n"
        "server.py.thing\n"
    )
    uses = cm._attr_uses(text, ["server"])
    assert uses == {"server": ("app", "json", "py")}
    assert cm._attr_uses("see server.py\n", ["server"]) == {}
