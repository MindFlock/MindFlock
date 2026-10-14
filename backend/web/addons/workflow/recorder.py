"""Workflow Optimizer — the recorder.

MindFlock keeps no history of where a session's time went: events live in a
100-entry in-memory buffer and the activity / stage clocks reset on restart.
So this records its own timeline (:mod:`.store`):

* :func:`loop` reads the published session rows (``/api/instances``, in
  memory — cheap) every :data:`TICK_S`, classifies each into a phase
  (:func:`phase_of`) and appends a row only when the phase, the "someone is
  at the keyboard" bit or the session's identity (:func:`meta_of`) changed,
  plus one heartbeat per :data:`HEARTBEAT_S` so server downtime shows up as
  a gap instead of as the last phase stretched over it.
* :meth:`Recorder.on_event` takes the bus's own word for the moments a
  sample would blur or miss: ``stage_changed`` / ``activity_changed`` (the
  exact second pre-commit hooks start), ``pr_state_changed`` → MERGED (no row
  field says "merged" — after a merge the stage just falls back),
  ``session.created`` (its ticket id) and ``session.deleted``.

Remote devices' rows (``<device>::<title>``) are skipped: each device
records its own sessions.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Dict, Iterable, List, Optional

from . import store

__all__ = ["phase_of", "meta_of", "Recorder", "RECORDER", "loop", "ATTENDED_S"]

#: Seconds between samples of the session rows.
TICK_S = 5.0
#: One "still alive" row this often (the analysis treats any row as proof of
#: life for :data:`~.analysis.ALIVE_S` after it).
HEARTBEAT_S = 300.0
#: A keystroke into any MindFlock window within this counts as "you're here".
ATTENDED_S = 600.0

# Ticket ids in titles / branches: Shortcut "sc-123", Jira/Linear "ENG-42",
# GitHub issues "issue-17" / "gh-17".
_TICKET_RES = (
    re.compile(r"\b(sc-\d+)\b", re.I),
    re.compile(r"\b((?:issue|gh)-\d+)\b", re.I),
    re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)\b"),
)


def phase_of(row: dict) -> str:
    """The phase a session row is in (see :data:`~.analysis.PHASES`).

    Precedence is "what is holding the work right now": a paused session is
    paused; setup running is setup; running hooks beat the agent's own state
    (the commit is what's happening); a question or a usage limit beats the
    stage; a failed commit is recovery until it's retried; a working agent is
    working (``rework`` once a PR is open); otherwise the stage says which
    handoff the work is waiting at, and an idle agent with nothing shipped is
    "your turn"."""
    status = str(row.get("status") or "").lower()
    stage = str(row.get("stage") or "")
    act = str(row.get("activity") or "")
    setup = row.get("setup")
    if status == "paused":
        return "paused"
    if (
        stage == "provisioning"
        or status == "loading"
        or (isinstance(setup, dict) and setup.get("state") == "running")
    ):
        return "setup"
    if stage == "precommit":
        return "precommit"
    if act == "clarify":
        return "question"
    if act == "limit":
        return "limit"
    if stage == "interrupt":
        return "hook_fix"
    if act == "working":
        return "rework" if stage == "pr" else "agent"
    if act == "offline":
        return "offline"
    if stage == "pr":
        return "review"
    if stage == "pushed":
        return "to_pr"
    if stage == "committed":
        return "to_push"
    return "your_turn"


def ticket_of(*texts: str) -> str:
    for text in texts:
        for rx in _TICKET_RES:
            m = rx.search(text or "")
            if m:
                return m.group(1)
    return ""


def meta_of(row: dict, ticket: str = "") -> dict:
    """What a session is, for grouping and labels."""
    title = str(row.get("title") or "")
    branch = str(row.get("branch") or "")
    ap = row.get("autopilot") if isinstance(row.get("autopilot"), dict) else {}
    if not ticket and ap.get("source") in ("tix", "iss", "pr"):
        ticket = str(ap.get("item") or "")
    ticket = ticket or ticket_of(branch, title)
    name = (
        str(ap.get("message") or "") if ap.get("source") in ("tix", "iss", "pr") else ""
    )
    return {
        "repo": str(row.get("repo") or ""),
        "branch": branch,
        "ticket": ticket,
        "name": name or str(row.get("name") or "") or title,
        "pr": str(row.get("pr_url") or ""),
        "created": row.get("created_at"),
    }


def attended(now: Optional[float] = None) -> bool:
    """Whether a person typed into any MindFlock window within
    :data:`ATTENDED_S` (keystrokes that bypass MindFlock are invisible)."""
    now = time.time() if now is None else now
    try:
        from backend.web import server

        stamps = server._HUMAN_INPUT_AT
        latest = max(stamps.values()) if stamps else 0.0
    except Exception:  # noqa: BLE001
        return False
    return now - latest < ATTENDED_S


def _local(title: str) -> bool:
    return bool(title) and "::" not in title


class Recorder:
    """The in-memory "last written" state, so only changes are appended.
    Thread-safe: the loop and bus callbacks (any thread) share it."""

    def __init__(self, write=None) -> None:
        self._lock = threading.Lock()
        self._write = write or store.append
        self._phase: Dict[str, tuple] = {}  # title -> (phase, attended)
        self._meta: Dict[str, dict] = {}  # title -> last meta written
        self._rows: Dict[str, dict] = {}  # title -> last row seen
        self._tickets: Dict[str, str] = {}  # title -> ticket id from session.created
        self._hb_at = 0.0

    # ---- sampling -------------------------------------------------------- #
    def observe(
        self,
        rows: Iterable[dict],
        now: Optional[float] = None,
        att: Optional[bool] = None,
    ) -> List[dict]:
        """One pass over the published rows; returns (and writes) the rows
        appended."""
        now = time.time() if now is None else now
        att = attended(now) if att is None else att
        out: List[dict] = []
        seen = set()
        with self._lock:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or "")
                if not _local(title):
                    continue
                seen.add(title)
                self._rows[title] = row
                out += self._diff_locked(title, row, now, att)
            # A session that vanished from a non-empty list without a delete
            # event (removed elsewhere, tombstone convergence) has ended.
            if seen:
                for title in list(self._phase):
                    if title not in seen:
                        out.append({"t": now, "k": "end", "s": title})
                        self._forget_locked(title)
            if out or now - self._hb_at >= HEARTBEAT_S:
                if now - self._hb_at >= HEARTBEAT_S:
                    out.append({"t": now, "k": "hb"})
                self._hb_at = now
        self._write(out)
        return out

    def _diff_locked(self, title: str, row: dict, now: float, att: bool) -> List[dict]:
        out: List[dict] = []
        meta = meta_of(row, self._tickets.get(title, ""))
        old = self._meta.get(title)
        if old is None or any(
            old.get(k) != meta.get(k)
            for k in ("repo", "branch", "ticket", "name", "pr")
        ):
            out.append({"t": now, "k": "meta", "s": title, **meta})
            self._meta[title] = meta
        ph = phase_of(row)
        # The keyboard bit only matters while it's the person's move.
        key = (ph, bool(att) if ph == "your_turn" else False)
        if self._phase.get(title) != key:
            out.append({"t": now, "k": "ph", "s": title, "ph": ph, "a": key[1]})
            self._phase[title] = key
        return out

    def _forget_locked(self, title: str) -> None:
        for d in (self._phase, self._meta, self._rows, self._tickets):
            d.pop(title, None)

    # ---- the bus --------------------------------------------------------- #
    def on_event(self, env: dict, now: Optional[float] = None) -> List[dict]:
        """A bus envelope (any thread). Never raises."""
        try:
            return self._on_event(env, now)
        except Exception:  # noqa: BLE001 — a recorder bug must not break the bus
            return []

    def _on_event(self, env: dict, now: Optional[float]) -> List[dict]:
        ev = str(env.get("event") or "")
        title = str(env.get("session") or "")
        if not _local(title):
            return []
        now = float(env.get("ts") or 0) or (time.time() if now is None else now)
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        out: List[dict] = []
        with self._lock:
            if ev == "session.created":
                tid = data.get("ticket") or data.get("issue") or data.get("pr")
                if tid:
                    self._tickets[title] = str(tid)
                return []
            if ev == "session.deleted":
                if title in self._phase or title in self._meta:
                    out.append({"t": now, "k": "end", "s": title})
                self._forget_locked(title)
            elif (
                ev == "session.pr_state_changed"
                and str(env.get("new") or "").upper() == "MERGED"
            ):
                out.append(
                    {
                        "t": now,
                        "k": "merge",
                        "s": title,
                        "pr": str(data.get("url") or ""),
                    }
                )
            elif ev in ("session.stage_changed", "session.activity_changed"):
                row = self._rows.get(title)
                if row is None:
                    return []
                field = "stage" if ev == "session.stage_changed" else "activity"
                row = {**row, field: env.get("new")}
                self._rows[title] = row
                prev = self._phase.get(title)
                att = prev[1] if prev else False
                if phase_of(row) == "your_turn":
                    att = attended(now)
                out += self._diff_locked(title, row, now, att)
        self._write(out)
        return out


RECORDER = Recorder()


def _published_rows() -> List[dict]:
    from backend.web.core import events

    return list(events.sessions_snapshot() or [])


async def loop() -> None:
    """Forever: sample every :data:`TICK_S` (the addon's startup registers it;
    off under pytest). One bad tick never stops it."""
    while True:
        try:
            rows = _published_rows()
            await asyncio.to_thread(RECORDER.observe, rows)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            try:
                from backend import log

                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("workflow recorder tick failed: %v", err)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(TICK_S)
