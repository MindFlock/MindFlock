"""The ``review`` stop: a second opinion (docs/flights.md "Round 15").

``run`` spawns one worker of the flight's session (``spawn_worker``: a child
forked from its HEAD, on ``program`` / ``model`` when given) with a brief to
review ``git diff <base>...HEAD``, change nothing and end with a report whose
summary is "No findings" or a numbered list of what should block the merge,
most serious first. ``mode`` picks the brief: ``bugs`` (correctness bugs;
``focus`` is an "Also:" line), ``security`` (a security checklist) or
``standards`` (the team's rules, given in ``focus``); in the last two
``focus`` is the governing rules and leads the brief. ``detail.children`` is
``[title]``, as a split records its workers; ``detail.reviewed_sha`` the
session's HEAD the reviewer was asked to read.

Rounds converge. Findings record ``detail.followup`` (the findings verbatim,
the sha they were found at, the round). The stop's next ask in the same visit
— the retry after the agent's fix, a person's Retry — is a FOLLOW-UP: the
reviewer checks each earlier finding is fixed or rebutted in the commit
messages, reads only ``git diff <sha>..HEAD`` for bugs the fixes introduced,
and leaves code an earlier round read alone unless it is severe. A missing
sha, or one that is no longer an ancestor of HEAD (a rebase), falls back to a
full review. A new flight or a jump back starts fresh.

``observe`` reads that worker as a join does (``_children_obs``), plus its
full report from the session's mailbox (the row's summary is one capped
line; the list needs every line) — or, when it finished without one, its
last reply (what ``read_output`` view ``last_reply`` reads). Once the worker
has finished, and unless ``keep``, it is closed right there — deleted when
its worktree is clean and holds no commits of its own, else closed (Recently
closed keeps it) — and the outcome is remembered for the rest of this server
run, so a pass whose verdict was lost still judges from it.

``judge`` is pure: waiting while the worker works; "No findings" → done;
findings → ``detail.findings`` and, with ``block``, a fail whose reason
carries them (``on_fail: agent`` hands them to the agent —
:func:`fix_instructions` makes it fix or rebut each one in writing, for the
PR's description) for up to ``rounds`` hand-offs; findings still open after
the last one either finish the stop (``unresolved: continue``, recorded) or
park it on the person (``ask``: Done carries on, Retry gives the agent
another set of rounds). A reviewer that gives no verdict (failed, vanished,
ended with neither a report nor a readable last reply, could not start) is
not the agent's to fix: another reviewer is asked, up to
:data:`MAX_REASKS` times per visit, then the stop halts for the person.
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

#: How many times findings go back to the agent (``rounds``), by default
#: and at most.
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10
#: What findings still open after the last round do (``unresolved``).
UNRESOLVED = ("ask", "continue")
UNRESOLVED_LABELS = {
    "ask": "Ask me",
    "continue": "Carry on (keep them on record)",
}
#: How many more reviewers a stop asks, per visit, when one gives no verdict
#: (the driver's respawn of one that could not start counts too).
MAX_REASKS = 2
#: How much of a reviewer's last reply is read when it left no report.
REPLY_CHARS = 12000
#: ``detail.ask`` of a stop parked on the person with findings still open
#: after its last round (Done carries on, Retry gives more rounds).
ASK_UNRESOLVED = "unresolved"

_NO_FINDINGS_RE = re.compile(r"^\W*no findings\b", re.I)
_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+\S")
_ITEM_NO_RE = re.compile(r"^\s*(\d+)[.)]")

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
    unresolved = _flights._text(c.get("unresolved"), 16).lower() or "ask"
    if unresolved not in UNRESOLVED:
        if strict:
            raise _flights.PlanError(
                "unresolved is one of %s" % ", ".join(UNRESOLVED),
                where + ".unresolved",
            )
        unresolved = "ask"
    return {
        "mode": mode,
        "program": _flights._text(c.get("program"), 100),
        "model": _flights._text(c.get("model"), 200),
        "focus": _flights._text(c.get("focus"), FOCUS_CHARS),
        "block": _flights._bool(c.get("block"), True),
        "keep": _flights._bool(c.get("keep"), False),
        "rounds": _flights._int(c.get("rounds"), DEFAULT_ROUNDS, 1, MAX_ROUNDS),
        "unresolved": unresolved,
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


def _report_format(what: str) -> str:
    """Read-only, and the report ``judge`` parses: only what should block
    the merge, most serious first."""
    return (
        "Change NO files and make no commits: you only read and report.\n"
        "Report only findings that should block the merge, most serious "
        "first.\n"
        "End with your report. Its summary is exactly `No findings` when you "
        "found nothing; otherwise a numbered list of at most %d, most serious "
        "first, one per line:\n"
        "1. path/to/file.py:123 — %s\n"
    ) % (MAX_FINDINGS, what)


def _report_rules(what: str) -> str:
    """The brief's tail for the security / standards modes."""
    return REBUTTALS + _report_format(what)


def _rules_block(focus: str) -> str:
    """The team's rules, set apart at the head of the brief."""
    return (
        "The team's rules for this review (they govern it):\n"
        "----- rules -----\n%s\n----- end of rules -----\n" % focus
    )


#: Each mode's reviewer, what it looks for, and what a finding says.
_ROLES = {
    "bugs": "second-opinion reviewer",
    "security": "security reviewer",
    "standards": "standards reviewer",
}
_LOOK_FOR = {
    "bugs": "correctness bugs (wrong logic, broken edge cases, crashes, races, "
    "lost data, security holes)",
    "security": "security vulnerabilities",
    "standards": "breaches of the team's rules",
}
_WHAT = {
    "bugs": "what is wrong and why",
    "security": "the issue, how it is exploited, the fix",
    "standards": "which rule it breaks and what is missing",
}


def brief(parent: str, base: str, focus: str = "", mode: str = "bugs") -> str:
    """What the reviewer is asked to do (``mode``: see :data:`MODES`)."""
    focus = str(focus or "").strip()
    if mode == "security":
        return _security_brief(parent, base, focus)
    if mode == "standards":
        return _standards_brief(parent, base, focus)
    text = _opening(_ROLES["bugs"], parent, base)
    text += (
        "Look for correctness bugs only: wrong logic, broken edge cases, "
        "crashes, races, lost data, security holes. Ignore style, naming and "
        "refactoring ideas.\n"
    )
    text += _report_format(_WHAT["bugs"])
    if focus:
        text += "\nAlso: %s\n" % focus
    return text


def followup_brief(
    parent: str,
    base: str,
    sha: str,
    findings: str,
    focus: str = "",
    mode: str = "bugs",
    round_no: int = 2,
) -> str:
    """A later round's brief: check the earlier round's ``findings`` (found
    at ``sha``) are fixed or rebutted, review only what changed since, and
    leave code an earlier round read alone unless it is severe."""
    mode = mode if mode in MODES else "bugs"
    focus = str(focus or "").strip()
    text = (
        '[MindFlock route] You are a %s for the session "%s", on a FOLLOW-UP '
        "review (round %d). An earlier round reviewed this branch at commit "
        "%s and reported:\n"
        "----- earlier findings -----\n%s\n----- end of earlier findings -----\n"
        "The author has committed fixes since. In this worktree:\n"
        "(a) Check each earlier finding: fixed in the code, or answered by a "
        "written rebuttal in the commit messages (`git log %s..HEAD`). Report "
        "again only one that is neither, as it stands now.\n"
        "(b) Read `git diff %s..HEAD` (the fix commits) and report only %s "
        "those commits INTRODUCED.\n"
        "(c) Raise no new finding in code an earlier round already read "
        "(`git diff %s...%s`) unless it is severe: data loss, a security "
        "hole, or a crash on a common path.\n"
    ) % (
        _ROLES[mode],
        parent,
        round_no,
        sha,
        findings.strip() or "(none recorded)",
        sha,
        sha,
        _LOOK_FOR[mode],
        base,
        sha,
    )
    if focus and mode != "bugs":
        text += "\n" + _rules_block(focus) + "\n"
    elif focus:
        text += "Also: %s\n" % focus
    return text + _report_format(_WHAT[mode])


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
        "rebutted, the route asks the reviewer to check your fixes."
    )


def _base_of(flight: dict) -> str:
    """The ref the session's change is measured against (blocking): its
    recorded base branch, else the repo's default branch."""
    from backend.web.core import flight_driver as _driver

    inst = _driver._server().ENGINE.instances.get(flight["session"])
    return str(getattr(inst, "BaseBranch", "") or "") or "origin/HEAD"


def _head_of(flight: dict, since: str = "") -> Tuple[str, bool]:
    """The session's HEAD sha ("" when unreadable) and whether ``since`` is
    still an ancestor of it — False after a rebase dropped it (blocking)."""
    from backend.mcp import gitlocal as _gitlocal
    from backend.web.core import flight_driver as _driver

    inst = _driver._server().ENGINE.instances.get(flight["session"])
    if inst is None:
        return "", False
    try:
        folder = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001 — not inspectable: a full review
        return "", False
    head = _gitlocal.head_sha(folder) or ""
    if not head or not since:
        return head, False
    probe = _gitlocal.git(folder, "merge-base", "--is-ancestor", since, head)
    return head, probe is not None


def rounds_of(config: dict) -> int:
    """How many times a review stop's findings go back to the agent."""
    return _flights._int((config or {}).get("rounds"), DEFAULT_ROUNDS, 1, MAX_ROUNDS)


def reasks_of(detail: Optional[dict]) -> int:
    """How many more reviewers this visit of the stop has asked already (an
    older record's single ``respawned`` counts as one)."""
    d = detail if isinstance(detail, dict) else {}
    n = _flights._int(d.get("reasks"), 0, 0, 100)
    return max(n, 1 if d.get("respawned") else 0)


def _followup_of(detail: Optional[dict]) -> Optional[dict]:
    """The earlier round a new ask follows up (``detail.followup``), if any."""
    f = (detail if isinstance(detail, dict) else {}).get("followup")
    if isinstance(f, dict) and str(f.get("findings") or "").strip():
        return f
    return None


async def _run(flight: dict, step: dict, action: dict, now: float) -> dict:
    from backend.web.core import flight_driver as _driver

    cfg = step["config"]
    mode = _mode(cfg)
    focus = cfg.get("focus") or ""
    ss = (flight.get("step_states") or {}).get(step["id"]) or {}
    prior = _followup_of(ss.get("detail"))
    base = await asyncio.to_thread(_base_of, flight)
    head, follows = await asyncio.to_thread(
        _head_of, flight, str((prior or {}).get("sha") or "")
    )
    rnd = 1
    if prior is not None and follows:
        rnd = _flights._int(prior.get("round"), 1, 1, 1000) + 1
        text = followup_brief(
            flight["session"], base, prior["sha"], prior["findings"], focus, mode, rnd
        )
    else:
        text = brief(flight["session"], base, focus, mode)
    carried = dict(action.get("detail") or {})
    reasks = max(reasks_of(carried), reasks_of(ss.get("detail")))
    why = ""
    while True:
        try:
            title = await _driver.spawn_worker(
                flight,
                step,
                text,
                model=cfg.get("model") or "",
                program=cfg.get("program") or "",
                report_back=True,
                title_prefix="review",
            )
            break
        except _kinds.StepFailed as err:
            # Could not start: nothing the agent can fix, no round spent.
            why = "couldn't start the reviewer: %s" % (str(err) or "no reason given")
            if reasks >= MAX_REASKS:
                out = dict(_flights.fail_action(flight, why), reason=why)
                keep = {"reasks": reasks, "phase": None}
                if prior is not None:
                    keep["followup"] = prior
                out["detail"] = keep
                return dict(out, then="halt") if out.get("then") == "agent" else out
            reasks += 1
    if rnd > 1:
        note = "asking %s to check the fixes (%s, round %d)" % (
            title,
            _NOUNS[mode],
            rnd,
        )
    else:
        note = "asking %s for a %s" % (title, _NOUNS[mode])
        if prior is not None:
            note += " — a full review: the commit the last round read is gone"
    detail = dict(carried, children=[title], base=base, reviewed_sha=head, round=rnd)
    if prior is not None:
        detail["followup"] = prior
    if reasks:
        detail["reasks"] = reasks
    if why:
        detail["respawned"] = why
    if detail.get("respawned"):
        note += " (%s)" % detail["respawned"]
    return dict(action, note=note, detail=detail)


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
    left in the session's mailbox, if any. ``started`` sticks once it has
    been seen past ``loading``: one gone without it never started."""
    from backend.web.core import thread as _thread

    started = bool(isinstance(prev, dict) and prev.get("started"))
    if facts.get("present"):
        out = _flights.child_state(facts, prev, since, now)
        if started or str(facts.get("status") or "") not in ("", "loading"):
            out["started"] = True
        return out
    out = {"title": str(facts.get("title") or ""), "state": "gone"}
    if started:
        out["started"] = True
    rep = _thread.report_json(msg)
    if rep is not None and rep["ts"] > since:
        out["report"] = rep
        if rep["status"] == "failed":
            out["state"] = "failed"
        elif rep["status"] != "blocked":
            out["state"] = "done"
    return out


def _last_reply(title: str) -> str:
    """The reviewer's newest assistant message (blocking) — the source MCP
    ``read_output`` view ``last_reply`` reads (``server._agent_output``);
    "" when there is none."""
    from backend import providers
    from backend.session import tmux
    from backend.web.core import flight_driver as _driver

    inst = _driver._server().ENGINE.instances.get(title)
    if inst is None:
        return ""
    try:
        folder = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        folder = ""
    try:
        prov = providers.resolve(getattr(inst, "Program", "") or "")
        text = prov.last_assistant_text(tmux.to_mindflock_tmux_name(title), folder)
    except Exception:  # noqa: BLE001 — no reply: "did not finish"
        return ""
    return text if isinstance(text, str) else ""


def _clean_reply(raw: str) -> str:
    """The tail of a last reply (where its verdict is), made safe to type."""
    from backend.web.core import mailbox as _mailbox

    raw = str(raw or "")[-REPLY_CHARS:]
    lines = [_mailbox.sanitize(ln) for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


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
    if msg is None and entry["state"] == "done" and facts.get("present"):
        # No report is never a silent pass: its last reply is read (before
        # the close takes its worktree) and judged in the report's place.
        out["reply"] = _clean_reply(await asyncio.to_thread(_last_reply, title))
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


def parse_reply(text: str) -> Optional[Tuple[int, str]]:
    """A reviewer's last reply read as its report (it ended without one):
    ``(0, "")`` when a "No findings" line comes after any numbered list;
    ``(count, the list)`` for its LAST numbered list; None when it holds
    neither — that reviewer did not finish."""
    lines = str(text or "").splitlines()
    items = [i for i, ln in enumerate(lines) if _ITEM_RE.match(ln)]
    nones = [i for i, ln in enumerate(lines) if _NO_FINDINGS_RE.match(ln)]
    if nones and (not items or nones[-1] > items[-1]):
        return 0, ""
    if not items:
        return None
    # The last list: from the last item numbered 1 (an earlier list — the
    # steps it took — is not its findings).
    first = 0
    for k, i in enumerate(items):
        if _ITEM_NO_RE.match(lines[i]).group(1).lstrip("0") == "1":
            first = k
    block = "\n".join(lines[items[first] : items[-1] + 1]).strip()
    if len(block) > FINDINGS_CHARS:
        block = block[: FINDINGS_CHARS - 1].rstrip() + "…"
    return len(items) - first, block


def _plural(n: int) -> str:
    return "%d finding%s" % (n, "" if n == 1 else "s")


def _no_verdict(ss: dict, why: str, detail: dict) -> dict:
    """A reviewer that gave no verdict is not the agent's failure: ask
    another (up to :data:`MAX_REASKS` per visit), then halt for the person
    with the plain reason — never handed to the agent, no round spent."""
    n = reasks_of(ss["detail"])
    if n < MAX_REASKS:
        return {
            "action": "run",
            "note": "%s — asking another reviewer" % why,
            "detail": {"reasks": n + 1, "respawned": why},
        }
    return {
        "action": "fail",
        "reason": why,
        "no_fix": True,
        "detail": dict(detail, reasks=n),
    }


def _judge(flight, step, ss, obs, now) -> dict:
    cfg = step["config"]
    noun = _NOUNS[_mode(cfg)]
    kids = ss["detail"].get("children") or []
    if not kids:
        return {"action": "run", "note": "asking for a %s" % noun}
    o = obs.get("review")
    if not isinstance(o, dict):
        return _flights._wait("%s in progress" % noun)
    if o.get("failed"):
        return _no_verdict(ss, str(o["failed"]), {})
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
        return _no_verdict(
            ss, "the reviewer did not finish", dict(closed, reviewer=entry)
        )
    read = ""
    if o.get("report"):
        n, text = parse_findings(o.get("text") or "")
    else:
        got = parse_reply(o.get("reply") or "")
        if got is None:
            return _no_verdict(
                ss,
                "the reviewer did not finish: %s ended without a report" % who,
                closed,
            )
        n, text = got
        read = " (read from %s's last reply)" % who
    if not n:
        return {
            "action": "done",
            "note": "no findings" + read,
            "detail": dict(closed, findings="", receipt="no findings"),
        }
    d = ss["detail"]
    detail = dict(
        closed,
        findings=text,
        receipt=_plural(n),
        # What the next ask in this visit follows up (a fix, a Retry).
        followup={
            "findings": text,
            "sha": str(d.get("reviewed_sha") or ""),
            "round": _flights._int(d.get("round"), 1, 1, 1000),
        },
    )
    if not cfg.get("block", True):
        return {"action": "done", "note": _plural(n) + read, "detail": detail}
    rounds = rounds_of(cfg)
    if step.get("on_fail") == "agent" and ss["rounds"] >= rounds:
        # Out of rounds: a question for the person, not a crash.
        left = "%s left open" % _plural(n)
        if cfg.get("unresolved") == "continue":
            return {
                "action": "done",
                "note": left,
                "detail": dict(detail, receipt=left),
            }
        return {
            "action": "you",
            "note": "%s still open after %d round%s"
            % (_plural(n), ss["rounds"], "" if ss["rounds"] == 1 else "s"),
            "detail": dict(detail, receipt=left, ask=ASK_UNRESOLVED),
        }
    return {
        "action": "fail",
        "reason": "the %s has %s:\n%s" % (noun, _plural(n), text),
        "detail": detail,
    }


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
                    "rounds",
                    "int",
                    "Fix rounds (times findings go back to the agent)",
                    default=DEFAULT_ROUNDS,
                ),
                _flights._f(
                    "unresolved",
                    "select",
                    "Findings still open after the last round",
                    default="ask",
                    options=UNRESOLVED,
                    option_labels=UNRESOLVED_LABELS,
                ),
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
