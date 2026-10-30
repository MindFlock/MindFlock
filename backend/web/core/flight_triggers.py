"""Flight triggers that watch the outside world (docs/flights.md "Round 15 →
Triggers"): a plan whose trigger is ``main_push`` or ``main_red`` watches a
repository's default branch; one whose trigger is ``hook`` waits for ``POST
/api/hooks/flyway/{token}``.

:func:`tick` runs every :data:`TICK_SECONDS` inside
``template_schedule.run_loop`` (through :func:`maybe_tick`, which paces the
loop's faster beat down to a minute): for each watching plan that is not
paused it ``git fetch``\\ es the default branch in ``trigger.repo`` (the
branch is ``origin/HEAD``'s short name, else ``main`` / ``master``) and
compares the tip with what it last saw in ``<flights dir>/trigger_state.json``
(``{plan_id: {sha, fired_at, checks: {sha, verdict, names, fired}}}``):

* ``main_push`` fires when the tip is new. The first sight only arms the
  plan (a schedule counts from when it was set, so does this); a move inside
  ``quiet_minutes`` of the last fire is left where it is and fires, as one
  flight on the latest tip, once the quiet is over.
* ``main_red`` fires when the checks of the tip (the GitHub Checks + Status
  APIs through :mod:`backend.web.core.github_pr`'s request seam, cached per
  sha once the verdict is final) are failing — once per sha, inside the same
  quiet rule. A tip already red when first seen fires too: red IS the event.

A fire is the schedule path's own take-off (``template_schedule.fire_or_skip``:
a session from the plan's ``session`` config, never stacked on a flight still
in the air) with the subject ``{kind: "manual", title: "main moved to
a1b2c3d"}`` / ``"main is red at a1b2c3d (<check names>)"`` and ``by`` the
trigger's name. The hook's subject is ``"hook: <the first 80 chars of the
body, or the JSON's title / text>"``.

Everything here degrades: a repo that cannot be fetched is skipped this tick
(``error`` in its state row), one plan's crash never costs the rest, and the
state file is rewritten only when something changed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.fsutil import atomic_write_json, read_json

__all__ = [
    "TICK_SECONDS",
    "HOOK_BODY_MAX",
    "state_path",
    "load_state",
    "save_state",
    "default_branch",
    "fetch_tip",
    "read_checks",
    "tick",
    "maybe_tick",
    "hook_fire",
    "hook_title",
    "watching_plans",
    "plan_by_token",
]

#: How often the watchers look (the schedule loop beats faster; see
#: :func:`maybe_tick`).
TICK_SECONDS = 60.0
#: ``git fetch`` may hit the network: a wedged one degrades to "skipped".
_FETCH_TIMEOUT_S = 60
#: The most of a hook's body that names the subject / is read at all.
HOOK_BODY_MAX = 64 * 1024
_STATE_FILE = "trigger_state.json"
_HOOK_TITLE_CHARS = 80
_RED_NAMES_MAX = 6
_LOCK = threading.Lock()
_last_tick = 0.0
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


# --------------------------------------------------------------------------- #
# The state file
# --------------------------------------------------------------------------- #
def state_path() -> str:
    from backend.web.core import flights

    return os.path.join(flights.flights_dir(), _STATE_FILE)


def load_state() -> Dict[str, dict]:
    """``{plan_id: {sha, fired_at, checks, error?}}`` as stored ({} when none)."""
    data = read_json(state_path(), {}, expect=dict)
    return {str(k): v for k, v in (data or {}).items() if isinstance(v, dict) and k}


def save_state(state: Dict[str, dict]) -> None:
    with _LOCK:
        atomic_write_json(state_path(), state, prefix=".ftr.")


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #
def _git(folder: str, *args: str, timeout: int = 30) -> Tuple[int, str]:
    """``git -C folder args`` → ``(returncode, stdout)``; ``(-1, "")`` when git
    could not run (missing binary, timeout)."""
    try:
        cp = subprocess.run(
            ["git", "-C", folder, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=dict(os.environ, **_ENV),
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()


def _has_origin(folder: str) -> bool:
    return _git(folder, "remote", "get-url", "origin")[0] == 0


def default_branch(folder: str) -> str:
    """The repo's default branch: ``origin/HEAD``'s short name, else the first
    of main / master that exists (on origin or locally); "" when none."""
    code, out = _git(
        folder, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if code == 0 and out.startswith("origin/"):
        return out[len("origin/") :]
    for name in ("main", "master"):
        for ref in ("refs/remotes/origin/" + name, "refs/heads/" + name):
            if _git(folder, "rev-parse", "--verify", "--quiet", ref)[0] == 0:
                return name
    return ""


def fetch_tip(folder: str, branch: str) -> str:
    """``git fetch origin <branch>`` then the sha of ``origin/<branch>`` (the
    local branch when the repo has no origin); "" when it cannot be had."""
    if not branch:
        return ""
    if _has_origin(folder):
        _git(folder, "fetch", "--quiet", "origin", branch, timeout=_FETCH_TIMEOUT_S)
        code, sha = _git(folder, "rev-parse", "--verify", "--quiet", "origin/" + branch)
    else:
        code, sha = _git(folder, "rev-parse", "--verify", "--quiet", branch)
    return sha if code == 0 and sha else ""


# --------------------------------------------------------------------------- #
# Checks (GitHub REST, the one request seam github_pr has)
# --------------------------------------------------------------------------- #
_RED = (
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
)


async def read_checks(folder: str, sha: str) -> Tuple[str, List[str]]:
    """``(verdict, failing names)`` for ``sha`` in the GitHub repo behind
    ``folder``'s origin: ``failed`` / ``pending`` / ``ok`` / ``none`` (nothing
    reports) / ``unknown`` (no token, no GitHub remote, a fault). Both the
    Checks API and the combined Status API are read, as ``pr_checks`` does."""
    from backend.web.core import github_pr as gh

    ref = gh.repo_ref(folder)
    if ref is None or not sha:
        return "unknown", []
    token = await gh.api_token()
    if not token:
        return "unknown", []
    failed: List[str] = []
    pending = reported = False
    status, data = await gh._request(
        "GET",
        "/repos/{}/commits/{}/check-runs".format(ref.slug, sha),
        token=token,
        params={"per_page": "100"},
    )
    if status == 200 and isinstance(data, dict):
        for run in data.get("check_runs") or []:
            if not isinstance(run, dict):
                continue
            reported = True
            if str(run.get("status") or "") != "completed":
                pending = True
                continue
            if str(run.get("conclusion") or "") in _RED:
                failed.append(str(run.get("name") or "check"))
    elif status not in (200, 404):
        return "unknown", []
    status, data = await gh._request(
        "GET", "/repos/{}/commits/{}/status".format(ref.slug, sha), token=token
    )
    if status == 200 and isinstance(data, dict):
        if int(data.get("total_count") or 0):
            reported = True
            combined = str(data.get("state") or "")
            if combined == "failure":
                for st in data.get("statuses") or []:
                    if isinstance(st, dict) and str(st.get("state")) in (
                        "failure",
                        "error",
                    ):
                        failed.append(str(st.get("context") or "status"))
                if not failed:
                    failed.append("status")
            elif combined == "pending":
                pending = True
    elif status not in (200, 404):
        return "unknown", []
    if failed:
        return "failed", failed
    if pending:
        return "pending", []
    return ("ok" if reported else "none"), []


# --------------------------------------------------------------------------- #
# The tick
# --------------------------------------------------------------------------- #
def watching_plans(plans: Optional[List[dict]] = None) -> List[dict]:
    """The plans whose trigger watches a repo (``main_push`` / ``main_red``),
    not paused, with a repo set."""
    from backend.web.core import flights

    out = []
    for plan in flights.list_plans() if plans is None else plans:
        trig = plan.get("trigger") if isinstance(plan, dict) else None
        if not isinstance(trig, dict) or trig.get("kind") not in flights.MAIN_TRIGGERS:
            continue
        if trig.get("paused") or not str(trig.get("repo") or "").strip():
            continue
        out.append(plan)
    return out


def _quiet_s(plan: dict) -> float:
    from backend.web.core import flights

    trig = plan.get("trigger") or {}
    try:
        mins = int(trig.get("quiet_minutes", flights.TRIGGER_QUIET_MINUTES))
    except (TypeError, ValueError):
        mins = flights.TRIGGER_QUIET_MINUTES
    return max(0, mins) * 60.0


def _in_quiet(plan: dict, row: dict, now: float) -> bool:
    fired = row.get("fired_at")
    try:
        fired = float(fired) if fired else 0.0
    except (TypeError, ValueError):
        fired = 0.0
    return bool(fired) and now - fired < _quiet_s(plan)


def _short(sha: str) -> str:
    return sha[:7]


def red_title(branch: str, sha: str, names: List[str]) -> str:
    shown = [n for n in names if n][:_RED_NAMES_MAX]
    more = len([n for n in names if n]) - len(shown)
    tail = ", ".join(shown) + (" +%d more" % more if more > 0 else "")
    return "%s is red at %s%s" % (branch, _short(sha), " (%s)" % tail if tail else "")


#: The answer of :func:`_held`: the move waits, nothing was tried.
HELD = 0


def _held(plan: dict) -> str:
    """Why a watcher must wait before firing ("" = it may): the plan's
    previous flight is still in the air or its session busy. A watcher
    HOLDS the move (no skipped run every minute) and fires once it is free;
    the hook, which cannot wait, is refused ``busy`` instead."""
    from backend.web.core import template_schedule as ts

    runs = ts.load_runs(ts.flight_runs_path())
    return ts.flight_busy_reason(runs.get(ts._key(plan.get("id"))) or {})


async def _fire(
    plan: dict, kind: str, title: str, now: float, start, hold: bool = True
) -> Tuple[int, dict]:
    """A take-off with the trigger's subject: ``(status, body)``, or
    ``(HELD, {"held": why})`` when ``hold`` and the plan is busy."""
    from backend.web.core import template_schedule as ts

    if hold:
        why = await asyncio.to_thread(_held, plan)
        if why:
            return HELD, {"held": why}
    return await ts.fire_or_skip(
        plan,
        kind,
        now=now,
        start=start,
        subject={"kind": "manual", "title": title},
    )


async def _tick_plan(
    plan: dict, row: dict, now: float, start, checks_reader
) -> Tuple[bool, bool]:
    """One plan's pass against its state ``row`` (mutated in place).
    Returns ``(changed, fired)``."""
    trig = plan.get("trigger") or {}
    kind = str(trig.get("kind") or "")
    repo = str(trig.get("repo") or "")
    branch = await asyncio.to_thread(default_branch, repo)
    tip = await asyncio.to_thread(fetch_tip, repo, branch) if branch else ""
    changed = False
    if not tip:
        err = "no default branch" if not branch else "cannot fetch %s" % branch
        if row.get("error") != err:
            row["error"] = err
            changed = True
        return changed, False
    if row.pop("error", None) is not None:
        changed = True
    if row.get("branch") != branch:
        row["branch"] = branch
        changed = True
    if kind == "main_push":
        seen = str(row.get("sha") or "")
        if not seen:
            row["sha"] = tip  # first sight arms
            return True, False
        if tip == seen:
            return changed, False
        if _in_quiet(plan, row, now):
            if row.get("pending") != tip:
                row["pending"] = tip
                changed = True
            return changed, False
        status, body = await _fire(
            plan, kind, "%s moved to %s" % (branch, _short(tip)), now, start
        )
        if status == HELD:
            # Busy: the move waits (pending) and fires once the flight lands.
            if row.get("pending") != tip or row.get("held") != body["held"]:
                row["pending"], row["held"] = tip, body["held"]
                changed = True
            return changed, False
        row["sha"] = tip  # spent, fired or refused (a refusal is a failed run)
        row.pop("pending", None)
        row.pop("held", None)
        row["last_status"] = status
        if status < 400:
            row["fired_at"] = now
        return True, status < 400
    # main_red
    if row.get("sha") != tip:
        row["sha"] = tip
        changed = True
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    if checks.get("sha") != tip:
        checks = {"sha": tip, "verdict": "", "names": [], "fired": False}
        changed = True
    if checks.get("verdict") not in ("ok", "failed"):
        verdict, names = await checks_reader(repo, tip)
        if verdict != checks.get("verdict") or names != checks.get("names"):
            checks["verdict"] = verdict
            checks["names"] = list(names)
            checks["read_at"] = now
            changed = True
    row["checks"] = checks
    if checks.get("verdict") != "failed" or checks.get("fired"):
        return changed, False
    if _in_quiet(plan, row, now):
        return changed, False
    status, body = await _fire(
        plan, kind, red_title(branch, tip, list(checks.get("names") or [])), now, start
    )
    if status == HELD:
        if row.get("held") != body["held"]:
            row["held"] = body["held"]
            changed = True
        return changed, False
    checks["fired"] = True  # spent, fired or refused
    row.pop("held", None)
    row["last_status"] = status
    if status < 400:
        row["fired_at"] = now
    return True, status < 400


async def tick(
    now: Optional[float] = None,
    start=None,
    plans: Optional[List[dict]] = None,
    checks_reader=None,
) -> List[str]:
    """One pass over the watching plans. ``start`` / ``checks_reader`` /
    ``plans`` are the tests' seams (the flights' start call, :func:`read_checks`,
    the plan list). Returns the plan ids that fired."""
    now = time.time() if now is None else now
    checks_reader = checks_reader or read_checks
    rows = await asyncio.to_thread(watching_plans, plans)
    if not rows:
        return []
    state = await asyncio.to_thread(load_state)
    fired: List[str] = []
    dirty = False
    for plan in rows:
        pid = str(plan.get("id") or "")
        row = dict(state.get(pid) or {})
        try:
            changed, did = await _tick_plan(plan, row, now, start, checks_reader)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — one plan never costs the rest
            row["error"] = str(err) or type(err).__name__
            changed, did = True, False
        if changed:
            state[pid] = row
            dirty = True
        if did:
            fired.append(pid)
    # Rows of plans that no longer watch anything are dropped.
    keep = {str(p.get("id") or "") for p in rows}
    hooks = {
        str(p.get("id") or "")
        for p in (plans if plans is not None else _all_plans())
        if ((p.get("trigger") or {}).get("kind") == "hook")
    }
    for pid in list(state):
        if pid not in keep and pid not in hooks:
            state.pop(pid, None)
            dirty = True
    if dirty:
        await asyncio.to_thread(save_state, state)
    return fired


def _all_plans() -> List[dict]:
    from backend.web.core import flights

    try:
        return flights.list_plans()
    except Exception:  # noqa: BLE001
        return []


async def maybe_tick(now: Optional[float] = None) -> List[str]:
    """:func:`tick`, at most once per :data:`TICK_SECONDS` (the schedule loop
    calls this on its own, faster beat)."""
    global _last_tick
    now = time.time() if now is None else now
    if now - _last_tick < TICK_SECONDS:
        return []
    _last_tick = now
    return await tick(now)


# --------------------------------------------------------------------------- #
# The hook
# --------------------------------------------------------------------------- #
def plan_by_token(token: str, plans: Optional[List[dict]] = None) -> Optional[dict]:
    """The plan whose ``hook`` trigger minted ``token`` (any kind: a plan
    switched off hook keeps its token but no longer answers)."""
    from backend.web.core import flights

    token = str(token or "").lower()
    if not _TOKEN_RE.match(token):
        return None
    for plan in flights.list_plans() if plans is None else plans:
        trig = plan.get("trigger") if isinstance(plan, dict) else None
        if (
            isinstance(trig, dict)
            and trig.get("kind") == "hook"
            and str(trig.get("token") or "") == token
        ):
            return plan
    return None


def hook_title(raw: Any) -> str:
    """``"hook: <text>"`` — the JSON body's ``title`` (else ``text``), else
    the body itself, whitespace squashed, the first 80 chars; ``"hook"`` for
    an empty body."""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw[:HOOK_BODY_MAX]).decode("utf-8", "replace")
    else:
        text = str(raw or "")
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for key in ("title", "text"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    text = val
                    break
            else:
                text = ""
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > _HOOK_TITLE_CHARS:
        text = text[:_HOOK_TITLE_CHARS].rstrip()
    return "hook: " + text if text else "hook"


async def hook_fire(
    token: str, body: Any, now: Optional[float] = None, start=None
) -> Tuple[int, dict]:
    """``POST /api/hooks/flyway/{token}``: ``(status, body)`` — 202 with the
    flight; 404 ``unknown_token``; 409 ``paused``; 429 ``quiet`` (with
    ``retry_after`` seconds) inside the plan's quiet minutes; 409 ``busy``
    while its previous flight is still in the air."""
    now = time.time() if now is None else now
    plan = await asyncio.to_thread(plan_by_token, token)
    if plan is None:
        return 404, {"error": "unknown_token", "detail": "no flyway answers that hook"}
    if (plan.get("trigger") or {}).get("paused"):
        return 409, {"error": "paused", "detail": "that flyway's trigger is paused"}
    pid = str(plan.get("id") or "")
    state = await asyncio.to_thread(load_state)
    row = dict(state.get(pid) or {})
    if _in_quiet(plan, row, now):
        left = int(_quiet_s(plan) - (now - float(row.get("fired_at") or 0)))
        return 429, {
            "error": "quiet",
            "detail": "fired %s ago; quiet for %d more minutes"
            % (_ago(now - float(row.get("fired_at") or 0)), max(1, left // 60)),
            "retry_after": max(1, left),
        }
    status, out = await _fire(plan, "hook", hook_title(body), now, start, hold=False)
    if status < 400:
        row["fired_at"] = now
        row["last_status"] = status
        state[pid] = row
        await asyncio.to_thread(save_state, state)
    return status, out


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%ds" % seconds
    return "%d min" % (seconds // 60)
