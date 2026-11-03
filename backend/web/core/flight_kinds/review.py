"""The ``review`` stop: a second opinion (docs/flights.md "Round 15").

``run`` spawns one worker of the flight's session (``spawn_worker``: a child
forked from its HEAD, on ``program`` / ``model`` when given) with a brief to
review ``git diff <base>...HEAD`` for correctness bugs, change nothing and
end with a report whose summary is "No findings" or a numbered list.
``detail.children`` is ``[title]``, as a split records its workers.

``observe`` reads that worker as a join does (``_children_obs``), plus its
full report from the session's mailbox (the row's summary is one capped
line; the list needs every line). Once the worker has finished, and unless
``keep``, it is closed right there — deleted when its worktree is clean and
holds no commits of its own, else closed (Recently closed keeps it) — and
the outcome is remembered for the rest of this server run, so a pass whose
verdict was lost still judges from it.

``judge`` is pure: waiting while the worker works; "No findings" (or an
empty summary) → done; findings → ``detail.findings`` and, with ``block``,
a fail whose reason carries them (``on_fail: agent`` hands them to the
agent; the retry after its fix asks for a fresh second opinion); a worker
that failed or vanished without a report → fail.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Dict, Optional, Tuple

from backend import log
from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as _flights

#: How many findings the brief asks for at most.
MAX_FINDINGS = 10
#: How much of the reviewer's report a step keeps (and a fail reason carries).
FINDINGS_CHARS = 4000

_NO_FINDINGS_RE = re.compile(r"^\W*no findings\b", re.I)
_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+\S")

#: ``(flight, step, since, title) -> obs`` of a reviewer that has finished
#: (and was closed): the worker is gone after that, its outcome is not.
_FINISHED: Dict[Tuple[str, str, float, str], dict] = {}
_FINISHED_MAX = 256
_LOCK = threading.Lock()


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    return {
        "program": _flights._text(c.get("program"), 100),
        "model": _flights._text(c.get("model"), 200),
        "focus": _flights._text(c.get("focus")),
        "block": _flights._bool(c.get("block"), True),
        "keep": _flights._bool(c.get("keep"), False),
    }


def _title(config: dict) -> str:
    program = str(config.get("program") or "").strip()
    return "Second opinion (%s)" % program if program else "Second opinion"


def brief(parent: str, base: str, focus: str = "") -> str:
    """What the reviewer is asked to do."""
    text = (
        "[MindFlock route] You are a second-opinion reviewer for the session "
        '"%s". Review the change on this branch: run `git diff %s...HEAD` '
        "(and `git log %s..HEAD` for context) in this worktree and read the "
        "code around it.\n"
        "Look for correctness bugs only: wrong logic, broken edge cases, "
        "crashes, races, lost data, security holes. Ignore style, naming and "
        "refactoring ideas.\n"
        "Change NO files and make no commits: you only read and report.\n"
        "End with your report. Its summary is exactly `No findings` when you "
        "found nothing; otherwise a numbered list of at most %d, one per "
        "line:\n"
        "1. path/to/file.py:123 — what is wrong and why\n"
    ) % (parent, base, base, MAX_FINDINGS)
    focus = str(focus or "").strip()
    if focus:
        text += "\nAlso: %s\n" % focus
    return text


def _base_of(flight: dict) -> str:
    """The ref the session's change is measured against (blocking): its
    recorded base branch, else the repo's default branch."""
    from backend.web.core import flight_driver as _driver

    inst = _driver._server().ENGINE.instances.get(flight["session"])
    return str(getattr(inst, "BaseBranch", "") or "") or "origin/HEAD"


async def _run(flight: dict, step: dict, action: dict, now: float) -> dict:
    from backend.web.core import flight_driver as _driver

    cfg = step["config"]
    base = await asyncio.to_thread(_base_of, flight)
    title = await _driver.spawn_worker(
        flight,
        step,
        brief(flight["session"], base, cfg.get("focus") or ""),
        model=cfg.get("model") or "",
        program=cfg.get("program") or "",
        report_back=True,
        title_prefix="review",
    )
    return dict(
        action,
        note="asking %s for a second opinion" % title,
        detail={"children": [title], "base": base},
    )


# --------------------------------------------------------------------------- #
# Observing the reviewer
# --------------------------------------------------------------------------- #
def _report_message(parent: str, title: str, since: float) -> Optional[dict]:
    from backend.web.core import mailbox as _mailbox

    return _mailbox.last_result(parent, title, since=since)


def _clean_text(msg: Optional[dict]) -> str:
    """The report's text (summary + details), line by line made safe to type
    into the agent, capped."""
    from backend.web.core import mailbox as _mailbox

    raw = str((msg or {}).get("text") or "")
    lines = [_mailbox.sanitize(ln) for ln in raw.splitlines()]
    text = "\n".join(ln for ln in lines if ln).strip()
    if len(text) > FINDINGS_CHARS:
        text = text[: FINDINGS_CHARS - 1].rstrip() + "…"
    return text


def _close(parent: str, title: str) -> str:
    """Close the reviewer ``kill_session``'s way (blocking): ``deleted`` (its
    worktree and branch removed) when the worktree is clean and holds no
    commits the session's HEAD lacks, else ``closed`` (kept in Recently
    closed); "" when that failed or it was already gone."""
    from backend.mcp import gitlocal as _gitlocal
    from backend.web.core import flight_driver as _driver
    from backend.web.core import session_end as _session_end

    engine = _driver._server().ENGINE
    inst = engine.instances.get(title)
    if inst is None:
        return ""
    wipe = False
    try:
        folder = inst.GetWorktreePath() or ""
        mine = engine.instances.get(parent)
        ref = _gitlocal.head_sha(mine.GetWorktreePath() or "") if mine else None
        if (
            folder
            and ref
            and not getattr(inst, "InPlace", False)
            and _gitlocal.is_dirty(folder) is False
        ):
            wipe = _gitlocal.commits_between(folder, ref, "HEAD") == []
    except Exception:  # noqa: BLE001 — not inspectable: keep its worktree
        wipe = False
    try:
        _session_end.end_session(engine, title, wipe=wipe)
    except Exception as err:  # noqa: BLE001 — FolderInUse, gone, a kill error
        log.ErrorLog.Printf("flight review: closing %s: %v", title, err)
        return ""
    return "deleted" if wipe else "closed"


def _facts(flight: dict, step: dict, title: str) -> dict:
    """The reviewer's join facts (blocking), ``present: False`` when gone."""
    from backend.web.core import flight_driver as _driver

    for c in _driver._children_obs(flight, step):
        if c.get("title") == title:
            return c
    return {"title": title, "present": False}


def _entry(facts: dict, msg: Optional[dict], prev, since: float, now: float) -> dict:
    """The reviewer's join entry; a gone worker is judged by the report it
    left in the session's mailbox, if any."""
    from backend.web.core import thread as _thread

    if facts.get("present"):
        return _flights.child_state(facts, prev, since, now)
    out: dict = {"title": str(facts.get("title") or ""), "state": "gone"}
    rep = _thread.report_json(msg)
    if rep is not None and rep["ts"] > since:
        out["report"] = rep
        if rep["status"] == "failed":
            out["state"] = "failed"
        elif rep["status"] != "blocked":
            out["state"] = "done"
    return out


async def _observe(flight: dict, step: dict, ss: dict, sess: dict, now: float):
    kids = ss["detail"].get("children") or []
    if not kids or not flight["session"]:
        return None
    title = str(kids[0])
    since = float(ss["since"] or 0.0)
    key = (flight["id"], step["id"], since, title)
    with _LOCK:
        hit = _FINISHED.get(key)
    if hit is not None:
        return dict(hit)
    facts = await asyncio.to_thread(_facts, flight, step, title)
    msg = await asyncio.to_thread(_report_message, flight["session"], title, since)
    entry = _entry(facts, msg, ss["detail"].get("reviewer"), since, now)
    out = {"entry": entry, "text": _clean_text(msg), "report": msg is not None}
    if entry["state"] not in ("done", "failed"):
        return out
    if not step["config"].get("keep") and facts.get("present"):
        out["closed"] = await asyncio.to_thread(_close, flight["session"], title)
    with _LOCK:
        if len(_FINISHED) >= _FINISHED_MAX:
            _FINISHED.pop(next(iter(_FINISHED)))
        _FINISHED[key] = dict(out)
    return out


# --------------------------------------------------------------------------- #
# Judging
# --------------------------------------------------------------------------- #
def parse_findings(text: str) -> Tuple[int, str]:
    """``(count, text)`` of a reviewer's report: ``(0, "")`` for "No
    findings" or nothing at all; else the numbered items (a report that is
    not a list counts as one finding)."""
    text = str(text or "").strip()
    head = text.split("\nDetails:", 1)[0].strip()
    if not head or _NO_FINDINGS_RE.match(head):
        return 0, ""
    items = [ln for ln in head.splitlines() if _ITEM_RE.match(ln)]
    if not items:
        items = [ln for ln in text.splitlines() if _ITEM_RE.match(ln)]
    return max(1, len(items)), text


def _plural(n: int) -> str:
    return "%d finding%s" % (n, "" if n == 1 else "s")


def _judge(flight, step, ss, obs, now) -> dict:
    kids = ss["detail"].get("children") or []
    if not kids:
        return {"action": "run", "note": "asking for a second opinion"}
    o = obs.get("review")
    if not isinstance(o, dict):
        return _flights._wait("second opinion in progress")
    if o.get("failed"):
        return {"action": "fail", "reason": str(o["failed"])}
    entry = o.get("entry") or {}
    state = entry.get("state")
    who = str(entry.get("title") or kids[0])
    if state == "asking":
        return _flights._wait(
            "second opinion in progress: %s is asking something" % who,
            reviewer=entry,
        )
    if state not in ("done", "failed", "gone"):
        return _flights._wait("second opinion in progress (%s)" % who, reviewer=entry)
    closed = {"closed": o["closed"]} if o.get("closed") else {}
    if state != "done":
        return {
            "action": "fail",
            "reason": "the reviewer did not finish",
            "detail": dict(closed, reviewer=entry),
        }
    if not o.get("report"):
        return {
            "action": "done",
            "note": "%s finished without a report" % who,
            "detail": dict(closed, findings="", receipt="no report"),
        }
    n, text = parse_findings(o.get("text") or "")
    if not n:
        return {
            "action": "done",
            "note": "no findings",
            "detail": dict(closed, findings="", receipt="no findings"),
        }
    detail = dict(closed, findings=text, receipt=_plural(n))
    if step["config"].get("block", True):
        return {
            "action": "fail",
            "reason": "the second opinion has %s:\n%s" % (_plural(n), text),
            "detail": detail,
        }
    return {"action": "done", "note": _plural(n), "detail": detail}


KIND = _kinds.register(
    _kinds.Kind(
        kind="review",
        spec={
            "label": "Review",
            "actor": "robot",
            "family": "work",
            "summary": (
                "A second agent reviews the diff for correctness bugs; its "
                "findings can fail the stop and go to the agent."
            ),
            "default_title": "Second opinion",
            "fields": [
                _flights._f(
                    "program",
                    "text",
                    "Reviewer's CLI (empty = the session's)",
                    default="",
                    placeholder="claude, codex, …",
                ),
                _flights._f(
                    "model", "text", "Model (empty = the CLI's default)", default=""
                ),
                _flights._f(
                    "focus",
                    "textarea",
                    "Also look at",
                    default="",
                    placeholder="e.g. the migration's rollback path",
                ),
                _flights._f("block", "bool", "Findings fail the stop", default=True),
                _flights._f(
                    "keep", "bool", "Keep the reviewer session after", default=False
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        default_title=_title,
    )
)
