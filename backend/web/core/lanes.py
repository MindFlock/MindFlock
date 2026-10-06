"""Ship lanes: how far MindFlock carries a session once its agent is done.

A LANE is the user's answer to "when it's done, what then?" — leave it, commit,
push, open a PR, or merge once checks pass — plus an optional "ask me before it
ships". It is not a second engine. Every lane is carried out by the autopilot
(``core.autopilot`` + the driver in ``server.py``), armed through
:func:`arm_session`, which is the body the ``/fast-track`` route used to hold.
So there is ONE driver per session whoever asked: the ⏩ button, the
``/lane`` route, ``ship-now``, the MCP's ``set_autopilot`` (which calls
``/fast-track``), and a team run (``core.team_run_driver``).

Lane → autopilot depth::

    leave  → no record at all (see below)        commit → commit
    push   → push       pr → pr       merge → merge

``leave`` disarms rather than arming the "agent" rung: an armed, running record
mutes the session's ``turn_ended`` announcement for as long as it runs (the
chain is meant to announce its own outcome), and a lane that ships nothing has
no outcome to announce. A team run's ``leave`` task is driven from the run's
own record instead.

"ASK ME BEFORE IT SHIPS" holds the run one rung short of its first outward
step: a commit lane stops when the agent is done (the "agent" rung), a push /
PR / merge lane stops once it has committed. The autopilot then finishes at the
held rung and the session waits on you in the Outbox; :func:`ship_now` (the
approval) re-arms it at the real target without re-earning the idle dwell.
The held rung is ``depth`` on the record; the target is ``lane`` there.

DUPLICATE WINDOWS share one branch (``foo`` + ``foo-copy``): the lane is a fact
about the BRANCH's work, so :func:`fill_duplicates` shows the owner's lane on
the copy's row with ``owner`` naming the window that actually drives it. The
copy is never armed by anything here.
"""

from __future__ import annotations

import os
import time
from typing import Iterable, Optional

from backend.web.core import autopilot as _autopilot

__all__ = [
    "LANES",
    "LaneError",
    "normalize_lane",
    "depth_of",
    "lane_of_depth",
    "held_depth",
    "awaiting_approval",
    "lane_of",
    "lane_dto",
    "fill_duplicates",
    "branch_key",
    "branch_driver",
    "shared_checkout",
    "arm_session",
    "set_lane",
    "ship_now",
]

#: The lanes, nearest first.
LANES = ("leave", "commit", "push", "pr", "merge")
_DEPTH_OF = {
    "leave": "agent",
    "commit": "commit",
    "push": "push",
    "pr": "pr",
    "merge": "merge",
}
_LANE_OF_DEPTH = {v: k for k, v in _DEPTH_OF.items()}


class LaneError(Exception):
    """A lane request that cannot be honoured: ``status`` is the HTTP code the
    route answers with, the message the sentence it says."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _server():
    """The ``backend.web.server`` module, imported lazily (circular import)."""
    from backend.web import server

    return server


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def normalize_lane(value) -> str:
    """A lane name for anything a caller may send, or ``""`` for "not one".

    Accepts the autopilot's own rung names too ("agent" is ``leave``, "off"
    is ``leave``), so ``/fast-track``'s vocabulary and the lane vocabulary
    can never disagree about what a word means."""
    v = str(value or "").strip().lower()
    if v in ("off", "agent", "none"):
        return "leave"
    return v if v in LANES else ""


def depth_of(lane: str) -> str:
    """The autopilot depth that carries ``lane`` out (``""`` when unknown)."""
    return _DEPTH_OF.get(normalize_lane(lane), "")


def lane_of_depth(depth: str) -> str:
    """The lane an autopilot depth carries out (``""`` for off/unknown)."""
    return _LANE_OF_DEPTH.get(_autopilot.normalize_depth(depth), "")


def held_depth(lane: str, ask_first: bool) -> str:
    """The depth to ARM for ``lane``: the lane's own, or — when asking first —
    one rung short of the first step that leaves the machine. Committing is
    local and undoable, so a commit lane that asks first stops at "agent" (the
    commit itself is what you approve) and every outward lane stops at
    "commit" (the push is)."""
    lane = normalize_lane(lane)
    if not ask_first or lane == "leave":
        return depth_of(lane)
    return "agent" if lane == "commit" else "commit"


def awaiting_approval(rec: Optional[dict]) -> bool:
    """Whether an autopilot record is parked at its held rung, waiting for the
    user's go: asked first, finished, and the lane goes further than where it
    stopped."""
    if not rec or not rec.get("ask_first") or rec.get("state") != "done":
        return False
    lane = normalize_lane(rec.get("lane"))
    held = _autopilot.normalize_depth(rec.get("depth"))
    if not lane or held in ("", "off"):
        return False
    return _autopilot.DEPTH_ORDER.index(held) < _autopilot.DEPTH_ORDER.index(
        depth_of(lane)
    )


def lane_of(title: str, rec: Optional[dict] = None) -> Optional[dict]:
    """The row's ``lane`` block for a session's OWN record, or None.

    ``{"target", "ask_first", "owner"}``. A record armed before lanes existed
    has no ``lane`` field; its depth is its lane."""
    if rec is None:
        rec = _autopilot.get(title)
    if not rec:
        return None
    target = normalize_lane(rec.get("lane")) or lane_of_depth(rec.get("depth"))
    if not target:
        return None
    return {
        "target": target,
        "ask_first": bool(rec.get("ask_first")),
        "owner": title,
        "by": str(rec.get("by") or "user"),
    }


def lane_dto(title: str) -> Optional[dict]:
    """:func:`lane_of` that never raises (a row build must not fail on it)."""
    try:
        return lane_of(title)
    except Exception:  # noqa: BLE001 — enrichment only
        return None


def branch_key(row: dict) -> str:
    """``"<repo>::<branch>"`` — what a duplicated window shares with its
    original. ``""`` when either half is unknown (no key, no grouping)."""
    repo = str((row or {}).get("repo") or "").strip()
    branch = str((row or {}).get("branch") or "").strip()
    if not repo or not branch:
        return ""
    return repo + "::" + branch


def fill_duplicates(rows: Iterable[dict]) -> None:
    """Give a copy window the lane of the window that drives its branch, in
    place. ``owner`` keeps naming the driver, so the UI can say "carried by
    foo" and nothing ever arms the copy. A row with its own lane keeps it."""
    rows = [r for r in rows if isinstance(r, dict) and not r.get("device")]
    owners = {}
    for r in rows:
        lane = r.get("lane")
        key = branch_key(r)
        if key and isinstance(lane, dict) and lane.get("owner") == r.get("title"):
            owners.setdefault(key, lane)
    if not owners:
        return
    for r in rows:
        if r.get("lane"):
            continue
        lane = owners.get(branch_key(r))
        if lane:
            r["lane"] = dict(lane)


def _inst_branch_key(srv, inst) -> str:
    """:func:`branch_key` of a live instance, from the same fields its row
    carries (``repo`` = :func:`snapshot._repo_name`, ``branch`` = ``Branch``)."""
    try:
        repo = srv._repo_name(inst)
    except Exception:  # noqa: BLE001
        repo = ""
    return branch_key({"repo": repo, "branch": getattr(inst, "Branch", "") or ""})


def branch_driver(title: str) -> str:
    """The OTHER live window that drives ``title``'s branch, or ``""``.

    Duplicate windows share one branch (``foo`` + ``foo-copy``); whichever of
    them carries an autopilot record (or belongs to a team run) is the one
    driver. A lane, an "ask first" or a Ship now on the other window would arm
    a SECOND driver on the same tree — both commit, both push — so the
    routes refuse it and name the window that already drives the branch."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return ""
    key = _inst_branch_key(srv, inst)
    if not key:
        return ""
    try:
        from backend.web.core import team_runs as _runs

        run_titles = set(_runs.title_index())
    except Exception:  # noqa: BLE001 — no run index: records alone decide
        run_titles = set()
    for other, oinst in list(srv.ENGINE.instances.items()):
        if other == title or _inst_branch_key(srv, oinst) != key:
            continue
        if _autopilot.get(other) is not None or other in run_titles:
            return other
    return ""


def shared_checkout(title: str) -> str:
    """Another live session working in the SAME folder as ``title`` (an
    in-place session on a checkout others share), or ``""``. A lane there
    would commit every sharer's work under one session's name — and the
    Outbox, keyed by (repo, branch), shows one approval for all of them."""
    srv = _server()
    inst = srv.ENGINE.instances.get(title)
    if inst is None or not getattr(inst, "InPlace", False):
        return ""

    def _path(i) -> str:
        try:
            p = i.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            p = ""
        return os.path.realpath(p) if p else ""

    mine = _path(inst)
    if not mine:
        return ""
    for other, oinst in list(srv.ENGINE.instances.items()):
        if other != title and _path(oinst) == mine:
            return other
    return ""


# --------------------------------------------------------------------------- #
# Arming (blocking: callers on the event loop use asyncio.to_thread)
# --------------------------------------------------------------------------- #
def arm_session(
    title: str,
    lane: str,
    *,
    ask_first: bool = False,
    message: str = "",
    base: str = "",
    source: str = "session",
    item: str = "",
    message_auto: Optional[bool] = None,
    require_workspace: bool = True,
    now: Optional[float] = None,
    by: str = "user",
) -> Optional[dict]:
    """Arm the autopilot to carry ``title`` along ``lane``. Returns the stored
    record, or None for ``leave`` (which disarms). ``by`` records who chose
    the lane (``"user"`` or ``"agent:<title>"``).

    The body of the ``/fast-track`` route, unchanged in what it decides about
    the commit message: a message typed by a human is kept as written; with
    none, an intake run's own name (and its placeholder flag) survives a
    re-arm; then a FAILED commit's on-disk message; then — only when there is
    work to describe — a cheap generated placeholder, replaced at commit time
    by one written from the final diff.

    ``require_workspace=False`` is the intake shape: the session may not exist
    yet (a team run arms before it creates, so a restart mid-launch keeps the
    target). Raises :class:`LaneError` for an unknown lane, an unknown session
    or a session with no workspace yet (when one is required).
    """
    srv = _server()
    lane = normalize_lane(lane)
    if not lane:
        raise LaneError("unknown lane — pick one of: " + ", ".join(LANES))
    inst = srv.ENGINE.instances.get(title)
    if require_workspace and inst is None:
        raise LaneError("instance not found: %s" % title, 404)
    wt = ""
    if inst is not None:
        try:
            wt = inst.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            wt = ""
    if require_workspace and not wt:
        raise LaneError("workspace not ready", 409)
    if lane == "leave":
        _autopilot.disarm(title)
        return None
    msg = str(message or "").strip()
    msg_auto = bool(message_auto) if message_auto is not None else False
    if not msg:
        # An intake-armed run already carries the ticket / PR / issue NAME as its
        # message. Re-arming with ⏩ used to overwrite that with a generated
        # "Work on <slug>", throwing away the one genuinely descriptive subject
        # available. Prefer it — and carry its placeholder flag across, or
        # re-arming would freeze a placeholder into the commit.
        # A subject a model WROTE for an earlier commit describes that diff,
        # not this work: it is never carried as if someone had typed it.
        prev = _autopilot.get(title) or {}
        if not prev.get("message_written"):
            msg = str(prev.get("message") or "").strip()
            msg_auto = bool(msg) and bool(prev.get("message_auto"))
    if not msg and wt:
        # Only adopt the on-disk message when a FAILED attempt is pending — the
        # same rule GET /commit-message applies. Reusing it unconditionally meant
        # a message left by unrelated work became the subject of whatever was
        # armed next.
        msg = srv._pending_commit_message(wt)
    if not msg and wt and srv._is_dirty(wt):
        # The ⏩ button presses with no message, and `.mindflock_commit_msg` only
        # exists once something has committed THROUGH MindFlock — so the single
        # most common press ("I have work, carry it to a PR") used to be rejected
        # outright. Generate a subject instead of refusing. It stays the CHEAP
        # default rather than asking a model here: arming is arm-and-wait, so a
        # message written from the tree as it looks right now would describe a
        # diff that no longer exists by the time the commit happens. The real
        # message is written at commit time.
        msg = srv._autopilot_default_message(inst, wt)
        msg_auto = bool(msg)
    branch = ""
    if wt:
        try:
            branch = srv._current_branch(wt) or ""
        except Exception:  # noqa: BLE001
            branch = ""
    rec = _autopilot.arm(
        title,
        held_depth(lane, ask_first),
        source=source,
        item=item,
        message=msg,
        message_auto=msg_auto,
        base=str(base or ""),
        branch=branch,
        retryable=srv._precommit_retry_hooks(),
        boot=srv._SERVER_BOOT_ID,
        now=now,
        lane=lane,
        ask_first=bool(ask_first),
        by=by,
    )
    if rec is None:
        raise LaneError("could not arm the lane", 500)
    return rec


def set_lane(title: str, lane: str, ask_first: bool = False, **kw) -> Optional[dict]:
    """:func:`arm_session` under the name the ship-lane callers use."""
    return arm_session(title, lane, ask_first=ask_first, **kw)


def ship_now(
    title: str,
    lane: str = "",
    *,
    message: str = "",
    now: Optional[float] = None,
) -> Optional[dict]:
    """Ship what is there NOW: arm ``lane`` (default: the session's own lane)
    with no "ask first", WITHOUT the idle dwell. A session with no lane of its
    own is refused — never shipped at the Settings fast-track default, which is
    nobody's choice for THIS session (a copy window, a held group member). ``message`` (the Outbox approval card's edited commit message) is
    committed as written.

    The dwell exists to tell "the agent finished" from "the agent paused"; a
    human pressing Ship has just answered that question, so making them wait
    another 30 seconds for the driver to re-learn it is pure latency. The
    record is armed under THIS server's lease so the driver's claim does not
    treat its first pass as a takeover (which would throw the dwell away), and
    with the agent counted as having worked (a clean tree then means "nothing
    to ship", said as a halt, instead of "waiting for the agent to start").

    The caller has already refused a session whose agent is mid-turn.
    """
    srv = _server()
    ts = float(now if now is not None else time.time())
    rec = _autopilot.get(title) or {}
    if not str(message or "").strip() and rec.get("message_drafted"):
        # The approval card showed this exact message: approving it unedited
        # commits exactly that text (never a second, different draft).
        message = str(rec.get("message") or "")
    target = (
        normalize_lane(lane)
        or normalize_lane(rec.get("lane"))
        or lane_of_depth(rec.get("depth"))
    )
    if not target:
        raise LaneError(
            "this session has no lane of its own — pick how far it goes first", 409
        )
    if target == "leave":
        raise LaneError("this session's lane ships nothing — pick a lane first")
    # An edited message from the approval card is a human's sentence: it is
    # committed as written (never replaced by a generated one).
    armed = arm_session(
        title,
        target,
        ask_first=False,
        message=str(message or ""),
        source=str(rec.get("source") or "session"),
        item=str(rec.get("item") or ""),
        now=ts,
        by=str(rec.get("by") or "user"),
    )
    return _autopilot.update(
        title,
        idle_since=ts - _autopilot.IDLE_SETTLE_S - 1.0,
        worked_at=ts,
        owner=srv._SERVER_BOOT_ID,
        owner_at=ts,
        note="shipping now",
    ) or (armed or None)
