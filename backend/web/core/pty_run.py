"""A detachable PTY run — the plain-PTY stand-in for a helper tmux session.

The install and login terminals (:mod:`backend.web.core.setup_install`,
:mod:`backend.web.core.provider_login`) host their command in a throwaway tmux
session the browser attaches to. That fails exactly when it matters most: tmux
is itself the missing dependency on a fresh Mac or a minimal Linux, so the
first install button a new user pressed printed ``tmux new-session failed`` and
installed nothing. Without tmux the command runs here instead, straight under a
PTY, with the two tmux properties those terminals rely on kept:

* **It outlives the window.** A reader thread drains the PTY whether or not a
  browser is attached (an undrained PTY blocks its writer — apt would stall),
  and closing the websocket only detaches. Closing an install window halfway
  through an ``apt-get`` must not kill it.
* **Reattaching shows what happened.** Output is kept in a bounded backlog and
  replayed to whoever attaches next, before the live stream.

Runs are keyed by name in this process (they die with the server, as a tmux
helper session would not — acceptable for a minutes-long install or a login).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Callable, Dict, List, Optional

#: Replayed to a reattaching browser; older output is dropped.
_BACKLOG_MAX = 256 * 1024

_RUNS: Dict[str, "PtyRun"] = {}
_LOCK = threading.Lock()


class PtyRun:
    """One command under a PTY, drained by a thread, fanned out to listeners."""

    def __init__(self, argv: List[str], cwd: str, env: Optional[dict] = None):
        import ptyprocess

        self.proc = ptyprocess.PtyProcess.spawn(
            argv,
            cwd=cwd,
            env={**(env if env is not None else os.environ), "TERM": "xterm-256color"},
            dimensions=(24, 80),
        )
        self._backlog = bytearray()
        self._subs: List[Callable[[Optional[bytes]], None]] = []
        self._lock = threading.Lock()
        self.finished = False
        threading.Thread(target=self._drain, name="pty-run", daemon=True).start()

    def _drain(self) -> None:
        fd = self.proc.fd
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:  # EIO once the child side is gone
                data = b""
            if not data:
                break
            with self._lock:
                self._backlog += data
                if len(self._backlog) > _BACKLOG_MAX:
                    del self._backlog[: len(self._backlog) - _BACKLOG_MAX]
                subs = list(self._subs)
            for cb in subs:
                _call(cb, data)
        with self._lock:
            self.finished = True
            subs = list(self._subs)
        for cb in subs:
            _call(cb, None)
        try:  # reap, so a finished run leaves no zombie
            self.proc.wait()
        except Exception:  # noqa: BLE001
            pass

    def alive(self) -> bool:
        return not self.finished

    def attach(self, cb: Callable[[Optional[bytes]], None]) -> bytes:
        """Register ``cb`` for live output (``None`` = the run ended) and return
        the backlog — atomically, so no chunk is both replayed and streamed, or
        neither."""
        with self._lock:
            if not self.finished:
                self._subs.append(cb)
            return bytes(self._backlog)

    def detach(self, cb) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    def write(self, data: bytes) -> None:
        if self.finished:
            return
        try:
            os.write(self.proc.fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        try:
            self.proc.setwinsize(rows, cols)
        except Exception:  # noqa: BLE001
            pass

    def terminate(self) -> None:
        try:
            self.proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass


def _call(cb, data) -> None:
    try:
        cb(data)
    except Exception:  # noqa: BLE001 — a dead listener must not stop the drain
        pass


def get(name: str) -> Optional[PtyRun]:
    with _LOCK:
        return _RUNS.get(name)


def start(name: str, argv: List[str], cwd: str, env: Optional[dict] = None) -> PtyRun:
    """Start a run under ``name``, replacing (and killing) any previous one."""
    with _LOCK:
        old = _RUNS.pop(name, None)
    if old is not None:
        old.terminate()
    run = PtyRun(argv, cwd, env)
    with _LOCK:
        _RUNS[name] = run
    return run


def kill(name: str) -> None:
    """Terminate and forget the run under ``name`` (no-op when there is none)."""
    with _LOCK:
        run = _RUNS.pop(name, None)
    if run is not None:
        run.terminate()


async def bridge(ws, run: PtyRun, allow_input: bool = True) -> None:
    """Attach websocket ``ws`` to ``run`` until the browser goes away.

    The same wire protocol as :func:`backend.web.core.terminal.pump_pty` (bytes
    out; bytes/text in; ``{"type": "resize"}`` control frames), with one
    difference: disconnecting only detaches — the run goes on. When the run
    ends the socket stays open, so its last words stay on screen instead of a
    reconnecting client starting the command over."""
    loop = asyncio.get_running_loop()
    out_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()

    def _on_output(data: Optional[bytes]) -> None:
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, data)
        except RuntimeError:  # loop closed under us
            pass

    backlog = run.attach(_on_output)

    async def _pump_out() -> None:
        if backlog:
            await ws.send_bytes(backlog)
        while True:
            data = await out_q.get()
            if data is None:
                return
            await ws.send_bytes(data)

    sender = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b is not None:
                if allow_input:
                    run.write(b)
                continue
            t = msg.get("text")
            if t is None:
                continue
            try:
                j = json.loads(t)
            except (ValueError, TypeError):
                j = None
            if isinstance(j, dict) and j.get("type") == "resize":
                try:
                    run.resize(int(j["rows"]), int(j["cols"]))
                except (KeyError, TypeError, ValueError):
                    pass
            elif allow_input:
                run.write(t.encode("utf-8"))
    except Exception:  # noqa: BLE001 — WebSocketDisconnect and friends
        pass
    finally:
        run.detach(_on_output)
        sender.cancel()


async def serve(ws, session: str) -> None:
    """Attach ``ws`` to the helper terminal ``session`` — its plain-PTY run when
    there is one (the host has no tmux), else the tmux session of that name."""
    run = get(session)
    if run is not None:
        await bridge(ws, run)
        return
    from backend.web.core.terminal import pump_pty, spawn_tmux_attach

    try:
        proc = spawn_tmux_attach(session)
    except Exception as exc:  # noqa: BLE001
        await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await ws.close(code=4500)
        return
    await pump_pty(ws, proc, allow_input=True)
