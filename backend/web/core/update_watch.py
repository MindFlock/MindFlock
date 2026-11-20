"""Updates with nobody watching: finish them, take them, say what happened.

One lifespan task (:func:`watch_loop`) with four jobs:

* **Apply a finished install.** The installer (:mod:`backend.web.core.self_update`)
  replaces the engine and writes ``update.json``; the restart that makes the
  new build take effect used to happen only when a browser polled
  ``/api/update/state``. A closed tab, a sleeping phone or an update started
  from another device then left the server running old Python on a replaced
  venv, lazily importing NEW modules into the OLD process — the stale-copy
  trap. So the server watches the file itself (every :data:`TICK_S`) and
  re-execs once the install is ``done`` (in the mode it runs in), through the
  same once-only :func:`~backend.web.core.self_update.finish_state` the route
  uses — which holds off while Setup's install terminal is running, and for
  an automatic install until nobody has typed for a minute (at most half an
  hour: :func:`~backend.web.core.self_update.restart_wait`), and never
  restarts a process that already runs the installed build.

* **Take a new version by itself** (``updates.mode == "auto"``, and no
  desktop app that drives this engine itself —
  :func:`~backend.web.core.self_update.managed_by_shell`). Detection
  (:func:`announce`) remembers the target (:data:`_PENDING`); the next tick
  starts the install at once — a release install runs in the background and
  disturbs nothing. A development checkout's update moves files under the
  running server (and under anything else using the checkout), so it starts
  at the same quiet moment the restart would wait for. A version whose
  automatic attempt failed is not tried again for :data:`RETRY_AFTER_S`.

* **Say a newer version exists** (``update.available``) — only in ``ask``
  mode and only when someone can act on it: this device can install it, or
  other members of "Your devices" are behind. Once per answer, kept on disk
  across restarts; a GitHub lookup that fails changes nothing (it used to
  forget the answer and announce it again after every blip). A development
  checkout is told about new commits on its own branch, never about
  releases; one that can't update (another branch, uncommitted changes) is
  told nothing at all.

* **Say how it went**, once each: ``update.done`` after the server is back
  on the new build (from the restarted process — the state file survives the
  restart) and ``update.failed`` with a plain sentence. The "already said"
  marks are in ``update_auto.json`` beside ``update.json``, keyed by the
  update, because the installer rewrites ``update.json`` after its health
  check.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from backend.fsutil import atomic_write_json
from backend import log
from backend.web.core import restart as _restart
from backend.web.core import self_update as _self_update

#: Seconds between looks at ``update.json`` (a small local file).
TICK_S = 5.0

#: First check this long after boot (not in the way of startup), then every
#: :data:`ANNOUNCE_EVERY_S` — and soon after the mode is set (:func:`check_soon`).
ANNOUNCE_FIRST_S = 120.0
ANNOUNCE_EVERY_S = 30 * 60.0

#: A version whose automatic install failed is left alone this long.
RETRY_AFTER_S = 6 * 3600.0

#: An outcome older than this is history, not news: no event for it (a state
#: file left from long ago, read by a server that boots today).
REPORT_WINDOW_S = 24 * 3600.0

#: ``update.done`` waits this long after boot, so the browsers that lost the
#: server during the restart are back on the event stream to hear it live.
DONE_AFTER_BOOT_S = 20.0

#: What was last announced (``"<version>|<here>|<behind keys>"``) — one event
#: per distinct answer, not one per check. Kept on disk too (:func:`_last_path`,
#: read once per process) so a restart — every update ends in one — doesn't
#: announce the same answer again.
_LAST = {"sig": "", "loaded": False}

#: The version the automatic path is to install (set by :func:`announce`):
#: ``key`` (a release tag, or a checkout's target commit), ``ref``,
#: ``version``, ``dev`` and ``since`` (when it was first seen).
_PENDING: dict = {"key": "", "ref": "", "version": "", "dev": False, "since": 0.0}

#: Set by :func:`check_soon`: the loop checks at its next tick.
_SOON = {"now": False}

#: The last (update key, state) :func:`_report` looked at — the common case,
#: the same finished update every tick, costs no file read.
_SEEN = {"mark": ""}

_BOOT = {"at": time.time()}


def _last_path() -> Path:
    from backend.config import config as _config

    return Path(_config.GetConfigDir()) / "update_announced.json"


def _last_sig() -> str:
    if not _LAST["loaded"]:
        _LAST["loaded"] = True
        try:
            doc = json.loads(_last_path().read_text(encoding="utf-8"))
            _LAST["sig"] = str((doc or {}).get("sig") or "")
        except (OSError, ValueError, AttributeError):
            pass
    return str(_LAST["sig"] or "")


def _remember(sig: str) -> None:
    if _last_sig() == sig:
        return
    _LAST["sig"] = sig
    try:
        atomic_write_json(
            _last_path(), {"sig": sig}, indent=None, newline=False, ensure_ascii=True
        )
    except OSError as err:
        _log("update announce: could not save the marker (%v)", err)


def _log(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


def _emit(event: str, data: dict) -> None:
    from backend.web.core import events as _events

    _events.BUS.emit(event, data=data)


def check_soon() -> None:
    """Ask the loop to check at its next tick (the mode just changed)."""
    _SOON["now"] = True


# --------------------------------------------------------------------------- #
# The book: what was already said, and which automatic attempts failed
# --------------------------------------------------------------------------- #
def _book_path() -> Path:
    return _self_update._state_dir() / "update_auto.json"


def _book() -> dict:
    try:
        doc = json.loads(_book_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    failed = doc.get("failed") if isinstance(doc.get("failed"), dict) else {}
    reported = doc.get("reported") if isinstance(doc.get("reported"), dict) else {}
    return {"failed": dict(failed), "reported": dict(reported)}


def _save_book(book: dict) -> None:
    now = time.time()
    # Only failures that still hold anything back are worth keeping.
    book["failed"] = {
        k: v
        for k, v in book.get("failed", {}).items()
        if isinstance(v, (int, float)) and now - float(v) < RETRY_AFTER_S
    }
    try:
        atomic_write_json(
            _book_path(), book, indent=None, newline=False, ensure_ascii=True
        )
    except OSError as err:
        _log("update: could not save %s (%v)", _book_path(), err)


def _retry_blocked(key: str, now: Optional[float] = None) -> bool:
    """Whether an automatic attempt at ``key`` failed within :data:`RETRY_AFTER_S`."""
    if not key:
        return False
    now = time.time() if now is None else now
    at = _book()["failed"].get(key)
    return isinstance(at, (int, float)) and now - float(at) < RETRY_AFTER_S


def _target_key(st: dict) -> str:
    """What an automatic attempt is remembered by: a checkout's target
    commit, a release's tag."""
    if st.get("dev"):
        return str(st.get("commit") or "")
    return str(st.get("ref") or "")


def _note_failed(key: str) -> None:
    if not key:
        return
    book = _book()
    book["failed"][key] = time.time()
    _save_book(book)


def failure_sentence(st: dict) -> str:
    """``update.failed``'s ``error``: one plain sentence."""
    error = str(st.get("error") or "").strip()
    if error == "interrupted":
        return "The update stopped before it finished."
    if error:
        error = error[0].upper() + error[1:]
        return error if error.endswith(".") else error + "."
    code = st.get("code")
    return "The install did not finish%s. Settings has the log." % (
        " (exit %s)" % code if code not in (None, "", 0) else ""
    )


def notes_url(st: dict) -> str:
    """The release page for the version an update brought, "" when it isn't
    a release (a checkout that moved within one version)."""
    version = str(st.get("version") or "")
    if not version or version == str(st.get("from_version") or ""):
        return ""
    tag = "v" + version.lstrip("vV")
    if not _self_update.is_release_tag(tag):
        return ""
    return "https://github.com/%s/releases/tag/%s" % (_self_update.UPDATE_REPO, tag)


def _report(st: dict, now: Optional[float] = None) -> Optional[str]:
    """Emit ``update.failed`` / ``update.done`` for ``st`` once. Returns the
    event emitted (None otherwise)."""
    now = time.time() if now is None else now
    state = str(st.get("state") or "")
    if state not in ("failed", "rolled_back", "done"):
        return None
    key = _self_update._update_key(st)
    mark = "%s#%s" % (key, state)
    if _SEEN["mark"] == mark:
        return None
    when = float(st.get("finished_at") or st.get("started_at") or 0.0)
    if not when or now - when > REPORT_WINDOW_S:
        _SEEN["mark"] = mark
        return None
    if state == "done":
        if not _self_update.applied(st):
            return None  # not back on it yet: said once this process runs it
        if now - _BOOT["at"] < DONE_AFTER_BOOT_S:
            return None  # the browsers are still reconnecting
    book = _book()
    slot = "done" if state == "done" else "failed"
    if book["reported"].get(slot) == key:
        _SEEN["mark"] = mark
        return None
    book["reported"][slot] = key
    auto = bool(st.get("auto"))
    if slot == "failed" and auto:
        book["failed"][_target_key(st)] = now
    _save_book(book)
    _SEEN["mark"] = mark
    if slot == "failed":
        _emit(
            "update.failed",
            {
                "version": str(st.get("version") or ""),
                "error": failure_sentence(st),
                "auto": auto,
            },
        )
        return "update.failed"
    _emit(
        "update.done",
        {
            "version": str(st.get("version") or ""),
            "from_version": str(st.get("from_version") or ""),
            "auto": auto,
            "notes_url": notes_url(st),
        },
    )
    return "update.done"


# --------------------------------------------------------------------------- #
# The automatic path
# --------------------------------------------------------------------------- #
def _set_pending(key: str, ref: str, version: str, dev: bool) -> None:
    if _PENDING.get("key") != key:
        _PENDING.update(key=key, since=time.time())
    _PENDING.update(ref=ref, version=version, dev=dev)


def _clear_pending() -> None:
    _PENDING.update(key="", ref="", version="", dev=False, since=0.0)


def _auto_start() -> Optional[dict]:
    """Start the pending automatic install when nothing is in its way.
    Returns ``start_update``'s answer when it tried (None otherwise)."""
    pending = dict(_PENDING)
    if not pending["key"]:
        return None
    mode, _ = _self_update.update_mode()
    if mode != "auto" or _self_update.managed_by_shell():
        return None
    if _self_update.running() or _self_update.restart_pending():
        return None
    if _retry_blocked(pending["key"]):
        return None
    # A checkout moves files under everything using it: wait for a pause.
    if pending["dev"] and not _self_update.quiet_for(
        _self_update.AUTO_IDLE_S, float(pending["since"] or 0.0)
    ):
        return None
    _log("update: installing %s by itself", pending["version"] or pending["ref"])
    result = _self_update.start_update(pending["ref"], auto=True)
    if result.get("ok"):
        return result
    if result.get("quiet"):
        _clear_pending()  # nothing to do after all (up to date, one running)
        return result
    _note_failed(pending["key"])
    _emit(
        "update.failed",
        {
            "version": pending["version"],
            "error": failure_sentence({"error": result.get("error")}),
            "auto": True,
        },
    )
    return result


def tick() -> bool:
    """One look at the update state; True when a restart was scheduled.

    ``started`` with a dead installer settles as interrupted here too
    (:func:`~backend.web.core.self_update.running` does it), so a screen
    opened later reads the truth rather than a spinner. Never raises."""
    try:
        st = _self_update.read_state()
        state = st.get("state")
        if state == "started":
            _self_update.running()
            return False
        try:
            _report(st)
            if _auto_start() is not None:
                return False
        except Exception as err:  # noqa: BLE001 — never in the restart's way
            _log("update watch (auto) failed: %v", err)
        if state != "done" or st.get("restarted"):
            return False
        # Held there while Setup's install terminal runs, or someone types
        # (an automatic install): "restart pending" meanwhile.
        _, restart_now = _self_update.finish_state()
        if restart_now:
            _log(
                "update: %s installed — restarting onto it",
                str(st.get("ref") or st.get("version") or "?"),
            )
            _restart.reset_tailscale_attempts()
            # Same mode as now: an update is no reason to drop a rig started
            # with `mindflock serve tailscale` back to loopback.
            _restart.reexec_soon(keep_mode=True)
        return restart_now
    except Exception as err:  # noqa: BLE001 — the loop must not die
        _log("update watch failed: %v", err)
        return False


# --------------------------------------------------------------------------- #
# What Settings shows (GET /api/update/check)
# --------------------------------------------------------------------------- #
_WHY = {
    "off": "Does not check for new versions. Check now still works.",
    "ask": "Tells you once when a new version is out. One click installs it.",
    "auto": "Installs by itself and restarts when you pause.",
    "shell": "The desktop app installs updates by itself and restarts when you pause.",
}


def auto_status(
    mode: str, shell: bool, blocked: str, dev: Optional[dict] = None
) -> dict:
    """``{active, why, waiting}`` — the ``auto`` object of ``GET
    /api/update/check``. ``active``: this server installs by itself.
    ``waiting``: why a found update hasn't happened yet (``typing``,
    ``setup``, ``retry_later``) or ""."""
    can = bool(dev.get("can_update")) if dev is not None else not blocked
    active = mode == "auto" and not shell and can
    if mode == "auto" and shell:
        why = _WHY["shell"]
    elif mode == "auto" and not can:
        why = (dev or {}).get("why_not") or blocked or _WHY["ask"]
    else:
        why = _WHY.get(mode, _WHY["ask"])
    waiting = ""
    try:
        waiting = _self_update.restart_wait()
        pending = dict(_PENDING)
        if not waiting and active and pending["key"]:
            if _retry_blocked(pending["key"]):
                waiting = "retry_later"
            elif (
                pending["dev"]
                and not _self_update.running()
                and not (
                    _self_update.quiet_for(
                        _self_update.AUTO_IDLE_S, float(pending["since"] or 0.0)
                    )
                )
            ):
                waiting = "typing"
    except Exception:  # noqa: BLE001 — a status line never fails a screen
        waiting = ""
    return {"active": bool(active), "why": why, "waiting": waiting}


def status_fields(blocked: str, dev: Optional[dict] = None) -> dict:
    """The fields ``GET /api/update/check`` adds: ``mode``, ``mode_set``,
    ``managed_by_shell``, ``auto`` and ``dev`` (null unless this is a
    development checkout; ``dev`` here is its full status)."""
    from backend.web.core import dev_update

    mode, mode_set = _self_update.update_mode()
    shell = _self_update.managed_by_shell()
    return {
        "mode": mode,
        "mode_set": mode_set,
        "managed_by_shell": shell,
        "auto": auto_status(mode, shell, blocked, dev),
        "dev": dev_update.public(dev),
    }


# --------------------------------------------------------------------------- #
# Detection and update.available
# --------------------------------------------------------------------------- #
def members_behind(version: str) -> List[dict]:
    """The OTHER live members of "Your devices" whose hello reports a version
    older than ``version`` (``[{key, host, version}]``). Offline members are
    left out: their version is a memory, not a fact."""
    try:
        from backend.web.core import fleet as _fleet
        from backend.web.core import remote as _remote

        me = _fleet._self_key()
        out = []
        for key in sorted(_fleet.live_members()):
            if key == me:
                continue
            dev = _remote._DEVICES.get(key) or {}
            theirs = str(dev.get("version") or "")
            if dev.get("reachable") and _self_update.is_newer(version, theirs):
                out.append(
                    {"key": key, "host": str(dev.get("host") or key), "version": theirs}
                )
        return out
    except Exception:  # noqa: BLE001
        return []


def notice(
    release: Optional[dict], actionable: Optional[bool] = None
) -> Optional[dict]:
    """The ``update.available`` payload for ``release`` (None when nothing
    anyone can act on is behind it): ``{latest, tag, current, here, blocked,
    behind, count, detail, auto}``. ``here`` is true only when THIS device is
    behind and can install it (``actionable``; default: nothing blocks the
    installer here). ``count`` counts every device behind that can act."""
    if not release:
        return None
    latest = str(release.get("version") or "")
    current = _self_update.installed_version()
    newer = _self_update.is_newer(latest, current)
    if newer and actionable is None:
        actionable = not _self_update.blocked_reason()
    here = bool(newer and actionable)
    behind = members_behind(latest)
    if not here and not behind:
        return None
    count = len(behind) + (1 if here else 0)
    if behind:
        detail = "MindFlock v%s is out — %d of your devices %s behind" % (
            latest,
            count,
            "is" if count == 1 else "are",
        )
    else:
        detail = "MindFlock v%s is out (this device runs v%s)" % (latest, current)
    return {
        "latest": latest,
        "tag": str(release.get("tag") or ""),
        "current": current,
        "here": here,
        "blocked": "",
        "behind": behind,
        "count": count,
        "detail": detail,
        "auto": False,
    }


def dev_notice(dev: dict) -> dict:
    """``update.available`` for a development checkout that can move forward."""
    current = _self_update.installed_version()
    latest = str(dev.get("target_version") or "") or current
    behind = int(dev.get("behind") or 0)
    if latest and _self_update.is_newer(latest, current):
        detail = "MindFlock v%s is ready for this development checkout" % latest
    else:
        detail = "%d new commit%s on %s for this development checkout" % (
            behind,
            "" if behind == 1 else "s",
            dev.get("branch") or "main",
        )
    return {
        "latest": latest,
        "tag": "",
        "current": current,
        "here": True,
        "blocked": "",
        "behind": [],
        "count": 1,
        "detail": detail,
        "auto": False,
        "dev": True,
    }


async def announce(force: bool = False) -> Optional[dict]:
    """Check once: in ``auto`` mode remember what to install (the next tick
    starts it), in ``ask`` mode emit ``update.available`` when the answer is
    new, in ``off`` mode do nothing. Returns what was emitted (None
    otherwise). Never raises."""
    try:
        mode, _ = _self_update.update_mode()
        if mode == "off":
            _clear_pending()
            return None
        kind = _self_update.install_kind()
        if kind == "editable":
            from backend.web.core import dev_update

            dev = await asyncio.to_thread(dev_update.status, refresh=True, force=force)
            if not dev or dev.get("behind", 0) <= 0 or not dev.get("can_update"):
                _clear_pending()
                return None  # nothing new, or nothing anyone can do here: quiet
            if mode == "auto":
                _set_pending(
                    dev["target"], dev["branch"], dev.get("target_version") or "", True
                )
                return None
            _clear_pending()
            data = dev_notice(dev)
            sig = "dev:%s" % dev["target"]
        else:
            release = await _self_update.latest_release(force=force)
            if release is None:
                return None  # couldn't tell: keep what was said and what is pending
            newer = _self_update.is_newer(
                str(release.get("version") or ""), _self_update.installed_version()
            )
            actionable = newer and not _self_update.blocked_reason()
            if mode == "auto":
                if actionable and not _self_update.managed_by_shell():
                    _set_pending(
                        str(release.get("tag") or ""),
                        str(release.get("tag") or ""),
                        str(release.get("version") or ""),
                        False,
                    )
                else:
                    _clear_pending()
                return None  # automatic: nothing to offer anyone
            _clear_pending()
            data = notice(release, actionable)
            if data is None:
                _remember("")
                return None
            sig = "%s|%s|%s" % (
                data["latest"],
                int(data["here"]),
                ",".join(b["key"] for b in data["behind"]),
            )
        if sig == _last_sig():
            return None
        _remember(sig)
        _emit("update.available", data)
        return data
    except Exception as err:  # noqa: BLE001
        _log("update announce failed: %v", err)
        return None


async def watch_loop() -> None:
    """The lifespan task: :func:`tick` every :data:`TICK_S`, :func:`announce`
    after :data:`ANNOUNCE_FIRST_S` and then every :data:`ANNOUNCE_EVERY_S`
    (or at the next tick after :func:`check_soon`). The announce half never
    runs under pytest (it would ask GitHub)."""
    loop = asyncio.get_running_loop()
    next_announce = loop.time() + ANNOUNCE_FIRST_S
    while True:
        if await asyncio.to_thread(tick):
            return  # re-exec scheduled; this process has no future
        due = loop.time() >= next_announce or _SOON["now"]
        if due and "pytest" not in sys.modules:
            _SOON["now"] = False
            next_announce = loop.time() + ANNOUNCE_EVERY_S
            await announce()
        await asyncio.sleep(TICK_S)
