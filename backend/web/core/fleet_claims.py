"""Don't start the same ticket on two of the user's devices.

Every MindFlock server is standalone, and every ticket guard it has is local:
the ingestion ledger, its own sessions, its own pending launches. The only
signal two machines shared was a ``feature/<slug>/`` branch on the remote,
which exists only once the work is pushed. So two devices scanning the same
queue could both start a ticket.

This module makes "taken" a fleet-wide fact, over the paired-device plumbing
remote control already has (:mod:`backend.web.core.remote`):

* **Advertise.** ``GET /api/tickets/claims`` lists what THIS device holds
  (:func:`local_claims`): a live ticket session, a launch still starting, a
  fresh ``in_flight`` ledger marker, a team run's reservation.
* **Ask.** :func:`holder` asks every connected device for its claims
  (cached :data:`CACHE_TTL` s; ``fresh=True`` before a launch) and answers
  who, if anyone, already has the slug. A device that predates the claims
  route is read from the session list remote control already polls.
* **Break ties.** Two devices can pass a check at the same moment, so the
  launch paths write their own ``in_flight`` marker FIRST and then ask with
  ``own_since``: a peer's session or starting launch always wins, a peer's
  marker wins only when it is older (device key breaks an exact tie). Each
  side reads after its own write, so at least one of two racers sees the
  other and backs off.

Best-effort by design: an unreachable device holds nothing, so a dead
laptop never blocks the queue (the pushed-branch check still applies).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional

#: Seconds a device's claims answer is reused for listing (launches pass
#: ``fresh=True`` and always ask again).
CACHE_TTL = 10.0

#: An ``in_flight`` marker with no session behind it is a launch still in
#: progress — or one that died without cleaning up. Past this age it is no
#: longer advertised (the session itself is, once it exists), so a crashed
#: launch can't hold a ticket fleet-wide forever. Team-run reservations are
#: advertised regardless: they wait in a queue on purpose.
MARKER_MAX_AGE = 2 * 3600.0

#: Kinds that block a start everywhere, whatever the timing.
_HARD = ("session", "starting")

_cache: Dict[str, tuple] = {}  # device key -> (fetched_at, claims dict)


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def local_claims() -> Dict[str, dict]:
    """``{slug: {"kind", "since"}}`` for every ticket this device holds."""
    from backend.web import server as srv
    from backend.web.core import pending as _pending
    from backend.web.core import ticket_start as _ticket_start
    from backend.ticket_ingestion.state import in_flight_stories

    claims: Dict[str, dict] = {}
    now = time.time()
    try:
        markers = in_flight_stories(_ticket_start._REPO_ROOT)
    except Exception:  # noqa: BLE001 — an unreadable ledger holds nothing
        markers = {}
    for slug, entry in markers.items():
        since = _ts(entry.get("processed_at"))
        reserved = bool(entry.get("reserved_by"))
        if not reserved and since and now - since > MARKER_MAX_AGE:
            continue
        claims[slug] = {"kind": "reserved" if reserved else "in_flight", "since": since}
    for title, meta in _pending.snapshot().items():
        if meta.get("kind") == "tix":
            claims[title] = {"kind": "starting", "since": _ts(meta.get("since"))}
    for title, inst in list(srv.ENGINE.instances.items()):
        branch = str(getattr(inst, "Branch", "") or "")
        if _pending.session_kind(title, branch) != "tix":
            continue
        created = getattr(inst, "CreatedAt", None)
        since = created.timestamp() if hasattr(created, "timestamp") else 0.0
        claims[title] = {"kind": "session", "since": since}
    return claims


def claims_json() -> dict:
    from backend.web.core import remote as _remote

    return {"device": _remote.self_identity()["key"], "claims": local_claims()}


def _claims_from_instances(dev: dict) -> Dict[str, dict]:
    """A pre-claims device: its ticket sessions, from the session list remote
    control already keeps (no timing — they read as held since forever)."""
    from backend.web.core import pending as _pending

    out: Dict[str, dict] = {}
    for row in dev.get("instances") or []:
        if not isinstance(row, dict) or row.get("device"):
            continue
        title = str(row.get("title") or "")
        if _pending.session_kind(title, str(row.get("branch") or "")) == "tix":
            out[title] = {"kind": "session", "since": 0.0}
    return out


async def _device_claims(dev: dict, fresh: bool) -> Dict[str, dict]:
    from backend.web.core import remote as _remote

    key = dev["key"]
    hit = _cache.get(key)
    if not fresh and hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    status, body = await _remote.get_json(dev, "/api/tickets/claims")
    if (
        status == 200
        and isinstance(body, dict)
        and isinstance(body.get("claims"), dict)
    ):
        claims = {
            str(k): v
            for k, v in body["claims"].items()
            if isinstance(v, dict) and v.get("kind")
        }
    elif status == 404:
        claims = _claims_from_instances(dev)
    else:
        claims = {}  # unreachable / refused: holds nothing
    _cache[key] = (time.monotonic(), claims)
    return claims


async def fleet_claims(fresh: bool = False) -> Dict[str, List[dict]]:
    """``{slug: [{"device", "label", "kind", "since"}, …]}`` across every
    connected device (this one excluded)."""
    from backend.web.core import remote as _remote

    devs = _remote.connected_devices()
    if not devs:
        return {}
    answers = await asyncio.gather(*(_device_claims(d, fresh) for d in devs))
    out: Dict[str, List[dict]] = {}
    for dev, claims in zip(devs, answers):
        for slug, c in claims.items():
            out.setdefault(slug, []).append(
                {
                    "device": dev["key"],
                    "label": dev.get("host") or dev["key"],
                    "kind": str(c.get("kind")),
                    "since": _ts(c.get("since")),
                }
            )
    return out


def _blocks(claim: dict, own_since: Optional[float], self_key: str) -> bool:
    if claim["kind"] in _HARD or own_since is None:
        return True
    return (claim["since"], claim["device"]) < (own_since, self_key)


async def holder(
    slug: str, *, own_since: Optional[float] = None, fresh: bool = False
) -> Optional[dict]:
    """The device that already has ``slug``, or None.

    Without ``own_since`` (a listing, or a check before this device has
    marked anything) any claim counts. With it — this device's own
    ``in_flight`` marker is already written — a peer's MARKER counts only if
    it is older, so of two racers exactly one backs off."""
    from backend.web.core import remote as _remote

    if not slug:
        return None
    self_key = _remote.self_identity()["key"]
    claims = (await fleet_claims(fresh=fresh)).get(slug) or []
    for c in sorted(claims, key=lambda c: (c["kind"] not in _HARD, c["since"])):
        if _blocks(c, own_since, self_key):
            return c
    return None


def describe(claim: dict) -> str:
    """The Intake chip / 409 wording for a claim."""
    where = claim.get("label") or claim.get("device") or "another device"
    if claim.get("kind") == "session":
        return "running on " + where
    if claim.get("kind") == "reserved":
        return "queued in a team run on " + where
    return "starting on " + where


def clear_cache() -> None:
    _cache.clear()
