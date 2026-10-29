"""Other people's keystrokes into a session's pane: the safe path.

A guest link's browser (:mod:`backend.web.core.guests`) and a peer typing
through a shared session (:mod:`backend.peer.sessions_bridge`) both reach a
pane on the owner's machine. Neither may hold a WRITABLE tmux client: on one,
the tmux prefix key hands the typist tmux itself — ``prefix c`` opens a new
window running a plain shell outside the session's agent, ``prefix :`` runs
``run-shell``, ``prefix s`` switches the client to any other session on the
tmux server. So their terminal is two separate halves:

* **output** — a READ-ONLY attach client (:func:`spawn_readonly_attach`,
  ``attach-session -r``). tmux ignores every key it is sent, so the only
  bytes written to it are the terminal emulator's own answers to tmux's
  probes (:func:`backend.web.core.terminal.is_terminal_reply` — a client that
  never answers is one tmux keeps waiting on). A ``-r`` client is also
  ``ignore-size``: it sizes the window while it is the only client, and
  leaves it at the owner's size while the owner's own (writable) client is
  attached — a visitor never reshapes the owner's view.
* **input** — :func:`send_keys`: ``tmux send-keys -l`` into the exact pane
  target (``=name:``), straight to the program in the pane, never through
  tmux's key table. :class:`Typist` delivers it in order, off the event loop.

Mouse reports (the web terminal's wheel scroll is SGR mouse input) are not
keystrokes: typed literally they would land as ``[<64;10;5M`` in the agent's
prompt. :func:`send_keys` routes them the way the owner's root wheel binding
does (:func:`backend.web.core.terminal.apply_scroll_speed`): to the pane's
program when it has the mouse, else the wheel scrolls tmux's copy mode;
clicks are dropped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from typing import Callable, List, Optional

__all__ = ["spawn_readonly_attach", "send_keys", "send_keys_argv", "Typist"]

_log = logging.getLogger("mindflock.terminal")

#: Most UTF-8 bytes one ``send-keys -l`` carries: tmux refuses a command
#: message over ~16 KiB ("command too long") — a long paste is several calls.
MAX_KEYS_BYTES = 8192

# An SGR mouse report (what tmux asks of the browser's terminal), and the
# legacy X10 one (three raw bytes after ESC [ M).
_MOUSE = re.compile(
    rb"\x1b\[<([0-9]{1,4});([0-9]{1,4});([0-9]{1,4})([Mm])|\x1b\[M...", re.S
)


def spawn_readonly_attach(tmux_name: str, rows: int, cols: int):
    """A READ-ONLY tmux client on the session (see the module docstring)."""
    import ptyprocess

    return ptyprocess.PtyProcess.spawn(
        ["tmux", "attach-session", "-r", "-t", "=" + tmux_name],
        dimensions=(rows, cols),
        env={**os.environ, "TERM": "xterm-256color"},
    )


def _split_text(text: str) -> List[str]:
    """``text`` in pieces of at most :data:`MAX_KEYS_BYTES` UTF-8 bytes each,
    cut between characters."""
    if len(text.encode("utf-8", "surrogatepass")) <= MAX_KEYS_BYTES:
        return [text]
    out, cur, size = [], [], 0
    for ch in text:
        n = len(ch.encode("utf-8", "surrogatepass"))
        if size + n > MAX_KEYS_BYTES:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += n
    if cur:
        out.append("".join(cur))
    return out


def send_keys_argv(tmux_name: str, data: bytes) -> List[List[str]]:
    """The ``tmux`` calls that type ``data`` into the session's active pane,
    byte for byte, past tmux's key bindings (``send-keys -l``). argv can't
    carry NUL, which is what Ctrl+Space sends: those go as the key
    ``C-Space``. Mouse reports are not here (:func:`send_keys` routes them)."""
    target = "=" + tmux_name + ":"
    text = data.decode("utf-8", "replace")
    calls = []
    for i, part in enumerate(text.split("\x00")):
        if i:
            calls.append(["tmux", "send-keys", "-t", target, "C-Space"])
        if part:
            for piece in _split_text(part):
                calls.append(["tmux", "send-keys", "-t", target, "-l", "--", piece])
    return calls


def _run(argv: List[str]) -> str:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
        check=False,
    ).stdout


def _scroll_lines() -> int:
    try:
        from backend.web.core.terminal import load_scroll_speed

        return max(1, int(round(load_scroll_speed())))
    except Exception:  # noqa: BLE001
        return 1


def _mouse_argv(
    tmux_name: str, events: List[re.Match], run: Callable[[List[str]], str]
) -> List[List[str]]:
    """What a run of mouse reports does to the pane: the tmux root wheel
    binding's rule (core.terminal.apply_scroll_speed), applied by hand
    because the reports never pass through a writable client."""
    target = "=" + tmux_name + ":"
    flags = run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{mouse_any_flag} #{mouse_sgr_flag} #{pane_in_mode}",
        ]
    ).split()
    if len(flags) != 3:
        return []  # no such pane
    grabbed, sgr, in_mode = (f == "1" for f in flags)
    lines = _scroll_lines()
    calls: List[List[str]] = []
    if grabbed and not in_mode:
        # The program asked for the mouse: it gets the reports, as SGR (the
        # only encoding it can parse straight from here); a wheel notch is
        # forwarded once per scroll line, like the root binding.
        if not sgr:
            return []
        seq = ""
        for m in events:
            if m.group(1) is None:
                continue
            wheel = int(m.group(1)) & 0xC0 == 0x40
            seq += m.group(0).decode("ascii") * (lines if wheel else 1)
        if seq:
            calls.append(["tmux", "send-keys", "-t", target, "-l", "--", seq])
        return calls
    # Nobody has the mouse: the wheel scrolls tmux's copy mode (scroll up
    # enters it, scrolling back past the bottom leaves it — copy-mode -e).
    for m in events:
        if m.group(1) is None or m.group(4) != b"M":
            continue
        btn = int(m.group(1))
        if btn & 0xC0 != 0x40 or btn & 0x03 > 1:
            continue  # a click / drag / release: not ours to make
        up = btn & 0x03 == 0
        if up and not in_mode:
            calls.append(["tmux", "copy-mode", "-e", "-t", target])
            in_mode = True
        if in_mode:
            calls.append(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    target,
                    "-X",
                    "-N",
                    str(lines),
                    "scroll-up" if up else "scroll-down",
                ]
            )
    return calls


def send_keys(
    tmux_name: str, data: bytes, run: Optional[Callable[[List[str]], str]] = None
) -> None:
    """Type ``data`` into the session's active pane (:func:`send_keys_argv`),
    its mouse reports routed (module docstring), all in order. ``run``
    executes one argv (tests pass their own)."""
    run = run or _run
    pos = 0
    events: List[re.Match] = []

    def keys(chunk: bytes) -> None:
        for argv in send_keys_argv(tmux_name, chunk):
            run(argv)

    def mouse() -> None:
        for argv in _mouse_argv(tmux_name, events, run):
            run(argv)
        events.clear()

    for m in _MOUSE.finditer(data):
        if m.start() > pos:
            if events:
                mouse()
            keys(data[pos : m.start()])
        events.append(m)
        pos = m.end()
    if events:
        mouse()
    if pos < len(data):
        keys(data[pos:])


class Typist:
    """One terminal's keystrokes into one pane, delivered in order by
    ``send`` (:func:`send_keys`) in a worker thread: what queued while a
    call ran goes as one call."""

    def __init__(
        self, tmux_name: str, send: Callable[[str, bytes], None] = send_keys
    ) -> None:
        self.tmux_name = tmux_name
        self._send = send
        self._keys: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> "Typist":
        self._task = asyncio.ensure_future(self._run())
        return self

    def type(self, data: bytes) -> None:
        if data:
            self._keys.put_nowait(bytes(data))

    def close(self) -> None:
        self._keys.put_nowait(None)

    async def _run(self) -> None:
        while True:
            data = await self._keys.get()
            if data is None:
                return
            while not self._keys.empty():  # what queued meanwhile: one call
                more = self._keys.get_nowait()
                if more is None:
                    self._keys.put_nowait(None)
                    break
                data += more
            try:
                await asyncio.to_thread(self._send, self.tmux_name, data)
            except Exception as err:  # noqa: BLE001
                _log.warning("keystrokes not delivered: %s", type(err).__name__)
