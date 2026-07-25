"""Shared terminal plumbing: the ONE PTY<->websocket pump.

Every websocket terminal — the per-instance agent/shell terminals, and addon
panes like the MindFlock log tail and the Assistant chat — bridges a spawned
``ptyprocess`` to a websocket the same way. This module owns that single
implementation so nobody re-implements the ~70-line bridge (and its resize /
read-only / teardown semantics) again.

Note: the websocket *route* layer still owns connection validation and the
close-code protocol (4404 instance-gone / 4409 workspace-gone / 4500 spawn
failure) — the pump only runs once a socket is accepted and a PTY is spawned.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

import ptyprocess
from fastapi import WebSocket, WebSocketDisconnect

# --------------------------------------------------------------------------- #
# Terminal scroll speed (UI setting)
#
# The web terminals run tmux with ``mouse on``, so the mouse wheel scrolls
# through tmux's copy-mode. The number of lines per wheel notch is the
# copy-mode WheelUp/DownPane binding's ``-N`` count — that's what this setting
# tunes (server-wide, since tmux key bindings are global). Persisted to a small
# file so it survives restarts; applied when sessions are created and live when
# the setting changes.
# --------------------------------------------------------------------------- #
_MINDFLOCK_HOME = Path(
    os.environ.get("MINDFLOCK_ASSISTANT_DIR", str(Path.home() / ".mindflock-assistant"))
)
SCROLL_SPEED_PATH = Path(
    os.environ.get("MINDFLOCK_SCROLL_SPEED_FILE", str(_MINDFLOCK_HOME / "scroll-speed"))
)
_SCROLL_SPEED_DEFAULT = 1
_SCROLL_SPEED_MIN = 1 / 3
_SCROLL_SPEED_MAX = 3


def _clamp_scroll_speed(n):
    """Clamp to [MIN, MAX] and snap to thirds of a line (0.33 … 3). tmux ``-N``
    counts are whole lines, so the browser enforces the fractional part by
    scaling (eating or adding) wheel ticks — see attachWheelScroll in app.js."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return _SCROLL_SPEED_DEFAULT
    if n != n:  # NaN
        return _SCROLL_SPEED_DEFAULT
    n = max(_SCROLL_SPEED_MIN, min(_SCROLL_SPEED_MAX, n))
    thirds = round(n * 3)
    return thirds // 3 if thirds % 3 == 0 else round(thirds / 3, 4)


def load_scroll_speed():
    """Persisted wheel scroll speed (lines per notch, fractional below 1);
    default 1 if unset."""
    try:
        raw = SCROLL_SPEED_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return _SCROLL_SPEED_DEFAULT
    return _clamp_scroll_speed(raw)


def save_scroll_speed(n):
    """Persist the scroll speed (clamped); return the value written."""
    speed = _clamp_scroll_speed(n)
    try:
        SCROLL_SPEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCROLL_SPEED_PATH.write_text(str(speed), encoding="utf-8")
    except OSError:
        pass
    return speed


def _tmux_server_running() -> bool:
    """True if a tmux server is up (``tmux list-sessions`` exits 0). False on a
    non-zero exit or if the probe times out — callers treat that as "no server
    yet" and re-apply their tuning when the next session starts."""
    try:
        return (
            subprocess.run(
                ["tmux", "list-sessions"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def apply_scroll_speed(speed: Optional[int] = None) -> None:
    """Tune the tmux mouse-wheel scroll amount to ``speed`` per notch, server-wide.

    Two cases, because a wheel notch is handled differently depending on whether
    the pane's app has grabbed the mouse:

    * **Shell / copy-mode panes** (no app mouse mode): the wheel enters tmux
      copy-mode, so we bind copy-mode WheelUp/DownPane to scroll ``speed`` lines.
    * **Agent panes** running a full-screen app that enables its own mouse mode
      (e.g. Claude Code): tmux forwards the wheel to the app instead of entering
      copy-mode, so the copy-mode binding never fires there. We rebind the *root*
      wheel keys to forward the event ``speed`` times per notch when the app has
      the mouse, so the app scrolls proportionally (falling back to the normal
      copy-mode entry otherwise). ``speed == 1`` reproduces tmux's default.

    No-op if no tmux server is running yet — re-applied when the next session
    starts (server calls this on startup and per session creation).
    """
    speed = load_scroll_speed() if speed is None else _clamp_scroll_speed(speed)
    if not _tmux_server_running():
        return
    # Sub-line speeds (< 1) are enforced browser-side by eating wheel events;
    # tmux can only scroll whole lines, so it runs at the one-line floor here.
    lines = max(1, int(round(speed)))
    for table in ("copy-mode", "copy-mode-vi"):
        for key, direction in (
            ("WheelUpPane", "scroll-up"),
            ("WheelDownPane", "scroll-down"),
        ):
            try:
                subprocess.run(
                    [
                        "tmux",
                        "bind-key",
                        "-T",
                        table,
                        key,
                        "send-keys",
                        "-X",
                        "-N",
                        str(lines),
                        direction,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                pass  # best-effort tuning; a wedged tmux must not block startup
    # Root wheel bindings: when the pane's app grabs the mouse, forward the wheel
    # `lines` times; otherwise keep tmux's default (enter copy-mode on scroll-up).
    forward = " ; ".join(["send-keys -M"] * lines)
    grabbed = "#{||:#{pane_in_mode},#{mouse_any_flag}}"
    for key, otherwise in (
        ("WheelUpPane", "copy-mode -e"),
        ("WheelDownPane", "send-keys -M"),
    ):
        try:
            subprocess.run(
                [
                    "tmux",
                    "bind-key",
                    "-T",
                    "root",
                    key,
                    "if-shell",
                    "-F",
                    grabbed,
                    forward,
                    otherwise,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass  # best-effort tuning; a wedged tmux must not block startup


# --------------------------------------------------------------------------- #
# Exit markers: a launched agent session is wrapped so it records its exit code
# when it ends; if the tmux session is gone and the marker says it quit normally
# we don't relaunch it (we restart fresh instead of --continue resume). Shared by
# the per-instance agent sessions and addon-owned sessions (e.g. the Assistant).
# --------------------------------------------------------------------------- #
_EXIT_MARKER_DIR = Path(
    os.environ.get(
        "MINDFLOCK_EXIT_MARKER_DIR",
        str(Path.home() / ".mindflock-assistant" / ".exit-markers"),
    )
)


def _exit_marker_path(session_name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_name)
    return _EXIT_MARKER_DIR / (safe + ".code")


def _read_exit_marker(session_name: str):
    """Recorded exit code of a finished session as an int, or None if there's no
    marker (killed outright / crashed / never wrote one)."""
    try:
        raw = _exit_marker_path(session_name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _clear_exit_marker(session_name: str) -> None:
    try:
        _exit_marker_path(session_name).unlink()
    except OSError:
        pass


def _is_natural_exit(code) -> bool:
    """True when the agent ended the way the user asked: a clean quit (0) or an
    interrupt (130 = SIGINT/Ctrl+C). Anything else (137 SIGKILL, 143 SIGTERM,
    nonzero crash) or a missing marker counts as unnatural -> safe to resume.

    Note: providers own the authoritative exit-code policy
    (``CodingProvider.is_natural_exit``); this is the shared default (0/130).
    """
    return code in (0, 130)


def _wrap_launch_cmd(cmd: str, session_name: str) -> str:
    """Wrap a shell launch command so the session records its exit code when the
    agent ends. Clears any stale marker first, so an old 'natural' marker can't
    make a later kill look natural. ``cmd`` is already a shell string (program
    name, launcher path, or launch command); tmux runs the result via ``sh -c``."""
    marker = _exit_marker_path(session_name)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    q = shlex.quote(str(marker))
    return "rm -f %s; %s; echo $? > %s" % (q, cmd, q)


def spawn_tmux_attach(session_name: str, dimensions=(24, 80)):
    """Spawn a PTY attached to a tmux session (interactive)."""
    return ptyprocess.PtyProcess.spawn(
        ["tmux", "attach-session", "-t", session_name],
        dimensions=dimensions,
        env={**os.environ, "TERM": "xterm-256color"},
    )


def spawn_tail(path, lines: int = 500, dimensions=(24, 80)):
    """Spawn a PTY following a log file (read-only consumers)."""
    return ptyprocess.PtyProcess.spawn(
        ["tail", "-F", "-n", str(lines), str(path)],
        dimensions=dimensions,
        env={**os.environ, "TERM": "xterm-256color"},
    )


async def pump_pty(ws: WebSocket, proc, allow_input: bool = True) -> None:
    """Bridge a PtyProcess to a websocket: PTY output -> ws, ws input -> PTY,
    resize control frames -> ioctl. ``allow_input=False`` makes it read-only
    (used for the log stream)."""
    loop = asyncio.get_running_loop()
    out_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
    fd = proc.fd

    def _on_readable() -> None:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            out_q.put_nowait(None)
            return
        out_q.put_nowait(data)

    loop.add_reader(fd, _on_readable)

    async def _pump_out() -> None:
        while True:
            data = await out_q.get()
            if data is None:
                break
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                break
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    sender = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b is not None:
                if allow_input:
                    os.write(fd, b)
                continue
            t = msg.get("text")
            if t is not None:
                try:
                    j = json.loads(t)
                except (ValueError, TypeError):
                    j = None
                if isinstance(j, dict) and j.get("type") == "resize":
                    try:
                        proc.setwinsize(int(j["rows"]), int(j["cols"]))
                    except Exception:  # noqa: BLE001
                        pass
                elif allow_input:
                    os.write(fd, t.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        out_q.put_nowait(None)
        sender.cancel()
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
