"""Platform-specific window-size monitoring for tmux sessions.

Merges the two Go build-tagged files behind a single ``os.name`` guard:

- ``tmux_unix.go`` (``//go:build !windows``): installs a ``SIGWINCH`` handler,
  debounces resize signals by 50ms, and resizes the PTY. Errors are throttled
  to once per 60s via ``log.NewEvery``.
- ``tmux_windows.go`` (``//go:build windows``): SIGWINCH is unavailable, so it
  polls the terminal size every 250ms and resizes only when it changes. Errors
  are logged every time (no throttle).

Both implementations resize via ``TmuxSession.update_window_size`` and respect
``t.ctx`` cancellation (modelled here with a :class:`threading.Event`). The
public entry point is :func:`monitor_window_size`, called from
``TmuxSession.attach`` exactly where Go calls ``t.monitorWindowSize()``.
"""

from __future__ import annotations

import os
import signal
import struct
import threading

from backend import log


def _get_terminal_size():
    """Return ``(cols, rows, err)`` mirroring ``term.GetSize(int(os.Stdin.Fd()))``.

    Go's ``term.GetSize`` returns ``(width, height, err)`` i.e. ``(cols, rows)``.
    We query ``TIOCGWINSZ`` on stdin and unpack the kernel ``winsize`` struct
    (row, col, xpixel, ypixel), returning them in the same (cols, rows) order.
    """
    import fcntl
    import termios

    try:
        buf = fcntl.ioctl(0, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _x, _y = struct.unpack("HHHH", buf)
        return cols, rows, None
    except Exception as e:  # noqa: BLE001 - mirror Go error return
        return 0, 0, e


def monitor_window_size(t) -> None:
    """Monitor and handle window resize events while attached.

    Dispatches to the Unix (SIGWINCH + debounce) or Windows (250ms poll)
    implementation based on the running platform, matching the Go build tags.
    ``t`` is the :class:`~backend.session.tmux.tmux.TmuxSession`.
    """
    if os.name == "nt":
        _monitor_window_size_windows(t)
    else:
        _monitor_window_size_unix(t)


# ---------------------------------------------------------------------------
# Unix (port of tmux_unix.go)
# ---------------------------------------------------------------------------
def _monitor_window_size_unix(t) -> None:
    every_n = log.NewEvery(60.0)

    def do_update() -> None:
        cols, rows, err = _get_terminal_size()
        if err is not None:
            if every_n.ShouldLog() and log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to update window size: %v", err)
        else:
            uerr = t.update_window_size(cols, rows)
            if uerr is not None:
                if every_n.ShouldLog() and log.ErrorLog is not None:
                    log.ErrorLog.Printf("failed to update window size: %v", uerr)

    # winchChan: buffered channel of signals -> a threading.Event we set from
    # the SIGWINCH handler. debounced -> a second event the debounce thread sets.
    winch_event = threading.Event()
    debounced_event = threading.Event()

    # signal.signal must run on the main thread; guard against being called
    # from a worker thread (Python raises ValueError otherwise).
    prev_handler = None
    installed = False

    def _winch_handler(signum, frame):
        winch_event.set()

    try:
        prev_handler = signal.signal(signal.SIGWINCH, _winch_handler)
        installed = True
        # Send initial SIGWINCH to trigger the first resize, mirroring
        # syscall.Kill(syscall.Getpid(), syscall.SIGWINCH).
        os.kill(os.getpid(), signal.SIGWINCH)
    except (ValueError, OSError, AttributeError):
        # Not on main thread or SIGWINCH unavailable: fall back to a single
        # initial update plus the poll-based path so resizing still works.
        installed = False

    if not installed:
        # No signal handling available: do the deferred initial update and a
        # lightweight poll loop so behaviour degrades gracefully.
        _poll_loop(t, do_update, interval=0.25)
        do_update()
        return

    # Debounce goroutine: on each SIGWINCH, (re)arm a 50ms timer that, when it
    # fires, signals the resize handler. ctx cancellation stops the loop.
    def _debounce() -> None:
        try:
            resize_timer = None
            while not t._ctx_done():
                # Wait for a winch with a small timeout so we can observe ctx.
                if winch_event.wait(0.05):
                    winch_event.clear()
                    if resize_timer is not None:
                        resize_timer.cancel()

                    def _fire():
                        if not t._ctx_done():
                            debounced_event.set()

                    resize_timer = threading.Timer(0.05, _fire)
                    resize_timer.daemon = True
                    resize_timer.start()
        finally:
            t._wg_done()

    # Resize handler goroutine: on each debounced signal, do an update.
    def _handler() -> None:
        try:
            while not t._ctx_done():
                if debounced_event.wait(0.05):
                    debounced_event.clear()
                    do_update()
        finally:
            # signal.Stop(winchChan): restore the previous handler.
            try:
                if prev_handler is not None:
                    signal.signal(signal.SIGWINCH, prev_handler)
            except (ValueError, OSError):
                pass
            t._wg_done()

    # wg.Add(2) for the two goroutines (Go: t.wg.Add(2)).
    t._wg_add(2)
    th1 = threading.Thread(target=_debounce, daemon=True)
    th2 = threading.Thread(target=_handler, daemon=True)
    th1.start()
    th2.start()

    # defer doUpdate(): set the initial size after wiring up the goroutines.
    do_update()


# ---------------------------------------------------------------------------
# Windows (port of tmux_windows.go)
# ---------------------------------------------------------------------------
def _monitor_window_size_windows(t) -> None:
    def do_update() -> None:
        cols, rows, err = _get_terminal_size()
        if err is not None:
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to update window size: %v", err)
        else:
            uerr = t.update_window_size(cols, rows)
            if uerr is not None and log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to update window size: %v", uerr)

    # Do one at the start to set the initial size.
    do_update()

    last_cols, last_rows, _ = _get_terminal_size()

    def _poll() -> None:
        nonlocal last_cols, last_rows
        try:
            while not t._ctx_done():
                # Ticker: 250ms. We sleep in small slices to observe ctx.
                woke = t._ctx_wait(0.25)
                if woke:
                    return
                cols, rows, err = _get_terminal_size()
                if err is not None:
                    continue
                if cols != last_cols or rows != last_rows:
                    last_cols, last_rows = cols, rows
                    do_update()
        finally:
            t._wg_done()

    t._wg_add(1)
    th = threading.Thread(target=_poll, daemon=True)
    th.start()


def _poll_loop(t, do_update, interval: float) -> None:
    """Fallback poll loop used when SIGWINCH cannot be installed (non-main thread).

    Mirrors the Windows poll structure so resizing still works under threads
    that cannot register signal handlers.
    """
    last_cols, last_rows, _ = _get_terminal_size()

    def _poll() -> None:
        nonlocal last_cols, last_rows
        try:
            while not t._ctx_done():
                woke = t._ctx_wait(interval)
                if woke:
                    return
                cols, rows, err = _get_terminal_size()
                if err is not None:
                    continue
                if cols != last_cols or rows != last_rows:
                    last_cols, last_rows = cols, rows
                    do_update()
        finally:
            t._wg_done()

    t._wg_add(1)
    th = threading.Thread(target=_poll, daemon=True)
    th.start()


# snake_case is already the public name; provide Go-cased alias too.
monitorWindowSize = monitor_window_size


__all__ = ["monitor_window_size", "monitorWindowSize"]
