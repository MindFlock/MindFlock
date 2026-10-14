"""The shared session's screen, for the person on the other end of the link.

The ``screen`` op lets a peer WATCH the session you shared: they see what
its terminal shows right now, colours included, refreshed by polling (the
wire is request/response only — there is no stream to subscribe to). It is
gated by the same permission as ``diff`` ("may see my changes"): the screen
of a sandboxed session working on the shared folder shows nothing the diff
and the files don't.

Two halves, one per side:

* :func:`capture` (the SHARER's side) — ``tmux capture-pane -p -e`` of the
  shared session's agent pane: the visible screen with its SGR escapes, plus
  the pane's size. Only ever the shared session's own pane: the caller names
  it from the link's ``session_title``, never from anything the peer sent.
* :func:`sanitize` (the VIEWER's side) — what arrives is the other machine's
  output, so before it reaches a browser terminal everything but colour/style
  (SGR, ``ESC [ … m``), newlines and tabs is dropped. No OSC (clipboard
  writes, hyperlinks, window titles), no cursor movement, no device queries:
  a peer can colour text in your viewer and do nothing else with it.
"""

from __future__ import annotations

import re
import subprocess
from typing import Optional

__all__ = ["MAX_SCREEN_CHARS", "capture", "sanitize"]

#: A screen is a few KB; this bounds a pathological one (a huge pane full of
#: escapes) well under the wire's 1 MiB frame.
MAX_SCREEN_CHARS = 200_000

_TMUX_TIMEOUT = 10

# One SGR sequence: ESC [ params m (params: digits, ';' and ':' only).
_SGR = re.compile(r"\x1b\[[0-9;:]{0,64}m")
# Any other escape sequence, in the shapes a terminal would act on:
# CSI (ESC [ … final), OSC (ESC ] … BEL or ST), DCS/SOS/PM/APC (ESC P/X/^/_
# … ST), and two-byte escapes (ESC + one char).
_OTHER_ESC = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
    r"|\x1b"
    # The same in 8-bit form: C1 CSI and OSC.
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x9d[^\x07\x9c]*(?:\x07|\x9c)?"
)
# Control characters a viewer must never see raw: C0 except \t and \n, DEL,
# and the C1 range (which some terminals treat as 8-bit escapes).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f-\x9f]")


def sanitize(text: str) -> str:
    """Keep the colours, drop every other escape and control character."""
    if not isinstance(text, str) or not text:
        return ""
    text = text[:MAX_SCREEN_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    out = []
    pos = 0
    for m in _SGR.finditer(text):
        out.append(_strip(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_strip(text[pos:]))
    return "".join(out)


def _strip(chunk: str) -> str:
    return _CONTROL.sub("", _OTHER_ESC.sub("", chunk))


def _tmux(args: list) -> Optional[str]:
    from backend.web.core import proc as _proc

    try:
        cp = _proc.run_capped(
            ["tmux", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TMUX_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — no screen is an answer, not an error
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.decode("utf-8", "replace")


def capture(session_name: str) -> dict:
    """``{"text", "cols", "rows"}`` for tmux session ``session_name``: the
    visible screen with its SGR escapes (``text`` is "" when the pane can't
    be read). Blocking — run it off the event loop."""
    target = "=" + session_name  # exact session match, never a prefix
    text = _tmux(["capture-pane", "-p", "-e", "-t", target]) or ""
    cols = rows = 0
    dims = _tmux(
        ["display-message", "-p", "-t", target, "#{pane_width} #{pane_height}"]
    )
    if dims:
        parts = dims.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            cols, rows = min(int(parts[0]), 1000), min(int(parts[1]), 1000)
    return {"text": text[-MAX_SCREEN_CHARS:], "cols": cols, "rows": rows}
