"""Out of one model, not out of usage: step the session down a model.

A plan can run out of ONE model while it still has room overall. Claude Code
then answers every prompt with a notice instead of a turn — "You've hit your
individual spend limit · … · your Fable limit resets Nov 30 at 11pm
(America/New_York)" — until that date, and the session's queue, its route,
its orchestrator's messages all stop there. The notice reads as idle (it is
not the plan's limit screen), so nothing else notices.

This sweep does what the person would: it reads the notice, types the
provider's switch command for the next model down (``/model opus``), then
one line telling the agent to carry on with the request that bounced, and
emits :data:`EVENT` (the app's toast). A limit on the new model steps down
again (opus → sonnet → haiku); the bottom of the ladder stays put.

Acts only when ALL of these hold (:func:`check`):

* the session runs here, its agent is up and idle, and its provider has a
  ladder and a switch command (``BaseProvider.model_ladder`` /
  ``model_switch_command``; ``[usage] model_ladder`` / ``model_switch`` for a
  TOML provider);
* the NEWEST notice on its screen is a limit on a model of that ladder
  (``BaseProvider.model_limit``): nothing after it but the prompt — no reply,
  no "Set model to …". An account-wide limit names no model and is the
  prompt queue's limit hold, untouched;
* no dialog is on screen, and nobody typed into it in the last
  :data:`TYPED_HOLD_S` (a person at the keys decides);
* it has not been switched for that same model within :data:`COOLDOWN_S`
  (a switch that didn't take is retried after it, never in a loop).

``general.model_downshift: false`` turns it off. Runs in one server per
config dir (the ``model-downshift`` lease), like the sleep sweep.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend import log, providers, session
from backend.fsutil import atomic_write_json, read_json
from backend.session import mux, tmux
from backend.session.tmux.tmux import exact_pane_target

__all__ = [
    "EVENT",
    "LEASE",
    "enabled",
    "check",
    "resume_text",
    "notice_text",
    "tick",
    "run_loop",
]

#: How often the sweep looks.
TICK_SECONDS = 15.0
#: The singleton lease (``sleep.hold_lease``) and how long it lasts unrenewed.
LEASE = "model-downshift"
LEASE_TTL_S = 4 * TICK_SECONDS
#: The event one switch emits (the app's toast).
EVENT = "session.model_downshifted"
#: The same session and model are not switched again within this long.
COOLDOWN_S = 5 * 60.0
#: A person who typed into the session this recently has the keys.
TYPED_HOLD_S = 30.0
#: Between the switch command and the resume line: the CLI redraws.
SWITCH_SETTLE_S = 2.0

#: How long after a switch the person's saved default is watched and put
#: back (the CLI may write it a moment after it answers).
RESTORE_WATCH_S = 60.0

_LOCK = threading.Lock()
#: title -> (model switched away from, when).
_SWITCHED: Dict[str, Tuple[str, float]] = {}
#: default file -> {"had": bool, "model": value, "until": epoch}: the
#: person's own default before a switch rewrote it.
_RESTORE: Dict[str, dict] = {}


def _server():
    from backend.web import server

    return server


def _info(fmt: str, *args) -> None:
    logger = log.InfoLog or log.ErrorLog
    if logger is not None:
        try:
            logger.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


def enabled() -> bool:
    """``general.model_downshift`` (unset = on). Never raises."""
    try:
        from backend.config import settings as _settings

        val = _settings.load_settings().general.model_downshift
    except Exception:  # noqa: BLE001
        return True
    return val is not False


def _when(ts: Optional[float]) -> str:
    """ "Nov 30 at 11pm" for a reset epoch, "" when unknown."""
    if not ts:
        return ""
    try:
        d = _dt.datetime.fromtimestamp(float(ts))
    except (OverflowError, OSError, ValueError):
        return ""
    hour = d.strftime("%I").lstrip("0") or "12"
    mins = "" if d.minute == 0 else d.strftime(":%M")
    return "%s %d at %s%s%s" % (
        d.strftime("%b"),
        d.day,
        hour,
        mins,
        d.strftime("%p").lower(),
    )


def _cap(model: str) -> str:
    return str(model or "").strip().capitalize()


def resume_text(old: str, new: str, reset_at: Optional[float]) -> str:
    """The line typed after the switch: why the model changed, and to carry
    on with the request the limit bounced."""
    back = _when(reset_at)
    return (
        "MindFlock note: %s's limit is used up%s, so this session switched to "
        "%s. Carry on with the last request."
        % (_cap(old), (" until " + back) if back else "", _cap(new))
    )


def notice_text(title: str, old: str, new: str, reset_at: Optional[float]) -> str:
    """The toast: "Switched “api” from Fable to Opus — Fable is out until
    Nov 30 at 11pm"."""
    back = _when(reset_at)
    return "Switched “%s” from %s to %s: %s is used up%s" % (
        title,
        _cap(old),
        _cap(new),
        _cap(old),
        (" until " + back) if back else "",
    )


def _capture(name: str) -> str:
    try:
        cp = mux.run(
            ["tmux", "capture-pane", "-p", "-t", exact_pane_target(name)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return ""
    return cp.stdout.decode("utf-8", "replace") if cp.returncode == 0 else ""


def check(srv, inst, title: str, row: dict, now: float, text: Optional[str] = None):
    """``(limit, command)`` when ``title`` should step down now — ``limit``
    the provider's :meth:`model_limit` reading (``model``, ``next``,
    ``reset_at``), ``command`` what to type — else None. ``text`` is its
    screen (captured when None)."""
    if row.get("device") or row.get("sleeping"):
        return None
    if str(row.get("activity") or "") != "idle":
        return None
    try:
        if not inst.Started() or inst.Status in (session.Paused, session.Loading):
            return None
    except Exception:  # noqa: BLE001
        return None
    provider = providers.resolve(getattr(inst, "Program", "") or "")
    try:
        if not provider.model_ladder():
            return None
    except Exception:  # noqa: BLE001
        return None
    name = tmux.to_mindflock_tmux_name(title)
    if text is None:
        if srv._live_session_name(name) is None:
            return None
        text = _capture(name)
    try:
        limit = provider.model_limit(text, now)
    except Exception:  # noqa: BLE001 — detection never breaks the sweep
        return None
    if not limit or not limit.get("next"):
        return None
    cmd = provider.model_switch_command(limit["next"])
    if not cmd:
        return None
    with _LOCK:
        last = _SWITCHED.get(title)
    if last and last[0] == limit["model"] and now - last[1] < COOLDOWN_S:
        return None
    try:
        typed = float(srv._HUMAN_INPUT_AT.get(title, 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        typed = 0.0
    if now - typed < TYPED_HOLD_S:
        return None
    return limit, cmd


def _default_of(path: str) -> Tuple[bool, object]:
    """``(has a "model" key, its value)`` in the JSON file ``path`` (a
    missing or unreadable file reads as no key)."""
    data = read_json(path, None, expect=dict)
    if not data:
        return False, None
    return "model" in data, data.get("model")


def _remember_default(path: Optional[str], now: float) -> None:
    """Note the person's default before a switch rewrites it. A watch already
    running for that file keeps ITS snapshot (the one from before the first
    switch), only its window extends."""
    if not path:
        return
    with _LOCK:
        rec = _RESTORE.get(path)
        if rec is not None:
            rec["until"] = now + RESTORE_WATCH_S
            return
        had, model = _default_of(path)
        _RESTORE[path] = {"had": had, "model": model, "until": now + RESTORE_WATCH_S}


def restore_defaults(now: Optional[float] = None) -> List[str]:
    """Put back the person's default model where a switch saved its own
    (Claude Code's ``/model`` writes settings.json). Only the ``model`` key
    is touched. Returns the files rewritten."""
    now = time.time() if now is None else now
    fixed: List[str] = []
    with _LOCK:
        items = list(_RESTORE.items())
    for path, rec in items:
        try:
            cur = read_json(path, None, expect=dict)
            if cur is not None and (("model" in cur), cur.get("model")) != (
                rec["had"],
                rec["model"],
            ):
                if rec["had"]:
                    cur["model"] = rec["model"]
                else:
                    cur.pop("model", None)
                atomic_write_json(path, cur)
                fixed.append(path)
                _info("model-downshift: kept the default model in %s", path)
        except Exception:  # noqa: BLE001 — never break the sweep
            pass
        if now >= rec["until"]:
            with _LOCK:
                _RESTORE.pop(path, None)
    return fixed


def _switch(srv, inst, title: str, limit: dict, cmd: str, now: float) -> bool:
    """Type the switch, then the resume line. True when both went in."""
    from backend.web.core import queue_driver as _queue_driver

    name = tmux.to_mindflock_tmux_name(title)
    if _queue_driver._dialog_on_screen(inst, name):
        return False
    with _LOCK:
        _SWITCHED[title] = (limit["model"], now)
    try:
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        default_file = provider.model_default_file(inst)
    except Exception:  # noqa: BLE001
        default_file = None
    _remember_default(default_file, now)
    if not srv._send_to_agent(name, cmd, submit=True):
        return False
    time.sleep(SWITCH_SETTLE_S)
    # The switch saved its model as the CLI's default for new sessions: put
    # the person's own back (and again on the next passes, should the CLI
    # write it late).
    restore_defaults(now)
    if _queue_driver._dialog_on_screen(inst, name):
        # The switch asked something (a picker, a confirm): leave it to the
        # person rather than type into it.
        return False
    return bool(
        srv._send_to_agent(
            name,
            resume_text(limit["model"], limit["next"], limit.get("reset_at")),
            submit=True,
        )
    )


def announce(title: str, limit: dict) -> Optional[dict]:
    """ONE :data:`EVENT` for a switch. Never raises."""
    try:
        from backend.web.core import events as _events

        return _events.BUS.emit(
            EVENT,
            session=title,
            new=str(limit.get("next") or ""),
            data={
                "from": limit.get("model"),
                "to": limit.get("next"),
                "reset_at": limit.get("reset_at"),
                "detail": notice_text(
                    title, limit["model"], limit["next"], limit.get("reset_at")
                ),
            },
        )
    except Exception:  # noqa: BLE001 — a notice must never break the sweep
        return None


def tick(srv=None, now: Optional[float] = None) -> List[str]:
    """One pass: step down every session the newest notice of which is a
    one-model limit. Returns the titles switched."""
    if srv is None:
        srv = _server()
    if not enabled():
        return []
    from backend.web.core import events as _events
    from backend.web.core import sleep as _sleep

    if not _sleep.hold_lease(LEASE, LEASE_TTL_S):
        return []
    now = time.time() if now is None else now
    restore_defaults(now)
    rows = [r for r in _events.sessions_snapshot() if isinstance(r, dict)]
    by_title = {r.get("title"): r for r in rows if not r.get("device")}
    with srv.ENGINE.lock:
        items = list(srv.ENGINE.instances.items())
    done: List[str] = []
    for title, inst in items:
        row = by_title.get(title)
        if row is None:
            continue
        try:
            hit = check(srv, inst, title, row, now)
            if hit is None:
                continue
            limit, cmd = hit
            if _switch(srv, inst, title, limit, cmd, now):
                _info(
                    "model-downshift: %s %s -> %s (%s)",
                    title,
                    limit["model"],
                    limit["next"],
                    cmd,
                )
                announce(title, limit)
                done.append(title)
        except Exception:  # noqa: BLE001 — one bad session can't stop the pass
            continue
    with _LOCK:
        for t in [t for t in _SWITCHED if t not in srv.ENGINE.instances]:
            _SWITCHED.pop(t, None)
    return done


async def run_loop(srv=None) -> None:
    """Drive :func:`tick` forever (started by the server lifespan), after the
    boot quiet window."""
    if srv is None:
        srv = _server()
    try:
        left = srv._BOOT_QUIET_SECONDS - (time.monotonic() - srv._BOOT_MONO)
    except Exception:  # noqa: BLE001
        left = 0.0
    if left > 0:
        await asyncio.sleep(left + 1.0)
    from backend.web.core import sleep as _sleep

    try:
        while True:
            try:
                runner = getattr(srv, "_in_bg_pool", None)
                await (runner(tick, srv) if runner else asyncio.to_thread(tick, srv))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(TICK_SECONDS)
    finally:
        _sleep.release_lease(LEASE)
