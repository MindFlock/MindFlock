"""The ``someone`` stop: a perch whose ask goes out (docs/flights.md
"Round 15").

The same perch as ``you`` — the flight parks ``waiting_you`` and your Done /
Skip move it on — but run first sends the ask: to a peer link named ``who``
(a MindFlock message, the text + the items + a one-tap link), else to
``webhook`` as JSON ``{text, items, done_url}`` (a Slack incoming webhook
shows ``text``, so the link rides in it too). The link is ``GET
/api/hooks/flight/<token>/done`` (:mod:`._hooks`): a page with one Done
button whose POST marks the perch done "by <who>". Nobody to ask fails the
stop.

The core parks the step through ``run``'s returned ``you`` action
(``flights.apply``), as it parks a ``you`` step from the planner; a judge
asked later (a restart mid-send) sends again when no token was recorded.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import List

from backend.web.core import flights as _flights
from backend.web.core.flight_kinds import Kind, StepFailed, register

from . import _hooks

_f = _flights._f

#: How long the webhook POST may take.
WEBHOOK_TIMEOUT_S = 20.0


def _config(raw: dict, strict: bool, where: str) -> dict:
    webhook = _flights._text(raw.get("webhook"), 2000)
    if strict and webhook and not webhook.lower().startswith(("http://", "https://")):
        raise _flights.PlanError(
            "the webhook starts with http:// or https://", where + ".webhook"
        )
    who = _flights._text(raw.get("who"), 64)
    if strict and not who and not webhook:
        raise _flights.PlanError(
            "an Ask someone stop needs who to ask (a peer's name) or a webhook",
            where + ".who",
        )
    return {
        "who": who,
        "text": _flights._text(raw.get("text"), 1000),
        "items": _flights._items(raw.get("items")),
        "webhook": webhook,
    }


def _title(config: dict) -> str:
    return "Ask %s" % config["who"] if config.get("who") else "Ask someone"


def message_for(text: str, items: List[str], url: str) -> str:
    """The ask as one readable message (peer or Slack)."""
    lines = [text]
    lines += ["• %s" % i for i in items]
    if url:
        lines += ["", "Done: %s" % url]
    return "\n".join(lines)


def _peer_link(who: str):
    """``(service, link_id)`` of the peer link named ``who`` (case-blind)."""
    from backend.peer import service as _svc

    service = _svc.get_service()
    want = who.strip().lower()
    for link in service.store.list():
        if str(getattr(link, "peer_name", "") or "").strip().lower() == want:
            return service, link.link_id
    raise StepFailed("no peer link named %s" % who)


async def _send_peer(who: str, message: str) -> None:
    service, link_id = _peer_link(who)
    try:
        await service.human_message(link_id, message)
    except Exception as err:  # noqa: BLE001 — PeerServiceError's sentence
        raise StepFailed("could not reach %s: %s" % (who, err))


def _post_webhook(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            req, timeout=WEBHOOK_TIMEOUT_S
        ) as resp:  # noqa: S310
            status = int(resp.status)
    except urllib.error.HTTPError as err:
        status = int(err.code)
    except Exception as err:  # noqa: BLE001
        raise StepFailed(
            "the webhook did not answer: %s" % (getattr(err, "reason", None) or err)
        )
    if not 200 <= status < 300:
        raise StepFailed("the webhook answered HTTP %d" % status)


def _ask(flight: dict, step: dict) -> dict:
    """The perch action: what the box and the dialog show while waiting."""
    cfg = step["config"]
    text = _flights.fill_prompt(cfg.get("text") or step["title"], flight)
    return {
        "action": "you",
        "note": text,
        "detail": {"items": list(cfg.get("items") or [])},
    }


async def _run(flight, step, action, now) -> dict:
    cfg = step["config"]
    who = str(cfg.get("who") or "")
    webhook = str(cfg.get("webhook") or "")
    if not who and not webhook:
        raise StepFailed(
            "nobody to ask — edit this stop to name a peer or a webhook, or Skip it"
        )
    out = _ask(flight, step)
    items = out["detail"]["items"]
    token = _hooks.mint(flight["id"], step["id"], "someone", now)
    url = _hooks.public_base() + _hooks.done_path_for(token)
    message = message_for(out["note"], items, url)
    if who:
        await _send_peer(who, message)
        sent_to = who
    else:
        await asyncio.to_thread(
            _post_webhook, webhook, {"text": message, "items": items, "done_url": url}
        )
        sent_to = "the webhook"
    out["detail"].update(
        token=token,
        done_url=url,
        sent_to=sent_to,
        # Your own Done keeps this; the link's Done writes "done by <who>".
        receipt="done by you",
    )
    return out


def _judge(flight, step, ss, obs, now) -> dict:
    if not ss["detail"].get("token"):
        # Interrupted mid-send (a restart): send it again.
        return {"action": "run", "note": "asking again (it was interrupted)"}
    return _ask(flight, step)


register(
    Kind(
        kind="someone",
        spec={
            "label": "Someone",
            "actor": "you",
            "family": "people",
            "summary": "Ask someone else (a peer, or a Slack webhook) to do or "
            "check something; waits like Wait for me until they press Done.",
            "default_title": "Ask someone",
            "fields": [
                _f(
                    "who",
                    "text",
                    "Who (a peer link's name; empty = the webhook)",
                    default="",
                ),
                _f("text", "textarea", "What to ask", default=""),
                _f(
                    "items", "textarea", "Things to tick off (one per line)", default=""
                ),
                _f(
                    "webhook",
                    "text",
                    "Webhook (Slack incoming webhook or any JSON POST)",
                    default="",
                    placeholder="https://hooks.slack.com/…",
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        needs_session=False,
        default_title=_title,
    )
)
