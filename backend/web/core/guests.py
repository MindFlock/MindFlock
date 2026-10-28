"""Guest links: let someone else type into a FEW of this device's sessions.

A peer link (docs/peer-link.md) joins two MindFlocks. This is the simpler
thing: one person, one MindFlock, and a collaborator who opens a link in a
browser and works in the sessions the owner picked — the rest of the rail
simply isn't there for them. No second install, no sandbox: they sit at the owner's
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
403 that says so. ``GET /api/instances`` lists only the rows the link
reaches: a session the guest may not open is not a crossed-out stub, it is
absent (nothing to leak, nothing to wonder about).

The guest's FOCUS — which session their page has selected — is reported by
their browser (``POST /api/instances/<title>/focus``, heartbeat every half
minute) and kept here per link (:func:`note_focus`, :func:`focus_of`), so
the owner's rail can show a pill with the guest's name on the row they are
looking at and follow them from session to session (``guest.focus_changed``
on the bus; ``GET /api/guests`` rows carry ``focus`` for a fresh page). A
focus older than :data:`FOCUS_TTL_S` is forgotten: the guest left.

The gate must be ON for any of this to mean anything (with it off, a link is
just the URL everyone can already open), so creating a link needs it on, and
the owner's token keeps working beside the links; rotating it (Settings →
Security) does not touch them — revoke a link on its own, here.

Persisted as ``<config dir>/guest_links.json`` (the title_store pattern):
``id -> {token, label, sessions, created_at}``. The token is a 160-bit
``token_urlsafe``; comparisons are constant-time. Links name sessions by
title, so a session closed and later recreated under the same title is open
to the link again — the owner made the title the unit of sharing.

A link reaches its named sessions AND their sub-sessions: anything whose
live parent chain (:mod:`backend.web.core.lineage`) leads to a named title,
whether it existed when the link was made or an agent spawns it later
(:func:`reach`, computed per request, so a worker that appears mid-visit is
open to the guest at once). Sub-sessions are never named on a link
themselves — they belong to their parent, and the picker leaves them out.
The owner's RENAMES are what the guest sees too: ``GET /api/aliases`` is
in the read-only set (filtered to the link's reach) and ``session.renamed``
reaches the guest's stream, so the shared session is called what the owner
calls it, now and after any later rename.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from typing import Dict, Iterable, List, Optional

from backend import config
from backend.web.core import lineage as _lineage
from backend.web.core import title_store

_LOCK = threading.Lock()
_LINKS: Optional[Dict[str, dict]] = None  # id -> link record
#: Bumped on every revoke / edit. A guest websocket remembers the value it
#: connected under and re-checks its token when it moves (auth middleware),
#: so Revoke / Kill cuts an open terminal within a frame, not at the next
#: page load.
_REVISION = 0

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
        "focus",
    }
)

#: How long a guest's last focus report stands for "they are here" (their
#: page re-reports every 30 s while open).
FOCUS_TTL_S = 90.0
#: link id -> {"title", "ts"}: where each guest's page is, in memory only.
_FOCUS: Dict[str, dict] = {}

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
    {
        "/api/voice/transcribe",
        "/api/voice/speak",
        "/api/paste-image",
        # The guest introducing themselves (:func:`set_name`).
        "/api/guest/name",
    }
)

#: Longest name a guest may pick.
MAX_NAME = 40

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
            "name": str(v.get("name") or "").strip()[:MAX_NAME],
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
    """The row the owner sees: everything but the token (the URL carries it),
    plus where the guest's page is right now (``focus``, None when they are
    not here)."""
    return {
        "id": link_id,
        "label": rec["label"],
        "name": rec.get("name", ""),
        "sessions": list(rec["sessions"]),
        "created_at": rec["created_at"],
        "focus": focus_of(link_id),
    }


def display_name(rec: dict) -> str:
    """What a guest is called: the name they picked, else the owner's label
    for the link (older links), else "Guest"."""
    return str(rec.get("name") or rec.get("label") or "").strip() or "Guest"


def _owner_name() -> str:
    try:
        from backend.web.core import person as _person

        return str(_person.current().get("name") or "")
    except Exception:  # noqa: BLE001
        return ""


def name_taken_by(name: str, exclude: str = "") -> str:
    """Who already goes by ``name`` (case-insensitively): another link's
    guest (its id), the owner ("owner"), or "" when it is free. Two people
    on one rail may not share a pill."""
    want = str(name or "").strip().casefold()
    if not want:
        return ""
    if want == _owner_name().strip().casefold():
        return "owner"
    with _LOCK:
        for link_id, rec in _load().items():
            if link_id != exclude and display_name(rec).casefold() == want:
                return link_id
    return ""


def set_name(link_id: str, name: str) -> str:
    """The guest picks what they are called on the owner's rail. Required
    before they are shown anywhere; must be unique among the people on this
    rail. ``ValueError`` says why not. Returns the name as stored."""
    clean = " ".join(str(name or "").split())[:MAX_NAME]
    if not clean:
        raise ValueError("pick a name")
    taken = name_taken_by(clean, exclude=link_id)
    if taken:
        raise ValueError(
            "someone here already goes by “%s” — pick another name" % clean
        )
    with _LOCK:
        links = _load()
        rec = links.get(link_id)
        if rec is None:
            raise ValueError("this link is gone")
        rec["name"] = clean
        _persist()
    # A pill already on the rail changes its text at once.
    cur = focus_of(link_id)
    if cur is not None:
        _emit_focus(link_id, clean, "", cur["title"], joined=False)
    return clean


# --------------------------------------------------------------------------- #
# Focus: where each guest's page is
# --------------------------------------------------------------------------- #
def focus_of(link_id: str) -> Optional[dict]:
    """``{title, ts}`` of the guest's last focus report, None when there is
    none or it is older than :data:`FOCUS_TTL_S` (they left)."""
    rec = _FOCUS.get(link_id)
    if not rec or time.time() - float(rec.get("ts") or 0) > FOCUS_TTL_S:
        return None
    return {"title": rec["title"], "ts": rec["ts"]}


def note_focus(guest: dict, title: str) -> dict:
    """The guest's page selected ``title`` (or is still on it: the heartbeat).
    Emits ``guest.focus_changed`` when the session changed or they had been
    away — so the owner's rail moves the pill the moment it happens, and the
    first report after a while reads as "joined" (``data.joined``)."""
    link_id = str(guest.get("id") or "")
    before = focus_of(link_id)
    rec = {"title": str(title or ""), "ts": time.time()}
    _FOCUS[link_id] = rec
    if before is None or before["title"] != rec["title"]:
        _emit_focus(
            link_id,
            display_name(guest),
            before["title"] if before else "",
            rec["title"],
            joined=before is None,
        )
    return {"title": rec["title"], "ts": rec["ts"]}


def _emit_focus(link_id: str, who: str, old: str, new: str, *, joined: bool) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(
            "guest.focus_changed",
            session=new,
            old=old,
            new=new,
            data={"link": link_id, "label": who, "title": new, "joined": joined},
        )
    except Exception:  # noqa: BLE001 — a pill is never worth a 500
        pass


def forget_focus(link_id: str = "") -> None:
    """Drop one link's focus (revoked), or every link's."""
    if link_id:
        _FOCUS.pop(link_id, None)
    else:
        _FOCUS.clear()


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
        "name": "",
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
            global _REVISION
            _REVISION += 1  # a narrowed link drops the sockets it lost
        if label is not None:
            rec["label"] = str(label or "").strip()[:MAX_LABEL]
        _persist()
        return _public(link_id, rec)


def revoke(link_id: str) -> bool:
    """Forget a link: its token stops working on the next request, and its
    open sockets close (:func:`revision`)."""
    global _REVISION
    with _LOCK:
        links = _load()
        if link_id not in links:
            return False
        links.pop(link_id)
        _persist()
        _REVISION += 1
        forget_focus(link_id)
        return True


def kill_all() -> int:
    """The kill switch: revoke every link at once. Returns how many."""
    global _REVISION
    with _LOCK:
        links = _load()
        n = len(links)
        if n:
            links.clear()
            _persist()
        _REVISION += 1
        forget_focus()
        return n


def revision() -> int:
    return _REVISION


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
                    # ``_token`` is for the middleware's socket guard only:
                    # public_scope never copies it out.
                    return {
                        "id": link_id,
                        "label": rec["label"],
                        "name": rec.get("name", ""),
                        "sessions": list(rec["sessions"]),
                        "_token": rec["token"],
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
def _live_instances() -> dict:
    """This server's live sessions (``title -> Instance``), a snapshot; ``{}``
    when the engine is not up (unit tests of the pure policy). Never raises."""
    try:
        from backend.web import server as _srv

        return dict(_srv.ENGINE.instances)
    except Exception:  # noqa: BLE001
        return {}


def reach(guest: dict, instances: Optional[dict] = None) -> List[str]:
    """Every title the link opens: the named sessions, then (sorted) every
    live session whose parent chain leads to one of them — a sub-session
    rides with its parent, whenever it was spawned. ``instances`` defaults to
    the engine's live sessions."""
    named = [str(t) for t in guest.get("sessions", ()) if t]
    if not named:
        return []
    live = _live_instances() if instances is None else instances
    is_named = set(named)
    extra = sorted(
        t
        for t in live
        if t not in is_named
        and any(a in is_named for a in _lineage.ancestors_of(live, t))
    )
    return named + extra


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
        if title not in reach(guest):
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
    """What ``/api/config`` tells the SPA: ``{label, sessions}`` for a guest
    (``sessions`` = the link's whole reach, sub-sessions included), None for
    the owner."""
    g = scope_guest(scope)
    if g is None:
        return None
    return {
        "label": g.get("label", ""),
        "name": g.get("name", ""),
        "sessions": reach(g),
    }


def aliases_for(guest: dict, aliases: Dict[str, str]) -> Dict[str, str]:
    """The owner's renames a guest may read: those of the sessions the link
    reaches, nothing about the crossed-out rows."""
    allowed = set(reach(guest))
    return {t: a for t, a in aliases.items() if t in allowed}


def stub_rows(rows: List[dict], guest: dict) -> List[dict]:
    """``GET /api/instances`` for a guest: the rows the link reaches (named
    sessions and their sub-sessions), as is — and nothing else. A session
    the guest may not open is left out, not crossed out: their rail is their
    sessions, full stop. Another device's rows are never theirs."""
    allowed = set(reach(guest))
    return [
        r
        for r in rows
        if isinstance(r, dict) and r.get("title") in allowed and not r.get("device")
    ]


def event_allowed(env: dict, guest: dict) -> bool:
    """Whether one ``/api/events`` envelope may reach a guest: its own
    sessions' events and the session-less ones (hello, server-wide)."""
    s = env.get("session") or ""
    return not s or s in reach(guest)
