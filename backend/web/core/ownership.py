"""Who owns a session, who is watching it, and who holds its keyboard
(docs/people.md "Owner, driver, viewers").

Three facts per session title, all web metadata (never the engine's
byte-exact Instance JSON):

* **owner** — the person id the session belongs to. It defaults to the
  ``person`` stamp :mod:`backend.web.core.person_sessions` records at create;
  only ``POST /api/instances/{title}/owner`` (the device owner,
  ``auth.may_configure``) changes it, persisted as ``<config dir>/
  session_owner.json`` (explicit overrides only).
* **driver** — the person id holding the keyboard, or ``""`` (nobody). The
  first person to attach a terminal to a session nobody drives takes it; an
  input from someone while nobody drives takes it too (a lock released for
  idleness must not leave its own holder read-only). It is released by its
  holder (``{"release": true}``), when their last terminal on the session
  closes, and after :data:`IDLE_RELEASE_S` without input from them. In
  memory only: every terminal closes when the server stops, so a restart
  starts with nobody driving.
* **viewers** — live presence: one entry per person with a terminal open on
  the session, ``{name, device, devices, since, typing}``. Kept per
  (title, person, device) underneath — the same person on two devices is
  one viewer with two devices.

A peer typing into a session I share with them
(:mod:`backend.peer.sessions_bridge`) holds a seat here too, as the person
``peer:<link id>`` named after the peer (``attach(..., name=)``): the same
one keyboard, and the peer's name on the owner's rail as driver or viewer.

The driver is a PERSON, not a device: all of one person's devices share an
id, so a person never locks themselves out from their own laptop. Nothing
here is access control — the bearer token and device pairing still gate
every request; this is attribution and a soft lock that keeps two people
from typing into one agent at once.

Every change emits ``session.presence_changed`` (data: the row fields
below); a change of driver also emits ``session.driver_changed`` (``old`` /
``new`` = person ids, data: ``{reason, name, previous_name}``) — the event
that tells the previous driver.
"""

from __future__ import annotations

import itertools
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from backend import config
from backend.web.core import title_store

#: The driver lock lapses after this long without input from its holder.
IDLE_RELEASE_S = 600.0
#: A viewer reads as ``typing`` this long after their last input.
TYPING_S = 5.0
#: The "<name> is driving" notice repeats at most this often per terminal.
NOTICE_EVERY_S = 3.0

#: Seconds since the epoch; tests swap it.
clock: Callable[[], float] = time.time

_LOCK = threading.RLock()
_OWNERS: Optional[Dict[str, str]] = None  # title -> person id (overrides)
#: title -> {"person", "device", "since", "last_input"}
_DRIVERS: Dict[str, dict] = {}
#: connection id -> {"title", "person", "device", "since", "notify", "said"}
_CONNS: Dict[int, dict] = {}
#: (title, person, device) -> last input epoch — the per-viewer presence.
_INPUT_AT: Dict[Tuple[str, str, str], float] = {}
#: person id -> the name a terminal attached with (a peer's), while it has one.
_NAMES: Dict[str, str] = {}
_IDS = itertools.count(1)

_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _valid(pid: object) -> str:
    from backend.config import settings as _settings

    return _settings.person_id(pid)


def _own_id() -> str:
    try:
        from backend.web.core import person as _person

        return _person.known_id()
    except Exception:  # noqa: BLE001
        return ""


def name_of(pid: str) -> str:
    """How MindFlock names ``pid``: this device's person's name when it is
    them, the name its terminal attached with (a peer, :func:`attach`), else
    ``""`` (another person's name never travels — the UI falls back to their
    device). Never raises."""
    if not pid:
        return ""
    with _LOCK:
        named = _NAMES.get(pid)
    if named:
        return named
    try:
        if pid == _own_id():
            from backend.config import settings as _settings

            return _settings.load_settings().person.name or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _label(pid: str, device: str = "") -> str:
    """A name for sentences: the person's name, else their device, else
    "Someone"."""
    return name_of(pid) or device or "Someone"


def caller(scope) -> Tuple[str, str]:
    """``(person id, device)`` a request or websocket comes from: the person
    :func:`backend.web.core.auth.request_person` names, and the device key in
    the ``X-MindFlock-Remote`` marker — this device's own key for a browser or
    CLI here. Never raises."""
    pid = ""
    try:
        from backend.web.core import auth as _auth

        pid = _auth.request_person(scope)
    except Exception:  # noqa: BLE001
        pass
    device = ""
    try:
        s = getattr(scope, "scope", scope)
        for k, v in s.get("headers") or []:
            if k == b"x-mindflock-remote":
                cand = v.decode("latin-1").strip()
                if _DEVICE_RE.match(cand):
                    device = cand.lower()
                break
        if not device:
            from backend.web.core import remote as _remote

            device = str(_remote.self_identity().get("key") or "")
    except Exception:  # noqa: BLE001
        pass
    return pid, device


def _emit(event: str, title: str, old=None, new=None, data=None) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(event, session=title, old=old, new=new, data=data or {})
    except Exception:  # noqa: BLE001 — presence never breaks a request
        pass


# --------------------------------------------------------------------------- #
# owner
# --------------------------------------------------------------------------- #
def _path() -> str:
    return os.path.join(config.GetConfigDir(), "session_owner.json")


def _owners() -> Dict[str, str]:
    global _OWNERS
    if _OWNERS is None:
        _OWNERS = title_store.load(
            _path, lambda d: {str(k): _valid(v) for k, v in d.items() if _valid(v)}
        )
    return _OWNERS


def _persist() -> None:
    title_store.persist(_path, _OWNERS, "session owners")


def owner(title: str) -> str:
    """The person ``title`` belongs to: an explicit owner, else the person it
    was created for, else ``""``. Never raises."""
    try:
        with _LOCK:
            got = _owners().get(str(title or ""))
        if got:
            return got
        from backend.web.core import person_sessions as _ps

        return _ps.person(title)
    except Exception:  # noqa: BLE001
        return ""


def set_owner(title: str, pid: str) -> str:
    """Make ``pid`` the owner of ``title``; ValueError when it is not a
    person id. Returns the owner."""
    pid = _valid(pid)
    if not pid:
        raise ValueError("person must be a person id")
    with _LOCK:
        owners = _owners()
        if owners.get(title) != pid:
            owners[title] = pid
            _persist()
    _presence_changed(title)
    return pid


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def _expire(title: str) -> Optional[dict]:
    """Drop ``title``'s driver when idle too long (caller holds the lock);
    the dropped record, so the caller can announce it after unlocking."""
    d = _DRIVERS.get(title)
    if d and clock() - d["last_input"] > IDLE_RELEASE_S:
        return _DRIVERS.pop(title)
    return None


def _announce_expired(title: str, gone: Optional[dict]) -> None:
    if gone:
        _driver_changed(title, gone["person"], "", "idle", gone.get("device", ""))


def _expire_all() -> None:
    with _LOCK:
        gone = [(t, _expire(t)) for t in list(_DRIVERS)]
    for t, g in gone:
        _announce_expired(t, g)


def driver(title: str) -> str:
    """The person holding ``title``'s keyboard, or ``""`` — after releasing a
    lock idle past :data:`IDLE_RELEASE_S`."""
    with _LOCK:
        gone = _expire(title)
        d = _DRIVERS.get(title)
        pid = d["person"] if d else ""
    _announce_expired(title, gone)
    return pid


def _driver_changed(
    title: str, old: str, new: str, reason: str, device: str = ""
) -> None:
    _emit(
        "session.driver_changed",
        title,
        old=old,
        new=new,
        data={
            "reason": reason,
            "name": _label(new, device) if new else "",
            "previous_name": name_of(old),
        },
    )
    _notify(title)
    _presence_changed(title)


def take(title: str, pid: str, device: str = "", reason: str = "take") -> str:
    """Give ``title``'s keyboard to ``pid``; the previous driver (``""`` for
    nobody). Taking from nobody or from yourself only refreshes the lock."""
    if not pid:
        return ""
    with _LOCK:
        gone = _expire(title)
        d = _DRIVERS.get(title)
        prev = d["person"] if d else ""
        now = clock()
        if d and prev == pid:
            d["last_input"] = now
            d["device"] = device or d.get("device", "")
        else:
            _DRIVERS[title] = {
                "person": pid,
                "device": device,
                "since": now,
                "last_input": now,
            }
    _announce_expired(title, gone)
    if prev != pid:
        _driver_changed(title, prev, pid, reason, device)
    return prev


def release(title: str, pid: Optional[str] = None, reason: str = "release") -> bool:
    """Release ``title``'s keyboard — only when ``pid`` holds it (``None``:
    whoever does). True when somebody was released."""
    with _LOCK:
        d = _DRIVERS.get(title)
        if not d or (pid is not None and d["person"] != pid):
            return False
        _DRIVERS.pop(title, None)
    _driver_changed(title, d["person"], "", reason)
    return True


def busy(title: str, pid: str) -> Optional[dict]:
    """``None`` when ``pid`` may type into ``title`` (they drive, or nobody
    does); else the 409 body a typed-into-terminal route answers:
    ``{"error": "driven by <name>", "driver": {"person", "name", "device"}}``.
    Does not take anything."""
    cur = driver(title)
    if not cur or cur == pid:
        return None
    with _LOCK:
        d = dict(_DRIVERS.get(title) or {})
    name = _label(cur, d.get("device", ""))
    return {
        "error": "driven by %s" % name,
        "driver": {"person": cur, "name": name_of(cur), "device": d.get("device", "")},
    }


def gate(title: str, pid: str, device: str = "", want: bool = False) -> Optional[dict]:
    """The rule for ``/send``, the prompt queue and ``/answer``: ``None`` when
    ``pid`` may type (refreshing their lock when they drive), else the 409
    body of :func:`busy`. ``want`` (the request's ``"take": true``) takes the
    keyboard first."""
    if want and pid:
        take(title, pid, device)
        return None
    body = busy(title, pid)
    if body is None and pid and driver(title) == pid:
        with _LOCK:
            d = _DRIVERS.get(title)
            if d:
                d["last_input"] = clock()
    return body


# --------------------------------------------------------------------------- #
# terminals and presence
# --------------------------------------------------------------------------- #
def attach(
    title: str,
    pid: str,
    device: str,
    notify: Optional[Callable[[dict], None]] = None,
    name: str = "",
) -> int:
    """Register a terminal websocket on ``title``; its connection id. The
    first person to attach to a session nobody drives takes the keyboard.
    ``notify`` (optional, any thread) receives :func:`mode` frames when the
    driver changes from now on — the caller sends the first one itself.
    ``name``: what to call ``pid`` while it has a terminal open (a peer —
    :func:`name_of` knows nobody else's name)."""
    with _LOCK:
        cid = next(_IDS)
        if pid and name:
            _NAMES[pid] = name
        _CONNS[cid] = {
            "title": title,
            "person": pid,
            "device": device,
            "since": clock(),
            "notify": None,
            "said": 0.0,
        }
    if pid and not driver(title):
        take(title, pid, device, reason="attach")
    else:
        _presence_changed(title)
    with _LOCK:
        if cid in _CONNS:
            _CONNS[cid]["notify"] = notify
    return cid


def detach(cid: int) -> None:
    """Forget a closed terminal; the last one of the driver's on the session
    releases the keyboard."""
    with _LOCK:
        c = _CONNS.pop(cid, None)
        if c is None:
            return
        title, pid = c["title"], c["person"]
        still = any(o["title"] == title and o["person"] == pid for o in _CONNS.values())
    if still or not pid or not release(title, pid, reason="left"):
        _presence_changed(title)  # (a release announced it already)
    with _LOCK:
        if not any(o["person"] == pid for o in _CONNS.values()):
            _NAMES.pop(pid, None)


def may_type(cid: int) -> Tuple[bool, str]:
    """Whether the terminal ``cid`` may type now: its person drives, or
    nobody does (then they take it). ``(False, notice)`` otherwise, where
    ``notice`` is "<name> is driving — take control to type" at most once per
    :data:`NOTICE_EVERY_S` (``""`` in between)."""
    with _LOCK:
        c = _CONNS.get(cid)
        if c is None:
            return True, ""
        title, pid, device = c["title"], c["person"], c["device"]
    cur = driver(title)
    if not pid or cur == pid:
        return True, ""
    if not cur:
        take(title, pid, device, reason="input")
        return True, ""
    with _LOCK:
        d = _DRIVERS.get(title) or {}
        now = clock()
        if now - c["said"] < NOTICE_EVERY_S:
            return False, ""
        c["said"] = now
    return False, "%s is driving — take control to type" % _label(
        cur, d.get("device", "")
    )


def note_input(title: str, pid: str, device: str) -> None:
    """A person put input into ``title`` from ``device`` (presence per
    (title, person, device)); refreshes their driver lock. Announces
    presence when they START typing (not on every keystroke)."""
    if not pid:
        return
    with _LOCK:
        now = clock()
        key = (title, pid, device)
        started = now - _INPUT_AT.get(key, 0.0) > TYPING_S
        _INPUT_AT[key] = now
        d = _DRIVERS.get(title)
        if d and d["person"] == pid:
            d["last_input"] = now
    if started:
        _presence_changed(title)


def mode(title: str, pid: str) -> dict:
    """The ``{"type": "driver", ...}`` frame a terminal is told on attach and
    on every change of driver: ``driving`` (this terminal's person holds the
    keyboard), ``driver`` (person id or ``""``), ``driver_name``."""
    cur = driver(title)
    with _LOCK:
        d = dict(_DRIVERS.get(title) or {})
    return {
        "type": "driver",
        "driving": bool(pid) and cur == pid,
        "driver": cur,
        "driver_name": _label(cur, d.get("device", "")) if cur else "",
    }


def _notify(title: str) -> None:
    with _LOCK:
        targets = [
            (c["person"], c["notify"])
            for c in _CONNS.values()
            if c["title"] == title and c["notify"] is not None
        ]
    for pid, cb in targets:
        try:
            cb(mode(title, pid))
        except Exception:  # noqa: BLE001
            pass


def viewers(title: str) -> Dict[str, dict]:
    """``{person id: {name, device, devices, since, typing}}`` — one entry per
    person with a terminal open on ``title``. ``device`` is the one they used
    last (else the first they opened), ``since`` their earliest attach."""
    out: Dict[str, dict] = {}
    with _LOCK:
        now = clock()
        conns = [c for c in _CONNS.values() if c["title"] == title and c["person"]]
        conns.sort(key=lambda c: c["since"])
        for c in conns:
            pid, dev = c["person"], c["device"]
            at = _INPUT_AT.get((title, pid, dev), 0.0)
            v = out.get(pid)
            if v is None:
                v = out[pid] = {
                    "name": "",
                    "device": dev,
                    "devices": [],
                    "since": c["since"],
                    "typing": False,
                    "_at": at,
                }
            if dev not in v["devices"]:
                v["devices"].append(dev)
            if at > v["_at"]:
                v["_at"], v["device"] = at, dev
            if now - at <= TYPING_S:
                v["typing"] = True
    for pid, v in out.items():
        v.pop("_at", None)
        v["name"] = name_of(pid)
    return out


def terminals(title: str) -> int:
    """How many terminal websockets are attached to ``title`` right now, by
    anyone (a connection with no person counts too) — what the sleep sweep
    (:mod:`backend.web.core.sleep`) reads as "someone has it open"."""
    with _LOCK:
        return sum(1 for c in _CONNS.values() if c["title"] == title)


def row_fields(title: str) -> dict:
    """``{owner, driver, driver_name, viewers}`` for an ``/api/instances``
    row (and the ``session.presence_changed`` event's data)."""
    cur = driver(title)
    with _LOCK:
        d = dict(_DRIVERS.get(title) or {})
    return {
        "owner": owner(title),
        "driver": cur,
        "driver_name": _label(cur, d.get("device", "")) if cur else "",
        "viewers": viewers(title),
    }


def overlay(rows: List[dict]) -> None:
    """Refresh the presence fields of this device's own rows, in place — the
    listing serves a cached snapshot, and presence moves faster than it.
    Rows another device sent (``device`` set) keep theirs, unchanged."""
    _expire_all()
    for r in rows:
        if isinstance(r, dict) and r.get("title") and not r.get("device"):
            try:
                r.update(row_fields(str(r["title"])))
            except Exception:  # noqa: BLE001
                pass


def _presence_changed(title: str) -> None:
    _emit("session.presence_changed", title, data=row_fields(title))


def drop(title: str) -> None:
    """Forget a deleted session (owner override, driver, presence stamps)."""
    with _LOCK:
        owners = _owners()
        if title in owners:
            owners.pop(title, None)
            _persist()
        _DRIVERS.pop(title, None)
        for key in [k for k in _INPUT_AT if k[0] == title]:
            _INPUT_AT.pop(key, None)


def rename(old: str, new: str) -> int:
    """After a person-id switch (pairing adoption, the sync tie-break): every
    owner override, driver lock, live terminal and presence stamp that named
    ``old`` now names ``new`` — otherwise the person who just switched is
    "someone else" to their own session and cannot type until the lock lapses.
    Returns how many records moved; never raises."""
    old, new = _valid(old), _valid(new)
    if not old or not new or old == new:
        return 0
    moved = 0
    touched = set()
    try:
        with _LOCK:
            owners = _owners()
            for title, pid in list(owners.items()):
                if pid == old:
                    owners[title] = new
                    moved += 1
            if moved:
                _persist()
            for title, d in _DRIVERS.items():
                if d.get("person") == old:
                    d["person"] = new
                    moved += 1
                    touched.add(title)
            for c in _CONNS.values():
                if c.get("person") == old:
                    c["person"] = new
                    moved += 1
                    touched.add(c.get("title", ""))
            for key in [k for k in _INPUT_AT if k[1] == old]:
                _INPUT_AT[(key[0], new, key[2])] = _INPUT_AT.pop(key)
                moved += 1
                touched.add(key[0])
        for title in touched:
            if title:
                _presence_changed(title)
    except Exception:  # noqa: BLE001 — bookkeeping only
        pass
    return moved


def _reset_for_tests() -> None:
    global _OWNERS
    with _LOCK:
        _OWNERS = None
        _DRIVERS.clear()
        _CONNS.clear()
        _INPUT_AT.clear()
        _NAMES.clear()
