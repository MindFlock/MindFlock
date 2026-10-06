"""Who may do what to which session — the MCP server's guard-rail.

This is a GUARD-RAIL, not a security boundary: every agent runs as the same
OS user and can reach the same HTTP API with ``curl``. Its job is to keep a
well-meaning agent inside its own subtree and to make the destructive paths
deliberate.

Scopes (weakest first): ``readonly`` (read tools + its own inbox) <
``children`` (default) < ``all``. Picked by ``--scope`` /
``MINDFLOCK_MCP_SCOPE``; a MindFlock-attached server that cannot tell which
session it is in drops to ``readonly`` (fail closed), and an unparseable scope
value does too.

"Managed" — what a session may steer (delivery ``now``, answer_prompt,
kill_session, set_parent):

* scope ``all``: any local session;
* scope ``children`` with an identity: transitive descendants via the live
  ``parent`` chain;
* scope ``children`` without one (an external client): sessions spawned by
  THIS server process, plus their descendants;
* never itself, never a remote ``device::title`` row, never a pending row.

Shipping (ship_session, set_autopilot) is "managed" plus the caller's OWN
session; a merge additionally needs ``confirm_merge`` and, under
``children``, a target that is a descendant (never the caller itself).

Messaging (delivery ``auto``/``inbox``) to any local session is allowed in
every scope but ``readonly``.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from backend.mcp.identity import is_local_row
from backend.mcp.protocol import ToolError

__all__ = ["SCOPES", "DEFAULT_SCOPE", "Flock", "Policy", "parse_scope"]

SCOPES = ("readonly", "children", "all")
DEFAULT_SCOPE = "children"
_MAX_CHAIN = 64  # parent-chain walk bound (the server refuses cycles; belt+braces)


def parse_scope(value: Optional[str]) -> Optional[str]:
    """Normalize a scope string; ``None`` for an unknown non-empty value
    ("" / None → the default)."""
    v = (value or "").strip().lower()
    if not v:
        return DEFAULT_SCOPE
    return v if v in SCOPES else None


class Flock:
    """One ``/api/instances`` listing with lineage helpers.

    ``parent`` is read lazily-valid: a parent that is not a live local title
    counts as no parent (the server applies the same rule; repeated here so an
    older server's dangling value can't grant anything)."""

    def __init__(self, rows: Iterable[dict], self_title: Optional[str] = None):
        self.rows: List[dict] = [r for r in rows if isinstance(r, dict)]
        self.by_title: Dict[str, dict] = {}
        for r in self.rows:
            t = str(r.get("title") or "")
            if t and t not in self.by_title:
                self.by_title[t] = r
        self.local: Dict[str, dict] = {
            t: r for t, r in self.by_title.items() if is_local_row(r)
        }
        self.self_title = self_title if self_title in self.local else None

    def row(self, title: str) -> Optional[dict]:
        return self.by_title.get(title)

    def parent_of(self, title: str) -> str:
        r = self.local.get(title)
        p = str((r or {}).get("parent") or "")
        return p if p in self.local and p != title else ""

    def children_of(self, title: str) -> List[str]:
        return [t for t in self.local if self.parent_of(t) == title]

    def ancestors_of(self, title: str) -> List[str]:
        out: List[str] = []
        cur = self.parent_of(title)
        while cur and cur not in out and len(out) < _MAX_CHAIN:
            out.append(cur)
            cur = self.parent_of(cur)
        return out

    def is_descendant(self, title: str, ancestor: str) -> bool:
        return bool(ancestor) and ancestor in self.ancestors_of(title)

    def descendants_of(self, title: str) -> List[str]:
        return [t for t in self.local if self.is_descendant(t, title)]

    def siblings_of(self, title: str) -> List[str]:
        p = self.parent_of(title)
        if not p:
            return []
        return [t for t in self.children_of(p) if t != title]


class Policy:
    """Scope + managed-set decisions. ``spawned_by_me`` is the in-memory set of
    titles this process created (what an external client manages)."""

    def __init__(self, configured: Optional[str], identity_managed: bool) -> None:
        parsed = parse_scope(configured)
        self.configured = parsed or "readonly"
        self.invalid_scope = parsed is None
        self.identity_managed = identity_managed
        self.spawned_by_me: Set[str] = set()

    def scope(self, self_title: Optional[str]) -> str:
        """The effective scope for this call: fail closed to ``readonly`` when
        MindFlock attached us but we can't say which session we are."""
        if self.identity_managed and not self_title:
            return "readonly"
        return self.configured

    def scope_note(self, self_title: Optional[str]) -> str:
        """Why the effective scope is narrower than configured ("" if not)."""
        if self.invalid_scope:
            return "unknown scope value; running readonly"
        if self.identity_managed and not self_title:
            return (
                "MindFlock attached this server but it cannot tell which session "
                "it runs in, so it is readonly"
            )
        return ""

    # -- predicates ---------------------------------------------------------- #
    def is_managed(self, flock: Flock, target: str) -> bool:
        if target not in flock.local:
            return False
        me = flock.self_title
        if me and target == me:
            return False
        scope = self.scope(me)
        if scope == "all":
            return True
        if scope != "children":
            return False
        if me:
            return flock.is_descendant(target, me)
        if target in self.spawned_by_me:
            return True
        return any(a in self.spawned_by_me for a in flock.ancestors_of(target))

    # -- guards (raise ToolError with a fix-it message) ------------------------ #
    def require_write(self, flock: Flock, action: str) -> None:
        if self.scope(flock.self_title) == "readonly":
            note = self.scope_note(flock.self_title)
            raise ToolError(
                "%s is not allowed: this MindFlock MCP server is in readonly scope%s."
                % (action, " (" + note + ")" if note else "")
            )

    def require_local(self, flock: Flock, title: str) -> dict:
        row = flock.row(title)
        if row is None:
            raise ToolError(
                "no session named %r (call list_sessions to see the titles)" % title
            )
        if "::" in title:
            raise ToolError(
                "%r is a session on another device; remote sessions are read-only "
                "here" % title
            )
        if row.get("pending"):
            raise ToolError("%r has not been created yet (still pending)" % title)
        return row

    def require_message(self, flock: Flock, title: str) -> dict:
        self.require_write(flock, "messaging")
        row = self.require_local(flock, title)
        if flock.self_title and title == flock.self_title:
            raise ToolError("you cannot message yourself")
        return row

    def require_managed(self, flock: Flock, title: str, action: str) -> dict:
        self.require_write(flock, action)
        row = self.require_local(flock, title)
        if flock.self_title and title == flock.self_title:
            raise ToolError("%s is never allowed on your own session" % action)
        if not self.is_managed(flock, title):
            raise ToolError(self._unmanaged_message(flock, title, action))
        return row

    def _unmanaged_message(self, flock: Flock, title: str, action: str) -> str:
        scope = self.scope(flock.self_title)
        if flock.self_title:
            return (
                "%s needs %r to be one of your descendants (scope %s); it is not. "
                "Message it with send_message instead, or ask the user."
                % (action, title, scope)
            )
        return (
            "%s needs %r to be a session this MCP server spawned (scope %s, no "
            "session identity). Start the server with --scope all to manage "
            "other sessions." % (action, title, scope)
        )

    def require_ship(
        self,
        flock: Flock,
        title: str,
        action: str,
        merge: bool = False,
        confirm_merge: bool = False,
        depth: str = "",
    ) -> dict:
        """ship_session / set_autopilot: the target must be the caller itself
        or a session it manages (never remote, never pending). ``merge`` —
        the one step that cannot be undone — also needs ``confirm_merge`` and
        either scope ``all`` or a target that is the caller's own descendant
        (under ``children`` a session may not merge ITSELF: that is its
        parent's or the user's call).

        Whatever the scope, an agent never ships PAST what the user chose:
        a team run's member or lead ships only with its group; a lane the
        user set with "ask me before it ships" waits for the user's own go;
        and ``depth`` may not go further than a lane the user set (``"off"``
        — stopping — is always allowed)."""
        self.require_write(flock, action)
        row = self.require_local(flock, title)
        self.require_user_lane(row, title, action, depth)
        me = flock.self_title
        is_self = bool(me) and title == me
        if not is_self and not self.is_managed(flock, title):
            if me:
                raise ToolError(
                    "%s needs %r to be you or one of your descendants (scope %s); "
                    "it is not. Ask its parent or the user to ship it."
                    % (action, title, self.scope(me))
                )
            raise ToolError(self._unmanaged_message(flock, title, action))
        if merge:
            if not confirm_merge:
                raise ToolError(
                    "merging is the one step that cannot be undone: pass "
                    "confirm_merge=true once the user (or your task) clearly "
                    "asked for a merge; otherwise stop at depth pr"
                )
            if is_self and self.scope(me) != "all":
                raise ToolError(
                    "merging your own session needs scope all; stop at depth pr "
                    "and let your parent or the user merge it"
                )
        return row

    @staticmethod
    def require_user_lane(row: dict, title: str, action: str, depth: str) -> None:
        """Refuse a ship that would override the user's own choice for
        ``title`` (see :meth:`require_ship`)."""
        run = row.get("run") if isinstance(row.get("run"), dict) else None
        if run and run.get("id"):
            raise ToolError(
                "%s refused: %r is %s group %r — MindFlock ships it with the "
                "group, and your user steers the group"
                % (
                    action,
                    title,
                    "the lead of" if run.get("role") == "lead" else "part of",
                    run.get("name") or run.get("id"),
                )
            )
        if depth in ("", "off"):
            return
        lane = row.get("lane") if isinstance(row.get("lane"), dict) else None
        if not lane or str(lane.get("by") or "user") != "user":
            return
        if lane.get("ask_first"):
            raise ToolError(
                "%s refused: your user set %r to ask them before it ships — "
                "only they can approve it (MindFlock's Outbox)" % (action, title)
            )
        order = ("leave", "commit", "push", "pr", "merge")
        want = {"agent": "leave", "off": "leave"}.get(depth, depth)
        have = str(lane.get("target") or "")
        if have in order and want in order and order.index(want) > order.index(have):
            raise ToolError(
                "%s refused: your user set %r to stop at %s; depth %s would go "
                "further — ask them" % (action, title, have, depth)
            )

    def require_set_parent(self, flock: Flock, target: str, new_parent: str) -> dict:
        """Adopt / re-parent / detach rules (see set_parent's description)."""
        self.require_write(flock, "set_parent")
        row = self.require_local(flock, target)
        me = flock.self_title
        if me and target == me:
            raise ToolError("set_parent is never allowed on your own session")
        if new_parent:
            self.require_local(flock, new_parent)
            if new_parent == target or flock.is_descendant(new_parent, target):
                raise ToolError(
                    "making %r the parent of %r would create a cycle"
                    % (new_parent, target)
                )
        scope = self.scope(me)
        if scope == "all":
            return row
        # children scope: the target must already be ours, or be an orphaned
        # agent-spawned session we are adopting.
        managed = self.is_managed(flock, target)
        if not managed:
            if not row.get("spawned"):
                raise ToolError(
                    "only sessions created by an agent (spawned) can be adopted; "
                    "%r was created by the user. The user can re-parent it, or "
                    "run the server with --scope all." % target
                )
            if flock.parent_of(target):
                raise ToolError(
                    "%r already has a parent (%r) outside your tree; it can only be "
                    "adopted once orphaned" % (target, flock.parent_of(target))
                )
        # The new parent must keep the session inside our own tree.
        if new_parent and new_parent != me and not self.is_managed(flock, new_parent):
            raise ToolError(
                "the new parent %r must be you or one of your descendants" % new_parent
            )
        if not new_parent and not managed:
            raise ToolError("%r has no parent to detach from" % target)
        return row
