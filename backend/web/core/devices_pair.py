"""Add a NEW device without Tailscale: a long code through the device tunnel.

``docs/devices-anywhere.md`` §7. Two halves:

* **The inviter** (Settings → Your devices → Add a device → "It isn't on my
  network"): needs "Reach my devices from anywhere" on here (the flow offers
  to turn it on) and its tunnel up, then mints a single-use ``mfd1:`` code
  (:mod:`backend.peer.fleet_pair`) naming a fresh PAIRING path on the
  tunnel (not the members' one — a new code, a cancel or the expiry retire
  it), this device's key fingerprint, and shows the code with its safety
  number. The tunnel server answers that code (:func:`serve`): the joiner
  proves it; the joiner is recorded here as PENDING (:func:`_begin`,
  durable, on no roster) before the bundle leaves; it adopts it and
  confirms with the group key — only then is it put on this device's roster
  (:func:`_commit`). Every other ending after the bundle went out raises a
  loud alarm (:func:`_unfinished`): whoever read the bundle holds the
  group's key, so the person is told and offered **Change key**
  (:func:`change_key`). A code dies on first use, after 10 minutes, after 5
  wrong proofs, and when the tunnel restarts.

* **The joiner** (the same screen's paste box, :func:`join`): before
  anything is sent the person confirms — the computer that made the code
  will be able to run agents here and its settings replace this one's — and
  compares the safety number with the one next to the code on their own
  computer (:func:`preview`; :class:`NeedsConfirm` until they did). Then it
  pins the fingerprint from the code, checks the inviter's proof, adopts the
  bundle (leaving another group first only when the person confirmed that
  too), and — when ``added`` never came — asks the inviter's members' path
  whether it is on its roster (:func:`_check_joined`): kept if so, undone
  if not. Only when the person left "Let my devices reach this computer
  from anywhere" ticked does it turn its own switch and remote control on,
  use the inviter's members' address (``devices_anywhere.seed``: it can't
  read GitHub yet), and finish the join over the tunnel like any other
  (``fleet.after_adopt``: announce, settings sync from the inviter — which
  normally brings the GitHub sign-in, so this device can publish its own
  address).

Nothing here logs a code, a secret, a bundle or an address.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Optional

from backend.peer import fleet_pair
from backend.web.core import devices_anywhere as _anywhere

log = logging.getLogger(__name__)

#: How long the joiner keeps looking for the inviter through the tunnel
#: after the pairing, and how often.
FINISH_WAIT = 120.0
FINISH_STEP = 3.0
#: After a lost ``added``: how many times (and how far apart) the joiner asks
#: the inviter's members' path whether it is on the roster.
CHECK_TRIES = 3
CHECK_STEP = 2.0
#: A pending joiner older than this was cut off mid-pairing (a crash or a
#: restart of this server): it becomes an alarm.
STALE_PENDING_S = fleet_pair.PAIR_TIMEOUT + 60.0
#: At most one "denied" log line per this many seconds (L4).
DENIED_LOG_EVERY = 60.0
#: Alarms kept (newest last).
MAX_ALERTS = 20

GONE_RESTARTED = (
    "the private tunnel restarted, so that code stopped working — make a new one"
)

NOTE_NO_GITHUB = (
    "{host} can't reach this computer yet: GitHub isn't signed in here, so this "
    "computer can't leave its address for your other devices. Connect GitHub "
    "(Settings → GitHub) with the same account as on {host}. This computer can "
    "already reach {host}."
)
NOTE_SWITCH_OFF = (
    "Your devices can't reach each other yet: “Reach my devices from anywhere” "
    "is off on this computer, and so is remote control — nothing was turned on "
    "that you didn't tick. Turn the switch on (above) when you want them to "
    "reach each other, or use Tailscale on both."
)
NOTE_NOT_REACHED = (
    "{host} didn't answer through its private tunnel yet — MindFlock keeps "
    "trying. If it stays like this, check MindFlock is running on {host}, then "
    "turn on Settings sync here."
)

#: What the new computer says before it joins (M2). The UI and the CLI show
#: it with the safety number; nothing is sent until the person confirms.
CONFIRM_TEXT = (
    "The computer that made this code will be able to run agents on this "
    "computer, and its settings will replace this computer's. Only continue if "
    "the code came from your own computer."
)
SAFETY_TEXT = (
    "Check that your other computer shows the same number next to the code: {safety}"
)
LEAVE_TEXT = (
    "This computer is already one of {count} devices ({hosts}). Joining with a "
    "code from a different group makes it leave them."
)

#: The inviter's alarm when a code was used and the join didn't finish (M1).
ALERT_TEXT = (
    "A pairing code was used but the join didn't finish ({why}). If that wasn't "
    "you, change your devices' key now."
)
ALERT_REUSED_TEXT = (
    "A pairing code was used by two computers. One of them wasn't yours: check "
    "Your devices, remove any device you don't know, and change your devices' "
    "key now."
)
_WHY = {
    "aborted": "the other computer stopped after it got the key",
    "unconfirmed": "the other computer got the key but never confirmed",
    "error": "this computer couldn't finish adding it",
    "interrupted": "MindFlock stopped in the middle of it",
}

_ERRORS = {
    "pin": (
        "The computer that answered isn't the one that made the code (the "
        "connection may have been intercepted) — nothing was sent. Make a new "
        "code and try again."
    ),
    "denied": (
        "That code didn't work — it may be mistyped, used already, or older than "
        "10 minutes. Make a new code on the other computer."
    ),
    "used": (
        "This code was already used by another computer. If that wasn't one of "
        "yours, tell the other computer: it shows a warning under Your devices "
        "with Change key. Then make a new code."
    ),
    "same_name": (
        "One of your devices already has this computer's name. If it's this "
        "computer, it has joined already; otherwise rename this computer, then "
        "make a new code."
    ),
    "proof": "The other computer didn't answer like MindFlock — make a new code and try again.",
    "protocol": "The other computer didn't answer like MindFlock — make a new code and try again.",
    "unconfirmed": (
        "The other computer didn't confirm the join, so nothing changed here — "
        "make a new code and try again."
    ),
}
UNREACHABLE = (
    "Couldn't reach the other computer through its private tunnel — check "
    "MindFlock is running there and the code is new, then try again."
)


class NeedsSwitch(RuntimeError):
    """ "Reach my devices from anywhere" is off here (the flow offers it)."""


class NotUp(RuntimeError):
    """The tunnel didn't come up (the message is the status line)."""


class NeedsConfirm(ValueError):
    """The person hasn't confirmed this join yet (M2): ``info`` is what to
    show them (:func:`preview`). Nothing was sent anywhere."""

    def __init__(self, info: dict):
        super().__init__(info.get("text") or CONFIRM_TEXT)
        self.info = info


# --------------------------------------------------------------------------- #
# The inviter
# --------------------------------------------------------------------------- #
_BOOK: list = [None]
_GONE = {"reason": ""}
_DENIED_LOG = {"at": 0.0, "n": 0}


def _book() -> fleet_pair.PairBook:
    if _BOOK[0] is None:
        _BOOK[0] = fleet_pair.PairBook()
    return _BOOK[0]


def _reset_for_tests() -> None:
    _BOOK[0] = None
    _GONE["reason"] = ""
    _DENIED_LOG.update(at=0.0, n=0)


def _fleet():
    from backend.web.core import fleet as _f

    return _f


def _row(inv: dict) -> dict:
    from backend.web.core.mobile_access import qr_svg

    f = _fleet()
    return {
        "code": inv["code"],
        "expires_at": inv["expires_at"],
        "host": f._self_host(),
        # The number the new computer shows before it joins: the person
        # compares the two (a code made by anyone else shows another one).
        "safety": inv.get("safety") or "",
        "qr_svg": qr_svg(inv["code"].upper()),
    }


async def create_invite(turn_on: bool = False) -> dict:
    """A fresh code (older ones are cancelled, their pairing path retired).
    ``turn_on``: switch "Reach my devices from anywhere" on here first (the
    person agreed in the flow). Raises :class:`NeedsSwitch` while it is off
    and ``turn_on`` is False, :class:`NotUp` when the tunnel doesn't come
    up."""
    if not _anywhere.enabled():
        if not turn_on:
            raise NeedsSwitch("turn on Reach my devices from anywhere first")
        _anywhere.save_switch(True)
    f = _fleet()
    if not f.in_fleet():
        f.create()
    if not await _anywhere.ensure_up():
        st = _anywhere.status()
        raise NotUp(st.get("text") or "the private tunnel didn't start")
    tunnel, fp = _anywhere.current_url(), _anywhere.fingerprint()
    if not tunnel or not fp:
        raise NotUp("the private tunnel didn't start")
    url = _anywhere.open_pair_path(fleet_pair.TTL_S)
    if not url:
        raise NotUp("the private tunnel didn't start")
    inv = _book().create(
        url, bytes.fromhex(fp), loopback=_anywhere._loopback(), tunnel=tunnel
    )
    _GONE["reason"] = ""
    return _row(inv)


def invite_status() -> dict:
    """``{invite: row | None, gone: "<why the last code stopped>"}`` for
    this device's own screen."""
    live = _book().active()
    url = _anywhere.current_url()
    for inv in live:
        if inv["tunnel"] != url:
            _book().revoke(inv["invite_id"])
            _GONE["reason"] = GONE_RESTARTED
    live = [i for i in live if i["tunnel"] == url]
    return {"invite": _row(live[-1]) if live else None, "gone": _GONE["reason"]}


def cancel_invite() -> None:
    _book().revoke()
    _anywhere.close_pair_path()
    _GONE["reason"] = ""


def tunnel_changed(url: str) -> None:
    """The tunnel (re)started at ``url``: a code made on another tunnel
    can't be used any more (the new ingress has no pairing path anyway)."""
    if _BOOK[0] is None:
        return
    for inv in _book().active():
        if inv["tunnel"] != url:
            _book().revoke(inv["invite_id"])
            _GONE["reason"] = GONE_RESTARTED


# -- the pairing record: pending joiners and alarms (fleet_pairing.json) ------
def _store_path() -> Path:
    from backend.config import settings as _settings

    return _settings.settings_path().parent / "fleet_pairing.json"


def _read_store() -> dict:
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    pending = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
    alerts = raw.get("alerts") if isinstance(raw.get("alerts"), list) else []
    f = _fleet()
    clean_p = {}
    for k, v in pending.items():
        if isinstance(k, str) and f.DEVICE_RE.match(k) and isinstance(v, dict):
            try:
                at = float(v.get("at") or 0)
                added_at = float(v.get("added_at") or 0)
            except (TypeError, ValueError):
                continue
            clean_p[k] = {
                "host": f.clean_host(v.get("host"), k),
                "dns": f.clean_dns(v.get("dns"), k),
                "runs": bool(v.get("runs")),
                "at": at,
                "added_at": added_at,
            }
    clean_a = []
    for a in alerts:
        if not (isinstance(a, dict) and a.get("id")):
            continue
        try:
            at = float(a.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        clean_a.append(
            {
                "id": str(a.get("id") or "")[:32],
                "device": str(a.get("device") or "")[:64],
                "host": f.clean_host(a.get("host"), str(a.get("device") or "")),
                "outcome": str(a.get("outcome") or "")[:32],
                "at": at,
                "text": str(a.get("text") or "")[:500],
            }
        )
    return {"pending": clean_p, "alerts": clean_a[-MAX_ALERTS:]}


def _write_store(doc: dict) -> None:
    from backend.fsutil import atomic_write_json

    atomic_write_json(
        _store_path(),
        doc,
        indent=1,
        sort_keys=True,
        newline=False,
        ensure_ascii=True,
        mode=0o600,
        prefix=".fleet_pairing.",
    )


def _alert(doc: dict, device: str, host: str, outcome: str) -> dict:
    text = (
        ALERT_REUSED_TEXT
        if outcome == "used"
        else ALERT_TEXT.format(why=_WHY.get(outcome) or outcome)
    )
    row = {
        "id": secrets.token_hex(6),
        "device": device,
        "host": host or device,
        "outcome": outcome,
        "at": time.time(),
        "text": text,
    }
    doc["alerts"] = (doc["alerts"] + [row])[-MAX_ALERTS:]
    return row


def _raise_alarm(row: dict) -> None:
    """The loud part: an event (the bell, a toast, a phone push) and a log
    line. The durable part is the stored row Your devices shows."""
    f = _fleet()
    log.warning(
        "devices pair: a code was used without the join finishing (%s)", row["outcome"]
    )
    f._emit(
        "device.pair_unfinished",
        device=row["device"],
        host=row["host"],
        outcome=row["outcome"],
        id=row["id"],
        detail=row["text"],
    )


def _begin(device: str, host: str, dns: str, runs: bool, bundle: dict) -> None:
    """Record ``device`` as PENDING before its bundle leaves (M1). Refuses
    (:class:`fleet_pair.PairRefused`) a name that is a live member now.
    Pending is not membership: it is on no roster and passes no handshake."""
    f = _fleet()
    with f._LOCK:
        if device in f.live_members():
            raise fleet_pair.PairRefused("name taken")
        doc = _read_store()
        try:
            added_at = float(bundle["members"][device]["added_at"])
        except (KeyError, TypeError, ValueError):
            added_at = time.time()
        doc["pending"][device] = {
            "host": f.clean_host(host, device),
            "dns": f.clean_dns(dns, device),
            "runs": bool(runs),
            "at": time.time(),
            "added_at": added_at,
        }
        _write_store(doc)


def _unfinished(device: str, host: str, outcome: str) -> None:
    """The bundle went out and the join didn't complete: the pending entry
    goes, an alarm is stored and raised."""
    f = _fleet()
    with f._LOCK:
        doc = _read_store()
        doc["pending"].pop(device, None)
        row = _alert(doc, device, host, outcome)
        _write_store(doc)
    _raise_alarm(row)


def _reused(device: str, host: str) -> None:
    """A right proof for a code that was already used: two computers had it."""
    f = _fleet()
    with f._LOCK:
        doc = _read_store()
        row = _alert(doc, device, host, "used")
        _write_store(doc)
    _raise_alarm(row)


def _sweep_stale(now: Optional[float] = None) -> None:
    """Pending joiners older than :data:`STALE_PENDING_S` (this server
    stopped mid-pairing — the bundle may have gone out) become alarms."""
    f = _fleet()
    now = time.time() if now is None else now
    raised = []
    with f._LOCK:
        doc = _read_store()
        stale = [
            k for k, v in doc["pending"].items() if now - v["at"] > STALE_PENDING_S
        ]
        if not stale:
            return
        for k in stale:
            p = doc["pending"].pop(k)
            raised.append(_alert(doc, k, p["host"], "interrupted"))
        _write_store(doc)
    for row in raised:
        _raise_alarm(row)


def pairing_status() -> dict:
    """``{pending: [{device, host, at}], alerts: [{id, device, host,
    outcome, at, text}]}`` for Settings → Your devices."""
    _sweep_stale()
    doc = _read_store()
    return {
        "pending": [
            {"device": k, "host": v["host"], "at": v["at"]}
            for k, v in sorted(doc["pending"].items())
        ],
        "alerts": list(doc["alerts"]),
    }


def dismiss_alerts(alert_id: str = "") -> int:
    """ "It was me": drop one alarm (or all) → how many went."""
    f = _fleet()
    with f._LOCK:
        doc = _read_store()
        before = len(doc["alerts"])
        doc["alerts"] = [a for a in doc["alerts"] if alert_id and a["id"] != alert_id]
        _write_store(doc)
        return before - len(doc["alerts"])


async def change_key() -> dict:
    """The alarm's **Change key**: replace the devices' key on every member
    (and every member's own access token — :func:`fleet.rotate_key`) and
    this device's own token, as removing a device does: whoever took the
    key with a code is locked out. Clears the alarms. → the rotation result
    (``rekeyed``, ``missed``: offline members take the new key when next
    seen)."""
    f = _fleet()
    out = await f.rotate_key()
    try:
        from backend.web.core import auth as _auth

        _auth.rotate_token()
        out["rotated"] = sorted(set(out.get("rotated") or []) | {f._self_key()})
    except Exception as err:  # noqa: BLE001 — env-pinned token / store failure
        out.setdefault("rotate_failed", []).append(f._self_key())
        log.warning(
            "devices pair: couldn't replace this token (%s)", type(err).__name__
        )
    dismiss_alerts()
    return out


async def _commit(device: str, host: str, dns: str, runs: bool) -> None:
    """The joiner adopted the bundle (its confirm proved the group key): put
    it on this roster — refused if that name became a live member meanwhile
    (L1, checked under the lock) — record the admit (L2: only now), then
    make the membership real here (remote control on, settings sync seeded,
    automation settled)."""
    f = _fleet()
    with f._LOCK:
        doc = _read_store()
        pend = doc["pending"].get(device) or {}
        if device in f.live_members():
            raise f.NameTaken(device)
        if pend.get("added_at"):
            # Before the add: only a device still removed here is noted.
            f._note_admit(device, pend["added_at"])
        f.add_new_member(device, host, by=f._self_key(), dns=f.clean_dns(dns, device))
        doc["pending"].pop(device, None)
        _write_store(doc)
    await f.after_admit(runs, device, via="anywhere")
    with f._LOCK:
        rec = f._ADMITTED.get(device)
        if rec is not None:
            rec["host"] = host or device
    _anywhere.want_read()  # look for its address soon
    f._emit(
        "device.joined",
        device=device,
        host=host,
        detail="%s joined your devices" % (host or device),
    )


def _make_bundle(device: str, host: str, dns: str) -> dict:
    f = _fleet()
    if not f.in_fleet():
        raise ValueError("not in a group")
    if device in f.live_members():
        # A new device only: a live member's entry must not be rewritten by
        # whoever holds a code (the tunnel can't bind a name to an address
        # the way the tailnet does for the 8-character code).
        raise fleet_pair.PairRefused("name taken")
    # The admit (tombstone bypass) is recorded at commit, not here (L2).
    return f.bundle_for(device, host, f.clean_dns(dns, device), note_admit=False)


def _log_outcome(outcome: str) -> None:
    """One word per pairing — but "denied" (anyone who has an old code can
    make those) at most once a minute, with how many (L4)."""
    if outcome != "denied":
        log.info("devices pair: %s", outcome)
        return
    now = time.monotonic()
    _DENIED_LOG["n"] += 1
    if now - _DENIED_LOG["at"] >= DENIED_LOG_EVERY:
        log.info("devices pair: denied (%d since the last line)", _DENIED_LOG["n"])
        _DENIED_LOG.update(at=now, n=0)


async def serve(reader, writer, server_pub: bytes, sn: bytes, msg: dict) -> None:
    """The tunnel server's ``pair`` branch (``devices_anywhere._pair_handler``)."""
    f = _fleet()
    outcome = await fleet_pair.serve_pair(
        reader,
        writer,
        server_pub=server_pub,
        sn=sn,
        msg=msg,
        book=_book(),
        self_key=f._self_key,
        make_bundle=_make_bundle,
        begin=_begin,
        commit=_commit,
        unfinished=_unfinished,
        reused=_reused,
        addr=_anywhere.current_url,
    )
    _log_outcome(outcome)
    if not _book().known():
        # No code left at all (killed after 5 wrong tries, or expired): its
        # pairing path goes now. A USED code keeps it until it would have
        # expired, so a second holder hears "already used" (the alarm).
        _anywhere.close_pair_path()


# --------------------------------------------------------------------------- #
# The joiner
# --------------------------------------------------------------------------- #
def _error_text(err: BaseException) -> str:
    if isinstance(err, fleet_pair.PairError):
        if err.reason == "refused":
            return err.text or "this computer couldn't take that group"
        return _ERRORS.get(err.reason) or UNREACHABLE
    return UNREACHABLE


def _other_group() -> Optional[dict]:
    """This device's OTHER live members, when it is in a group with some:
    ``{count, hosts}`` (all of them, itself included, in ``count``)."""
    f = _fleet()
    if not f.in_fleet():
        return None
    me = f._self_key()
    live = f.live_members()
    others = sorted((v.get("host") or k) for k, v in live.items() if k != me)
    if not others:
        return None
    return {"count": len(live), "hosts": others}


def preview(text: str) -> dict:
    """What the new computer shows before it joins with ``text`` (M2):
    ``{safety, other_group, text, safety_text, leave_text}``. Raises
    ValueError for a code that doesn't parse."""
    code = fleet_pair.parse_code(text, loopback=_anywhere._loopback())
    safety = fleet_pair.safety_number(code.fp, code.invite_id)
    other = _other_group()
    return {
        "needs_confirm": True,
        "safety": safety,
        "other_group": other,
        "text": CONFIRM_TEXT,
        "safety_text": SAFETY_TEXT.format(safety=safety),
        "leave_text": (
            LEAVE_TEXT.format(count=other["count"], hosts=", ".join(other["hosts"]))
            if other
            else ""
        ),
    }


async def join(
    text: str,
    reachable: bool = True,
    confirm: bool = False,
    leave_group: bool = False,
) -> dict:
    """Join with a pasted ``mfd1:`` code → the join status (``joining``; it
    goes on in the background — poll ``/api/fleet``). Raises
    :class:`NeedsConfirm` (nothing sent) until the person confirmed — and,
    when this device has other live members, confirmed leaving them
    (``leave_group``); ValueError for a code that doesn't parse, or while
    another join runs."""
    f = _fleet()
    code = fleet_pair.parse_code(text, loopback=_anywhere._loopback())
    me = f._self_key()
    if not f.DEVICE_RE.match(me or ""):
        raise ValueError(
            "this computer's name (%s) can't be used as a device name" % (me or "?")
        )
    info = preview(text)
    if not confirm or (info["other_group"] and not leave_group):
        raise NeedsConfirm(info)
    f._start_join(
        {"key": "", "host": "your other computer"}, state="joining", via="anywhere"
    )
    f._JOIN_TASK[0] = f._spawn(_run_join(code, bool(reachable), bool(leave_group)))
    return f.join_status()


async def _reach(inviter: str) -> Optional[dict]:
    """Look for the inviter through the tunnel until it answers (or
    :data:`FINISH_WAIT`) → its discovery record, or None."""
    from backend.web.core import remote as _remote

    f = _fleet()
    deadline = time.monotonic() + FINISH_WAIT
    while True:
        try:
            await _remote.discover_now()
        except Exception:  # noqa: BLE001
            pass
        dev = f._device(inviter)
        if dev and dev.get("reachable") and f.member_device(dev):
            return dev
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(FINISH_STEP)


async def _check_joined(
    code: fleet_pair.PairCode, inviter: str, bundle: dict, addr: str
) -> Optional[bool]:
    """After a lost ``added`` (L5): dial the inviter's members' path with
    the key the bundle carried. Its handshake lets only roster members
    through, so getting in means the join was committed there (True); a
    ``denied`` means it wasn't (False); no answer → None (can't tell)."""
    from backend.peer import fleet_record as _rec
    from backend.peer import fleet_tunnel as _ft

    f = _fleet()
    try:
        keys = _rec.derive(bundle["key"], bundle["id"], int(bundle["epoch"]))
    except (KeyError, TypeError, ValueError):
        return None
    me = f._self_key()
    verdict: Optional[bool] = None
    for i in range(CHECK_TRIES):
        if i:
            await asyncio.sleep(CHECK_STEP)
        try:
            _r, w = await _ft.dial(
                addr, code.fp.hex(), keys, me, inviter, edge_ssl=_anywhere._edge_ssl()
            )
        except _ft.TunnelAuthError as err:
            if err.reason == "denied":
                verdict = False
                continue  # its commit may still be landing: ask again
            return None
        except Exception:  # noqa: BLE001 — can't reach it right now
            continue
        try:
            w.transport.abort()
        except Exception:  # noqa: BLE001
            pass
        return True
    return verdict


async def _run_join(
    code: fleet_pair.PairCode, reachable: bool, leave_group: bool = False
) -> None:
    f = _fleet()
    runs = f.runs_automation()
    taken: dict = {}

    async def adopt(inviter: str, bundle: dict) -> None:
        members = bundle.get("members")
        if not isinstance(members, dict) or inviter not in members:
            raise fleet_pair.PairRefused("that computer isn't in its own group")
        doc = f.state()
        if (
            leave_group
            and doc.get("id")
            and doc["id"] != bundle.get("id")
            and _other_group()
        ):
            # The person confirmed it: leave the other group properly (it is
            # told) before taking this one's key.
            await f.leave_fleet()
            taken["left"] = True
        before = f.state()
        try:
            taken["exposed"] = f.adopt_bundle(bundle)
        except f.OlderBundle:
            raise fleet_pair.PairRefused(
                "that computer has an older key for your devices than this one — "
                "make the code on another of your devices"
            ) from None
        except ValueError as err:
            raise fleet_pair.PairRefused(str(err)) from None
        taken["before"] = before

    async def check(inviter: str, bundle: dict, addr: str) -> Optional[bool]:
        return await _check_joined(code, inviter, bundle, addr)

    try:
        inviter, bundle, addr = await fleet_pair.pair(
            code,
            device=f._self_key(),
            host=f._self_host(),
            dns=f._self_dns(),
            runs=runs,
            adopt=adopt,
            edge_ssl=_anywhere._edge_ssl(),
            check_joined=check,
        )
    except BaseException as err:
        if "before" in taken:
            # Adopted, but the inviter never took us on: undo, so this device
            # doesn't hold a key its group doesn't know it has.
            f.restore(taken["before"])
        text = _error_text(err)
        if taken.get("left"):
            text += (
                " (This computer had already left its other group, as you confirmed.)"
            )
        f._set_join(state="error", error=text)
        if isinstance(err, asyncio.CancelledError):
            raise
        if not isinstance(err, fleet_pair.PairError):
            log.info(
                "devices pair: couldn't reach the inviter (%s)", type(err).__name__
            )
        return
    host = str((bundle["members"].get(inviter) or {}).get("host") or inviter)
    exposed = list(taken.get("exposed") or [])
    f._set_join(state="finishing", device=inviter, host=host)
    try:
        await _finish(code, reachable, inviter, host, bundle, exposed, runs, addr)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 — joined either way: never stuck
        log.warning("devices pair: finishing failed (%s)", type(err).__name__)
        f._set_join(
            state="joined",
            error="",
            note=NOTE_NOT_REACHED.format(host=host),
            self_reachable=True,
        )


async def _finish(
    code: fleet_pair.PairCode,
    reachable: bool,
    inviter: str,
    host: str,
    bundle: dict,
    exposed: list,
    runs: bool,
    addr: str = "",
) -> None:
    """After the pairing: seed the inviter's members' address, and (box
    ticked) turn remote control and the switch on, reach the inviter through
    its tunnel and finish the join like any other. Box unticked: nothing is
    turned on (M2) — only the person is taken."""
    f = _fleet()
    f.adopted_side_effects(bundle, inviter, remote_control=reachable)
    if addr:
        try:
            _anywhere.seed(inviter, addr, code.fp.hex())
        except ValueError:
            pass
    if not reachable:
        f._set_join(state="joined", error="", note=NOTE_SWITCH_OFF, self_reachable=True)
        f._emit(
            "device.joined",
            device=f._self_key(),
            host=f._self_host(),
            via=inviter,
            detail="joined %s's devices" % host,
        )
        return
    try:
        if not _anywhere.enabled():
            _anywhere.save_switch(True)
        await _anywhere.apply()
    except Exception as err:  # noqa: BLE001
        f._set_join(
            state="joined",
            error="",
            note="“Reach my devices from anywhere” couldn't be turned on here "
            "(%s) — turn it on above." % (err or "error"),
            self_reachable=True,
        )
        return
    dev = await _reach(inviter)
    if dev is None:
        f._set_join(
            state="joined",
            error="",
            note=NOTE_NOT_REACHED.format(host=host),
            self_reachable=True,
        )
        return

    async def note() -> str:
        # Settings sync just ran: it normally brought the GitHub sign-in.
        try:
            await _anywhere.publish_soon()
        except Exception:  # noqa: BLE001
            pass
        if not await _anywhere.github_ready():
            return NOTE_NO_GITHUB.format(host=host)
        return ""

    await f.after_adopt(dev, exposed, runs, note_fn=note)
