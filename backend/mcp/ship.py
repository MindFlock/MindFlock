"""The ship and ticket tools: ``ship_session``, ``set_autopilot``,
``spawn_ticket_session`` and ``list_tickets``.

A mixin of :class:`backend.mcp.tools.Toolbox` (kept apart only for size): the
handlers use the toolbox's API client, identity, policy and clock. Like every
other tool they drive the SAME server routes the UI does — nothing here
commits, pushes or opens a PR by itself:

* ``ship_session`` walks ``/commit`` → ``/push-branch`` → ``/make-pr`` →
  ``/merge-pr`` up to a depth, NOW. Commit and push are fire-and-forget into
  the session's shell (the user watches the hooks run), so completion is
  observed through ``GET /ship-status`` — the commit marker's exit status and
  mtime, ``HEAD`` against the local ``origin/<branch>`` ref, and the shell
  pane's tail for the reason a step failed. It is idempotent: a step that is
  already done is skipped, so calling it again after a timeout resumes.
* ``set_autopilot`` arms MindFlock's own autopilot (``POST /fast-track``),
  which ships the session when its turn ends. "The agent is done" is not
  knowable — only "a turn ended" — so ``ship_session`` refuses a target that
  is mid-turn and points here instead.
* ``spawn_ticket_session`` starts a ticket the way the Intake panel does
  (``POST /api/tickets/start``), as the caller's worker.
* ``list_tickets`` is a thin view of the Intake panel's cached ticket list.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from backend import client
from backend.mcp.policy import Flock
from backend.mcp.protocol import ToolContext, ToolError

_log = logging.getLogger(__name__)

__all__ = [
    "ShipTools",
    "SHIP_DEPTHS",
    "AUTOPILOT_DEPTHS",
    "PUSH_COMMAND",
    "push_failure",
]

#: ship_session's rungs, in order.
SHIP_DEPTHS = ("commit", "push", "pr", "merge")
#: set_autopilot's choices ("off" disarms; the server's "agent" rung is for
#: intake, not a live session).
AUTOPILOT_DEPTHS = ("off",) + SHIP_DEPTHS
SHIP_POLL_S = 2.0
DEFAULT_SHIP_WAIT_S = 600
MAX_SHIP_WAIT_S = 1500
#: After POST /commit, how long a clean tree with an unchanged HEAD and no
#: commit marker may last before it reads as "nothing was committed".
COMMIT_CLEAN_GRACE_S = 8.0
SUGGEST_TIMEOUT_S = 150.0  # the server runs the session's CLI headlessly
PR_TIMEOUT_S = 150.0  # gh / REST, plus a merge's post-merge fetch
TICKET_READY_WAIT_S = 45.0
TAIL_LINES = 40
#: The one-liner /push-branch types (server.instance_push_branch).
PUSH_COMMAND = "git push --no-verify -u origin HEAD"
_PUSH_FAILURES = (
    "error: failed to push",
    "! [rejected]",
    "! [remote rejected]",
    "fatal:",
    "permission denied",
    "authentication failed",
    "could not read from remote",
)


def push_failure(before: str, now: str) -> Optional[str]:
    """The failure text after the LAST push command in the shell tail ``now``,
    or None. ``before`` is the tail as it was before this push was typed: an
    old push's failure is still on screen until the new command echoes, so a
    segment identical to the one already there is never news."""

    def segment(text: str) -> Optional[str]:
        i = (text or "").rfind(PUSH_COMMAND)
        return None if i < 0 else text[i + len(PUSH_COMMAND) :]

    seg = segment(now)
    if seg is None or seg == segment(before):
        return None
    low = seg.lower()
    if any(m in low for m in _PUSH_FAILURES):
        return seg.strip()[-3000:]
    return None


def _first_line(text: str, limit: int = 72) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit].rstrip()


# --------------------------------------------------------------------------- #
# Model-facing text
# --------------------------------------------------------------------------- #
D_SHIP = (
    "Ship a session's work NOW through MindFlock's own commit/push/PR/merge "
    "buttons, in order, up to depth: commit (stage all + commit, running the "
    "repo's pre-commit hooks), push, pr (open a pull request; returns its "
    "URL), merge (merge that PR; needs confirm_merge=true). title: yourself or "
    "a session you manage. The commit message is written from the diff "
    "unless you pass message; the PR body is the worker's report_result "
    "summary when it sent one (or pr_body). Steps already done are skipped, so "
    "calling it again resumes. Refused while the target is mid-turn or on a "
    "dialog (shipping half-finished work): wait_for_session first, or use "
    "set_autopilot to ship when its turn ends; also refused while its "
    "autopilot is armed. Merge only when the PR is "
    "mergeable and CI is not failing or running. wait=true (default) waits up "
    "to timeout_s for the commit hooks and the push. Returns steps "
    "[{step, state, ...}] with the commit sha, the PR url; a failed step "
    "is an error naming it, with the hook output or the push error."
)
D_AUTOPILOT = (
    "Arm MindFlock's autopilot on a session (yourself or one you manage): "
    "when its agent's turn ends it ships itself up to depth (commit, push, pr "
    "or merge) through the same commit/push/PR/merge buttons, retrying the "
    "pre-commit hooks Settings allow-lists and halting with a reason on anything that needs a "
    "human. depth off disarms. Use it for a worker that is still working "
    "(ship_session refuses mid-turn). merge needs confirm_merge=true and waits "
    "for CI. Returns the autopilot state {depth, state, step, reason, url}; "
    "get_session shows it later."
)
D_TICKET = (
    "Start a ticket (Shortcut, Jira, Linear, GitHub Issues or Asana: "
    "whatever Intake → Tickets is configured for) as your worker, exactly as "
    "the Intake panel's Begin work does: a provisioned session titled by the "
    "ticket (e.g. sc-23588) on its feature branch, seeded with the ticket "
    "text. ticket: its slug (sc-23588), id or URL as list_tickets shows it; "
    "add source when the ticket is not in that list. note adds your own "
    "instructions after the ticket text; the report-back footer is appended "
    "when its CLI gets the MindFlock tools. autopilot arms shipping at "
    "creation (default off: the worker reports and you decide; merge needs "
    "confirm_merge=true). It forks from the ticket repository's base branch, "
    "not your HEAD. Spawn limits apply. Then wait_for_session."
)
D_TICKETS = (
    "List tickets from the Intake → Tickets sources (cached, so cheap): "
    "{source, id, slug, name, url, state, session, has_session, eligible, "
    "reasons, assignee}. query matches slug, id or name; source narrows to "
    "one source; startable_only drops tickets that already have a session. "
    "Start one with spawn_ticket_session."
)

_TITLE_OR_SELF = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "description": "session title (default: yourself)",
}


def _obj(props: dict, required: Tuple[str, ...] = ()) -> dict:
    out: Dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


S_SHIP = _obj(
    {
        "title": _TITLE_OR_SELF,
        "depth": {"type": "string", "enum": list(SHIP_DEPTHS)},
        "message": {
            "type": "string",
            "maxLength": 5000,
            "description": "commit message (default: written from the diff)",
        },
        "pr_title": {"type": "string", "maxLength": 256},
        "pr_body": {"type": "string", "maxLength": 60000},
        "base": {
            "type": "string",
            "maxLength": 200,
            "description": "PR base branch (default: the configured one)",
        },
        "confirm_merge": {"type": "boolean", "default": False},
        "wait": {"type": "boolean", "default": True},
        "timeout_s": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SHIP_WAIT_S,
            "default": DEFAULT_SHIP_WAIT_S,
        },
    },
    ("depth",),
)
S_AUTOPILOT = _obj(
    {
        "title": _TITLE_OR_SELF,
        "depth": {"type": "string", "enum": list(AUTOPILOT_DEPTHS)},
        "message": {"type": "string", "maxLength": 5000},
        "base": {"type": "string", "maxLength": 200},
        "confirm_merge": {"type": "boolean", "default": False},
    },
    ("depth",),
)
S_TICKET = _obj(
    {
        "ticket": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "slug (sc-23588), id or URL",
        },
        "source": {"type": "string", "maxLength": 200},
        "agent": {
            "type": "string",
            "maxLength": 100,
            "description": "agent CLI (default: the source's)",
        },
        "effort": {"type": "string", "maxLength": 20},
        "note": {"type": "string", "maxLength": 4000},
        "autopilot": {
            "type": "string",
            "enum": list(AUTOPILOT_DEPTHS),
            "default": "off",
        },
        "confirm_merge": {"type": "boolean", "default": False},
        "report_back": {"type": "boolean", "default": True},
        "wait_ready": {"type": "boolean", "default": True},
    },
    ("ticket",),
)
S_TICKETS = _obj(
    {
        "query": {"type": "string", "maxLength": 200},
        "source": {"type": "string", "maxLength": 200},
        "startable_only": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
    }
)

_TICKET_KEYS = (
    "source",
    "id",
    "slug",
    "name",
    "url",
    "session",
    "has_session",
    "eligible",
    "reasons",
    "assignee",
)


class _Timeout(Exception):
    """A ship step outlived the call's wait budget (not an error: resume)."""


class ShipTools:
    """The four handlers; mixed into ``Toolbox``."""

    ship_poll_s = SHIP_POLL_S
    ticket_ready_wait_s = TICKET_READY_WAIT_S

    # -- helpers ---------------------------------------------------------------- #
    def _target(self, flock: Flock, args: dict, action: str) -> str:
        title = str(args.get("title") or "").strip() or (flock.self_title or "")
        if not title:
            raise ToolError(
                "%s needs title: you are not inside a MindFlock session" % action
            )
        return title

    def _ship_status(self, title: str, tail: int = 0) -> dict:
        path = self.api.inst_path(
            title, "/ship-status" + ("?tail=%d" % tail if tail else "")
        )
        try:
            resp = self.api.get(path)
        except client.ApiError as err:
            if err.status == 404 and "instance not found" not in err.message:
                raise ToolError(
                    "this MindFlock server has no /ship-status route; upgrade it "
                    "to ship from the MCP"
                ) from None
            raise self._api_error(err, "ship_session(%s)" % title) from None
        return resp if isinstance(resp, dict) else {}

    def _fresh_row(self, title: str) -> dict:
        """``GET /stage``: the session's row recomputed now (stage, pr_url,
        merge_state) — the listing can lag a just-finished step."""
        try:
            resp = self.api.get(self.api.inst_path(title, "/stage"), timeout=60.0)
        except client.ApiError as err:
            raise self._api_error(err, "ship_session(%s)" % title) from None
        return resp if isinstance(resp, dict) else {}

    @staticmethod
    def _shippable(title: str, row: dict) -> None:
        """Refuse a target whose agent is not between turns — the autopilot's
        own rule ("a turn ended"), minus its dwell: the caller vouches for the
        rest."""
        status = str(row.get("status") or "")
        activity = str(row.get("activity") or "")
        hint = (
            "wait_for_session(%r) until it is done, or set_autopilot(title=%r, "
            "depth=...) to ship it when its turn ends" % (title, title)
        )
        if status == "loading":
            raise ToolError("%r is still starting; nothing to ship yet" % title)
        if status == "paused":
            raise ToolError("%r is paused (no worktree); resume it first" % title)
        if activity == "working":
            raise ToolError(
                "%r is mid-turn: shipping now would commit half-finished work. %s"
                % (title, hint)
            )
        if activity == "clarify":
            raise ToolError(
                "%r is blocked on a dialog, so its work may be unfinished: answer "
                "it (read_output view=screen, answer_prompt), then %s" % (title, hint)
            )
        if activity == "limit":
            raise ToolError(
                "%r stopped at its usage limit, so its work may be unfinished; "
                "%s" % (title, hint)
            )

    def _commit_message(self, title: str, args: dict, row: dict, warn: list) -> str:
        """The message: the caller's; else a blocked attempt's (a retry keeps
        its subject); else one written from the diff (the ✨ button); else the
        worker's report headline; else a plain default."""
        msg = str(args.get("message") or "").strip()
        if msg:
            return msg
        try:
            pending = self.api.get(self.api.inst_path(title, "/commit-message"))
            msg = str((pending or {}).get("message") or "").strip()
        except (client.ApiError, ToolError):
            msg = ""
        if msg:
            return msg
        try:
            resp = self.api.post(
                self.api.inst_path(title, "/commit-message/suggest"),
                {},
                timeout=SUGGEST_TIMEOUT_S,
            )
            msg = str((resp or {}).get("message") or "").strip()
        except (client.ApiError, ToolError) as err:
            reason = getattr(err, "message", str(err))
            warn.append("could not write a commit message from the diff (%s)" % reason)
            msg = ""
        if msg:
            return msg
        report = row.get("last_report") if isinstance(row, dict) else None
        msg = _first_line((report or {}).get("summary") or "")
        if msg:
            warn.append("committed under the worker's report headline")
            return msg
        warn.append("committed under a generic message; amend it if needed")
        return "Work from MindFlock session %s" % title

    def _await_commit(
        self, title: str, before: dict, deadline: float, ctx: ToolContext
    ) -> dict:
        t0 = float(before.get("now") or 0.0)
        head0 = str(before.get("head_sha") or "")
        started = self.monotonic()
        while True:
            ctx.check_cancelled()
            st = self._ship_status(title)
            if not st.get("committing"):
                at = st.get("commit_at")
                fresh = isinstance(at, (int, float)) and at >= t0 - 1.0
                head = str(st.get("head_sha") or "")
                if fresh and st.get("commit_rc") == 0:
                    return {"ok": True, "sha": head}
                if fresh and st.get("commit_rc") not in (None, 0):
                    failed = self._ship_status(title, TAIL_LINES)
                    return {
                        "ok": False,
                        "rc": st.get("commit_rc"),
                        "failed_step": failed.get("failed_step"),
                        "failed_hook": failed.get("failed_hook"),
                        "output_tail": failed.get("shell_tail") or "",
                    }
                if head and head != head0:
                    return {"ok": True, "sha": head}
                if (
                    not st.get("dirty")
                    and self.monotonic() - started >= COMMIT_CLEAN_GRACE_S
                ):
                    return {"ok": False, "nothing": True}
            if self.monotonic() >= deadline:
                raise _Timeout()
            ctx.sleep(self.ship_poll_s)

    def _await_push(
        self, title: str, before_tail: str, deadline: float, ctx: ToolContext
    ) -> dict:
        while True:
            ctx.check_cancelled()
            st = self._ship_status(title, TAIL_LINES)
            if st.get("pushed"):
                return {"ok": True, "sha": st.get("head_sha")}
            why = push_failure(before_tail, str(st.get("shell_tail") or ""))
            if why:
                return {"ok": False, "output_tail": why}
            if self.monotonic() >= deadline:
                raise _Timeout()
            ctx.sleep(self.ship_poll_s)

    @staticmethod
    def _fail(message: str, steps: List[dict], **extra: Any) -> ToolError:
        data: Dict[str, Any] = {"steps": steps}
        data.update({k: v for k, v in extra.items() if v not in (None, "", [])})
        return ToolError(message, data)

    # -- ship_session ----------------------------------------------------------- #
    def ship_session(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        title = self._target(flock, args, "ship_session")
        depth = args["depth"]
        row = self.policy.require_ship(
            flock,
            title,
            "ship_session",
            merge=depth == "merge",
            confirm_merge=bool(args.get("confirm_merge")),
            depth=depth,
        )
        auto = row.get("autopilot") if isinstance(row.get("autopilot"), dict) else {}
        if auto.get("state") == "running":
            # Two drivers on one shell double-commit and double-push: the
            # autopilot's lease is the server's, so it wins.
            raise ToolError(
                "%r has its autopilot armed (depth %s, %s): it will ship itself. "
                'Disarm it first with set_autopilot(title=%r, depth="off") to '
                "ship by hand."
                % (title, auto.get("depth"), auto.get("note") or "waiting", title)
            )
        if title != flock.self_title:
            self._shippable(title, row)
        timeout = int(args.get("timeout_s") or DEFAULT_SHIP_WAIT_S)
        wait = args.get("wait", True)
        ctx.start_progress(timeout)
        deadline = self.monotonic() + timeout
        want = SHIP_DEPTHS.index(depth)
        steps: List[dict] = []
        warnings: List[str] = []
        out: Dict[str, Any] = {"title": title, "depth": depth, "steps": steps}

        phase = ["commit"]

        def done(ok: bool = True) -> dict:
            out["ok"] = ok
            if warnings:
                out["warnings"] = warnings
            return out

        def started(step: str, **extra: Any) -> dict:
            steps.append(dict({"step": step, "state": "started"}, **extra))
            out["ok"] = True
            out["hint"] = (
                "the %s is running in the session's shell; call ship_session "
                "again with the same depth to continue (finished steps are "
                "skipped)" % step
            )
            if warnings:
                out["warnings"] = warnings
            return out

        def timed_out(step: str) -> dict:
            steps.append({"step": step, "state": "running"})
            out["timed_out"] = True
            out["hint"] = (
                "the %s is still running; call ship_session again with the same "
                "depth to keep going (finished steps are skipped)" % step
            )
            if warnings:
                out["warnings"] = warnings
            return out

        try:
            # ---- commit ---------------------------------------------------- #
            st = self._ship_status(title)
            if st.get("committing"):
                # Someone (the user, the autopilot) is committing already: let
                # that finish, then see what is left.
                if not wait:
                    return started("commit", note="a commit was already running")
                self._await_commit(title, st, deadline, ctx)
                st = self._ship_status(title)
            if st.get("dirty"):
                msg = self._commit_message(title, args, row, warnings)
                before = self._ship_status(title)
                try:
                    self.api.post(
                        self.api.inst_path(title, "/commit"),
                        {"message": msg},
                        timeout=60.0,
                    )
                except client.ApiError as err:
                    raise self._fail(
                        "ship_session(%s): commit failed: %s" % (title, err.message),
                        steps,
                    ) from None
                if not wait:
                    return started("commit", message=_first_line(msg, 200))
                res = self._await_commit(title, before, deadline, ctx)
                if res.get("nothing"):
                    steps.append(
                        {
                            "step": "commit",
                            "state": "skipped",
                            "reason": "nothing to commit (the tree came out clean)",
                        }
                    )
                elif not res["ok"]:
                    hook = res.get("failed_step") or res.get("failed_hook")
                    steps.append({"step": "commit", "state": "failed"})
                    raise self._fail(
                        "ship_session(%s): the commit was blocked by a pre-commit "
                        "hook%s (exit %s). Fix it (or send_message the session to), "
                        "then call ship_session again: the same message is reused."
                        % (title, " (%s)" % hook if hook else "", res.get("rc")),
                        steps,
                        failed_hook=res.get("failed_hook"),
                        output_tail=res.get("output_tail"),
                    )
                else:
                    steps.append(
                        {
                            "step": "commit",
                            "state": "done",
                            "sha": res.get("sha"),
                            "message": _first_line(msg, 200),
                        }
                    )
            else:
                steps.append(
                    {
                        "step": "commit",
                        "state": "skipped",
                        "reason": "nothing to commit (working tree clean)",
                    }
                )
            if want == 0:
                return done()

            # ---- push ------------------------------------------------------ #
            phase[0] = "push"
            st = self._ship_status(title, TAIL_LINES)
            out["branch"] = st.get("branch")
            if st.get("pushed"):
                steps.append(
                    {"step": "push", "state": "skipped", "reason": "already pushed"}
                )
            else:
                if st.get("beyond_base") == 0:
                    raise self._fail(
                        "ship_session(%s): nothing to push — %s has no commits "
                        "beyond %s" % (title, st.get("branch"), st.get("base")),
                        steps,
                    )
                try:
                    self.api.post(
                        self.api.inst_path(title, "/push-branch"), {}, timeout=60.0
                    )
                except client.ApiError as err:
                    raise self._fail(self._push_error(title, err), steps) from None
                if not wait:
                    return started("push", branch=st.get("branch"))
                res = self._await_push(
                    title, str(st.get("shell_tail") or ""), deadline, ctx
                )
                if not res["ok"]:
                    steps.append({"step": "push", "state": "failed"})
                    raise self._fail(
                        "ship_session(%s): the push failed; see output_tail" % title,
                        steps,
                        output_tail=res.get("output_tail"),
                    )
                steps.append(
                    {
                        "step": "push",
                        "state": "done",
                        "branch": st.get("branch"),
                        "sha": res.get("sha"),
                    }
                )
            if want == 1:
                return done()

            # ---- pr -------------------------------------------------------- #
            phase[0] = "pr"
            fresh = self._fresh_row(title)
            if fresh.get("stage") == "pr" and fresh.get("pr_url"):
                steps.append(
                    {
                        "step": "pr",
                        "state": "skipped",
                        "reason": "a PR is already open",
                        "url": fresh["pr_url"],
                    }
                )
                out["pr_url"] = fresh["pr_url"]
            else:
                handoff = self._open_pr(title, args, row, steps, out)
                if handoff:
                    return done(ok=False)
            if want == 2:
                return done()

            # ---- merge ----------------------------------------------------- #
            phase[0] = "merge"
            return done(ok=self._merge(title, steps, out))
        except _Timeout:
            return timed_out(phase[0])

    def _push_error(self, title: str, err: client.ApiError) -> str:
        msg = err.message
        if "checks haven't passed" in msg:
            return (
                "ship_session(%s): this repository gates pushes on its check "
                "command, which has not passed for this commit. set_autopilot "
                "(depth push or further) runs the check and pushes once it "
                "passes; or have the session run it." % title
            )
        if err.status == 409 and ("red zone" in msg or "green zone" in msg):
            return (
                "ship_session(%s): push refused — %s. Zoned files need a human; "
                "ask your user." % (title, msg)
            )
        return "ship_session(%s): push failed: %s" % (title, msg)

    def _pr_body(self, args: dict, row: dict) -> str:
        body = str(args.get("pr_body") or "").strip()
        if body:
            return body
        report = row.get("last_report") if isinstance(row, dict) else None
        summary = str((report or {}).get("summary") or "").strip()
        if not summary:
            return ""
        status = str((report or {}).get("status") or "")
        return "%s\n\n---\nReported by MindFlock worker `%s`%s." % (
            summary,
            row.get("title"),
            " (status: %s)" % status if status else "",
        )

    def _open_pr(
        self, title: str, args: dict, row: dict, steps: List[dict], out: dict
    ) -> bool:
        """POST /make-pr; True when it handed off to the browser (stop)."""
        payload: Dict[str, Any] = {}
        if args.get("base"):
            payload["base"] = args["base"]
        if args.get("pr_title"):
            payload["title"] = args["pr_title"]
        body = self._pr_body(args, row)
        if body:
            payload["body"] = body
        try:
            resp = self.api.post(
                self.api.inst_path(title, "/make-pr"), payload, timeout=PR_TIMEOUT_S
            )
        except client.ApiError as err:
            raise self._fail(
                "ship_session(%s): opening the PR failed: %s" % (title, err.message),
                steps,
            ) from None
        resp = resp if isinstance(resp, dict) else {}
        if resp.get("ok"):
            step = {"step": "pr", "state": "done", "url": resp.get("url")}
            if resp.get("note"):
                step["note"] = resp["note"]
            steps.append(step)
            out["pr_url"] = resp.get("url")
            return False
        steps.append(
            {
                "step": "pr",
                "state": "handoff",
                "url": resp.get("compare_url"),
                "message": resp.get("message"),
            }
        )
        out["pr_url"] = None
        out["hint"] = (
            "MindFlock could not open the PR itself (no gh CLI and no GitHub "
            "token); give your user the url, which opens a prefilled PR"
        )
        return True

    def _merge(self, title: str, steps: List[dict], out: dict) -> bool:
        """POST /merge-pr behind the UI's own gate; False when it handed off
        to the browser."""
        fresh = self._fresh_row(title)
        if fresh.get("stage") != "pr":
            raise self._fail(
                "ship_session(%s): there is no open PR to merge (stage %s)"
                % (title, fresh.get("stage") or "unknown"),
                steps,
            )
        ms = fresh.get("merge_state")
        if isinstance(ms, dict):
            checks = str(ms.get("checks") or "")
            if not ms.get("can_merge"):
                raise self._fail(
                    "ship_session(%s): the PR cannot be merged yet: %s"
                    % (title, "; ".join(ms.get("blockers") or ["blocked"])),
                    steps,
                    pr_url=fresh.get("pr_url"),
                )
            if checks == "failed":
                raise self._fail(
                    "ship_session(%s): CI failed on the PR; not merging" % title,
                    steps,
                    pr_url=fresh.get("pr_url"),
                )
            if checks == "pending":
                raise self._fail(
                    "ship_session(%s): CI is still running on the PR. Call again "
                    "later, or set_autopilot(depth=merge, confirm_merge=true) "
                    "to merge once it passes" % title,
                    steps,
                    pr_url=fresh.get("pr_url"),
                )
        try:
            resp = self.api.post(
                self.api.inst_path(title, "/merge-pr"), {}, timeout=PR_TIMEOUT_S
            )
        except client.ApiError as err:
            raise self._fail(
                "ship_session(%s): merge failed: %s" % (title, err.message),
                steps,
                pr_url=fresh.get("pr_url"),
            ) from None
        resp = resp if isinstance(resp, dict) else {}
        if resp.get("ok"):
            steps.append({"step": "merge", "state": "done", "url": fresh.get("pr_url")})
            return True
        steps.append(
            {
                "step": "merge",
                "state": "handoff",
                "url": resp.get("pr_url"),
                "message": resp.get("message"),
            }
        )
        out["hint"] = (
            "MindFlock could not merge the PR itself (no gh CLI and no GitHub "
            "token); give your user the url"
        )
        return False

    # -- set_autopilot ---------------------------------------------------------- #
    def set_autopilot(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        title = self._target(flock, args, "set_autopilot")
        depth = args["depth"]
        self.policy.require_ship(
            flock,
            title,
            "set_autopilot",
            merge=depth == "merge",
            confirm_merge=bool(args.get("confirm_merge")),
            depth=depth,
        )
        path = self.api.inst_path(title, "/fast-track")
        if depth == "off":
            try:
                resp = self.api.delete(path)
            except client.ApiError as err:
                raise self._api_error(err, "set_autopilot(%s)" % title) from None
            return {
                "title": title,
                "autopilot": None,
                "stopped": bool((resp or {}).get("stopped")),
            }
        payload: Dict[str, Any] = {"depth": depth}
        if flock.self_title:
            # Recorded on the lane: an agent's choice, which the agent may
            # change again — never mistaken for the user's.
            payload["by"] = "agent:" + flock.self_title
        for key in ("message", "base"):
            if args.get(key):
                payload[key] = args[key]
        try:
            resp = self.api.post(path, payload)
        except client.ApiError as err:
            raise self._api_error(err, "set_autopilot(%s)" % title) from None
        return {
            "title": title,
            "autopilot": (resp or {}).get("autopilot"),
            "hint": (
                "it ships itself once its agent's turn has ended and stayed idle; "
                "get_session(%r).autopilot shows progress, and a halt carries its "
                "reason" % title
            ),
        }

    # -- tickets ---------------------------------------------------------------- #
    def _ticket_listing(self) -> dict:
        try:
            resp = self.api.get("/api/tickets", timeout=90.0)
        except client.ApiError as err:
            raise self._api_error(err, "list_tickets") from None
        return resp if isinstance(resp, dict) else {}

    def list_tickets(self, args: dict, ctx: ToolContext) -> dict:
        data = self._ticket_listing()
        rows = [t for t in data.get("tickets") or [] if isinstance(t, dict)]
        source = str(args.get("source") or "").strip()
        if source:
            rows = [t for t in rows if str(t.get("source")) == source]
        query = str(args.get("query") or "").strip().lower()
        if query:
            rows = [
                t
                for t in rows
                if query in str(t.get("slug") or "").lower()
                or query == str(t.get("id") or "").lower()
                or query in str(t.get("name") or "").lower()
            ]
        if args.get("startable_only"):
            rows = [t for t in rows if not t.get("has_session")]
        limit = int(args.get("limit") or 30)
        out: Dict[str, Any] = {
            "tickets": [
                dict(
                    {k: t.get(k) for k in _TICKET_KEYS},
                    state=t.get("bucket"),
                )
                for t in rows[:limit]
            ],
            "more": max(0, len(rows) - limit),
            "sources": data.get("sources") or [],
        }
        if data.get("errors"):
            out["errors"] = data["errors"]
        if data.get("stale"):
            out["stale"] = True
        return out

    def _resolve_ticket(self, ref: str, source: str) -> Tuple[str, str, dict]:
        """``(source, id, listing row or {})`` for a ticket reference."""
        want = ref.strip()
        low = want.lower()
        try:
            data = self._ticket_listing()
        except ToolError:
            data = {}
        rows = [t for t in data.get("tickets") or [] if isinstance(t, dict)]
        hits = [
            t
            for t in rows
            if (not source or str(t.get("source")) == source)
            and low
            in {
                str(t.get("slug") or "").lower(),
                str(t.get("id") or "").lower(),
                str(t.get("session") or "").lower(),
                str(t.get("url") or "").lower(),
            }
        ]
        if len(hits) == 1:
            return str(hits[0]["source"]), str(hits[0]["id"]), hits[0]
        if len(hits) > 1:
            raise ToolError(
                "ticket %r matches tickets on several sources (%s); pass source"
                % (want, ", ".join(sorted({str(t.get("source")) for t in hits})))
            )
        if not source:
            raise ToolError(
                "ticket %r is not in the Intake ticket list (list_tickets); pass "
                "source plus the tracker's own id to start it anyway" % want
            )
        if re.match(r"^https?://", want):
            raise ToolError(
                "start a ticket that is not in list_tickets by its id, not its URL"
            )
        # Shortcut's slug is sc-<id>; the tracker fetches by the bare number.
        m = re.fullmatch(r"sc-(\d+)", low)
        return source, (m.group(1) if m else want), {}

    def spawn_ticket_session(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        me = flock.self_title
        self.policy.require_write(flock, "spawn_ticket_session")
        autopilot = args.get("autopilot") or "off"
        if autopilot == "merge" and not args.get("confirm_merge"):
            raise ToolError(
                "autopilot merge merges the worker's PR with no one looking: pass "
                "confirm_merge=true only when that was clearly asked for"
            )
        source, ticket_id, listed = self._resolve_ticket(
            str(args["ticket"]), str(args.get("source") or "").strip()
        )
        payload: Dict[str, Any] = {
            "source": source,
            "id": ticket_id,
            "spawned": True,
            "report_back": bool(args.get("report_back", True)),
            # The source's default rung is NOT applied to an agent's worker:
            # it reports back and its parent decides, unless asked otherwise.
            "depth": autopilot,
        }
        if me:
            payload["parent"] = me
        for key in ("agent", "effort", "note"):
            if args.get(key):
                payload[key] = args[key]
        try:
            resp = self.api.post("/api/tickets/start", payload, timeout=90.0)
        except client.ApiError as err:
            if err.status == 409 and "already exists" in err.message:
                raise ToolError(
                    "a session for this ticket already exists (%s). Message it, "
                    "or adopt it with set_parent if an agent spawned it" % err.message
                ) from None
            raise self._api_error(err, "spawn_ticket_session") from None
        resp = resp if isinstance(resp, dict) else {}
        title = str(resp.get("title") or "")
        if not title:
            raise ToolError("spawn_ticket_session: the server did not name the session")
        self.policy.spawned_by_me.add(title)
        self._note_dispatch(title)
        ready, row = False, None
        if args.get("wait_ready", True):
            ctx.start_progress(self.ticket_ready_wait_s)
            ready, row = self._await_ticket_row(title, ctx)
        out: Dict[str, Any] = {
            "title": title,
            "ticket": {
                "source": source,
                "id": ticket_id,
                "slug": listed.get("slug"),
                "name": listed.get("name"),
                "url": listed.get("url"),
            },
            "branch": (row or {}).get("branch") or resp.get("branch"),
            "program": resp.get("program"),
            "status": (row or {}).get("status") or "loading",
            "ready": ready,
            "report_back": bool(resp.get("report_back")),
            "autopilot": autopilot,
            "warnings": [
                "ticket sessions are provisioned from the ticket repository's "
                "base branch, not forked from your HEAD"
            ],
        }
        if resp.get("reason"):
            out["reason"] = resp["reason"]
        if "report_back" not in resp:
            out["warnings"].append(
                "this MindFlock server ignores parent/report-back on ticket "
                "starts (upgrade it): the session is not your child"
            )
        if not ready:
            out["hint"] = (
                "still provisioning (a first clone can take minutes); "
                "wait_for_session will wait for it"
            )
        return out

    def _await_ticket_row(
        self, title: str, ctx: ToolContext
    ) -> Tuple[bool, Optional[dict]]:
        deadline = self.monotonic() + self.ticket_ready_wait_s
        row: Optional[dict] = None
        while self.monotonic() < deadline:
            ctx.sleep(self.ready_poll_s)
            rows = self.api.instances(sleep=ctx.sleep)
            match = [r for r in rows if r.get("title") == title]
            if not match:
                self.policy.spawned_by_me.discard(title)
                why = self._create_failure(title)
                raise ToolError(
                    "ticket session %r failed to start: %s"
                    % (title, why or "it disappeared while provisioning")
                )
            row = match[0]
            if not row.get("pending") and str(row.get("status")) != "loading":
                return True, row
        return False, row
