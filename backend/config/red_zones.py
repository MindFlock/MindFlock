"""Red-zone store + guard-file builder — the data side of the red-zone guard.

A *red zone* is a path glob the coding agent may read but must never create,
modify, delete or move. Zones are declared per repo (default, reaching every
worktree/clone of the same origin) or per worktree, persisted to a small JSON
store, and *enforced* by a per-tool hook the providers install into the CLI's
own hooks config (:mod:`backend.providers._tool_hook_src`). The hook reads a
**guard file** — one per worktree root, keyed by ``sha1(realpath(root))`` —
at *fire time*, so adding a zone mid-flight takes effect on the very next tool
call without relaunching the agent.

This module is deliberately light (stdlib + a lazy ``git`` subprocess) and free
of import cycles so providers, the engine and the web layer can all import it.
``backend.config`` must stay importable without pulling in ``backend.web`` or
``backend.providers``; nothing here imports either at module scope.

Everything that touches the filesystem resolves its paths from the environment
at call time (``MINDFLOCK_RED_ZONES_FILE`` / ``MINDFLOCK_RED_ZONE_DIR`` /
``MINDFLOCK_TOOL_FEED_DIR``), which is how the test-suite's ``conftest``
redirects the stores away from the user's real ``~/.mindflock`` /
``~/.mindflock-assistant``. Readers never raise: a missing or corrupt store is
an empty document, and a git lookup failure returns ``None`` so callers can
leave a previously-written guard file untouched rather than dropping rules over
a transient error.

GREEN ZONES (v3) are the inverse: "the agent may ONLY modify files here". They
are task-level, so they are WORKTREE-scope only, and they are stored under
their own keys (``worktrees[wt].green``, guard ``green_rules``) so an older
server or an older baked-in hook — which reads ``zones`` / ``rules`` as red —
never sees one and can never enforce a green zone as a red one. ONE predicate,
:func:`classify`, decides every path for every consumer (the hook carries a
byte-for-byte mirror, the frontend a TS twin; ``tests/fixtures/
zone_classify_cases.json`` pins all three to the same answers).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as _stat
import subprocess
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "store_path",
    "guard_dir",
    "feed_dir",
    "guard_path",
    "feed_path",
    "repo_identity",
    "normalize_pattern",
    "compile_pattern",
    "matches",
    "repo_zones",
    "worktree_zones",
    "effective_zones",
    "add_zone",
    "remove_zone",
    "set_waiver",
    "set_plan_first",
    "plan_first",
    "forget_worktree",
    "all_repos",
    "store_digest",
    "zone_files",
    "sync_guard",
    "sync_for_workdir",
    "remove_guard",
    "gc_guards",
    "PLAN_PROMPT",
    "REMAINING_PROMPT",
    "go_message",
    "zone_added_message",
    "deny_reason",
    "decorate_prompt",
    "STORE_VERSION",
    "ZoneConflict",
    "DEFAULT_COMPANIONS",
    "GREEN_OUTSIDE",
    "classify",
    "zones_doc",
    "verdict",
    "breach_verdicts",
    "green_exempt",
    "set_green_exempt",
    "drop_green_exempt",
    "companions_config",
    "set_companions",
    "test_companions",
    "worktree_blobs",
    "rev_blobs",
    "green_label",
    "green_deny_reason",
    "green_scope_message",
    "exact_re",
]

STORE_VERSION = 1

# One lock guards every read-modify-write of the store AND the compute+write of
# a guard file (so a route and the reconcile tick never race to a stale guard).
# Reentrant because sync_guard holds it while calling effective_zones/zone_files,
# which take it again.
_LOCK = threading.RLock()

# repo_identity memoizes SUCCESSES only, keyed by realpath(path), each with the
# (git config path, mtime_ns) of every repo the resolution visited — a hit is
# re-validated with a few stat()s, so `git remote add origin` mid-run moves the
# id (no subprocess on a hit). Bounded so a long-lived server that sees many
# worktrees cannot leak unboundedly.
_IDENTITY_MEMO: Dict[
    str, Tuple[Tuple[str, str], Tuple[Tuple[str, Optional[int]], ...]]
] = {}
_IDENTITY_MEMO_MAX = 4096

# Per-root record of the (content-hash, store-digest) we last WROTE, so
# sync_guard can tell an external edit ("healed") from a legitimate change.
_GUARD_MEMO: Dict[str, Tuple[str, str]] = {}

_GIT_TIMEOUT = 15

#: The ``pattern`` a green breach reports (there is no single zone to name:
#: the path is in NONE of them).
GREEN_OUTSIDE = "outside green"

#: Files an agent legitimately writes OUTSIDE its green zone(s) as a side
#: effect of in-scope work (a dependency bump rewrites the lockfile, a test
#: run refreshes a snapshot). Writable, flagged amber, never a breach. The
#: repo's own "derived outputs" (``companions_config``) and the test files
#: that import a green file (``test_companions``) join them at sync time.
DEFAULT_COMPANIONS = (
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "__snapshots__/",
    "*.snap",
)


class ZoneConflict(LookupError):
    """The same pattern as both a red and a green zone — a contradiction the
    routes answer with 409 (red would silently win, and the user would think
    the green one did something)."""


# --------------------------------------------------------------------------- #
# The deny-reason template. The hook source (``_tool_hook_src._MF_DENY_TMPL``)
# carries a byte-identical copy so the reason the model sees on a live block
# matches the one the server would render; ``test_red_zones`` asserts equality.
# --------------------------------------------------------------------------- #
_DENY_REASON_TMPL = (
    'MindFlock red zone: {rel} is protected by "{label}". The user has made '
    "this path off-limits for edits. Find an approach that leaves it unchanged, "
    "or stop and ask the user — don't route around the block (copies, wrappers, "
    "shell edits)."
)

# The green deny reason. Byte-identical to ``_tool_hook_src._MF_GREEN_TMPL``
# (asserted by ``test_tool_hook``). It tells the agent what to DO — autopilot
# and intake sessions have no user to "stop and ask".
_GREEN_DENY_TMPL = (
    "MindFlock scope: {rel} is outside the green zone(s) the user scoped this "
    "task to ({label}). Finish the in-scope work and list any out-of-scope "
    "files you need in your reply instead of editing them."
)
# At most this many zones are named in a green reason (then "+N more").
_LABEL_CAP = 5


# --------------------------------------------------------------------------- #
# Paths (resolved from env at call time)
# --------------------------------------------------------------------------- #
def _config_dir() -> str:
    """``GetConfigDir()`` (``~/.mindflock``) via a lazy import (no cycle)."""
    from backend.config.config import GetConfigDir

    return GetConfigDir()


def _assistant_dir() -> str:
    """The sidecar-store root, honouring ``MINDFLOCK_ASSISTANT_DIR`` (the same
    override the activity markers use, which conftest redirects to tmp)."""
    return os.environ.get(
        "MINDFLOCK_ASSISTANT_DIR",
        os.path.join(os.path.expanduser("~"), ".mindflock-assistant"),
    )


def store_path() -> str:
    """The zone store JSON file."""
    from backend.config.home_guard import guard

    env = os.environ.get("MINDFLOCK_RED_ZONES_FILE")
    if env:
        return guard(env, "red-zone store")
    return os.path.join(_config_dir(), "red_zones.json")


def guard_dir() -> str:
    """The directory holding per-root guard files (what the hook reads)."""
    from backend.config.home_guard import guard

    return guard(
        os.environ.get(
            "MINDFLOCK_RED_ZONE_DIR", os.path.join(_assistant_dir(), ".red-zones")
        ),
        "red-zone guard dir",
    )


def feed_dir() -> str:
    """The directory holding per-session tool-feed ``.jsonl`` files."""
    from backend.config.home_guard import guard

    return guard(
        os.environ.get(
            "MINDFLOCK_TOOL_FEED_DIR", os.path.join(_assistant_dir(), ".tool-feed")
        ),
        "tool-feed dir",
    )


def guard_path(root: str) -> str:
    """The guard file for ``root`` — named by ``sha1(realpath(root))[:20]``.

    The hook computes the *exact same* name from its own environment, so the
    server and the fire-time hook always agree on which file governs a root.
    """
    h = hashlib.sha1(os.path.realpath(root).encode("utf-8", "replace")).hexdigest()
    return os.path.join(guard_dir(), h[:20] + ".json")


def _sanitize(name: str) -> str:
    """Filesystem-safe form of a tmux/session name (matches the marker/feed
    sanitizer used elsewhere, byte-for-byte)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def feed_path(tmux_name: str) -> str:
    """The tool-feed file for a tmux session."""
    return os.path.join(feed_dir(), _sanitize(tmux_name) + ".jsonl")


# --------------------------------------------------------------------------- #
# Store I/O (atomic; missing/corrupt -> empty doc; never raises on read)
# --------------------------------------------------------------------------- #
def _empty_doc() -> dict:
    return {"version": STORE_VERSION, "repos": {}, "worktrees": {}}


def _load() -> dict:
    """The store document, or an empty one on any failure. Never raises."""
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 — missing/corrupt store = empty doc
        return _empty_doc()
    if not isinstance(data, dict):
        return _empty_doc()
    data.setdefault("version", STORE_VERSION)
    if not isinstance(data.get("repos"), dict):
        data["repos"] = {}
    if not isinstance(data.get("worktrees"), dict):
        data["worktrees"] = {}
    return data


def _save(data: dict) -> None:
    """Atomically persist ``data``: mkstemp in the store dir + fsync + replace.

    Callers hold ``_LOCK``. Best-effort — a persistence failure must not crash a
    route or a loop, so the OSError is swallowed after being given a chance.
    """
    import tempfile

    path = store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def store_digest() -> str:
    """sha1 of the store file bytes (``""`` when absent). Tamper detection."""
    try:
        with open(store_path(), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Repo identity
# --------------------------------------------------------------------------- #
def _git(path: str, *args: str) -> Optional[str]:
    """Run ``git -C path <args>`` and return trimmed stdout, or None on any
    non-zero exit / error / timeout. Never raises."""
    try:
        cp = subprocess.run(
            ["git", "-C", path, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — git missing / hung / bad path
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.decode("utf-8", "replace").strip()


def _normalize_origin(url: str) -> Optional[Tuple[str, str]]:
    """``(repo_id, label)`` from a remote URL, or None when the URL names a
    *local* path (caller then follows it) or is unparseable.

    ``repo_id`` is fully lower-cased ``host/owner/repo`` with ``.git`` and
    trailing slashes stripped; ``label`` keeps the original case of the last
    two path segments (``Owner/Repo``) for display.
    """
    u = (url or "").strip()
    if not u:
        return None
    if u.startswith("file://"):
        u = u[len("file://") :]
    # A local path or file:// origin is followed by the caller, not normalized.
    if u.startswith(("/", ".", "~")):
        return None

    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://(.*)$", u)
    if scheme:
        rest = scheme.group(2)
        rest = re.sub(r"^[^@/]+@", "", rest)  # strip userinfo
        m = re.match(r"^([^/:]+)(?::\d+)?/(.*)$", rest)
        if not m:
            return None
        host, pathpart = m.group(1), m.group(2)
    else:
        # scp-like: [user@]host:path
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.*)$", u)
        if not m:
            return None
        host, pathpart = m.group(1), m.group(2)

    pathpart = pathpart.strip("/")
    if pathpart.endswith(".git"):
        pathpart = pathpart[:-4]
    pathpart = pathpart.strip("/")
    if not host:
        return None
    repo_id = (host + "/" + pathpart).lower().rstrip("/")
    segs = [s for s in pathpart.split("/") if s]
    if len(segs) >= 2:
        label = "/".join(segs[-2:])
    elif segs:
        label = segs[-1]
    else:
        label = host
    return repo_id, label


def _local_target(url: str, base: str) -> Optional[str]:
    """The directory a local/``file://`` origin points at (realpath), resolved
    against ``base`` for a relative path, or None when it is not local."""
    u = (url or "").strip()
    if u.startswith("file://"):
        u = u[len("file://") :]
    if not u.startswith(("/", ".", "~")):
        return None
    u = os.path.expanduser(u)
    if not os.path.isabs(u):
        u = os.path.join(base, u)
    return os.path.realpath(u)


def _path_identity(path: str) -> Optional[Tuple[str, str]]:
    """``("path:<repo root realpath>", basename)`` fallback for an origin-less
    repo. The root is the parent of the git-common-dir, so every worktree of one
    clone shares the id."""
    common = _git(path, "rev-parse", "--git-common-dir")
    if common is None:
        return None
    if not os.path.isabs(common):
        common = os.path.join(path, common)
    root = os.path.realpath(os.path.dirname(common))
    return "path:" + root, os.path.basename(root) or root


def _mtime_ns(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _config_stamp(path: str) -> Optional[Tuple[str, Optional[int]]]:
    """``(config file, mtime_ns)`` of the repo at ``path`` — the file an origin
    add/change rewrites (a linked worktree's ``--git-path config`` resolves to
    the shared one). None when git can't say."""
    cfg = _git(path, "rev-parse", "--git-path", "config")
    if not cfg:
        return None
    if not os.path.isabs(cfg):
        cfg = os.path.join(path, cfg)
    return cfg, _mtime_ns(cfg)


def _resolve_identity(
    path: str, hops: int, stamps: Optional[list] = None
) -> Optional[Tuple[str, str]]:
    if stamps is not None:
        st = _config_stamp(path)
        if st is not None:
            stamps.append(st)
    origin = _git(path, "remote", "get-url", "origin")
    if origin:
        norm = _normalize_origin(origin)
        if norm is not None:
            return norm
        # Local / file:// origin: follow it (provisioned base clones point at
        # the user's own checkout) up to a small hop limit.
        if hops > 0:
            target = _local_target(origin, path)
            if target and os.path.isdir(target):
                nested = _resolve_identity(target, hops - 1, stamps)
                if nested is not None:
                    return nested
    # No usable origin -> stable path identity.
    return _path_identity(path)


def repo_identity(path: str) -> Optional[Tuple[str, str]]:
    """``(repo_id, label)`` for the repo at ``path``, or None on git failure.

    Callers MUST treat None as "keep the previous guard file untouched" — never
    write a guard with fewer rules because a lookup blipped. Successes are
    memoized (bounded) by realpath; failures are never cached, so a transient
    error retries on the next call. A memo hit is re-validated against the
    mtime of every git config the resolution read (a few stat()s, no git): an
    origin added or changed while the server runs re-resolves at once, so an
    old worktree and a new sibling never file zones under different ids.
    """
    if not path:
        return None
    key = os.path.realpath(path)
    with _LOCK:
        cached = _IDENTITY_MEMO.get(key)
    if cached is not None:
        result, stamps = cached
        if all(_mtime_ns(p) == m for p, m in stamps):
            return result
    fresh: list = []
    result = _resolve_identity(path, hops=3, stamps=fresh)
    if result is not None:
        with _LOCK:
            if len(_IDENTITY_MEMO) >= _IDENTITY_MEMO_MAX:
                _IDENTITY_MEMO.clear()
            _IDENTITY_MEMO[key] = (result, tuple(fresh))
    return result


# --------------------------------------------------------------------------- #
# Pattern semantics (gitignore-flavoured, over worktree-relative POSIX paths)
# --------------------------------------------------------------------------- #
_MAX_PATTERN_LEN = 400
# Regex metacharacters escaped identically by Python `re` and JS `RegExp`.
_REGEX_SPECIAL = set(".^$*+?()[]{}|\\/")


def normalize_pattern(p: str) -> Tuple[str, bool]:
    """``(normalized, anchored)`` for a raw pattern. ``anchored`` is True when a
    leading ``/`` pinned it to the root.

    Raises ``ValueError`` on empty / NUL / a ``..`` segment / length > 400 / an
    absolute filesystem path.
    """
    if p is None:
        raise ValueError("empty pattern")
    s = p.strip().replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    anchored = False
    if s.startswith("/"):
        anchored = True
        s = s.lstrip("/")
    if not s:
        raise ValueError("empty pattern")
    if "\x00" in s:
        raise ValueError("NUL in pattern")
    if len(s) > _MAX_PATTERN_LEN:
        raise ValueError("pattern too long")
    if os.path.isabs(os.path.expanduser(s)) or (len(s) >= 2 and s[1] == ":"):
        raise ValueError("absolute path is not a valid pattern")
    for seg in s.split("/"):
        if seg == "..":
            raise ValueError("'..' segment is not allowed in a pattern")
    return s, anchored


def _translate_glob(body: str) -> str:
    """Translate one glob body to a regex fragment valid in Python and JS."""
    out: List[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "*":
            if i + 1 < n and body[i + 1] == "*":
                # gitignore semantics: a whole ``**/`` segment spans ZERO or
                # more directories, so ``backend/**/secret.py`` also protects
                # ``backend/secret.py`` and ``**/x`` protects a root-level ``x``.
                if body[i + 2 : i + 3] == "/" and (i == 0 or body[i - 1] == "/"):
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "[":
            j = i + 1
            if j < n and body[j] in ("!", "^"):
                j += 1
            if j < n and body[j] == "]":
                j += 1
            while j < n and body[j] != "]":
                j += 1
            if j >= n:  # no close -> literal '['
                out.append("\\[")
                i += 1
                continue
            inner = body[i + 1 : j]
            neg = inner.startswith(("!", "^"))
            if neg:
                inner = inner[1:]
            # Escape a backslash inside the class (JS-safe); leave ranges intact.
            inner = inner.replace("\\", "\\\\")
            # A literal '[' member: escaped (Python warns on a possible
            # nested set; JS reads it the same either way).
            inner = inner.replace("[", "\\[")
            # A LEADING ']' is a literal member in a glob and in Python, but
            # JS reads ``[]`` as an empty class and ``[^]`` as "any char":
            # escape it so both engines agree.
            if inner.startswith("]"):
                inner = "\\" + inner
            out.append("[" + ("^" if neg else "") + inner + "]")
            i = j + 1
            continue
        if c in _REGEX_SPECIAL:
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def compile_pattern(p: str) -> str:
    """A regex SOURCE (anchored, ``^…$``) valid in BOTH Python ``re`` and JS
    ``RegExp``, matching the path itself and everything beneath it.

    Never emits Python-only syntax (``\\Z``, ``(?P…``, inline ``(?i)`` flags),
    so the frontend can compile the same source with ``new RegExp(src)``.
    """
    norm, anchored = normalize_pattern(p)
    body = norm.rstrip("/")  # a trailing slash is directory syntax, not depth
    # A basename pattern (no interior slash) matches at any depth unless anchored.
    any_depth = "/" not in body and not anchored
    frag = _translate_glob(body)
    prefix = "^(?:.*/)?" if any_depth else "^"
    return prefix + frag + "(?:/.*)?$"


def matches(re_src: str, rel: str, ci: bool = False) -> bool:
    """Whether ``rel`` matches the compiled source. Never raises."""
    try:
        return re.match(re_src, rel, re.IGNORECASE if ci else 0) is not None
    except re.error:
        return False


def glob_escape(rel: str) -> str:
    """A glob that means exactly the literal path ``rel``: each glob
    metacharacter (``[ ] * ?``) becomes a one-character class, so a zone
    built from a CONCRETE path (Allow this file, Go — only the planned
    files) matches ``app/[slug]/page.tsx`` itself, not ``app/s/page.tsx``."""
    return "".join("[" + c + "]" if c in "[]*?" else c for c in rel)


def exact_re(rel: str) -> str:
    """A regex source matching exactly ``rel`` (and nothing beneath it) —
    for companion FILES, whose names may contain glob characters that
    :func:`compile_pattern` would read as wildcards. Python- and JS-valid."""
    return "^" + "".join("\\" + c if c in _REGEX_SPECIAL else c for c in rel) + "$"


# --------------------------------------------------------------------------- #
# The ONE predicate. Mirrored byte-for-byte in behaviour by the hook
# (``_tool_hook_src._mf_classify``) and by the frontend's ``classifyPath``;
# ``tests/fixtures/zone_classify_cases.json`` holds the shared cases.
# --------------------------------------------------------------------------- #
_NESTED_RE = re.compile(r"^\.claude/worktrees/[^/]+(?:/(.*))?$")
_ARTIFACT_RE = re.compile(r"^\.mindflock_[^/]*(?:/.*)?$")
_RE_MEMO: Dict[str, str] = {}


def _entry_re(e) -> Optional[str]:
    """The regex source of a zone-doc entry: a bare pattern string, or a dict
    carrying ``re`` (or at least ``pattern``). None when unusable."""
    if isinstance(e, str):
        src = _RE_MEMO.get(e)
        if src is None:
            try:
                src = compile_pattern(e)
            except ValueError:
                return None
            if len(_RE_MEMO) > 4096:
                _RE_MEMO.clear()
            _RE_MEMO[e] = src
        return src
    if isinstance(e, dict):
        if e.get("re"):
            return str(e["re"])
        if e.get("pattern"):
            return _entry_re(str(e["pattern"]))
    return None


def _any_match(entries, rel: str, ci: bool) -> bool:
    for e in entries or ():
        src = _entry_re(e)
        if src and matches(src, rel, ci):
            return True
    return False


def _green_one(rel: str, green, comps, ci: bool) -> str:
    """``"ok"`` / ``"companion"`` / ``"outside"`` for ONE representation of a
    path while green zones exist."""
    if _any_match(green, rel, ci) or _ARTIFACT_RE.match(rel):
        return "ok"
    m = _NESTED_RE.match(rel)
    inner = None
    if m:
        inner = m.group(1)
        if not inner:
            return "ok"  # .claude/worktrees/<n> itself: sandbox bookkeeping
        if _any_match(green, inner, ci) or _ARTIFACT_RE.match(inner):
            return "ok"
    if _any_match(comps, rel, ci) or (inner and _any_match(comps, inner, ci)):
        return "companion"
    return "outside"


def classify(
    rel_real: Optional[str],
    rel_lex: Optional[str],
    zones_doc: dict,
    ci: bool = False,
) -> str:
    """``"blocked"`` | ``"outside"`` | ``"companion"`` | ``"ok"`` for a
    worktree-relative path. ``rel_real`` is the path's REALPATH relative to
    the root, ``rel_lex`` the path as written (either may be None: not in the
    root that way; pass the same value twice, or None, when there is only
    one). ``zones_doc`` = ``{"red": [...], "green": [...], "companions":
    [...]}`` whose entries are pattern strings or zone dicts with ``re``.

    Order: a RED match on ANY representation (and the ``.claude/worktrees/
    <n>/``-stripped one) → blocked — red always wins. Else, with no green
    zone → ok. Else EVERY representation must be writable (a symlink inside
    the scope pointing outside it is outside): green match, a MindFlock
    workspace artifact (``.mindflock_*``) or the nested-sandbox dir itself →
    ok; a companion → companion (writable, flagged, never a breach); else
    outside. The stripped nested candidate is the one any-of allowance.
    Never raises."""
    try:
        doc = zones_doc or {}
        cands: List[str] = []
        for c in (rel_real, rel_lex):
            if c is not None and c not in cands:
                cands.append(c)
        if not cands:
            return "ok"
        red = doc.get("red") or ()
        for c in cands:
            m = _NESTED_RE.match(c)
            subs = [c] + ([m.group(1)] if m and m.group(1) else [])
            if any(_any_match(red, x, ci) for x in subs):
                return "blocked"
        green = doc.get("green") or ()
        if not green:
            return "ok"
        comps = doc.get("companions") or ()
        verdicts = [_green_one(c, green, comps, ci) for c in cands]
        if "outside" in verdicts:
            return "outside"
        if "companion" in verdicts:
            return "companion"
        return "ok"
    except Exception:  # noqa: BLE001 — a reader never raises
        return "ok"


def verdict(doc: dict, rel: str, ci: bool = False) -> Optional[dict]:
    """``{"kind", "pattern", "zone_id"}`` when ``rel`` is blocked (the first
    red zone it hits) or outside the green scope (``pattern`` =
    :data:`GREEN_OUTSIDE`); None when it may be written (ok / companion)."""
    v = classify(rel, None, doc, ci)
    if v == "blocked":
        m = _NESTED_RE.match(rel or "")
        subs = [rel] + ([m.group(1)] if m and m.group(1) else [])
        for z in doc.get("red") or ():
            src = _entry_re(z)
            if src and any(matches(src, x, ci) for x in subs):
                if isinstance(z, dict):
                    return {
                        "kind": "red",
                        "pattern": z.get("pattern"),
                        "zone_id": z.get("id"),
                    }
                return {"kind": "red", "pattern": str(z), "zone_id": None}
        return {"kind": "red", "pattern": None, "zone_id": None}
    if v == "outside":
        return {"kind": "green", "pattern": GREEN_OUTSIDE, "zone_id": None}
    return None


def breach_verdicts(
    doc: dict,
    rels: Iterable[str],
    ci: bool = False,
    *,
    root: Optional[str] = None,
    rev: Optional[str] = None,
) -> Dict[str, dict]:
    """``{rel: verdict}`` for the paths of ``rels`` that are BREACHES: blocked,
    or outside and not exempt. An exempt path (``doc["exempt"]``) stops being
    exempt the moment its content moves off the recorded blob — the working
    tree's (``rev=None``) or the one at ``rev`` (the push/PR gate). Blob
    lookups need ``root``; without it every exempt path stays exempt."""
    out: Dict[str, dict] = {}
    exempt = doc.get("exempt") or {}
    pending: Dict[str, dict] = {}
    for rel in rels or ():
        if not rel or rel in out:
            continue
        v = verdict(doc, rel, ci)
        if v is None:
            continue
        if v["kind"] == "green" and rel in exempt:
            pending[rel] = v
            continue
        out[rel] = v
    if pending and root:
        blobs = (
            rev_blobs(root, rev, list(pending))
            if rev
            else worktree_blobs(root, list(pending))
        )
        for rel, v in pending.items():
            cur = blobs.get(rel)
            if cur is not None and cur not in exempt_ids(exempt.get(rel)):
                out[rel] = v
    return out


def exempt_ids(value) -> Tuple[str, ...]:
    """The accepted blob identities of one exemption value: a single sha /
    ``"deleted"`` (v3.0), or several separated by spaces — the working-tree
    blob AND the ones at ``HEAD`` / ``origin/<branch>`` when the scope was
    set, so work COMMITTED before it (then edited further) is exempt at the
    push/PR gate (which compares the blob at the pushed rev) too."""
    return tuple(str(value or "").split())


# --------------------------------------------------------------------------- #
# Blob identities (green exemptions)
# --------------------------------------------------------------------------- #
_BLOB_MEMO: Dict[Tuple[str, str], Tuple[tuple, str]] = {}
_BLOB_MEMO_MAX = 4096


def _py_blob(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    h = hashlib.sha1(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def worktree_blobs(root: str, rels: Iterable[str]) -> Dict[str, str]:
    """``{rel: git blob sha | "deleted"}`` of the WORKING-TREE content (git's
    own ``hash-object``, so clean filters/autocrlf agree with what a commit
    would store; a plain sha1-of-blob fallback). Memoized on the stat key.
    Never raises; a path it can't read is left out."""
    out: Dict[str, str] = {}
    todo: List[str] = []
    keys: Dict[str, tuple] = {}
    for rel in rels or ():
        p = os.path.join(root, rel)
        try:
            st = os.stat(p)
        except OSError:
            if not os.path.lexists(p):
                out[rel] = "deleted"
            continue
        if not _stat.S_ISREG(st.st_mode):
            continue
        k = (st.st_mtime_ns, st.st_size, st.st_ino)
        keys[rel] = k
        hit = _BLOB_MEMO.get((root, rel))
        # Racy-git rule: a stat key younger than the timestamp granularity
        # can't vouch for the content (a same-size rewrite in the same tick
        # keeps mtime/size/inode), so a fresh file is always re-hashed.
        if hit and hit[0] == k and time.time() - st.st_mtime > 2.0:
            out[rel] = hit[1]
        elif "\n" not in rel:
            todo.append(rel)
    if todo:
        shas: List[str] = []
        try:
            cp = subprocess.run(
                ["git", "-C", root, "hash-object", "--stdin-paths"],
                input=("\n".join(todo) + "\n").encode("utf-8", "replace"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT,
            )
            if cp.returncode == 0:
                shas = cp.stdout.decode("ascii", "replace").split()
        except Exception:  # noqa: BLE001
            shas = []
        if len(shas) != len(todo):
            shas = [_py_blob(os.path.join(root, r)) or "" for r in todo]
        if len(_BLOB_MEMO) > _BLOB_MEMO_MAX:
            _BLOB_MEMO.clear()
        for rel, sha in zip(todo, shas):
            if sha:
                out[rel] = sha
                _BLOB_MEMO[(root, rel)] = (keys[rel], sha)
    return out


def rev_blobs(root: str, rev: str, rels: Iterable[str]) -> Dict[str, str]:
    """``{rel: blob sha | "deleted"}`` at a commit (``git ls-tree``); a path
    the tree doesn't carry is ``"deleted"``. ``{}`` on git failure (the
    caller then keeps every exemption — the gate fails open like its
    siblings)."""
    rels = [r for r in (rels or ()) if r]
    if not rels or not rev or rev.startswith("-"):
        return {}
    try:
        cp = subprocess.run(
            ["git", "-C", root, "ls-tree", "-r", "-z", "--full-tree", rev, "--"] + rels,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return {}
    if cp.returncode != 0:
        return {}
    found: Dict[str, str] = {}
    for rec in cp.stdout.decode("utf-8", "replace").split("\0"):
        head, _tab, path = rec.partition("\t")
        bits = head.split()
        if len(bits) >= 3 and path:
            found[path] = bits[2]
    return {r: found.get(r, "deleted") for r in rels}


# --------------------------------------------------------------------------- #
# Companions + the zone document every consumer classifies against
# --------------------------------------------------------------------------- #
_TESTS_MEMO: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
_TESTS_TTL_S = 20.0
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?[\w-]*|__tests__|spec)(/|$)|(^|/)(test_[^/]*\.py|[^/]*_test\.(py|go|rs)"
    r"|[^/]*\.(test|spec)\.[^/]+)$"
)


def _green_sig(green) -> str:
    return "\n".join(sorted(str(_entry_re(z) or "") for z in green or ()))


def test_companions(wt: str, green, fp: Optional[str] = None) -> List[str]:
    """Test files that import a file inside the green scope — a change to a
    scoped file is expected to come with its tests. Read off the Code Map's
    import graph (``code_map.list_files`` + ``build_graph``; flag 2 = test
    file). Memoized ``_TESTS_TTL_S`` per (worktree, green set); ``[]`` on any
    failure — companions only ever WIDEN what is allowed, so missing them
    costs a deny the user can one-click allow, never a silent hole."""
    if not green:
        return []
    root = os.path.realpath(wt)
    key = (root, _green_sig(green))
    now = time.time()
    hit = _TESTS_MEMO.get(key)
    if hit and now - hit[0] < _TESTS_TTL_S:
        return list(hit[1])
    out: List[str] = []
    try:
        from backend.web.core import code_map  # lazy: config must not import web

        rows, _trunc = code_map.list_files(wt, (), fp=fp)
        graph = code_map.build_graph(wt, rows, fp=fp)
        flag_test = int(getattr(code_map, "FLAG_TEST", 2))
        rels: List[str] = []
        tests: List[bool] = []
        for r in rows:
            if isinstance(r, (list, tuple)) and r:
                rel = str(r[0])
                flags = r[2] if len(r) > 2 and isinstance(r[2], int) else 0
                is_test = bool(flags & flag_test) or bool(_TEST_PATH_RE.search(rel))
            else:
                rel = str(r)
                is_test = bool(_TEST_PATH_RE.search(rel))
            rels.append(rel)
            tests.append(is_test)
        doc = {"green": list(green)}
        edges: List[Tuple[int, int]] = []
        for e in graph.get("edges") or []:
            try:
                src, dst = int(e[0]), int(e[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= src < len(rels) and 0 <= dst < len(rels):
                edges.append((src, dst))
        # ADDENDUM A.2, shared with the Atlas: a test-named file that
        # non-test code imports (test_plans.py, web/testimonials/card.py) is
        # CODE — never a companion the agent may write outside its scope.
        eff = code_map.effective_tests((i for i, t in enumerate(tests) if t), edges)
        seen = set()
        for src, dst in edges:
            if src not in eff or rels[src] in seen:
                continue
            if _any_match(doc["green"], rels[dst], False):
                if not _any_match(doc["green"], rels[src], False):
                    seen.add(rels[src])
        out = sorted(seen)[:2000]
    except Exception:  # noqa: BLE001
        out = []
    if len(_TESTS_MEMO) > 256:
        _TESTS_MEMO.clear()
    _TESTS_MEMO[key] = (now, out)
    return list(out)


def _companion_rules(repo_id: Optional[str], tests: Iterable[str]) -> List[dict]:
    out: List[dict] = []
    for p in DEFAULT_COMPANIONS:
        out.append({"pattern": p, "re": compile_pattern(p), "source": "default"})
    for p in companions_config(repo_id):
        try:
            out.append({"pattern": p, "re": compile_pattern(p), "source": "repo"})
        except ValueError:
            continue
    for rel in tests or ():
        out.append({"pattern": "/" + rel, "re": exact_re(rel), "source": "tests"})
    return out


def zones_doc(
    wt: str,
    repo_id: Optional[str],
    *,
    zones: Optional[List[dict]] = None,
    ci: Optional[bool] = None,
    with_tests: bool = True,
    fp: Optional[str] = None,
) -> dict:
    """The document :func:`classify` reads for ``wt``: ``{"red": enforced
    red zones, "green": green zones, "companions": [{"pattern", "re",
    "source"}] (only while green exists), "exempt": {rel: sha}, "ci",
    "mode": "green"|"red"|None}``. ``zones`` = an ``effective_zones`` result
    the caller already holds. Never raises."""
    try:
        if zones is None:
            zones = effective_zones(wt, repo_id)
        red = [
            z for z in zones if z.get("kind", "red") == "red" and not z.get("waived")
        ]
        green = [z for z in zones if z.get("kind") == "green"]
        comps: List[dict] = []
        exempt: Dict[str, str] = {}
        if green:
            tests = test_companions(wt, green, fp=fp) if with_tests else []
            comps = _companion_rules(repo_id, tests)
            exempt = green_exempt(wt)
        if ci is None:
            ci = case_insensitive(wt)
        mode = "green" if green else ("red" if red else None)
        return {
            "red": red,
            "green": green,
            "companions": comps,
            "exempt": exempt,
            "ci": bool(ci),
            "mode": mode,
        }
    except Exception:  # noqa: BLE001
        return {
            "red": [],
            "green": [],
            "companions": [],
            "exempt": {},
            "ci": False,
            "mode": None,
        }


# --------------------------------------------------------------------------- #
# Zone helpers
# --------------------------------------------------------------------------- #
def _new_zone_id() -> str:
    return "rz_" + os.urandom(5).hex()


def _zone_view(z: dict, scope: str, waived: bool, kind: str = "red") -> dict:
    """A display copy of a zone with the compiled regex + scope + waived flag
    + ``kind`` (``"red"`` keep-out / ``"green"`` only-here)."""
    out = dict(z)
    out["scope"] = scope
    out["re"] = compile_pattern(z["pattern"])
    out["waived"] = waived
    out["kind"] = kind
    return out


def repo_zones(repo_id: str) -> List[dict]:
    """Fresh copies of a repo's zones (empty when unknown)."""
    with _LOCK:
        data = _load()
        repo = data["repos"].get(repo_id) or {}
        return [dict(z) for z in (repo.get("zones") or [])]


def _wt_entry(data: dict, wt: str) -> dict:
    key = os.path.realpath(wt)
    return data["worktrees"].get(key) or data["worktrees"].get(wt) or {}


def worktree_zones(wt: str) -> List[dict]:
    """Fresh copies of a worktree's own RED zones (empty when unknown)."""
    with _LOCK:
        data = _load()
        return [dict(z) for z in (_wt_entry(data, wt).get("zones") or [])]


def effective_zones(wt: str, repo_id: Optional[str]) -> List[dict]:
    """Every zone reaching ``wt``: repo zones (scope ``repo``, ``waived`` flagged
    from the worktree's waiver list), the worktree's own red zones (scope
    ``worktree``, never waivable) and its green zones (scope ``worktree``,
    ``kind: "green"``). Each carries the compiled ``re`` and its ``kind``.

    Consumers that only mean red must filter on ``kind`` — or, better, go
    through :func:`zones_doc` + :func:`classify`, the one predicate.
    """
    out: List[dict] = []
    with _LOCK:
        data = _load()
        wt_entry = _wt_entry(data, wt)
        waive = set(wt_entry.get("waive") or [])
        if repo_id:
            repo = data["repos"].get(repo_id) or {}
            for z in repo.get("zones") or []:
                out.append(_zone_view(z, "repo", z.get("id") in waive))
        for z in wt_entry.get("zones") or []:
            out.append(_zone_view(z, "worktree", False))
        for z in wt_entry.get("green") or []:
            out.append(_zone_view(z, "worktree", False, "green"))
    return out


def _pat_key(pattern: str) -> Optional[str]:
    """The contradiction key of a pattern: normalized, anchor and trailing
    slash ignored (``/src/`` red and ``src`` green are the same fight)."""
    try:
        norm, _anch = normalize_pattern(pattern)
    except (TypeError, ValueError):
        return None
    return norm.rstrip("/")


def _conflict(data: dict, kind: str, scope: str, owner: str, repo_id, key) -> bool:
    """Whether a zone of the OTHER kind with the same pattern key already
    reaches the same place."""
    others: List[dict] = []
    if kind == "green":
        entry = data["worktrees"].get(owner) or {}
        others += list(entry.get("zones") or [])
        rid = repo_id or entry.get("repo_id")
        if rid:
            others += list((data["repos"].get(rid) or {}).get("zones") or [])
    elif scope == "worktree":
        others += list((data["worktrees"].get(owner) or {}).get("green") or [])
    else:
        for entry in data["worktrees"].values():
            if isinstance(entry, dict) and entry.get("repo_id") == owner:
                others += list(entry.get("green") or [])
    return any(_pat_key(z.get("pattern") or "") == key for z in others)


def add_zone(
    scope: str,
    owner: str,
    pattern: str,
    *,
    name: str = "",
    note: str = "",
    label: str = "",
    repo_id: Optional[str] = None,
    kind: str = "red",
) -> dict:
    """Add a zone. ``scope`` ``"repo"`` → ``owner`` is a repo_id; ``"worktree"``
    → ``owner`` is the worktree realpath. Dedupes on the same normalized pattern
    AND anchoring in the same scope/owner/kind (returns the existing zone); the
    stored pattern keeps a leading ``/`` anchor. ``ValueError`` on an invalid
    pattern, scope or kind — and on a REPO-scope green zone (green describes a
    task, not repo policy: a repo-wide one would leak into Verify/intake
    sessions of the same repo and deny their own bookkeeping writes).
    :class:`ZoneConflict` when the same pattern is already the other kind.
    """
    if scope not in ("repo", "worktree"):
        raise ValueError("scope must be 'repo' or 'worktree'")
    if kind not in ("red", "green"):
        raise ValueError("kind must be 'red' or 'green'")
    if kind == "green" and scope != "worktree":
        raise ValueError(
            "green zones are worktree-scope only (they scope a task, not the repo)"
        )
    norm, anchored = normalize_pattern(pattern)  # validates; raises on bad input
    # Keep the anchor: `/config` (only the ROOT config/) and `config` (any
    # depth) are different zones. Dropping the '/' stored a zone that matched
    # backend/config/ too — wider than the preview the user approved.
    stored = ("/" + norm) if anchored else norm
    with _LOCK:
        data = _load()
        own = owner if scope == "repo" else os.path.realpath(owner)
        if _conflict(data, kind, scope, own, repo_id, norm.rstrip("/")):
            raise ZoneConflict(
                "`%s` is already a %s zone here — a path can't be both kept out "
                "and the only place allowed"
                % (stored, "red" if kind == "green" else "green")
            )
        if scope == "repo":
            repo = data["repos"].setdefault(
                owner, {"label": label or owner, "zones": [], "plan_first": False}
            )
            if label:
                repo["label"] = label
            zones = repo.setdefault("zones", [])
        else:
            entry = data["worktrees"].setdefault(
                own, {"repo_id": repo_id, "zones": [], "waive": []}
            )
            if repo_id:
                entry["repo_id"] = repo_id
            zones = entry.setdefault("green" if kind == "green" else "zones", [])
        for z in zones:
            try:
                same = normalize_pattern(z["pattern"]) == (norm, anchored)
            except (KeyError, TypeError, ValueError):
                same = False
            if same:
                return dict(z)
        zone = {
            "id": _new_zone_id(),
            "pattern": stored,
            "name": name or "",
            "note": note or "",
            "created": int(time.time()),
        }
        if kind == "green":
            zone["kind"] = "green"
        zones.append(zone)
        _save(data)
        return dict(zone)


def remove_zone(zone_id: str) -> Optional[dict]:
    """Remove a zone by id from wherever it lives; returns
    ``{"zone", "scope", "owner", "kind"}`` or None when unknown. Removing the
    LAST green zone of a worktree also drops its exemption set (it only means
    something while a green scope exists)."""
    with _LOCK:
        data = _load()
        for repo_id, repo in data["repos"].items():
            for i, z in enumerate(repo.get("zones") or []):
                if z.get("id") == zone_id:
                    zone = repo["zones"].pop(i)
                    _save(data)
                    return {
                        "zone": dict(zone),
                        "scope": "repo",
                        "owner": repo_id,
                        "kind": "red",
                    }
        for wt, entry in data["worktrees"].items():
            for key, kind in (("zones", "red"), ("green", "green")):
                for i, z in enumerate(entry.get(key) or []):
                    if z.get("id") == zone_id:
                        zone = entry[key].pop(i)
                        if kind == "green" and not entry.get("green"):
                            entry.pop("green_exempt", None)
                        _save(data)
                        return {
                            "zone": dict(zone),
                            "scope": "worktree",
                            "owner": wt,
                            "kind": kind,
                        }
    return None


def green_exempt(wt: str) -> Dict[str, str]:
    """``{rel: "sha [sha …]"}`` (each a blob sha or ``"deleted"``, see
    :func:`exempt_ids`) — paths already changed outside the green scope when
    it was set (or narrowed). A monitor/gate skips such a path while its
    content still equals a recorded blob, so scoping a session mid-flight
    never turns its earlier work into breaches."""
    with _LOCK:
        data = _load()
        ex = _wt_entry(data, wt).get("green_exempt") or {}
        return {str(k): str(v) for k, v in ex.items()} if isinstance(ex, dict) else {}


def set_green_exempt(wt: str, mapping: Dict[str, str]) -> Dict[str, str]:
    """Merge ``{rel: sha}`` into the worktree's exemption set (an existing
    entry keeps its ORIGINAL sha — re-recording would absolve an edit made
    after the scope was set). Returns the new set."""
    key = os.path.realpath(wt)
    with _LOCK:
        data = _load()
        entry = data["worktrees"].setdefault(
            key, {"repo_id": None, "zones": [], "waive": []}
        )
        ex = entry.get("green_exempt")
        if not isinstance(ex, dict):
            ex = {}
        for rel, sha in (mapping or {}).items():
            if rel and rel not in ex:
                ex[str(rel)] = str(sha)
        entry["green_exempt"] = ex
        _save(data)
        return dict(ex)


def drop_green_exempt(wt: str, paths: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Drop ``paths`` (all when None) from the exemption set — "Treat as
    breaches". Returns what is left."""
    key = os.path.realpath(wt)
    with _LOCK:
        data = _load()
        entry = data["worktrees"].get(key) or data["worktrees"].get(wt)
        if not entry:
            return {}
        ex = entry.get("green_exempt")
        if not isinstance(ex, dict):
            ex = {}
        if paths is None:
            ex = {}
        else:
            for p in paths:
                ex.pop(p, None)
        if ex:
            entry["green_exempt"] = ex
        else:
            entry.pop("green_exempt", None)
        _save(data)
        return dict(ex)


def companions_config(repo_id: Optional[str]) -> List[str]:
    """The repo's configured companion patterns ("derived outputs": a built
    bundle, generated clients) — writable outside a green scope."""
    if not repo_id:
        return []
    with _LOCK:
        data = _load()
        pats = (data["repos"].get(repo_id) or {}).get("companions") or []
        return [str(p) for p in pats if isinstance(p, str) and p]


def set_companions(repo_id: str, patterns: Iterable[str], label: str = "") -> List[str]:
    """Replace a repo's companion patterns. ``ValueError`` on any invalid
    pattern (nothing is saved then). Returns the stored list."""
    out: List[str] = []
    for p in patterns or []:
        norm, anchored = normalize_pattern(str(p))
        stored = ("/" + norm) if anchored else norm
        if stored not in out:
            out.append(stored)
    with _LOCK:
        data = _load()
        repo = data["repos"].setdefault(
            repo_id, {"label": label or repo_id, "zones": [], "plan_first": False}
        )
        if label:
            repo["label"] = label
        repo["companions"] = out
        _save(data)
    return list(out)


def set_waiver(wt: str, zone_id: str, waived: bool) -> None:
    """Add or drop a repo-zone waiver for one worktree (session exception)."""
    key = os.path.realpath(wt)
    with _LOCK:
        data = _load()
        entry = data["worktrees"].setdefault(
            key, {"repo_id": None, "zones": [], "waive": []}
        )
        waive = set(entry.get("waive") or [])
        if waived:
            waive.add(zone_id)
        else:
            waive.discard(zone_id)
        entry["waive"] = sorted(waive)
        _save(data)


def set_plan_first(repo_id: str, on: bool, label: str = "") -> None:
    with _LOCK:
        data = _load()
        repo = data["repos"].setdefault(
            repo_id, {"label": label or repo_id, "zones": [], "plan_first": False}
        )
        if label:
            repo["label"] = label
        repo["plan_first"] = bool(on)
        _save(data)


def plan_first(repo_id: Optional[str]) -> bool:
    if not repo_id:
        return False
    with _LOCK:
        data = _load()
        return bool((data["repos"].get(repo_id) or {}).get("plan_first"))


def forget_worktree(wt: str) -> None:
    """Drop a worktree's own zones + waivers (called when it is removed)."""
    key = os.path.realpath(wt)
    with _LOCK:
        data = _load()
        if key in data["worktrees"] or wt in data["worktrees"]:
            data["worktrees"].pop(key, None)
            data["worktrees"].pop(wt, None)
            _save(data)


def all_repos() -> Dict[str, dict]:
    """``{repo_id: {"label", "zones", "plan_first", "companions"}}`` (fresh
    copies; ``zones`` are the repo's red zones — green is worktree-only)."""
    with _LOCK:
        data = _load()
        out: Dict[str, dict] = {}
        for rid, repo in data["repos"].items():
            out[rid] = {
                "label": repo.get("label") or rid,
                "zones": [dict(z) for z in (repo.get("zones") or [])],
                "plan_first": bool(repo.get("plan_first")),
                "companions": [
                    str(p) for p in (repo.get("companions") or []) if isinstance(p, str)
                ],
            }
        return out


# --------------------------------------------------------------------------- #
# Zone-file expansion (Bash backstop, ignored-file display, dry-run)
# --------------------------------------------------------------------------- #
def _git_z(root: str, *args: str, cap: int = 0) -> List[str]:
    """Run a NUL-delimited ``git ls-files`` and return the entries (best-effort;
    empty on failure). ``cap`` limits how many are scanned."""
    try:
        cp = subprocess.run(
            ["git", "-C", root, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        return []
    if cp.returncode != 0:
        return []
    parts = cp.stdout.decode("utf-8", "replace").split("\0")
    entries = [p for p in parts if p]
    return entries[:cap] if cap else entries


# Safety bound on the ignored-file listing when it cannot be scoped by pathspec
# (a rule with a glob in its first segment, or a bare regex rule). Hitting it
# sets ``truncated`` — it never silently drops a zoned file.
_IGNORED_SCAN_MAX = 250000


def _icase_pathspec(spec: str) -> str:
    """``:(literal)x`` → ``:(literal,icase)x`` (and the same for ``glob``): the
    case-insensitive twin of a pathspec from :func:`_ignored_pathspecs`."""
    if spec.startswith(":(") and ")" in spec:
        magic, rest = spec[2:].split(")", 1)
        return ":(" + magic + ",icase)" + rest
    return ":(icase)" + spec


def _ignored_pathspecs(rules: List[dict]) -> Optional[List[str]]:
    """Git pathspecs covering (a SUPERSET of) every path the rules can match, or
    None when some rule cannot be scoped — the caller then lists the whole
    ignored tree. The regex stays the authority; these only prune the walk.

    Why: ``ls-files --ignored`` is sorted, so a big ``.venv/`` or
    ``node_modules/`` fills any scan cap first and an ignored zoned file that
    sorts after it (``config/local.toml``, the headline "my configuration file"
    case) vanished from the guard, the Map and the monitor. Scoping the walk to
    where a zone can live makes the listing tiny instead of capped.

    - rooted pattern (anchored, or with a ``/``): its literal leading directory
      (``backend/**/x.py`` → ``backend``), or the whole literal path;
    - basename pattern using only ``*`` / ``?`` (same meaning under git's
      ``:(glob)`` magic): ``**/<pat>`` and ``**/<pat>/**`` — any depth;
    - anything else (a bracket class, ``**`` in a basename, a glob in the first
      segment of a rooted pattern, a rule with no ``pattern``) → None.
    """
    specs: List[str] = []
    for r in rules:
        pat = r.get("pattern")
        if not isinstance(pat, str) or not pat:
            return None
        try:
            norm, anchored = normalize_pattern(pat)
        except ValueError:
            return None
        body = norm.rstrip("/")
        if not body:
            return None
        globs = [body.find(c) for c in "*?[" if c in body]
        gi = min(globs) if globs else -1
        if anchored or "/" in body:
            if gi < 0:
                specs.append(":(literal)" + body)
                continue
            cut = body.rfind("/", 0, gi)
            if cut <= 0:
                return None
            specs.append(":(literal)" + body[:cut])
            continue
        if "[" in body or "**" in body:
            return None
        specs.append(":(glob)**/" + body)
        specs.append(":(glob)**/" + body + "/**")
    return specs


def zone_files(
    root: str, rules: List[dict], cap: int = 5000, ci: Optional[bool] = None
) -> Tuple[List[str], List[str], List[str], bool]:
    """``(files, dirs, ignored, truncated)`` — the zone-matched paths in ``root``.

    ``files`` are tracked+untracked matches; ``ignored`` are matched git-ignored
    files (this is how "my configuration file", usually gitignored, becomes
    visible and guarded). ``dirs`` are the parent dirs of matched files plus any
    dir a rule matches directly. Caps keep the Pre-hook off any recursive walk.

    The ignored listing is scoped by pathspecs derived from the rules'
    ``pattern`` (see :func:`_ignored_pathspecs`), so a huge ignored ``.venv``
    can no longer crowd a zoned file out; when a rule can't be scoped the whole
    ignored tree is listed and only a very large bound sets ``truncated``.

    ``ci`` (probed from ``root`` when None) makes matching case-insensitive on a
    case-insensitive filesystem, the same rule the hook applies from the guard's
    ``ci`` flag — otherwise a ``Config/`` zone on macOS would guard nothing in
    ``config/`` for the Bash backstop, the Map and the monitor.
    """
    srcs = [r.get("re") for r in rules if r.get("re")]
    if not srcs:
        return [], [], [], False
    if ci is None:
        ci = _probe_case_insensitive(os.path.realpath(root))
    flags = re.IGNORECASE if ci else 0
    compiled = []
    for src in srcs:
        try:
            compiled.append(re.compile(src, flags))
        except re.error:
            continue

    def _hit(rel: str) -> bool:
        return any(c.match(rel) for c in compiled)

    tracked = _git_z(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    # Git pathspecs match case-sensitively by default, so on a case-insensitive
    # root (the macOS default) each one gains the `icase` magic. Dropping the
    # scoping there instead would walk the whole ignored tree, where a big
    # `.venv` fills the scan bound before a gitignored zoned config file.
    specs = _ignored_pathspecs(rules)
    if specs and ci:
        specs = [_icase_pathspec(s) for s in specs]
    ign_args = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"]
    if specs:
        ign_args += ["--"] + specs
    ignored_all = _git_z(root, *ign_args)
    files: List[str] = []
    ignored: List[str] = []
    truncated = False
    if len(ignored_all) > _IGNORED_SCAN_MAX:
        ignored_all = ignored_all[:_IGNORED_SCAN_MAX]
        truncated = True
    for rel in tracked:
        if _hit(rel):
            files.append(rel)
            if len(files) >= cap:
                truncated = True
                break
    for rel in ignored_all:
        if _hit(rel):
            files.append(rel)
            ignored.append(rel)
            if len(files) >= cap:
                truncated = True
                break

    dirs = set()
    for rel in files:
        parent = os.path.dirname(rel)
        while parent:
            dirs.add(parent)
            if len(dirs) >= 500:
                break
            parent = os.path.dirname(parent)
        if len(dirs) >= 500:
            break
    # A rule that names a directory directly (matches the dir path itself).
    for rel in files:
        d = os.path.dirname(rel)
        if d and _hit(d):
            dirs.add(d)
    return sorted(set(files)), sorted(dirs), sorted(set(ignored)), truncated


# --------------------------------------------------------------------------- #
# Guard file
# --------------------------------------------------------------------------- #
def _control_protect_set(root: str) -> List[str]:
    """Absolute paths + dir prefixes the hook must never let the agent edit:
    the stores that hold the zones, and the hook-config files that arm it."""
    home = os.path.expanduser("~")
    out = [
        store_path(),
        guard_dir(),
        feed_dir(),
        os.path.join(root, ".claude", "settings.json"),
        os.path.join(root, ".claude", "settings.local.json"),
        os.path.join(root, ".codex", "hooks.json"),
        os.path.join(home, ".claude", "settings.json"),
        os.path.join(home, ".claude", "settings.local.json"),
    ]
    # Absolute + de-duplicated, keeping order.
    seen = set()
    result = []
    for p in out:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            result.append(ap)
    return result


def case_insensitive(root: str) -> bool:
    """Public face of the case-sensitivity probe: whether zone matching under
    ``root`` must ignore case (what the guard's ``ci`` flag records)."""
    return _probe_case_insensitive(os.path.realpath(root))


def _probe_case_insensitive(root: str) -> bool:
    """Whether ``root``'s filesystem is case-insensitive. Best-effort — False
    on any doubt.

    The BASENAME is swapped first (stat ``parent/rOOT`` and compare inodes):
    swapping the whole path walks through every ancestor, and on WSL a repo
    under ``/mnt/c`` (case-insensitive NTFS) sits below ``/mnt`` on ext4,
    where ``/MNT`` doesn't exist — the full swap said "sensitive" for a
    folder that is not, which fails OPEN for red and CLOSED for green. With
    no letter in the basename, git's own ``core.ignorecase`` (set at init
    from the same probe) decides."""
    try:
        st = os.stat(root)
    except OSError:
        return False
    trimmed = root.rstrip("/") or root
    base = os.path.basename(trimmed)
    if base and base.swapcase() != base:
        swapped = os.path.join(os.path.dirname(trimmed), base.swapcase())
        try:
            st2 = os.stat(swapped)
        except OSError:
            return False
        return (st2.st_ino, st2.st_dev) == (st.st_ino, st.st_dev)
    val = _git(root, "config", "--bool", "core.ignorecase")
    return (val or "").strip().lower() == "true"


def _guard_rules(zones: List[dict]) -> List[dict]:
    """A guard rule list — non-waived zones only, minimal shape."""
    rules = []
    for z in zones:
        if z.get("waived"):
            continue
        rules.append(
            {
                "id": z.get("id"),
                "pattern": z.get("pattern"),
                "name": z.get("name") or "",
                "scope": z.get("scope"),
                "re": z.get("re") or compile_pattern(z["pattern"]),
            }
        )
    return rules


def _sym_targets(root: str, files: List[str]) -> List[str]:
    """Realpaths of zone-matched symlinks that point OUTSIDE ``root`` — the
    "config.toml -> ../shared/config.toml" case the realpath match would miss."""
    out = set()
    root_real = os.path.realpath(root)
    for rel in files:
        p = os.path.join(root, rel)
        try:
            if os.path.islink(p):
                real = os.path.realpath(p)
                if real != root_real and not real.startswith(root_real + os.sep):
                    out.add(real)
        except OSError:
            continue
    return sorted(out)


def _guard_content_hash(content: dict) -> str:
    """Stable hash of a guard doc ignoring ``ts``."""
    c = {k: v for k, v in content.items() if k != "ts"}
    return hashlib.sha1(
        json.dumps(c, sort_keys=True).encode("utf-8", "replace")
    ).hexdigest()


def sync_guard(
    root: str,
    repo_id: Optional[str] = None,
    *,
    breaches: Optional[List[str]] = None,
    lroot: Optional[str] = None,
) -> str:
    """Write ``root``'s guard file to reflect the current store.

    Returns ``"written"`` (created/changed), ``"unchanged"`` (already current),
    ``"healed"`` (the file was tampered with — it differed from what we last
    wrote although the store had not changed since), or ``"skipped"`` (the repo
    identity could not be resolved, so we leave any existing file untouched
    rather than dropping rules over a transient git error).

    Holds ``_LOCK`` across the whole compute+write so a route and the reconcile
    tick can never race a stale guard onto disk, and compares against the
    ON-DISK bytes (not an in-memory memo) because another process may have
    deleted or edited the file.
    """
    real_root = os.path.realpath(root)
    # The test-companion scan reads the import graph (seconds on a cold big
    # repo): warm its memo BEFORE taking the store lock so a route and the
    # tick don't queue behind it; inside the lock it is a memo hit.
    try:
        pre_id = repo_id
        if pre_id is None:
            ident0 = repo_identity(real_root)
            pre_id = ident0[0] if ident0 else None
        pre_green = [
            z
            for z in effective_zones(lroot or real_root, pre_id)
            if z.get("kind") == "green"
        ]
        if pre_green:
            test_companions(lroot or real_root, pre_green)
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        if repo_id is None:
            ident = repo_identity(real_root)
            if ident is None:
                return "skipped"
            repo_id = ident[0]
        zones = effective_zones(lroot or real_root, repo_id)
        rules = _guard_rules([z for z in zones if z.get("kind", "red") == "red"])
        green_rules = _guard_rules([z for z in zones if z.get("kind") == "green"])
        files: List[str] = []
        dirs: List[str] = []
        sym: List[str] = []
        protect: List[str] = []
        companions: List[dict] = []
        ci = _probe_case_insensitive(real_root)
        if rules:
            files, dirs, _ignored, _trunc = zone_files(real_root, rules, ci=ci)
            sym = _sym_targets(real_root, files)
        # enforcing(g) = rules or green_rules: the control files must be
        # protected whichever kind is armed (a green-only guard that left
        # the zone store writable was a one-command bypass).
        if rules or green_rules:
            protect = _control_protect_set(real_root)
        if green_rules:
            tests = test_companions(lroot or real_root, green_rules)
            companions = [
                {"pattern": c["pattern"], "re": c["re"]}
                for c in _companion_rules(repo_id, tests)
            ]
        content = {
            # v2: green lives in `green_rules`/`companions`; `rules`, `files`,
            # `dirs` and `sym` stay RED-only, so a v1 hook still baked into a
            # hooks file reads this guard exactly as it always did.
            "v": 2,
            "root": real_root,
            "lroot": lroot or real_root,
            "ci": ci,
            "rules": rules,
            "green_rules": green_rules,
            "companions": companions,
            "protect": protect,
            "files": files,
            "dirs": dirs,
            "sym": sym,
            "breaches": list(breaches or []),
            "ts": int(time.time()),
        }
        new_hash = _guard_content_hash(content)
        path = guard_path(real_root)

        on_disk = None
        try:
            with open(path, encoding="utf-8") as f:
                on_disk = json.load(f)
        except Exception:  # noqa: BLE001 — absent/corrupt = must (re)write
            on_disk = None
        prev_exists = on_disk is not None
        on_disk_hash = _guard_content_hash(on_disk) if isinstance(on_disk, dict) else ""

        digest_now = store_digest()
        if prev_exists and on_disk_hash == new_hash:
            _GUARD_MEMO[real_root] = (new_hash, digest_now)
            return "unchanged"

        _write_guard_atomic(path, content)
        result = "written"
        last = _GUARD_MEMO.get(real_root)
        if prev_exists and last and last[1] == digest_now and last[0] != on_disk_hash:
            # We wrote it, the store hasn't changed since, yet the file on disk
            # differs -> something outside MindFlock edited it.
            result = "healed"
        _GUARD_MEMO[real_root] = (new_hash, digest_now)
        return result


def _write_guard_atomic(path: str, content: dict) -> None:
    import tempfile

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(content, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def sync_for_workdir(workdir: str) -> str:
    """Convenience for providers at hook-install time: resolve the identity and
    sync the guard. Best-effort, never raises."""
    try:
        real = os.path.realpath(workdir)
        return sync_guard(real, lroot=workdir)
    except Exception:  # noqa: BLE001 — hook install must never break a launch
        return "skipped"


def remove_guard(root: str) -> None:
    try:
        os.unlink(guard_path(root))
    except OSError:
        pass
    _GUARD_MEMO.pop(os.path.realpath(root), None)


def gc_guards(live_roots, max_age_s: int = 86400) -> int:
    """Delete guard files whose root is no longer live AND older than
    ``max_age_s``. Returns the count removed. Never raises."""
    keep = set()
    for r in live_roots or ():
        try:
            keep.add(os.path.basename(guard_path(r)))
        except Exception:  # noqa: BLE001
            continue
    removed = 0
    d = guard_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    now = time.time()
    for name in names:
        if not name.endswith(".json") or name in keep:
            continue
        p = os.path.join(d, name)
        try:
            if now - os.stat(p).st_mtime <= max_age_s:
                continue
            os.unlink(p)
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------- #
# Prompt texts + decoration
# --------------------------------------------------------------------------- #
PLAN_PROMPT = (
    "Before changing anything: list every file you intend to create, modify or "
    "delete for this task, with a one-line intent for each, in a fenced code "
    "block tagged mindflock-plan — one `path — intent` per line, paths relative "
    "to the repo root. Then stop and wait for my go-ahead."
)

REMAINING_PROMPT = (
    "Pause before your next edit: list the files you have changed so far and "
    "every file you still intend to change, each with a one-line intent, in a "
    "fenced code block tagged mindflock-plan — one `path — intent` per line, "
    "paths relative to the repo root. Then stop and wait for my go-ahead."
)

_RED_ZONE_NOTE_MARKER = "Red zones (MindFlock"


def _zone_label(z: dict) -> str:
    """`` `pattern` `` plus `` (name)`` when named — for the go/note messages."""
    p = "`%s`" % z.get("pattern", "")
    name = z.get("name") or ""
    return p + (" (%s)" % name if name else "")


def _names(zones: List[dict], cap: int = _LABEL_CAP) -> str:
    """``name-or-pattern, …`` of ``zones``, at most ``cap`` then ``(+N more)``."""
    labels = []
    for z in zones or []:
        lab = (
            (z.get("name") or z.get("pattern") or "") if isinstance(z, dict) else str(z)
        )
        if lab and lab not in labels:
            labels.append(lab)
    shown = ", ".join(labels[:cap])
    if len(labels) > cap:
        shown += " (+%d more)" % (len(labels) - cap)
    return shown


def green_label(zones: List[dict]) -> str:
    """The zone list a green deny reason names (the hook's
    ``_mf_green_label`` renders the same string from the guard)."""
    return _names(zones) or "the green zones"


def green_deny_reason(rel: str, zones: List[dict]) -> str:
    """The reason the guard shows the model for an edit outside the scope."""
    return _GREEN_DENY_TMPL.format(rel=rel or "this path", label=green_label(zones))


def _ticks(zones: List[dict], cap: int = 8) -> str:
    pats = []
    for z in zones or []:
        p = z.get("pattern") if isinstance(z, dict) else str(z)
        if p and p not in pats:
            pats.append(p)
    shown = ", ".join("`%s`" % p for p in pats[:cap])
    if len(pats) > cap:
        shown += " (+%d more)" % (len(pats) - cap)
    return shown


def _outside_consequence(hard: bool) -> str:
    return (
        "edits outside are blocked"
        if hard
        else "changes outside are flagged and block pushes"
    )


def go_message(
    zones: List[dict], scope: Optional[List[dict]] = None, hard: bool = True
) -> str:
    """The single message the **Go** button sends, carrying the staged zones.
    ``scope`` (the "Go — only the planned files" action) = the green zones
    just created from the plan."""
    red = [z for z in zones or [] if z.get("kind", "red") != "green"]
    parts = ["Go ahead with your plan"]
    if red:
        joined = ", ".join(_zone_label(z) for z in red)
        parts[0] += (
            ", with these changes: do not modify "
            + joined
            + " — they are red zones and edits there are blocked. Adjust the "
            "plan around them; if the task can't be done without touching one, "
            "stop and tell me which file and why."
        )
    else:
        parts[0] += "."
    if scope:
        parts.append(
            "Scope: only modify the planned files — %s (MindFlock green zones; "
            "%s). If you need a file that isn't in your plan, finish the "
            "in-scope work and list it in your reply instead of editing it."
            % (_ticks(scope), _outside_consequence(hard))
        )
    else:
        parts.append(
            "If you need a file that isn't in your plan, say so before editing it."
        )
    return " ".join(parts)


def zone_added_message(
    zone: dict, *, hard: bool = True, green: Optional[List[dict]] = None
) -> str:
    """The message sent when a zone is added mid-flight with "tell the agent".

    A GREEN zone never says "revert": what the agent changed before the
    scope existed was legitimate (and is exempt); throwing it away is the
    worst possible reading. ``green`` = every green zone now in force (the
    scope the message names)."""
    if zone.get("kind") == "green":
        scope = green or [zone]
        return (
            "MindFlock: your scope is now limited to %s (green zones) — only "
            "modify files inside it; %s. Keep what you already changed. If the "
            "task needs a file outside, finish the in-scope work and list it in "
            "your reply instead of editing it."
            % (_ticks(scope), _outside_consequence(hard))
        )
    if not hard:
        return (
            "MindFlock: `%s` is now a red zone — do not modify it; changes "
            "there are flagged and block pushes. Don't route around it "
            "(copies, wrappers, shell edits). If you can't finish without "
            "touching it, stop and tell me why." % zone.get("pattern", "")
        )
    return (
        "MindFlock: `%s` is now a red zone — edits there are blocked. Don't "
        "modify it or route around it (copies, wrappers, shell edits). If you "
        "already changed files there, revert those changes with git. If you "
        "can't finish without touching it, stop and tell me why."
        % zone.get("pattern", "")
    )


def green_scope_message(
    removed: Optional[dict], remaining: List[dict], *, hard: bool = True
) -> str:
    """The notice when the green scope NARROWS or goes away (a zone removed),
    or widens by an allow (``removed=None``): the agent must stop avoiding —
    or stop editing — paths whose status just changed."""
    if not remaining:
        return (
            "MindFlock: the green-zone scope was removed — you may modify files "
            "anywhere in the repo again (red zones still apply)."
        )
    if removed is not None:
        return (
            "MindFlock: `%s` is no longer in your scope. Only modify files "
            "inside %s; %s. Keep what you already changed there."
            % (
                removed.get("pattern", ""),
                _ticks(remaining),
                _outside_consequence(hard),
            )
        )
    return (
        "MindFlock: your scope was widened — you may now also edit %s. Current "
        "scope: %s." % (_ticks(remaining[-1:]), _ticks(remaining))
    )


def deny_reason(rel: str, zone: dict, green: Optional[List[dict]] = None) -> str:
    """The reason string the guard shows the model on a blocked edit (a green
    ``zone``/``green`` list → the scope reason)."""
    if zone.get("kind") == "green" or zone.get("pattern") == GREEN_OUTSIDE:
        return green_deny_reason(rel, green or [])
    label = zone.get("name") or zone.get("pattern") or rel
    return _DENY_REASON_TMPL.format(rel=rel, label=label)


_GREEN_NOTE_MARKER = "Scope (MindFlock green zones)"


def decorate_prompt(
    prompt: str,
    workdir: str,
    *,
    hard_guard: bool,
    plan_first: Optional[bool] = None,
) -> str:
    """Append the plan-first instruction, a red-zones note and/or a green
    scope note to a launch prompt. Idempotent (never appends twice) and safe
    on an empty prompt.
    """
    if not prompt:
        return prompt
    out = prompt
    try:
        real = os.path.realpath(workdir) if workdir else ""
        ident = repo_identity(real) if real else None
        repo_id = ident[0] if ident else None
    except Exception:  # noqa: BLE001 — decoration is best-effort
        real, repo_id = "", None

    want_plan = plan_first if plan_first is not None else plan_first_flag(repo_id)
    if want_plan and PLAN_PROMPT not in out:
        out = out + "\n\n---\n\n" + PLAN_PROMPT

    if not real:
        return out
    try:
        allz = [z for z in effective_zones(real, repo_id) if not z.get("waived")]
    except Exception:  # noqa: BLE001
        allz = []
    zones = [z for z in allz if z.get("kind", "red") != "green"]
    green = [z for z in allz if z.get("kind") == "green"]
    if zones and _RED_ZONE_NOTE_MARKER not in out:
        joined = ", ".join("`%s`" % z.get("pattern", "") for z in zones)
        if hard_guard:
            note = "Red zones (MindFlock blocks edits here): " + joined
        else:
            note = (
                "Red zones (MindFlock — do not modify these paths; changes "
                "there are flagged and block pushes): " + joined
            )
        out = out + "\n\n" + note
    if green and _GREEN_NOTE_MARKER not in out:
        out = out + (
            "\n\n%s: only modify files under %s — everything else is "
            "read-only (%s)."
            % (_GREEN_NOTE_MARKER, _ticks(green), _outside_consequence(hard_guard))
        )
    return out


def plan_first_flag(repo_id: Optional[str]) -> bool:
    """Alias kept for readability inside :func:`decorate_prompt`."""
    return plan_first(repo_id)
