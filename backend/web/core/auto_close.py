"""Finished sub-sessions close themselves.

An orchestrator agent spawns workers (MCP ``spawn_session``, team runs, Flock
mode); each worker sends ``report_result`` and its parent is told to clean it
up after merging. Agents often skip that, so finished workers sat idle in the
rail for hours, looking like live work. This sweep CLOSES them — agent
stopped, worktree and branch kept, the session in Recently closed like a
manual ✕ (never deleted) — and the parent's Thread keeps the worker's summary
(:mod:`backend.web.core.finished_children`), so nothing leaves the parent's
view.

A session is closed when ALL of these hold (:func:`judge`):

1. **It is a sub-session.** Spawned by an agent (``Instance.Spawned``) and
   still under a live parent, or orphaned by one (below). Never a session the
   person started (an adopted one — ``set_parent`` — is theirs too), an
   intake session (``pending.session_kind``), an in-place or peer-shared
   session, a feature's hidden helper (a Verify run), a member of a running
   team run, a session on a running route or the branch of a parent's running
   route, a worker with open workers of its own, nor a remote row (only this
   server's ENGINE is swept).
2. **It is finished.** Its newest ``report_result`` says done or failed —
   blocked means it waits on someone, so it stays — and nothing was sent to it
   since (a message, a queued prompt, a person typing); its agent is idle or
   gone (asleep counts), with no question or dialog up, no prompt queued, no
   mail waiting, no background job, not shipping by itself.
3. **Its work is accounted for.** A clean worktree, and its branch has no
   commits beyond its fork point, or every commit is reachable from its
   parent's HEAD (the reachability ``kill_session(mode="delete")`` checks), or
   its PR merged / was closed (a ``separate=true`` worker). Unmerged work is
   unfinished business, not clutter: it stays open. When its parent is gone
   it is an **orphan**: kept, and its row says "parent closed — review or
   close" (``auto_close.orphan_of``).
4. **The grace window passed** (``general.auto_close_grace_minutes``, 10 by
   default) since it finished — its report, or its last turn ending, whichever
   is later — so the parent can still ``read_output`` / ``get_diff``. A merge
   inside the window closes it when the window ends; a merge after it closes
   it on the next pass. And nobody is looking: no terminal attached to it in
   any connected UI (an open pane is one), no outside tmux client, nobody
   typed into it within the window.

A person's "Keep open" (the session's ⋯ menu, ``set_keep_open``) and
``general.auto_close_children: false`` turn it off for one session / all.

Each close is logged with its reason; one pass's closes are announced as ONE
``system.subsessions_closed`` event per parent (the app's toast with Undo,
which reopens them and marks them Keep open — :func:`undo`). The sweep waits
out the server's boot quiet window before its first pass and runs at most
:data:`MAX_PER_PASS` closes a pass, so a restart never floods anything.

**Two servers, one config dir.** The sweep runs in one of them
(``sleep.hold_lease``), and judges "gone" by disk state
(``engine.disk_view``): a session must be live in state.json to be closed,
and a parent is gone only when it is tombstoned there — never because it is
missing from THIS server's ENGINE (the other server may have just made it).

Persisted at ``<config dir>/auto_close.json`` (or ``$MINDFLOCK_AUTO_CLOSE_FILE``):
``{"keep_open": {title: {"created_at"}}, "orphans": {title: {"parent",
"parent_branch", "parent_folder", "created_at", "report", "at"}}}``. Entries
carry the session's creation time, so a reused title inherits nothing.
"""

from __future__ import annotations

import asyncio
import copy
import os
import threading
import time
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from backend import log, session
from backend.config.config import GetConfigDir
from backend.config.home_guard import guard
from backend.fsutil import atomic_write_json, read_json
from backend.session import tmux
from backend.web.core import title_store

__all__ = [
    "DEFAULT_GRACE_MINUTES",
    "EVENT",
    "LEASE",
    "enabled",
    "grace_seconds",
    "store_path",
    "keep_open",
    "set_keep_open",
    "orphan_of",
    "note_parent_gone",
    "forget",
    "rename",
    "row_field",
    "judge",
    "work_accounted",
    "tick",
    "run_loop",
    "undo",
]

#: ``general.auto_close_grace_minutes`` when unset.
DEFAULT_GRACE_MINUTES = 10
#: How often the sweep looks.
TICK_SECONDS = 60.0
#: At most this many closes per pass (each one stops an agent and saves the
#: engine; the next pass is a minute away).
MAX_PER_PASS = 6
#: The singleton lease (``sleep.hold_lease``) and how long it lasts unrenewed.
LEASE = "auto-close"
LEASE_TTL_S = 3 * TICK_SECONDS
#: The one event a pass emits per parent.
EVENT = "system.subsessions_closed"
#: Two creation times within this many seconds are the same session.
_SAME_S = 1.0

_FileName = "auto_close.json"

# Verdicts.
CLOSE = "close"
WAIT = "wait"  # finished and accounted for: closes once the window passes
KEEP = "keep"
ORPHAN = "orphan"  # parent gone and work not accounted for: kept, flagged

# Reasons (stable strings: the log, the row's note and the tests name them).
R_NOT_SUB = "not a sub-session"
R_PARENT_ELSEWHERE = "its parent is not on this server"
R_INTAKE = "an intake session"
R_IN_PLACE = "in place"
R_PEER = "peer-shared"
R_HIDDEN = "a feature's helper session"
R_KEEP_OPEN = "kept open"
R_TEAM = "in a team run"
R_ROUTE = "on a running route"
R_STATUS = "not running"
R_WORKSPACE = "workspace missing"
R_GONE = "not live on disk"
R_NO_REPORT = "has not reported"
R_BLOCKED = "blocked: waiting on someone"
R_NEW_WORK = "given more since its report"
R_BUSY = "not idle"
R_ASKING = "asking something"
R_BACKGROUND = "background jobs running"
R_QUEUE = "prompts queued"
R_MAIL = "messages waiting"
R_AUTOPILOT = "shipping by itself"
R_WORKERS = "its own workers are open"
R_DIRTY = "uncommitted changes"
R_UNMERGED = "unmerged commits"
R_UNKNOWN_GIT = "its work can't be inspected"
R_GRACE = "in its grace window"
R_TYPED = "typed in recently"
R_VIEWED = "open in a terminal"
R_DIALOG = "dialog on screen"

# Why a close was allowed (the log line).
W_NO_COMMITS = "no commits of its own"
W_MERGED = "merged into its parent"
W_PR_MERGED = "its PR merged"
W_PR_CLOSED = "its PR was closed"

_LOCK = threading.RLock()
#: How long a store transaction waits for the process lock before giving up.
_TXN_WAIT_S = 10.0
_CACHE_LOCK = threading.Lock()
#: The sweep's last verdict per title: ``{"verdict", "why", "closes_at"}``
#: (in memory, this server only — the row's ``closes_at``).
_VERDICTS: Dict[str, dict] = {}


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


def _info(fmt: str, *args) -> None:
    logger = log.InfoLog or log.ErrorLog
    if logger is not None:
        try:
            logger.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """``general.auto_close_children`` (unset = on). Never raises. A LOCAL
    import, read per call (the store caches by mtime)."""
    try:
        from backend.config import settings as _settings

        val = _settings.load_settings().general.auto_close_children
    except Exception:  # noqa: BLE001 — an unreadable store keeps the default
        return True
    return val is not False


def grace_seconds() -> float:
    """``general.auto_close_grace_minutes`` in seconds (unset =
    :data:`DEFAULT_GRACE_MINUTES`, at least one minute). Never raises."""
    try:
        from backend.config import settings as _settings

        val = _settings.load_settings().general.auto_close_grace_minutes
    except Exception:  # noqa: BLE001
        val = None
    minutes = DEFAULT_GRACE_MINUTES if val is None else max(1, int(val))
    return minutes * 60.0


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def store_path() -> str:
    """``$MINDFLOCK_AUTO_CLOSE_FILE`` (tests point it at a tmp file), else
    ``<config dir>/auto_close.json``. Refused under pytest when it resolves
    into the real home (``backend.config.home_guard``)."""
    env = os.environ.get("MINDFLOCK_AUTO_CLOSE_FILE")
    if env:
        return guard(env, "auto-close")
    return guard(os.path.join(GetConfigDir(), _FileName), "auto-close")


#: ``(path, mtime_ns, size)`` of the last read and what it parsed to: every
#: row build asks the store, and it rarely changes.
_CACHE: Dict[str, object] = {"sig": None, "data": None}


def _signature(p: str):
    try:
        st = os.stat(p)
    except OSError:
        return (p, None, None)
    return (p, st.st_mtime_ns, st.st_size)


def _load() -> Dict[str, Dict[str, dict]]:
    """The store (a private copy). Cached by the file's signature."""
    p = store_path()
    sig = _signature(p)
    with _CACHE_LOCK:
        if _CACHE["sig"] == sig and _CACHE["data"] is not None:
            return copy.deepcopy(_CACHE["data"])  # type: ignore[arg-type]
    data = _read(p)
    with _CACHE_LOCK:
        _CACHE["sig"], _CACHE["data"] = sig, copy.deepcopy(data)
    return data


def _read(p: str) -> Dict[str, Dict[str, dict]]:
    data = read_json(p, {}, expect=dict) or {}
    out: Dict[str, Dict[str, dict]] = {}
    for key in ("keep_open", "orphans"):
        part = data.get(key)
        out[key] = (
            {
                k: v
                for k, v in part.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
            if isinstance(part, dict)
            else {}
        )
    return out


def _save(data: Mapping) -> None:
    atomic_write_json(
        store_path(), dict(data), indent=None, newline=False, ensure_ascii=True
    )


class _Txn:
    """Read-modify-write the store under the process lock AND the file lock
    (the other server on this config dir writes it too)."""

    def __enter__(self) -> Dict[str, Dict[str, dict]]:
        # Anything that raises in here never reaches __exit__, so it must let
        # go of what it took itself: a leaked RLock is held by its thread for
        # good, and the next removal on another thread blocks forever on it.
        # Bounded: a removal path calls this, and bookkeeping must never be
        # what hangs a close — a stuck holder costs one record, not the route.
        if not _LOCK.acquire(timeout=_TXN_WAIT_S):
            raise TimeoutError("auto-close store busy")
        self._flock = None
        try:
            p = store_path()
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            self._flock = title_store.file_lock(p)
            self._flock.__enter__()
            self.data = _read(p)  # fresh under the lock, never the cache
            self._before = repr(self.data)
        except BaseException:
            try:
                if self._flock is not None:
                    self._flock.__exit__(None, None, None)
            finally:
                _LOCK.release()
            raise
        return self.data

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and repr(self.data) != self._before:
                _save(self.data)
        finally:
            try:
                self._flock.__exit__(None, None, None)
            finally:
                _LOCK.release()


def _num(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same(a: Optional[float], b: Optional[float]) -> bool:
    """Same session by creation time; an unknown time matches anything."""
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) < _SAME_S


def _created(inst) -> Optional[float]:
    at = getattr(inst, "CreatedAt", None)
    try:
        return float(at.timestamp()) if at is not None else None
    except Exception:  # noqa: BLE001
        return None


def _entry(part: str, title: str, created: Optional[float]) -> Optional[dict]:
    try:
        rec = _load()[part].get(title)
    except Exception:  # noqa: BLE001 — an unreadable store says nothing
        return None
    if rec is None or not _same(_num(rec.get("created_at")), created):
        return None
    return rec


def keep_open(title: str, created: Optional[float] = None) -> bool:
    """Whether a person asked to keep ``title`` open (the ⋯ menu's Keep open)."""
    return _entry("keep_open", title, created) is not None


def set_keep_open(title: str, keep: bool, created: Optional[float] = None) -> bool:
    """Turn ``title``'s Keep open on or off → the new state. Never raises
    past a store write failure (which propagates: the route reports it)."""
    with _Txn() as data:
        if keep:
            data["keep_open"][title] = {"created_at": created}
        else:
            data["keep_open"].pop(title, None)
    with _LOCK:
        _VERDICTS.pop(title, None)
    return bool(keep)


def orphan_of(title: str, created: Optional[float] = None) -> str:
    """The parent ``title`` was spawned by when that parent is gone, else ""."""
    rec = _entry("orphans", title, created)
    return str(rec.get("parent") or "") if rec else ""


def note_parent_gone(instances: Mapping, parent: str, parent_inst=None) -> List[str]:
    """Record that ``parent`` is leaving while its agent-spawned children stay
    — called by every removal path BEFORE the children's links move
    (``session_end.reparent_children``, ``session_removal._orphan_children``)
    and before the parent's mailbox is dropped, so each child's report to it
    is kept. ``parent_inst`` (when the caller still has it) gives the
    parent's branch and folder, the ref its merge is judged against later.
    → the titles recorded. Never raises."""
    try:
        from backend.web.core import mailbox as _mailbox
        from backend.web.core import thread as _thread

        kids = [
            (t, i)
            for t, i in list(instances.items())
            if t != parent
            and (getattr(i, "Parent", "") or "") == parent
            and bool(getattr(i, "Spawned", False))
        ]
        if not kids or not parent:
            return []
        branch = str(getattr(parent_inst, "Branch", "") or "") if parent_inst else ""
        folder = ""
        if parent_inst is not None:
            try:
                folder = parent_inst.GetWorktreePath() or parent_inst.Path or ""
            except Exception:  # noqa: BLE001
                folder = getattr(parent_inst, "Path", "") or ""
        now = time.time()
        recorded = []
        with _Txn() as data:
            for title, inst in kids:
                created = _created(inst)
                try:
                    rep = _thread.report_json(
                        _mailbox.last_result(parent, title, since=created)
                    )
                except Exception:  # noqa: BLE001
                    rep = None
                data["orphans"][title] = {
                    "parent": parent,
                    "parent_branch": branch,
                    "parent_folder": str(folder or ""),
                    "created_at": created,
                    "report": (
                        {"status": rep["status"], "ts": rep["ts"]} if rep else None
                    ),
                    "at": now,
                }
                recorded.append(title)
        return recorded
    except Exception as err:  # noqa: BLE001 — bookkeeping, never the removal
        _info("auto-close: noting %s's children failed (%s)", parent, err)
        return []


def forget(title: str) -> None:
    """Drop ``title``'s records (it left the registry). Never raises."""
    try:
        with _Txn() as data:
            data["keep_open"].pop(title, None)
            data["orphans"].pop(title, None)
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        _VERDICTS.pop(title, None)


def rename(old: str, new: str) -> None:
    """Move ``old``'s records to ``new`` (core.rename_stores)."""
    with _Txn() as data:
        for part in ("keep_open", "orphans"):
            if old in data[part]:
                data[part][new] = data[part].pop(old)
    with _LOCK:
        if old in _VERDICTS:
            _VERDICTS[new] = _VERDICTS.pop(old)


def _prune(live: Iterable[str], tombstoned: Mapping[str, float]) -> None:
    """Drop records of titles tombstoned on disk and not live anywhere — by
    disk state, never by "not in my ENGINE" (two servers, one config dir)."""
    live = set(live)
    try:
        with _Txn() as data:
            for part in ("keep_open", "orphans"):
                for t in [t for t in data[part] if t in tombstoned and t not in live]:
                    data[part].pop(t, None)
    except Exception:  # noqa: BLE001
        pass


def row_field(inst) -> Optional[dict]:
    """The row's ``auto_close`` field, or None for a session the sweep never
    closes (not agent-spawned). ``{"keep_open": bool, "orphan_of": str,
    "closes_at": epoch | None, "why": str}`` — ``orphan_of`` names the gone
    parent of a worker that now has none ("parent closed — review or
    close"), ``closes_at`` when a finished worker will close (this server's
    sweep's last pass), ``why`` that pass's reason it stays. Dict reads only."""
    try:
        if not getattr(inst, "Spawned", False):
            return None  # orphan records are only ever kept for spawned ones
        title = inst.Title
        created = _created(inst)
        orphan = orphan_of(title, created)
        srv = _server()
        parent = getattr(inst, "Parent", "") or ""
        if parent and parent != title and parent in srv.ENGINE.instances:
            orphan = ""  # moved under a live session again: not adrift
        with _LOCK:
            v = dict(_VERDICTS.get(title) or {})
        return {
            "keep_open": keep_open(title, created),
            "orphan_of": orphan,
            "closes_at": v.get("closes_at") if v.get("verdict") == WAIT else None,
            "why": str(v.get("why") or ""),
        }
    except Exception:  # noqa: BLE001 — enrichment only
        return None


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #
def _git():
    from backend.mcp import gitlocal

    return gitlocal


def _base_sha(inst) -> str:
    try:
        return str(inst.GetGitWorktree().GetBaseCommitSHA() or "")
    except Exception:  # noqa: BLE001
        return ""


def _parent_ref(srv, parent: str, orphan: Optional[dict], wt: str) -> Optional[str]:
    """The commit a worker's merge is judged against: its live parent's HEAD
    (what ``kill_session`` delete compares with), else — its parent gone —
    that parent's kept folder's HEAD, else its branch tip in the worker's
    repository. None when none resolves."""
    g = _git()
    pinst = srv.ENGINE.instances.get(parent) if parent else None
    if pinst is not None:
        try:
            folder = pinst.GetWorktreePath() if pinst.Started() else pinst.Path
        except Exception:  # noqa: BLE001
            folder = getattr(pinst, "Path", "") or ""
        sha = g.head_sha(folder) if folder and os.path.isdir(folder) else None
        if sha:
            return sha
    if orphan:
        folder = str(orphan.get("parent_folder") or "")
        sha = g.head_sha(folder) if folder and os.path.isdir(folder) else None
        if sha:
            return sha
        branch = str(orphan.get("parent_branch") or "")
        if branch and not branch.startswith("-"):
            out = g.git(wt, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
            if out:
                return out
    return None


def work_accounted(
    srv, inst, title: str, row: Mapping, parent: str, orphan: Optional[dict]
) -> Tuple[Optional[bool], str]:
    """Whether the worker's work is accounted for → ``(True, how)``,
    ``(False, why not)`` or ``(None, why unknown)`` (kept, like False).
    Shells out to git: called only for a worker that passed every cheap rule."""
    g = _git()
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return None, R_WORKSPACE
    dirty = g.is_dirty(wt)
    if dirty is None:
        return None, R_UNKNOWN_GIT
    if dirty:
        return False, R_DIRTY
    base = _base_sha(inst)
    if base and g.commits_between(wt, base, "HEAD", limit=1) == []:
        return True, W_NO_COMMITS
    ref = _parent_ref(srv, parent, orphan, wt)
    if ref and g.commits_between(wt, ref, "HEAD", limit=1) == []:
        return True, W_MERGED
    if row.get("pr_merged"):
        return True, W_PR_MERGED
    branch = str(row.get("branch") or getattr(inst, "Branch", "") or "")
    try:
        info = srv._pr_info(wt, branch) if branch else None
    except Exception:  # noqa: BLE001
        info = None
    state = str((info or {}).get("state") or "").upper()
    if state == "MERGED" and (
        not info.get("head") or info.get("head") == g.head_sha(wt)
    ):
        return True, W_PR_MERGED
    if state == "CLOSED":
        return True, W_PR_CLOSED
    if base or ref:
        return False, R_UNMERGED
    return None, R_UNKNOWN_GIT


def _last_poke(srv, title: str) -> float:
    """When anything last reached the session: a delivered message, a queued
    prompt sent, a person typing."""
    clocks = [0.0]
    for src in ("_QUEUE_STATE", "_MAIL_STATE"):
        try:
            clocks.append(
                float((getattr(srv, src).get(title) or {}).get("sent_at") or 0.0)
            )
        except Exception:  # noqa: BLE001
            pass
    try:
        clocks.append(float(srv._HUMAN_INPUT_AT.get(title, 0.0) or 0.0))
    except Exception:  # noqa: BLE001
        pass
    return max(clocks)


def _has_live_children(instances: Mapping, title: str) -> bool:
    return any(
        t != title and (getattr(i, "Parent", "") or "") == title
        for t, i in instances.items()
    )


def _route_running(title: str, parent: str, created: Optional[float] = None) -> bool:
    """Whether a route still needs ``title``: its OWN route is in the air, or
    its parent's route has a stop running (or parked on you) that still
    waits on it — one that names it among its workers (an agent stop's
    workers, a split's, a review stop's reviewer), or the stop it was
    spawned during. A worker of a stop the route has already passed is no
    longer the route's: it closes by the ordinary rules. (Holding every
    worker for the whole route kept finished reviewers open for hours while
    the route sat at a later stop, e.g. waiting on PR comments.)"""
    from backend.web.core import flights as _flights

    if _flights.for_session(title) is not None:
        return True
    if not parent:
        return False
    pf = _flights.for_session(parent)
    if pf is None:
        return False
    return _route_waits_on(pf, title, created)


def _route_waits_on(flight: Mapping, title: str, created: Optional[float]) -> bool:
    from backend.web.core import flights as _flights

    try:
        states = flight.get("step_states") or {}
        active = [
            s
            for s in flight.get("steps") or []
            if (states.get(s.get("id")) or {}).get("state") in _flights.ACTIVE
        ]
        for s in active:
            kids = ((states.get(s["id"]) or {}).get("detail") or {}).get(
                "children"
            ) or []
            for k in kids:
                name = k.get("title") if isinstance(k, Mapping) else k
                if str(name or "") == title:
                    return True
        if not active:
            return False
        spawned_in = _flights.branch_step(dict(flight), title, created)
        return any(s["id"] == spawned_in for s in active)
    except Exception:  # noqa: BLE001 — can't tell: the route keeps it
        return True


def judge(
    srv,
    inst,
    title: str,
    row: Optional[Mapping],
    now: float,
    grace_s: float,
    *,
    disk=None,
    store: Optional[Mapping] = None,
) -> Tuple[str, str, Optional[float]]:
    """``(verdict, reason, closes_at)`` for one session: :data:`CLOSE`,
    :data:`WAIT` (finished and accounted for, ``closes_at`` when the window
    ends), :data:`KEEP` or :data:`ORPHAN` (parent gone, work unaccounted:
    kept and flagged). ``row`` is its published ``/api/instances`` row,
    ``disk`` an ``engine.disk_view()``, ``store`` a loaded store. Cheap
    rules first; git and the probes that shell out last."""
    row = row or {}
    store = store if store is not None else _load()
    created = _created(inst)
    instances = srv.ENGINE.instances

    # --- 1. a sub-session ------------------------------------------------- #
    orphan = store.get("orphans", {}).get(title)
    if orphan is not None and not _same(_num(orphan.get("created_at")), created):
        orphan = None
    if not getattr(inst, "Spawned", False):
        return KEEP, R_NOT_SUB, None
    stored = getattr(inst, "Parent", "") or ""
    parent = stored if stored and stored != title and stored in instances else ""
    if not parent and orphan is None and stored and stored != title:
        # Not in THIS server's ENGINE is not gone: only a tombstone on disk
        # says the parent left (the other server may have just made it).
        if disk is not None and stored in getattr(disk, "tombstoned", {}):
            orphan = {"parent": stored, "report": None}
        else:
            return KEEP, R_PARENT_ELSEWHERE, None
    if not parent and orphan is None:
        return KEEP, R_NOT_SUB, None
    from backend.web.core import pending as _pending

    if _pending.session_kind(title, str(getattr(inst, "Branch", "") or "")):
        return KEEP, R_INTAKE, None
    if getattr(inst, "InPlace", False):
        return KEEP, R_IN_PLACE, None
    if getattr(inst, "PeerShare", "") or "":
        return KEEP, R_PEER, None
    if row.get("hidden_by"):
        return KEEP, R_HIDDEN, None
    kept = store.get("keep_open", {}).get(title)
    if kept is not None and _same(_num(kept.get("created_at")), created):
        return KEEP, R_KEEP_OPEN, None
    adrift = orphan is not None and not parent  # flagged unless it closes

    def _stay(why: str) -> Tuple[str, str, Optional[float]]:
        return (ORPHAN if adrift else KEEP), why, None

    try:
        from backend.web.core import team_runs as _team_runs

        if _team_runs.owner_of_title(title) is not None:
            return KEEP, R_TEAM, None
    except Exception:  # noqa: BLE001
        return KEEP, R_TEAM, None
    try:
        if _route_running(title, parent, created):
            return KEEP, R_ROUTE, None
    except Exception:  # noqa: BLE001
        return KEEP, R_ROUTE, None
    if disk is not None and title not in getattr(disk, "live", frozenset()):
        return KEEP, R_GONE, None
    try:
        if not inst.Started() or inst.Status in (session.Paused, session.Loading):
            return _stay(R_STATUS)
    except Exception:  # noqa: BLE001
        return _stay(R_STATUS)
    if row.get("workspace_missing"):
        return _stay(R_WORKSPACE)

    # --- 2. finished ------------------------------------------------------ #
    rep = row.get("last_report") if parent else None
    if not isinstance(rep, Mapping) and orphan is not None:
        rep = orphan.get("report")
    if not isinstance(rep, Mapping):
        return _stay(R_NO_REPORT)
    status = str(rep.get("status") or "")
    if status == "blocked":
        return _stay(R_BLOCKED)
    if status not in ("done", "failed"):
        return _stay(R_NO_REPORT)
    rep_ts = float(rep.get("ts") or 0.0)
    if _last_poke(srv, title) > rep_ts:
        return _stay(R_NEW_WORK)
    asleep = bool(row.get("sleeping"))
    activity = str(row.get("activity") or "")
    if not asleep and activity not in ("idle", "offline"):
        return _stay(R_BUSY)
    since = float(row.get("activity_since") or 0.0)
    if not asleep and since <= 0:
        return _stay(R_BUSY)  # not read yet: never "idle for ages"
    if row.get("asking"):
        return _stay(R_ASKING)
    if row.get("background"):
        return _stay(R_BACKGROUND)
    try:
        from backend.web.core import prompt_queue as _prompt_queue

        if _prompt_queue.get_state(title)["items"]:
            return _stay(R_QUEUE)
    except Exception:  # noqa: BLE001
        return _stay(R_QUEUE)
    try:
        from backend.web.core import mailbox as _mailbox

        if _mailbox.next_pending(title) is not None:
            return _stay(R_MAIL)
    except Exception:  # noqa: BLE001
        return _stay(R_MAIL)
    try:
        if srv._autopilot_running(title):
            return _stay(R_AUTOPILOT)
    except Exception:  # noqa: BLE001
        return _stay(R_AUTOPILOT)
    if _has_live_children(instances, title):
        return _stay(R_WORKERS)

    # --- 3. accounted for (git) ------------------------------------------- #
    ok, how = work_accounted(srv, inst, title, row, parent, orphan)
    if not ok:
        return _stay(how)

    # --- 4. the grace window, and nobody looking -------------------------- #
    finished_at = max(rep_ts, since if not asleep else 0.0)
    if now - finished_at < grace_s:
        return WAIT, R_GRACE, finished_at + grace_s
    human = 0.0
    try:
        human = float(srv._HUMAN_INPUT_AT.get(title, 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        pass
    if now - human < grace_s:
        return WAIT, R_TYPED, human + grace_s
    from backend.web.core import ownership as _ownership
    from backend.web.core import sleep as _sleep

    name = tmux.to_mindflock_tmux_name(title)
    if _ownership.terminals(title) > 0:
        return WAIT, R_VIEWED, None
    try:
        if _sleep._outside_clients(name) > 0:
            return WAIT, R_VIEWED, None
    except Exception:  # noqa: BLE001
        pass
    try:
        if srv._tmux_client_input_recent(title, grace_s):
            return WAIT, R_TYPED, None
    except Exception:  # noqa: BLE001
        pass
    return CLOSE, how, None


def _live_check(srv, inst, title: str, row: Mapping) -> Optional[str]:
    """Re-read live what the row could have gone stale on, right before a
    close: a turn that started since, a background job, a dialog. None = go."""
    if row.get("sleeping"):
        return None
    try:
        name = tmux.to_mindflock_tmux_name(title)
        if srv._live_session_name(name) is None:
            return None  # no agent running: nothing to cut off
        if srv._agent_activity(inst, title) not in ("idle", "offline"):
            return R_BUSY
        if srv._background_jobs(inst, title):
            return R_BACKGROUND
        if srv._dialog_on_screen(inst, name):
            return R_DIALOG
    except Exception:  # noqa: BLE001 — unknown is not "go"
        return R_BUSY
    return None


# --------------------------------------------------------------------------- #
# closing
# --------------------------------------------------------------------------- #
def _close(srv, title: str, parent: str, how: str) -> Optional[str]:
    """Close ``title`` the way ✕ does (worktree + branch kept, Recently
    closed) → its Recently closed entry id ("" when none was found), or None
    when the close failed."""
    from backend.web.core import session_end as _session_end

    try:
        _session_end.end_session(srv.ENGINE, title, wipe=False)
    except Exception as err:  # noqa: BLE001
        _info("auto-close %s: close failed (%s)", title, err)
        return None
    _info(
        "auto-close: closed finished worker %s of %s (%s; reopen it from "
        "Recently closed)",
        title,
        parent or "a closed parent",
        how,
    )
    try:
        for e in srv._load_recently_closed():
            if isinstance(e, dict) and e.get("title") == title:
                return str(e.get("id") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def notice_text(parent: str, titles: List[str]) -> str:
    """``Closed 2 finished workers of api-lead: a, b.``"""
    names = ", ".join(titles[:5]) + (
        " and %d more" % (len(titles) - 5) if len(titles) > 5 else ""
    )
    count = (
        "1 finished worker" if len(titles) == 1 else "%d finished workers" % len(titles)
    )
    of = " of %s" % parent if parent else ""
    return "Closed %s%s: %s." % (count, of, names)


def announce(parent: str, closed: List[Tuple[str, str]]) -> Optional[dict]:
    """ONE :data:`EVENT` for the workers of ``parent`` a pass closed
    (``closed`` = ``[(title, entry id)]``). Never raises."""
    try:
        from backend.web.core import events as _events

        titles = [t for t, _ in closed]
        return _events.BUS.emit(
            EVENT,
            session=parent,
            new=str(len(titles)),
            data={
                "parent": parent,
                "count": len(titles),
                "titles": titles,
                "entries": [{"title": t, "id": i} for t, i in closed],
                "detail": notice_text(parent, titles),
            },
        )
    except Exception:  # noqa: BLE001 — a notice must never break the sweep
        return None


def tick(srv=None, now: Optional[float] = None) -> List[str]:
    """One pass → the titles closed. Blocking (git, tmux) — off the loop.
    Never raises."""
    if srv is None:
        srv = _server()
    if now is None:
        now = time.time()
    closed: List[str] = []
    try:
        if not enabled():
            with _LOCK:
                _VERDICTS.clear()
            return closed
        from backend.web.core import sleep as _sleep

        # One sweeper per config dir (two servers would race to close the
        # same worker and announce it twice).
        if not _sleep._lease_note(LEASE, _sleep.hold_lease(LEASE, LEASE_TTL_S)):
            with _LOCK:
                _VERDICTS.clear()
            return closed
        from backend.web.core import engine as _engine
        from backend.web.core import events as _events

        grace_s = grace_seconds()
        disk = _engine.disk_view()
        store = _load()
        rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
        by_title = {r.get("title"): r for r in rows if not r.get("device")}
        with srv.ENGINE.lock:
            items = list(srv.ENGINE.instances.items())
        verdicts: Dict[str, dict] = {}
        todo: List[Tuple[str, object, str, str]] = []
        for title, inst in items:
            row = by_title.get(title)
            if row is None:
                continue
            try:
                verdict, why, at = judge(
                    srv, inst, title, row, now, grace_s, disk=disk, store=store
                )
            except Exception:  # noqa: BLE001 — one bad session can't stop the pass
                continue
            verdicts[title] = {"verdict": verdict, "why": why, "closes_at": at}
            if verdict == CLOSE:
                parent = getattr(inst, "Parent", "") or ""
                if parent not in srv.ENGINE.instances:
                    parent = str(
                        (store["orphans"].get(title) or {}).get("parent") or parent
                    )
                todo.append((title, inst, parent, why))
        with _LOCK:
            _VERDICTS.clear()
            _VERDICTS.update(verdicts)
        by_parent: Dict[str, List[Tuple[str, str]]] = {}
        for title, inst, parent, how in todo[:MAX_PER_PASS]:
            row = by_title.get(title) or {}
            stale = _live_check(srv, inst, title, row)
            if stale:
                with _LOCK:
                    _VERDICTS[title] = {
                        "verdict": KEEP,
                        "why": stale,
                        "closes_at": None,
                    }
                continue
            entry = _close(srv, title, parent, how)
            if entry is None:
                continue
            closed.append(title)
            by_parent.setdefault(parent, []).append((title, entry))
        for parent, done in by_parent.items():
            announce(parent, done)
        _prune(disk.live | set(srv.ENGINE.instances), disk.tombstoned)
    except Exception as err:  # noqa: BLE001 — the sweep must never die
        _info("auto-close: pass failed (%s)", err)
    return closed


async def run_loop(srv=None) -> None:
    """Drive :func:`tick` forever (started by the server lifespan). Waits out
    the boot quiet window first: the rows it reads are not settled before,
    and a restart must not read as a burst of closes."""
    if srv is None:
        srv = _server()
    try:
        left = srv._BOOT_QUIET_SECONDS - (time.monotonic() - srv._BOOT_MONO)
    except Exception:  # noqa: BLE001
        left = 0.0
    if left > 0:
        await asyncio.sleep(left + 1.0)
    from backend.web.core import sleep as _sleep

    try:
        while True:
            try:
                runner = getattr(srv, "_in_bg_pool", None)
                await (runner(tick, srv) if runner else asyncio.to_thread(tick, srv))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(TICK_SECONDS)
    finally:
        # Shutting down: hand the sweep to the other server now.
        _sleep.release_lease(LEASE)


# --------------------------------------------------------------------------- #
# undo
# --------------------------------------------------------------------------- #
async def undo(entry_ids: Iterable[str], srv=None) -> dict:
    """The toast's Undo: reopen each Recently closed entry and mark the
    reopened session Keep open, so the next pass leaves it be →
    ``{"reopened": [titles], "failed": {id: error}}``."""
    if srv is None:
        srv = _server()
    import json as _json

    reopened: List[str] = []
    failed: Dict[str, str] = {}
    for eid in entry_ids:
        eid = str(eid or "")
        if not eid:
            continue
        try:
            resp = await srv._reopen_closed_entry(eid)
            body = _json.loads(bytes(resp.body).decode("utf-8") or "{}")
        except Exception as err:  # noqa: BLE001
            failed[eid] = str(err)
            continue
        if resp.status_code >= 300:
            failed[eid] = str(body.get("error") or resp.status_code)
            continue
        title = str(body.get("title") or "")
        if not title:
            continue
        inst = srv.ENGINE.instances.get(title)
        try:
            set_keep_open(title, True, _created(inst) if inst is not None else None)
        except Exception as err:  # noqa: BLE001
            _info("auto-close undo %s: keep-open not saved (%s)", title, err)
        reopened.append(title)
    return {"reopened": reopened, "failed": failed}
