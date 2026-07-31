"""Notify addon: session-event notifications over two channels (roadmap B5).

The reference implementation of the *generic* extension path — manifest →
``core/slots.js`` imports the descriptor's ``module`` → the module registers
itself in ``window.mindflockAddons`` → ``init(ctx)`` subscribes to the client
event bus (``window.mindflock.events``). See docs/extensions.md.

What it does: notifies you when a session needs your input (activity →
``clarify``), when its open PR leaves the review stage (merged / closed), when it
blows its cost budget, and a few opt-in transitions besides. One rule list
(:data:`NOTIFY_RULES`), two delivery channels:

* **Browser** — ``static/addons/notify.js`` applies the rules client-side, where
  the Notification API (and its permission prompt) lives. Only fires while a tab
  is open on a secure origin.
* **ntfy** (optional, off by default) — this module applies the same rules
  *server-side* on the event bus and pushes to the user's ntfy topic
  (:mod:`backend.web.core.ntfy`), so the alert reaches a phone with nothing open
  at all. That is the channel that makes "go check on session X" work while
  you're away from the machine.

The rules stay server-side data so the backend remains the one source of truth
for which transitions matter, and so both channels agree on what "notify me"
means — the per-rule switches in Settings → Notifications govern both.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.config import settings as settings_store
from backend.web.core import mobile_access, mobile_announce, ntfy

from .base import SECRET_MASK, Addon, AppContext, FrontendDescriptor

#: Event → notification rules both channels apply. ``old``/``new`` of ``None``
#: match any value; ``{session}`` in title/body is replaced with the envelope's
#: session title. Each rule has a stable ``id``, a short ``label`` for the
#: settings UI, and a ``default_enabled`` flag: default-on rules are opt-out
#: (muted via settings.notifications.muted_rules); default-off rules are opt-in
#: (turned on via settings.notifications.enabled_rules) because they're noisy.
#:
#: ``priority``/``tags`` are ntfy-only presentation: priority 4 buzzes a phone
#: through most do-not-disturb setups, 2 lands silently, 3 is normal — so the
#: "come look at this" rules ring and the ambient ones don't. Tags are ntfy's
#: emoji shortcodes, kept to widely-supported names.
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
        "priority": 4,
        "tags": ["question"],
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
        "priority": 3,
        "tags": ["white_check_mark"],
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
        "priority": 4,
        "tags": ["moneybag"],
    },
    {
        # Roadmap D: the agent ran out of usage and is parked on its CLI's
        # limit screen. Actionable in the "stop waiting for this one" sense —
        # nothing will move until the provider's window resets, and MindFlock
        # picks it back up on its own when it does.
        "id": "usage_limit",
        "label": "A session runs out of usage",
        "event": "session.activity_changed",
        "old": None,
        "new": "limit",
        "title": "{session} ran out of usage",
        "body": "The agent hit its usage limit. MindFlock resumes it when the window resets.",
        "default_enabled": True,
        "priority": 3,
        "tags": ["hourglass"],
    },
    {
        # The other end of the same story. Emitted once per reopening by the
        # drain-loop watcher (server._watch_one_limited), which only ever looks
        # at sessions that had run out — so this cannot fire on a window that
        # merely rolled over while everything was fine.
        "id": "usage_restored",
        "label": "Usage comes back after running out",
        "event": "session.usage_restored",
        "old": None,
        "new": None,
        "title": "Usage is back",
        "body": "The provider's window reset — {session} can run again.",
        "default_enabled": True,
        "priority": 3,
        "tags": ["arrows_counterclockwise"],
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
        "priority": 2,
        "tags": ["zzz"],
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
        "priority": 2,
        "tags": ["hourglass"],
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
        "priority": 4,
        "tags": ["x"],
    },
]

#: Rule fields the client never needs: opt-in/opt-out semantics (it gets the
#: resolved ``enabled``) and the ntfy-only presentation hints.
_INTERNAL_RULE_FIELDS = ("default_enabled", "priority", "tags")

#: At most one push per (session, event) per this many seconds. Mirrors
#: ``DEDUPE_MS`` in static/addons/notify.js so both channels collapse a flapping
#: transition the same way.
_DEDUPE_SECONDS = 5.0


def _rule_enabled(rule: dict, muted: set, opted_in: set) -> bool:
    """Whether ``rule`` is active given the muted (opt-out) and enabled (opt-in)
    id sets. Default-on rules fire unless muted; default-off rules fire only when
    explicitly opted in."""
    if rule.get("default_enabled", True):
        return rule["id"] not in muted
    return rule["id"] in opted_in


def _enabled_rules() -> List[dict]:
    """The raw rules currently switched on (internal fields intact — this is the
    server-side dispatch view, not the client payload)."""
    try:
        notifs = settings_store.load_settings().notifications
        muted = set(notifs.muted_rules)
        opted_in = set(notifs.enabled_rules)
    except Exception:  # noqa: BLE001 — settings must never break dispatch
        muted, opted_in = set(), set()
    return [r for r in NOTIFY_RULES if _rule_enabled(r, muted, opted_in)]


def _rules_with_state() -> List[dict]:
    """NOTIFY_RULES each tagged with its current ``enabled`` flag from settings.

    Internal fields (:data:`_INTERNAL_RULE_FIELDS`) are NOT exposed to the
    client — the frontend only needs the resolved ``enabled``.
    """
    try:
        notifs = settings_store.load_settings().notifications
        muted = set(notifs.muted_rules)
        opted_in = set(notifs.enabled_rules)
    except Exception:  # noqa: BLE001 — settings must never break the config read
        muted, opted_in = set(), set()
    return [
        {
            **{k: v for k, v in rule.items() if k not in _INTERNAL_RULE_FIELDS},
            "enabled": _rule_enabled(rule, muted, opted_in),
        }
        for rule in NOTIFY_RULES
    ]


def _matches(rule: dict, envelope: dict) -> bool:
    """Whether ``envelope`` satisfies ``rule`` (``None`` constraint = any value).

    The server-side twin of ``ruleMatches`` in static/addons/notify.js — keep the
    two in step, they decide the same thing for their own channel.
    """
    if rule.get("event") != envelope.get("event"):
        return False
    if rule.get("old") is not None and rule["old"] != envelope.get("old"):
        return False
    if rule.get("new") is not None and rule["new"] != envelope.get("new"):
        return False
    return True


def _fill(template: str, envelope: dict) -> str:
    """``{session}`` → the envelope's session title (the JS ``fill``'s twin)."""
    return str(template or "").replace(
        "{session}", str(envelope.get("session") or "session")
    )


class NotifyAddon(Addon):
    id = "notify"
    label = "Notifications"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()
        self._unsubscribe = None
        # (session, event) -> monotonic ts of the last push, for _DEDUPE_SECONDS.
        self._last_push: dict = {}
        self._push_lock = threading.Lock()

    # --- ntfy channel (server-side dispatch) ------------------------------- #
    async def on_startup(self, ctx: AppContext) -> None:
        """Subscribe the ntfy channel to the event bus.

        Always subscribed, cheaply: the callback's first act is to check whether
        ntfy is configured at all, so an unconfigured install pays one settings
        read per event. Subscribing unconditionally means turning ntfy on in
        Settings takes effect immediately instead of on the next restart.
        """
        try:
            ntfy.set_loop(asyncio.get_running_loop())
        except RuntimeError:  # no loop: publish_soon falls back to a thread
            pass
        if self._unsubscribe is None and ctx is not None:
            self._unsubscribe = ctx.subscribe("*", self._on_event)

    async def on_shutdown(self, ctx: AppContext) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            finally:
                self._unsubscribe = None

    def _on_event(self, envelope: dict) -> None:
        """Push a matching event to ntfy. Runs on the emitting thread — so it
        stays tiny and hands the HTTP call off to the loop.

        Unlike the browser channel there is no replay to guard against (the bus
        replays only to reconnecting websockets) and no first-poll burst after a
        restart (``server._emit_state_changes`` seeds on first sighting without
        emitting), so a restart doesn't re-push old news.
        """
        try:
            cfg = ntfy.load()
            if not cfg.active:
                return
            for rule in _enabled_rules():
                if not _matches(rule, envelope):
                    continue
                key = (str(envelope.get("session") or ""), str(rule["id"]))
                now = time.monotonic()
                with self._push_lock:
                    if now - self._last_push.get(key, 0.0) < _DEDUPE_SECONDS:
                        continue
                    self._last_push[key] = now
                # Tap the notification -> the mobile view, already on the
                # session it is about. Cached, never probed here: this runs on
                # the emitting thread. Empty when there's no tailnet — and then
                # publish() falls back to the user's own configured click URL.
                click = mobile_announce.click_for(str(envelope.get("session") or ""))
                message = _fill(rule.get("body", ""), envelope)
                if click:
                    # Also in the text: a notification you have to go find the
                    # server for is a notification that made you get up. The
                    # click action covers most clients; the line covers the rest
                    # (and a phone that shows the push without acting on it).
                    message += "\n" + click
                ntfy.publish_soon(
                    cfg,
                    title=_fill(rule.get("title", ""), envelope),
                    message=message,
                    priority=rule.get("priority"),
                    tags=rule.get("tags"),
                    click=click or None,
                )
        except Exception as err:  # noqa: BLE001 — a push never breaks an emit
            ntfy.log_error(
                "ntfy dispatch for %s failed: %s", envelope.get("event"), err
            )

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
            """Turn one rule on/off (for BOTH channels). Default-on rules persist
            as an opt-out (``muted_rules``); default-off rules persist as an
            opt-in (``enabled_rules``). Unknown ids are rejected."""
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

        @router.get("/ntfy")
        def get_ntfy() -> JSONResponse:
            """The ntfy channel's state for Settings → Notifications.

            Never returns the token — only ``has_token``. Includes a scannable
            QR of the subscribe URL (the fastest path from "configured here" to
            "subscribed on the phone") and, when no topic is set yet, a random
            one to offer.
            """
            return JSONResponse(_ntfy_view())

        @router.post("/ntfy")
        def set_ntfy(payload: dict) -> JSONResponse:
            """Save the ntfy channel config; returns the same view as ``GET``.

            Only the keys present are touched. The token follows the store's
            secret convention (empty / the mask sentinel = keep the saved one),
            with one extra safety: retargeting the channel at a *different host*
            without supplying a new token drops the old one rather than shipping
            server A's credential to server B.

            That convention has no way to say "I want no token at all", which
            matters here more than for the app's other secrets: an ntfy token is
            *optional* (public topics need none), and a wrong one is worse than
            none — ntfy rejects a bad credential with 401 instead of ignoring
            it, so a stray token breaks a push that would otherwise work.
            ``{"clear_token": true}`` is that escape hatch, and it wins over any
            ``token`` in the same payload: it is the destructive intent, and the
            only way to send both is to mean it.
            """
            payload = payload or {}
            stored = settings_store.load_settings().notifications
            patch: dict = {}
            note = ""

            server = stored.ntfy_server
            if "server" in payload:
                server = ntfy.normalize_server(str(payload.get("server") or ""))
                patch["ntfy_server"] = server
            topic = stored.ntfy_topic
            if "topic" in payload:
                topic = str(payload.get("topic") or "").strip()
                patch["ntfy_topic"] = topic
            if "click_url" in payload:
                click, stripped = ntfy.strip_token_param(
                    str(payload.get("click_url") or "")
                )
                patch["ntfy_click_url"] = click
                if stripped:
                    note = (
                        "The access token was removed from the tap-to-open URL — "
                        "it would have been stored on the ntfy server."
                    )
            if "enabled" in payload:
                patch["ntfy_enabled"] = bool(payload.get("enabled"))

            # Validate whenever the channel is (or is becoming) on, and whenever
            # a topic is present at all — but not "pick a topic" for someone who
            # is clearing the field on their way to switching the channel off.
            will_be_on = bool(patch.get("ntfy_enabled", stored.ntfy_enabled))
            if will_be_on or topic:
                problem = ntfy.validate(server, topic)
                if problem:
                    return JSONResponse({"error": problem}, status_code=400)

            token = str(payload.get("token") or "").strip()
            if payload.get("clear_token"):
                patch["ntfy_token"] = ""  # "" clears (see update_settings)
                if stored.ntfy_token:
                    note = (
                        (note + " ") if note else ""
                    ) + "The saved access token was cleared."
            elif token and token != SECRET_MASK:
                patch["ntfy_token"] = token
            elif stored.ntfy_token and not ntfy.same_host(
                stored.ntfy_server or ntfy.DEFAULT_SERVER, server
            ):
                patch["ntfy_token"] = ""  # "" clears (see update_settings)
                note = (
                    ((note + " ") if note else "")
                    + "The saved access token was cleared — it belonged to the previous ntfy server."
                )

            if patch:
                settings_store.update_settings(notifications=patch)
            # Channel just came on: the first thing worth pushing to a phone
            # that has never heard from this machine is how to reach it.
            if patch.get("ntfy_enabled") and not stored.ntfy_enabled:
                mobile_announce.announce_soon(mobile_announce.REASON_NTFY)
            view = _ntfy_view()
            if note:
                view["note"] = note
            return JSONResponse(view)

        @router.post("/ntfy/test")
        async def test_ntfy(payload: Optional[dict] = None) -> JSONResponse:
            """Send one test push and report the verdict.

            Takes its config from the request body when supplied, so the user can
            verify a topic *before* saving it (same shape as the ticketing
            connection test). Exempt from the rate cap: it is an explicit,
            hand-driven action.
            """
            payload = payload or {}
            stored = settings_store.load_settings().notifications
            server = ntfy.normalize_server(
                str(payload.get("server") or stored.ntfy_server or "")
            )
            topic = str(payload.get("topic") or stored.ntfy_topic or "").strip()
            token = str(payload.get("token") or "").strip()
            if not token or token == SECRET_MASK:
                token = stored.ntfy_token
            problem = ntfy.validate(server, topic)
            if problem:
                return JSONResponse({"ok": False, "error": problem})
            click, _ = ntfy.strip_token_param(
                str(payload.get("click_url") or stored.ntfy_click_url or "")
            )
            cfg = ntfy.NtfyConfig(
                enabled=True,
                server=server,
                topic=topic,
                token=token,
                click_url=click,
            )
            # The test push is the first one anyone sees, so it shows what the
            # real ones will: the phone URL, tappable. Probed here (an async
            # route can afford it) rather than read from the cache — this is
            # also where a first-run install *fills* that cache.
            phone_url, _live = await asyncio.to_thread(mobile_access.tailnet_url)
            mobile_announce.remember_url(phone_url)
            message = (
                "Push notifications are working — this is where session "
                "alerts will land."
            )
            if phone_url:
                message += "\n" + phone_url
            ok, error = await ntfy.publish(
                cfg,
                title="MindFlock is connected",
                message=message,
                priority=3,
                tags=["bell"],
                click=phone_url or None,
                budgeted=False,
            )
            return JSONResponse({"ok": ok, "error": error})

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


def _ntfy_view() -> dict:
    """The ntfy channel as the settings screen needs it — never the token.

    ``suggested_topic`` is always a fresh random name: it seeds the field's
    placeholder when nothing is set AND backs the Generate button, so the client
    never has to invent a topic with its own weaker randomness.
    """
    cfg = ntfy.load()
    return {
        "enabled": cfg.enabled,
        "server": cfg.server,
        "server_default": ntfy.DEFAULT_SERVER,
        "topic": cfg.topic,
        "has_token": bool(cfg.token),
        "click_url": cfg.click_url,
        "configured": cfg.configured,
        "active": cfg.active,
        "public_server": cfg.is_public_server,
        "subscribe_url": cfg.subscribe_url,
        "last": ntfy.last_result(),
        "suggested_topic": ntfy.random_topic(),
        # Same renderer as the Settings → Mobile QR; None when segno is absent.
        "qr_svg": mobile_access.qr_svg(cfg.subscribe_url) if cfg.topic else None,
    }
