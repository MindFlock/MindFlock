"""The ``listen`` stop: wait for an inbound hook (docs/flights.md "Round 15").

Run mints (or finds again) a token bound to the stop and shows its URL —
``POST /api/hooks/flight/<token>`` (:mod:`._hooks`, a public route) — on the
box; Grafana, Slack or a CI job posts to it, the body lands in
``detail.payload`` and the stop is done ("heard"). ``match`` (optional) is a
substring the body must contain: anything else is kept in ``detail.ignored``
and the hook stays armed. ``timeout_minutes`` 0 waits forever.
"""

from __future__ import annotations

from backend.web.core import flights as _flights
from backend.web.core.flight_kinds import Kind, register

from . import _hooks

_f = _flights._f


def _config(raw: dict, strict: bool, where: str) -> dict:
    return {
        "match": _flights._text(raw.get("match"), 500),
        "timeout_minutes": _flights._int(raw.get("timeout_minutes"), 0, 0, 7 * 1440),
    }


def _title(config: dict) -> str:
    if config.get("match"):
        return "Listen for %s" % config["match"][:40]
    return "Listen for a hook"


def _judge(flight, step, ss, obs, now) -> dict:
    d = ss["detail"]
    cfg = step["config"]
    if not d.get("token"):
        # Interrupted between the intent and the outcome: arm it again (the
        # store gives the same token back).
        return {"action": "run", "note": "arming the hook again"}
    if "payload" in d:
        match = str(cfg.get("match") or "")
        if match and match not in _hooks.payload_text(d["payload"]):
            # The match was edited after it arrived: listen again.
            return {"action": "run", "note": "re-arming: the hook did not match"}
        return {"action": "done", "note": "heard", "detail": {"receipt": "heard"}}
    mins = int(cfg.get("timeout_minutes") or 0)
    if mins and now - float(ss["since"] or now) > mins * 60.0:
        return {
            "action": "fail",
            "reason": "nothing arrived in %d min" % mins,
        }
    return _flights._wait("waiting at %s" % d.get("url", ""))


async def _run(flight, step, action, now) -> dict:
    token = _hooks.mint(flight["id"], step["id"], "listen", now)
    url = _hooks.path_for(token)
    base = _hooks.public_base()
    detail = {"token": token, "url": url}
    if base:
        detail["full_url"] = base + url
    return dict(action, note="waiting at %s" % url, detail=detail)


register(
    Kind(
        kind="listen",
        spec={
            "label": "Listen",
            "actor": "robot",
            "family": "outside",
            "summary": "Wait for something outside to call this stop's URL "
            "(a webhook from Grafana, Slack or CI).",
            "default_title": "Listen for a hook",
            "fields": [
                _f(
                    "match",
                    "text",
                    "Only when the body contains (empty = anything)",
                    default="",
                    placeholder='"status": "resolved"',
                ),
                _f(
                    "timeout_minutes",
                    "int",
                    "Give up after (minutes, 0 = never)",
                    default=0,
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        needs_session=False,
        default_title=_title,
        router=_hooks.router,
    )
)
