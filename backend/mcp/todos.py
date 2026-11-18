"""The To-do tools: ``add_todo``, ``list_todos`` and ``update_todo``.

A mixin of :class:`backend.mcp.tools.Toolbox` (kept apart for size) over the
To-do extension's REST API (``/api/todo``…): ONE list per person of what they
must do by hand — a manual test step, a click, a login, a decision — across
every session they run, each entry naming the session it came from.

Adding to the person's list is talking to the PERSON, not steering a
session, so ``add_todo`` (and the read ``list_todos``) work in every scope,
``readonly`` included, and from an external client (no session to stamp).
``update_todo`` edits only what the caller may answer for: its own items, its
descendants' and items with no session — any item under scope ``all``.

Peer mode (:mod:`backend.mcp.peer_tools`) never serves these: a peer's agent
does not reach the owner's list.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from backend import client
from backend.mcp.policy import Flock
from backend.mcp.protocol import ToolContext, ToolError

_log = logging.getLogger(__name__)

__all__ = [
    "TodoTools",
    "KINDS",
    "PRIORITIES",
    "MAX_ADD",
    "DETAILS_PREVIEW",
]

KINDS = ("task", "manual_test", "needs_you")
PRIORITIES = ("high", "normal", "low")
#: Items one add_todo call may carry (the REST route takes 50).
MAX_ADD = 20
MAX_TEXT = 500
MAX_DETAILS = 20000
#: How much of an item's details a list_todos row shows.
DETAILS_PREVIEW = 300
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500
_DUE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?")

D_ADD = (
    "Put an item on your person's To-do list: the ONE place they collect "
    "what they must do by hand across all their sessions. They switch between "
    "many sessions and forget small manual steps, and a line in chat scrolls "
    "away, so saying it in chat is NOT enough. Call this whenever the person "
    "has to act themselves: a manual test step, a click in a product, a login "
    "or credential, a decision, a follow-up after merge. One item per action. "
    'text: the one-line action ("Log in to staging as admin, open Billing"). '
    "details: everything needed to do it without re-reading this chat: exact "
    "commands, URLs and values, copy-paste ready, and what they should see. "
    'kind: "manual_test" for a test step, "needs_you" for anything else that '
    'blocks on them, "task" (default) otherwise. priority: high | normal '
    '(default) | low. due: "YYYY-MM-DDTHH:MM" local time (a reminder). link: '
    "a URL to open. Several at once: items=[{text, details, kind, ...}] (max "
    "20). Each item is stamped with your session, so one click takes the "
    "person back here. Works in every scope. Returns the ids: "
    "update_todo(id, done=true) once it is done or moot; list_todos first to "
    "avoid adding the same step twice."
)
D_LIST = (
    "Read your person's To-do list. Compact rows: id, text, done, kind, "
    "priority, session (where it came from), due, a details preview "
    "(has_more_details: it was cut; pass id= for the full item). mine "
    "(default true inside a session): only items your session added; false: "
    "everyone's. include_done: also checked-off items (default false). Check "
    "it before add_todo so the same step is not added twice."
)
D_UPDATE = (
    "Change a To-do item by id: done=true checks it off (false reopens it), "
    "text, details, priority (high | normal | low). You may change your own "
    "items, your descendants' items and items with no session (any item with "
    "scope all). Check off an item you added once it is done or no longer "
    "applies; never one the person still has to do."
)


def _obj(props: dict, required: tuple = ()) -> dict:
    out: Dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


_TEXT = {"type": "string", "minLength": 1, "maxLength": MAX_TEXT}
_DETAILS = {"type": "string", "maxLength": MAX_DETAILS}
_PRIORITY = {"type": "string", "enum": list(PRIORITIES)}
_ITEM_PROPS = {
    "text": dict(_TEXT, description="the one-line action"),
    "details": dict(
        _DETAILS,
        description="everything needed to do it: commands, URLs, values, "
        "what to expect",
    ),
    "kind": {"type": "string", "enum": list(KINDS)},
    "priority": _PRIORITY,
    "due": {
        "type": "string",
        "maxLength": 16,
        "description": "YYYY-MM-DDTHH:MM, local time",
    },
    "link": {"type": "string", "maxLength": 2000},
}
S_ADD = _obj(
    dict(
        _ITEM_PROPS,
        items={
            "type": "array",
            "items": _obj(_ITEM_PROPS, ("text",)),
            "minItems": 1,
            "maxItems": MAX_ADD,
            "description": "several items in one call",
        },
    )
)
S_LIST = _obj(
    {
        "mine": {"type": "boolean"},
        "include_done": {"type": "boolean"},
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT},
    }
)
S_UPDATE = _obj(
    {
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "done": {"type": "boolean"},
        "text": _TEXT,
        "details": _DETAILS,
        "priority": _PRIORITY,
    },
    ("id",),
)


def _item_payload(raw: Any, where: str, session: Optional[str]) -> dict:
    """One add_todo item → the POST body fields: defaults sent as absent,
    the caller's session and ``source: "agent"`` stamped."""
    if not isinstance(raw, dict):
        raise ToolError("%s must be an object with text" % where)
    text = " ".join(str(raw.get("text") or "").split())
    if not text:
        raise ToolError("%s needs text: the one-line action" % where)
    if len(text) > MAX_TEXT:
        raise ToolError(
            "%s text is %d characters; keep it to one line (limit %d) and put "
            "the rest in details" % (where, len(text), MAX_TEXT)
        )
    out: Dict[str, Any] = {"text": text}
    details = str(raw.get("details") or "").strip()
    if details:
        if len(details) > MAX_DETAILS:
            raise ToolError(
                "%s details is %d characters; the limit is %d"
                % (where, len(details), MAX_DETAILS)
            )
        out["details"] = details
    kind = str(raw.get("kind") or "task")
    if kind not in KINDS:
        raise ToolError("%s kind must be one of %s" % (where, ", ".join(KINDS)))
    if kind != "task":
        out["kind"] = kind
    priority = str(raw.get("priority") or "normal")
    if priority not in PRIORITIES:
        raise ToolError(
            "%s priority must be one of %s" % (where, ", ".join(PRIORITIES))
        )
    if priority != "normal":
        out["priority"] = priority
    due = str(raw.get("due") or "").strip()
    if due:
        if not _DUE_RE.fullmatch(due):
            raise ToolError(
                '%s due must be "YYYY-MM-DDTHH:MM" (local time), got %r' % (where, due)
            )
        out["due"] = due
    link = str(raw.get("link") or "").strip()
    if link:
        out["link"] = link
    if session:
        out["session"] = session
    out["source"] = "agent"
    return out


def _brief(item: Any) -> Dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    return {"id": str(item.get("id") or ""), "text": str(item.get("text") or "")}


def _row(item: dict, full: bool = False) -> Dict[str, Any]:
    """A compact list_todos row (``full``: details uncut)."""
    out: Dict[str, Any] = {
        "id": str(item.get("id") or ""),
        "text": str(item.get("text") or ""),
        "done": bool(item.get("done")),
        "kind": str(item.get("kind") or "task"),
        "priority": str(item.get("priority") or "normal"),
    }
    for key in ("session", "due"):
        if item.get(key):
            out[key] = str(item[key])
    details = str(item.get("details") or "")
    if details:
        if full or len(details) <= DETAILS_PREVIEW:
            out["details"] = details
            out["has_more_details"] = False
        else:
            out["details"] = details[:DETAILS_PREVIEW].rstrip() + "…"
            out["has_more_details"] = True
    if full:
        for key in ("source", "link", "created_at", "done_at"):
            if item.get(key):
                out[key] = item[key]
    return out


class TodoTools:
    """The three handlers; mixed into ``Toolbox``."""

    # -- helpers ---------------------------------------------------------------- #
    def _todo_items(self) -> tuple:
        """``(items, counts)`` from ``GET /api/todo``."""
        try:
            resp = self.api.get("/api/todo")
        except client.ApiError as err:
            raise self._todo_error(err, "reading the To-do list") from None
        if not isinstance(resp, dict):
            return [], {}
        items = [i for i in resp.get("todos") or [] if isinstance(i, dict)]
        counts = resp.get("counts") if isinstance(resp.get("counts"), dict) else {}
        return items, counts

    def _todo_error(self, err: client.ApiError, what: str) -> ToolError:
        # FastAPI's own 404 (no such route) — not the store's {"error": …}.
        if err.status == 404 and "Not Found" in (err.message or ""):
            return ToolError(
                "%s failed: this MindFlock server has no To-do list (/api/todo); "
                "turn on the To-do extension or upgrade the server" % what
            )
        return ToolError("%s failed: %s" % (what, err.message))

    def _may_update_todo(self, flock: Flock, item: dict) -> None:
        """Refuse an update of an item the caller does not answer for."""
        me = flock.self_title
        if self.policy.scope(me) == "all":
            return
        owner = str(item.get("session") or "")
        if not owner:
            return
        if me and (owner == me or flock.is_descendant(owner, me)):
            return
        if not me and self.policy.is_managed(flock, owner):
            return
        raise ToolError(
            "to-do %r belongs to session %r, which is not %s; leave it to that "
            "session or the person (scope all may change any item)"
            % (
                str(item.get("id") or ""),
                owner,
                "you or one of your descendants" if me else "a session you spawned",
            )
        )

    # -- handlers --------------------------------------------------------------- #
    def add_todo(self, args: dict, ctx: ToolContext) -> dict:
        raws: List[Any] = []
        top = {
            k: args[k]
            for k in ("text", "details", "kind", "priority", "due", "link")
            if args.get(k) not in (None, "")
        }
        if top:
            raws.append(top)
        extra = args.get("items") or []
        if not isinstance(extra, list):
            raise ToolError("items must be a list of {text, details, ...}")
        raws.extend(extra)
        if not raws:
            raise ToolError(
                "add_todo needs text (one item) or items=[{text, ...}] (several)"
            )
        if len(raws) > MAX_ADD:
            raise ToolError(
                "%d items in one call; the limit is %d: split them"
                % (len(raws), MAX_ADD)
            )
        # Talking to the person, not steering a session: no scope check.
        session = self.flock().self_title
        many = len(raws) > 1
        items = [
            _item_payload(r, "items[%d]" % i if many else "the item", session)
            for i, r in enumerate(raws)
        ]
        body = {"items": items} if many else items[0]
        try:
            resp = self.api.post("/api/todo/items", body)
        except client.ApiError as err:
            raise self._todo_error(err, "add_todo") from None
        resp = resp if isinstance(resp, dict) else {}
        out: Dict[str, Any] = {
            "added": [_brief(i) for i in resp.get("added") or []],
            "updated": [_brief(i) for i in resp.get("updated") or []],
        }
        if session:
            out["session"] = session
        counts = resp.get("counts")
        if isinstance(counts, dict) and counts:
            out["counts"] = counts
        return out

    def list_todos(self, args: dict, ctx: ToolContext) -> dict:
        flock = self.flock()
        me = flock.self_title
        items, counts = self._todo_items()
        want = str(args.get("id") or "").strip()
        if want:
            for item in items:
                if str(item.get("id") or "") == want:
                    return {"todo": _row(item, full=True)}
            raise ToolError(
                "no to-do %r on the list (it may be in the history: checked off "
                "long ago or removed)" % want
            )
        mine_arg = args.get("mine")
        mine = bool(me) if mine_arg is None else bool(mine_arg)
        out: Dict[str, Any] = {}
        if mine and not me:
            mine = False
            out["note"] = "mine needs a session identity; showing everyone's"
        if not args.get("include_done"):
            items = [i for i in items if not i.get("done")]
        if mine:
            items = [i for i in items if str(i.get("session") or "") == me]
        limit = int(args.get("limit") or DEFAULT_LIST_LIMIT)
        out.update(
            {
                "mine": mine,
                "todos": [_row(i) for i in items[:limit]],
                "more": max(0, len(items) - limit),
            }
        )
        if counts:
            out["counts"] = counts
        return out

    def update_todo(self, args: dict, ctx: ToolContext) -> dict:
        todo_id = str(args.get("id") or "").strip()
        if not todo_id:
            raise ToolError("update_todo needs id (from add_todo or list_todos)")
        change: Dict[str, Any] = {}
        if args.get("done") is not None:
            change["done"] = bool(args["done"])
        if args.get("text") is not None:
            text = " ".join(str(args["text"]).split())
            if not text:
                raise ToolError("text must not be empty")
            change["text"] = text
        if args.get("details") is not None:
            change["details"] = str(args["details"]).strip()
        if args.get("priority") is not None:
            priority = str(args["priority"])
            if priority not in PRIORITIES:
                raise ToolError("priority must be one of %s" % ", ".join(PRIORITIES))
            change["priority"] = priority
        if not change:
            raise ToolError("nothing to change: pass done, text, details or priority")
        flock = self.flock()
        items, _ = self._todo_items()
        item = next((i for i in items if str(i.get("id") or "") == todo_id), None)
        if item is None:
            raise ToolError(
                "no to-do %r on the list (call list_todos for the ids)" % todo_id
            )
        self._may_update_todo(flock, item)
        path = "/api/todo/items/" + urllib.parse.quote(todo_id, safe="")
        try:
            resp = self.api.patch(path, change)
        except client.ApiError as err:
            raise self._todo_error(err, "update_todo(%s)" % todo_id) from None
        todo = resp.get("todo") if isinstance(resp, dict) else None
        return {"todo": _row(todo if isinstance(todo, dict) else dict(item, **change))}
