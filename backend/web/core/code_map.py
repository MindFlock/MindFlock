"""Code Map analysis: the worktree's file list, change set, import graph, blast
radius, tool feed and the agent's declared plan — everything the Map pane tab
and the red-zone monitor read, computed here so ``server.py`` stays routes-only.

WHY A SEPARATE MODULE. The Map is a view over four independent sources — git
(what exists / what changed), the source text (who imports whom), the tool
feed the provider hook appends to (what the agent is touching right now), and
the agent's transcript (what it SAID it would touch). Each is cheap on its own
only because it is cached against something that proves it has not moved: the
file list, change set and graph against the same worktree fingerprint the
header's diff stat already computes (so an idle session costs a ``git status``
per poll, not a re-scan), the per-file import scan against ``(mtime, size)``,
the transcript scan behind the provider's own ``(path, mtime, size)`` cache.

WHAT "NEVER RAISES" MEANS HERE. Every public function returns an empty/neutral
value on any failure (git missing, worktree deleted mid-poll, a file vanishing
between ``ls-files`` and ``stat``). The Map is advisory UI and the push gate
fails open on the same terms as every other git probe in ``core``; a traceback
in a 2-second poll would only turn a transient git hiccup into a broken tab.

The graph is deliberately a heuristic (regexes, not parsers): it resolves
Python, JS/TS, CSS, Go, Java/Kotlin, Rust, C/C++ and Terraform imports (plus
"API edges" from frontend route literals to backend route declarations) well
enough to layer directories and rank what they export, and a file the
resolvers get wrong costs one missing or extra relation, never correctness.
The same one-read-per-file scan also yields the imported NAMES per edge and
each file's entry points (``graph_detail``) — the facts the Atlas
(``code_outline``) builds on.

The server is imported lazily (:func:`_server`) — it imports this module.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


_LOCK = threading.RLock()

# O_NONBLOCK: opening a FIFO read-only otherwise waits for a writer, forever.
# O_NOCTTY: opening a tty must never make it the server's controlling terminal.
_O_READ_NB = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)


def _open_regular(path: str) -> Optional[IO[bytes]]:
    """``open(path, "rb")`` for a REGULAR file only; None for anything else
    (FIFO, socket, device, directory). Raises ``OSError`` like ``open``.

    WHY. Every path this module reads comes out of an agent-writable place —
    the worktree, a feed record's ``tp``, the feed dir itself — and git lists
    symlinks, so ``lnk.js -> some.fifo`` or ``x.py -> /dev/tty`` is a normal
    ``ls-files`` row. A plain ``open()``/``read()`` on one blocks with no
    timeout (the graph budget is only checked BETWEEN files), parking one
    thread-pool worker per Map fetch until every ``to_thread`` route and the
    monitor loop stall. So: ``stat`` first (a device is never even opened —
    some have open side effects), then open non-blocking (a FIFO swapped in
    after the stat can't block the open) and re-check the fd actually opened
    with ``fstat``."""
    if not stat.S_ISREG(os.stat(path).st_mode):
        return None
    fd = os.open(path, _O_READ_NB)
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


# --------------------------------------------------------------------------- #
# Tool-feed location
# --------------------------------------------------------------------------- #


def _feed_dir() -> str:
    """The tool-feed directory — ``red_zones.feed_dir()``, the one definition
    the hook installer and the fire-time hook share (``$MINDFLOCK_TOOL_FEED_DIR``
    else ``~/.mindflock-assistant/.tool-feed``, resolved at call time so tests
    can redirect it)."""
    from backend.config import red_zones

    return red_zones.feed_dir()


def _feed_path(tmux_name: str) -> str:
    """``red_zones.feed_path(tmux_name)`` — sanitized byte-for-byte like the
    hook's own, so the file the hook appends to is the file we read."""
    from backend.config import red_zones

    return red_zones.feed_path(tmux_name or "")


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


def _git(wt: str, *args: str, timeout: float = 60) -> Optional[bytes]:
    """stdout of ``git -C wt <args>``, or None on any failure (non-zero exit,
    timeout, git missing). Callers treat None as "unknown", never as empty."""
    try:
        cp = subprocess.run(
            ["git", "-C", wt, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout


def _rev(rev: Any) -> Optional[str]:
    """``rev`` made safe to put on a ``git diff`` command line, or None when
    it can't be (empty, not a string, or ``-``-leading, which git would parse
    as an OPTION). ``origin/<x>`` is spelled as its full ref
    ``refs/remotes/origin/<x>``.

    WHY FULL REFS. The push/PR/merge gate reads ``diff <fork> <target>``, so
    every revision on that line must mean exactly one object. A short name
    resolves through git's DWIM order — ``refs/tags/`` and ``refs/heads/``
    BEFORE ``refs/remotes/`` — so a tag or local branch the agent names
    ``origin/feat``, pointed at the fork, would quietly empty the committed
    diff and wave the push through. ``HEAD`` (``$GIT_DIR/HEAD`` wins first)
    and full hex shas (taken as object names) need no rewrite. The other half
    is the ``--`` every caller puts after its revisions: without it a FILE
    named ``HEAD`` / ``<fork-sha>`` / ``origin/<branch>`` makes git die with
    "ambiguous argument", which reads as "no changes"."""
    if not isinstance(rev, str):
        return None
    rev = rev.strip()
    if not rev or rev.startswith("-"):
        return None
    if rev.startswith("origin/") and len(rev) > len("origin/"):
        return "refs/remotes/" + rev
    return rev


def _z_fields(out: bytes) -> List[str]:
    """NUL-separated git output as text. Paths that aren't valid UTF-8 are
    replaced rather than surrogate-escaped: the result goes straight into a
    JSON response, and a lone surrogate would fail the encoder there."""
    return [f.decode("utf-8", "replace") for f in out.split(b"\0") if f]


def _wt_fingerprint(srv: Any, wt: str, fork: str) -> Optional[str]:
    """``srv._worktree_fingerprint`` when the server re-exports it (so a test
    that monkeypatches the server name is honoured), else the definition in
    ``core.snapshot`` — the server re-imports ``_DIFF_STAT_CACHE`` and
    ``_session_fork_point`` from there, but not this one."""
    fn = getattr(srv, "_worktree_fingerprint", None)
    if fn is None:
        from backend.web.core import snapshot

        fn = snapshot._worktree_fingerprint
    return fn(wt, fork)


def fingerprint(inst: Any, wt: str) -> Optional[str]:
    """The worktree-state fingerprint every Map cache is keyed on.

    Reuses the header diff stat's entry in ``_DIFF_STAT_CACHE`` while it is
    fresh (its ``[2]`` is exactly this fingerprint), so a Map poll right after
    a sidebar poll costs nothing; otherwise computes it the same way
    (``_worktree_fingerprint`` against the session's fork point). ``None`` =
    can't fingerprint (git trouble / pathological dirty set) — callers then
    fall back to short TTL caching."""
    try:
        srv = _server()
        cached = srv._DIFF_STAT_CACHE.get(wt)
        if cached and len(cached) > 2 and cached[2] and cached[0] > time.time():
            return cached[2]
        fork = srv._session_fork_point(inst, wt)
        return _wt_fingerprint(srv, wt, fork)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# File list
# --------------------------------------------------------------------------- #

FLAG_IGNORED = 1  # a git-ignored file shown because a red zone matches it
FLAG_TEST = 2  # a test file (folded into "+N tests" on blast arms)
MAX_FILES = 40000
_UNCACHEABLE_TTL = 3.0
_LIST_CACHE: Dict[tuple, tuple] = {}  # key -> (expires|None, rows, truncated)
_LIST_CACHE_MAX = 8

_TEST_SEGMENTS = frozenset({"test", "tests", "__tests__", "spec"})
# A whole path segment that names a test tree: test/tests/testsv2/test-utils,
# integration_tests/e2e-tests, __tests__, testdata, fixtures, spec. Matched on
# the lowercased segment.
_TEST_SEGMENT_RE = re.compile(
    r"^(?:tests?[\w-]*|[\w-]*[_-]tests?|__tests__|__mocks__|testdata|fixtures|spec)$"
)
# Per-language test FILE names, case-sensitive where the convention is
# (``FooTest.java`` yes, ``Latest.java`` no).
_TEST_FILE_RE = re.compile(
    r"(?:_test\.(?:go|rs|c|cc|cpp|rb)|_spec\.rb"
    r"|[A-Za-z0-9]Tests?\.(?:java|kt|cs|scala|groovy)|^Tests?\.(?:java|kt|cs)"
    r"|[A-Za-z0-9]Spec\.(?:kt|scala|groovy)"
    r"|^test_[^/]*\.(?:c|cc|cpp))$"
)


def _is_test_path(rel: str) -> bool:
    """Whether ``rel`` looks like a test: any path segment that names a test
    tree (test/tests/testsv2/*_tests/__tests__/testdata/fixtures/spec, see
    :data:`_TEST_SEGMENT_RE`), or a per-language test file name —
    ``test_*.py`` / ``*_test.py`` / ``*.test.*`` / ``*.spec.*`` /
    ``*_test.go`` / ``FooTest(s).java|kt|cs`` / ``FooSpec.kt`` /
    ``*_test.rs`` / ``test_*.c``. Segments are case-insensitive, because
    ``Tests/`` is as much a test dir as ``tests/``.

    WHY SO BROAD. The Atlas keeps tests OUT of the dependency layering (a
    test imports everything, so one unrecognised test folder lands in the top
    tier and claims to "use" the whole repo). A false positive costs less: a
    test-named file that non-test code imports is reclassified as code by
    ``code_outline``."""
    parts = rel.lower().split("/")
    if any(seg in _TEST_SEGMENTS for seg in parts):
        return True
    if any(_TEST_SEGMENT_RE.match(seg) for seg in parts):
        return True
    name = parts[-1]
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return True
    if _TEST_FILE_RE.search(rel.rsplit("/", 1)[-1]):
        return True
    stem = name.split(".", 1)
    if len(stem) == 2 and stem[0]:
        rest = "." + stem[1]
        for marker in (".test.", ".spec."):
            idx = rest.find(marker)
            # needs something after the marker: "a.test.ts", not "a.test."
            if idx >= 0 and len(rest) > idx + len(marker):
                return True
    return False


def effective_tests(flagged: Iterable[Any], edges: Iterable[Any]) -> Set[Any]:
    """THE test predicate every consumer shares (ADDENDUM A.2): the
    name-flagged files (:func:`_is_test_path`, deliberately broad) MINUS any
    that a non-test file imports — ``test_plans.py`` imported by
    ``server.py``, ``web/testimonials/card.py`` imported by ``web/main.py``
    are code. ``edges`` are ``(src, dst)`` pairs in the same key space as
    ``flagged`` (rel strings or row indices)."""
    fl = set(flagged)
    imported_by_code = set()
    for e in edges:
        try:
            s, t = e[0], e[1]
        except (TypeError, IndexError):
            continue
        if s not in fl:
            imported_by_code.add(t)
    return fl - imported_by_code


def _clean_rel(p: str) -> str:
    p = (p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def list_files(
    wt: str, extra_ignored: Iterable[str] = (), fp: Optional[str] = None
) -> Tuple[List[list], bool]:
    """``(rows, truncated)`` — every file the Map draws, as ``[rel, size,
    flags]`` sorted by path.

    Source: ``git ls-files --cached --others --exclude-standard`` (tracked +
    untracked-not-ignored, i.e. what the agent can see and git will carry),
    plus ``extra_ignored`` — the git-ignored files a red zone matches, flagged
    :data:`FLAG_IGNORED` (the "my configuration file" case: usually ignored,
    exactly what the user wants to see guarded). Tracked-but-deleted files and
    submodule gitlinks are dropped by the stat pass.

    Capped at :data:`MAX_FILES`; the zone-matched ignored files are kept first
    (they are why the user opened the Map) and the git listing fills the rest
    in path order. Cached on ``(wt, fp, extras)``; ``fp=None`` (unknown
    fingerprint) caches for :data:`_UNCACHEABLE_TTL` seconds only. The rows
    are shared with the cache — callers must not mutate them."""
    try:
        extras = tuple(sorted({c for c in (_clean_rel(p) for p in extra_ignored) if c}))
        key = (wt, fp, extras)
        now = time.time()
        with _LOCK:
            hit = _LIST_CACHE.get(key)
            if hit is not None and (hit[0] is None or hit[0] > now):
                return list(hit[1]), hit[2]
        out = _git(wt, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if out is None:
            return [], False
        listed = sorted(set(_z_fields(out)))
        extra_set = set(extras)
        cap = MAX_FILES
        rows: List[list] = []
        seen: Set[str] = set()
        truncated = False
        for rel, flag in [(r, FLAG_IGNORED) for r in extras] + [
            (r, 0) for r in listed if r not in extra_set
        ]:
            if rel in seen:
                continue
            if len(rows) >= cap:
                truncated = True
                break
            try:
                st = os.lstat(os.path.join(wt, rel))
            except OSError:
                continue  # vanished (deleted in the working tree) or unreadable
            if (st.st_mode & 0o170000) == 0o040000:
                continue  # a directory: submodule gitlink
            seen.add(rel)
            if _is_test_path(rel):
                flag |= FLAG_TEST
            rows.append([rel, int(st.st_size), flag])
        rows.sort(key=lambda r: r[0])
        with _LOCK:
            _LIST_CACHE[key] = (
                None if fp is not None else now + _UNCACHEABLE_TTL,
                rows,
                truncated,
            )
            while len(_LIST_CACHE) > _LIST_CACHE_MAX:
                _LIST_CACHE.pop(next(iter(_LIST_CACHE)))
        return list(rows), truncated
    except Exception:  # noqa: BLE001
        return [], False


# --------------------------------------------------------------------------- #
# Change set
# --------------------------------------------------------------------------- #

_CHANGED_CACHE: Dict[str, tuple] = {}  # wt -> (fp|None, expires|None, result)


def _parse_numstat_z(out: bytes) -> Dict[str, Tuple[int, int]]:
    """``{path: (added, removed)}`` from ``diff --numstat -z --no-renames``.
    ``-z`` terminates each RECORD with NUL (``adds\\tdels\\tpath\\0``);
    binary files report ``-\\t-`` and count as 0/0."""
    res: Dict[str, Tuple[int, int]] = {}
    for rec in _z_fields(out):
        bits = rec.split("\t", 2)
        if len(bits) < 3 or not bits[2]:
            continue
        try:
            res[bits[2]] = (int(bits[0]), int(bits[1]))
        except ValueError:
            res[bits[2]] = (0, 0)
    return res


def _parse_name_status_z(out: bytes) -> Dict[str, str]:
    """``{path: status}`` from ``diff --name-status -z --no-renames``: a
    status field then a path field, each NUL-terminated (no rename pairs —
    ``--no-renames`` guarantees it)."""
    fields = _z_fields(out)
    res: Dict[str, str] = {}
    i = 0
    while i + 1 < len(fields):
        status, path = fields[i], fields[i + 1]
        i += 2
        res[path] = (status[:1] or "M").upper()
    return res


def changed_files(inst: Any, wt: str) -> List[dict]:
    """``[{"path", "status", "added", "removed"}]`` — the session's total
    change vs its fork point, sorted by path.

    The Diff-tab convention exactly: ``git add -N .`` (so new files count),
    then ``--numstat`` + ``--name-status`` against
    ``srv._session_fork_point`` with ``-z --no-renames`` (a moved file is a
    delete plus an add — both halves land on the map, and no path arrives
    quoted or as an ``a => b`` pseudo-path). Status is git's letter (A/M/D/T/U).

    Cached against the fingerprint taken BEFORE anything is read (before
    ``add -N`` and both diffs). A key older than the data can only cost one
    extra recompute (intent-to-add turns ``??`` into ``A``, so the next probe
    misses once, then settles); a key NEWER than the data — a fingerprint
    taken after the diffs — would pin a write that landed between the diff
    and the fingerprint as "already seen", hiding a red-zone edit until some
    other file moved. Unknown fingerprint → 3 s TTL.

    Every revision goes through :func:`_rev` and is followed by ``--``, so a
    file named after the fork sha can't make git fail (and the map blank)."""
    try:
        srv = _server()
        now = time.time()
        fp = fingerprint(inst, wt)
        with _LOCK:
            hit = _CHANGED_CACHE.get(wt)
        if hit is not None:
            if fp is not None and hit[0] == fp:
                return [dict(r) for r in hit[2]]
            if hit[1] is not None and hit[1] > now:
                return [dict(r) for r in hit[2]]
        fork = _rev(srv._session_fork_point(inst, wt))
        if fork is None:
            return []
        _git(wt, "add", "-N", "--", ".")
        num = _git(wt, "diff", "--numstat", "-z", "--no-renames", fork, "--")
        ns = _git(wt, "diff", "--name-status", "-z", "--no-renames", fork, "--")
        if num is None or ns is None:
            return []
        counts = _parse_numstat_z(num)
        statuses = _parse_name_status_z(ns)
        result = []
        for path in sorted(set(counts) | set(statuses)):
            added, removed = counts.get(path, (0, 0))
            result.append(
                {
                    "path": path,
                    "status": statuses.get(path, "M"),
                    "added": added,
                    "removed": removed,
                }
            )
        with _LOCK:
            _CHANGED_CACHE[wt] = (
                fp,
                None if fp is not None else now + _UNCACHEABLE_TTL,
                result,
            )
            while len(_CHANGED_CACHE) > 64:
                _CHANGED_CACHE.pop(next(iter(_CHANGED_CACHE)))
        return [dict(r) for r in result]
    except Exception:  # noqa: BLE001
        return []


def committed_changed(inst: Any, wt: str, target: str = "HEAD") -> List[str]:
    """Paths that differ between the session's fork point and ``target``
    (``HEAD`` for a push; ``origin/<branch>`` for a PR/merge) — the COMMITTED
    change set, which is what actually leaves the machine. The push/PR/merge
    gate intersects this with the red zones.

    Why not :func:`changed_files`: a change committed and then reverted in the
    working tree is invisible to a fork→worktree diff but still ships with the
    push. Not cached (the gate runs once per request). ``[]`` on git failure —
    the gate fails open like every other git probe.

    Both revisions go through :func:`_rev` (``origin/<b>`` → the full
    remote-tracking ref, so a same-named tag/branch can't stand in for it)
    and end with ``--`` (so a decoy FILE named ``HEAD`` / ``<fork-sha>`` /
    ``origin/<b>`` can't make git bail out "ambiguous" — which this function
    would otherwise report as an empty, gate-passing change set)."""
    try:
        fork = _rev(_server()._session_fork_point(inst, wt))
        tgt = _rev(target)
        if fork is None or tgt is None:
            return []
        out = _git(wt, "diff", "--name-only", "-z", "--no-renames", fork, tgt, "--")
        if out is None:
            return []
        return sorted(set(_z_fields(out)))
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# Import graph
# --------------------------------------------------------------------------- #

GRAPH_BUDGET_S = 6.0
_MAX_SCAN_BYTES = 2 * 1024 * 1024
_SCAN_MEMO: Dict[str, tuple] = {}  # abs -> (mtime_ns, size, specs)
_SCAN_MEMO_MAX = 200_000
_GRAPH_CACHE: Dict[tuple, dict] = {}
_GRAPH_CACHE_MAX = 8
_JSON_MEMO: Dict[str, tuple] = {}  # abs -> (mtime_ns, size, parsed|None)

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
_JS_PROBE_EXTS = _JS_EXTS + (".json", ".css")
_CSS_EXTS = (".css", ".scss", ".sass", ".less")
_LANG_OF = {".py": "py"}
_LANG_OF.update({e: "js" for e in _JS_EXTS})
_LANG_OF.update({e: "css" for e in _CSS_EXTS})
# v3 resolvers: Go (go.mod module path), Java/Kotlin (FQN imports + same
# package), Rust (mod/use crate::), C/C++ (#include), HCL/Terraform (module
# source dirs). A language here is SCANNED (read + memoized); resolution is
# per-language in :func:`_build_graph`.
_LANG_OF.update({".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin"})
_LANG_OF.update({".rs": "rust", ".tf": "hcl", ".hcl": "hcl"})
_LANG_OF.update(
    {e: "c" for e in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx")}
)
_C_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx")
_JVM_LANGS = ("java", "kotlin")

# A dotted-name list (``a.b as c, d``) that runs to end of line / comment /
# ``;`` / a continuation — so docstring prose like "import core/engine
# helpers" or "import the data" never reads as an import.
_PY_NAMES = r"[A-Za-z_][\w.]*(?:[ \t]+as[ \t]+\w+)?"
_PY_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+("
    + _PY_NAMES
    + r"(?:[ \t]*,[ \t]*"
    + _PY_NAMES
    + r")*)[ \t]*(?=#|;|\\|$)",
    re.M,
)
_PY_FROM_RE = re.compile(
    r"^[ \t]*from[ \t]+(\.*)[ \t]*([\w.]*)[ \t]+import[ \t]+"
    r"(\([^)]*\)|\*|[A-Za-z_][\w \t,]*?)[ \t]*(?=#|;|\\|$)",
    re.M,
)
# `(?<![\w.$])` keeps `obj.import(...)` / `foo_import` from matching. The span
# between the keyword and `from` is TEMPERED: it may not cross another
# import/export keyword. Untempered, every keyword scanned ahead to the next
# quote/paren/semicolon — in a semicolon-free file of quote-free exports
# (generated constants, `interface` blocks) that is quadratic: ~7 s at 200 KB,
# minutes at the 2 MB scan cap, all inside ONE file's scan (the graph budget
# is only checked between files) and inside `_sre`, which holds the GIL, so
# `to_thread` didn't even keep the event loop alive. Tempered, each keyword's
# scan stops at the next statement and the whole pass is linear.
_JS_FROM_RE = re.compile(
    r"""(?<![\w.$])(?:import|export)\s+(?:type\s+)?"""
    r"""(?:(?!(?<![\w.$])(?:import|export)\b)[^'"`;()])*?"""
    r"""\bfrom\s*(['"])([^'"\n]+)\1"""
)
_JS_BARE_RE = re.compile(r"""(?<![\w.$])import\s*(['"])([^'"\n]+)\1""")
_JS_CALL_RE = re.compile(
    r"""(?<![\w.$])(?:require|import)\s*\(\s*(['"])([^'"\n]+)\1\s*\)"""
)
_CSS_RE = re.compile(r"""@(?:import|use|forward)\s+(?:url\(\s*)?(['"])([^'"\n]+)\1""")


def _ext(rel: str) -> str:
    return posixpath.splitext(rel)[1].lower()


def _row_rel(f: Any) -> str:
    """A ``files`` entry is a ``[rel, size, flags]`` row or a bare rel."""
    if isinstance(f, str):
        return f
    return str(f[0])


def _read_head(path: str) -> Optional[str]:
    """The first :data:`_MAX_SCAN_BYTES` of a REGULAR file as text; None for
    anything else (see :func:`_open_regular` — a symlinked FIFO must not
    hang the build)."""
    fh = _open_regular(path)
    if fh is None:
        return None
    with fh:
        data = fh.read(_MAX_SCAN_BYTES)
    return data.decode("utf-8", "replace")


def _py_specs(text: str) -> List[tuple]:
    """Raw Python import statements: ``("imp", dotted, alias)`` for ``import
    a.b [as c]`` (alias = the local name: ``c``, else ``a.b`` itself), and
    ``("from", level, module, names, aliases)`` for ``from ..m import x, y as
    z`` (``aliases`` parallel to ``names``: the local name each is bound to).
    The aliases are what :func:`_py_attr_uses` looks for (``alias.attr``)."""
    specs: List[tuple] = []
    for m in _PY_IMPORT_RE.finditer(text):
        for part in m.group(1).split(","):
            name = part.strip().split()
            if name and re.match(r"^[A-Za-z_][\w.]*$", name[0]):
                dotted = name[0].strip(".")
                alias = name[2] if len(name) >= 3 and name[1] == "as" else dotted
                specs.append(("imp", dotted, alias))
    for m in _PY_FROM_RE.finditer(text):
        level = len(m.group(1))
        module = m.group(2).strip(".")
        if not level and not module:
            continue
        body = re.sub(r"#[^\n]*", "", m.group(3)).strip().strip("()\\")
        names = []
        aliases = []
        for part in re.split(r"[,\n]", body):
            bits = part.strip().split()
            if bits and bits[0] != "*" and re.match(r"^[A-Za-z_]\w*$", bits[0]):
                names.append(bits[0])
                aliases.append(
                    bits[2] if len(bits) >= 3 and bits[1] == "as" else bits[0]
                )
        specs.append(("from", level, module, tuple(names), tuple(aliases)))
    return specs


def _js_specs(text: str) -> List[str]:
    out: List[str] = []
    for rx in (_JS_FROM_RE, _JS_BARE_RE, _JS_CALL_RE):
        # Bundler query/hash suffixes (`./x.json?raw`, `./a.svg#icon`) name
        # the same file.
        out.extend(
            re.split(r"[?#]", m.group(2).strip(), 1)[0] for m in rx.finditer(text)
        )
    return list(dict.fromkeys(s for s in out if s))


def _css_specs(text: str) -> List[str]:
    return list(dict.fromkeys(m.group(2).strip() for m in _CSS_RE.finditer(text)))


# --------------------------------------------------------------------------- #
# Per-file scan records (v3: names, entry points, per-language facts)
# --------------------------------------------------------------------------- #
#
# One READ per file per (mtime, size) produces a small record: the import
# specifiers every resolver needs plus the facts that give edges their NAMES
# (``alias.attr`` uses, JS named imports, Go/Java/C/HCL declarations), the
# file's entry points (routes, CLI commands, mains — the "swagger" lens and
# the server half of API edges) and, for JS/TS, the ``"/api/…"`` string
# literals (the client half). Everything is regex over text: linear, bounded
# spans, never a parser — a file the heuristics misread costs one missing or
# extra relation, never an exception (each scanner is wrapped).

# A dotted chain `a.b.c` not preceded by a word char or dot (`self.x.y` has
# head `self`). Heads are bucketed so alias lookups never rescan the text.
_CHAIN_RE = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)")
_MAX_USES = 200
# A chain ending in one of these, not called or indexed, is a FILE NAME in a
# comment or docstring (``server.py``), not an attribute read off an alias.
_FILE_EXT_WORDS = frozenset(
    "py pyi js mjs cjs ts tsx jsx json md toml yaml yml txt sh go rs java kt "
    "c h cc cpp hpp css scss html lock cfg ini".split()
)


def _attr_uses(text: str, aliases: Iterable[str]) -> Dict[str, tuple]:
    """``{alias: (attr, …)}`` — what the file reads off each import alias
    (``alias.attr``; a dotted alias like ``a.b`` matches ``a.b.attr``)."""
    al = {a for a in aliases if a}
    if not al:
        return {}
    heads = {a.split(".", 1)[0] for a in al}
    by_head: Dict[str, Set[str]] = {}
    for m in _CHAIN_RE.finditer(text):
        c = m.group(1)
        h = c.split(".", 1)[0]
        if h in heads:
            head, _dot, last = c.rpartition(".")
            nxt = text[m.end() : m.end() + 1]
            if last in _FILE_EXT_WORDS and not (nxt and nxt in "(["):
                if "." not in head:
                    continue
                c = head
            by_head.setdefault(h, set()).add(c)
    out: Dict[str, tuple] = {}
    for a in al:
        pre = a + "."
        got = {
            c[len(pre) :].split(".", 1)[0]
            for c in by_head.get(a.split(".", 1)[0], ())
            if c.startswith(pre)
        }
        if got:
            out[a] = tuple(sorted(got)[:_MAX_USES])
    return out


_TLS = threading.local()
# Per-file caps on the entry-point passes: a pathological file (thousands of
# decorator-looking lines) must cost a bounded amount, never quadratic time.
_MAX_ENTRY_MATCHES = 4000


def _line_of(text: str, pos: int) -> int:
    """1-based line of ``pos``. The newline offsets are built once per text
    (per thread) and bisected — ``text.count`` per call is quadratic over a
    file with thousands of entry points."""
    c = getattr(_TLS, "nl", None)
    if c is None or c[0] is not text:
        c = (text, [m.start() for m in re.finditer("\n", text)])
        _TLS.nl = c
    return bisect.bisect_left(c[1], pos) + 1


def _next_match(matches: List["re.Match[str]"], starts: List[int], pos: int):
    """The first of ``matches`` (pre-collected, with their ``starts``) at or
    after ``pos`` — a bisect instead of a fresh ``search`` per decorator,
    which scans to the end of the file every time nothing follows."""
    i = bisect.bisect_left(starts, pos)
    return matches[i] if i < len(matches) else None


def _paren_blocks(text: str, head: "re.Pattern[str]") -> List[str]:
    """Bodies of ``head ( … )`` blocks (Go ``import (…)`` / ``const (…)``).
    Found by ``str.find`` so an unclosed block ends the scan instead of
    making every later ``(`` rescan the rest of the file."""
    out = []
    for m in head.finditer(text):
        end = text.find(")", m.end())
        if end < 0:
            break
        out.append(text[m.end() : end])
    return out


def _entry(kind: str, method: str, route: str, line: int, handler: str) -> dict:
    return {
        "kind": kind,
        "method": method,
        "route": route,
        "line": line,
        "handler": handler,
    }


def _call_args(text: str, start: int, cap: int = 800) -> str:
    """The text inside the parentheses that open at ``text[start]`` (``(``),
    up to the matching close paren — strings skipped, ``cap`` chars max."""
    if start >= len(text) or text[start] != "(":
        return ""
    depth = 0
    i = start
    end = min(len(text), start + cap)
    q = ""
    while i < end:
        c = text[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = ""
        elif c in "\"'`":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    return text[start + 1 : end]


_FIRST_STR_RE = re.compile(r"""^\s*[rbuRBUfF]{0,2}(["'`])((?:\\.|(?!\1).)*)\1""", re.S)


def _first_str(args: str) -> Optional[str]:
    m = _FIRST_STR_RE.match(args)
    return m.group(2) if m else None


# --- Python ---------------------------------------------------------------- #

_PY_ROUTER_RE = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)[ \t]*(?::[^=\n]*)?=[ \t]*(?:[\w.]+\.)?"
    r"(APIRouter|Blueprint|Router)[ \t]*(?=\()",
    re.M,
)
_PY_DECO_RE = re.compile(
    r"^[ \t]*@[ \t]*((?:[A-Za-z_]\w*\.)*)([A-Za-z_]\w*)\."
    r"(get|post|put|patch|delete|head|options|route|websocket|api_route|command"
    r"|event|action|shortcut|view|message|task)\b[ \t]*(\()?",
    re.M,
)
_PY_SHARED_TASK_RE = re.compile(r"^[ \t]*@[ \t]*(?:[\w.]+\.)?shared_task\b", re.M)
_PY_DEF_AFTER_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_]\w*)", re.M)
_PY_MAIN_RE = re.compile(
    r"""^if[ \t]+(?:__name__[ \t]*==[ \t]*(["'])__main__\1|(["'])__main__\2[ \t]*==[ \t]*__name__)""",
    re.M,
)
_PY_ADD_PARSER_RE = re.compile(r"""\.add_parser\(\s*[rbu]?(["'])([^"'\n]+)\1""")
_DJANGO_PATH_RE = re.compile(
    r"""(?<![\w.])(?:re_)?path\(\s*r?(["'])([^"'\n]*)\1\s*,\s*([\w.]+)"""
)
_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
_KW_STR_RE = r"""\b%s\s*=\s*[rbu]?(["'])([^"'\n]*)\1"""


def _slackish(text: str, dm: Optional["re.Match[str]"]) -> bool:
    """Whether the decorated ``def`` takes Slack Bolt's listener arguments
    (``ack`` / ``say`` / ``respond``) — tells ``@app.command(SCAN_CMD)``
    (a Slack slash command named by a constant) from a click/typer
    command."""
    if dm is None:
        return False
    params = text[dm.end() : dm.end() + 400].split(")", 1)[0]
    return bool(re.search(r"\b(?:ack|say|respond)\b", params))


def _py_entries(text: str, rel: str) -> List[dict]:
    """Routes (FastAPI/Flask/Starlette decorators, with same-file
    ``APIRouter(prefix=)`` / ``Blueprint(url_prefix=)``; Flask ``methods=[…]``;
    Django ``path()`` in urls.py), CLI commands (click/typer ``.command``,
    argparse ``add_parser``), events (Slack ``@app.command("/x")`` /
    ``.event/.action/.shortcut/.view/.message``, Celery tasks) and
    ``if __name__ == "__main__"``. Decorators are found ANYWHERE in the file
    — routers built inside functions are as real as top-level ones."""
    out: List[dict] = []
    prefixes: Dict[str, str] = {}
    for k, m in enumerate(_PY_ROUTER_RE.finditer(text)):
        if k >= _MAX_ENTRY_MATCHES:
            break
        args = _call_args(text, m.end(), 400)
        pm = re.search(_KW_STR_RE % "(?:url_)?prefix", args)
        if pm:
            prefixes[m.group(1)] = pm.group(2)
    defs = list(_PY_DEF_AFTER_RE.finditer(text))
    def_starts = [d.start() for d in defs]
    for k, m in enumerate(_PY_DECO_RE.finditer(text)):
        if k >= _MAX_ENTRY_MATCHES:
            break
        owner, verb = m.group(2), m.group(3)
        args = _call_args(text, m.end() - 1, 400) if m.group(4) else ""
        first = _first_str(args)
        dm = _next_match(defs, def_starts, m.end())
        if dm and dm.start() - m.end() > 2000:
            dm = None  # a decorated class / something far away: no handler
        handler = dm.group(1) if dm else ""
        line = _line_of(text, dm.start(1)) if dm else _line_of(text, m.start())
        if verb in _HTTP_VERBS or verb in ("route", "api_route", "websocket"):
            if first is None or (first and not first.startswith("/")):
                continue
            meth = verb.upper()
            if verb in ("route", "api_route"):
                meth = "ANY"
                mm = re.search(r"\bmethods\s*=\s*[\[(]([^\])]*)[\])]", args)
                if mm:
                    ms = re.findall(r"""["'](\w+)["']""", mm.group(1))
                    if ms:
                        meth = "/".join(x.upper() for x in ms)
            elif verb == "websocket":
                meth = "WS"
            route = prefixes.get(owner, "") + first
            out.append(_entry("http", meth, route or "/", line, handler))
        elif verb == "command":
            if (first and first.startswith("/")) or (
                first is None and _slackish(text, dm)
            ):
                what = first or args.strip().split(",")[0].strip() or handler
                out.append(_entry("event", "SLACK", what, line, handler))
            else:
                name = first
                if not name:
                    nm = re.search(_KW_STR_RE % "name", args)
                    name = nm.group(2) if nm else handler
                out.append(_entry("cli", "CLI", name or handler, line, handler))
        elif verb == "task":
            out.append(_entry("event", "TASK", handler, line, handler))
        elif first or (args.strip() and _slackish(text, dm)):
            what = first or args.strip().split(",")[0].strip()
            out.append(_entry("event", verb.upper(), what, line, handler))
    for m in _PY_SHARED_TASK_RE.finditer(text):
        dm = _next_match(defs, def_starts, m.end())
        if dm:
            out.append(
                _entry(
                    "event",
                    "TASK",
                    dm.group(1),
                    _line_of(text, dm.start(1)),
                    dm.group(1),
                )
            )
    for m in _PY_ADD_PARSER_RE.finditer(text):
        out.append(_entry("cli", "CLI", m.group(2), _line_of(text, m.start()), ""))
    if rel.rsplit("/", 1)[-1] == "urls.py":
        for m in _DJANGO_PATH_RE.finditer(text):
            out.append(
                _entry(
                    "http",
                    "ANY",
                    "/" + m.group(2).lstrip("^/"),
                    _line_of(text, m.start()),
                    m.group(3),
                )
            )
    for m in _PY_MAIN_RE.finditer(text):
        out.append(_entry("main", "MAIN", "__main__", _line_of(text, m.start()), ""))
    return out


def _scan_py(text: str, rel: str) -> dict:
    specs = _py_specs(text)
    aliases: List[str] = []
    for sp in specs:
        if sp[0] == "imp":
            aliases.append(sp[2])
        else:
            aliases.extend(sp[4])
    return {
        "specs": specs,
        "uses": _attr_uses(text, aliases),
        "entry": _py_entries(text, rel),
    }


# --- JS / TS ---------------------------------------------------------------- #

# Bounded spans only (`{…}` ≤ 4000 chars): see the _JS_FROM_RE note on why an
# unbounded lazy span here would be quadratic on a pathological file.
_JS_IMPORT_NAMES_RE = re.compile(
    r"""(?<![\w.$])import\s+(?:type\s+)?(?:([A-Za-z_$][\w$]*)\s*,?\s*)?"""
    r"""(?:\{([^{}]{0,4000})\}\s*)?(?:\*\s*as\s+([A-Za-z_$][\w$]*)\s*)?"""
    r"""from\s*(['"])([^'"\n]+)\4"""
)
_JS_REEXPORT_NAMES_RE = re.compile(
    r"""(?<![\w.$])export\s+(?:type\s+)?(?:\{([^{}]{0,4000})\}|\*(?:\s+as\s+[\w$]+)?)"""
    r"""\s*from\s*(['"])([^'"\n]+)\2"""
)
_JS_DEFAULT_RE = re.compile(
    r"""^export\s+default\s+(?:(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)"""
    r"""|(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s*;?\s*$)""",
    re.M,
)
_JS_ROUTE_RE = re.compile(
    r"""(?<![\w$.])(app|router|server|fastify|[A-Za-z_$][\w$]*(?:Router|router|App))"""
    r"""\.(get|post|put|patch|delete|all|head|options)\(\s*(['"`])(/[^'"`\n]*)\3"""
)
_NEXT_ROUTE_FILE_RE = re.compile(r"(?:^|/)app/(.*/)?route\.(?:ts|js|tsx|jsx|mjs)$")
_NEXT_HANDLER_RE = re.compile(
    r"^export\s+(?:(?:async\s+)?function\s+|const\s+)"
    r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b",
    re.M,
)
_JS_LIT_RE = re.compile(
    r"""['"`](/(?:[A-Za-z0-9_\-./:]|\$\{[^}\n]{0,80}\}){2,160})['"`?]"""
)
_MAX_LITS = 500


def _js_names_list(body: str) -> List[Tuple[str, str]]:
    """``[(exported, local)]`` from a ``{ a, b as c, type T }`` body."""
    out = []
    for part in body.split(","):
        bits = part.strip().split()
        if bits and bits[0] == "type" and len(bits) > 1:
            bits = bits[1:]
        if not bits or not re.match(r"^[A-Za-z_$][\w$]*$", bits[0]):
            continue
        local = bits[2] if len(bits) >= 3 and bits[1] == "as" else bits[0]
        out.append((bits[0], local))
    return out


def _js_spec_key(spec: str) -> str:
    return re.split(r"[?#]", spec.strip(), 1)[0]


def _js_lits(text: str) -> List[str]:
    """Normalized ``"/api/…"``-style path literals: ``${…}`` → ``x``, query
    dropped, no trailing slash, ≥ 2 segments."""
    out: Dict[str, None] = {}
    for m in _JS_LIT_RE.finditer(text):
        lit = re.sub(r"\$\{[^}]*\}", "x", m.group(1)).split("?", 1)[0].rstrip("/")
        if lit.count("/") < 2 or len(lit) < 5 or "//" in lit:
            continue
        out[lit] = None
        if len(out) >= _MAX_LITS:
            break
    return list(out)


def _scan_js(text: str, rel: str) -> dict:
    names: Dict[str, List[str]] = {}
    ns: Dict[str, List[str]] = {}
    for m in _JS_IMPORT_NAMES_RE.finditer(text):
        key = _js_spec_key(m.group(5))
        got = names.setdefault(key, [])
        if m.group(1):
            got.append("default")
        if m.group(2):
            got.extend(e for e, _l in _js_names_list(m.group(2)))
        if m.group(3):
            ns.setdefault(key, []).append(m.group(3))
    for m in _JS_REEXPORT_NAMES_RE.finditer(text):
        key = _js_spec_key(m.group(3))
        got = names.setdefault(key, [])
        if m.group(1):
            got.extend(e for e, _l in _js_names_list(m.group(1)))
    default = None
    dm = _JS_DEFAULT_RE.search(text)
    if dm:
        default = dm.group(1) or dm.group(2) or dm.group(3)
    entry: List[dict] = []
    for m in _JS_ROUTE_RE.finditer(text):
        entry.append(
            _entry(
                "http", m.group(2).upper(), m.group(4), _line_of(text, m.start()), ""
            )
        )
    nm = _NEXT_ROUTE_FILE_RE.search(rel)
    if nm:
        segs = [
            s
            for s in (nm.group(1) or "").strip("/").split("/")
            if s and not (s.startswith("(") and s.endswith(")"))
        ]
        route = "/" + "/".join(segs)
        for m in _NEXT_HANDLER_RE.finditer(text):
            entry.append(
                _entry("http", m.group(1), route, _line_of(text, m.start()), m.group(1))
            )
    return {
        "specs": _js_specs(text),
        "names": {k: tuple(dict.fromkeys(v)) for k, v in names.items() if v},
        "ns": {k: tuple(v) for k, v in ns.items()},
        "uses": _attr_uses(text, [a for v in ns.values() for a in v]),
        "default": default,
        "entry": entry,
        "lits": _js_lits(text),
    }


def _scan_css(text: str, rel: str) -> dict:
    return {"specs": _css_specs(text)}


# --- Go --------------------------------------------------------------------- #

_GO_PKG_RE = re.compile(r"^package[ \t]+(\w+)", re.M)
_GO_IMPORT_BLOCK_RE = re.compile(r"^import[ \t]*\(", re.M)
_GO_IMPORT_ONE_RE = re.compile(r'^import[ \t]+(?:([\w.]+)[ \t]+)?"([^"\n]+)"', re.M)
_GO_IMPORT_LINE_RE = re.compile(r'^[ \t]*(?:([\w.]+)[ \t]+)?"([^"\n]+)"', re.M)
_GO_DEF_RE = re.compile(
    r"^(?:func[ \t]+([A-Z]\w*)|type[ \t]+([A-Z]\w*)|(?:var|const)[ \t]+([A-Z]\w*))",
    re.M,
)
_GO_DEF_BLOCK_RE = re.compile(r"^(?:type|var|const)[ \t]*\(", re.M)
_GO_ROUTE_RE = re.compile(
    r"""(?:(?<![\w.])|\.)(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)"""
    r"""\(\s*"((?:[A-Z]+\s+)?/[^"\n]*)\"(?:\s*,\s*([\w.]+))?"""
)
_GO_MAIN_RE = re.compile(r"^func[ \t]+main[ \t]*\([ \t]*\)", re.M)


def _go_aliases(alias: str, path: str) -> List[str]:
    """Local names an import may be referred to by: the explicit alias, else
    the last path segment (and, for ``…/v2`` / ``go-foo`` style paths, the
    names the package clause most likely uses)."""
    if alias and alias not in ("_", "."):
        return [alias]
    segs = path.rstrip("/").split("/")
    last = segs[-1]
    out = [last]
    if re.match(r"^v\d+$", last) and len(segs) > 1:
        out.append(segs[-2])
    for cand in list(out):
        c = re.sub(r"^go-|-go$|\.go$", "", cand).replace("-", "_").replace(".", "_")
        if c not in out:
            out.append(c)
    return out


def _scan_go(text: str, rel: str) -> dict:
    specs: List[tuple] = []
    for m in _GO_IMPORT_ONE_RE.finditer(text):
        specs.append((m.group(1) or "", m.group(2)))
    for body in _paren_blocks(text, _GO_IMPORT_BLOCK_RE):
        for m in _GO_IMPORT_LINE_RE.finditer(body):
            specs.append((m.group(1) or "", m.group(2)))
    defs: Set[str] = set()
    for m in _GO_DEF_RE.finditer(text):
        defs.add(m.group(1) or m.group(2) or m.group(3))
    for body in _paren_blocks(text, _GO_DEF_BLOCK_RE):
        defs.update(re.findall(r"^[ \t]+([A-Z]\w*)\b", body, re.M))
    aliases = [a for al, p in specs for a in _go_aliases(al, p)]
    pm = _GO_PKG_RE.search(text)
    pkg = pm.group(1) if pm else ""
    entry: List[dict] = []
    for m in _GO_ROUTE_RE.finditer(text):
        verb, route = m.group(1), m.group(2)
        meth = "ANY" if verb in ("HandleFunc", "Handle") else verb.upper()
        mm = re.match(r"^([A-Z]+)\s+(/.*)$", route)
        if mm:
            meth, route = mm.group(1), mm.group(2)
        entry.append(
            _entry("http", meth, route, _line_of(text, m.start()), m.group(3) or "")
        )
    if pkg == "main":
        for m in _GO_MAIN_RE.finditer(text):
            entry.append(
                _entry("main", "MAIN", "main()", _line_of(text, m.start()), "main")
            )
    return {
        "specs": specs,
        "pkg": pkg,
        "defs": frozenset(defs),
        "uses": _attr_uses(text, aliases),
        "entry": entry,
    }


def _scan_gomod(text: str, rel: str) -> dict:
    m = re.search(r"^module[ \t]+(\S+)", text, re.M)
    return {"module": m.group(1).strip('"') if m else ""}


# --- Java / Kotlin ---------------------------------------------------------- #

_JV_PKG_RE = re.compile(r"^[ \t]*package[ \t]+([\w.]+)", re.M)
_JV_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+(static[ \t]+)?([\w.]+?)(\.\*)?(?:[ \t]+as[ \t]+\w+)?[ \t]*;?[ \t]*$",
    re.M,
)
_JV_HEADER_LINE_RE = re.compile(r"^[ \t]*(?:package|import)[ \t][^\n]*$", re.M)
# Comments and string/char literals, so a class named in prose or a log
# message never reads as a same-package reference.
_JV_STRIP_RE = re.compile(
    r"""/\*.*?(?:\*/|\Z)|//[^\n]*|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""", re.S
)
_JV_TYPE_DEF_RE = re.compile(
    r"\b(?:class|interface|enum|record|object)[ \t]+([A-Z]\w*)"
)
_KT_TOP_DEF_RE = re.compile(
    r"^(?:(?:public|internal|private|inline|suspend|const|data|sealed|open|abstract)[ \t]+)*"
    r"(?:fun|val|var|typealias)[ \t]+(?:<[^>\n]*>[ \t]*)?(?:[\w.]+\.)?([A-Za-z_]\w*)",
    re.M,
)
_JV_MAPPING_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b[ \t]*(\()?")
_JV_EVENT_RE = re.compile(
    r"@(Scheduled|KafkaListener|SqsListener|RabbitListener|JmsListener|StreamListener)\b[ \t]*(\()?"
)
_JV_EVENT_METHOD = {
    "Scheduled": "SCHEDULED",
    "KafkaListener": "KAFKA",
    "SqsListener": "SQS",
    "RabbitListener": "RABBIT",
    "JmsListener": "JMS",
    "StreamListener": "STREAM",
}
_JV_CLASS_HEAD_RE = re.compile(r"\b(?:class|interface|object)[ \t]+([A-Z]\w*)")
_JV_ANNOT_SKIP_RE = re.compile(r"\s*(?:@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)*")
_JV_HANDLER_RE = re.compile(r"[^;{}=]{0,300}?\b([A-Za-z_]\w*)\s*\(")
_JV_MAIN_RE = re.compile(r"\bpublic[ \t]+static[ \t]+void[ \t]+main[ \t]*\(")
_KT_MAIN_RE = re.compile(r"^fun[ \t]+main[ \t]*\(", re.M)
_JV_NOT_NAMES = frozenset(
    {"if", "for", "while", "switch", "return", "new", "catch", "synchronized", "fun"}
)


def _jv_route(args: str) -> str:
    m = re.search(r'\b(?:value|path)\s*=\s*\{?\s*"([^"]*)"', args)
    if m:
        return m.group(1)
    m = re.match(r'\s*\{?\s*"([^"]*)"', args)
    return m.group(1) if m else ""


def _jv_handler(text: str, pos: int) -> str:
    """The method a Java/Kotlin annotation at ``pos`` decorates: skip any
    further annotations, then the first ``name(`` of the declaration."""
    m = _JV_ANNOT_SKIP_RE.match(text, pos)
    at = m.end() if m else pos
    hm = _JV_HANDLER_RE.match(text, at)
    if hm and hm.group(1) not in _JV_NOT_NAMES:
        return hm.group(1)
    return ""


def _jv_entries(text: str, rel: str, lang: str) -> List[dict]:
    """Spring ``@…Mapping`` routes (joined with the class-level
    ``@RequestMapping`` prefix), ``@Scheduled``/``@KafkaListener``/
    ``@SqsListener``/… events and ``main`` methods."""
    out: List[dict] = []
    head = _JV_CLASS_HEAD_RE.search(text)
    hpos = head.start() if head else 0
    prefix = ""
    if head:
        before = text[:hpos]
        # The annotation block right above the class: after the last import
        # (``;``) or a previous top-level type's closing brace — NOT any
        # ``}``, which annotation arguments (``extraTags = {…}``) contain.
        closers = [m.start() for m in re.finditer(r"^\}", before, re.M)]
        cut = max([before.rfind(";")] + closers[-1:])
        for m in _JV_MAPPING_RE.finditer(before, cut + 1):
            if m.group(1) == "Request" and m.group(2):
                prefix = _jv_route(_call_args(text, m.end() - 1))
    for k, m in enumerate(_JV_MAPPING_RE.finditer(text, hpos)):
        if k >= _MAX_ENTRY_MATCHES:
            break
        args = _call_args(text, m.end() - 1, 400) if m.group(2) else ""
        kind = m.group(1)
        if kind == "Request":
            rm = re.search(r"RequestMethod\.(\w+)", args)
            meth = rm.group(1).upper() if rm else "ANY"
        else:
            meth = kind.upper()
        end = m.end() - 1 + len(args) + 2 if m.group(2) else m.end()
        handler = _jv_handler(text, end)
        if not handler:
            continue  # a type-level mapping on a nested class, not a method
        route = prefix + _jv_route(args)
        out.append(
            _entry("http", meth, route or "/", _line_of(text, m.start()), handler)
        )
    for k, m in enumerate(_JV_EVENT_RE.finditer(text)):
        if k >= _MAX_ENTRY_MATCHES:
            break
        args = _call_args(text, m.end() - 1, 400) if m.group(2) else ""
        end = m.end() - 1 + len(args) + 2 if m.group(2) else m.end()
        what = _first_str(args)
        if what is None:
            km = re.search(
                r"""\b(?:topics|queues|value|destination|cron|fixedRate|fixedDelay)\s*=\s*\{?\s*"?([^",}\n]*)""",
                args,
            )
            what = km.group(1).strip() if km else ""
        out.append(
            _entry(
                "event",
                _JV_EVENT_METHOD[m.group(1)],
                what,
                _line_of(text, m.start()),
                _jv_handler(text, end),
            )
        )
    stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    main_re = _KT_MAIN_RE if lang == "kotlin" else _JV_MAIN_RE
    for m in main_re.finditer(text):
        out.append(
            _entry("main", "MAIN", stem + ".main", _line_of(text, m.start()), "main")
        )
    return out


def _scan_jvm(text: str, rel: str, lang: str) -> dict:
    pm = _JV_PKG_RE.search(text)
    specs = [
        (m.group(2), bool(m.group(1)), bool(m.group(3)))
        for m in _JV_IMPORT_RE.finditer(text)
    ]
    body = _JV_STRIP_RE.sub(" ", _JV_HEADER_LINE_RE.sub("", text))
    defs = set(_JV_TYPE_DEF_RE.findall(body))
    if lang == "kotlin":
        defs.update(_KT_TOP_DEF_RE.findall(body))
    return {
        "specs": specs,
        "pkg": pm.group(1) if pm else "",
        "toks": frozenset(map(sys.intern, re.findall(r"\b([A-Z]\w*)\b", body))),
        "defs": frozenset(defs),
        "entry": _jv_entries(text, rel, lang),
    }


# --- Rust ------------------------------------------------------------------- #

_RS_MOD_RE = re.compile(
    r"^[ \t]*(?:pub(?:\([^)\n]*\))?[ \t]+)?mod[ \t]+(\w+)[ \t]*;", re.M
)
_RS_USE_RE = re.compile(
    r"^[ \t]*(?:pub(?:\([^)\n]*\))?[ \t]+)?use[ \t]+((?:crate|super|self)(?:::\w+)*)"
    r"(?:::\{([^;]{0,2000})\}|::\*)?(?:[ \t]+as[ \t]+\w+)?[ \t]*;",
    re.M,
)
_RS_ROUTE_RE = re.compile(r'#\[(get|post|put|patch|delete)\(\s*"([^"\n]+)"')
_RS_AXUM_RE = re.compile(
    r'\.route\(\s*"(/[^"\n]*)"\s*,\s*(get|post|put|patch|delete)\(\s*([\w:]+)'
)
_RS_FN_AFTER_RE = re.compile(r"\bfn[ \t]+(\w+)")
_RS_MAIN_RE = re.compile(r"^(?:pub[ \t]+)?(?:async[ \t]+)?fn[ \t]+main[ \t]*\(", re.M)


def _scan_rust(text: str, rel: str) -> dict:
    specs: List[tuple] = [("mod", m.group(1)) for m in _RS_MOD_RE.finditer(text)]
    for m in _RS_USE_RE.finditer(text):
        names: List[str] = []
        if m.group(2):
            for part in m.group(2).split(","):
                nm = (
                    part.strip()
                    .split(" as ", 1)[0]
                    .strip()
                    .split("::")[-1]
                    .strip("{} ")
                )
                if nm and nm not in ("self", "*") and re.match(r"^\w+$", nm):
                    names.append(nm)
        specs.append(("use", m.group(1), tuple(names), bool(m.group(2))))
    entry: List[dict] = []
    fns = list(_RS_FN_AFTER_RE.finditer(text))
    fn_starts = [f.start() for f in fns]
    for m in _RS_ROUTE_RE.finditer(text):
        fm = _next_match(fns, fn_starts, m.end())
        entry.append(
            _entry(
                "http",
                m.group(1).upper(),
                m.group(2),
                _line_of(text, fm.start() if fm else m.start()),
                fm.group(1) if fm else "",
            )
        )
    for m in _RS_AXUM_RE.finditer(text):
        entry.append(
            _entry(
                "http",
                m.group(2).upper(),
                m.group(1),
                _line_of(text, m.start()),
                m.group(3).split("::")[-1],
            )
        )
    for m in _RS_MAIN_RE.finditer(text):
        entry.append(
            _entry("main", "MAIN", "main()", _line_of(text, m.start()), "main")
        )
    return {"specs": specs, "entry": entry}


# --- C / C++ ---------------------------------------------------------------- #

_C_INC_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]+)[>"]', re.M)
_C_COMMENT_RE = re.compile(r"/\*.*?(?:\*/|\Z)|//[^\n]*", re.S)
_C_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_C_CALLISH_RE = re.compile(r"\b([A-Za-z_]\w*)[ \t]*\(")
_C_TYPE_NAME_RE = re.compile(
    r"\b(?:struct|enum|union|class)[ \t]+([A-Za-z_]\w*)|\}[ \t]*([A-Za-z_]\w*)[ \t]*;"
    r"|^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)",
    re.M,
)
_C_MAIN_RE = re.compile(r"^(?:(?:static|extern)[ \t]+)?int[ \t\n]+main[ \t]*\(", re.M)
_C_KEYWORDS = frozenset(
    "if for while switch return sizeof defined case do else typeof alignof "
    "static_assert __attribute__ __declspec".split()
)
_MAX_C_TOKS = 3000
_C_TYPISH_RE = re.compile(
    r"\b(?:struct|enum|union)[ \t]+([A-Za-z_]\w*)|\b([A-Za-z_]\w*_t|[A-Z]\w*)\b"
)


def _c_in_conditional(text: str, pos: int) -> bool:
    """Whether ``pos`` sits inside an ``#if``/``#ifdef`` block (a ``main``
    behind ``#ifdef SELF_TEST`` is not the program's entry point)."""
    before = text[:pos]
    opens = len(re.findall(r"^[ \t]*#[ \t]*if", before, re.M))
    closes = len(re.findall(r"^[ \t]*#[ \t]*endif", before, re.M))
    return opens > closes


def _blank_keep_lines(m: "re.Match[str]") -> str:
    """A comment replaced by its newlines only, so line numbers survive."""
    return "\n" * m.group(0).count("\n") or " "


def _scan_c(text: str, rel: str) -> dict:
    code = _C_COMMENT_RE.sub(_blank_keep_lines, text)
    specs = [(m.group(2).strip(), m.group(1) == '"') for m in _C_INC_RE.finditer(code)]
    # Only identifiers that can NAME a header's interface — calls/macros,
    # ``*_t`` / CamelCase / UPPER_CASE types and constants, struct tags —
    # interned: this set lives in the scan memo for every C file.
    toks = set(_C_CALLISH_RE.findall(code)) - _C_KEYWORDS
    toks.update(a or b for a, b in _C_TYPISH_RE.findall(code))
    if len(toks) > _MAX_C_TOKS:
        toks = set(sorted(toks)[:_MAX_C_TOKS])
    rec: dict = {"specs": specs, "toks": frozenset(map(sys.intern, toks))}
    if rel.lower().endswith(_C_HEADER_EXTS):
        defs = {n for n in _C_CALLISH_RE.findall(code) if n not in _C_KEYWORDS}
        for m in _C_TYPE_NAME_RE.finditer(code):
            defs.add(m.group(1) or m.group(2) or m.group(3))
        rec["defs"] = frozenset(defs)
    entry = []
    for k, m in enumerate(_C_MAIN_RE.finditer(code)):
        if k >= 16:
            break
        if not _c_in_conditional(code, m.start()):
            entry.append(
                _entry("main", "MAIN", "main()", _line_of(code, m.start()), "main")
            )
    rec["entry"] = entry
    return rec


# --- HCL / Terraform -------------------------------------------------------- #

_HCL_SRC_RE = re.compile(
    r'^[ \t]*(?:source|config_path)[ \t]*=[ \t]*"(\.\.?/[^"?\n]*)', re.M
)
_HCL_DEF_RE = re.compile(r'^(?:variable|output)[ \t]+"([^"\n]+)"', re.M)


def _scan_hcl(text: str, rel: str) -> dict:
    uses = set(re.findall(r"\bvar\.(\w+)", text))
    uses.update(re.findall(r"^[ \t]*(\w+)[ \t]*=", text, re.M))
    uses.update(re.findall(r"\bmodule\.\w+\.(\w+)", text))
    uses.update(re.findall(r"\bdependency\.\w+\.outputs\.(\w+)", text))
    return {
        "specs": [m.group(1).rstrip("/") for m in _HCL_SRC_RE.finditer(text)],
        "uses": frozenset(uses),
        "defs": frozenset(_HCL_DEF_RE.findall(text)),
    }


_SCANNERS: Dict[str, Any] = {
    "py": _scan_py,
    "js": _scan_js,
    "css": _scan_css,
    "go": _scan_go,
    "gomod": _scan_gomod,
    "java": lambda t, r: _scan_jvm(t, r, "java"),
    "kotlin": lambda t, r: _scan_jvm(t, r, "kotlin"),
    "rust": _scan_rust,
    "c": _scan_c,
    "hcl": _scan_hcl,
}
_EMPTY_REC: dict = {}


def _scan_text(text: str, lang: str, rel: str) -> dict:
    """One file's scan record; ``{}`` when the scanner fails (one odd file
    costs its own relations, never the graph)."""
    fn = _SCANNERS.get(lang)
    if fn is None:
        return _EMPTY_REC
    try:
        return fn(text, rel)
    except Exception:  # noqa: BLE001
        return _EMPTY_REC
    finally:
        _TLS.nl = None  # don't pin the last file's text to the thread


def link_escapes(abs_path: str, rel: str) -> bool:
    """Whether ``abs_path`` (= worktree + ``rel``) is a SYMLINK resolving
    outside the worktree. Its content is not the worktree's: the Atlas,
    search and entry points must not surface its names (the file view
    already refuses it). Only leaf links are checked — git never tracks a
    path below a symlinked directory — so this is one ``lstat`` per file."""
    try:
        if not stat.S_ISLNK(os.lstat(abs_path).st_mode):
            return False
    except OSError:
        return False
    tail = rel.replace("/", os.sep)
    if abs_path.endswith(os.sep + tail):
        root = abs_path[: len(abs_path) - len(tail) - 1]
    else:
        root = os.path.dirname(abs_path)
    real = os.path.realpath(abs_path)
    rr = os.path.realpath(root)
    return not (real == rr or real.startswith(rr.rstrip(os.sep) + os.sep))


def _scan_file(
    abs_path: str,
    rel: str,
    lang: str,
    over_budget: bool,
    reads: Optional[list] = None,
) -> Optional[dict]:
    """One file's scan record, memoized on ``(mtime_ns, size)`` — resolution
    depends on the whole file set and is redone per build, but READING is the
    cost, and a file that hasn't changed never needs re-reading. ``None`` =
    not memoized and the build is over budget (the caller marks the graph
    partial; the next build picks up from here). ``reads[0]`` (when given) is
    bumped per file actually read.

    Only REGULAR files are read: a symlink to a FIFO/tty/device (git lists
    the link) is ``{}`` — no imports — never an ``open()`` that blocks;
    neither is a link resolving OUTSIDE the worktree (:func:`link_escapes`)."""
    if link_escapes(abs_path, rel):
        return _EMPTY_REC
    try:
        st = os.stat(abs_path)
    except OSError:
        return _EMPTY_REC
    if not stat.S_ISREG(st.st_mode):
        return _EMPTY_REC
    with _LOCK:
        memo = _SCAN_MEMO.get(abs_path)
    if memo is not None and memo[0] == st.st_mtime_ns and memo[1] == st.st_size:
        return memo[2]
    if over_budget:
        return None
    if reads is not None:
        reads[0] += 1
    try:
        text = _read_head(abs_path)
    except OSError:
        return _EMPTY_REC
    if text is None:
        return _EMPTY_REC
    rec = _scan_text(text, lang, rel)
    with _LOCK:
        if len(_SCAN_MEMO) >= _SCAN_MEMO_MAX:
            _SCAN_MEMO.clear()
        _SCAN_MEMO[abs_path] = (st.st_mtime_ns, st.st_size, rec)
    return rec


def _py_index(py_rels: Sequence[str]) -> Dict[str, List[tuple]]:
    """``{dotted: [(root_depth, rel), …]}`` — every ``.py`` file under every
    suffix-dotted name rooted at each ancestor dir (``backend/web/server.py``
    → ``backend.web.server``, ``web.server``, ``server``), because a repo's
    import roots are unknowable without running it (``src/`` layouts, a
    ``tests/`` dir on ``sys.path``, scripts importing siblings).
    ``pkg/__init__.py`` indexes as ``pkg``. ``root_depth`` = how many leading
    dirs the dotted name drops (0 = rooted at the repo root)."""
    idx: Dict[str, List[tuple]] = {}
    for rel in py_rels:
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        # Only suffixes made entirely of identifiers are importable.
        first_ok = len(parts)
        for i in range(len(parts) - 1, -1, -1):
            if not parts[i].isidentifier():
                break
            first_ok = i
        for i in range(first_ok, len(parts)):
            idx.setdefault(".".join(parts[i:]), []).append((i, rel))
    return idx


def _is_ancestor_dir(root: str, d: str) -> bool:
    return root == "" or d == root or d.startswith(root + "/")


_SRC_ROOT_NAMES = frozenset({"src", "lib", "python", "py", "source"})


def _py_lookup(
    idx: Dict[str, List[tuple]], dotted: str, importer_dir: str
) -> Optional[str]:
    """Best module file for ``dotted`` seen from a file in ``importer_dir``.

    A multi-segment name accepts any suffix match (``pkg.mod`` really is
    ``src/pkg/mod.py`` in a src layout). A SINGLE segment only matches a
    module rooted at the importer's own dir or an ancestor of it (a script
    importing its sibling, ``tests/`` on ``sys.path``) or at a conventional
    source root — otherwise ``import json`` would link every file to some
    unrelated ``fixtures/json.py``. Among candidates: rooted at an ancestor
    of the importer first, then the shallowest root, then path order."""
    cands = idx.get(dotted)
    if not cands:
        return None
    single = "." not in dotted
    best = None
    best_key = None
    for depth, rel in cands:
        root = "/".join(rel.split("/")[:depth])
        anc = _is_ancestor_dir(root, importer_dir)
        if single and depth > 0 and not anc:
            if root.rsplit("/", 1)[-1] not in _SRC_ROOT_NAMES:
                continue
        key = (0 if anc else 1, depth, rel)
        if best_key is None or key < best_key:
            best, best_key = rel, key
    return best


def _py_targets(
    spec: tuple, rel: str, idx: Dict[str, List[tuple]], fileset: Dict[str, int]
) -> List[str]:
    """The files one Python import statement resolves to (see
    :func:`_py_resolve`, which also says WHICH names each one provides)."""
    return [t for t, _names in _py_resolve(spec, rel, idx, fileset, None)]


def _py_resolve(
    spec: tuple,
    rel: str,
    idx: Dict[str, List[tuple]],
    fileset: Dict[str, int],
    uses: Optional[Dict[str, tuple]],
) -> List[Tuple[str, tuple]]:
    """``[(target_rel, names)]`` for one import statement of ``rel``.

    ``names`` are the symbols the importer takes from that file, where the
    statement says so: ``from m import a, b`` → ``(a, b)`` on ``m``;
    ``import pkg.mod`` / ``from pkg import mod`` (a MODULE import) → the
    attributes the importer reads off the alias (``mod.x`` → ``x``, from
    ``uses``, the scan's ``{alias: attrs}``). An import that resolved to a
    shorter prefix names the next dotted segment (``import a.b.C`` hitting
    ``a/b.py`` → ``C``). The interface ranking downstream intersects these
    with the target's real symbols, so an over-broad name costs nothing."""
    uses = uses or {}
    importer_dir = posixpath.dirname(rel)
    out: List[Tuple[str, tuple]] = []
    seen: Dict[str, int] = {}

    def _add(t: str, names: Iterable[str]) -> None:
        k = seen.get(t)
        if k is None:
            seen[t] = len(out)
            out.append((t, tuple(names)))
        else:
            out[k] = (t, tuple(dict.fromkeys(out[k][1] + tuple(names))))

    if spec[0] == "imp":
        parts = spec[1].split(".")
        alias = spec[2] if len(spec) > 2 else spec[1]
        for k in range(len(parts), 0, -1):
            hit = _py_lookup(idx, ".".join(parts[:k]), importer_dir)
            if hit:
                if k < len(parts):
                    _add(hit, (parts[k],))
                else:
                    _add(hit, uses.get(alias, ()))
                break
        return out
    level, module, names = spec[1], spec[2], spec[3]
    aliases = spec[4] if len(spec) > 4 else names
    if level:
        base = importer_dir
        for _ in range(level - 1):
            if not base:
                return out
            base = posixpath.dirname(base)
        target = posixpath.join(base, module.replace(".", "/")) if module else base

        def _mod(p: str) -> Optional[str]:
            # p == "" is the repo root package (`from . import x` in a
            # root-level file): only its __init__.py can be the module.
            cands = (p + ".py", p + "/__init__.py") if p else ("__init__.py",)
            for cand in cands:
                if cand in fileset:
                    return cand
            return None

        missing: List[str] = []
        for n, al in zip(names, aliases):
            hit = _mod(posixpath.join(target, n) if target else n)
            if hit:
                _add(hit, uses.get(al, ()))
            else:
                missing.append(n)
        if missing or not names:
            # `from .m import y` where y is a NAME inside m.py / m/__init__.py
            hit = _mod(target)
            if hit:
                _add(hit, missing)
        return out
    # absolute `from a.b import c, d`: try a.b.c per name, then a.b, then shorter
    parts = module.split(".")
    missing = []
    for n, al in zip(names, aliases):
        hit = _py_lookup(idx, module + "." + n, importer_dir)
        if hit:
            _add(hit, uses.get(al, ()))
        else:
            missing.append(n)
    if missing or not names:
        for k in range(len(parts), 0, -1):
            hit = _py_lookup(idx, ".".join(parts[:k]), importer_dir)
            if hit:
                _add(hit, missing if k == len(parts) else (parts[k],))
                break
    return out


def _probe(base: str, fileset: Dict[str, int], exts: Sequence[str]) -> Optional[str]:
    """First existing file for an extension-less/extensioned module base:
    exact, ``base+ext``, ``.js``→TS twin (ESM-style ``./x.js`` naming
    ``x.ts``), then ``base/index+ext``."""
    base = posixpath.normpath(base) if base else ""
    if base in (".", ""):
        base = ""
    elif base.startswith("../") or base == "..":
        return None
    if base and base in fileset:
        return base
    if base:
        for e in exts:
            if base + e in fileset:
                return base + e
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            for e in (".ts", ".tsx", ".mts", ".cts"):
                if stem + e in fileset:
                    return stem + e
    prefix = base + "/index" if base else "index"
    for e in exts:
        if prefix + e in fileset:
            return prefix + e
    return None


def _lenient_json(text: str) -> Any:
    """``json.loads`` for tsconfig-style JSONC: ``//`` and ``/* */`` comments
    (outside strings) and trailing commas are dropped first."""
    out: List[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def _load_json_memo(abs_path: str) -> Any:
    """A tsconfig/jsconfig parsed leniently, memoized on ``(mtime_ns, size)``;
    None when missing, malformed or not a REGULAR file (a config symlinked to
    a FIFO must not hang the build — see :func:`_open_regular`)."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    with _LOCK:
        memo = _JSON_MEMO.get(abs_path)
    if memo is not None and memo[0] == st.st_mtime_ns and memo[1] == st.st_size:
        return memo[2]
    try:
        fh = _open_regular(abs_path)
        if fh is None:
            return None
        with fh:
            raw = fh.read(_MAX_SCAN_BYTES)
        parsed = _lenient_json(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — malformed config = no aliases
        parsed = None
    with _LOCK:
        if len(_JSON_MEMO) > 2000:
            _JSON_MEMO.clear()
        _JSON_MEMO[abs_path] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def _ts_aliases(cfg_rel: str, wt: str) -> Optional[tuple]:
    """``(base_dir|None, paths_root, [(prefix, suffix|None, [targets])])``
    from a tsconfig/jsconfig's ``compilerOptions.baseUrl`` + ``paths``
    (all dirs worktree-relative; ``suffix`` None = an exact, star-less
    pattern), following up to 3 relative ``extends`` hops for configs that
    inherit them. ``paths`` targets resolve against ``baseUrl`` (itself
    relative to the config that sets it), or against the defining config's
    dir when no baseUrl is set — TypeScript's own rule. None = no aliases."""
    cur = cfg_rel
    base_dir: Optional[str] = None
    paths: Optional[dict] = None
    paths_dir = ""
    for _ in range(4):
        data = _load_json_memo(os.path.join(wt, cur))
        if not isinstance(data, dict):
            break
        co = data.get("compilerOptions")
        here = posixpath.dirname(cur)
        if isinstance(co, dict):
            if base_dir is None and isinstance(co.get("baseUrl"), str):
                base_dir = posixpath.normpath(posixpath.join(here, co["baseUrl"]))
            if paths is None and isinstance(co.get("paths"), dict):
                paths = co["paths"]
                paths_dir = here
        ext = data.get("extends")
        if not isinstance(ext, str) or not ext.startswith("."):
            break
        nxt = posixpath.normpath(posixpath.join(here, ext))
        if not nxt.endswith(".json"):
            nxt += ".json"
        if nxt.startswith(".."):
            break
        cur = nxt
    if base_dir is not None and base_dir == ".":
        base_dir = ""
    rules = []
    for pat, targets in (paths or {}).items():
        if not isinstance(pat, str) or not isinstance(targets, list):
            continue
        if pat.count("*") > 1:
            continue
        prefix, star, suffix = pat.partition("*")
        tl = [t for t in targets if isinstance(t, str) and t.count("*") <= 1]
        if tl:
            rules.append((prefix, suffix if star else None, tl))
    rules.sort(key=lambda r: -len(r[0]))
    if base_dir is None and not rules:
        return None
    root = base_dir if base_dir is not None else paths_dir
    if root == ".":
        root = ""
    return (base_dir, root, rules)


def _js_targets(
    spec: str,
    rel: str,
    fileset: Dict[str, int],
    wt: str,
    cfg_cache: Dict[str, Optional[tuple]],
    dir_cfg: Dict[str, Optional[str]],
) -> Optional[str]:
    if spec.startswith("./") or spec.startswith("../") or spec in (".", ".."):
        return _probe(
            posixpath.join(posixpath.dirname(rel), spec), fileset, _JS_PROBE_EXTS
        )
    if spec.startswith("/") or "://" in spec:
        return None
    cfg = _nearest_tsconfig(posixpath.dirname(rel), fileset, dir_cfg)
    if not cfg:
        return None
    if cfg not in cfg_cache:
        cfg_cache[cfg] = _ts_aliases(cfg, wt)
    alias = cfg_cache[cfg]
    if not alias:
        return None
    base_dir, root, rules = alias
    for prefix, suffix, targets in rules:
        if suffix is None:
            if spec != prefix:
                continue
            star = ""
        else:
            if not (spec.startswith(prefix) and spec.endswith(suffix)):
                continue
            if len(spec) < len(prefix) + len(suffix):
                continue
            star = spec[len(prefix) : len(spec) - len(suffix)]
        for t in targets:
            hit = _probe(
                posixpath.join(root, t.replace("*", star)), fileset, _JS_PROBE_EXTS
            )
            if hit:
                return hit
    if base_dir is not None:
        return _probe(posixpath.join(base_dir, spec), fileset, _JS_PROBE_EXTS)
    return None  # a bare package


def _nearest_tsconfig(
    d: str, fileset: Dict[str, int], dir_cfg: Dict[str, Optional[str]]
) -> Optional[str]:
    """Nearest ``tsconfig.json``/``jsconfig.json`` at or above dir ``d``
    (looked up in the file set — no filesystem walk), memoized per dir."""
    chain = []
    cur = d
    found: Optional[str] = None
    while True:
        if cur in dir_cfg:
            found = dir_cfg[cur]
            break
        chain.append(cur)
        hit = None
        for name in ("tsconfig.json", "jsconfig.json"):
            cand = posixpath.join(cur, name) if cur else name
            if cand in fileset:
                hit = cand
                break
        if hit:
            found = hit
            break
        if not cur:
            break
        cur = posixpath.dirname(cur)
    for c in chain:
        dir_cfg[c] = found
    return found


def _css_targets(spec: str, rel: str, fileset: Dict[str, int]) -> Optional[str]:
    if "://" in spec or spec.startswith(("//", "~", "/", "sass:")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
    if base.startswith(".."):
        return None
    hit = _probe(base, fileset, _CSS_EXTS)
    if hit:
        return hit
    # SCSS partials: `@use "vars"` → `_vars.scss`
    d, name = posixpath.split(base)
    partial = posixpath.join(d, "_" + name) if d else "_" + name
    for e in ("",) + _CSS_EXTS:
        if partial + e in fileset:
            return partial + e
    for e in _CSS_EXTS:
        cand = posixpath.join(base, "_index" + e)
        if cand in fileset:
            return cand
    return None


def build_graph(wt: str, files: Sequence[Any], fp: Optional[str] = None) -> dict:
    """``{"edges": [[src_idx, dst_idx]], "partial": bool, "langs": {ext: n}}``
    — who imports whom, as indices into ``files`` (rows from
    :func:`list_files` or bare rel paths). ``src`` imports ``dst``.

    Resolvers: Python (``import``/``from``, relative imports, a suffix module
    index), JS/TS (``import``/``export … from``/``require``/dynamic
    ``import()``, relative probing, nearest tsconfig/jsconfig ``baseUrl`` +
    ``paths``), CSS/SCSS/LESS (``@import``/``@use``), Go (``go.mod`` module
    path → package dir), Java/Kotlin (FQN imports, wildcard/static imports,
    and SAME-PACKAGE references, which Java writes without any import; never
    main → test), Rust (``mod x;``, ``use crate::/super::/self::``), C/C++
    (``#include`` relative, then suffix/unique basename; umbrella headers
    followed transitively), HCL/Terraform (module ``source`` /
    ``config_path`` dirs), and API edges (a JS/TS ``"/api/…"`` literal →
    the file declaring that route). Bare packages and anything outside
    ``files`` are ignored — the Map is about THIS repo.

    Budget: :data:`GRAPH_BUDGET_S` of reading per build. Past it, files not
    yet memoized are skipped and ``partial`` is set; the next call resumes
    from the per-file memo, so a huge repo converges over a few polls instead
    of stalling one. Every build reads at least :data:`_MIN_FRESH_READS` new
    files even past the deadline (a slow filesystem whose memo-hit stats
    alone eat the budget must still make progress), so repeated calls always
    converge to a complete graph. ``langs`` counts scanned files per
    extension (no dot). Cached on ``(wt, fp, file-list digest)`` when ``fp``
    is known and the build was complete. :func:`last_graph_partial` reports
    whether the most recent build for ``wt`` was partial — the snapshot
    route must not answer "unchanged" to a client holding a partial graph,
    or the resuming call never happens. :func:`graph_detail` is the same
    build with the edge names and entry points kept."""
    res = graph_detail(wt, files, fp)
    return {
        "edges": res["edges"],
        "partial": res["partial"],
        "langs": res["langs"],
    }


def graph_detail(
    wt: str,
    files: Sequence[Any],
    fp: Optional[str] = None,
    budget: Optional[float] = None,
) -> dict:
    """:func:`build_graph` plus what the Atlas needs from the same scan:

    - ``names``: ``{(src_idx, dst_idx): (name, …)}`` — the symbols ``src``
      takes from ``dst`` where the language says so (Python ``from m import
      a`` / ``mod.attr`` uses, JS named/default/namespace imports, Go
      ``pkg.Name`` uses, Java class names, Rust ``use`` items, C
      header-declared identifiers the includer mentions, HCL variables /
      outputs, and ``"GET /route"`` for API edges). Edges with no known
      names are absent.
    - ``entry``: ``{idx: [{"kind", "method", "route", "line", "handler"}]}``
      — per-file entry points (http / cli / event / main).
    - ``api_edges``: how many edges came only from route literals.

    ``budget`` overrides :data:`GRAPH_BUDGET_S` for this build (the Atlas
    shares one per-call budget between this and its outline pass). The
    returned containers are shared with the cache: callers must not mutate
    them. Never raises."""
    try:
        rels = [_row_rel(f) for f in files]
        digest = hashlib.sha1("\0".join(rels).encode("utf-8", "replace")).hexdigest()
        key = (wt, fp, digest)
        if fp is not None:
            with _LOCK:
                hit = _GRAPH_CACHE.get(key)
            if hit is not None:
                _note_partial(wt, False)
                return dict(hit, edges=[list(e) for e in hit["edges"]])
        res = _build_graph(wt, rels, budget)
        _note_partial(wt, bool(res["partial"]))
        if fp is not None and not res["partial"]:
            with _LOCK:
                _GRAPH_CACHE[key] = res
                while len(_GRAPH_CACHE) > _GRAPH_CACHE_MAX:
                    _GRAPH_CACHE.pop(next(iter(_GRAPH_CACHE)))
        return dict(res, edges=[list(e) for e in res["edges"]])
    except Exception:  # noqa: BLE001
        _note_partial(wt, True)
        return {
            "edges": [],
            "partial": True,
            "langs": {},
            "names": {},
            "entry": {},
            "api_edges": 0,
        }


# wt -> whether the most recent build_graph for it came back partial.
_GRAPH_PARTIAL: Dict[str, bool] = {}
_GRAPH_PARTIAL_MAX = 64
# Fresh file reads every build is allowed past the deadline (progress floor).
_MIN_FRESH_READS = 32


def _note_partial(wt: str, partial: bool) -> None:
    try:
        with _LOCK:
            _GRAPH_PARTIAL.pop(wt, None)  # re-insert = most recently used
            _GRAPH_PARTIAL[wt] = bool(partial)
            while len(_GRAPH_PARTIAL) > _GRAPH_PARTIAL_MAX:
                _GRAPH_PARTIAL.pop(next(iter(_GRAPH_PARTIAL)))
    except Exception:  # noqa: BLE001
        pass


def last_graph_partial(wt: str) -> bool:
    """Whether the most recent :func:`build_graph` for ``wt`` returned a
    PARTIAL graph (the read budget ran out); False when it was complete or
    there has been none. Never raises.

    WHY. A partial graph is never cached and the next build resumes from the
    per-file memo — but only if a next build HAPPENS. The snapshot route
    answers ``{"unchanged": true}`` for a matching ``?fp=`` before it ever
    reaches build_graph, and an idle worktree (plan review, the agent waiting
    for Go) never moves the fingerprint, so the client would hold a
    half-drawn blast radius under "still indexing" indefinitely. The route
    checks this before taking that shortcut."""
    try:
        with _LOCK:
            return bool(_GRAPH_PARTIAL.get(wt))
    except Exception:  # noqa: BLE001
        return False


def _build_graph(wt: str, rels: List[str], budget: Optional[float] = None) -> dict:
    """One uncached build. Two phases, and only the first is budgeted:

    1. READ — collect every source file's scan record (memo hit = one stat;
       miss = a read) until the deadline. Reading first means resolution
       time can never eat the reading budget: interleaved, a huge repo whose
       memoized prefix took the whole budget to RESOLVE read nothing new on
       every call and never converged. Past the deadline each build still
       reads :data:`_MIN_FRESH_READS` new files (unless the budget is
       non-positive — a "read nothing new" switch), so progress is certain.
    2. RESOLVE — map specifiers to files over the whole file set. Pure CPU on
       in-memory data; files without records this round simply draw no arms.
       Each language's resolver is isolated: one that trips on an odd repo
       costs that language's edges, not the graph."""
    index_of: Dict[str, int] = {}
    for i, rel in enumerate(rels):
        index_of.setdefault(rel, i)
    langs: Dict[str, int] = {}
    b = GRAPH_BUDGET_S if budget is None else budget
    deadline = time.monotonic() + b
    floor = _MIN_FRESH_READS if b > 0 else 0
    partial = False
    reads = [0]
    scanned: List[Tuple[int, str, str, dict]] = []
    for i, rel in enumerate(rels):
        ext = _ext(rel)
        if rel == "go.mod" or rel.endswith("/go.mod"):
            lang: Optional[str] = "gomod"
        else:
            lang = _LANG_OF.get(ext)
            if not lang:
                continue
            langs[ext[1:]] = langs.get(ext[1:], 0) + 1
        over = reads[0] >= floor and time.monotonic() > deadline
        rec = _scan_file(os.path.join(wt, rel), rel, lang, over, reads)
        if rec is None:
            partial = True
            continue
        if rec:
            scanned.append((i, rel, lang, rec))

    edges: Dict[Tuple[int, int], Set[str]] = {}

    def add(i: int, target: Any, names: Iterable[str] = ()) -> bool:
        j = index_of.get(target) if isinstance(target, str) else target
        if j is None or j == i:
            return False
        got = edges.get((i, j))
        fresh = got is None
        if fresh:
            got = edges[(i, j)] = set()
        got.update(n for n in names if n)
        return fresh

    by_lang: Dict[str, List[Tuple[int, str, dict]]] = {}
    rec_of: Dict[str, dict] = {}
    for i, rel, lang, rec in scanned:
        by_lang.setdefault(lang, []).append((i, rel, rec))
        rec_of[rel] = rec
    for fn in (
        _resolve_py,
        _resolve_js,
        _resolve_css,
        _resolve_go,
        _resolve_jvm,
        _resolve_rust,
        _resolve_c,
        _resolve_hcl,
    ):
        try:
            fn(wt, rels, index_of, by_lang, rec_of, add)
        except Exception:  # noqa: BLE001 — one resolver, not the graph
            continue
    api = 0
    try:
        api = _resolve_api(scanned, add)
    except Exception:  # noqa: BLE001
        api = 0
    entry = {i: list(rec["entry"]) for i, _r, _l, rec in scanned if rec.get("entry")}
    return {
        "edges": [list(e) for e in sorted(edges)],
        "partial": partial,
        "langs": langs,
        "names": {e: tuple(sorted(s)) for e, s in edges.items() if s},
        "entry": entry,
        "api_edges": api,
    }


def _resolve_py(wt, rels, index_of, by_lang, rec_of, add) -> None:
    items = by_lang.get("py")
    if not items:
        return
    py_idx = _py_index([r for r in rels if r.endswith(".py")])
    for i, rel, rec in items:
        uses = rec.get("uses") or {}
        for spec in rec.get("specs") or ():
            try:
                for t, names in _py_resolve(spec, rel, py_idx, index_of, uses):
                    add(i, t, names)
            except Exception:  # noqa: BLE001 — one odd specifier, not the graph
                continue


def _resolve_js(wt, rels, index_of, by_lang, rec_of, add) -> None:
    items = by_lang.get("js")
    if not items:
        return
    cfg_cache: Dict[str, Optional[tuple]] = {}
    dir_cfg: Dict[str, Optional[str]] = {}
    for i, rel, rec in items:
        named = rec.get("names") or {}
        ns = rec.get("ns") or {}
        uses = rec.get("uses") or {}
        for spec in rec.get("specs") or ():
            try:
                t = _js_targets(spec, rel, index_of, wt, cfg_cache, dir_cfg)
            except Exception:  # noqa: BLE001
                continue
            if not t:
                continue
            names = set(named.get(spec, ()))
            if "default" in names:
                names.discard("default")
                dflt = (rec_of.get(t) or {}).get("default")
                if dflt:
                    names.add(dflt)
            for alias in ns.get(spec, ()):
                names.update(uses.get(alias, ()))
            add(i, t, names)


def _resolve_css(wt, rels, index_of, by_lang, rec_of, add) -> None:
    for i, rel, rec in by_lang.get("css") or ():
        for spec in rec.get("specs") or ():
            try:
                add(i, _css_targets(spec, rel, index_of))
            except Exception:  # noqa: BLE001
                continue


def _resolve_go(wt, rels, index_of, by_lang, rec_of, add) -> None:
    """``import "mod/path/pkg"`` → every non-test ``.go`` file of the package
    dir under the ``go.mod`` whose module path prefixes it. Only files that
    define a name the importer uses (``pkg.Name``) are linked — a package is
    split across files, and linking all of them would credit every file
    with every use — unless no use is visible (blank/dot imports)."""
    items = by_lang.get("go")
    if not items:
        return
    mods: List[Tuple[str, str]] = []
    for _i, rel, rec in by_lang.get("gomod") or ():
        if rec.get("module"):
            mods.append((rec["module"], posixpath.dirname(rel)))
    if not mods:
        return
    mods.sort(key=lambda m: -len(m[0]))
    go_dirs: Dict[str, List[str]] = {}
    for _i, rel, _rec in items:
        if not rel.endswith("_test.go"):
            go_dirs.setdefault(posixpath.dirname(rel), []).append(rel)
    for i, rel, rec in items:
        uses = rec.get("uses") or {}
        for alias, path in rec.get("specs") or ():
            for mod, root in mods:
                if path != mod and not path.startswith(mod + "/"):
                    continue
                sub = path[len(mod) :].lstrip("/")
                d = posixpath.join(root, sub) if root else sub
                d = d.rstrip("/")
                targets = go_dirs.get(d) or []
                used: Set[str] = set()
                cands = _go_aliases(alias, path)
                pkgs = {(rec_of.get(t) or {}).get("pkg") for t in targets}
                for a in cands + [p for p in pkgs if p]:
                    used.update(uses.get(a, ()))
                for t in targets:
                    nm = used & set((rec_of.get(t) or {}).get("defs") or ())
                    if nm or not used or alias == ".":
                        add(i, t, nm)
                break


def _is_jvm_test(rel: str) -> bool:
    return "/src/test/" in "/" + rel or _is_test_path(rel)


def _jvm_source_root(rel: str, pkg: str) -> Tuple[str, str]:
    """``(source root, module)`` of a JVM file: its dir minus the package
    path (``api/src/main/java/``), and that minus ``src/<set>/<lang>/``
    (``api/``)."""
    d = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
    pp = pkg.replace(".", "/") + "/" if pkg else ""
    root = d[: -len(pp)] if pp and d.endswith(pp) else d
    m = re.match(r"^(.*?)(?:^|/)src/[^/]+/[^/]+/$", root)
    module = (m.group(1) + "/" if m and m.group(1) else "") if m else root
    return root, module


def _resolve_jvm(wt, rels, index_of, by_lang, rec_of, add) -> None:
    """Java/Kotlin: ``import a.b.C`` via an FQN index (package + file stem,
    plus every declared type / Kotlin top-level def), ``a.b.*`` to the
    package's files the importer names, static imports to their class, and
    SAME-PACKAGE references — capitalized tokens (comments and strings
    stripped) naming a sibling's class — which Java never writes as imports;
    without them a layered package reads as unrelated files. Main code never
    links to test code (a test double sharing a package name is not a
    dependency of production)."""
    items = (by_lang.get("java") or []) + (by_lang.get("kotlin") or [])
    if not items:
        return
    fqn: Dict[str, str] = {}
    pkg_names: Dict[str, Dict[str, List[str]]] = {}
    for _i, rel, rec in items:
        pkg = rec.get("pkg") or ""
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        names = {stem} | set(rec.get("defs") or ())
        for n in names:
            key = (pkg + "." + n) if pkg else n
            if n == stem or key not in fqn:
                fqn[key] = rel
            pkg_names.setdefault(pkg, {}).setdefault(n, []).append(rel)
    roots: Dict[str, Tuple[str, str]] = {}
    for _i, rel, rec in items:
        roots[rel] = _jvm_source_root(rel, rec.get("pkg") or "")

    def _nearest(rel: str, files: List[str]) -> List[str]:
        """The candidates in the importer's own source root, else its own
        module, else all: a copy-pasted twin (same package + class name in a
        sibling Gradle module) is not a dependency."""
        if len(files) < 2:
            return files
        mine = roots.get(rel) or ("", "")
        for k in (0, 1):
            near = [t for t in files if (roots.get(t) or ("", ""))[k] == mine[k]]
            if near:
                return near
        return files

    for i, rel, rec in items:
        src_test = _is_jvm_test(rel)
        toks = rec.get("toks") or frozenset()
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        own = {stem} | set(rec.get("defs") or ())

        def ok(t: str) -> bool:
            return t != rel and (src_test or not _is_jvm_test(t))

        for fq, static, star in rec.get("specs") or ():
            if star:
                for n, files in (pkg_names.get(fq) or {}).items():
                    if n in toks and n not in own:
                        for t in _nearest(rel, [f for f in files if ok(f)]):
                            add(i, t, (n,))
                continue
            cur = fq
            for _ in range(3):
                t = fqn.get(cur)
                if t is not None:
                    if ok(t):
                        add(i, t, (cur.rsplit(".", 1)[-1],))
                    break
                if "." not in cur:
                    break
                cur = cur.rsplit(".", 1)[0]
                if not cur.rsplit(".", 1)[-1][:1].isupper():
                    break  # only climb out of nested classes / static members
        pkg = rec.get("pkg")
        if pkg is None or pkg not in pkg_names:
            continue
        pn = pkg_names[pkg]
        for n in toks & pn.keys():
            if n in own:
                continue  # its own declaration, never a same-named twin
            for t in _nearest(rel, [f for f in pn[n] if ok(f)]):
                add(i, t, (n,))


def _rs_crate_root(d: str, fileset: Dict[str, int]) -> Optional[str]:
    cur = d
    for _ in range(64):
        for name in ("lib.rs", "main.rs"):
            if (posixpath.join(cur, name) if cur else name) in fileset:
                return cur
        if not cur:
            return None
        cur = posixpath.dirname(cur)
    return None


def _rs_mod_dir(rel: str) -> str:
    """The directory a Rust file's child modules live in: its own dir for
    ``lib.rs``/``main.rs``/``mod.rs``, else ``dir/<stem>``."""
    d, base = posixpath.split(rel)
    if base in ("lib.rs", "main.rs", "mod.rs"):
        return d
    return posixpath.join(d, base[:-3]) if d else base[:-3]


def _rs_module_file(d: str, fileset: Dict[str, int]) -> Optional[str]:
    cands = [d + ".rs", posixpath.join(d, "mod.rs")] if d else []
    cands += [posixpath.join(d, "lib.rs") if d else "lib.rs"]
    cands += [posixpath.join(d, "main.rs") if d else "main.rs"]
    for c in cands:
        if c in fileset:
            return c
    return None


def _resolve_rust(wt, rels, index_of, by_lang, rec_of, add) -> None:
    for i, rel, rec in by_lang.get("rust") or ():
        mdir = _rs_mod_dir(rel)
        for spec in rec.get("specs") or ():
            try:
                if spec[0] == "mod":
                    base = posixpath.join(mdir, spec[1]) if mdir else spec[1]
                    for c in (base + ".rs", posixpath.join(base, "mod.rs")):
                        if c in index_of:
                            add(i, c)
                            break
                    continue
                path, names, braced = spec[1], spec[2], spec[3]
                segs = path.split("::")
                head, segs = segs[0], segs[1:]
                if head == "crate":
                    base = _rs_crate_root(posixpath.dirname(rel), index_of)
                    if base is None:
                        continue
                elif head == "super":
                    base = posixpath.dirname(mdir)
                    while segs and segs[0] == "super":
                        base = posixpath.dirname(base)
                        segs = segs[1:]
                else:
                    base = mdir
                hit = None
                rest: List[str] = []
                for k in range(len(segs), 0, -1):
                    p = posixpath.join(base, *segs[:k]) if base else "/".join(segs[:k])
                    hit = _rs_module_file(p, index_of) if p else None
                    if hit:
                        rest = segs[k:]
                        break
                if hit is None:
                    hit = _rs_module_file(base, index_of)
                    rest = segs
                if hit is None:
                    continue
                nm = list(rest[:1]) if rest else []
                if braced and not rest:
                    nm = list(names)
                add(i, hit, nm)
            except Exception:  # noqa: BLE001
                continue


def _resolve_c(wt, rels, index_of, by_lang, rec_of, add) -> None:
    """``#include "x.h"`` relative to the includer, then (quoted or angled)
    a unique suffix match, then a unique basename. Names = the identifiers
    the header declares that the includer mentions. Umbrella headers (a
    header that only includes others) are followed transitively, so a file
    including ``lib.h`` is credited with the ``lib/res.h`` symbols it
    actually uses."""
    items = by_lang.get("c")
    if not items:
        return
    by_base: Dict[str, List[str]] = {}
    for _i, rel, _rec in items:
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)

    def resolve(rel: str, inc: str, quoted: bool) -> Optional[str]:
        if quoted:
            cand = posixpath.normpath(posixpath.join(posixpath.dirname(rel), inc))
            if cand in index_of:
                return cand
        lst = by_base.get(inc.rsplit("/", 1)[-1]) or []
        suf = [r for r in lst if r == inc or r.endswith("/" + inc)]
        if len(suf) == 1:
            return suf[0]
        if len(suf) > 1:
            # Several same-named headers: the one nearest the includer.
            def common(r: str) -> int:
                return len(posixpath.commonprefix([r, rel]).rsplit("/", 1)[0])

            ranked = sorted(suf, key=lambda r: (-common(r), r))
            if common(ranked[0]) > common(ranked[1]):
                return ranked[0]
        return None

    inc_of: Dict[str, List[str]] = {}
    for _i, rel, rec in items:
        out = []
        for inc, quoted in rec.get("specs") or ():
            t = resolve(rel, inc, quoted)
            if t and t != rel:
                out.append(t)
        inc_of[rel] = out
    for i, rel, rec in items:
        toks = rec.get("toks") or frozenset()
        direct = inc_of.get(rel) or []
        for h in direct:
            add(i, h, toks & ((rec_of.get(h) or {}).get("defs") or frozenset()))
        seen = set(direct) | {rel}
        frontier = list(direct)
        for _depth in range(4):
            nxt = []
            for h in frontier:
                for h2 in inc_of.get(h) or ():
                    if h2 in seen or not h2.lower().endswith(_C_HEADER_EXTS):
                        continue
                    seen.add(h2)
                    nxt.append(h2)
                    nm = toks & ((rec_of.get(h2) or {}).get("defs") or frozenset())
                    if nm:
                        add(i, h2, nm)
            frontier = nxt[:64]
            if not frontier or len(seen) > 256:
                break


def _resolve_hcl(wt, rels, index_of, by_lang, rec_of, add) -> None:
    """Terraform/Terragrunt: a module ``source = "../x"`` / ``config_path``
    links to every ``.tf``/``.hcl`` file of that dir; names = the target's
    ``variable``/``output`` names the caller sets or reads."""
    items = by_lang.get("hcl")
    if not items:
        return
    tf_dirs: Dict[str, List[str]] = {}
    for _i, rel, _rec in items:
        tf_dirs.setdefault(posixpath.dirname(rel), []).append(rel)
    for i, rel, rec in items:
        uses = rec.get("uses") or frozenset()
        for spec in rec.get("specs") or ():
            d = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
            if d == ".":
                d = ""
            if d.startswith(".."):
                continue
            for t in tf_dirs.get(d) or ():
                add(i, t, uses & ((rec_of.get(t) or {}).get("defs") or frozenset()))


_ROUTE_DYN_RE = re.compile(r"^(?:\{.*\}|<.*>|:.+|\*.*|\[.*\])$")


def _route_regex(route: str) -> Optional["re.Pattern[str]"]:
    """A server route as a regex over client literals: dynamic segments
    (``{id}``, ``<int:id>``, ``:id``, ``*``, ``[id]``) match one segment."""
    segs = [s for s in route.split("/") if s]
    if not segs:
        return None
    parts = ["[^/]+" if _ROUTE_DYN_RE.match(s) else re.escape(s) for s in segs]
    return re.compile("/" + "/".join(parts) + "$")


def _resolve_api(scanned: List[Tuple[int, str, str, dict]], add) -> int:
    """API edges: a JS/TS string literal naming a server route (≥ 2 fixed
    segments on both sides) links the client file to the file declaring the
    route, named ``"METHOD /route"``. Without these a full-stack repo's root
    has no relations at all — the frontend reaches the backend over HTTP,
    never by import. A literal that matches no route exactly may still match
    as a suffix (``/proxy/api/x`` → ``/api/x``), but only when that is
    unambiguous (≤ 3 routes). Test files are neither clients nor servers."""
    routes = []
    for i, rel, _lang, rec in scanned:
        if _is_test_path(rel):
            continue
        for e in rec.get("entry") or ():
            if e.get("kind") != "http":
                continue
            r = e.get("route") or ""
            if not r.startswith("/"):
                continue
            segs = [s for s in r.split("/") if s]
            if sum(1 for s in segs if not _ROUTE_DYN_RE.match(s)) < 2:
                continue
            rx = _route_regex(r)
            if rx is not None:
                meth = (e.get("method") or "ANY").split("/")[0]
                routes.append((rx, meth + " " + r, i))
    if not routes:
        return 0
    api = 0
    hits_of: Dict[str, list] = {}
    for i, rel, lang, rec in scanned:
        if lang != "js" or not rec.get("lits") or _is_test_path(rel):
            continue
        for lit in rec["lits"]:
            hits = hits_of.get(lit)
            if hits is None:
                hits = [r for r in routes if r[0].match(lit)]
                if not hits:
                    part = [r for r in routes if r[0].search(lit)]
                    hits = part if len(part) <= 3 else []
                hits_of[lit] = hits
            for _rx, name, j in hits:
                if add(i, j, (name,)):
                    api += 1
    return api


def blast(
    edges: Iterable[Sequence[int]], n: int, seeds: Iterable[int], depth: int
) -> Dict[int, int]:
    """``{idx: depth}`` — the files that (transitively) import the seeds, i.e.
    what a change to the seeds can break: depth 1 = direct importers, 2 =
    their importers, … (``depth`` is clamped to 1..3). Walks REVERSE edges;
    the seeds themselves are not in the result, and each file keeps the
    shallowest depth it is reached at."""
    try:
        depth = max(1, min(3, int(depth)))
        rev: Dict[int, List[int]] = {}
        for e in edges:
            s, d = int(e[0]), int(e[1])
            if 0 <= s < n and 0 <= d < n:
                rev.setdefault(d, []).append(s)
        seen = {int(s) for s in seeds if 0 <= int(s) < n}
        frontier = list(seen)
        out: Dict[int, int] = {}
        for level in range(1, depth + 1):
            nxt = []
            for x in frontier:
                for imp in rev.get(x, ()):
                    if imp in seen:
                        continue
                    seen.add(imp)
                    out[imp] = level
                    nxt.append(imp)
            frontier = nxt
            if not frontier:
                break
        return out
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# Tool feed
# --------------------------------------------------------------------------- #

_FEED_TAIL_BYTES = 512 * 1024


def read_feed(tmux_name: str, since: float = 0.0, limit: int = 500) -> List[dict]:
    """The session's tool-feed records with ``ts > since``, oldest first, at
    most ``limit`` (the newest ones).

    Tail-reads the last 512 KB only — the feed is append-only and trimmed by
    :func:`trim_feed`, but a poll must never pay for its whole history. When
    the read starts mid-file the first (partial) line is skipped; any line
    that doesn't parse (a torn write, a record the hook cut at its 16 KB line
    cap, a future format) is skipped too — per LINE, and on ANY exception
    (a pathologically nested line raises ``RecursionError``, not
    ``ValueError``), so one bad line can never cost the records around it.
    Records are stably sorted by ``ts`` because parallel hooks may append
    slightly out of order. A feed path that isn't a regular file (a FIFO
    planted in the feed dir) reads as empty rather than blocking."""
    try:
        path = _feed_path(tmux_name)
        fh = _open_regular(path)
        if fh is None:
            return []
        with fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - _FEED_TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
        if start > 0:
            nl = data.find(b"\n")
            data = data[nl + 1 :] if nl >= 0 else b""
        out: List[dict] = []
        for line in data.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001 — torn/cut/hostile line: skip it
                continue
            if not isinstance(rec, dict):
                continue
            ts = rec.get("ts")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            if ts <= since:
                continue
            out.append(rec)
        out.sort(key=lambda r: r["ts"])
        if limit and limit > 0 and len(out) > limit:
            out = out[-limit:]
        return out
    except Exception:  # noqa: BLE001
        return []


def trim_feed(
    tmux_name: str, max_bytes: int = 2_000_000, keep_bytes: int = 512_000
) -> bool:
    """Cut the feed back to its last ``keep_bytes`` (on a line boundary) once
    it exceeds ``max_bytes``. Atomic replace (temp file in the same dir +
    ``os.replace``), mode 0600, so a concurrent reader sees the old file or
    the new one, never a torn one. Returns True when it trimmed.

    NO RECORD MAY BE LOST. The feed is not just a display trail: it is the
    monitor's only source of ``deny`` records (→ ``session.red_zone_blocked``)
    and of Bash-backstop ``breach`` records. The hook appends by PATH (one
    ``O_APPEND`` write per record, no lock), so a record written after our
    last read of the old file but before the rename lands in the OLD inode,
    which the rename unlinks. So the old file stays OPEN across the replace
    (we are then its only holder) and whatever reached it after the catch-up
    read — during the fsync/chmod/rename window, or from a hook that opened
    the path just before the rename — is read back from that handle and
    appended to the new file. Those records may land after newer ones;
    :func:`read_feed` sorts by ``ts``."""
    try:
        path = _feed_path(tmux_name)
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size <= max_bytes:
            return False
        old = _open_regular(path)
        if old is None:
            return False
        with old:
            old.seek(max(0, size - keep_bytes))
            data = old.read()
            nl = data.find(b"\n")
            data = data[nl + 1 :] if nl >= 0 else b""
            d = os.path.dirname(path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".trim-", suffix=".jsonl")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.write(old.read())  # catch up on appends since the tail read
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
            # Carry over what the old inode got after the catch-up. The second
            # pass (after a beat) is for a hook that opened the path before the
            # rename but was descheduled before its write.
            try:
                for delay in (0.0, _TRIM_SETTLE_S):
                    if delay:
                        time.sleep(delay)
                    _append_bytes(path, old.read())
            except Exception:  # noqa: BLE001 — the trim itself happened
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


_TRIM_SETTLE_S = 0.05


def _append_bytes(path: str, data: bytes) -> None:
    """Append ``data`` to ``path`` with ``O_APPEND`` (the hook's own mode, so
    the two interleave by whole writes). No-op for empty data."""
    if not data:
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                break
            view = view[n:]
    finally:
        os.close(fd)


def gc_snaps(max_age_s: float = 3600) -> int:
    """Delete Bash stat-diff snapshots (``feed_dir()/.snap/*.json``) older
    than ``max_age_s``. A snap normally dies with its Post hook; one survives
    only when the tool never finished (killed CLI, interrupted turn). Returns
    how many were removed."""
    removed = 0
    try:
        snap_dir = os.path.join(_feed_dir(), ".snap")
        now = time.time()
        with os.scandir(snap_dir) as it:
            for entry in it:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime > max_age_s:
                        os.unlink(entry.path)
                        removed += 1
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return removed


def feed_transcript_path(records: Iterable[dict]) -> Optional[str]:
    """The newest ``tp`` (hook payload ``transcript_path``) in the records —
    the EXACT transcript file of the conversation the hooks are firing for,
    which beats any directory-slug guess (slugs truncate past 200 chars)."""
    best: Optional[str] = None
    best_ts = float("-inf")
    try:
        for r in records:
            tp = r.get("tp") if isinstance(r, dict) else None
            if not isinstance(tp, str) or not tp:
                continue
            ts = r.get("ts")
            ts = float(ts) if isinstance(ts, (int, float)) else float("-inf")
            if ts >= best_ts:
                best, best_ts = tp, ts
    except Exception:  # noqa: BLE001
        return best
    return best


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #

PLAN_TAG = "mindflock-plan"
_MAX_PLAN_ITEMS = 200
_PLAN_FENCE_RE = re.compile(
    r"^[ \t]*(`{3,}|~{3,})[ \t]*mindflock-plan\b[^\n]*\n(.*?)(?=^[ \t]*\1[ \t]*$|\Z)",
    re.M | re.S,
)
_BULLET_RE = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
_CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s+")
_SEP_RE = re.compile(r"^(?:[—–]+|-{1,2}>?|=>|:)\s*")
# Location suffixes on a path: `:12`, `:12:5`, `:40-55`, GitHub `#L40`,
# `#L40-L55` — a planned file cited with a range is still that file (left on,
# `views.py:40-55` became a ghost tile and the real edit read as off-plan).
_LINENO_RE = re.compile(
    r"(?:(?::\d+(?:-\d+)?){1,2}|#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?)$"
)
_TABLE_SEP_RE = re.compile(r"^:?-{2,}:?$")
_LEAD_STRIP = "`*()[],;:."
_FILE_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")
_URL_RE = re.compile(r"\b[a-zA-Z][\w+.-]*://\S+")
_PATH_TOKEN_RE = re.compile(r"[\w@.+~/\\$\[\]-]+")


def last_plan_block(text: Optional[str]) -> Optional[str]:
    """The body of the LAST ```` ```mindflock-plan ```` fence in ``text``
    (an unterminated final fence runs to the end), or None."""
    if not text or PLAN_TAG not in text:
        return None
    last = None
    for m in _PLAN_FENCE_RE.finditer(text):
        last = m.group(2)
    return last


def _norm_plan_path(p: str, wt: str) -> Optional[str]:
    """A worktree-relative POSIX path, or None when ``p`` is not a usable
    path inside the worktree (URL, home path, escapes via ``..``, absolute
    outside ``wt``). Absolute paths under ``wt`` (lexically or after
    realpath) are relativized; ``a.py:12`` line suffixes are dropped."""
    p = (p or "").strip().replace("\\", "/")
    if not p or "://" in p or p.startswith("~"):
        return None
    p = _LINENO_RE.sub("", p)
    if p.startswith("/"):
        rel = None
        roots = [wt]
        try:
            roots.append(os.path.realpath(wt))
        except Exception:  # noqa: BLE001
            pass
        cands = [p]
        try:
            cands.append(os.path.realpath(p))
        except Exception:  # noqa: BLE001
            pass
        for c in cands:
            for root in roots:
                if not root:
                    continue
                r = os.path.relpath(c, root).replace("\\", "/")
                if r != ".." and not r.startswith("../"):
                    rel = r
                    break
            if rel is not None:
                break
        if rel is None:
            return None
        p = rel
    while p.startswith("./"):
        p = p[2:]
    p = posixpath.normpath(p) if p else ""
    if not p or p in (".", "..") or p.startswith("../"):
        return None
    return p


def _pathish(tok: str) -> bool:
    """Whether a bare token LOOKS like a path (has a ``/`` or a file
    extension) once markdown and trailing punctuation are peeled off."""
    t = tok.strip(_LEAD_STRIP)
    return bool(t) and ("/" in t or bool(_FILE_EXT_RE.search(t)))


def _drop_lead_words(s: str) -> str:
    """Drop up to two leading NON-path words (``✅``, ``NEW:``, ``(new)``,
    ``Create``, ``M``) when a path-like token follows — agents decorate plan
    lines like that, and keeping the decoration as the "path" lost the real
    file (a ghost tile plus a false off-plan flag on the actual edit). A
    line whose second token isn't path-like (``Makefile — build``) is left
    alone."""
    for _ in range(2):
        m = re.match(r"^(\S+)\s+(\S+)", s)
        if not m:
            break
        first = m.group(1)
        if "/" in first or "." in first or "\\" in first or first.startswith("`"):
            break
        if not _pathish(m.group(2)):
            break
        s = s[m.start(2) :]
    return s


def _clean_tok(tok: str) -> str:
    """Trailing list punctuation off a path token: ``,`` ``;`` ``:`` and a
    sentence-ending ``.`` after a file extension (``README.md.``)."""
    tok = tok.strip().rstrip(",;")
    if tok.endswith(":"):
        tok = tok[:-1]
    if tok.endswith(".") and _FILE_EXT_RE.search(tok[:-1]):
        tok = tok[:-1]
    return tok


def _plan_line(line: str) -> Optional[Tuple[List[str], str]]:
    """``([raw_path, …], intent)`` for one plan line, any of: ``path —
    intent``, ``path - intent``, ``path: intent``, ``- path (intent)``,
    ``path``, ``a.py, b.py — intent`` (several paths, one intent), a
    markdown table row ``| path | intent |``; markdown (``**``, backticks,
    bullets, checkboxes), decorations before the path (``✅``, ``NEW:``,
    ``(new)``, a verb) and location suffixes (``:40-55``, ``#L12``)
    stripped. None for blank lines, headings, comments and table rulers."""
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("//"):
        return None
    if s.startswith("|"):
        cells = [c.strip() for c in s.strip("|").split("|")]
        cells = [c for c in cells if c]
        if not cells or all(_TABLE_SEP_RE.match(c.replace(" ", "")) for c in cells):
            return None
        s = cells[0] + (" — " + " | ".join(cells[1:]) if len(cells) > 1 else "")
    s = _BULLET_RE.sub("", s, count=1)
    s = _CHECKBOX_RE.sub("", s, count=1)
    s = s.replace("**", "").strip()
    s = _drop_lead_words(s)
    m = re.match(r"^`([^`]+)`(.*)$", s)
    if m:
        tok, rest = m.group(1).strip(), m.group(2)
    else:
        s = s.replace("`", "")
        m = re.match(r"^(\S+)(.*)$", s)
        if not m:
            return None
        tok, rest = m.group(1), m.group(2)
        for dash in ("—", "–"):
            if dash in tok:
                tok, _, tail = tok.partition(dash)
                rest = dash + tail + rest
                break
    more = tok.rstrip().endswith(",")
    if not more and rest.lstrip().startswith(","):
        more, rest = True, rest.lstrip()[1:]
    toks = [_clean_tok(tok)]
    while more:
        m2 = re.match(r"^\s*`?([^\s`,]+)`?\s*(,?)", rest)
        if not m2 or not _pathish(m2.group(1)):
            break
        toks.append(_clean_tok(m2.group(1)))
        rest = rest[m2.end() :]
        more = bool(m2.group(2))
    intent = rest.replace("`", "").replace("**", "").strip()
    intent = _SEP_RE.sub("", intent, count=1).strip()
    if intent.startswith("(") and intent.endswith(")"):
        intent = intent[1:-1].strip()
    toks = [t for t in toks if t]
    if not toks:
        return None
    return toks, intent[:300]


def _present(rel: str, file_set: Optional[Set[str]], wt: str) -> bool:
    if file_set is not None:
        if rel in file_set:
            return True
        try:
            return os.path.isdir(os.path.join(wt, rel))
        except Exception:  # noqa: BLE001
            return False
    try:
        return os.path.lexists(os.path.join(wt, rel))
    except Exception:  # noqa: BLE001
        return False


def _as_set(file_set: Any) -> Optional[Set[str]]:
    if file_set is None:
        return None
    if isinstance(file_set, (set, frozenset, dict)):
        return file_set  # type: ignore[return-value]
    return {_row_rel(f) for f in file_set}


def parse_plan_block(
    text: Optional[str], file_set: Any, wt: str
) -> Optional[List[dict]]:
    """``[{"path", "intent", "new"}]`` from the LAST ``mindflock-plan`` fence
    in ``text`` (an assistant message), or None when there is no such fence.

    Each line is one of ``path — intent`` / ``path - intent`` /
    ``path: intent`` / ``- path (intent)`` / ``path``, markdown stripped.
    Paths are made worktree-relative (absolute ones under ``wt``; absolute
    ones outside it are dropped — the Map can't draw them). A bare word with
    neither ``/`` nor ``.`` is kept only if it exists (``Makefile``), so a
    stray prose line doesn't become a ghost tile. ``new`` = not in
    ``file_set`` (rows / rels / a set; ``None`` = check the disk instead).
    Deduped by path, first intent wins, capped at 200."""
    block = last_plan_block(text)
    if block is None:
        return None
    return _plan_items_from_block(block, _as_set(file_set), wt)


def _plan_items_from_block(block: str, fs: Optional[Set[str]], wt: str) -> List[dict]:
    items: List[dict] = []
    seen: Set[str] = set()
    for line in block.splitlines():
        parsed = _plan_line(line)
        if parsed is None:
            continue
        raws, intent = parsed
        for raw in raws:
            rel = _norm_plan_path(raw, wt)
            if rel is None or rel in seen:
                continue
            if any(ch.isspace() for ch in rel):
                continue
            if "/" not in rel and "." not in rel and not _present(rel, fs, wt):
                continue
            seen.add(rel)
            items.append(
                {"path": rel, "intent": intent, "new": not _present(rel, fs, wt)}
            )
            if len(items) >= _MAX_PLAN_ITEMS:
                return items
    return items


def exitplan_items(plan_text: Optional[str], file_set: Any, wt: str) -> List[dict]:
    """``[{"path", "intent", "new"}]`` pulled out of a free-form plan (Claude
    plan mode's ``ExitPlanMode`` markdown), which names files in prose.

    Path-like tokens are taken after stripping markdown and URLs, and kept
    when they are in the file set (existing files — an extensionless one
    like ``Makefile`` too, when set off in backticks/bold or capitalized),
    or — for files the plan will create — when they contain a ``/``, aren't
    an existing dir and their parent dir exists. A root-level new file (no
    ``/``) is kept only when the plan set it off in backticks or bold with a
    file extension, so prose like "e.g." or "Node.js" doesn't become a ghost
    tile. The intent is the rest of the line after the path."""
    fs = _as_set(file_set)
    items: List[dict] = []
    seen: Set[str] = set()
    try:
        for raw_line in (plan_text or "").splitlines():
            marked = set()
            for m in re.finditer(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*", raw_line):
                span = (m.group(1) or m.group(2) or "").strip()
                if span:
                    marked.add(span)
            line = _URL_RE.sub(" ", raw_line)
            line = line.replace("**", "").replace("`", "")
            line = _BULLET_RE.sub("", line.strip(), count=1)
            for m in _PATH_TOKEN_RE.finditer(line):
                tok = m.group(0).rstrip(".,;:!?")
                if len(tok) < 2:
                    continue
                if "/" not in tok and "." not in tok:
                    # A bare word (no `/`, no `.`) is a file only when it EXISTS
                    # and the plan names it like one — set off in backticks /
                    # bold, or capitalized like Makefile / Dockerfile / LICENSE
                    # — so prose words that happen to name a script ("build",
                    # "run") stay prose. Existence is checked FIRST: requiring a
                    # `/` or `.` up front dropped every extensionless file.
                    if tok not in marked and not tok[:1].isupper():
                        continue
                    if not ((tok in fs) if fs is not None else _is_file(wt, tok)):
                        continue
                rel = _norm_plan_path(tok, wt)
                if rel is None or rel in seen:
                    continue
                exists = (rel in fs) if fs is not None else _is_file(wt, rel)
                if not exists:
                    if _is_dir(wt, rel) or not _FILE_EXT_RE.search(rel):
                        continue
                    if "/" in rel:
                        if not _is_dir(wt, posixpath.dirname(rel)):
                            continue
                    elif tok not in marked and rel not in marked:
                        continue
                seen.add(rel)
                rest = line[m.end() :].strip()
                rest = _SEP_RE.sub("", rest.lstrip(".,;:!?)"), count=1).strip()
                items.append(
                    {
                        "path": rel,
                        "intent": rest[:300],
                        "new": not _present(rel, fs, wt),
                    }
                )
                if len(items) >= _MAX_PLAN_ITEMS:
                    return items
    except Exception:  # noqa: BLE001
        return items
    return items


def _is_file(wt: str, rel: str) -> bool:
    try:
        return os.path.isfile(os.path.join(wt, rel))
    except Exception:  # noqa: BLE001
        return False


def _is_dir(wt: str, rel: str) -> bool:
    try:
        return os.path.isdir(os.path.join(wt, rel) if rel else wt)
    except Exception:  # noqa: BLE001
        return False


# (tmux_name, thread) -> {"declared": {"block", "ts"}, "exitplan": {"ts", "paths"}}
_PLAN_LATCH: Dict[tuple, dict] = {}
_PLAN_LATCH_MAX = 256
_TP_LATCH: Dict[str, str] = {}  # tmux_name -> last transcript path seen in its feed


def _provider_for(inst: Any) -> Any:
    """The session's provider (tests monkeypatch this)."""
    from backend import providers

    return providers.resolve(getattr(inst, "Program", "") or "")


def _thread_id(prov: Any, tmux_name: str, records: Sequence[dict]) -> str:
    """The conversation the window is running: the provider's recorded
    thread id, else the stem of the newest hook ``transcript_path``."""
    tid = ""
    fn = getattr(prov, "resume_thread_id", None)
    if callable(fn):
        try:
            tid = fn(tmux_name) or ""
        except Exception:  # noqa: BLE001
            tid = ""
    if not tid:
        tp = feed_transcript_path(records)
        if tp:
            tid = os.path.splitext(os.path.basename(tp))[0]
    return str(tid)


def _declared_text(
    prov: Any, tmux_name: str, wt: str, tp: Optional[str]
) -> Optional[str]:
    fn = getattr(prov, "last_assistant_text", None)
    if not callable(fn):
        return None
    try:
        text = fn(tmux_name, wt, contains=PLAN_TAG, transcript_path=tp)
    except Exception:  # noqa: BLE001
        return None
    return text if isinstance(text, str) else None


_TRANSCRIPT_TAIL = 8 * 1024 * 1024  # the provider's own transcript tail window


def _entry_texts(obj: Any) -> List[str]:
    """Every text body of one ASSISTANT transcript entry (a string content,
    or each ``{"type": "text"}`` block of a list content)."""
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return []
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [content]
    out: List[str] = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str) and t:
                    out.append(t)
    return out


def _declared_ts(tp: Optional[str], block: str) -> Optional[float]:
    """Epoch time the agent WROTE ``block``: the ``timestamp`` of the
    transcript entry where it first appeared, or None (no path, unreadable,
    not found, no parseable timestamp — the caller then falls back to
    first-seen time).

    WHY. The declared plan's ``ts`` is what off-plan ("edited after the
    plan"), "Go — with N red zones" (zones added since the plan) and the
    Plan/Watch mode all compare against. Stamping it with the time this
    process first SAW the block made the answer depend on whether the Map
    happened to be open when the agent wrote it: a restart, or a first look
    after work began, re-stamped the plan to "now", later than every edit —
    off-plan emptied, staged zones fell out of Go, and an older declared
    plan beat a newer ExitPlanMode one.

    "First appeared" mirrors the latch: walking back from the newest
    assistant entry carrying ``block``, identical re-emissions extend the run
    and the first DIFFERENT plan block ends it; the oldest entry in the run
    wins. Reads the last 8 MB only, and only when a new block is latched."""
    if not tp or not block:
        return None
    try:
        from backend.providers._timeparse import ts_epoch

        fh = _open_regular(tp)
        if fh is None:
            return None
        with fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - _TRANSCRIPT_TAIL)
            fh.seek(start)
            data = fh.read()
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line is cut mid-entry
        tag = PLAN_TAG.encode()
        found: Optional[float] = None
        matched = False
        for raw in reversed(lines):
            if tag not in raw or b"assistant" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            blocks = [b for b in (last_plan_block(t) for t in _entry_texts(obj)) if b]
            if not blocks:
                continue
            if block in blocks:
                matched = True
                ts = ts_epoch(obj.get("timestamp"))
                if ts is not None:
                    found = ts
            elif matched:
                break  # an older, different plan: the run started after it
        return found
    except Exception:  # noqa: BLE001
        return None


def _newest_plan_record(records: Sequence[dict], thread: str) -> Optional[dict]:
    """Newest ``kind == "plan"`` feed record with plan text that provably
    belongs to the CURRENT conversation — an old conversation's plan must not
    resurface after /clear.

    The hook stamps ``tp`` (the transcript path) on ``pre`` records only, so
    a ``post`` (the plan approved) inherits the ``tp`` of its ``pre`` by
    tool-use ``id``. When the thread is known, a record whose conversation is
    still unknown is SKIPPED, not trusted: the tp-less ``post`` of conversation
    A's approved plan was otherwise the newest record after /clear and got
    latched as B's plan (every file B then touched read as off-plan).
    ``fail`` records (a rejected or interrupted plan) are never the plan."""
    tp_by_id: Dict[str, str] = {}
    for r in records:
        if not isinstance(r, dict) or r.get("ev") != "pre":
            continue
        rid, rtp = r.get("id"), r.get("tp")
        if isinstance(rid, str) and rid and isinstance(rtp, str) and rtp:
            tp_by_id[rid] = rtp
    best = None
    for r in records:
        if not isinstance(r, dict) or r.get("kind") != "plan":
            continue
        if r.get("ev") == "fail":
            continue
        plan = r.get("plan")
        ts = r.get("ts")
        if not isinstance(plan, str) or not plan.strip():
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        tp = r.get("tp")
        if not (isinstance(tp, str) and tp):
            rid = r.get("id")
            tp = tp_by_id.get(rid) if isinstance(rid, str) else None
        if thread and (not tp or thread not in os.path.basename(tp)):
            continue
        if best is None or ts >= best["ts"]:
            best = r
    return best


def current_plan(
    inst: Any,
    tmux_name: str,
    wt: str,
    records: Sequence[dict],
    files: Any = None,
) -> dict:
    """``{"source": "declared"|"exitplan"|None, "ts": float|None,
    "items": [{"path", "intent", "new"}], "thread": str}`` — the plan the
    agent is currently working to.

    Two sources, newest wins:
    * **declared** — the LAST ``mindflock-plan`` fence in the newest assistant
      message containing that tag, via
      ``provider.last_assistant_text(tmux, wt, contains="mindflock-plan",
      transcript_path=<newest hook tp>)`` (the provider's
      ``(path, mtime, size)`` cache makes the repeat scan free). Its ``ts`` is
      when the agent WROTE the block — the ``timestamp`` of its transcript
      entry, read from the hook ``tp`` (:func:`_declared_ts`) — so a restart
      or a late first look at the Map doesn't move it; only when that can't
      be read (no ``tp``, no timestamp) is it when THIS process first saw it.
    * **exitplan** — the newest ``kind=="plan"`` feed record (Claude plan
      mode's ``ExitPlanMode``), paths pulled out of its prose.

    LATCHED per ``(tmux_name, thread)``: once seen, a plan stays until a newer
    block/record replaces it, so it survives the feed being trimmed, the
    caller passing only ``since``-filtered records, or the block scrolling
    out of the transcript tail. A new thread (``/clear``, relaunch) starts
    empty. ``new`` is recomputed every call against ``files`` (rows / rels /
    set; ``None`` = the disk) so a ghost tile turns solid once the file
    exists."""
    thread = ""
    try:
        records = [r for r in (records or []) if isinstance(r, dict)]
        prov = _provider_for(inst)
        thread = _thread_id(prov, tmux_name, records)
        key = (tmux_name, thread)
        tp = feed_transcript_path(records)
        with _LOCK:
            if tp:
                _TP_LATCH[tmux_name] = tp
                if len(_TP_LATCH) > _PLAN_LATCH_MAX:
                    _TP_LATCH.pop(next(iter(_TP_LATCH)))
            else:
                tp = _TP_LATCH.get(tmux_name)
            latch = dict(_PLAN_LATCH.get(key) or {})
        if tp and thread and thread not in os.path.basename(tp):
            # The newest hook fire predates the current conversation (/clear
            # before any tool call): that transcript is the OLD thread's, and
            # its plan must not latch under the new one. Let the provider
            # resolve this window's transcript itself.
            tp = None
        fs = _as_set(files)

        block = last_plan_block(_declared_text(prov, tmux_name, wt, tp))
        if block is not None:
            prev = latch.get("declared")
            if not prev or prev.get("block") != block:
                # When the agent wrote it (the transcript entry's own time),
                # else when this process first saw it.
                wrote = _declared_ts(tp, block)
                latch["declared"] = {
                    "block": block,
                    "ts": wrote if wrote is not None else time.time(),
                }

        rec = _newest_plan_record(records, thread)
        if rec is not None:
            prev = latch.get("exitplan")
            if not prev or float(rec["ts"]) > prev.get("ts", 0.0):
                paths = [
                    (it["path"], it["intent"])
                    for it in exitplan_items(rec["plan"], fs, wt)
                ]
                latch["exitplan"] = {"ts": float(rec["ts"]), "paths": paths}

        with _LOCK:
            if latch:
                _PLAN_LATCH[key] = latch
                while len(_PLAN_LATCH) > _PLAN_LATCH_MAX:
                    _PLAN_LATCH.pop(next(iter(_PLAN_LATCH)))

        dec = latch.get("declared")
        ex = latch.get("exitplan")
        if dec and (not ex or dec["ts"] >= ex["ts"]):
            items = _plan_items_from_block(dec["block"], fs, wt)
            return {
                "source": "declared",
                "ts": dec["ts"],
                "items": items,
                "thread": thread,
            }
        if ex:
            items = [
                {"path": p, "intent": intent, "new": not _present(p, fs, wt)}
                for p, intent in ex["paths"]
            ]
            return {
                "source": "exitplan",
                "ts": ex["ts"],
                "items": items,
                "thread": thread,
            }
    except Exception:  # noqa: BLE001
        pass
    return {"source": None, "ts": None, "items": [], "thread": thread}


def forget_plan(tmux_name: str) -> None:
    """Drop every latched plan (all threads) and the remembered transcript
    path for a window — for the session DELETE path, since titles (and so
    tmux names) are reused."""
    with _LOCK:
        for k in [k for k in _PLAN_LATCH if k[0] == tmux_name]:
            _PLAN_LATCH.pop(k, None)
        _TP_LATCH.pop(tmux_name, None)


_NESTED_WT_RE = re.compile(r"^\.claude/worktrees/[^/]+/")


def _rel_in_wt(p: Any, wt: Optional[str]) -> Optional[str]:
    """A feed path (absolute, or already made repo-relative by the route) as
    a worktree-relative path, or None when it lies outside ``wt``. A leading
    ``.claude/worktrees/<name>/`` (Claude's ``EnterWorktree`` sandbox) is
    stripped, as the hook does before zone matching — an edit there is an
    edit to the same logical file."""
    if not isinstance(p, str) or not p:
        return None
    if not p.startswith("/") and not os.path.isabs(p):
        rel = _norm_plan_path(p, wt or "")
    elif not wt:
        return None
    else:
        rel = _norm_plan_path(p, wt)
    if rel and rel.startswith(".claude/worktrees/"):
        rel = _NESTED_WT_RE.sub("", rel) or None
    return rel


def off_plan(
    plan: Optional[dict],
    changed: Iterable[Any],
    records: Iterable[dict],
    wt: Optional[str] = None,
) -> List[str]:
    """Files edited AFTER the plan's ``ts`` that no plan item covers (an item
    covers its own path and, for a directory item, everything under it).
    Empty when there is no plan or it has no items.

    "Edited" = the ``writes`` of feed records newer than the plan (pre or
    post; denied attempts and tool calls that then failed don't count; dirs
    are skipped) plus changed files (``changed_files`` dicts or rels). With
    ``wt`` given, a changed file counts only if its mtime — or, for a deleted
    file, its parent dir's — is newer than the plan, so work from before the
    plan isn't flagged; without ``wt`` every changed file counts and absolute
    feed paths can't be placed (pass ``wt``)."""
    try:
        if not plan or not plan.get("source") or not plan.get("items"):
            return []
        ts = float(plan.get("ts") or 0.0)
        planned = {it.get("path") for it in plan["items"] if it.get("path")}

        def covered(rel: str) -> bool:
            if rel in planned:
                return True
            parts = rel.split("/")
            for k in range(1, len(parts)):
                if "/".join(parts[:k]) in planned:
                    return True
            return False

        recs = [r for r in (records or []) if isinstance(r, dict)]
        failed = {r.get("id") for r in recs if r.get("ev") == "fail" and r.get("id")}
        edited: Set[str] = set()
        for r in recs:
            rts = r.get("ts")
            if isinstance(rts, bool) or not isinstance(rts, (int, float)) or rts <= ts:
                continue
            if r.get("deny") or r.get("ev") not in ("pre", "post"):
                continue
            if r.get("id") and r.get("id") in failed:
                continue
            for w in r.get("writes") or []:
                rel = _rel_in_wt(w, wt)
                if rel and not (wt and _is_dir(wt, rel)):
                    edited.add(rel)
        for c in changed or []:
            rel = c.get("path") if isinstance(c, dict) else c
            rel = _rel_in_wt(rel, wt) if isinstance(rel, str) else None
            if not rel:
                continue
            if wt:
                try:
                    mt = os.lstat(os.path.join(wt, rel)).st_mtime
                except OSError:
                    parent = posixpath.dirname(rel)
                    try:
                        mt = os.stat(
                            os.path.join(wt, parent) if parent else wt
                        ).st_mtime
                    except OSError:
                        continue
                if mt <= ts:
                    continue
            edited.add(rel)
        return sorted(p for p in edited if not covered(p))
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# Zone dry-run + other agents on the same repo
# --------------------------------------------------------------------------- #

_PREVIEW_SAMPLE = 20


def changed_matching(inst: Any, wt: str, re_src: str) -> List[str]:
    """The session's changed files (vs its fork point, committed + working —
    :func:`changed_files`) that ``re_src`` matches, sorted. What the add-zone
    route reports as ``already_changed`` and seeds as pre-existing breaches, so
    creating a zone over work already done never announces that work as a
    fresh breach. ``[]`` without an ``inst`` or on any failure."""
    if inst is None or not re_src:
        return []
    try:
        from backend.config import red_zones

        ci = red_zones.case_insensitive(wt)
        return sorted(
            c["path"]
            for c in changed_files(inst, wt)
            if c.get("path") and red_zones.matches(re_src, c["path"], ci)
        )
    except Exception:  # noqa: BLE001
        return []


def preview(wt: str, pattern: str, inst: Any = None) -> dict:
    """Dry run of a zone pattern over the worktree, for the Map's filter box
    ("N files · Red-zone these →") and the add row: what it would cover
    before anything is saved.

    ``{"re", "count", "sample": [≤20], "ignored_count", "changed", "truncated"}``
    — ``count`` includes git-ignored matches (that is how a gitignored config
    file becomes visible), ``changed`` is the matching subset of the session's
    change set (only with ``inst``; the fork point is per session).

    The ONE public function here that raises: ``ValueError`` for an invalid
    pattern (empty, ``..``, absolute, too long), which the route maps to 400 —
    a dry run that silently answered "0 files" for a typo would read as "the
    zone is fine, it just matches nothing".

    The rule handed to ``zone_files`` carries the ``pattern`` as well as the
    ``re``, exactly like a saved zone: the git-ignored listing is scoped by
    pathspecs derived from the pattern, and without it the whole ignored tree
    is walked — where a big ``.venv``/``node_modules`` sorts first and a
    gitignored ``config/local.toml`` (the headline "my configuration file"
    case) could fall past the scan bound and preview as 0 files."""
    from backend.config import red_zones

    re_src = red_zones.compile_pattern(pattern)  # ValueError -> caller's 400
    try:
        files, _dirs, ignored, truncated = red_zones.zone_files(
            wt, [{"re": re_src, "pattern": pattern}], cap=MAX_FILES
        )
    except Exception:  # noqa: BLE001
        files, ignored, truncated = [], [], False
    return {
        "re": re_src,
        "count": len(files),
        "sample": files[:_PREVIEW_SAMPLE],
        "ignored_count": len(ignored),
        "changed": changed_matching(inst, wt, re_src),
        "truncated": bool(truncated),
    }


OTHERS_WINDOW_S = 600.0
_OTHERS_CAP = 50
# Per-feed parse memo for others(): (mtime_ns, size) -> records. The Map polls
# every 2 s and each poll would otherwise tail-read EVERY same-repo session's
# feed; an idle feed doesn't change, so this makes the steady state a stat per
# peer.
_OTHERS_MEMO: Dict[str, tuple] = {}
_OTHERS_MEMO_MAX = 128


def _peer_records(tmux_name: str) -> List[dict]:
    try:
        st = os.stat(_feed_path(tmux_name))
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _OTHERS_MEMO.get(tmux_name)
        if hit and hit[0] == key:
            return hit[1]
    recs = read_feed(tmux_name, 0.0, 500)
    with _LOCK:
        if len(_OTHERS_MEMO) >= _OTHERS_MEMO_MAX:
            _OTHERS_MEMO.clear()
        _OTHERS_MEMO[tmux_name] = (key, recs)
    return recs


def others(inst: Any, repo_id: Optional[str], since: float = 0.0) -> List[dict]:
    """Recent edits (last :data:`OTHERS_WINDOW_S`) by OTHER live sessions on the
    same repo — the Map's "also edited by <title>" dots, the early warning that
    two agents are about to collide in one file from different worktrees.

    ``[{"session": title, "path": rel, "ts"}]`` newest first, one row per
    (session, path), capped at 50. A peer's write is relativized to ITS
    worktree and kept only when that rel path exists in THIS one (a file the
    other agent created has no tile here to put a dot on). Denied and failed
    calls don't count — they changed nothing. ``[]`` without a repo id."""
    if not repo_id or inst is None:
        return []
    try:
        from backend.config import red_zones
        from backend.session import tmux

        srv = _server()
        my_title = getattr(inst, "Title", "")
        try:
            my_wt = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            my_wt = ""
        if not my_wt:
            return []
        floor = max(float(since or 0.0), time.time() - OTHERS_WINDOW_S)
        newest: Dict[Tuple[str, str], float] = {}
        for title, other in list(srv.ENGINE.instances.items()):
            if other is inst or title == my_title:
                continue
            try:
                if not other.Started():
                    continue
                owt = other.GetWorktreePath()
            except Exception:  # noqa: BLE001
                continue
            if not owt or not os.path.isdir(owt):
                continue
            ident = red_zones.repo_identity(owt)
            if not ident or ident[0] != repo_id:
                continue
            recs = _peer_records(tmux.to_mindflock_tmux_name(title))
            failed = {
                r.get("id") for r in recs if r.get("ev") == "fail" and r.get("id")
            }
            for r in recs:
                ts = r.get("ts")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    continue
                if ts <= floor or r.get("deny") or r.get("ev") not in ("pre", "post"):
                    continue
                if r.get("id") and r.get("id") in failed:
                    continue
                for w in r.get("writes") or []:
                    rel = _rel_in_wt(w, owt)
                    if not rel or not os.path.exists(os.path.join(my_wt, rel)):
                        continue
                    k = (title, rel)
                    if ts > newest.get(k, 0.0):
                        newest[k] = float(ts)
        rows = [{"session": t, "path": p, "ts": ts} for (t, p), ts in newest.items()]
        rows.sort(key=lambda d: (-d["ts"], d["session"], d["path"]))
        return rows[:_OTHERS_CAP]
    except Exception:  # noqa: BLE001
        return []
