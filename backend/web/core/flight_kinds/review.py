"""The ``review`` stop: a second opinion (docs/flights.md "Round 15").

``run`` spawns one worker of the flight's session (``spawn_worker``: a child
forked from its HEAD, on ``program`` / ``model`` when given) with a brief to
review ``git diff <base>...HEAD``, change nothing and end with a report whose
summary is "No findings" or a numbered list. ``mode`` picks the brief:
``bugs`` (correctness bugs; ``focus`` is an "Also:" line), ``security`` (a
security checklist) or ``standards`` (the team's rules, given in ``focus``);
in the last two ``focus`` is the governing rules and leads the brief.
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
agent — :func:`fix_instructions` makes it fix or rebut each one in writing,
for the PR's description; the retry after its fix asks for a fresh second
opinion); a worker that failed or vanished without a report → fail.
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
#: ``focus`` holds a whole rules document in the security / standards modes.
FOCUS_CHARS = _flights.MAX_TEXT

#: What the reviewer looks for: the brief, the stop's title and its words.
MODES = ("bugs", "security", "standards")
MODE_TITLES = {
    "bugs": "Second opinion",
    "security": "Security review",
    "standards": "Standards review",
}
MODE_LABELS = {
    "bugs": "Correctness bugs",
    "security": "Security",
    "standards": "The team's standards",
}
#: How the stop's notes and fail reason name the review.
_NOUNS = {
    "bugs": "second opinion",
    "security": "security review",
    "standards": "standards review",
}

_NO_FINDINGS_RE = re.compile(r"^\W*no findings\b", re.I)
_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+\S")

#: ``(flight, step, since, title) -> obs`` of a reviewer that has finished
#: (and was closed): the worker is gone after that, its outcome is not.
_FINISHED: Dict[Tuple[str, str, float, str], dict] = {}
_FINISHED_MAX = 256
_LOCK = threading.Lock()


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    mode = _flights._text(c.get("mode"), 16).lower() or "bugs"
    if mode not in MODES:
        if strict:
            raise _flights.PlanError(
                "mode is one of %s" % ", ".join(MODES), where + ".mode"
            )
        mode = "bugs"
    return {
        "mode": mode,
        "program": _flights._text(c.get("program"), 100),
        "model": _flights._text(c.get("model"), 200),
        "focus": _flights._text(c.get("focus"), FOCUS_CHARS),
        "block": _flights._bool(c.get("block"), True),
        "keep": _flights._bool(c.get("keep"), False),
    }


def _mode(config: dict) -> str:
    mode = str((config or {}).get("mode") or "bugs")
    return mode if mode in MODES else "bugs"


def _title(config: dict) -> str:
    name = MODE_TITLES[_mode(config)]
    program = str(config.get("program") or "").strip()
    return "%s (%s)" % (name, program) if program else name


#: The ``security`` brief's checklist.
SECURITY_CHECKLIST = (
    "authentication and authorization: missing or bypassable checks, "
    "privilege escalation, broken session handling",
    "secrets and credentials: hard-coded, committed, logged or leaked keys, "
    "tokens and passwords",
    "injection: SQL, shell / command, template",
    "SSRF: server-side requests to a URL or host an attacker controls",
    "path traversal: file paths built from untrusted input",
    "crypto misuse: weak or home-made algorithms, bad randomness, missing "
    "signature or certificate verification",
    "IAM, infrastructure-as-code and RBAC: over-broad permissions, public "
    "resources, roles granted too widely",
    "unsafe deserialization of untrusted data",
    "sensitive data (credentials, personal data) in logs",
    "dependency risk: new or upgraded packages that are vulnerable, "
    "unmaintained or untrusted",
)

#: The ``standards`` brief's checklist (the rules may demand more).
STANDARDS_CHECKLIST = (
    "code quality: clarity, structure, duplication, error handling, naming "
    "and style where the rules set them",
    "observability: meaningful logging, metrics, and dashboards / alerts for "
    "what the change adds",
    "testing: unit tests for the new behavior, and evidence it was tested "
    "locally (in the PR description, the commits or test output)",
    "anything else the rules demand",
)


#: How a security / standards reviewer treats a rebuttal of an earlier
#: finding (:func:`fix_instructions` has the agent commit them).
REBUTTALS = (
    "The commit messages may carry the author's written rebuttals of earlier "
    "findings: do not report one again when its rebuttal answers it.\n"
)


def _report_rules(what: str) -> str:
    """The brief's tail: read-only, and the report ``judge`` parses."""
    return REBUTTALS + (
        "Change NO files and make no commits: you only read and report.\n"
        "End with your report. Its summary is exactly `No findings` when you "
        "found nothing; otherwise a numbered list of at most %d, one per "
        "line:\n"
        "1. path/to/file.py:123 — %s\n"
    ) % (MAX_FINDINGS, what)


def _rules_block(focus: str) -> str:
    """The team's rules, set apart at the head of the brief."""
    return (
        "The team's rules for this review (they govern it):\n"
        "----- rules -----\n%s\n----- end of rules -----\n" % focus
    )


def brief(parent: str, base: str, focus: str = "", mode: str = "bugs") -> str:
    """What the reviewer is asked to do (``mode``: see :data:`MODES`)."""
    focus = str(focus or "").strip()
    if mode == "security":
        return _security_brief(parent, base, focus)
    if mode == "standards":
        return _standards_brief(parent, base, focus)
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
    if focus:
        text += "\nAlso: %s\n" % focus
    return text


def _opening(role: str, parent: str, base: str) -> str:
    return (
        "[MindFlock route] You are a %s for the session "
        '"%s". Review the change on this branch: run `git diff %s...HEAD` '
        "(and `git log %s..HEAD` for context) in this worktree and read the "
        "code around it.\n"
    ) % (role, parent, base, base)


def _security_brief(parent: str, base: str, focus: str) -> str:
    text = _opening("security reviewer", parent, base)
    if focus:
        text += "\n" + _rules_block(focus) + "\n"
    text += "Look for security vulnerabilities the change introduces or exposes"
    text += " — the rules above first, then this checklist:\n" if focus else ":\n"
    text += "".join("- %s\n" % item for item in SECURITY_CHECKLIST)
    text += (
        "Report only real, exploitable issues — each with its file:line, how "
        "it can be exploited and what to change. No theoretical hardening, no "
        "style.\n"
    )
    return text + _report_rules("the issue, how it is exploited, the fix")


def _standards_brief(parent: str, base: str, focus: str) -> str:
    text = _opening("standards reviewer", parent, base)
    if focus:
        text += "\n" + _rules_block(focus) + "\n"
        text += "Check the change against every one of those rules, including:\n"
    else:
        text += (
            "No team rules were given: review against sound engineering "
            "standards, including:\n"
        )
    text += "".join("- %s\n" % item for item in STANDARDS_CHECKLIST)
    text += (
        "Each place the change falls short of a rule is a finding: give its "
        "file:line (or the PR / commit when evidence is missing) and the rule "
        "it breaks.\n"
    )
    return text + _report_rules("which rule it breaks and what is missing")


def fix_instructions(step: dict, reason: str) -> str:
    """What the agent is told, in place of "fix it", when a review stop hands
    it findings (``on_fail: agent``): "" for any other fail."""
    mode = _mode((step or {}).get("config") or {})
    if not re.match(r"the %s has \d+ finding" % _NOUNS[mode], str(reason or "")):
        return ""
    return (
        "Each finding must be FIXED, or answered with a written rebuttal (why "
        "the code is right as it is); leave none unanswered. Put each "
        "rebuttal in a commit message (an empty commit will do), where the "
        "next review reads it. Record every fix and rebuttal, numbered as "
        "above, for the PR's description. Once each one is fixed or "
        "rebutted, the route asks for a fresh review."
    )


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
    mode = _mode(cfg)
    title = await _driver.spawn_worker(
        flight,
        step,
        brief(flight["session"], base, cfg.get("focus") or "", mode),
        model=cfg.get("model") or "",
        program=cfg.get("program") or "",
        report_back=True,
        title_prefix="review",
    )
    return dict(
        action,
        note="asking %s for a %s" % (title, _NOUNS[mode]),
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
    noun = _NOUNS[_mode(step["config"])]
    kids = ss["detail"].get("children") or []
    if not kids:
        return {"action": "run", "note": "asking for a %s" % noun}
    o = obs.get("review")
    if not isinstance(o, dict):
        return _flights._wait("%s in progress" % noun)
    if o.get("failed"):
        return {"action": "fail", "reason": str(o["failed"])}
    entry = o.get("entry") or {}
    state = entry.get("state")
    who = str(entry.get("title") or kids[0])
    if state == "asking":
        return _flights._wait(
            "%s in progress: %s is asking something" % (noun, who),
            reviewer=entry,
        )
    if state not in ("done", "failed", "gone"):
        return _flights._wait("%s in progress (%s)" % (noun, who), reviewer=entry)
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
            "reason": "the %s has %s:\n%s" % (noun, _plural(n), text),
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
                "A second agent reviews the diff for correctness bugs, "
                "security holes or the team's standards; its findings can "
                "fail the stop and go to the agent to fix or rebut."
            ),
            "default_title": "Second opinion",
            "fields": [
                _flights._f(
                    "mode",
                    "select",
                    "Review for",
                    default="bugs",
                    options=MODES,
                    option_labels=MODE_LABELS,
                ),
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
                    "Also look at (security / standards: the team's rules)",
                    default="",
                    placeholder="e.g. the migration's rollback path — or paste "
                    "the team's review rules",
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
