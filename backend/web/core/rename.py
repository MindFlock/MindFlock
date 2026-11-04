"""One name per session: a rename IS the title.

A session used to have two names: the title every store, route, tmux session
and MCP identity is keyed by, and a display alias the SPA kept in localStorage
and mirrored to :mod:`core.aliases`. The owner: "It is like there is a secret
hidden session name. I want the display name and the internal name to be one
and the same." So ``POST /api/instances/{title}/rename`` renames the session
itself, everywhere the server keys by title, atomically under the registration
lock (``ENGINE.lock``):

* the engine: ``ENGINE.instances`` (and state.json, via a save that tombstones
  the old title so a co-running server's merge cannot bring it back), the
  instance's tmux handle, its worktree record, its children's ``Parent``;
* the tmux sessions (the agent's and its ``_sh`` shell) — ``tmux
  rename-session``, so a RUNNING agent keeps running, and its Claude hooks
  (which resolve the tmux name at fire time) write under the new name at once;
* every file named after the tmux name (activity / thread / voice markers, the
  exit marker the running wrapper will write, the MCP run file);
* every title-keyed store in :data:`REGISTRY` — each entry knows its own data.

The worktree folder and the branch are NOT renamed: they are named by branch.

A running agent's MCP server was launched with ``MINDFLOCK_SESSION_TITLE`` set
to the OLD title. :data:`renamed_from` (old → new, persisted) carries it across:
rows list the names a session used to have (``renamed_from``) so the MCP's
identity resolves to the renamed session, and a request for
``/api/instances/<old>/…`` whose old title is no longer live is routed to the
new one (:func:`resolve`). An entry goes when its session goes, or once the
session's agent has relaunched (its pane pid moved on) — a relaunch bakes the
new name in.

Migration (:func:`migrate_aliases`): the aliases recorded before this existed
become real titles — at every server start, skipping sessions whose agent is
running (``<config dir>/rename_migration.json`` records the last run), and again for every alias map a browser seeds (``POST /api/aliases``). An alias
that is not a free, valid name stays an alias; :func:`core.aliases.display_name`
keeps showing it.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from backend import config, log
from backend.session import tmux
from backend.web.core import title_store

#: Longest name a rename accepts. Titles are URL path segments, tmux names
#: and run-file names; the MCP's ``title`` arguments allow 120.
MAX_TITLE_LEN = 120

_SHELL_SUFFIX = "_sh"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def title_error(name: str) -> str:
    """Why ``name`` cannot be a session title ("" when it can).

    Titles name a URL path segment (``/api/instances/{title}``), a tmux
    session and the files keyed by it, so: not empty, at most
    :data:`MAX_TITLE_LEN` characters, no ``/`` or ``\\`` (a path segment),
    no ``::`` (the ``<device>::<title>`` separator remote rows use), no
    control characters, and not only dots (a path that means "here")."""
    name = str(name or "")
    if not name.strip():
        return "a session name cannot be empty"
    if name != name.strip():
        return "a session name cannot start or end with a space"
    if len(name) > MAX_TITLE_LEN:
        return "a session name can be at most %d characters" % MAX_TITLE_LEN
    if "/" in name or "\\" in name:
        return "a session name cannot contain / or \\"
    if "::" in name:
        return "a session name cannot contain ::"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "a session name cannot contain control characters"
    if set(name) == {"."}:
        return "a session name cannot be only dots"
    return ""


def _taken_by(srv, name: str, exclude: str) -> str:
    """The live session already called ``name`` — by title, by tmux name (two
    titles may sanitise to one: whitespace is dropped, ``.`` becomes ``_``), or
    by the alias it shows — ignoring ``exclude``; "" when free. Call under
    ``ENGINE.lock``."""
    if name in srv.ENGINE.instances and name != exclude:
        return name
    want = tmux.to_mindflock_tmux_name(name)
    for t in srv.ENGINE.instances:
        if t != exclude and tmux.to_mindflock_tmux_name(t) == want:
            return t
    return srv._aliases.owner_of(name, srv._session_branches(), exclude=exclude)


# --------------------------------------------------------------------------- #
# The registry of title-keyed stores
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Store:
    """One place that keys state by session title (or by its tmux name).

    ``rename(old, new)`` moves its data; ``keyed`` says which name it is
    called with (``"title"`` or ``"tmux"`` — the tmux names of the agent
    session). ``files`` lists the config-dir files it persists, so
    ``tests/unit/test_rename.py`` can fail when a new title-keyed store
    appears without an entry here."""

    name: str
    rename: Callable[[str, str], object]
    files: Tuple[str, ...] = ()
    keyed: str = "title"


#: Every store a rename walks, in order. Modules register at import time via
#: :func:`register`; :func:`_builtin_stores` adds the ones this module owns.
REGISTRY: List[Store] = []
_REGISTRY_LOCK = threading.Lock()


def register(
    name: str,
    rename: Callable[[str, str], object],
    *,
    files=(),
    keyed: str = "title",
) -> None:
    """Add (or replace, by ``name``) one store in :data:`REGISTRY`."""
    store = Store(name, rename, tuple(files), keyed)
    with _REGISTRY_LOCK:
        for i, s in enumerate(REGISTRY):
            if s.name == name:
                REGISTRY[i] = store
                return
        REGISTRY.append(store)


def registered_files() -> set:
    """Every config-dir file name the registry declares."""
    with _REGISTRY_LOCK:
        return {f for s in REGISTRY for f in s.files}


def _rename_stores(old: str, new: str) -> List[str]:
    """Run every registry entry; returns the names of the ones that failed
    (each is isolated: one broken store never stops the others)."""
    failed: List[str] = []
    old_n, new_n = tmux.to_mindflock_tmux_name(old), tmux.to_mindflock_tmux_name(new)
    with _REGISTRY_LOCK:
        stores = list(REGISTRY)
    for s in stores:
        try:
            if s.keyed == "tmux":
                s.rename(old_n, new_n)
            else:
                s.rename(old, new)
        except Exception as err:  # noqa: BLE001 — a rename has already happened
            failed.append(s.name)
            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "rename %s -> %s: store %s failed: %v", old, new, s.name, err
                )
    return failed


def move_key(d: dict, old, new) -> bool:
    """``d[new] = d.pop(old)`` when ``old`` is present (never overwriting a
    present ``new`` with nothing). True when something moved."""
    if old in d:
        d[new] = d.pop(old)
        return True
    return False


def replace_value(value, old: str, new: str):
    """``new`` when ``value == old``; a list/tuple with each ``old`` replaced;
    ``value`` unchanged otherwise."""
    if value == old:
        return new
    if isinstance(value, list):
        return [new if v == old else v for v in value]
    if isinstance(value, tuple):
        return tuple(new if v == old else v for v in value)
    return value


# --------------------------------------------------------------------------- #
# Files named after the tmux name
# --------------------------------------------------------------------------- #
def _marker_dirs() -> List[str]:
    """The directories whose files are named ``<tmux name><suffix>``: activity
    markers (+ reply files), thread markers (+ history, per-account), voice and
    Flock-mode flags."""
    out: List[str] = []
    try:
        from backend.providers import activity_markers as _am

        out.append(str(_am.marker_dir()))
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.providers import thread_markers as _tm

        out.append(str(_tm.marker_dir()))
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import voice as _voice

        out.append(str(_voice.flag_dir()))
    except Exception:  # noqa: BLE001
        pass
    return out


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "")


def _move_named_files(old_n: str, new_n: str, shell_too: bool) -> int:
    """Rename every ``<old tmux name>.…`` / ``<old>@…`` file in the marker dirs
    to the new name (and the ``_sh`` shell session's, when ``shell_too``).
    Tmux names hold no ``.`` and sanitised names no ``@``, so the prefix
    cannot match another session's file. Returns how many moved."""
    pairs = [(_safe(old_n), _safe(new_n))]
    if shell_too:
        pairs.append((_safe(old_n + _SHELL_SUFFIX), _safe(new_n + _SHELL_SUFFIX)))
    moved = 0
    for d in _marker_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fname in names:
            for o, n in pairs:
                if fname.startswith(o + ".") or fname.startswith(o + "@"):
                    try:
                        os.replace(
                            os.path.join(d, fname),
                            os.path.join(d, n + fname[len(o) :]),
                        )
                        moved += 1
                    except OSError:
                        pass
                    break
    return moved


def _move_exit_marker(old_n: str, new_n: str) -> None:
    """The agent's launch wrapper baked its exit-marker path in when it
    started (``…; echo $? > <old>.code``, core.terminal._wrap_launch_cmd). A
    finished agent's marker is moved; a RUNNING agent gets ``<old>.code`` as a
    symlink to ``<new>.code``, so when it exits its code lands under the name
    every reader now asks for. A relaunch's wrapper starts with ``rm -f`` of
    the new path and never touches the link, which :func:`forget` removes."""
    try:
        from backend.web.core import terminal as _terminal

        old_p = _terminal._exit_marker_path(old_n)
        new_p = _terminal._exit_marker_path(new_n)
    except Exception:  # noqa: BLE001
        return
    try:
        if old_p.is_symlink():
            old_p.unlink()
        elif old_p.exists():
            os.replace(old_p, new_p)
            return
        new_p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(new_p.name, old_p)
    except OSError:
        pass


def _move_mcp_run_file(old_n: str, new: str, new_n: str) -> None:
    """The session's Claude ``--mcp-config`` run file names its identity
    (``MINDFLOCK_SESSION_TITLE``). Write the new run file with the new title,
    and rewrite the OLD file the running CLI was pointed at the same way, so
    an MCP reconnect inside the running agent comes back as the new name. The
    old file goes with the ``renamed_from`` entry (:func:`forget`)."""
    try:
        from backend.providers import mcp_attach as _mcp
        from backend.fsutil import read_json
    except Exception:  # noqa: BLE001
        return
    old_p, new_p = _mcp.run_file_path(old_n), _mcp.run_file_path(new_n)
    doc = read_json(old_p, None, expect=dict)
    if doc is None:
        return
    entry = (doc.get("mcpServers") or {}).get(_mcp.SERVER_NAME)
    if isinstance(entry, dict) and isinstance(entry.get("env"), dict):
        if entry["env"].get("MINDFLOCK_SESSION_TITLE"):
            entry["env"]["MINDFLOCK_SESSION_TITLE"] = new
    try:
        _mcp._write_json_0600(new_p, doc)
        _mcp._write_json_0600(old_p, doc)
    except Exception:  # noqa: BLE001
        pass
    try:
        with _mcp._LAUNCHED_LOCK:
            move_key(_mcp._LAUNCHED, old_n, new_n)
    except Exception:  # noqa: BLE001
        pass


def _tmux_rename(old_n: str, new_n: str) -> bool:
    """``tmux rename-session`` for the agent session and its ``_sh`` shell.
    True when the agent's tmux session was renamed (or did not exist)."""
    ok = True
    for o, n in ((old_n, new_n), (old_n + _SHELL_SUFFIX, new_n + _SHELL_SUFFIX)):
        try:
            if (
                subprocess.run(
                    ["tmux", "has-session", "-t=" + o],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                ).returncode
                != 0
            ):
                continue
            cp = subprocess.run(
                ["tmux", "rename-session", "-t=" + o, n],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            if cp.returncode != 0 and o == old_n:
                ok = False
        except (OSError, subprocess.SubprocessError):
            if o == old_n:
                ok = False
    return ok


def _pane_pid(tmux_name: str) -> str:
    """The agent pane's pid ("" when there is no such tmux session)."""
    try:
        cp = subprocess.run(
            ["tmux", "display-message", "-p", "-t", "=" + tmux_name, "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


# --------------------------------------------------------------------------- #
# renamed_from: old title -> the session it became
# --------------------------------------------------------------------------- #
_MAP_LOCK = threading.Lock()
_MAP: Optional[Dict[str, dict]] = None  # old -> {"to", "at", "pid"}
_MAP_FILE = "session_renamed_from.json"


def _map_path() -> str:
    return os.path.join(config.GetConfigDir(), _MAP_FILE)


def _map() -> Dict[str, dict]:
    global _MAP
    if _MAP is None:
        _MAP = title_store.load(
            _map_path,
            lambda d: {
                str(k): dict(v)
                for k, v in d.items()
                if isinstance(v, dict) and v.get("to")
            },
        )
    return _MAP


def _note_renamed(old: str, new: str, pid: str) -> None:
    """Record ``old -> new``; collapse chains (whatever pointed at ``old`` now
    points at ``new``) and drop ``new`` as a key (it is a live title now)."""
    with _MAP_LOCK:
        m = _map()
        for rec in m.values():
            if rec.get("to") == old:
                rec["to"] = new
        m.pop(new, None)
        m[old] = {"to": new, "at": time.time(), "pid": pid}
        title_store.persist(_map_path, m, "session renames")


def resolve(title: str) -> str:
    """The live session an OLD title now names, else ``title`` unchanged.
    Only for a title that is not itself a live session."""
    try:
        with _MAP_LOCK:
            rec = _map().get(str(title or ""))
        return str(rec["to"]) if rec else str(title or "")
    except Exception:  # noqa: BLE001
        return str(title or "")


def former_titles(title: str) -> List[str]:
    """The old titles that resolve to ``title`` (oldest first)."""
    try:
        with _MAP_LOCK:
            items = [
                (r.get("at", 0), o) for o, r in _map().items() if r.get("to") == title
            ]
        return [o for _, o in sorted(items)]
    except Exception:  # noqa: BLE001
        return []


def overlay(rows) -> None:
    """Stamp ``renamed_from`` (the names this session used to have, which a
    running agent's MCP may still carry) on this device's own rows."""
    try:
        with _MAP_LOCK:
            m = dict(_map())
    except Exception:  # noqa: BLE001
        return
    if not m:
        return
    by_new: Dict[str, List[str]] = {}
    for o, r in m.items():
        by_new.setdefault(str(r.get("to")), []).append(o)
    for r in rows:
        if isinstance(r, dict) and not r.get("device"):
            olds = by_new.get(str(r.get("title") or ""))
            if olds:
                r["renamed_from"] = sorted(olds)


def _forget_entry(old: str) -> None:
    """Drop one old name's leftovers: the exit-marker link and the MCP run
    file its running agent was pointed at."""
    old_n = tmux.to_mindflock_tmux_name(old)
    try:
        from backend.web.core import terminal as _terminal

        p = _terminal._exit_marker_path(old_n)
        if p.is_symlink():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.providers import mcp_attach as _mcp

        os.remove(_mcp.run_file_path(old_n))
    except Exception:  # noqa: BLE001
        pass


def prune(live_titles, pane_pid: Callable[[str], str] = None) -> List[str]:
    """Drop entries whose session is gone, whose old name is a live session
    again, or whose agent relaunched since the rename (its pane pid changed —
    the relaunch baked the new name into its MCP config and wrapper). Returns
    the dropped old names."""
    live = set(live_titles or ())
    pane_pid = pane_pid or _pane_pid
    dropped: List[str] = []
    with _MAP_LOCK:
        m = _map()
        for old, rec in list(m.items()):
            to = str(rec.get("to") or "")
            gone = to not in live
            if not gone and rec.get("pid"):
                now_pid = pane_pid(tmux.to_mindflock_tmux_name(to))
                gone = bool(now_pid) and now_pid != rec.get("pid")
            if gone:
                m.pop(old, None)
                dropped.append(old)
        if dropped:
            title_store.persist(_map_path, m, "session renames")
    for old in dropped:
        if old not in live:
            _forget_entry(old)
    return dropped


def forget(title: str) -> None:
    """A removed session takes its old names with it."""
    with _MAP_LOCK:
        m = _map()
        olds = [o for o, r in m.items() if r.get("to") == title]
        for o in olds:
            m.pop(o, None)
        if olds:
            title_store.persist(_map_path, m, "session renames")
    for o in olds:
        _forget_entry(o)


def _reset_for_tests() -> None:
    global _MAP
    with _MAP_LOCK:
        _MAP = None


# --------------------------------------------------------------------------- #
# The rename
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    status: int
    body: dict = field(default_factory=dict)


def rename_session(srv, old: str, new: str) -> Result:
    """Rename ``old`` to ``new`` everywhere. ``srv`` is ``backend.web.server``.

    200 ``{title, from}`` (``unchanged: true`` for the same name); 400 for an
    invalid name; 404 for no such session; 409 for a name another session has
    (``session`` names it), for a remote or shared-folder (peer) row, or when
    tmux refused."""
    old = str(old or "")
    new = str(new or "").strip()
    if "::" in old:
        return Result(409, {"error": "a session on another device is renamed there"})
    err = title_error(new)
    if err:
        return Result(400, {"error": err})
    eng = srv.ENGINE
    with eng.lock:
        inst = eng.instances.get(old)
        if inst is None:
            return Result(404, {"error": "instance not found: %s" % old})
        if srv._peer_guard.is_peer_session(inst):
            return Result(
                409,
                {
                    "error": "renaming is not available for a shared-folder (peer) session",
                    "peer_share": True,
                },
            )
        if new == old:
            return Result(200, {"title": old, "from": old, "unchanged": True})
        taken = _taken_by(srv, new, exclude=old)
        if taken:
            return Result(
                409,
                {
                    "error": "another session is already named “%s”" % new,
                    "session": taken,
                },
            )
        old_n = tmux.to_mindflock_tmux_name(old)
        new_n = tmux.to_mindflock_tmux_name(new)
        pid = _pane_pid(old_n) if old_n != new_n else ""
        if old_n != new_n and not _tmux_rename(old_n, new_n):
            return Result(409, {"error": "tmux could not rename the session"})
        # The engine: the key, the instance, its tmux handle and worktree
        # record, and every child that names it as its parent.
        eng.instances.pop(old, None)
        inst.Title = new
        eng.instances[new] = inst
        ts = getattr(inst, "_tmux_session", None)
        if ts is not None and hasattr(ts, "sanitized_name"):
            ts.sanitized_name = new_n
        wt = getattr(inst, "_git_worktree", None)
        if wt is not None and hasattr(wt, "sessionName"):
            wt.sessionName = new
        for t, i in eng.instances.items():
            if t != new and (getattr(i, "Parent", "") or "") == old:
                i.Parent = new
        # Files named after the tmux name, then every title-keyed store.
        if old_n != new_n:
            _move_named_files(old_n, new_n, shell_too=True)
            _move_exit_marker(old_n, new_n)
            _move_mcp_run_file(old_n, new, new_n)
        failed = _rename_stores(old, new)
        _note_renamed(old, new, pid)
    # Persist outside the registry lock (the save takes the state-file lock
    # and re-enters ENGINE.lock itself): the old title is excluded, which
    # tombstones it, so a co-running server's merge cannot resurrect it.
    eng.save(exclude_titles=[old])
    _republish(srv, old, new)
    if log.InfoLog is not None:
        log.InfoLog.Printf("renamed session %s -> %s", old, new)
    body = {"title": new, "from": old}
    if failed:
        body["failed_stores"] = failed
    return Result(200, body)


def _republish(srv, old: str, new: str) -> None:
    """Re-key the published row (the listing carries probe fields over by
    title, so the renamed row keeps its state instead of placeholders) and
    tell every browser: ``session.renamed {from, to}``."""
    try:
        rows = srv._events.sessions_snapshot()
        for r in rows:
            if r.get("title") == old:
                r["title"] = new
                r["tmux_name"] = tmux.to_mindflock_tmux_name(new)
                r["name"] = srv._aliases.display_name(new, str(r.get("branch") or ""))
            if r.get("parent") == old:
                r["parent"] = new
        srv._events.set_sessions_snapshot(rows)
    except Exception:  # noqa: BLE001
        pass
    srv._events.BUS.emit(
        "session.renamed",
        session=old,
        old=old,
        new=new,
        data={"from": old, "to": new, "name": new, "title": new},
    )


# --------------------------------------------------------------------------- #
# Migration: aliases become titles
# --------------------------------------------------------------------------- #
_MIGRATION_FILE = "rename_migration.json"


def migrate_aliases(srv, aliases: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Rename every live session whose alias differs from its title to that
    alias, when it is a free valid name; skip (and log) otherwise. Returns
    ``{old: new}`` for the renames made. ``aliases`` defaults to the server's
    own store; a migrated alias is dropped from it (the rename dropped it)."""
    src = dict(aliases if aliases is not None else srv._aliases.all_aliases())
    done: Dict[str, str] = {}
    for title, alias in src.items():
        title, alias = str(title or ""), str(alias or "").strip()
        if not alias or alias == title or title not in srv.ENGINE.instances:
            continue
        if _agent_live(srv, title):
            # A running agent's MCP server was started with the old title and
            # may predate rename support (an upgrade restarts the server, not
            # the agents): renaming under it strands its identity (readonly
            # tools). It keeps its alias and migrates at a start when its
            # agent is down — after a crash, or once it was quit.
            continue
        res = rename_session(srv, title, alias)
        if res.status == 200 and not res.body.get("unchanged"):
            done[title] = res.body["title"]
        elif log.InfoLog is not None:
            log.InfoLog.Printf(
                "rename migration: %s stays aliased %s (%s)",
                title,
                alias,
                res.body.get("error", res.status),
            )
    return done


def _agent_live(srv, title: str) -> bool:
    """Whether ``title``'s agent tmux session is running now. An unreadable
    answer counts as live (never rename under an agent we can't see)."""
    try:
        return srv._live_session_name(tmux.to_mindflock_tmux_name(title)) is not None
    except Exception:  # noqa: BLE001
        return True


def migrate_once(srv) -> Dict[str, str]:
    """:func:`migrate_aliases` for the server's alias store at every server
    start (``rename_migration.json`` records the last run). Sessions whose
    agent is running are skipped and migrate at a later start, so the name
    is only taken over while no MCP server holds the old one. Never raises."""
    path = os.path.join(config.GetConfigDir(), _MIGRATION_FILE)
    try:
        done = migrate_aliases(srv)
        if not done and os.path.exists(path):
            return {}
        title_store.persist(
            lambda: path,
            {"migrated_at": time.time(), "renamed": done},
            "rename migration",
        )
        return done
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("rename migration failed: %v", err)
        return {}


# --------------------------------------------------------------------------- #
# Requests that still carry an old title
# --------------------------------------------------------------------------- #
_PREFIX = "/api/instances/"


def rewrite_path(path: str, live) -> str:
    """``/api/instances/<old>[/…]`` → the same path for the session ``<old>``
    became, when ``<old>`` is not a live title but a recorded former one;
    ``path`` unchanged otherwise. ``live`` is a container of live titles."""
    if not path.startswith(_PREFIX):
        return path
    rest = path[len(_PREFIX) :]
    title, sep, tail = rest.partition("/")
    if not title or "::" in title or title in live:
        return path
    to = resolve(title)
    if to == title or to not in live:
        return path
    return _PREFIX + to + sep + tail


class RenamedTitleMiddleware:
    """Route a request (HTTP or websocket) for an old title to the renamed
    session — a running agent's MCP server and hooks may still say the name it
    was launched with. Pure ASGI, so websockets are covered too."""

    def __init__(self, app, live: Callable[[], object]) -> None:
        self.app = app
        self._live = live

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path") or ""
            if path.startswith(_PREFIX):
                try:
                    from urllib.parse import quote

                    new = rewrite_path(path, self._live())
                    if new != path:
                        scope = dict(scope)
                        scope["path"] = new
                        scope["raw_path"] = quote(new).encode("ascii")
                except Exception:  # noqa: BLE001 — never break a request
                    pass
        await self.app(scope, receive, send)


_PRUNE_EVERY_S = 30.0
_PRUNED_AT = [0.0]


def prune_soon(live_titles) -> None:
    """:func:`prune` at most every :data:`_PRUNE_EVERY_S` (the instances tick
    calls this; an empty map costs nothing). Never raises."""
    try:
        now = time.monotonic()
        if now - _PRUNED_AT[0] < _PRUNE_EVERY_S:
            return
        _PRUNED_AT[0] = now
        with _MAP_LOCK:
            empty = not _map()
        if not empty:
            prune(live_titles)
    except Exception:  # noqa: BLE001 — housekeeping
        pass
