"""The registry entries of :mod:`core.rename`: every store that keys state by
session title (or by its tmux name) and how a rename moves it.

One function per store, each under the store's own lock and through its own
load/save, so the store modules stay as they are. Each is registered with the
config-dir files it persists — ``tests/unit/test_rename.py`` scans the source
for ``*.json`` store names and fails on one that is neither registered here
nor listed there as not session-keyed.

Deliberately NOT here: Recently closed (a closed session keeps the name it
closed under), the worktree folder and the branch (named by branch), and the
plans of flights (they hold no session).
"""

from __future__ import annotations

import os
import time
from typing import Iterable

from backend.web.core import rename as _rename
from backend.web.core.rename import move_key, register, replace_value


def _server():
    from backend.web import server

    return server


def _swap_fields(obj, old: str, new: str, keys: Iterable[str], skip=()) -> bool:
    """Replace ``old`` with ``new`` in the values of ``keys`` (a string, or a
    list of strings), anywhere inside ``obj`` except under ``skip`` keys.
    True when anything changed."""
    keys, skip = set(keys), set(skip)
    changed = False
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k in skip:
                continue
            if k in keys:
                nv = replace_value(v, old, new)
                if nv != v:
                    obj[k] = nv
                    changed = True
                    continue
            if isinstance(v, (dict, list)):
                changed = _swap_fields(v, old, new, keys, skip) or changed
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                changed = _swap_fields(v, old, new, keys, skip) or changed
    return changed


def _title_map(mod, loader: str = "_load", persist: str = "_persist"):
    """The rename of a :mod:`title_store`-style module (one ``title -> value``
    dict, loaded once, rewritten whole)."""

    def rename(old: str, new: str) -> None:
        with mod._LOCK:
            if move_key(getattr(mod, loader)(), old, new):
                getattr(mod, persist)()

    return rename


# --------------------------------------------------------------------------- #
# title -> value maps
# --------------------------------------------------------------------------- #
def _aliases(old: str, new: str) -> None:
    """The renamed session is CALLED its new title: an alias it had (or a stale
    one the new title had) would hide that again."""
    from backend.web.core import aliases

    aliases.drop(old)
    aliases.drop(new)


def _ownership(old: str, new: str) -> None:
    from backend.web.core import ownership as m

    with m._LOCK:
        if move_key(m._owners(), old, new):
            m._persist()
        move_key(m._DRIVERS, old, new)
        for c in m._CONNS.values():
            if c.get("title") == old:
                c["title"] = new
        for key in [k for k in m._INPUT_AT if k and k[0] == old]:
            m._INPUT_AT[(new,) + tuple(key[1:])] = m._INPUT_AT.pop(key)


def _sleep(old: str, new: str) -> None:
    from backend.web.core import sleep as m

    move_key(m._WOKE_AT, old, new)
    move_key(m._WAKE_WHY, old, new)
    with m._LOCK:
        if move_key(m._load(), old, new):
            m._persist()


def _held(old: str, new: str) -> None:
    """``held.json`` (core.held, ``title -> record``) — guarded: the module
    lands from another branch. Uses its own ``rename`` when it has one, else
    its lock + load + persist/save the way the sibling stores do."""
    try:
        from backend.web.core import held as m  # type: ignore[attr-defined]
    except ImportError:
        return
    if callable(getattr(m, "rename", None)):
        m.rename(old, new)
        return
    lock = getattr(m, "_LOCK", None)
    load = getattr(m, "_load", None)
    save = getattr(m, "_persist", None) or getattr(m, "_save", None)
    if lock is None or not callable(load) or not callable(save):
        raise RuntimeError("core.held has no rename/_LOCK/_load/_persist to move a key")
    with lock:
        data = load()
        data = data[0] if isinstance(data, tuple) else data
        if isinstance(data, dict) and move_key(data, old, new):
            try:
                save()
            except TypeError:
                save(data)


def _prompt_queue(old: str, new: str) -> None:
    from backend.web.core import prompt_queue as m

    with m._LOCK:
        data = m._load()
        if move_key(data, old, new):
            m._save(data)


def _autopilot(old: str, new: str) -> None:
    from backend.web.core import autopilot as m

    with m._LOCK:
        data = m._load()
        if move_key(data, old, new):
            m._save(data)


def _ports(old: str, new: str) -> None:
    """The block stays the session's: its running dev servers keep their
    ports (only a NEW allocation hashes the title)."""
    from backend.web.core import ports as m

    with m._lock:
        data = m._load()
        if move_key(data, old, new):
            m._save(data)


def _budget(old: str, new: str) -> None:
    from backend.web.core import budget as m

    srv = _server()
    with m._BUDGET_OV_LOCK:
        if move_key(srv._budget_overrides(), old, new):
            srv._persist_budget_overrides()
    move_key(m._BUDGET_FIRED, old, new)


def _asking(old: str, new: str) -> None:
    from backend.web.core import asking as m

    with m._LOCK:
        m._load_store()
        moved = False
        for d in (m._STATE, m._INPUT_AT, m._DIALOG_MEMO, m._DISMISSED_STORED):
            moved = move_key(d, old, new) or moved
        if moved:
            m._save_store()


def _tracker_claims(old: str, new: str) -> None:
    from backend.ticket_ingestion import tracker_claims as m

    with m._LOCK:
        claims = m._load()
        if move_key(claims, old, new):
            m._save(claims)
    try:
        m.clear_cache()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Stores that NAME sessions inside their records
# --------------------------------------------------------------------------- #
def _mailbox(old: str, new: str) -> None:
    """The box moves (merged into a stale box of the new name, if any) and
    every message that names the session as sender or recipient follows."""
    from backend.web.core import mailbox as m

    with m._mutate() as data:
        boxes = data["boxes"]
        changed = False
        if old in boxes:
            box = boxes.pop(old)
            if new in boxes:
                boxes[new]["messages"] = list(boxes[new].get("messages") or []) + list(
                    box.get("messages") or []
                )
            else:
                boxes[new] = box
            changed = True
        for box in boxes.values():
            for msg in box.get("messages") or []:
                for k in ("from", "to"):
                    if msg.get(k) == old:
                        msg[k] = new
                        changed = True
        if not changed:
            raise m._NoChange
        m._bump(data, new)
    with m._WAITERS_LOCK:
        move_key(m._WAITERS, old, new)


def _finished_children(old: str, new: str) -> None:
    from backend.web.core import finished_children as m

    with m._LOCK:
        data = m._load()
        changed = move_key(data, old, new)
        for entries in data.values():
            for e in entries:
                if isinstance(e, dict) and e.get("title") == old:
                    e["title"] = new
                    changed = True
        if changed:
            m._save(data)


def _flights(old: str, new: str) -> None:
    """A flight's ``session``, and the workers its join/spawn steps recorded.
    The plan copy (``steps``) is skipped: a step's ``title`` is its label."""
    from backend.web.core import flights as m

    with m._LOCK:
        flights = m._load_flights_locked()
        changed = False
        for f in flights.values():
            changed = (
                _swap_fields(f, old, new, ("session", "title", "children"), ("steps",))
                or changed
            )
        if changed:
            m._save_flights_locked(flights)


def _worker_order(old: str, new: str) -> None:
    from backend.web.core import worker_order as m

    with m.edit() as data:
        parents = data.get("parents") or {}
        move_key(parents, old, new)
        for p in parents.values():
            for key in ("planned", "workers"):
                d = p.get(key)
                if isinstance(d, dict):
                    move_key(d, old, new)
            planned = p.get("planned") or {}
            for k, v in list(planned.items()):
                planned[k] = replace_value(v, old, new)
            for w in (p.get("workers") or {}).values():
                if not isinstance(w, dict):
                    continue
                if "after" in w:
                    w["after"] = replace_value(w["after"], old, new)
                if isinstance(w.get("why"), dict):
                    move_key(w["why"], old, new)


def _guests(old: str, new: str) -> None:
    """A guest link reaches the session by title: it must keep reaching it."""
    from backend.web.core import guests as m

    with m._LOCK:
        changed = False
        for link in m._load().values():
            nv = replace_value(link.get("sessions") or [], old, new)
            if nv != link.get("sessions"):
                link["sessions"] = nv
                changed = True
        for f in m._FOCUS.values():
            if f.get("title") == old:
                f["title"] = new
        if changed:
            m._persist()
            m._REVISION += 1


def _test_plans(old: str, new: str) -> None:
    from backend.web.core import test_plans as m

    with m._LOCK:
        data = m._load()
        if _swap_fields(m._plans_of(data), old, new, ("run_session", "session")):
            m._save(data)


def _template_runs(old: str, new: str) -> None:
    from backend.web.core import template_schedule as m

    for path in (m.runs_path(), m.flight_runs_path()):
        runs = m.load_runs(path)
        if _swap_fields(runs, old, new, ("last_session", "session")):
            m.save_runs(runs, path)


def _team_runs(old: str, new: str) -> None:
    from backend.web.core import team_runs as m

    for run in m.list_runs(include_finished=True):
        if _swap_fields(run, old, new, ("title",), ("announced",)):
            m.save(run)


def _job_expectations(old: str, new: str) -> None:
    from backend.providers import claude_background as m

    with m._store_lock:
        data = m._load_store()
        jobs = data.get("jobs") or {}
        if not any(
            isinstance(j, dict) and j.get("title") == old for j in jobs.values()
        ):
            return
        import copy

        data = copy.deepcopy(data)
        for j in data["jobs"].values():
            if isinstance(j, dict) and j.get("title") == old:
                j["title"] = new
        m._save_store(data, time.time())


def _session_fences(old_n: str, new_n: str) -> None:
    """Fences are keyed by the tmux name (the hook resolves its own at fire
    time) and owned by ``orch:<orchestrator title>``. The guard file of every
    touched worktree is rewritten at once — until it is, the renamed worker's
    hook would find no fence of its own and see the folder unfenced."""
    from backend.config import red_zones as m

    old_k, new_k = m._session_key(old_n), m._session_key(new_n)
    old_t, new_t = _title_of(old_n), _title_of(new_n)
    touched = []
    with m._LOCK:
        data = m._load()
        for wt, entry in (data.get("worktrees") or {}).items():
            sessions = entry.get("sessions")
            if not isinstance(sessions, dict):
                continue
            changed = move_key(sessions, old_k, new_k)
            if old_t is not None:
                for fence in sessions.values():
                    if isinstance(fence, dict):
                        for k in ("owner", "by"):
                            v = str(fence.get(k) or "")
                            # "by" is the orchestrator's title; "owner" is
                            # "orch:<title>" (or "run:<id>", left alone).
                            if v == old_t:
                                fence[k] = new_t
                                changed = True
                            elif v == "orch:" + old_t:
                                fence[k] = "orch:" + new_t
                                changed = True
            if changed:
                touched.append(wt)
        if touched:
            m._save(data)
    for wt in touched:
        try:
            m.sync_guard(wt, lroot=wt)
        except Exception:  # noqa: BLE001
            pass
    try:
        if os.path.exists(m.feed_path(old_n)):
            os.replace(m.feed_path(old_n), m.feed_path(new_n))
    except OSError:
        pass


#: tmux name -> title for the rename in flight (fences need both).
_TITLES: dict = {}


def _title_of(tmux_name: str):
    return _TITLES.get(tmux_name)


def _note_titles(old: str, new: str) -> None:
    """First entry: remember which titles the tmux names belong to."""
    from backend.session import tmux

    _TITLES.clear()
    _TITLES[tmux.to_mindflock_tmux_name(old)] = old
    _TITLES[tmux.to_mindflock_tmux_name(new)] = new


# --------------------------------------------------------------------------- #
# In-memory, per-process
# --------------------------------------------------------------------------- #
#: ``(module, attribute)`` of title-keyed dicts that live only in memory. A
#: missing one is skipped (the module renamed it; the test pins this list).
_MEMORY_DICTS = (
    ("backend.web.core.agent_state", "_ACTIVITY_CACHE"),
    ("backend.web.server", "_LIMIT_PROBE"),
    ("backend.web.server", "_TRUST_DISMISS_AT"),
    ("backend.web.server", "_HUMAN_INPUT_AT"),
    ("backend.web.core.queue_driver", "_QUEUE_STATE"),
    ("backend.web.core.queue_driver", "_LIMIT_STATE"),
    ("backend.web.core.queue_driver", "_WORKER_HOLD"),
    ("backend.web.core.queue_driver", "_MAIL_STATE"),
    ("backend.web.core.flight_driver", "_TURN_ENDED"),
    ("backend.web.core.team_run_driver", "_TURN_ENDED"),
    ("backend.web.core.stage_reset", "_PINS"),
    ("backend.web.core.pending", "_PENDING"),
    ("backend.web.core.chat", "_CHATS"),
    ("backend.web.core.task_headlines", "_MEMO"),
    ("backend.web.core.thread", "_SEEDS"),
    ("backend.web.core.voice_actions", "_ANSWERED_AT"),
)
#: The same, keyed by the tmux name.
_MEMORY_TMUX_DICTS = (
    ("backend.web.server", "_THREAD_RECORD_AT"),
    ("backend.web.core.code_map", "_TP_LATCH"),
    ("backend.providers.claude_background", "_last"),
)


def _launch_queue(old: str, new: str) -> None:
    """``launch_queue.json`` (core.launch_gate): a queued launch's title, a
    waiting create's parent, and the gate's in-memory entries."""
    from backend.web.core import launch_gate as m

    m.rename(old, new)


def _memory(old: str, new: str) -> None:
    import importlib

    for modname, attr in _MEMORY_DICTS:
        try:
            d = getattr(importlib.import_module(modname), attr, None)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict):
            move_key(d, old, new)
    try:
        from backend.web.core import voice_actions as va

        with va._LOCK:
            for rec in va._LIVE.values():
                if rec.get("title") == old:
                    rec["title"] = new
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import session_stats

        session_stats.forget_tokens(old)
    except Exception:  # noqa: BLE001
        pass
    try:
        _server()._forget_probes(old)
    except Exception:  # noqa: BLE001
        pass


def _memory_tmux(old_n: str, new_n: str) -> None:
    import importlib

    for modname, attr in _MEMORY_TMUX_DICTS:
        try:
            d = getattr(importlib.import_module(modname), attr, None)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict):
            move_key(d, old_n, new_n)
    try:
        from backend.web.core import code_map, spoken

        code_map.forget_plan(old_n)
        spoken.forget(old_n)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
def install() -> None:
    """Register every entry (idempotent: entries replace by name)."""
    from backend.web.core import hidden_sessions, person_sessions, shared_branch

    register("titles", _note_titles)
    register("aliases", _aliases, files=("session_aliases.json",))
    register(
        "hidden sessions", _title_map(hidden_sessions), files=("session_hidden.json",)
    )
    register(
        "session person", _title_map(person_sessions), files=("session_person.json",)
    )
    register(
        "shared branch",
        _title_map(shared_branch),
        files=("session_shared_branch.json",),
    )
    register("ownership", _ownership, files=("session_owner.json",))
    register("sleep", _sleep, files=("sleeping.json",))
    register("held", _held, files=("held.json",))
    register("prompt queue", _prompt_queue, files=("prompt_queues.json",))
    register("autopilot", _autopilot, files=("autopilot.json",))
    register("ports", _ports, files=("ports.json",))
    register("budget", _budget, files=("budget_overrides.json",))
    register("asking", _asking, files=("asking.json",))
    register("tracker claims", _tracker_claims, files=("tracker_claims.json",))
    register("mailbox", _mailbox, files=("mailbox.json",))
    register("finished children", _finished_children, files=("finished_children.json",))
    register("flights", _flights, files=("flights.json",))
    register("worker order", _worker_order, files=("worker_order.json",))
    register("guest links", _guests, files=("guest_links.json",))
    register("test plans", _test_plans, files=("test_plans.json",))
    register(
        "template runs",
        _template_runs,
        files=("template_runs.json", "schedule_runs.json"),
    )
    register("team runs", _team_runs)
    register("job expectations", _job_expectations, files=("job_expectations.json",))
    register("session fences", _session_fences, files=("red_zones.json",), keyed="tmux")
    register("memory", _memory)
    register("memory (tmux)", _memory_tmux, keyed="tmux")
    # After "sleep" and "memory": a renamed queued launch is rebuilt through
    # its kind's factory, which reads the sleep store and the prompt-queue
    # state under the NEW title.
    register("launch queue", _launch_queue, files=("launch_queue.json",))
    # core.rename's own map, so the coverage test sees it declared.
    register("renamed_from", lambda old, new: None, files=(_rename._MAP_FILE,))


install()
