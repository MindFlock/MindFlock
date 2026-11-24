"""The ``report_issue`` tool: draft a report about MindFlock itself for the
person to send (docs/mcp.md ``report_issue``).

A mixin of :class:`backend.mcp.tools.Toolbox`. The tool only writes a DRAFT
on the server (``POST /api/feedback/drafts``, :mod:`backend.web.core.feedback`):
the app shows it in the feedback box and the person reads it, edits it and
presses Send. An agent can never file the issue by itself — it becomes a
public GitHub issue under the person's name.
"""

from __future__ import annotations

from typing import Any, Dict

from backend import client
from backend.mcp.protocol import ToolContext, ToolError

__all__ = ["FeedbackTools", "D_REPORT_ISSUE", "S_REPORT_ISSUE"]

D_REPORT_ISSUE = (
    "Draft a report about MindFlock ITSELF (the app, its CLI, this MCP) for "
    "the MindFlock developers: a bug the user hit, or an idea or feedback "
    "they voiced. Use it when the user asks to report it, or offer it when "
    "they are clearly stuck on a MindFlock fault; not for problems in the "
    "user's own project or in the agent CLI. It only DRAFTS: the app opens "
    "the draft for the user, who reads, edits and sends it; nothing is "
    "posted by you. Sent, it is a PUBLIC GitHub issue on MindFlock/MindFlock, "
    "so write it for strangers: title = one specific line; body = what "
    "happened, what was expected, steps to reproduce, exact error text. "
    "Never include the user's code, repository or company names, ticket "
    "contents, file paths, host names, tokens or anything else private: "
    "describe the situation in general words. MindFlock adds its version "
    "and OS. kind: bug (default) | idea | feedback. Returns {drafted, id, "
    "tell}: say `tell` to the user."
)

S_REPORT_ISSUE: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 4,
            "maxLength": 200,
            "description": "one specific line, no private names",
        },
        "body": {
            "type": "string",
            "minLength": 10,
            "maxLength": 20000,
            "description": "markdown: what happened, what was expected, steps, "
            "exact error text; nothing private",
        },
        "kind": {
            "type": "string",
            "enum": ["bug", "idea", "feedback"],
            "description": "bug (default), idea or feedback",
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}


class FeedbackTools:
    """``report_issue`` (mixed into :class:`Toolbox`)."""

    def report_issue(self, args: dict, ctx: ToolContext) -> Any:
        # Talking to the person, not steering a session: no scope check
        # (like add_todo).
        flock = self.flock()
        body = {
            "title": args.get("title") or "",
            "body": args.get("body") or "",
            "kind": args.get("kind") or "bug",
            "session": flock.self_title or "",
        }
        try:
            rec = self.api.post("/api/feedback/drafts", body) or {}
        except client.ApiError as err:
            if err.status in (404, 405):
                raise ToolError(
                    "this MindFlock server has no feedback box yet; update "
                    "it, or the user can open "
                    "https://github.com/MindFlock/MindFlock/issues/new"
                ) from None
            raise ToolError("report_issue: %s" % err.message) from None
        out: Dict[str, Any] = {
            "drafted": True,
            "id": rec.get("id"),
            "title": rec.get("title"),
            "tell": (
                "I drafted the report. It is waiting in MindFlock (? Help → "
                "Send feedback…, at the foot of the sidebar) for you to read "
                "and send; nothing is posted until you press Send."
            ),
        }
        if rec.get("removed"):
            out["removed"] = rec["removed"]
            out["note"] = (
                "%d path(s) or key-like value(s) were replaced in the draft"
                % rec["removed"]
            )
        return out
