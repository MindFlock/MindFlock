"""Inbound hooks of the ``listen`` and ``someone`` stops (docs/flights.md
"Round 15", task ``hooks``).

Not a kind (the underscore keeps :func:`flight_kinds.load` from importing it
as one): the shared token store and the ONE router both kinds use, included
once through the ``listen`` kind's ``Kind.router``.

A token is 32 lower-case hex (``secrets.token_hex(16)``) bound to one stop:
``token -> {flight, step, kind, at, spent}`` in ``<flights dir>/hook_tokens.json``,
so a restart finds it again (no re-mint, the URL on the box stays good). The
token IS the credential — the routes are public (auth.py
``_PUBLIC_FLIGHT_HOOK``) — so a spent or unknown one answers 404 and nothing
else:

* ``POST /api/hooks/flight/{token}`` — the ``listen`` inbound: the body
  (≤ 8 KB kept: JSON when it parses, else text) lands in the step's
  ``detail.payload`` and the token is spent; a body without the stop's
  ``match`` is kept in ``detail.ignored`` and the token stays armed (202).
* ``GET /api/hooks/flight/{token}/done`` — the ``someone`` perch's page: the
  ask, its items, one Done button posting to the same URL.
* ``POST /api/hooks/flight/{token}/done`` — Done on that perch, "done by
  <who>"; a plain Thanks page.
"""

import asyncio
import html
import json
import os
import secrets
import threading
import time
from typing import Optional, Tuple

from backend.fsutil import atomic_write_json, read_json
from backend.web.core import flights as _flights

_FILE = "hook_tokens.json"
_LOCK = threading.Lock()

#: What a listen stop keeps of an inbound body.
MAX_PAYLOAD = 8192
#: What the route reads at most before refusing (a hook is a small message).
MAX_READ = 256 * 1024
#: Spent tokens are forgotten after this long (an unspent one lives as long as
#: its flight).
KEEP_SPENT_S = 30 * 86400.0

PATH = "/api/hooks/flight/"


def path_for(token: str) -> str:
    return PATH + token


def done_path_for(token: str) -> str:
    return PATH + token + "/done"


def public_base() -> str:
    """This device's reachable URL root (the phone URL a push's one-tap button
    uses, ``mobile_announce.act_base``) — ``""`` when unknown (no tailnet, or
    the shared link answered by whichever device is up)."""
    try:
        from backend.web.core import mobile_announce as _ma

        return _ma.act_base()
    except Exception:  # noqa: BLE001 — unknown, a relative link then
        return ""


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def _path() -> str:
    return os.path.join(_flights.flights_dir(), _FILE)


def _load_locked() -> dict:
    data = read_json(_path(), {}, expect=dict)
    tokens = data.get("tokens") if isinstance(data, dict) else None
    return dict(tokens) if isinstance(tokens, dict) else {}


def _save_locked(tokens: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, {"tokens": tokens}, prefix=".hkt.", mode=0o600)


def _prune(tokens: dict, now: float) -> None:
    for tok in [
        t
        for t, e in tokens.items()
        if e.get("spent") and now - float(e.get("spent_at") or 0.0) > KEEP_SPENT_S
    ]:
        tokens.pop(tok, None)


def mint(flight_id: str, step_id: str, kind: str, now: Optional[float] = None) -> str:
    """The stop's armed token: the unspent one already stored for it (a
    restart, a retry after a timeout), else a new one."""
    ts = float(now if now is not None else time.time())
    with _LOCK:
        tokens = _load_locked()
        for tok, e in tokens.items():
            if (
                e.get("flight") == flight_id
                and e.get("step") == step_id
                and e.get("kind") == kind
                and not e.get("spent")
            ):
                return tok
        _prune(tokens, ts)
        tok = secrets.token_hex(16)
        tokens[tok] = {
            "flight": flight_id,
            "step": step_id,
            "kind": kind,
            "at": ts,
            "spent": False,
        }
        _save_locked(tokens)
    return tok


def lookup(token: str) -> Optional[dict]:
    """The armed token's entry (``flight``, ``step``, ``kind``), None for an
    unknown or spent one."""
    if not isinstance(token, str) or len(token) != 32:
        return None
    with _LOCK:
        e = _load_locked().get(token)
    if not isinstance(e, dict) or e.get("spent"):
        return None
    return e


def spend(token: str, now: Optional[float] = None) -> None:
    with _LOCK:
        tokens = _load_locked()
        e = tokens.get(token)
        if isinstance(e, dict) and not e.get("spent"):
            e["spent"] = True
            e["spent_at"] = float(now if now is not None else time.time())
            _save_locked(tokens)


def forget_flight(flight_id: str) -> None:
    """Spend every token of ``flight_id`` (nothing calls it from the core yet;
    a finished flight's tokens already answer 404 through the step check)."""
    with _LOCK:
        tokens = _load_locked()
        hit = False
        for e in tokens.values():
            if e.get("flight") == flight_id and not e.get("spent"):
                e["spent"], e["spent_at"], hit = True, time.time(), True
        if hit:
            _save_locked(tokens)


def _wake() -> None:
    from backend.web.core import flight_driver as _driver

    _driver.wake()


# --------------------------------------------------------------------------- #
# What a stop looks like from a hook
# --------------------------------------------------------------------------- #
class _Gone(Exception):
    """The token's stop is not listening (any more): a 404."""


def _stop_of(entry: dict, state: str) -> Tuple[dict, dict, dict]:
    """``(flight, step, step_state)`` of ``entry``'s stop when it is the
    flight's current step in ``state``, else :class:`_Gone`."""
    f = _flights.get(str(entry.get("flight") or ""))
    if f is None or f["state"] in _flights.FINAL:
        raise _Gone()
    cur = _flights._current(f)
    if (
        cur is None
        or cur["id"] != entry.get("step")
        or cur["kind"] != entry.get("kind")
    ):
        raise _Gone()
    ss = f["step_states"][cur["id"]]
    if ss["state"] != state:
        raise _Gone()
    return f, cur, ss


def decode_body(raw: bytes) -> Tuple[object, str, bool]:
    """``(payload, text, truncated)``: the body as JSON when it parses and
    fits in :data:`MAX_PAYLOAD`, else its text cut to that size; ``text`` is
    what a ``match`` looks in (the whole body)."""
    text = raw.decode("utf-8", "replace").replace("\x00", "")
    if len(text) <= MAX_PAYLOAD:
        try:
            return json.loads(text), text, False
        except ValueError:
            return text, text, False
    return text[:MAX_PAYLOAD], text, True


def payload_text(payload) -> str:
    """A stored payload as the text a ``match`` reads."""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def accept_listen(token: str, raw: bytes, now: Optional[float] = None) -> dict:
    """The listen inbound, blocking: ``{"heard": True}`` (stored, token spent)
    or ``{"ignored": True}`` (no ``match`` in it; token still armed). Raises
    :class:`_Gone` for an unknown / spent token or a stop not listening."""
    ts = float(now if now is not None else time.time())
    entry = lookup(token)
    if entry is None or entry.get("kind") != "listen":
        raise _Gone()
    payload, text, truncated = decode_body(raw)
    out: dict = {}

    def fn(f: dict) -> None:
        cur = _flights._current(f)
        if (
            f["state"] in _flights.FINAL
            or cur is None
            or cur["id"] != entry["step"]
            or cur["kind"] != "listen"
        ):
            raise _flights.FlightError("gone", 404, "not listening")
        ss = f["step_states"][cur["id"]]
        if ss["state"] != "running" or ss["detail"].get("token") != token:
            raise _flights.FlightError("gone", 404, "not listening")
        match = str(cur["config"].get("match") or "")
        if match and match not in text:
            ss["detail"]["ignored"] = text[-2000:]
            ss["detail"]["ignored_at"] = ts
            out["ignored"] = True
            return
        ss["detail"]["payload"] = payload
        ss["detail"]["heard_at"] = ts
        if truncated:
            ss["detail"]["truncated"] = True
        out["heard"] = True

    with _LOCK:
        # One inbound at a time: a second POST racing the first finds the
        # token spent instead of overwriting the payload.
        e = _load_locked().get(token)
        if not isinstance(e, dict) or e.get("spent"):
            raise _Gone()
        try:
            if _flights.mutate(entry["flight"], fn) is None:
                raise _Gone()
        except _flights.FlightError:
            raise _Gone()
        if out.get("heard"):
            tokens = _load_locked()
            tokens[token]["spent"] = True
            tokens[token]["spent_at"] = ts
            _save_locked(tokens)
    _wake()
    return out


def someone_view(token: str) -> dict:
    """``{title, text, items, who}`` of a someone perch waiting on its link,
    blocking; :class:`_Gone` otherwise."""
    entry = lookup(token)
    if entry is None or entry.get("kind") != "someone":
        raise _Gone()
    f, step, ss = _stop_of(entry, "waiting_you")
    if ss["detail"].get("token") != token:
        raise _Gone()
    cfg = step["config"]
    return {
        "title": step["title"],
        "text": ss["note"] or cfg.get("text") or step["title"],
        "items": [
            str(i) for i in (ss["detail"].get("items") or cfg.get("items") or [])
        ],
        "who": str(cfg.get("who") or ""),
        "plan": f["plan_name"],
    }


def someone_done(token: str, now: Optional[float] = None) -> str:
    """Done on the someone perch behind ``token``, blocking: the receipt
    "done by <who>" recorded, then the driver's own Done (``operate``). The
    token is spent either way once the stop is found."""
    from backend.web.core import flight_driver as _driver

    view = someone_view(token)
    entry = lookup(token) or {}
    who = view["who"] or "someone"
    receipt = ("done by %s" % who)[:80]

    def fn(f: dict) -> None:
        ss = f["step_states"].get(entry.get("step") or "")
        if ss is None or ss["state"] != "waiting_you":
            raise _flights.FlightError("gone", 404, "not waiting")
        ss["detail"]["receipt"] = receipt
        ss["detail"]["by"] = "someone"

    try:
        if _flights.mutate(entry["flight"], fn) is None:
            raise _Gone()
        _driver.operate(entry["flight"], "done", entry["step"], note=receipt)
    except _flights.FlightError:
        raise _Gone()
    finally:
        spend(token, now)
    return who


# --------------------------------------------------------------------------- #
# The routes (public: auth.py ``_PUBLIC_FLIGHT_HOOK``)
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<style>
body{font:16px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px;
background:#f6f5f2;color:#1d1d1b}
main{max-width:520px;margin:0 auto;background:#fff;border-radius:12px;
padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
h1{font-size:18px;margin:0 0 8px}p{white-space:pre-wrap;margin:0 0 12px}
ul{padding-left:20px;margin:0 0 16px}small{color:#6b6b66}
button{font:inherit;font-weight:600;padding:10px 22px;border:0;border-radius:8px;
background:#2f6f4f;color:#fff;cursor:pointer}
@media (prefers-color-scheme:dark){body{background:#151513;color:#eceae4}
main{background:#22221f}small{color:#9a9890}}
</style></head><body><main>%(body)s</main></body></html>"""


def _build_router():
    """The hook routes, or None without the web extra (the engine imports
    ``flights`` — and so this module — on its own too)."""
    try:
        from fastapi import APIRouter, Request
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:
        return None

    router = APIRouter()

    def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            _PAGE % {"title": html.escape(title), "body": body},
            status_code=status,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    def _gone_page() -> HTMLResponse:
        return _page(
            "Nothing to do",
            "<h1>Nothing to do</h1><p>This link was already used, or the flight "
            "it belonged to has moved on.</p>",
            404,
        )

    @router.post("/api/hooks/flight/{token}")
    async def flight_hook_listen(token: str, request: Request) -> JSONResponse:
        """A listen stop's inbound hook → ``{ok, heard}`` (200) or ``{ok,
        ignored}`` (202, the body lacked the stop's ``match``); 404 for an
        unknown or spent token, 413 for a body over 256 KB."""
        if lookup(token) is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_READ:
                return JSONResponse({"error": "too_large"}, status_code=413)
        try:
            out = await asyncio.to_thread(accept_listen, token, raw)
        except _Gone:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if out.get("ignored"):
            return JSONResponse({"ok": True, "ignored": True}, status_code=202)
        return JSONResponse({"ok": True, "heard": True})

    @router.get("/api/hooks/flight/{token}/done")
    async def flight_hook_done_page(token: str) -> HTMLResponse:
        """The someone perch's one-tap page (no side effect)."""
        try:
            view = await asyncio.to_thread(someone_view, token)
        except _Gone:
            return _gone_page()
        items = "".join("<li>%s</li>" % html.escape(i) for i in view["items"])
        body = (
            "<h1>%s</h1><p>%s</p>%s"
            '<form method="post"><button type="submit">Done</button></form>'
            "<p><small>%s · MindFlock</small></p>"
        ) % (
            html.escape(view["title"]),
            html.escape(view["text"]),
            "<ul>%s</ul>" % items if items else "",
            html.escape(view["plan"]),
        )
        return _page(view["title"], body)

    @router.post("/api/hooks/flight/{token}/done")
    async def flight_hook_done(token: str) -> HTMLResponse:
        """Done on the someone perch → a Thanks page; 404 for an unknown or spent
        token, or a perch no longer waiting."""
        try:
            await asyncio.to_thread(someone_done, token)
        except _Gone:
            return _gone_page()
        return _page("Thanks", "<h1>Thanks</h1><p>Marked done. You can close this.</p>")

    return router


router = _build_router()
