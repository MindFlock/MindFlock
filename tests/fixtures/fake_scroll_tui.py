"""A stand-in for a mouse-grabbing agent TUI (Claude Code, opencode, cline…)
for the scroll-and-look find tests: it owns its scrollback and scrolls on SGR
wheel reports, with the chrome real ones have — a pinned row on top that
changes as you scroll, an input box and a status bar below, and (while
scrolled) a hint floating over the middle of the content, like Claude Code's
"Jump to bottom" — and all four contain the word the tests search for, so a
hit on any of them is a bug.

    python fake_scroll_tui.py <n_lines> [lpt] [accel] [swallow] [lazy] [max_rows] [keys]

* lpt — lines per wheel report (Claude Code 1, opencode 3);
* accel 1 — reports < 20 ms apart scroll progressively further, the burst
  acceleration real apps do;
* swallow 1 — the first report after a change of direction is ignored
  (Claude Code cancels its scroll momentum with it);
* lazy N — parked at the very bottom, only the last N content lines are
  drawn and the rows above stay blank; any notch up leaves that mode (even
  when everything fits and nothing can scroll), and scrolling back down to
  the bottom re-enters it (Claude Code draws only its recent messages there,
  however tall the window);
* max_rows N — a resize taller than N is ignored, no redraw (Claude Code
  stops honouring heights past 2048);
* keys 1 — PageUp/PageDown page by half the view, Ctrl+Home/Ctrl+End jump to
  the ends, and (with lazy) a jump draws only N lines around where it lands
  until the next scroll, as Claude Code does. Without keys they're ignored.

It follows window resizes (SIGWINCH). Typing "+" appends a line (new
output: ``line N: fresh needle``). Content line i reads ``line i: …``;
every 17th line (and line 3) holds "needle", line 40 holds it twice.
"""

import os
import re
import select
import signal
import sys
import termios
import time
import tty

ARGS = sys.argv[1:] + [""] * 7
N = int(ARGS[0])
LPT = int(ARGS[1] or 1)
ACCEL = ARGS[2] == "1"
SWALLOW = ARGS[3] == "1"
LAZY = int(ARGS[4] or 0)
MAX_ROWS = int(ARGS[5] or 0)
KEYS = ARGS[6] == "1"

LINES = []
for i in range(N):
    if i == 40:
        LINES.append(f"line {i}: needle and needle")
    elif i % 17 == 5 or i == 3:
        LINES.append(f"line {i}: the needle is here")
    elif i % 10 == 0:
        LINES.append(f"## section {i}")
    else:
        LINES.append(f"line {i}: plain text")

cols, rows = os.get_terminal_size()
off = 0  # lines scrolled up from the bottom
lazy_on = True  # parked at the bottom in lazy-drawing mode
lazy_top = False  # just jumped to the top: only the first LAZY lines drawn


def view():
    return max(1, rows - 3)


def draw():
    v = view()
    top = max(0, len(LINES) - v - off)
    section = next(
        (LINES[j] for j in range(top, -1, -1) if LINES[j].startswith("##")), ""
    )
    out = ["\x1b[H\x1b[2J"]
    header = f"pinned needle {section}" if off else LINES[top - 1] if top else ""
    out.append(header[:cols].ljust(cols))
    for r in range(v):
        i = top + r
        text = LINES[i] if i < len(LINES) else ""
        if LAZY and lazy_on and not off and i < len(LINES) - LAZY:
            text = ""
        if LAZY and lazy_top and i >= LAZY:
            text = ""
        text = text[:cols].ljust(cols)
        if off and r == v // 2:
            text = (text[:28] + " [ jump needle ] ").ljust(cols)[:cols]
        out.append("\r\n" + text)
    out.append("\r\n" + ("-" * cols))
    out.append("\r\n" + "> needle typed in the input box"[:cols].ljust(cols))
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def clamp():
    global off
    off = max(0, min(max(0, len(LINES) - view()), off))


def on_winch(*_):
    global cols, rows
    c, r = os.get_terminal_size()
    if MAX_ROWS and r > MAX_ROWS:
        return  # ignored, like Claude Code past 2048 rows
    cols, rows = c, r
    clamp()
    draw()


signal.signal(signal.SIGWINCH, on_winch)
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
sys.stdout.write("\x1b[?1000h\x1b[?1006h\x1b[?25l")
draw()
buf = b""
last = 0.0
streak = 0
last_btn = None
try:
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
        except InterruptedError:
            continue
        if not r:
            continue
        buf += os.read(fd, 65536)
        moved = False
        while b"+" in buf:
            buf = buf.replace(b"+", b"", 1)
            LINES.append(f"line {len(LINES)}: fresh needle")
            moved = True
        while True:
            m = re.search(
                rb"\x1b\[<(\d+);(\d+);(\d+)[Mm]|\x1b\[([56])~|\x1b\[1;5([HF])", buf
            )
            if not m:
                buf = buf[-64:]
                break
            buf = buf[m.end() :]
            if m.group(4) or m.group(5):
                if not KEYS:
                    continue
                if m.group(4):
                    off += (view() // 2) if m.group(4) == b"5" else -(view() // 2)
                    lazy_top = False
                elif m.group(5) == b"H":
                    off = len(LINES)
                    lazy_top = bool(LAZY)
                else:
                    off = 0
                    lazy_top = False
                clamp()
                lazy_on = off == 0
                last_btn = None
                moved = True
                continue
            btn = int(m.group(1))
            if btn not in (64, 65):
                continue
            if SWALLOW and last_btn is not None and btn != last_btn:
                last_btn = btn
                continue
            last_btn = btn
            now = time.monotonic()
            streak = streak + 1 if ACCEL and now - last < 0.02 else 0
            last = now
            step = LPT * (1 + streak // 2)
            off += step if btn == 64 else -step
            clamp()
            if btn == 64:
                lazy_on = False
            elif off == 0:
                lazy_on = True
            lazy_top = False
            moved = True
        if moved:
            draw()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
