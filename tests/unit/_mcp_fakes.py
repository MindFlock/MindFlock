"""Shared fakes for the MCP tool tests: an in-memory MindFlock API, a fake
clock, and a tool context whose sleeps advance that clock.

:class:`FakeFlockApi` subclasses the real :class:`backend.mcp.api.Api` and
overrides only ``request``, so every path the tools build (quoting, query
strings, the ``instances()`` helper) is the real one. The mailbox follows the
DESIGN §2 contract: states pending/delivered/read/held, ``unread`` =
pending|held, ``mark_read`` consumes, ``after`` = strictly newer than an id.
"""

from __future__ import annotations

import copy
import itertools
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from backend import client
from backend.mcp.api import Api
from backend.mcp.identity import Identity
from backend.mcp.policy import Policy
from backend.mcp.protocol import Cancelled
from backend.mcp.tools import Toolbox


def row(title: str, **kw: Any) -> dict:
    """A /api/instances row with sensible defaults."""
    base = {
        "title": title,
        "status": "running",
        "activity": "idle",
        "activity_since": 1000.0,
        "stage": "agent",
        "program": "claude",
        "provider": "claude",
        "branch": "mindflock/" + title,
        "folder": "/nonexistent/" + title,
        "path": "/nonexistent/repo",
        "repo": "repo",
        "tmux_name": "mindflock_" + title.replace(".", "_"),
        "parent": "",
        "spawned": False,
        "in_place": False,
        "provisioned": False,
        "last_turn": "",
        "diff_stat": {"files": 1, "additions": 2, "deletions": 0},
        "queue": {"pending": 0, "enabled": True, "loop": False},
    }
    base.update(kw)
    return base


class FakeClock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


class FakeCtx:
    """ToolContext stand-in: sleeping advances the clock and runs hooks."""

    def __init__(
        self, clock: FakeClock, on_sleep: Optional[Callable[[float], None]] = None
    ):
        self.clock = clock
        self.on_sleep = on_sleep
        self.slept: List[float] = []
        self.progress_total: Optional[float] = None
        #: raise Cancelled once more than this many sleeps happened
        self.cancel_after: Optional[int] = None
        #: raise Cancelled on the N-th check_cancelled() call (1-based)
        self.cancel_at_check: Optional[int] = None
        self.checks = 0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.cancel_after is not None and len(self.slept) > self.cancel_after:
            raise Cancelled()
        self.clock.now += seconds
        if self.on_sleep:
            self.on_sleep(self.clock.now)

    def check_cancelled(self) -> None:
        self.checks += 1
        if self.cancel_at_check is not None and self.checks >= self.cancel_at_check:
            raise Cancelled()
        if self.cancel_after is not None and len(self.slept) > self.cancel_after:
            raise Cancelled()

    def start_progress(self, total=None) -> None:
        self.progress_total = total


class FakeFlockApi(Api):
    """In-memory MindFlock API."""

    def __init__(self, rows: List[dict], clock: Optional[FakeClock] = None) -> None:
        super().__init__(host="127.0.0.1", port=8765, env={})
        self._base = "http://127.0.0.1:8765"
        self.clock = clock or FakeClock()
        self.rows = rows
        self.mail: Dict[str, List[dict]] = {}
        self.calls: List[tuple] = []
        self.config_payload: Dict[str, Any] = {
            "default_program": "claude",
            "caps": {
                "git": True,
                "agent_mcp": {"enabled": True, "providers": ["claude", "codex"]},
            },
        }
        self.outputs: Dict[str, dict] = {}
        #: title -> the dialog id GET /dialog serves (absent: 409 not waiting)
        self.dialogs: Dict[str, str] = {}
        self.diffs: Dict[str, dict] = {}
        self.errors: Dict[tuple, client.ApiError] = {}
        self.created_status = "loading"
        self.on_create: Optional[Callable[[dict], None]] = None
        self._ids = itertools.count(1)
        self.route_missing: set = set()
        #: title -> error text served by GET /api/create_failures
        self.create_failures: Dict[str, str] = {}

    # -- helpers for tests ---------------------------------------------------- #
    def deliver(
        self,
        to: str,
        frm: str,
        text: str = "hi",
        kind: str = "message",
        state: str = "held",
        ts: Optional[float] = None,
        data=None,
    ) -> dict:
        msg = {
            "id": "m%d" % next(self._ids),
            "kind": kind,
            "from": frm,
            "to": to,
            "text": text,
            "data": data,
            "ts": self.clock.now if ts is None else ts,
            "reply_to": None,
            "hop": 0,
            "delivery": "auto",
            "state": state,
            "delivered_ts": None,
            "read_ts": None,
            "detail": "",
        }
        self.mail.setdefault(to, []).append(msg)
        return msg

    def row(self, title: str) -> dict:
        return next(r for r in self.rows if r["title"] == title)

    def paths(self, method: Optional[str] = None) -> List[str]:
        return [p for (m, p, _) in self.calls if method is None or m == method]

    # -- the API -------------------------------------------------------------- #
    def request(
        self,
        method,
        path,
        payload=None,
        *,
        timeout=30.0,
        retry_s=5.0,
        text=False,
        sleep=None,
    ):
        self.calls.append((method, path, copy.deepcopy(payload)))
        parsed = urllib.parse.urlsplit(path)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        key = (method, parsed.path)
        if key in self.errors:
            raise self.errors[key]
        if parsed.path == "/api/config":
            return copy.deepcopy(self.config_payload)
        if parsed.path == "/api/create_failures":
            want = q.get("title")
            return {
                "failures": {
                    t: {"error": e, "ts": self.clock.now}
                    for t, e in self.create_failures.items()
                    if not want or t == want
                }
            }
        if parsed.path == "/api/instances":
            if method == "GET":
                return copy.deepcopy(self.rows)
            return self._create(payload)
        if parts[:2] != ["api", "instances"] or len(parts) < 3:
            raise client.ApiError(404, "{'detail': 'Not Found'}")
        title = parts[2]
        rest = "/".join(parts[3:])
        if rest in self.route_missing:
            raise client.ApiError(404, "{'detail': 'Not Found'}")
        known = any(r["title"] == title for r in self.rows)
        if not known:
            raise client.ApiError(404, "instance not found: %s" % title)
        if rest == "messages" and method == "GET":
            return self._get_messages(title, q)
        if rest == "messages" and method == "POST":
            return self._post_message(title, payload)
        if rest == "messages/read":
            ids = set(payload.get("ids") or [])
            n = 0
            for m in self.mail.get(title, []):
                if m["id"] in ids and m["state"] in ("pending", "held"):
                    m["state"] = "read"
                    n += 1
            return {"marked": n}
        if rest == "output":
            out = self.outputs.get(
                title,
                {
                    "view": q.get("view"),
                    "text": "reply of " + title,
                    "truncated": False,
                },
            )
            return copy.deepcopy(out)
        if rest == "history":
            return "## User\nhello\n\n## Claude\n" + "x" * 50
        if rest == "diff":
            return copy.deepcopy(
                self.diffs.get(
                    title,
                    {
                        "added": 0,
                        "removed": 0,
                        "content": "",
                        "error": None,
                        "base": q.get("base"),
                    },
                )
            )
        if rest == "dialog" and method == "GET":
            if title not in self.dialogs:
                raise client.ApiError(
                    409, "session is not waiting on a prompt (activity: idle)"
                )
            return {"id": self.dialogs[title], "parsed": True, "options": []}
        if rest == "answer":
            return {"ok": True, "activity_before": self.row(title)["activity"]}
        if rest == "close" or (method == "DELETE" and rest == ""):
            self.rows[:] = [r for r in self.rows if r["title"] != title]
            return {"ok": True}
        if rest == "parent":
            r = self.row(title)
            r["parent"] = payload.get("parent") or ""
            return copy.deepcopy(r)
        raise client.ApiError(404, "{'detail': 'Not Found'}")

    def _create(self, payload: dict) -> dict:
        title = payload.get("title")
        if any(r["title"] == title for r in self.rows):
            raise client.ApiError(409, "instance %s already exists" % title)
        new = row(
            title,
            status=self.created_status,
            activity="offline",
            activity_since=0,
            program=payload.get("program") or "claude",
            parent=payload.get("parent") or "",
            spawned=bool(payload.get("spawned")),
            folder="/nonexistent/wt/" + title,
        )
        self.rows.append(new)
        if self.on_create:
            self.on_create(payload)
        out = {
            k: v
            for k, v in new.items()
            if k not in ("activity", "activity_since", "queue")
        }
        return out

    def _get_messages(self, title: str, q: dict) -> dict:
        msgs = list(self.mail.get(title, []))
        unread_only = q.get("unread") == "1" or (
            q.get("unread") is None and q.get("include_consumed") != "1"
        )
        if unread_only:
            msgs = [m for m in msgs if m["state"] in ("pending", "held")]
        if q.get("after"):
            ids = [m["id"] for m in self.mail.get(title, [])]
            if q["after"] in ids:
                cut = ids.index(q["after"])
                allowed = set(ids[cut + 1 :])
                msgs = [m for m in msgs if m["id"] in allowed]
        if q.get("from") is not None and "from" in q:
            msgs = [m for m in msgs if m["from"] == q["from"]]
        if q.get("kind"):
            msgs = [m for m in msgs if m["kind"] == q["kind"]]
        msgs = msgs[: int(q.get("limit") or 50)]
        out = copy.deepcopy(msgs)
        if q.get("mark_read") == "1":
            for m in self.mail.get(title, []):
                if m["id"] in {x["id"] for x in msgs} and m["state"] in (
                    "pending",
                    "held",
                ):
                    m["state"] = "read"
        unread = sum(
            1 for m in self.mail.get(title, []) if m["state"] in ("pending", "held")
        )
        return {
            "messages": out,
            "unread": unread,
            "version": len(self.mail.get(title, [])),
        }

    def _post_message(self, title: str, payload: dict) -> dict:
        state = "held" if payload.get("delivery") == "inbox" else "pending"
        msg = self.deliver(
            title,
            payload.get("from", ""),
            payload["text"],
            kind=payload.get("kind") or "message",
            state=state,
            data=payload.get("data"),
        )
        msg["reply_to"] = payload.get("reply_to")
        msg["delivery"] = payload.get("delivery") or "auto"
        return {"message": copy.deepcopy(msg), "delivery": state}


def make_box(
    rows: List[dict],
    me: Optional[str] = "orch",
    scope: Optional[str] = None,
    managed_marker: bool = False,
    clock: Optional[FakeClock] = None,
):
    """(box, api, clock) with identity pinned to ``me`` via the env title."""
    clock = clock or FakeClock()
    api = FakeFlockApi(rows, clock)
    env = {}
    if me:
        env["MINDFLOCK_SESSION_TITLE"] = me
    if managed_marker:
        env["MINDFLOCK_MCP_MANAGED"] = "1"
    identity = Identity(env, run=_no_tmux)
    policy = Policy(scope, identity.managed)
    box = Toolbox(api, identity, policy, clock=clock.time, monotonic=clock.monotonic)
    return box, api, clock


def _no_tmux(*a, **k):  # pragma: no cover — identity tests patch their own
    raise AssertionError(
        "tmux must not be consulted when MINDFLOCK_SESSION_TITLE is set"
    )
