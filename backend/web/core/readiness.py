"""Is each of your devices ready to work? — one summary per member.

Joining a computer brings the shared settings and the GitHub token, but not
an agent sign-in (accounts stay on each computer), not git push credentials,
not the dependencies. Settings → Devices used to show only "online, version
X" for a member that couldn't start a session. Each member now reports a
summary of itself — computed BY that member, about itself — and Devices shows
it read-only, with the fix to run on that device (never through a remote
terminal).

:func:`self_summary` is what this device says about itself (cached; the
doctor's checks take seconds). ``GET /api/fleet/readiness/self`` serves it to
the other members (fleet key only), and ``GET /api/fleet/readiness`` gathers
every member's for this device's own screen. A separate pair of routes on
purpose: ``GET /api/fleet`` (the roster) stays fast and unchanged.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Dict, List, Optional

#: How long a computed summary is reused (the doctor's probes cost seconds).
TTL = 120.0
#: Per-member fetch timeout when gathering the others'.
FETCH_TIMEOUT = 8.0
SELF_PATH = "/api/fleet/readiness/self"

_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {"at": 0.0, "value": None}


def _summary_from(
    checks: List[dict],
    *,
    deferred: List[str],
    push: Optional[dict],
    ts: dict,
    version: str,
) -> dict:
    """The summary from its parts. Pure (tests feed it)."""
    by_id = {c.get("id"): c for c in checks}
    missing = [
        c.get("label") or c.get("id") for c in checks if c.get("status") == "fail"
    ]
    auth = by_id.get("agent-auth") or {}
    agent_ok: Optional[bool]
    if auth.get("status") == "ok":
        agent_ok = True
    elif auth.get("status") == "warn":
        agent_ok = False
    else:
        agent_ok = None  # nothing to probe (or no agent at all)
    exp = ts.get("key_expiry") or {}
    fixes: List[str] = []
    if missing:
        fixes.append("install %s (Setup → Dependencies)" % ", ".join(missing))
    if agent_ok is False:
        fixes.append("sign in to %s (Setup)" % (auth.get("provider") or "the agent"))
    if deferred:
        fixes.append("install %s (Settings → Devices)" % ", ".join(deferred))
    if push and push.get("ok") is False:
        fixes.append(push.get("fix") or "connect GitHub (Setup)")
    if exp.get("expired") or exp.get("warn"):
        fixes.append(
            "renew the Tailscale key, or disable its expiry in the admin console"
        )
    return {
        "version": version,
        "deps_ok": not missing,
        "missing": missing,
        "agent": {"provider": auth.get("provider") or "", "signed_in": agent_ok},
        "deferred": deferred,
        "push": (
            {"ok": push.get("ok"), "message": push.get("message", "")}
            if push
            else {"ok": None, "message": ""}
        ),
        "tailscale": {
            "running": ts.get("backend_state") == "Running",
            "key_expiry_days": exp.get("days"),
            "key_expired": bool(exp.get("expired")),
            "key_warn": bool(exp.get("warn")),
        },
        "ready": not missing
        and agent_ok is not False
        and not deferred
        and not (push and push.get("ok") is False)
        and not exp.get("expired"),
        "fixes": fixes,
        "at": time.time(),
    }


def _compute() -> dict:
    from backend import __version__, doctor, tailscale_cli
    from backend.web.core import github_auth, settings_sync

    try:
        checks = [c.to_dict() for c in doctor.run_checks()]
    except Exception:  # noqa: BLE001
        checks = []
    try:
        deferred = sorted(settings_sync.deferred_providers())
    except Exception:  # noqa: BLE001
        deferred = []
    try:
        ts = tailscale_cli.health()
    except Exception:  # noqa: BLE001
        ts = {}
    repo = github_auth.remembered_repo()
    push = github_auth.push_check_cached(repo) if repo else None
    return _summary_from(
        checks, deferred=deferred, push=push, ts=ts, version=__version__
    )


def self_summary(*, fresh: bool = False) -> dict:
    """This device's summary of itself (cached :data:`TTL` seconds)."""
    with _LOCK:
        if (
            not fresh
            and _CACHE["value"]
            and time.monotonic() - float(_CACHE["at"]) < TTL
        ):
            return dict(_CACHE["value"])  # type: ignore[arg-type]
    value = _compute()
    with _LOCK:
        _CACHE.update(at=time.monotonic(), value=value)
    return dict(value)


def invalidate() -> None:
    with _LOCK:
        _CACHE.update(at=0.0, value=None)


async def gather(*, fresh: bool = False) -> dict:
    """``{"members": {key: summary | {"error"}}}`` for every live member: this
    one's own, the others' asked of them with the fleet key."""
    from backend.web.core import fleet, remote

    out: Dict[str, dict] = {}
    if not fleet.in_fleet():
        me = remote.self_identity().get("key") or ""
        if me:
            out[me] = await asyncio.to_thread(self_summary, fresh=fresh)
        return {"members": out}
    me = fleet._self_key()
    members = sorted(fleet.live_members())
    key = fleet.fleet_key()
    devs = {d["key"]: d for d in remote.fleet_devices()}

    async def one(k: str) -> None:
        if k == me:
            out[k] = await asyncio.to_thread(self_summary, fresh=fresh)
            return
        dev = devs.get(k)
        if not dev:
            out[k] = {"error": "offline"}
            return
        status, body = await remote.get_json(dev, SELF_PATH, FETCH_TIMEOUT, bearer=key)
        if status == 200 and isinstance(body, dict):
            out[k] = body
        elif status == 404:
            out[k] = {"error": "update MindFlock there to see this"}
        else:
            out[k] = {"error": "didn't answer" if not status else "HTTP %s" % status}

    await asyncio.gather(*(one(k) for k in members))
    return {"members": out}
