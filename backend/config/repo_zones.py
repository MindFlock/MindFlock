"""Shared zones — the committed ``.mindflock/zones.toml`` at a repo's root.

Local zones (:mod:`backend.config.red_zones`, ``~/.mindflock/red_zones.json``)
belong to one machine: a "whole repo" zone there is keyed by the repo's origin
URL, but a teammate's MindFlock never sees it. A zone the TEAM shares has to
travel with the code, so it lives in the repo, reviewed like any other change:

.. code-block:: toml

    [[zone]]
    kind = "red"             # red = agents may read, never change
    pattern = "config.toml"  # the same gitignore-flavoured globs as local zones
    note = "prod settings"
    author = "Ethan"         # the person who added it (docs/people.md)
    added = 2026-10-10

One ``[[zone]]`` per entry; ``kind`` is ``red`` (default) or ``green`` (agents
may ONLY change files here); ``name`` is optional; unknown keys are ignored so
a newer MindFlock can add fields without breaking an older one. No machine
paths, no secrets — just patterns, words and a date.

The file is read from the worktree being guarded (``<wt>/.mindflock/zones.toml``),
so it follows the BRANCH: a zone added in a session's worktree applies there at
once and reaches everyone else when the branch merges. :func:`load` is
memoized on the file's ``(mtime, size)``; a file that is not valid TOML, or
not shaped like the above, is ignored as a whole and logged once per version
(the guard fails open on it, exactly like a missing file) — a bad entry inside
a good file is skipped with its own logged line.

Precedence (:func:`merge`, used by ``red_zones.effective_zones``): local and
shared zones both apply. A shared zone identical to a local one (same kind,
same normalized pattern) is folded into the local zone — which then carries
``shared: true`` and the shared ``author`` — UNLESS the local copy is waived in
this worktree ("allow here" is a local choice and must not switch off a rule
the repo committed). Red against green on the same path is decided by
``red_zones.classify``: red always wins.

Stdlib only (``tomllib``), no import of ``backend.web``; never raises on read.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import threading
from typing import Dict, List, Optional, Tuple

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover — the app requires 3.12
    _toml = None  # type: ignore[assignment]

__all__ = [
    "REL_PATH",
    "file_path",
    "load",
    "load_doc",
    "zone_id",
    "parse",
    "render_zone",
    "add",
    "remove",
    "merge",
    "today",
]

_logger = logging.getLogger(__name__)

#: Where the shared zones live, relative to the repo (worktree) root.
REL_PATH = ".mindflock/zones.toml"

_KINDS = ("red", "green")

#: The header written into a brand-new file — a reviewer opening the diff
#: should know what it is without reading MindFlock's docs.
_HEADER = (
    "# Zones MindFlock enforces for everyone who works on this repo.\n"
    '#   kind = "red"   agents may read these paths, never change them\n'
    '#   kind = "green" agents may ONLY change these paths\n'
    "# Patterns are gitignore-style globs relative to the repo root. Plain TOML:\n"
    "# review changes here like code.\n"
)

_LOCK = threading.Lock()
#: realpath -> (stamp, zones, error)
_MEMO: Dict[str, Tuple[tuple, List[dict], str]] = {}
#: (realpath, stamp) pairs already logged as bad, so a broken file is one log
#: line per version, not one per guard tick.
_LOGGED: set = set()


def file_path(wt: str) -> str:
    """``<wt>/.mindflock/zones.toml``."""
    return os.path.join(wt, *REL_PATH.split("/"))


def today() -> str:
    return _dt.date.today().isoformat()


def _norm_key(pattern: str) -> Optional[Tuple[str, bool]]:
    from backend.config.red_zones import normalize_pattern

    try:
        return normalize_pattern(pattern)
    except (TypeError, ValueError):
        return None


def _stored(pattern: str) -> str:
    """The pattern as a zone stores it (the ``/`` anchor kept)."""
    key = _norm_key(pattern)
    if key is None:
        raise ValueError("invalid pattern")
    norm, anchored = key
    return ("/" + norm) if anchored else norm


def zone_id(kind: str, pattern: str) -> str:
    """A stable id for a shared zone: the same entry gets the same id on every
    machine and every read (``sz_`` + 10 hex of ``kind\\0pattern``)."""
    raw = ("%s\0%s" % (kind, pattern)).encode("utf-8", "replace")
    return "sz_" + hashlib.sha1(raw).hexdigest()[:10]


def _date_str(value) -> str:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()[:10]
    return str(value or "").strip()[:32]


def _text(value, cap: int = 400) -> str:
    return str(value or "").strip()[:cap] if isinstance(value, (str, int)) else ""


def parse(text: str) -> Tuple[List[dict], List[str]]:
    """``(zones, problems)`` for a file's text. Raises ``ValueError`` when the
    file as a whole is unusable (not TOML, ``zone`` not an array of tables);
    a single bad entry is left out and named in ``problems``.

    Each zone: ``{"id", "kind", "pattern", "name", "note", "author",
    "added"}`` — ``pattern`` as a local zone stores it (anchor kept)."""
    if _toml is None:  # pragma: no cover
        raise ValueError("tomllib unavailable")
    try:
        doc = _toml.loads(text)
    except Exception as err:  # noqa: BLE001 — tomllib.TOMLDecodeError
        raise ValueError("not valid TOML: %s" % err) from None
    entries = doc.get("zone", [])
    if not isinstance(entries, list):
        raise ValueError("`zone` must be an array of tables ([[zone]])")
    out: List[dict] = []
    problems: List[str] = []
    seen = set()
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            problems.append("zone %d is not a table" % i)
            continue
        kind = str(e.get("kind") or "red").strip().lower()
        if kind not in _KINDS:
            problems.append("zone %d: kind must be red or green" % i)
            continue
        pat = e.get("pattern")
        if not isinstance(pat, str):
            problems.append("zone %d: pattern is missing" % i)
            continue
        try:
            stored = _stored(pat)
        except ValueError:
            problems.append("zone %d: invalid pattern %r" % (i, pat[:80]))
            continue
        if (kind, stored) in seen:
            continue
        seen.add((kind, stored))
        out.append(
            {
                "id": zone_id(kind, stored),
                "kind": kind,
                "pattern": stored,
                "name": _text(e.get("name"), 120),
                "note": _text(e.get("note")),
                "author": _text(e.get("author"), 120),
                "added": _date_str(e.get("added")),
            }
        )
    return out, problems


def _stamp(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def load_doc(wt: str) -> Tuple[List[dict], str]:
    """``(zones, error)`` for ``wt``'s shared file: ``([], "")`` when there is
    none, ``([], <why>)`` when it is unusable (logged once per version).
    Fresh copies; never raises."""
    if not wt:
        return [], ""
    try:
        real = os.path.realpath(wt)
        path = file_path(real)
        stamp = _stamp(path)
        if stamp is None:
            with _LOCK:
                _MEMO.pop(real, None)
            return [], ""
        with _LOCK:
            hit = _MEMO.get(real)
        if hit is not None and hit[0] == stamp:
            return [dict(z) for z in hit[1]], hit[2]
        try:
            with open(path, "rb") as f:
                raw = f.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise ValueError("file is larger than 512 KiB")
            zones, problems = parse(raw.decode("utf-8"))
            error = ""
        except (OSError, UnicodeDecodeError, ValueError) as err:
            zones, problems, error = [], [], str(err) or type(err).__name__
        key = (real, stamp)
        if key not in _LOGGED and (error or problems):
            _LOGGED.add(key)
            if len(_LOGGED) > 1024:
                _LOGGED.clear()
            if error:
                _logger.warning("Ignoring shared zones file %s: %s", path, error)
            for p in problems:
                _logger.warning("Shared zones file %s: skipping %s", path, p)
        with _LOCK:
            _MEMO[real] = (stamp, zones, error)
            if len(_MEMO) > 512:
                _MEMO.clear()
        return [dict(z) for z in zones], error
    except Exception as err:  # noqa: BLE001 — a reader never raises
        return [], str(err)


def load(wt: str) -> List[dict]:
    """The shared zones in force for ``wt`` (``[]`` when none or unusable)."""
    return load_doc(wt)[0]


# --------------------------------------------------------------------------- #
# Writing (the Zones dialog's "Add to repo")
# --------------------------------------------------------------------------- #
def _q(s: str) -> str:
    """A TOML basic string."""
    out = []
    for ch in str(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render_zone(z: dict) -> str:
    """One ``[[zone]]`` block (no leading blank line)."""
    lines = ["[[zone]]", "kind = " + _q(z.get("kind") or "red")]
    lines.append("pattern = " + _q(z["pattern"]))
    if z.get("name"):
        lines.append("name = " + _q(z["name"]))
    if z.get("note"):
        lines.append("note = " + _q(z["note"]))
    if z.get("author"):
        lines.append("author = " + _q(z["author"]))
    added = _date_str(z.get("added"))
    try:
        _dt.date.fromisoformat(added)
        lines.append("added = " + added)  # a bare TOML date
    except ValueError:
        if added:
            lines.append("added = " + _q(added))
    return "\n".join(lines) + "\n"


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write_atomic(path: str, text: str) -> None:
    from backend.fsutil import atomic_write_text

    atomic_write_text(path, text, prefix="tmp")


def add(
    wt: str,
    pattern: str,
    *,
    kind: str = "red",
    note: str = "",
    name: str = "",
    author: str = "",
    added: str = "",
) -> Tuple[dict, bool]:
    """Append a zone to ``wt``'s shared file (created with a header when
    absent) → ``(zone, created)``; ``created`` is False when the same kind +
    pattern is already there (nothing is written).

    The rest of the file is kept byte for byte — comments included — so the
    diff a reviewer sees is exactly the new block. ``ValueError`` on a bad
    pattern/kind or when the existing file is unusable (fix it first; it is
    never overwritten). :class:`backend.config.red_zones.ZoneConflict` when
    the same pattern is already the other kind there."""
    from backend.config.red_zones import ZoneConflict

    kind = (kind or "red").strip().lower()
    if kind not in _KINDS:
        raise ValueError("kind must be 'red' or 'green'")
    stored = _stored(pattern)
    path = file_path(wt)
    with _LOCK:
        text = _read_text(path)
        current: List[dict] = []
        if text is not None:
            try:
                current, _problems = parse(text)
            except ValueError as err:
                raise ValueError(
                    "%s is not usable (%s) — fix it before adding to it"
                    % (REL_PATH, err)
                ) from None
        key = (_norm_key(stored) or ("", False))[0].rstrip("/")
        for z in current:
            zkey = (_norm_key(z["pattern"]) or ("", False))[0].rstrip("/")
            if z["kind"] == kind and _norm_key(z["pattern"]) == _norm_key(stored):
                return dict(z), False
            if z["kind"] != kind and zkey == key:
                raise ZoneConflict(
                    "`%s` is already a %s zone in %s — a path can't be both kept "
                    "out and the only place allowed" % (stored, z["kind"], REL_PATH)
                )
        zone = {
            "id": zone_id(kind, stored),
            "kind": kind,
            "pattern": stored,
            "name": _text(name, 120),
            "note": _text(note),
            "author": _text(author, 120),
            "added": added or today(),
        }
        block = render_zone(zone)
        if text is None or not text.strip():
            new = _HEADER + "\n" + block
        else:
            new = text if text.endswith("\n") else text + "\n"
            new += "\n" + block
        _write_atomic(path, new)
        _MEMO.pop(os.path.realpath(wt), None)
    return zone, True


def _chunks(text: str) -> List[str]:
    """``text`` split before every ``[[zone]]`` header line (chunk 0 is the
    preamble). The comment lines directly above a header (no blank line in
    between) belong to that header's zone — except in the preamble, which
    keeps the file's own header comment. Joining the chunks gives ``text``
    back exactly."""
    out: List[List[str]] = []
    cur: List[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("[[zone]]") and cur:
            lead: List[str] = []
            if out:  # cur is a zone chunk, not the preamble
                while cur and cur[-1].lstrip().startswith("#"):
                    lead.insert(0, cur.pop())
            out.append(cur)
            cur = lead
        cur.append(line)
    out.append(cur)
    return ["".join(c) for c in out]


def remove(wt: str, zid: str) -> Optional[dict]:
    """Remove the shared zone ``zid`` from ``wt``'s file, keeping every other
    byte (its own comment lines go with it). Returns the zone or None when it
    is not there. ``ValueError`` when the file is unusable."""
    path = file_path(wt)
    with _LOCK:
        text = _read_text(path)
        if text is None:
            return None
        try:
            parse(text)
        except ValueError as err:
            raise ValueError("%s is not usable (%s)" % (REL_PATH, err)) from None
        chunks = _chunks(text)
        kept: List[str] = []
        gone: Optional[dict] = None
        for i, ch in enumerate(chunks):
            if i == 0 and not ch.lstrip().startswith("[[zone]]"):
                kept.append(ch)
                continue
            try:
                zs, _p = parse(ch)
            except ValueError:
                zs = []
            if gone is None and zs and zs[0]["id"] == zid:
                gone = zs[0]
                continue
            kept.append(ch)
        if gone is None:
            return None
        new = "".join(kept)
        new = new.rstrip("\n") + "\n" if new.strip() else ""
        _write_atomic(path, new)
        _MEMO.pop(os.path.realpath(wt), None)
    return gone


# --------------------------------------------------------------------------- #
# Merging with the local zones
# --------------------------------------------------------------------------- #
def merge(local: List[dict], shared: List[dict]) -> List[dict]:
    """``local`` (zone views, see ``red_zones.effective_zones``) followed by
    the shared zones not already represented by an identical, NOT waived local
    zone. A local zone that represents a shared one is annotated in place with
    ``shared: True`` and ``shared_author``. See the module docstring."""
    from backend.config.red_zones import compile_pattern

    by_key: Dict[tuple, dict] = {}
    for z in local:
        if z.get("waived") or z.get("scope") == "session":
            continue
        k = _norm_key(z.get("pattern") or "")
        if k is not None:
            by_key.setdefault((z.get("kind") or "red", k), z)
    out = list(local)
    for s in shared:
        k = _norm_key(s["pattern"])
        twin = by_key.get((s["kind"], k)) if k is not None else None
        if twin is not None:
            twin["shared"] = True
            twin["shared_author"] = s.get("author") or ""
            continue
        try:
            rx = compile_pattern(s["pattern"])
        except ValueError:
            continue
        out.append(
            dict(
                s,
                scope="shared",
                re=rx,
                waived=False,
                shared=True,
                # Lifted by editing the committed file (or the Zones dialog),
                # never by a Map click in one worktree.
                locked=True,
                source=REL_PATH,
            )
        )
    return out
