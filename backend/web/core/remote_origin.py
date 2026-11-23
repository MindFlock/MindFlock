"""Sessions another of your devices started here — and the ones this device
started there.

Before this, a session that an agent on the laptop started on ML-Rig-2023
(``send_session``, ``send_files``, a forwarded create) said only "agent" there
and then sat idle forever. Now:

* **Provenance** (the receiving side, :func:`note_created`). A create that
  arrives over the authenticated device link (``POST /api/instances`` with
  the remote marker and a credential) records WHO made it: the origin device
  — taken from the link itself (:func:`caller`: the tailnet address the
  request came from names a discovered device, else the marker's claim,
  recorded as such) and never from a field of the request — plus the
  origin session's title and whether an agent or a person started it, from
  the create's ``origin: {session, by}``. Those two are INFORMATIONAL: the
  origin device says them about its own sessions; they are labelled so, and
  shown as one cleaned line. A forwarded create with no ``origin`` is the
  New Session dialog: "you, from <device>". Shown as the row's ``origin``
  (:func:`row_field`, :func:`label`): "from tripwire-send on
  EthanLaptop2026".
* **Finishing** (:func:`judge`, called by :mod:`auto_close`). A session an
  AGENT on another device started, with no person attached, closes itself
  once it is done — its agent ended its turn and stayed idle for the
  auto-close grace window — like a local finished worker: agent stopped,
  folder kept, its last reply in Recently closed. Never while it asks
  something, runs a background job, has a prompt or a message waiting, has
  a to-do of its own open for the owner, or once a person typed into it;
  never one a person started. "Keep" (``auto_close.set_keep_open``) stops
  it.
* **Control from the origin** (:func:`control_refusal`). The origin session
  may read the session's state, send it a follow-up message and close it —
  only a session THIS origin started, checked HERE, on the receiving side:
  the request must come over the device link (remote marker + a valid
  credential, remote control on, never a guest link), from the device the
  record names (as the link identifies it), naming the very origin session
  recorded. Its route (``/api/origin-control``) is not under
  ``/api/instances/…``, so the namespaced-title proxy never forwards it, and
  it is not on the ``fwd/`` allow-list: no third device can relay it.
* **Reporting back.** The session's ``report_result``, and every turn it
  ends (:func:`turn_ends`, delivered by :func:`run_loop`), go to the origin
  session's inbox on the origin device (``/api/origin-inbox``), as a
  ``result`` from ``<device>::<title>`` — what ``wait_for_session`` there
  waits on. The origin device accepts one only from the device it started
  that session on (:func:`started_by`, recorded by :func:`note_started` when
  the create went out).

Persisted at ``<config dir>/remote_origin.json`` (or
``$MINDFLOCK_REMOTE_ORIGIN_FILE``): ``{"here": {title: {device,
device_name, verified, session, by, at, created_at, turn_sent,
report_sent}}, "started": {"<device>::<title>": {"origin", "at"}}}``.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend import log
from backend.fsutil import atomic_write_json, read_json

__all__ = [
    "BYS",
    "caller",
    "clean_origin",
    "note_created",
    "record",
    "label",
    "row_field",
    "forget",
    "rename",
    "note_started",
    "started_by",
    "control_refusal",
    "judge",
    "turn_ends",
    "run_loop",
]

#: Who started a session from another device.
BYS = ("agent", "person")
#: How long an origin session title may be (one cleaned line).
SESSION_CHARS = 120
#: An unpinned (queued) agent create's record matches the session made
#: within this many seconds of it (the launch gate's queue: memory).
_MATCH_S = 2 * 3600.0
#: A person's create is never queued: its session exists at once.
_PERSON_MATCH_S = 10 * 60.0
#: Turn ends are delivered after this much quiet (and at most once a turn).
TURN_SETTLE_S = 60.0
LOOP_S = 30.0
#: Records of sessions started ON other devices are dropped after this long.
STARTED_TTL_S = 14 * 24 * 3600.0
#: The last reply carried by a turn-end delivery.
REPLY_CHARS = 3000
#: A provenance record is kept this long (closed and reopened sessions too).
KEEP_RECORDS_S = 60 * 24 * 3600.0

_FileName = "remote_origin.json"
_LOCK = threading.RLock()


def _info(fmt: str, *args) -> None:
    logger = log.InfoLog or log.ErrorLog
    if logger is not None:
        try:
            logger.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def store_path() -> str:
    """``$MINDFLOCK_REMOTE_ORIGIN_FILE`` (tests point it at a tmp file), else
    ``<config dir>/remote_origin.json``; refused under pytest when it
    resolves into the real home (``backend.config.home_guard``)."""
    from backend.config.config import GetConfigDir
    from backend.config.home_guard import guard

    env = os.environ.get("MINDFLOCK_REMOTE_ORIGIN_FILE")
    if env:
        return guard(env, "remote origin")
    return guard(os.path.join(GetConfigDir(), _FileName), "remote origin")


#: (path, mtime_ns, size) -> parsed store: every /api/instances row asks.
_CACHE: Dict[str, object] = {"sig": None, "data": None}


def _signature(p: str):
    try:
        st = os.stat(p)
    except OSError:
        return (p, None, None)
    return (p, st.st_mtime_ns, st.st_size)


def _load() -> Dict[str, dict]:
    import copy

    p = store_path()
    sig = _signature(p)
    if _CACHE["sig"] == sig and isinstance(_CACHE["data"], dict):
        return copy.deepcopy(_CACHE["data"])  # type: ignore[arg-type]
    data = read_json(p, None) if sig[1] is not None else None
    if not isinstance(data, dict):
        data = {}
    here = data.get("here") if isinstance(data.get("here"), dict) else {}
    started = data.get("started") if isinstance(data.get("started"), dict) else {}
    out = {
        "here": {k: v for k, v in here.items() if isinstance(v, dict)},
        "started": dict(started),
    }
    _CACHE.update(sig=sig, data=copy.deepcopy(out))
    return out


def _save(data: Dict[str, dict]) -> None:
    try:
        atomic_write_json(store_path(), data, indent=None, newline=False)
    except OSError as err:
        _info("remote origin: could not save (%s)", err)
    _CACHE.update(sig=None, data=None)


# --------------------------------------------------------------------------- #
# who is calling
# --------------------------------------------------------------------------- #
def caller(request) -> dict:
    """The device a request over the device link came from →
    ``{device, device_name, verified}``: the discovered device whose tailnet
    address it came from (``verified`` — the address is bound to that node's
    key), else the remote marker's claim (any holder of a credential could
    write it: ``verified`` false, shown as "claims to be")."""
    from backend.web.core import remote as _remote

    try:
        ip = request.client.host if request.client else ""
    except Exception:  # noqa: BLE001
        ip = ""
    key = _remote.device_for_ip(ip)
    verified = bool(key)
    if not key:
        try:
            key = str(request.headers.get(_remote.REMOTE_HEADER, "") or "")
        except Exception:  # noqa: BLE001
            key = ""
    key = _clean(key, 80)
    dev = _remote._DEVICES.get(key) or {}
    name = _clean(str(dev.get("host") or "") or key, 80)
    return {"device": key, "device_name": name, "verified": verified}


def link_of(request) -> Optional[dict]:
    """:func:`caller` for a request that came over the device link (the
    remote marker), else None — this device's own browser, CLI and agents."""
    from backend.web.core import remote as _remote

    return caller(request) if _remote.from_remote(request) else None


async def create_over_link(payload: dict, link: Optional[dict], create):
    """``POST /api/instances`` with provenance: ``origin`` is taken out of
    ``payload`` (never a create key); for a create over the link (``link``,
    :func:`link_of`) an agent origin's prompt ends with where it came from
    (:func:`footer`), then ``create(payload)`` runs and a 2xx answer is
    recorded (:func:`note_created`). Without ``link`` the origin is dropped."""
    import json as _json

    origin = payload.pop("origin", None)
    if link is None:
        return await create(payload)
    clean = clean_origin(origin)
    title = str(payload.get("title") or "").strip()
    if clean is not None and clean["by"] == "agent" and title:
        preview = dict(link, **clean)
        payload["prompt"] = str(payload.get("prompt") or "") + footer(preview, title)
    resp = await create(payload)
    if resp.status_code in (200, 201, 202):
        if not title:
            try:
                title = str(_json.loads(resp.body).get("title") or "")
            except (ValueError, AttributeError):
                title = ""
        if title:
            created = None
            try:
                from backend.web import server as _server

                live = _server.ENGINE.instances.get(title)
                created = _created(live) if live is not None else None
            except Exception:  # noqa: BLE001
                created = None
            await asyncio.to_thread(note_created, title, link, clean, created)
    return resp


def _clean(text: str, limit: int) -> str:
    from backend.web.core.send_session import clean_line

    return clean_line(str(text or "").strip(), limit)


def clean_origin(raw) -> Optional[dict]:
    """A create's ``origin`` → ``{session, by}`` (session one cleaned line,
    ``by`` agent|person; agent when it names a session) or None when it isn't
    one."""
    if not isinstance(raw, dict):
        return None
    session = raw.get("session")
    session = _clean(session, SESSION_CHARS) if isinstance(session, str) else ""
    by = raw.get("by")
    if by not in BYS:
        by = "agent" if session else "person"
    return {"session": session, "by": by}


# --------------------------------------------------------------------------- #
# provenance (the receiving side)
# --------------------------------------------------------------------------- #
def note_created(
    title: str, who: dict, origin: Optional[dict], created: Optional[float] = None
) -> dict:
    """Record that ``who`` (:func:`caller`) asked for ``title`` here, with the
    create's ``origin`` (:func:`clean_origin`; None = a person's create from
    that device's New Session dialog). Returns the record."""
    o = origin or {"session": "", "by": "person"}
    rec = {
        "device": str(who.get("device") or ""),
        "device_name": str(who.get("device_name") or who.get("device") or ""),
        "verified": bool(who.get("verified")),
        "session": o["session"] if o["by"] == "agent" else "",
        "by": o["by"],
        "at": time.time(),
        # The session the create made, when it already exists (an admitted
        # create registers it before answering): pinned at once. A queued
        # create pins on first sight, under record()'s stricter rules.
        "created_at": created,
        "turn_sent": 0.0,
        "report_sent": 0.0,
    }
    with _LOCK:
        data = _load()
        # Records outlive a close (a reopened session keeps its origin: the
        # creation time still matches) but not forever.
        cutoff = time.time() - KEEP_RECORDS_S
        data["here"] = {
            t: r for t, r in data["here"].items() if float(r.get("at") or 0.0) >= cutoff
        }
        data["here"][title] = rec
        _save(data)
    return dict(rec)


def _created(inst) -> Optional[float]:
    try:
        return float(inst.CreatedAt.timestamp())
    except Exception:  # noqa: BLE001
        return None


def record(title: str, inst=None) -> Optional[dict]:
    """The provenance of the LIVE session ``title`` (``inst``, else looked
    up), or None. A record is bound to the session it was made for by its
    creation time: pinned on first sight, so a later session reusing the
    title inherits nothing."""
    with _LOCK:
        data = _load()
        rec = data["here"].get(title)
        if not isinstance(rec, dict):
            return None
        created = _created(inst) if inst is not None else None
        pinned = rec.get("created_at")
        if created is None:
            return dict(rec) if pinned is not None or inst is None else None
        if pinned is None:
            # Unpinned: a create that was queued (memory). Bind only a
            # session that plausibly IS that create: made soon enough after
            # it, agent-spawned when an agent asked, and no failure of that
            # create recorded since (a failed one must not lend its origin
            # to the next session of that name).
            at = float(rec.get("at") or 0.0)
            window = _MATCH_S if rec.get("by") == "agent" else _PERSON_MATCH_S
            if not (at - 120.0 <= created <= at + window):
                return None
            if rec.get("by") == "agent" and not getattr(inst, "Spawned", False):
                return None
            if _failed_since(title, at):
                return None
            rec["created_at"] = created
            data["here"][title] = rec
            _save(data)
            return dict(rec)
        return dict(rec) if abs(float(pinned) - created) < 1.0 else None


def _failed_since(title: str, at: float) -> bool:
    """Whether a create of ``title`` failed at or after ``at`` (the
    server's recent create failures)."""
    try:
        from backend.web import server as _server

        rec = _server._CREATE_FAILURES.get(title)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(rec, dict) and float(rec.get("ts") or 0.0) >= at - 1.0


def _update(title: str, **fields) -> None:
    with _LOCK:
        data = _load()
        rec = data["here"].get(title)
        if isinstance(rec, dict):
            rec.update(fields)
            _save(data)


def label(rec: Optional[dict]) -> str:
    """How a session's origin reads: "from tripwire-send on EthanLaptop2026"
    (an agent session there), "you, from EthanLaptop2026" (a person), "an
    agent on EthanLaptop2026" (an agent, no session named). A device known
    only by what it claimed says so."""
    if not rec:
        return ""
    name = str(rec.get("device_name") or rec.get("device") or "another device")
    where = name if rec.get("verified") else "%s (unverified)" % name
    if rec.get("by") == "person":
        return "you, from %s" % where
    if rec.get("session"):
        return "from %s on %s" % (rec["session"], where)
    return "an agent on %s" % where


def row_field(inst) -> Optional[dict]:
    """The row's ``origin``: ``{device, device_name, verified, session, by,
    label}`` for a session another device started here, else None."""
    try:
        rec = record(inst.Title, inst)
    except Exception:  # noqa: BLE001 — enrichment only
        return None
    if not rec:
        return None
    return {
        "device": rec.get("device") or "",
        "device_name": rec.get("device_name") or "",
        "verified": bool(rec.get("verified")),
        "session": rec.get("session") or "",
        "by": rec.get("by") or "person",
        "label": label(rec),
    }


def forget(title: str) -> None:
    with _LOCK:
        data = _load()
        if data["here"].pop(title, None) is not None:
            _save(data)


def rename(old: str, new: str) -> None:
    with _LOCK:
        data = _load()
        rec = data["here"].pop(old, None)
        if rec is not None:
            data["here"][new] = rec
            _save(data)


def footer(rec: dict, title: str) -> str:
    """What an agent-started session's first prompt ends with: where it came
    from, that the session title there is informational, and how it reports
    back."""
    return (
        "\n\n---\nThis session was started %s (another of your user's "
        "devices) — that session's name is as its device reported it. When "
        "you finish, call mcp__mindflock__report_result once with your "
        "status and a summary: it reaches that session. Once you are done "
        "and idle, this session closes by itself (its folder is kept)." % label(rec)
    )


# --------------------------------------------------------------------------- #
# sessions this device started ON other devices (the origin side)
# --------------------------------------------------------------------------- #
def _started_key(device: str, title: str) -> str:
    return "%s::%s" % (device, title)


def note_started(device: str, title: str, origin_session: str) -> None:
    """This device's ``origin_session`` started ``title`` on ``device`` (its
    key here): what :func:`started_matches` checks before a report or a
    control request about it is accepted. Pinned to that session's creation
    time, so a later session reusing its title inherits nothing."""
    if not device or not title or not origin_session:
        return
    now = time.time()
    origin_created = None
    try:
        from backend.web import server as _server

        live = _server.ENGINE.instances.get(origin_session)
        origin_created = _created(live) if live is not None else None
    except Exception:  # noqa: BLE001
        origin_created = None
    with _LOCK:
        data = _load()
        data["started"] = {
            k: v
            for k, v in data["started"].items()
            if isinstance(v, dict) and now - float(v.get("at") or 0) < STARTED_TTL_S
        }
        data["started"][_started_key(device, title)] = {
            "origin": origin_session,
            "origin_created": origin_created,
            "at": now,
        }
        _save(data)


def started_by(device: str, title: str) -> str:
    """The local session that started ``title`` on ``device``, "" when none
    of this device's did."""
    with _LOCK:
        rec = _load()["started"].get(_started_key(device, title))
    return str(rec.get("origin") or "") if isinstance(rec, dict) else ""


def started_matches(device: str, title: str, origin: str, origin_inst) -> bool:
    """Whether the LIVE session ``origin`` (``origin_inst``) is the one that
    started ``title`` on ``device`` — its title AND its creation time (a
    record from before pinning matches by title)."""
    if not device or not origin or origin_inst is None:
        return False
    with _LOCK:
        rec = _load()["started"].get(_started_key(device, title))
    if not isinstance(rec, dict) or str(rec.get("origin") or "") != origin:
        return False
    pinned = rec.get("origin_created")
    if pinned is None:
        return True
    created = _created(origin_inst)
    return created is not None and abs(float(pinned) - created) < 1.0


# --------------------------------------------------------------------------- #
# the provenance rule (enforced HERE, on the receiving side)
# --------------------------------------------------------------------------- #
def control_refusal(
    title: str, inst, who: dict, origin_session: str, closed: bool = False
) -> Optional[Tuple[int, str, str]]:
    """Why the caller ``who`` (:func:`caller`) may NOT act on ``title`` for
    ``origin_session`` → ``(status, code, sentence)``, or None when it may:
    ``title`` was started here by an agent session of exactly that name on
    exactly that device — as the link identifies it now, at least as surely
    as when it was created."""
    # ``closed``: the session is gone (asked for its status): its record,
    # unbound, still decides — nothing about it is said to anyone else.
    rec = (
        record(title, inst) if inst is not None else (record(title) if closed else None)
    )
    if rec is None:
        return (
            403,
            "not_origin",
            "%s was not started from another device — only the session that "
            "started one may control it from there" % title,
        )
    if rec.get("by") != "agent" or not rec.get("session"):
        return (
            403,
            "not_origin",
            "%s was started by a person, not by a session — it is controlled "
            "from its own device" % title,
        )
    # Exact keys: folding names is for what a person types, never for
    # authorization ("a-b" and "ab" are different devices).
    if not who.get("device") or str(who.get("device")) != str(rec.get("device") or ""):
        return (
            403,
            "not_origin",
            "%s was started from another device, not this one" % title,
        )
    if rec.get("verified") and not who.get("verified"):
        return (
            403,
            "not_origin",
            "this request did not come from %s's tailnet address — it was "
            "started over the tailnet, and is controlled only that way" % title,
        )
    if str(origin_session or "") != str(rec.get("session") or ""):
        return (
            403,
            "not_origin",
            "%s was started by another session on that device — only the "
            "session that started it may control it" % title,
        )
    return None


# --------------------------------------------------------------------------- #
# finishing: the auto-close rule for agent-started remote sessions
# --------------------------------------------------------------------------- #
R_PERSON = "a person started it"
R_NOT_IDLE = "not idle"
R_TYPED_EVER = "a person typed into it"
R_TODO = "a to-do of its own is open"
R_FINISHED = "done and idle (started by an agent on another device)"


def _open_todo(title: str) -> bool:
    """Whether a to-do the session raised for the owner (``add_todo`` —
    the item's ``session``) is still open. Unknown counts as open."""
    try:
        from backend.web.addons.todo import store as _todos

        return any(
            isinstance(t, dict)
            and str(t.get("session") or "") == title
            and not t.get("done")
            for t in _todos.list_items()
        )
    except Exception:  # noqa: BLE001
        return True


def judge(srv, inst, title: str, row, now: float, grace_s: float):
    """``(verdict, reason, closes_at)`` (the :mod:`auto_close` verdicts) for a
    session another device started here, or None when it isn't one (the
    ordinary worker rules apply). Never closes one a person started."""
    from backend.web.core import auto_close as ac

    rec = record(title, inst)
    if rec is None:
        return None
    if rec.get("by") != "agent":
        return ac.KEEP, R_PERSON, None
    row = row or {}
    if row.get("hidden_by"):
        return ac.KEEP, ac.R_HIDDEN, None
    created = _created(inst)
    if ac.keep_open(title, created):
        return ac.KEEP, ac.R_KEEP_OPEN, None
    try:
        from backend import session as _session

        if not inst.Started() or inst.Status in (_session.Paused, _session.Loading):
            return ac.KEEP, ac.R_STATUS, None
    except Exception:  # noqa: BLE001
        return ac.KEEP, ac.R_STATUS, None
    if row.get("workspace_missing"):
        return ac.KEEP, ac.R_WORKSPACE, None
    human = 0.0
    try:
        human = float(srv._HUMAN_INPUT_AT.get(title, 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        pass
    if human > 0:
        # A person took it over: it is theirs now, never closed for them.
        return ac.KEEP, R_TYPED_EVER, None
    asleep = bool(row.get("sleeping"))
    activity = str(row.get("activity") or "")
    since = float(row.get("activity_since") or 0.0)
    if not asleep and (activity not in ("idle", "offline") or since <= 0):
        return ac.KEEP, R_NOT_IDLE, None
    if row.get("asking"):
        return ac.KEEP, ac.R_ASKING, None
    if row.get("background"):
        return ac.KEEP, ac.R_BACKGROUND, None
    try:
        from backend.web.core import prompt_queue as _prompt_queue

        if _prompt_queue.get_state(title)["items"]:
            return ac.KEEP, ac.R_QUEUE, None
    except Exception:  # noqa: BLE001
        return ac.KEEP, ac.R_QUEUE, None
    try:
        from backend.web.core import mailbox as _mailbox

        if _mailbox.next_pending(title) is not None:
            return ac.KEEP, ac.R_MAIL, None
    except Exception:  # noqa: BLE001
        return ac.KEEP, ac.R_MAIL, None
    try:
        if srv._autopilot_running(title):
            return ac.KEEP, ac.R_AUTOPILOT, None
    except Exception:  # noqa: BLE001
        return ac.KEEP, ac.R_AUTOPILOT, None
    if ac._has_live_children(srv.ENGINE.instances, title):
        return ac.KEEP, ac.R_WORKERS, None
    if _open_todo(title):
        return ac.KEEP, R_TODO, None
    # Its turn ended at ``since``; a message delivered after that (the
    # origin's follow-up) restarts the clock.
    finished_at = max(since if not asleep else 0.0, ac._last_poke(srv, title))
    if now - finished_at < grace_s:
        return ac.WAIT, ac.R_GRACE, finished_at + grace_s
    from backend.session import tmux
    from backend.web.core import ownership as _ownership
    from backend.web.core import sleep as _sleep

    if _ownership.terminals(title) > 0:
        return ac.WAIT, ac.R_VIEWED, None
    try:
        if _sleep._outside_clients(tmux.to_mindflock_tmux_name(title)) > 0:
            return ac.WAIT, ac.R_VIEWED, None
    except Exception:  # noqa: BLE001
        pass
    return ac.CLOSE, R_FINISHED, None


# --------------------------------------------------------------------------- #
# reporting back to the origin
# --------------------------------------------------------------------------- #
def turn_ends(srv, rows: List[dict], now: Optional[float] = None) -> List[dict]:
    """The agent-started sessions from other devices whose agent ended a
    turn that was not reported yet → ``[{title, rec, since}]``: idle (or
    asleep) for :data:`TURN_SETTLE_S`, the turn newer than the last one
    delivered and than its last report_result. Dict reads only."""
    now = time.time() if now is None else now
    out = []
    for r in rows:
        title = str(r.get("title") or "")
        if not title or r.get("device"):
            continue
        inst = srv.ENGINE.instances.get(title)
        if inst is None:
            continue
        rec = record(title, inst)
        if not rec or rec.get("by") != "agent" or not rec.get("session"):
            continue
        if str(r.get("activity") or "") not in ("idle", "offline") or r.get("asking"):
            continue
        since = float(r.get("activity_since") or 0.0)
        if since <= 0 or now - since < TURN_SETTLE_S:
            continue
        if since <= max(
            float(rec.get("turn_sent") or 0.0), float(rec.get("report_sent") or 0.0)
        ):
            continue
        out.append({"title": title, "rec": rec, "since": since})
    return out


async def deliver(rec: dict, title: str, text: str, data: dict) -> Tuple[int, dict]:
    """Post ``text`` to the origin session's inbox on the origin device →
    ``(http status, answer)``; 0 when the device can't be reached."""
    from backend.web.core import remote as _remote

    dev = _remote.find_device(str(rec.get("device") or ""))
    if dev is None or not _remote.is_connected(dev):
        return 0, {"error": "%s is not reachable from here" % rec.get("device_name")}
    status, body = await _remote.post_json(
        dev,
        "/api/origin-inbox",
        {
            "to": rec.get("session") or "",
            "from": title,
            "kind": "result",
            "text": text,
            "data": data,
        },
        20.0,
    )
    return status, body if isinstance(body, dict) else {}


def note_reported(title: str, at: Optional[float] = None) -> None:
    _update(title, report_sent=time.time() if at is None else at)


#: One deliverer per config dir: two servers on one ``~/.mindflock`` would
#: each send the same turn end (``sleep.hold_lease``, as the auto-close
#: sweep does).
LEASE = "remote-origin"


async def _tick(srv) -> None:
    from backend.web.core import events as _events
    from backend.web.core import sleep as _sleep

    if not await asyncio.to_thread(_sleep.hold_lease, LEASE, 3 * LOOP_S):
        return

    rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
    for item in turn_ends(srv, rows):
        title, rec = item["title"], item["rec"]
        try:
            reply = await asyncio.to_thread(_last_reply, srv, title)
        except Exception:  # noqa: BLE001
            reply = ""
        text = "%s ended its turn%s" % (
            title,
            (":\n\n" + reply) if reply else " (no reply recorded).",
        )
        status, _ = await deliver(
            rec, title, text, {"status": "idle", "turn_end": True}
        )
        if status in (200, 201):
            _update(title, turn_sent=item["since"])
        elif status in (403, 404, 409):
            # The origin refused (its session is gone, or it doesn't know
            # this one): don't knock again for this turn.
            _update(title, turn_sent=item["since"])


def _last_reply(srv, title: str) -> str:
    inst = srv.ENGINE.instances.get(title)
    if inst is None:
        return ""
    try:
        kind, text, *_ = srv._agent_output(inst, title, "last_reply")
    except Exception:  # noqa: BLE001
        return ""
    text = str(text or "").strip()
    if len(text) > REPLY_CHARS:
        text = "… " + text[-REPLY_CHARS:]
    return text


async def run_loop(srv=None) -> None:
    """Deliver turn ends to their origin sessions, every :data:`LOOP_S`."""
    if srv is None:
        from backend.web import server as srv  # type: ignore[no-redef]
    from backend.web.core import sleep as _sleep

    try:
        while True:
            try:
                await _tick(srv)
            except Exception as err:  # noqa: BLE001 — the loop must never die
                _info("remote origin: turn-end pass failed (%s)", err)
            await asyncio.sleep(LOOP_S)
    finally:
        _sleep.release_lease(LEASE)
