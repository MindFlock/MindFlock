"""The session engine: config/state/storage + the in-memory instance set.

``get_engine()`` returns a process-wide singleton (the in-memory ``instances``
dict is the live source of truth, shared with the 4s adopt loop and background
spawn tasks, so it must NOT be rebuilt per request). The reload/adopt loop that
pulls in instances written by other processes (the TUI, a co-running pipeline)
lives here too.

Multi-server convergence (L1): several servers can share one
``~/.mindflock/state.json``. Two rules keep them from resurrecting each
other's dead sessions:

* **Deletion tombstones** — deleting a session records ``{title: deleted_ts}``
  in the state file's ``tombstones`` map (pruned after 24h). Merge-on-save and
  the adopt/reload path drop any instance whose title carries a tombstone
  NEWER than the instance's own last-updated stamp, so a deletion propagates
  to every co-running server instead of ping-ponging back from their merges.
  Re-creating a session with the same title clears its tombstone (the new
  instance's timestamps postdate it).
* **Liveness validation** — adopt and merge skip/drop instances whose worktree
  directory is gone AND whose tmux session no longer exists, with a ~120s
  grace window from creation/last-known activity so in-flight provisioning is
  never culled. Paused sessions are exempt (their worktree is removed by
  design; the branch lives on).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from typing import Dict, Optional

from backend import config, log
from backend import session

# Tombstones older than this are pruned on every save (a co-running server
# only needs to see one within its merge/adopt cycle, i.e. seconds).
TOMBSTONE_TTL_SECONDS = 24 * 3600.0
# How long a freshly created / recently active instance is exempt from the
# dead-instance cull (worktree missing + tmux gone) — provisioning can take
# a while before the worktree dir exists.
ADOPT_GRACE_SECONDS = 120.0


def _epoch(dt) -> float:
    """A datetime's epoch seconds, or 0.0 when absent/unparseable."""
    try:
        return float(dt.timestamp()) if dt is not None else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _data_last_seen(data) -> float:
    """Last-known activity for a serialized instance (created/updated max)."""
    return max(
        _epoch(getattr(data, "created_at", None)),
        _epoch(getattr(data, "updated_at", None)),
    )


def _inst_last_seen(inst) -> float:
    """Last-known activity for an in-memory instance (created/updated max)."""
    return max(
        _epoch(getattr(inst, "CreatedAt", None)),
        _epoch(getattr(inst, "UpdatedAt", None)),
    )


def _prune_tombstones(tombs: dict, now: float) -> Dict[str, float]:
    """Drop malformed entries and any tombstone older than the 24h TTL."""
    out: Dict[str, float] = {}
    for title, ts in (tombs or {}).items():
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if isinstance(title, str) and title and now - ts < TOMBSTONE_TTL_SECONDS:
            out[title] = ts
    return out


def _merge_tombstones(a: dict, b: dict) -> Dict[str, float]:
    """Union of two tombstone maps, keeping the newest timestamp per title."""
    out = dict(a or {})
    for title, ts in (b or {}).items():
        if title not in out or ts > out[title]:
            out[title] = ts
    return out


def _is_tombstoned(title: str, last_seen: float, tombs: dict) -> bool:
    """True when ``title`` carries a tombstone NEWER than the instance's own
    last-updated stamp (a same-name session re-created after the deletion has
    newer timestamps and survives)."""
    ts = tombs.get(title)
    return ts is not None and float(ts) > last_seen


def _tmux_session_exists(title: str) -> bool:
    """Best-effort: does the mindflock tmux session for ``title`` exist?"""
    try:
        from backend.session import tmux as _tmux

        name = _tmux.to_mindflock_tmux_name(title)
        return (
            subprocess.run(
                ["tmux", "has-session", "-t", "=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001 — no tmux binary etc. = "doesn't exist"
        return False


def _data_looks_dead(data, now: float) -> bool:
    """True when a serialized instance's workspace is gone AND its tmux
    session no longer exists (outside the provisioning grace window).

    Paused sessions are never dead this way — their worktree is removed by
    design and the branch preserved. An instance with no recorded worktree
    path can't be judged and is kept.
    """
    try:
        if int(data.status) == int(session.Paused):
            return False
        wt = (getattr(data.worktree, "worktree_path", "") or "").strip()
        if not wt:
            return False  # nothing recorded to validate against
        if os.path.isdir(wt):
            return False
        if now - _data_last_seen(data) < ADOPT_GRACE_SECONDS:
            return False  # in-flight provisioning / just-created
        return not _tmux_session_exists(data.title)
    except Exception:  # noqa: BLE001 — when in doubt, keep it
        return False


def _load_disk_tombstones(state=None, now: Optional[float] = None) -> Dict[str, float]:
    """The (pruned) tombstone map currently on disk. Never raises."""
    if now is None:
        now = time.time()
    try:
        st = state if state is not None else config.LoadState()
        return _prune_tombstones(getattr(st, "tombstones", {}) or {}, now)
    except Exception:  # noqa: BLE001
        return {}


class Engine:
    """Holds the storage + the in-memory set of instances (keyed by title)."""

    def __init__(self) -> None:
        self.cfg = config.LoadConfig()
        self.state = config.LoadState()
        self.storage = session.NewStorage(self.state)
        self.instances: Dict[str, session.Instance] = {}
        # Guards mutations / check-then-act sequences on ``instances``: the
        # 4s adopt loop mutates it from a worker thread (asyncio.to_thread)
        # while API handlers mutate it on the event loop. Reentrant so nested
        # helpers can take it again. Never held across slow operations
        # (subprocess calls, Start/Kill) — only around the dict access itself.
        self.lock = threading.RLock()
        # Seed from persisted state so previously-created sessions show up —
        # skipping tombstoned entries (deleted by a co-running server) and
        # instances whose workspace + tmux session are both gone (L1). One bad
        # entry no longer aborts the whole seed.
        now = time.time()
        tombs = _prune_tombstones(getattr(self.state, "tombstones", {}) or {}, now)
        try:
            from backend.session.instance import FromInstanceData
            from backend.session.storage import InstanceData

            raw = self.state.GetInstances()
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8")
            for x in (json.loads(raw) if raw else []) or []:
                data = InstanceData.from_dict(x)
                if not data.title:
                    continue
                if _is_tombstoned(data.title, _data_last_seen(data), tombs):
                    continue
                if _data_looks_dead(data, now):
                    continue
                try:
                    # attach=False: no server-side PTY. The web layer talks to
                    # sessions via `tmux send-keys` and the terminal websocket
                    # attaches on its own; a server-held attach would shrink
                    # the window to the smallest client and duplicate
                    # keystroke routing (same reason _sync_external_instances
                    # passes attach=False).
                    inst = FromInstanceData(data, attach=False)
                except Exception as err:  # noqa: BLE001
                    if log.ErrorLog is not None:
                        log.ErrorLog.Printf(
                            "failed to load instance %s: %v", data.title, err
                        )
                    continue
                self.instances[inst.Title] = inst
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to load instances: %v", err)

    def default_program(self) -> str:
        try:
            return self.cfg.GetProgram()
        except Exception:  # noqa: BLE001
            return "claude"

    def save(self, exclude_titles=()) -> None:
        """Persist instances, merging in entries written by other processes.

        A plain "write my in-memory set" would clobber instances that another
        process (e.g. MindFlock persisting sessions via the engine) wrote to
        state.json after this server started. So we re-read the file fresh and
        keep any on-disk instance whose title this server neither owns nor is
        explicitly removing (``exclude_titles``). Reads use the pure
        ``InstanceData.from_dict`` (no tmux/worktree reconstruction side
        effects).

        L1 convergence rules applied here:
          * every ``exclude_titles`` entry gets a deletion tombstone (now);
          * in-memory instances tombstoned by another server are dropped
            (never written back — deletions converge instead of resurrecting);
          * surviving owned instances newer than their tombstone clear it
            (same-name re-create after delete);
          * merged-in on-disk instances are dropped when tombstoned or when
            their workspace AND tmux session are both gone (post-grace).
        """
        from backend.session.storage import InstanceData, _marshal_instances

        try:
            now = time.time()
            exclude = set(exclude_titles)

            # Hold the cross-process state lock for the whole read-merge-write
            # so a co-running server can't write between our read and our save
            # (lost update). Reentrant: the inner LoadState/SaveState nest.
            with config.state_file_lock():
                # Fresh disk state: other servers' instances AND their
                # tombstones.
                fresh = None
                try:
                    fresh = config.LoadState()
                except Exception as err:  # noqa: BLE001
                    if log.ErrorLog is not None:
                        log.ErrorLog.Printf("merge-on-save state read failed: %v", err)
                tombs = _merge_tombstones(
                    _prune_tombstones(getattr(self.state, "tombstones", {}) or {}, now),
                    _load_disk_tombstones(fresh, now),
                )
                # Record the deletions this save performs.
                for title in exclude:
                    tombs[title] = now

                # Deletions from other servers apply to OUR in-memory set too —
                # otherwise this server writes the dead session right back.
                with self.lock:
                    for title, inst in list(self.instances.items()):
                        if title in exclude:
                            continue
                        if _is_tombstoned(title, _inst_last_seen(inst), tombs):
                            self.instances.pop(title, None)

                    mine = [i for i in self.instances.values() if i.Started()]
                mine_titles = {i.Title for i in mine}
                # A surviving owned session re-created after a deletion clears
                # the old tombstone, so deleting it AGAIN later works.
                for i in mine:
                    ts = tombs.get(i.Title)
                    if ts is not None and _inst_last_seen(i) >= ts:
                        tombs.pop(i.Title, None)

                datas = [i.ToInstanceData() for i in mine]
                try:
                    raw = fresh.GetInstances() if fresh is not None else b""
                    if isinstance(raw, (bytes, bytearray)):
                        raw = bytes(raw).decode("utf-8")
                    for x in (json.loads(raw) if raw else []) or []:
                        t = x.get("title")
                        if not t or t in mine_titles or t in exclude:
                            continue
                        data = InstanceData.from_dict(x)
                        if _is_tombstoned(t, _data_last_seen(data), tombs):
                            continue
                        if _data_looks_dead(data, now):
                            continue
                        datas.append(data)
                except Exception as merge_err:  # noqa: BLE001
                    if log.ErrorLog is not None:
                        log.ErrorLog.Printf("merge-on-save read failed: %v", merge_err)
                self.state.tombstones = tombs
                self.state.SaveInstances(_marshal_instances(datas))
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to save instances: %v", err)


_ENGINE: Optional[Engine] = None


def get_engine() -> Engine:
    """The process-wide engine singleton (built on first use)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = Engine()
    return _ENGINE


# (mtime_ns, size) of state.json at the last sync, plus the tombstones parsed
# from that same read — the fast path below still needs them.
_LAST_STATE_SIG: list = [None, None]


def _sync_external_instances() -> int:
    """Pull instances written to state.json by other processes into the engine.

    Reconstructs each on-disk instance not already in the engine without
    attaching its tmux session (no server-side PTY). Returns the number newly
    added. Kills go through the API — but deletions performed by a co-running
    server (tombstones, L1) ARE applied here: a tombstoned in-memory instance
    is dropped, and tombstoned / dead on-disk entries are never adopted.

    When state.json is unchanged since the last sync (the common case on the
    4s loop), the read+parse+adopt work is skipped; only the in-memory
    tombstone drop still runs (an instance can be added to THIS engine's
    memory between syncs without touching the file).
    """
    from backend.session.instance import FromInstanceData
    from backend.session.storage import InstanceData

    engine = get_engine()
    now = time.time()
    sig = None
    try:
        st = os.stat(os.path.join(config.GetConfigDir(), config.StateFileName))
        sig = (st.st_mtime_ns, st.st_size)
    except Exception:  # noqa: BLE001 — stat trouble: fall through to a full sync
        pass
    if sig is not None and sig == _LAST_STATE_SIG[0] and _LAST_STATE_SIG[1] is not None:
        tombs = _LAST_STATE_SIG[1]
        with engine.lock:
            for title, inst in list(engine.instances.items()):
                if _is_tombstoned(title, _inst_last_seen(inst), tombs):
                    engine.instances.pop(title, None)
        return 0
    state = config.LoadState()
    tombs = _load_disk_tombstones(state, now)
    _LAST_STATE_SIG[0] = sig
    _LAST_STATE_SIG[1] = tombs
    # Apply other servers' deletions to the in-memory set (convergence).
    with engine.lock:
        for title, inst in list(engine.instances.items()):
            if _is_tombstoned(title, _inst_last_seen(inst), tombs):
                engine.instances.pop(title, None)
    raw = state.GetInstances()
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    try:
        items = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return 0
    added = 0
    for x in items or []:
        title = x.get("title")
        if not title or title in engine.instances:
            continue
        try:
            data = InstanceData.from_dict(x)
            if _is_tombstoned(title, _data_last_seen(data), tombs):
                continue  # deleted by another server — never re-adopt
            if _data_looks_dead(data, now):
                continue  # workspace + tmux both gone (post-grace)
            inst = FromInstanceData(data, attach=False)
            # Re-check under the lock: an API handler may have created this
            # title while we reconstructed — never clobber a live Instance.
            with engine.lock:
                if title in engine.instances:
                    continue
                engine.instances[title] = inst
            added += 1
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("reload: skipping %s: %v", title, err)
    return added


async def _reload_loop() -> None:
    while True:
        await asyncio.sleep(4)
        try:
            await asyncio.to_thread(_sync_external_instances)
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("reload loop error: %v", err)
