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
* Each sample also reads every session's ``HEAD`` straight from its
  ``.git`` files (no git process): the first sight of a branch writes a
  ``head`` row, and when HEAD moves one ``git log`` counts the commits that
  were authored since the old HEAD was seen (``commit`` row, ``n`` of them,
  ``merge`` when the new HEAD has two parents). A pull, a rebase, a
  cherry-pick or an amend keeps the old author time, so it brings ``n`` 0:
  only a commit written just now finishes a unit of work.
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
import os
import re
import subprocess
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

from . import store

__all__ = ["phase_of", "meta_of", "Recorder", "RECORDER", "loop", "ATTENDED_S"]

#: Seconds between samples of the session rows.
TICK_S = 5.0
#: One "still alive" row this often (the analysis treats any row as proof of
#: life for :data:`~.analysis.ALIVE_S` after it).
HEARTBEAT_S = 300.0
#: A keystroke into any MindFlock window within this counts as "you're here".
ATTENDED_S = 600.0
#: Phases where the "someone is at the keyboard" bit is recorded: the agent
#: is idle, so whose move it is depends on whether a person is around.
_IDLE_PHASES = frozenset({"your_turn", "to_push", "to_pr", "review"})
#: A commit authored this long before its old HEAD was first seen still
#: counts as new (the sample is up to TICK_S late; clocks drift a little).
_AUTHOR_SLACK_S = 120.0
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

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


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_head(wt: str) -> str:
    """The commit ``HEAD`` points at in worktree ``wt``, read from the
    ``.git`` files (a linked worktree's ``gitdir:`` file, its ``commondir``,
    loose refs, then ``packed-refs``); ``""`` when unreadable. Cheap enough
    for every sample — never runs git."""
    if not wt:
        return ""
    dotgit = os.path.join(wt, ".git")
    if os.path.isdir(dotgit):
        gitdir = dotgit
    else:
        line = _read(dotgit)
        if not line.startswith("gitdir:"):
            return ""
        gitdir = os.path.normpath(os.path.join(wt, line[len("gitdir:") :].strip()))
    head = _read(os.path.join(gitdir, "HEAD"))
    if not head.startswith("ref:"):
        return head if _SHA_RE.match(head) else ""
    ref = head[len("ref:") :].strip()
    common = _read(os.path.join(gitdir, "commondir"))
    common = os.path.normpath(os.path.join(gitdir, common)) if common else gitdir
    for base in (gitdir, common):
        sha = _read(os.path.join(base, ref))
        if _SHA_RE.match(sha):
            return sha
    for line in _read(os.path.join(common, "packed-refs")).splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref and _SHA_RE.match(parts[0]):
            return parts[0]
    return ""


def new_commits(wt: str, old: str, new: str, since: float) -> Tuple[int, bool]:
    """``(n, merge)`` for HEAD moving ``old`` → ``new`` in ``wt``: how many of
    the commits reachable from ``new`` but not ``old`` are single-parent and
    were authored at or after ``since``, and whether ``new`` itself is a
    merge commit. A failure answers ``(0, False)`` — no unit is closed on a
    guess."""
    log = ["git", "-C", wt, "log", "--format=%H %P|%at"]
    # The old HEAD may be gone (gc'd after a rewrite): then the new tip alone.
    attempts = [log + ["-n", "200", new] + (["^" + old] if old else [])]
    if old:
        attempts.append(log + ["-n", "1", new])
    out = ""
    for attempt in attempts:
        try:
            cp = subprocess.run(
                attempt,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 0, False
        if cp.returncode == 0:
            out = cp.stdout.decode("utf-8", "replace")
            break
    n = 0
    merge = False
    for i, line in enumerate(out.splitlines()):
        shas, _, at = line.partition("|")
        parents = len(shas.split()) - 1
        if i == 0 and shas.split()[:1] == [new]:
            merge = parents >= 2
        try:
            authored = float(at)
        except ValueError:
            continue
        if parents <= 1 and authored >= since:
            n += 1
    return n, merge


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
        # title -> {"branch", "sha", "seen"}: the HEAD last written, and when
        # that sha was first seen (the author-time floor for its successors).
        self._head: Dict[str, dict] = {}
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
        # HEAD reads are file reads, outside the lock; the git log for a moved
        # HEAD runs after it (the bus callbacks share the lock).
        heads = {
            str(r.get("title") or ""): read_head(str(r.get("path") or ""))
            for r in rows or []
            if isinstance(r, dict) and _local(str(r.get("title") or ""))
        }
        moved: List[tuple] = []
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
                out += self._head_locked(title, row, heads.get(title, ""), now, moved)
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
        for title, wt, old, new, since in moved:
            n, merge = new_commits(wt, old, new, since)
            out.append(
                {
                    "t": now,
                    "k": "commit",
                    "s": title,
                    "sha": new,
                    "n": n,
                    "merge": merge,
                }
            )
        self._write(out)
        return out

    def _head_locked(
        self, title: str, row: dict, sha: str, now: float, moved: List[tuple]
    ) -> List[dict]:
        """A ``head`` row the first time a branch's HEAD is seen; a moved HEAD
        on the same branch is queued in ``moved`` for :func:`new_commits`."""
        if not sha:
            return []
        branch = str(row.get("branch") or "")
        old = self._head.get(title)
        if old is not None and old["sha"] == sha and old["branch"] == branch:
            return []
        self._head[title] = {"branch": branch, "sha": sha, "seen": now}
        if old is None or old["branch"] != branch:
            return [{"t": now, "k": "head", "s": title, "sha": sha}]
        since = old["seen"] - _AUTHOR_SLACK_S
        moved.append((title, str(row.get("path") or ""), old["sha"], sha, since))
        return []

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
        # The keyboard bit only matters while the agent is idle.
        key = (ph, bool(att) if ph in _IDLE_PHASES else False)
        if self._phase.get(title) != key:
            out.append({"t": now, "k": "ph", "s": title, "ph": ph, "a": key[1]})
            self._phase[title] = key
        return out

    def _forget_locked(self, title: str) -> None:
        for d in (self._phase, self._meta, self._rows, self._tickets, self._head):
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
                if phase_of(row) in _IDLE_PHASES:
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
