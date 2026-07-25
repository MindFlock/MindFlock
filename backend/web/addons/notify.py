"""Notify addon: desktop notifications for session events (roadmap B5).

The reference implementation of the *generic* extension path — manifest →
``core/slots.js`` imports the descriptor's ``module`` → the module registers
itself in ``window.mindflockAddons`` → ``init(ctx)`` subscribes to the client
event bus (``window.mindflock.events``). See docs/extensions.md.

What it does: shows a browser Notification when a session needs your input
(activity → ``clarify``) or its open PR leaves the review stage (merged /
closed). The event → notification rules live server-side (``GET
/api/notify/config``) so the backend stays the one source of truth for which
transitions matter; ``static/addons/notify.js`` applies them client-side, where
the Notification API (and its permission prompt) lives.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.config import settings as settings_store

from .base import Addon, AppContext, FrontendDescriptor

#: Event → notification rules the frontend module applies. ``old``/``new`` of
#: ``None`` match any value; ``{session}`` in title/body is replaced with the
#: envelope's session title. Each rule has a stable ``id``, a short ``label`` for
#: the settings UI, and a ``default_enabled`` flag: default-on rules are opt-out
#: (muted via settings.notifications.muted_rules); default-off rules are opt-in
#: (turned on via settings.notifications.enabled_rules) because they're noisy.
NOTIFY_RULES: List[dict] = [
    {
        "id": "needs_input",
        "label": "A session needs your input",
        "event": "session.activity_changed",
        "old": None,
        "new": "clarify",
        "title": "{session} needs your input",
        "body": "The agent is waiting on a clarification.",
        "default_enabled": True,
    },
    {
        # There is no dedicated "merged" event: an open PR sets stage "pr", and
        # merging/closing it moves the stage off "pr" (usually to "pushed").
        "id": "pr_closed",
        "label": "A pull request is merged or closed",
        "event": "session.stage_changed",
        "old": "pr",
        "new": None,
        "title": "{session}: PR merged or closed",
        "body": "The pull request left the open-review stage.",
        "default_enabled": True,
    },
    {
        # J5 — cost guardrail: emitted once per session when its estimated
        # cost crosses the configured per-session budget (data: {cost, budget}).
        "id": "budget_exceeded",
        "label": "A session exceeds its cost budget",
        "event": "session.budget_exceeded",
        "old": None,
        "new": None,
        "title": "{session} exceeded its cost budget",
        "body": "The session's estimated cost crossed its configured budget.",
        "default_enabled": True,
    },
    {
        # Opt-in (noisy): fires whenever a session finishes its turn and goes
        # idle — useful for babysitting a long autonomous run.
        "id": "session_idle",
        "label": "A session finishes and goes idle",
        "event": "session.activity_changed",
        "old": None,
        "new": "idle",
        "title": "{session} is idle",
        "body": "The agent finished its turn and is waiting.",
        "default_enabled": False,
    },
    {
        # Opt-in (noisy): the commit hit the pre-commit lock — hooks are running
        # (stage -> "precommit"). See server._session_stage.
        "id": "precommit_running",
        "label": "Pre-commit hooks are running",
        "event": "session.stage_changed",
        "old": None,
        "new": "precommit",
        "title": "{session}: pre-commit hooks running",
        "body": "A commit triggered the pre-commit hooks.",
        "default_enabled": False,
    },
    {
        # Default-on (actionable, not noisy): a commit was blocked by the
        # pre-commit hooks and the tree is still dirty, so the session parks in
        # the "interrupt" stage (see server._session_stage). This is the failure
        # counterpart to precommit_running — it fires once per block, and the
        # session needs a re-commit to move on.
        "id": "precommit_failed",
        "label": "Pre-commit hooks failed a commit",
        "event": "session.stage_changed",
        "old": None,
        "new": "interrupt",
        "title": "{session}: pre-commit hooks failed",
        "body": "A commit was blocked by the pre-commit hooks — re-commit to continue.",
        "default_enabled": True,
    },
]


def _rule_enabled(rule: dict, muted: set, opted_in: set) -> bool:
    """Whether ``rule`` is active given the muted (opt-out) and enabled (opt-in)
    id sets. Default-on rules fire unless muted; default-off rules fire only when
    explicitly opted in."""
    if rule.get("default_enabled", True):
        return rule["id"] not in muted
    return rule["id"] in opted_in


def _rules_with_state() -> List[dict]:
    """NOTIFY_RULES each tagged with its current ``enabled`` flag from settings.

    ``default_enabled`` is an internal field (opt-in vs opt-out semantics) and is
    NOT exposed to the client — the frontend only needs the resolved ``enabled``.
    """
    try:
        notifs = settings_store.load_settings().notifications
        muted = set(notifs.muted_rules)
        opted_in = set(notifs.enabled_rules)
    except Exception:  # noqa: BLE001 — settings must never break the config read
        muted, opted_in = set(), set()
    return [
        {
            **{k: v for k, v in rule.items() if k != "default_enabled"},
            "enabled": _rule_enabled(rule, muted, opted_in),
        }
        for rule in NOTIFY_RULES
    ]


class NotifyAddon(Addon):
    id = "notify"
    label = "Notifications"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    # --- routes ----------------------------------------------------------- #
    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/notify")

        @router.get("/config")
        def get_config() -> JSONResponse:
            """The notification rules the frontend subscribes with, each tagged
            with its current ``enabled`` state so the client skips muted ones."""
            return JSONResponse({"rules": _rules_with_state()})

        @router.post("/rules/{rule_id}")
        def set_rule(rule_id: str, payload: dict) -> JSONResponse:
            """Turn one rule on/off. Default-on rules persist as an opt-out
            (``muted_rules``); default-off rules persist as an opt-in
            (``enabled_rules``). Unknown ids are rejected."""
            rule = next((r for r in NOTIFY_RULES if r["id"] == rule_id), None)
            if rule is None:
                return JSONResponse(
                    {"error": f"unknown rule: {rule_id}"}, status_code=404
                )
            enabled = bool((payload or {}).get("enabled", True))
            notifs = settings_store.load_settings().notifications
            muted = list(notifs.muted_rules)
            opted_in = list(notifs.enabled_rules)
            if rule.get("default_enabled", True):
                # Opt-out: on = absent from muted, off = present in muted.
                if enabled:
                    muted = [m for m in muted if m != rule_id]
                elif rule_id not in muted:
                    muted.append(rule_id)
            else:
                # Opt-in: on = present in enabled, off = absent from enabled.
                if enabled and rule_id not in opted_in:
                    opted_in.append(rule_id)
                elif not enabled:
                    opted_in = [e for e in opted_in if e != rule_id]
            settings_store.update_settings(
                notifications={"muted_rules": muted, "enabled_rules": opted_in}
            )
            return JSONResponse({"rules": _rules_with_state()})

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # --- frontend --------------------------------------------------------- #
    def frontend(self) -> List[FrontendDescriptor]:
        return [
            FrontendDescriptor(
                id="notify",
                label="Notifications",
                where="settings",
                module="/addons/notify.js",
                api_base="/api/notify",
                order=40,
                # where="settings": no sidebar bar. slots.js still imports the
                # module (it keys on `module`, not `where`); the module has no
                # visible surface of its own — it subscribes to the event bus
                # and exposes enable/disable/state. The on/off toggle lives in
                # the bell dropdown and Settings → Notifications.
            )
        ]
