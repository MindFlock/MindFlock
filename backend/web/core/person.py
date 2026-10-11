"""The PERSON behind a MindFlock server — one per person, shared by all of
their devices (docs/people.md).

Auth stays what it was: a bearer token per server, plus the fleet key between
the person's own devices. The person is an IDENTIFIER layered on top — who a
session, a claim, an event or a commit is for — never a credential: nothing
is ever allowed because a request names a person.

**The record** is settings ``person`` (:class:`backend.config.settings.
PersonSettings`): ``{"id", "name", "git_name", "git_email"}``. ``id`` is a
uuid4 made once, the first time anything asks (:func:`person_id`; the server
also asks at startup, so an existing store gets its id on its first load).

**One id across the devices.** Ids are agreed, never edited:

* pairing — a device that joins another's group (a code, Ask/Approve, or the
  one-click add of a token-paired device) adopts the INVITER's id from the
  pairing payload (:func:`handshake` / :func:`adopt`): the device that owns
  the fleet key others paired to wins;
* settings sync — two devices already in one group with different ids (both
  paired before people existed) settle on one with :func:`winner`: the
  automation device's id, else the lexicographically smaller one. Both sides
  compute the same answer from the same facts, so they converge in one pass
  without talking about it (:func:`reconcile`); the switch is logged once.

``name``, ``git_name`` and ``git_email`` sync like any shared setting.

**Git identity.** :func:`git_identity_args` is what every commit MindFlock
makes on the person's behalf (fast-track/ship, team-run integration, the
peer-link export) puts in front of the subcommand: ``-c user.name=… -c
user.email=…`` when both are set, nothing otherwise (the repo's own git
config decides, exactly as before). An agent's own commits run in its shell
and are never touched.
"""

from __future__ import annotations

import threading
import uuid
from typing import Dict, List, Optional

from backend import log

_LOCK = threading.Lock()
#: (old id, new id) pairs already logged — "logged once" per switch.
_LOGGED: set = set()

#: The request header a proxied request carries the person id in
#: (:mod:`backend.web.core.remote`), beside the device key.
HEADER = "X-MindFlock-Person"


def _settings():
    from backend.config import settings as _s

    return _s


def _info(fmt: str, *args) -> None:
    if log.InfoLog is not None:
        log.InfoLog.Printf(fmt, *args)


def valid_id(value: object) -> str:
    """``value`` as a well-formed person id (lower-case), or ``""``."""
    return _settings().person_id(value)


def ensure_id() -> str:
    """This device's person id, made (uuid4) and saved the first time.

    ``""`` — and nothing saved — while settings.json exists but can't be
    read: saving would replace it with defaults. Never raises."""
    s = _settings()
    try:
        pid = s.load_settings().person.id
    except Exception:  # noqa: BLE001
        pid = ""
    if pid:
        return pid
    with _LOCK:
        try:
            current = s.load_settings_strict()
        except Exception:  # noqa: BLE001 — unreadable: never save over it
            return ""
        if current.person.id:
            return current.person.id
        pid = str(uuid.uuid4())
        try:
            s.update_settings(person={"id": pid})
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("person: couldn't save a new id: %v", err)
            return ""
        _info("person: this device's person id is %s", pid)
        return pid


def person_id() -> str:
    """This device's person id, made if there is none yet (:func:`ensure_id`)
    — for the places that hand it to someone (a pairing, a session stamp)."""
    return ensure_id()


def known_id() -> str:
    """This device's person id as stored — ``""`` when none was made yet.
    Never writes: for the passive readers (headers, claims, events, the sync
    export), which must not save settings.json as a side effect. The server
    makes the id at startup, so in practice it is always there."""
    try:
        return _settings().load_settings().person.id
    except Exception:  # noqa: BLE001
        return ""


def current() -> Dict[str, str]:
    """``{"id", "name", "git_name", "git_email"}`` — the whole record."""
    pid = ensure_id()
    try:
        p = _settings().load_settings().person
    except Exception:  # noqa: BLE001
        return {"id": pid, "name": "", "git_name": "", "git_email": ""}
    return {
        "id": pid or p.id,
        "name": p.name,
        "git_name": p.git_name,
        "git_email": p.git_email,
    }


def public() -> Dict[str, str]:
    """``{"id", "name"}`` — what an agent (whoami) or a row is told."""
    p = current()
    return {"id": p["id"], "name": p["name"]}


def update(name=None, git_name=None, git_email=None) -> Dict[str, str]:
    """Change the editable fields (``None`` = leave alone, ``""`` = clear).

    ValueError on a value the store would silently drop (a git email that
    isn't one, say) — so the person hears about it rather than seeing their
    edit vanish. The id is never set here."""
    s = _settings()
    patch: Dict[str, str] = {}
    for key, value, clean in (
        ("name", name, lambda v: s._person_text(v, 80)),
        ("git_name", git_name, s._git_name),
        ("git_email", git_email, s._git_email),
    ):
        if value is None:
            continue
        raw = str(value).strip()
        got = clean(raw)
        if raw and not got:
            raise ValueError("%s isn't valid: %r" % (key.replace("_", " "), raw))
        patch[key] = got
    ensure_id()
    if patch:
        s.update_settings(person=patch)
    return current()


def git_identity_args() -> List[str]:
    """``["-c", "user.name=…", "-c", "user.email=…"]`` when the person's git
    name AND email are set, else ``[]`` (the repo's git config decides).

    Goes between ``git`` and its subcommand, for commits MindFlock makes on
    the person's behalf — never an agent's own. Never raises."""
    try:
        p = _settings().load_settings().person
    except Exception:  # noqa: BLE001
        return []
    if not p.git_name or not p.git_email:
        return []
    return ["-c", "user.name=" + p.git_name, "-c", "user.email=" + p.git_email]


def git_author() -> Optional[tuple]:
    """``(name, email)`` when configured, else None."""
    args = git_identity_args()
    if not args:
        return None
    return args[1].split("=", 1)[1], args[3].split("=", 1)[1]


# --------------------------------------------------------------------------- #
# agreeing on one id
# --------------------------------------------------------------------------- #
def handshake() -> Dict[str, str]:
    """What the inviter puts in a pairing payload (``"person"``)."""
    return current()


def _set_id(new: str, why: str) -> bool:
    s = _settings()
    with _LOCK:
        try:
            old = s.load_settings_strict().person.id
        except Exception:  # noqa: BLE001
            return False
        if old == new:
            return False
        try:
            s.update_settings(person={"id": new})
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("person: couldn't adopt %s: %v", new, err)
            return False
    if (old, new) not in _LOGGED:
        _LOGGED.add((old, new))
        _info("person: id %s -> %s (%s)", old or "(none)", new, why)
    # The sessions stamped with the old id are this person's too.
    try:
        from backend.web.core import person_sessions as _ps

        _ps.rename(old, new)
        from backend.web.core import ownership as _own

        _own.rename(old, new)
    except Exception:  # noqa: BLE001 — bookkeeping only
        pass
    return True


def adopt(payload: object, source: str = "") -> bool:
    """The joining side of a pairing: take the inviter's id (and its name /
    git identity where this device has none). False when the payload carries
    no usable id (an inviter that predates people) — nothing changes.
    Never raises."""
    if not isinstance(payload, dict):
        return False
    pid = valid_id(payload.get("id"))
    if not pid:
        return False
    try:
        changed = _set_id(pid, "paired with %s" % (source or "another device"))
        mine = current()
        s = _settings()
        fill = {}
        for key, clean in (
            ("name", lambda v: s._person_text(v, 80)),
            ("git_name", s._git_name),
            ("git_email", s._git_email),
        ):
            theirs = clean(payload.get(key))
            if theirs and not mine.get(key):
                fill[key] = theirs
        if fill:
            s.update_settings(person=fill)
            changed = True
        return changed
    except Exception as err:  # noqa: BLE001 — a pairing must not fail on this
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("person: couldn't adopt the inviter's: %v", err)
        return False


def winner(
    mine: str,
    theirs: str,
    *,
    self_device: str,
    their_device: str,
    automation_device: str = "",
) -> str:
    """The id two devices of one group settle on — the same answer computed
    from either side: the automation device's id (when it is one of the two),
    else the lexicographically smaller one. A missing id loses to a present
    one."""
    mine, theirs = valid_id(mine), valid_id(theirs)
    if not mine or not theirs:
        return mine or theirs
    if mine == theirs:
        return mine
    auto = str(automation_device or "")
    if auto and auto == self_device and auto != their_device:
        return mine
    if auto and auto == their_device and auto != self_device:
        return theirs
    return min(mine, theirs)


def reconcile(
    theirs: object, their_device: str, self_device: str, automation_device: str = ""
) -> bool:
    """Settle this device's id against another group member's (a settings-sync
    export's ``person``). True when this device switched. Never raises."""
    try:
        theirs_id = valid_id(theirs.get("id") if isinstance(theirs, dict) else theirs)
        if not theirs_id:
            return False
        mine = known_id()
        if mine == theirs_id:
            return False
        if not mine:  # none made here yet: the group's is ours
            return _set_id(theirs_id, "took %s's over settings sync" % their_device)
        win = winner(
            mine,
            theirs_id,
            self_device=self_device,
            their_device=their_device,
            automation_device=automation_device,
        )
        if win == mine:
            key = (theirs_id, mine)
            if key not in _LOGGED:
                _LOGGED.add(key)
                _info(
                    "person: %s has person id %s; keeping %s (it adopts this one)",
                    their_device,
                    theirs_id,
                    mine,
                )
            return False
        return _set_id(win, "settled with %s over settings sync" % their_device)
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("person: couldn't reconcile: %v", err)
        return False
