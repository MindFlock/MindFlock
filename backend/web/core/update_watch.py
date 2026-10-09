"""Finish an engine update with nobody watching, and say when one is out.

Two jobs, one lifespan task (:func:`watch_loop`):

* **Apply a finished install.** The installer (:mod:`backend.web.core.self_update`)
  replaces the tool venv and writes ``update.json``; the restart that makes
  the new build take effect used to happen only when a browser polled
  ``/api/update/state``. A closed tab, a sleeping phone or an update started
  from another device then left the server running old Python on a replaced
  venv, lazily importing NEW modules into the OLD process — the stale-copy
  trap. So the server watches the file itself (every :data:`TICK_S`) and
  re-execs once the install is ``done`` (in the mode it runs in), through the
  same once-only :func:`~backend.web.core.self_update.finish_state` the route
  uses — which holds off while Setup's install terminal is running (a re-exec
  mid-install would take a PTY-backed one down with it), and never restarts a
  process that already runs the installed build
  (:func:`~backend.web.core.self_update.applied`).

* **Say a newer release exists** (``update.available``), once per release and
  per set of devices behind it: browsers, the bell and /m learn it without
  anyone opening Settings → Advanced. Fleet-aware — it names the members of
  "Your devices" whose hello reports an older version. The release lookup is
  :func:`~backend.web.core.self_update.latest_release`'s 15-minute cache, so
  this costs at most one GitHub request per :data:`ANNOUNCE_EVERY_S`.
"""

from __future__ import annotations

import asyncio
import sys
from typing import List, Optional

from backend import log
from backend.web.core import restart as _restart
from backend.web.core import self_update as _self_update

#: Seconds between looks at ``update.json`` (a small local file).
TICK_S = 5.0

#: First release check this long after boot (not in the way of startup), then
#: every :data:`ANNOUNCE_EVERY_S`.
ANNOUNCE_FIRST_S = 120.0
ANNOUNCE_EVERY_S = 30 * 60.0

#: What was last announced (``"<version>|<here>|<behind keys>"``) — one event
#: per distinct answer, not one per check.
_LAST = {"sig": ""}


def _log(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


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
        if state != "done" or st.get("restarted"):
            return False
        # Held there while Setup's install terminal runs ("restart pending").
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


def notice(release: Optional[dict]) -> Optional[dict]:
    """The ``update.available`` payload for ``release`` (None when nothing is
    behind it): ``{latest, tag, current, here, blocked, behind, count,
    detail}``. ``count`` counts every device behind, this one included."""
    if not release:
        return None
    latest = str(release.get("version") or "")
    current = _self_update.installed_version()
    here = _self_update.is_newer(latest, current)
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
        "blocked": _self_update.blocked_reason() if here else "",
        "behind": behind,
        "count": count,
        "detail": detail,
    }


async def announce() -> Optional[dict]:
    """Check once and emit ``update.available`` when the answer is new.
    Returns what was emitted (None otherwise). Never raises."""
    try:
        data = notice(await _self_update.latest_release())
        if data is None:
            _LAST["sig"] = ""
            return None
        sig = "%s|%s|%s" % (
            data["latest"],
            int(data["here"]),
            ",".join(b["key"] for b in data["behind"]),
        )
        if sig == _LAST["sig"]:
            return None
        _LAST["sig"] = sig
        from backend.web.core import events as _events

        _events.BUS.emit("update.available", data=data)
        return data
    except Exception as err:  # noqa: BLE001
        _log("update announce failed: %v", err)
        return None


async def watch_loop() -> None:
    """The lifespan task: :func:`tick` every :data:`TICK_S`, :func:`announce`
    after :data:`ANNOUNCE_FIRST_S` and then every :data:`ANNOUNCE_EVERY_S`.
    The announce half never runs under pytest (it would ask GitHub)."""
    loop = asyncio.get_running_loop()
    next_announce = loop.time() + ANNOUNCE_FIRST_S
    while True:
        if await asyncio.to_thread(tick):
            return  # re-exec scheduled; this process has no future
        if loop.time() >= next_announce and "pytest" not in sys.modules:
            next_announce = loop.time() + ANNOUNCE_EVERY_S
            await announce()
        await asyncio.sleep(TICK_S)
