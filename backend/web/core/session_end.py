"""Ending a session: the one function behind DELETE, /close, /cleanup and the
workspace delete.

Every way a session is ended on purpose used to be its own route body, and
the bodies drifted: DELETE skipped the Recently-closed record, /cleanup
removed a folder a second session (a copy) was still working in, and each
orphaned its children differently. :func:`end_session` is the single path:

* stop the agent (its tmux session and PTY) and the shell pane;
* ``wipe=False`` keeps the worktree and branch exactly as they are;
* ``wipe=True`` removes both — but only after :func:`folder_in_use` says no
  other live session has the same folder (compared by resolved real path),
  else :class:`FolderInUse` is raised and **nothing** has changed;
* the session is **always** recorded in Recently closed, so an ended session
  can be found again whichever button ended it (a wiped one is listed with
  its folder gone);
* its children keep running: their ``Parent`` becomes the ended session's
  own parent (or none), so lineage never dangles, and they are reported back
  so the UI can tell the user.

The routes keep their paths and success shapes; they add the keys of the
returned dict. Server-side state (registry, per-title caches, red zones,
IDE window) is reached through the lazily imported server module, the way
the other ``core`` modules do, so the route tests' seams still apply.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, List, Optional

from backend import log


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


class SessionNotFound(LookupError):
    """``end_session`` was asked for a title that is not in the registry."""


class FolderInUse(RuntimeError):
    """A wipe was refused: other live sessions work in the same folder.

    ``sessions`` holds their titles; the route answers **409**
    ``{"error": "folder in use", "sessions": [...]}`` with it.
    """

    def __init__(self, sessions: List[str]):
        super().__init__("folder in use")
        self.sessions = list(sessions)

    def to_json(self) -> dict:
        return {"error": "folder in use", "sessions": list(self.sessions)}


def _worktree_of(inst: Any) -> str:
    try:
        return inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        return ""


def folder_in_use(engine: Any, title: str, folder: str) -> List[str]:
    """Titles of every live instance other than ``title`` whose worktree is
    ``folder`` (both sides resolved with ``os.path.realpath``), sorted.

    The check behind refusing to remove a folder: a session made by
    "Duplicate session" runs in place in its origin's worktree, and one more
    can be adopted on the same directory by hand, so the folder belongs to all
    of them until the last one is gone.
    """
    if not folder:
        return []
    try:
        real = os.path.realpath(folder)
    except OSError:
        return []
    others = []
    for t, inst in list(engine.instances.items()):
        if t == title:
            continue
        wp = _worktree_of(inst)
        if not wp:
            continue
        try:
            if os.path.realpath(wp) == real:
                others.append(t)
        except OSError:
            continue
    return sorted(others)


def reparent_children(engine: Any, title: str) -> List[str]:
    """Move every child of ``title`` to ``title``'s own parent (or to none)
    and return their titles, sorted. The caller persists (``engine.save``).

    Children keep running when their parent is ended; only the link moves up
    one step, so an orchestrator's workers land under *its* orchestrator
    rather than dangling on a name the next session may take.
    """
    with engine.lock:
        me = engine.instances.get(title)
        grand = (getattr(me, "Parent", "") or "") if me is not None else ""
        if grand == title or grand not in engine.instances:
            grand = ""
        kids = sorted(
            t
            for t, i in engine.instances.items()
            if t != title and (getattr(i, "Parent", "") or "") == title
        )
        for t in kids:
            engine.instances[t].Parent = grand
    return kids


def end_session(
    engine: Any,
    title: str,
    *,
    wipe: bool,
    force: bool = False,
    scrub: bool = False,
) -> dict:
    """End the session ``title``; see the module docstring for the contract.

    ``wipe`` removes the worktree and branch (``inst.Kill``); without it only
    the agent and shell panes are stopped. ``force`` skips the folder-in-use
    refusal (the caller has confirmed the other sessions may lose the folder).
    ``scrub`` (the /cleanup route) additionally ``rmtree``s whatever ``Kill``
    left behind — never for an in-place session, whose folder is the user's
    own checkout.

    Returns ``{"title", "wiped", "children"}``. Raises :class:`SessionNotFound`
    and :class:`FolderInUse`; everything else is logged, because the removal
    has already begun and must not be reported as failed by its housekeeping.
    Blocking (it shells out): call it off the event loop.
    """
    srv = _server()
    inst = engine.instances.get(title)
    if inst is None:
        raise SessionNotFound(title)
    wt = _worktree_of(inst)
    in_place = bool(getattr(inst, "InPlace", False))

    # A. Refuse to pull the folder out from under a sibling — and change
    # nothing: no record, no kill, no registry change.
    # ...but only when the wipe would actually remove something: an in-place
    # session's folder is the user's own checkout (never removed), and a
    # folder that is already gone has nothing left to lose. Refusing those
    # would make a Duplicate copy undeletable while its origin runs, and a
    # "clean up missing" sweep fail for every session that shared the folder.
    if wipe and not force and not in_place and wt and os.path.isdir(wt):
        others = folder_in_use(engine, title, wt)
        if others:
            raise FolderInUse(others)

    # The undo record FIRST, while the instance still serializes with its
    # worktree, branch and program intact. A wiped session is listed with its
    # folder gone (the page shows it as not reopenable); a kept one reopens.
    srv._record_closed(inst, wiped=wipe)

    rz_shared = bool(folder_in_use(engine, title, wt))
    if wipe:
        # Red zones: drop the feed cursor/plan latches (titles are reused) and
        # tombstone the title so the reconcile loop can't re-adopt it while
        # Kill runs; when no other session shares the worktree, remove its
        # guard file. BEFORE Kill, while the worktree path still resolves the
        # way the guard file's name was derived from it. The worktree-scope
        # zones are settled AFTER Kill (after_kill), once we know whether the
        # folder is really gone — an in-place session's folder is the user's
        # own checkout and keeps its "This worktree" zones.
        srv._red_zone_monitor.forget(
            title, wt, rz_shared, worktree_removed=False, inst=inst
        )
        try:
            inst.Kill()
        except Exception as err:  # noqa: BLE001
            # Even if kill partially failed, drop it from the active set.
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("kill error for %s: %v", title, err)
        if scrub and wt and not in_place:
            # Belt and braces over Kill: whatever it left (a worktree remove
            # that failed on a lock file, a clone it does not own) goes too.
            shutil.rmtree(wt, ignore_errors=True)
        # The dying agent's last Post hooks can recreate the feed file between
        # forget and Kill; and the zones go only with a folder that is gone.
        srv._red_zone_monitor.after_kill(title, wt, rz_shared)
    else:
        # Keep the worktree: only the agent's tmux session (and PTY) goes.
        srv._kill_agent_session(title)
    srv._kill_shell_session(title)

    # Keep the IDE window open if another live session (e.g. a copy) still
    # shares this worktree — only close it when this is the last one on the
    # dir. Same for the Atlas index and the ~/.claude.json trust entry, which
    # only a removed folder has no further use for.
    if not srv._worktree_in_use_by_other(wt, title):
        srv._close_cursor_window(wt)
        if wipe:
            srv._code_outline.forget(wt)
            # GC the trust entry pre_trust_workdir seeded for this worktree
            # (G3) — guarded internally to MindFlock-owned paths only.
            srv._remove_trust_entry(wt)

    # C. Children keep running under the ended session's own parent, so the
    # removal teardown below finds nothing left to orphan.
    children = reparent_children(engine, title)

    with engine.lock:
        engine.instances.pop(title, None)
    # Merge-on-save with the ended title excluded removes it from disk without
    # the stale-set rewrite that storage.DeleteInstance would do (which would
    # both clobber other processes' instances and reconstruct/attach every
    # other session via LoadInstances). The children's new parent is saved
    # by the same write.
    engine.save(exclude_titles={title})
    srv._EVENT_SNAPSHOT.pop(title, None)
    srv._aliases.drop(title)
    srv._BUDGET_FIRED.pop(title, None)
    # Titles come straight back — a reopen restores this very one — so the
    # rolling record goes with the session rather than lingering to be
    # inherited. A recreated same-title session must also start with a fresh
    # branch baseline and its own port block.
    srv._forget_session_state(title)
    srv._LAST_BRANCH.pop(title, None)
    srv._ports.release(title)
    # The flock-wide teardown every removal runs (mailbox, autopilot record,
    # MCP run file, …). Its orphaning pass is a no-op here: the children have
    # already moved up.
    srv._on_session_removed(title)
    data: Optional[dict] = None
    if not wipe:
        data = {"closed": True}
    elif scrub:
        data = {"cleaned": True}
    if children:
        data = dict(data or {}, children=list(children))
    if data is None:
        srv._events.BUS.emit("session.deleted", session=title)
    else:
        srv._events.BUS.emit("session.deleted", session=title, data=data)
    return {"title": title, "wiped": bool(wipe), "children": list(children)}
