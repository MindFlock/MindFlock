"""The team-run tools: ``start_team_run``, ``get_run``, ``list_runs``,
``wait_for_run`` and ``control_run`` — plus the two a split's LEAD uses,
``propose_run_plan`` and ``report_integrated``.

A mixin of :class:`backend.mcp.tools.Toolbox` (kept apart for size), driving
the same ``/api/runs*`` routes the UI does. What a run IS (a server-driven
group: one session per ticket or task line, a concurrency cap with the rest
queued, every member carried along its ship lane by MindFlock's autopilot) is
documented in docs/team-runs.md.

A SPLIT's lead only PROPOSES: ``propose_run_plan`` hands the server pieces
(each a self-contained prompt and the paths it may change); the server
validates them, the user approves with one click, and the server — not the
lead — starts, fences, merges and releases them. When a merge conflicts the
server hands it back to the lead, which resolves it and says so with
``report_integrated``; the server re-checks ancestry rather than taking its
word. Both tools are the lead's alone (checked by identity here and by the
server).

THE SESSIONS ARE THE SERVER'S, not the caller's. ``start_team_run`` creates
sessions with no parent: the calling agent cannot answer, steer or kill them
(they never enter ``policy.spawned_by_me``), so a chatty orchestrator cannot
override what the run decided. It watches with ``get_run`` /
``wait_for_run`` and steers the GROUP with ``control_run`` — and only a group
it started itself (or one its user started), never another agent's.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from backend import client
from backend.mcp.policy import Flock
from backend.mcp.protocol import ToolContext, ToolError

_log = logging.getLogger(__name__)

__all__ = [
    "RunTools",
    "LANES",
    "WAIT_UNTIL",
    "CONTROL_ACTIONS",
    "USER_ONLY_ACTIONS",
]

LANES = ("leave", "commit", "push", "pr", "merge")
WAIT_UNTIL = ("needs_you", "done", "change")
CONTROL_ACTIONS = (
    "pause",
    "resume",
    "cancel",
    "retry",
    "start_now",
    "skip",
    "release",
)
#: What an agent may never do to a group its USER started (see _may_control).
USER_ONLY_ACTIONS = ("release", "resume")
#: Seconds per server long-poll round (like wait_for_message's).
RUN_POLL_S = 25
MAX_RUN_WAIT_S = 1500
DEFAULT_RUN_WAIT_S = 600

D_START_RUN = (
    "Start a TEAM RUN: MindFlock works on several things at once for your "
    "user, one session per item, and carries each one as far as its lane "
    "says. items: ticket IDs or links (PAY-412, sc-123, a URL; a line of only "
    "IDs is that many tickets) and/or task lines (one thing per line). lane: "
    "leave (no commit) | commit | push | pr (one PR per item) | merge (merges "
    "once CI is green; needs confirm_merge=true); default: your user's "
    "fast-track setting, which is leave when they never set one. concurrency: how many run at once (1-8, default 3); "
    "the rest wait in a queue. MindFlock owns these sessions, not you: you "
    "cannot steer or kill them, and they do not report to you. It commits "
    "with messages written from each diff, retries failed hooks, nudges a "
    "stalled agent and surfaces only what needs your user (a prompt, a "
    "repeated failure, the budget). repo_path (default: your session's repo) "
    "is where task lines run; tickets use their own repository. Unresolvable "
    "ticket IDs are refused, never turned into tasks. Then wait_for_run "
    "(until needs_you or done) and get_run."
)
D_GET_RUN = (
    "One team run: its state, counts (queued, active, needs_you, shipped, "
    "failed), cost, and every task {id, ref, title, state, reason, detail, "
    "pr_url}. state queued|starting|working|needs_you|shipping|shipped|"
    "failed|cancelled|skipped; needs_you carries a reason (prompt, stuck, "
    "blocked, ship_halted, restart, budget, approve) and a one-line detail."
)
D_LIST_RUNS = (
    "List team runs, newest first: {id, name, state, paused, policy, counts, "
    "cost_usd}. active_only drops finished and cancelled ones."
)
D_WAIT_RUN = (
    "Wait until a team run changes: until needs_you (something waits on your "
    "user, or the run is done), done (finished or cancelled), or change (any "
    "update). Long-polls the server in 25s rounds up to timeout_s (max 1500). "
    "Returns {reason: needs_you|done|change|timeout, run} with the same run "
    "view as get_run."
)
D_CONTROL_RUN = (
    "Steer a team run you (or your user) started: pause (nothing new starts "
    "or ships; agents keep working), resume (budget_usd raises the budget "
    "too), cancel (stop starting and shipping; sessions and branches are "
    "kept), and per task (task_id): retry (fresh=true starts a new title on a "
    "new branch and keeps the old), start_now (past the concurrency cap "
    "once), skip (a queued task is removed; a running one is detached), "
    "release (a one-for-all group's one PR, once it is ready). resume and "
    "release are refused on a group your user started: those are theirs. "
    "Never answers an agent's prompt for your user. Returns the run summary."
)

D_PROPOSE_PLAN = (
    "LEAD of a split only: propose how to cut the run's task into parallel "
    "pieces. pieces: 2-8 of {title (a few words), prompt (self-contained: "
    "what to do and how to test it), paths (the path globs it may change, "
    "e.g. src/auth/tokens*, tests/auth/test_tokens.py)}. Paths must not "
    "overlap between pieces; commit shared groundwork BEFORE proposing "
    "(workers fork from your last commit). why: one line on the split. The "
    "server validates and returns problems to fix; when ok, your user "
    "approves and MindFlock starts, fences and merges the workers itself — "
    "do not spawn sessions."
)
D_REPORT_INTEGRATED = (
    "LEAD of a split only: after MindFlock handed you a merge conflict and "
    "you merged that worker's branch yourself (resolved, committed), report "
    "it: run_id, task_id (from the hand-off), head_sha (your new HEAD). The "
    "server verifies by ancestry: verified=false means the branch is not in "
    "your HEAD yet."
)

_RUN_ID = {"type": "string", "minLength": 3, "maxLength": 40}


def _obj(props: dict, required: Tuple[str, ...] = ()) -> dict:
    out: Dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


S_START_RUN = _obj(
    {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            "description": "ticket IDs / links and task lines",
        },
        "name": {"type": "string", "maxLength": 80},
        "lane": {"type": "string", "enum": list(LANES)},
        "ask_first": {
            "type": "boolean",
            "default": False,
            "description": "stop before each item's first push/commit for your user's go",
        },
        "grouping": {"type": "string", "enum": ["each", "together"], "default": "each"},
        "concurrency": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
        "program": {"type": "string", "maxLength": 100, "description": "agent CLI"},
        "repo_path": {"type": "string", "maxLength": 1000},
        "budget_usd": {"type": "number", "minimum": 0},
        "split": {"type": "boolean", "default": False},
        "confirm_merge": {"type": "boolean", "default": False},
    },
    ("items",),
)
S_GET_RUN = _obj({"run_id": _RUN_ID}, ("run_id",))
S_LIST_RUNS = _obj({"active_only": {"type": "boolean", "default": False}})
S_WAIT_RUN = _obj(
    {
        "run_id": _RUN_ID,
        "until": {"type": "string", "enum": list(WAIT_UNTIL), "default": "needs_you"},
        "timeout_s": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_RUN_WAIT_S,
            "default": DEFAULT_RUN_WAIT_S,
        },
    },
    ("run_id",),
)
S_CONTROL_RUN = _obj(
    {
        "run_id": _RUN_ID,
        "action": {"type": "string", "enum": list(CONTROL_ACTIONS)},
        "task_id": {"type": "string", "maxLength": 20},
        "fresh": {"type": "boolean", "default": False},
        "budget_usd": {"type": "number", "minimum": 0},
    },
    ("run_id", "action"),
)

S_PROPOSE_PLAN = _obj(
    {
        "run_id": _RUN_ID,
        "pieces": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": _obj(
                {
                    "title": {"type": "string", "minLength": 1, "maxLength": 60},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "paths": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {"type": "string", "minLength": 1, "maxLength": 300},
                    },
                },
                ("title", "prompt", "paths"),
            ),
        },
        "why": {"type": "string", "maxLength": 2000},
    },
    ("run_id", "pieces"),
)
S_REPORT_INTEGRATED = _obj(
    {
        "run_id": _RUN_ID,
        "task_id": {"type": "string", "minLength": 1, "maxLength": 20},
        "head_sha": {"type": "string", "minLength": 4, "maxLength": 64},
    },
    ("run_id", "task_id", "head_sha"),
)

_TASK_KEYS = ("id", "title", "state", "reason", "detail", "pr_url")


def compact_run(run: dict) -> dict:
    """A run as the tools return it: the summary plus one line per task."""
    out = {
        k: run.get(k)
        for k in (
            "id",
            "name",
            "state",
            "paused",
            "pause_reason",
            "policy",
            "counts",
            "cost_usd",
            "created_at",
        )
        if k in run
    }
    tasks = run.get("tasks")
    if isinstance(tasks, list):
        out["tasks"] = []
        for t in tasks:
            row = {k: t.get(k) for k in _TASK_KEYS}
            row["ref"] = t.get("ticket_id") or (t.get("text") or "")[:80]
            out["tasks"].append(row)
    lead = run.get("lead")
    if isinstance(lead, dict):
        out["lead"] = lead.get("title")
    plan = run.get("plan")
    if isinstance(plan, dict):
        out["plan"] = {
            "state": plan.get("state"),
            "round": plan.get("round"),
            "pieces": [
                {"title": p.get("title"), "paths": p.get("paths")}
                for p in plan.get("pieces") or []
                if isinstance(p, dict)
            ],
        }
    for key in ("check", "release"):
        block = run.get(key)
        if isinstance(block, dict) and block.get("state") not in (
            None,
            "none",
            "pending",
        ):
            out[key] = {
                k: block.get(k)
                for k in (
                    "state",
                    "summary",
                    "tests",
                    "pr_url",
                    "compare_url",
                    "local_origin",
                    "detail",
                )
                if block.get(k) not in (None, "")
            }
    return out


class RunTools:
    """The five handlers; mixed into ``Toolbox``."""

    run_poll_s = RUN_POLL_S

    # -- helpers ---------------------------------------------------------------- #
    def _run_api(self, method: str, path: str, payload=None, what: str = "", **kw):
        try:
            if method == "GET":
                resp = self.api.get(path, **kw)
            else:
                resp = self.api.post(path, payload or {}, **kw)
        except client.ApiError as err:
            if err.status in (404, 405) and "no such group" not in err.message:
                if "Not Found" in err.message or err.status == 405:
                    raise ToolError(
                        "this MindFlock server has no team runs; upgrade it to "
                        "use %s" % what
                    ) from None
            raise self._api_error(err, what) from None
        return resp if isinstance(resp, dict) else {}

    def _caller_repo(self, flock: Flock) -> str:
        me = flock.self_title
        row = flock.local.get(me) if me else None
        return str((row or {}).get("path") or "")

    def _may_control(self, flock: Flock, run: dict, action: str) -> None:
        """An agent steers a group it started, or one its user started —
        never another agent's. On a group its USER started, an agent may slow
        or stop things (pause, cancel, skip, retry, start_now) but never take
        the outward step the user kept for themselves: ``release`` (the one
        PR / merge) and ``resume`` (lifting a pause or budget stop the user —
        or the budget — put there) are the user's alone."""
        me = flock.self_title
        by = str(run.get("created_by") or "user")
        if by == "user" and me and action in USER_ONLY_ACTIONS:
            raise ToolError(
                "control_run(%s) refused: group %r was started by your user, and "
                "only they can %s it (its card in MindFlock). Tell them it is "
                "ready instead." % (action, run.get("name"), action)
            )
        if by == "user" or not me:
            return
        if by != "agent:" + me:
            raise ToolError(
                "control_run(%s) refused: group %r was started by %s, not by you"
                % (action, run.get("name"), by[len("agent:") :] or by)
            )

    # -- start_team_run --------------------------------------------------------- #
    def start_team_run(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        self.policy.require_write(flock, "start_team_run")
        lane = args.get("lane")
        if lane == "merge" and not args.get("confirm_merge"):
            raise ToolError(
                "lane merge merges every item's PR with no one looking: pass "
                "confirm_merge=true only when that was clearly asked for"
            )
        items = [str(i).strip() for i in args["items"] if str(i).strip()]
        if not items:
            raise ToolError("start_team_run needs at least one item")
        repo = str(args.get("repo_path") or "").strip() or self._caller_repo(flock)
        program = str(args.get("program") or "").strip()
        preview = self._run_api(
            "POST",
            "/api/runs/preview",
            {"text": "\n".join(items), "repo_path": repo, "program": program},
            "start_team_run",
            timeout=120.0,
        )
        parsed = [p for p in preview.get("items") or [] if isinstance(p, dict)]
        bad = [p for p in parsed if p.get("kind") == "ticket" and p.get("error")]
        if bad:
            raise ToolError(
                "start_team_run: these tickets did not resolve (fix or drop them; "
                "they are never turned into tasks): "
                + "; ".join("%s — %s" % (p.get("ref"), p.get("error")) for p in bad)
            )
        if not parsed:
            raise ToolError("start_team_run: nothing to do")
        if any(p.get("kind") == "task" for p in parsed) and not repo:
            raise ToolError(
                "start_team_run: task lines need repo_path (you are not inside a "
                "MindFlock session, so there is no repo to default to)"
            )
        create_items: List[dict] = []
        for p in parsed:
            if p.get("kind") == "ticket":
                create_items.append(
                    {"kind": "ticket", "source": p.get("source"), "id": p.get("id")}
                )
            else:
                create_items.append({"kind": "task", "text": p.get("text")})
        policy: Dict[str, Any] = {
            "ask_first": bool(args.get("ask_first")),
            "grouping": args.get("grouping") or "each",
            "release": "ask",
        }
        if lane:
            policy["lane"] = lane
        payload: Dict[str, Any] = {
            "name": str(args.get("name") or "").strip()
            or preview.get("name_suggestion")
            or "",
            "items": create_items,
            "policy": policy,
            "concurrency": int(args.get("concurrency") or 3),
            "repo_path": repo,
            "split": bool(args.get("split")),
            "created_by": ("agent:" + flock.self_title if flock.self_title else "user"),
        }
        if program:
            payload["program"] = program
        if args.get("budget_usd") is not None:
            payload["budget_usd"] = float(args["budget_usd"])
        resp = self._run_api(
            "POST", "/api/runs", payload, "start_team_run", timeout=180.0
        )
        run = resp.get("run") or {}
        return {
            "run_id": run.get("id"),
            "name": run.get("name"),
            "lane": (run.get("policy") or {}).get("lane"),
            "tasks": [
                {
                    "id": t.get("id"),
                    "ref": t.get("ticket_id") or (t.get("text") or "")[:80],
                    "title": t.get("title"),
                    "state": t.get("state"),
                }
                for t in run.get("tasks") or []
            ],
            "warnings": list(resp.get("warnings") or [])
            + list(preview.get("warnings") or []),
            "note": "MindFlock owns these sessions now — use get_run / wait_for_run",
        }

    # -- read ----------------------------------------------------------------- #
    def _get_run(self, run_id: str, what: str) -> dict:
        resp = self._run_api("GET", "/api/runs/%s" % run_id, what=what)
        run = resp.get("run")
        if not isinstance(run, dict):
            raise ToolError("%s: the server returned no run" % what)
        return run

    def get_run(self, args: dict, ctx: ToolContext) -> dict:
        return compact_run(self._get_run(str(args["run_id"]), "get_run"))

    def list_runs(self, args: dict, ctx: ToolContext) -> dict:
        path = "/api/runs" + ("?active=1" if args.get("active_only") else "")
        resp = self._run_api("GET", path, what="list_runs")
        return {"runs": [r for r in resp.get("runs") or [] if isinstance(r, dict)]}

    def wait_for_run(self, args: dict, ctx: ToolContext) -> dict:
        run_id = str(args["run_id"])
        until = args.get("until") or "needs_you"
        timeout = int(args.get("timeout_s") or DEFAULT_RUN_WAIT_S)
        ctx.start_progress(timeout)
        first = self._get_run(run_id, "wait_for_run")
        rev = int(first.get("rev") or 0)
        deadline = self.monotonic() + timeout
        while True:
            ctx.check_cancelled()
            left = deadline - self.monotonic()
            if left <= 0:
                return {"reason": "timeout", "run": compact_run(first)}
            wait = max(1, min(self.run_poll_s, int(left)))
            resp = self._run_api(
                "GET",
                "/api/runs/%s?wait=%d&until=%s&rev=%d" % (run_id, wait, until, rev),
                what="wait_for_run",
                timeout=wait + 15.0,
            )
            run = resp.get("run") if isinstance(resp.get("run"), dict) else first
            reason = str(resp.get("reason") or "timeout")
            if reason != "timeout":
                return {"reason": reason, "run": compact_run(run)}
            first = run
            if self.run_poll_s <= 0:  # pragma: no cover — defensive
                ctx.sleep(1.0)

    # -- control -------------------------------------------------------------- #
    def control_run(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        self.policy.require_write(flock, "control_run")
        run_id = str(args["run_id"])
        action = args["action"]
        run = self._get_run(run_id, "control_run")
        self._may_control(flock, run, action)
        task_id = str(args.get("task_id") or "").strip()
        base = "/api/runs/%s" % run_id
        if action in ("retry", "start_now", "skip"):
            if not task_id:
                raise ToolError("control_run(%s) needs task_id" % action)
            route = {"retry": "retry", "start_now": "start-now", "skip": "skip"}[action]
            body: Dict[str, Any] = {}
            if action == "retry":
                body["fresh"] = bool(args.get("fresh"))
            self._run_api(
                "POST",
                "%s/tasks/%s/%s" % (base, task_id, route),
                body,
                "control_run(%s)" % action,
            )
        elif action == "resume":
            body = {}
            if args.get("budget_usd") is not None:
                body["budget_usd"] = float(args["budget_usd"])
            self._run_api("POST", base + "/resume", body, "control_run(resume)")
        else:
            self._run_api(
                "POST", "%s/%s" % (base, action), {}, "control_run(%s)" % action
            )
        return compact_run(self._get_run(run_id, "control_run"))

    # -- the lead's two ---------------------------------------------------------- #
    def _require_lead(self, flock: Flock, run: dict, action: str) -> str:
        """Only the run's lead calls these, and only for its own run."""
        me = flock.self_title
        lead = str(
            run.get("lead", {}).get("title")
            if isinstance(run.get("lead"), dict)
            else ""
        )
        if not me:
            raise ToolError(
                "%s needs a MindFlock session identity — only the group's lead "
                "calls it" % action
            )
        if not lead or me != lead:
            raise ToolError(
                "%s refused: you are not the lead of group %r (its lead is %s)"
                % (action, run.get("name"), lead or "none")
            )
        return me

    def propose_run_plan(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        self.policy.require_write(flock, "propose_run_plan")
        run_id = str(args["run_id"])
        run = self._get_run(run_id, "propose_run_plan")
        me = self._require_lead(flock, run, "propose_run_plan")
        payload = {
            "pieces": args["pieces"],
            "why": str(args.get("why") or ""),
            "from": me,
        }
        try:
            resp = self.api.post("/api/runs/%s/plan" % run_id, payload)
        except client.ApiError as err:
            if err.status == 422:
                body = getattr(err, "payload", None) or {}
                problems = body.get("problems") or []
                return {
                    "ok": False,
                    "problems": problems or [{"piece": "", "error": err.message}],
                    "note": "fix these and call propose_run_plan again",
                }
            raise self._api_error(err, "propose_run_plan") from None
        resp = resp if isinstance(resp, dict) else {}
        n = len(((resp.get("plan") or {}).get("pieces")) or [])
        return {
            "ok": True,
            "problems": [],
            "note": "proposed %d pieces — your user approves them and picks where "
            "they run (separate worktrees, or your folder); MindFlock then starts, "
            "fences and commits or merges the workers. Wait; do not spawn "
            "sessions." % n,
        }

    def report_integrated(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        self.policy.require_write(flock, "report_integrated")
        run_id = str(args["run_id"])
        run = self._get_run(run_id, "report_integrated")
        me = self._require_lead(flock, run, "report_integrated")
        resp = self._run_api(
            "POST",
            "/api/runs/%s/integrated" % run_id,
            {"task_id": args["task_id"], "head_sha": args["head_sha"], "from": me},
            "report_integrated",
        )
        verified = bool(resp.get("verified"))
        out = {"ok": bool(resp.get("ok", True)), "verified": verified}
        if not verified:
            out["note"] = (
                "the worker's branch is not in your HEAD yet — finish the merge "
                "(git merge --no-ff <branch>), commit, and report again"
            )
        return out


def run_of(resp: Optional[dict]) -> Optional[dict]:
    """The ``run`` block of a response, or None."""
    run = (resp or {}).get("run")
    return run if isinstance(run, dict) else None
