"""Guest links: let someone else type into a FEW of this device's sessions.

A peer link (docs/peer-link.md) joins two MindFlocks. This is the simpler
thing: one person, one MindFlock, and a collaborator who opens a link in a
browser and works in the sessions the owner picked — the rest of the rail is
crossed out for them. No second install, no sandbox: they sit at the owner's
agent, in the owner's worktree, on the owner's machine, which is exactly what
"come look at this with me" means.

The link is the access-token gate (:mod:`backend.web.core.auth`) with a
second kind of token. A guest token signs a browser in like the owner's does
(``?token=`` → cookie → every request), but the middleware records which
link it is and :func:`path_allowed` decides per request: anything under
``/api/instances/<title>`` for a title on the link, the handful of read-only
boot calls the web UI needs, the live event stream (filtered to those
sessions by the server), and the static page itself. Everything else — other
sessions, creating sessions, settings, the owner's token, these links — is a
403 that says so. ``GET /api/instances`` still lists every row, but a row the
guest may not open is a stub (``locked: true``, title and status only), so the
rail can cross it out without leaking what it is doing.

The gate must be ON for any of this to mean anything (with it off, a link is
just the URL everyone can already open), so creating a link needs it on, and
the owner's token keeps working beside the links; rotating it (Settings →
Security) does not touch them — revoke a link on its own, here.

Persisted as ``<config dir>/guest_links.json`` (the title_store pattern):
``id -> {token, label, sessions, created_at}``. The token is a 160-bit
``token_urlsafe``; comparisons are constant-time. Links name sessions by
title, so a session closed and later recreated under the same title is open
to the link again — the owner made the title the unit of sharing.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from typing import Dict, Iterable, List, Optional

from backend import config
from backend.web.core import title_store

_LOCK = threading.Lock()
_LINKS: Optional[Dict[str, dict]] = None  # id -> link record

#: Longest label / most sessions one link carries — bounds, not features.
MAX_LABEL = 80
MAX_SESSIONS = 50

#: The session sub-routes a guest may change (anything else under their
#: session answers GET only): the typing surface — the terminal and shell
#: sockets, prompts, answers, the queue, voice, find, the family thread,
#: waking a sleeping agent, background-job expectations, the map's ask-plan.
#: Not: closing, deleting, copying, committing, pushing, PRs, fast-track,
#: branches, fences, zones, owner/parent/order/profile — the owner's calls.
SESSION_WRITE_OK = frozenset(
    {
        "terminal",
        "shell",
        "send",
        "answer",
        "asking",
        "dialog",
        "prompt",
        "queue",
        "voice",
        "find",
        "messages",
        "thread",
        "wake",
        "jobs",
        "background",
        "code-map",
    }
)

#: Read-only routes the SPA needs to boot and draw a rail. Each one is checked
#: for what it reveals: nothing about sessions the guest may not open, no
#: token, no settings. ``/api/instances`` is here too — its rows are stubbed
#: for a guest by :func:`stub_rows`.
GLOBAL_GET_OK = frozenset(
    {
        "/api/instances",
        "/api/config",
        "/api/prefs",
        "/api/aliases",
        "/api/addons",
        "/api/devices",
        "/api/providers",
        "/api/providers/status",
        "/api/setup",
        "/api/person",
        "/api/voice",
        "/api/voice/help",
        "/api/mindflock/status",
    }
)

#: Global POSTs that only serve the guest's own typing: speech in and out,
#: and an image pasted into a pane.
GLOBAL_POST_OK = frozenset(
    {"/api/voice/transcribe", "/api/voice/speak", "/api/paste-image"}
)

REFUSAL = "not part of your invite"


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def _path() -> str:
    return os.path.join(config.GetConfigDir(), "guest_links.json")


def _clean(d: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for k, v in d.items():
        if (
            not isinstance(v, dict)
            or not isinstance(v.get("token"), str)
            or not v["token"]
        ):
            continue
        out[str(k)] = {
            "token": v["token"],
            "label": str(v.get("label") or "")[:MAX_LABEL],
            "sessions": _titles(v.get("sessions")),
            "created_at": float(v.get("created_at") or 0),
        }
    return out


def _load() -> Dict[str, dict]:
    global _LINKS
    if _LINKS is None:
        _LINKS = title_store.load(_path, _clean)
    return _LINKS


def _persist() -> None:
    title_store.persist(_path, _LINKS, "guest links")


def _titles(v) -> List[str]:
    """Distinct, non-empty session titles, in the order given, capped."""
    if not isinstance(v, (list, tuple)):
        return []
    seen: List[str] = []
    for t in v:
        t = str(t or "").strip()
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= MAX_SESSIONS:
            break
    return seen


def _public(link_id: str, rec: dict) -> dict:
    """The row the owner sees: everything but the token (the URL carries it)."""
    return {
        "id": link_id,
        "label": rec["label"],
        "sessions": list(rec["sessions"]),
        "created_at": rec["created_at"],
    }


def create(label: str, sessions: Iterable[str]) -> dict:
    """Mint a link for ``sessions``. Raises ``ValueError`` with no sessions.
    Returns the public row plus ``token`` (the one time it is handed out
    beside the row — the route turns it into the URL)."""
    titles = _titles(list(sessions))
    if not titles:
        raise ValueError("pick at least one session")
    rec = {
        "token": secrets.token_urlsafe(20),
        "label": str(label or "").strip()[:MAX_LABEL],
        "sessions": titles,
        "created_at": time.time(),
    }
    link_id = secrets.token_hex(6)
    with _LOCK:
        links = _load()
        while link_id in links:
            link_id = secrets.token_hex(6)
        links[link_id] = rec
        _persist()
    return {**_public(link_id, rec), "token": rec["token"]}


def update(
    link_id: str, *, label: Optional[str] = None, sessions=None
) -> Optional[dict]:
    """Change a link's label and/or sessions in place (the URL stays). None
    for an unknown id; ``ValueError`` when the new session list is empty."""
    with _LOCK:
        links = _load()
        rec = links.get(link_id)
        if rec is None:
            return None
        if sessions is not None:
            titles = _titles(list(sessions))
            if not titles:
                raise ValueError("pick at least one session")
            rec["sessions"] = titles
        if label is not None:
            rec["label"] = str(label or "").strip()[:MAX_LABEL]
        _persist()
        return _public(link_id, rec)


def revoke(link_id: str) -> bool:
    """Forget a link: its token stops working on the next request."""
    with _LOCK:
        links = _load()
        if link_id not in links:
            return False
        links.pop(link_id)
        _persist()
        return True


def list_links() -> List[dict]:
    """Every link, oldest first, without tokens."""
    with _LOCK:
        links = _load()
        rows = [_public(k, v) for k, v in links.items()]
    rows.sort(key=lambda r: r["created_at"])
    return rows


def token_of(link_id: str) -> str:
    """The link's token (for the owner's Copy button), ``""`` if unknown."""
    with _LOCK:
        rec = _load().get(link_id)
        return rec["token"] if rec else ""


# --------------------------------------------------------------------------- #
# Authentication: which link a credential is
# --------------------------------------------------------------------------- #
def guest_for(candidate: Optional[str]) -> Optional[dict]:
    """The link ``candidate`` is the token of — ``{id, label, sessions}`` —
    or None. Constant-time per link; never raises (the request path)."""
    if not candidate:
        return None
    try:
        cand = str(candidate)
        with _LOCK:
            for link_id, rec in _load().items():
                if hmac.compare_digest(cand, rec["token"]):
                    return {
                        "id": link_id,
                        "label": rec["label"],
                        "sessions": list(rec["sessions"]),
                    }
    except Exception:  # noqa: BLE001 — a broken store is "no guest", never a 500
        return None
    return None


def guest_for_any(
    candidates: Iterable[Optional[str]], limit: int = 16
) -> Optional[dict]:
    for i, cand in enumerate(candidates):
        if i >= limit:
            break
        g = guest_for(cand)
        if g is not None:
            return g
    return None


def any_links() -> bool:
    """Cheap pre-check for the middleware: no links, no guest lookups."""
    try:
        with _LOCK:
            return bool(_load())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Authorization: what a link may reach
# --------------------------------------------------------------------------- #
def _session_of(path: str):
    """``(title, first sub-segment)`` for ``/api/instances/<title>[/...]``,
    else None. ``scope["path"]`` is already percent-decoded."""
    prefix = "/api/instances/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    title, _, tail = rest.partition("/")
    action = tail.split("/", 1)[0]
    return title, action


def path_allowed(method: str, path: str, guest: dict) -> bool:
    """Whether a request on ``path`` is inside the link's invite."""
    method = (method or "GET").upper()
    if not path.startswith("/api/"):
        return True  # the page, its assets, /m
    if path == "/api/events":
        return True  # filtered per envelope by the server
    sess = _session_of(path)
    if sess is not None:
        title, action = sess
        if title not in guest.get("sessions", ()):
            return False
        if method in ("GET", "HEAD", "OPTIONS"):
            return True
        return action in SESSION_WRITE_OK
    if method in ("GET", "HEAD"):
        return path in GLOBAL_GET_OK
    if method == "POST":
        return path in GLOBAL_POST_OK
    return False


def scope_guest(scope) -> Optional[dict]:
    """The link a request was signed in with (set by the middleware), or
    None for the owner. Takes a Request, a WebSocket or an ASGI scope."""
    try:
        sc = getattr(scope, "scope", scope)
        state = sc.get("state") if isinstance(sc, dict) else None
        g = state.get("guest") if isinstance(state, dict) else None
        return g if isinstance(g, dict) else None
    except Exception:  # noqa: BLE001
        return None


def public_scope(scope) -> Optional[dict]:
    """What ``/api/config`` tells the SPA: ``{label, sessions}`` for a guest,
    None for the owner."""
    g = scope_guest(scope)
    if g is None:
        return None
    return {"label": g.get("label", ""), "sessions": list(g.get("sessions", []))}


def stub_rows(rows: List[dict], guest: dict) -> List[dict]:
    """``GET /api/instances`` for a guest: every row the link names, as is;
    every other row cut down to what the rail needs to cross it out."""
    allowed = set(guest.get("sessions", ()))
    out: List[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("title") in allowed and not r.get("device"):
            out.append(r)
        else:
            out.append(
                {
                    "title": r.get("title", ""),
                    "status": r.get("status", ""),
                    "device": r.get("device", ""),
                    "hidden_by": r.get("hidden_by", ""),
                    "pending": r.get("pending", False),
                    "locked": True,
                }
            )
    return out


def event_allowed(env: dict, guest: dict) -> bool:
    """Whether one ``/api/events`` envelope may reach a guest: its own
    sessions' events and the session-less ones (hello, server-wide)."""
    s = env.get("session") or ""
    return not s or s in guest.get("sessions", ())
