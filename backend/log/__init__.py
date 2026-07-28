"""Port of the Go ``log`` package (mindflock/log/log.go).

Provides three module-level loggers (``InfoLog``, ``WarningLog``, ``ErrorLog``)
that write to ``{tempdir}/mindflock.log`` in append mode, plus the ``Every``
throttling helper.

The loggers mimic Go's ``log.Logger`` configured with
``Ldate|Ltime|Lshortfile`` and a level prefix, so a log line looks like::

    INFO:2026/06/18 21:13:00 main.go:10: hello world

Call sites are unchanged from the Go form, e.g.::

    log.InfoLog.Printf("nuked first stdin: %s", buf)
    log.ErrorLog.Print(err)
    log.ErrorLog.Println(msg)
"""

from __future__ import annotations

import datetime
import inspect
import os
import tempfile
import threading
import time as _time
from typing import Optional, TextIO

__all__ = [
    "WarningLog",
    "InfoLog",
    "ErrorLog",
    "Initialize",
    "initialize",
    "Close",
    "close",
    "Every",
    "NewEvery",
    "new_every",
]

# var logFileName = filepath.Join(os.TempDir(), "mindflock.log")
logFileName: str = os.path.join(tempfile.gettempdir(), "mindflock.log")

# var globalLogFile *os.File
globalLogFile: Optional[TextIO] = None


# Size-based rotation (not in the Go original): once the log grows past
# _MAX_LOG_BYTES it is renamed to ``<file>.1`` and a fresh file started, so
# comprehensive request logging can't grow the file unbounded. Overridable via
# MINDFLOCK_LOG_MAX_BYTES (0 disables rotation). Exactly one backup is kept.
def _max_log_bytes() -> int:
    try:
        return int(os.environ.get("MINDFLOCK_LOG_MAX_BYTES", 5 * 1024 * 1024))
    except (TypeError, ValueError):
        return 5 * 1024 * 1024


_MAX_LOG_BYTES: int = _max_log_bytes()
# Bytes in the current file, tracked in-process so rotation never has to stat.
_bytes_written: int = 0
# One lock serialises emit+rotate across threads (the Go logger is synchronised;
# this port previously was not, which could interleave lines / race a rotate).
_LOCK = threading.RLock()


class _Logger:
    """Minimal stand-in for Go's ``*log.Logger`` with ``Ldate|Ltime|Lshortfile``.

    Replicates the exact on-disk line format Go produces, including the level
    prefix written before the date, the ``YYYY/MM/DD HH:MM:SS`` timestamp in
    local time, and the trailing ``file:line:`` reference of the caller.
    """

    def __init__(self, out: TextIO, prefix: str) -> None:
        self._out = out
        self._prefix = prefix

    # --- Go formatting -----------------------------------------------------
    def _header(self) -> str:
        now = datetime.datetime.now()
        date = now.strftime("%Y/%m/%d")
        # Go's Ltime is HH:MM:SS (no fractional seconds unless Lmicroseconds).
        clock = now.strftime("%H:%M:%S")

        # Lshortfile: final path element of the caller's file + line number.
        filename = "???"
        line = 0
        frame = inspect.currentframe()
        try:
            # Walk up out of this module to the first frame whose file is not
            # this one. That is the real call site (Print/Printf/Println are
            # also defined here, so we skip every in-module frame).
            this_file = __file__
            target = frame
            while target is not None and target.f_code.co_filename == this_file:
                target = target.f_back
            if target is not None:
                filename = os.path.basename(target.f_code.co_filename)
                line = target.f_lineno
        finally:
            del frame

        # prefix + date + " " + time + " " + file:line + ": "
        return "{prefix}{date} {clock} {file}:{line}: ".format(
            prefix=self._prefix,
            date=date,
            clock=clock,
            file=filename,
            line=line,
        )

    def _emit(self, text: str) -> None:
        # Go's Output() appends a newline only if the message lacks one.
        if not text.endswith("\n"):
            text = text + "\n"
        line = self._header() + text
        # Serialise write + rotate so concurrent loggers can't interleave a line
        # or race the rename. Writing to the module-level file (not self._out)
        # keeps every logger pointed at the current file after a rotation.
        with _LOCK:
            global _bytes_written
            out = globalLogFile if globalLogFile is not None else self._out
            if out is None:
                return
            nbytes = len(line.encode("utf-8", "replace"))
            if (
                _MAX_LOG_BYTES > 0
                and globalLogFile is not None
                and _bytes_written + nbytes > _MAX_LOG_BYTES
            ):
                rotated = _rotate()
                if rotated is not None:
                    out = rotated
            try:
                out.write(line)
                out.flush()
                _bytes_written += nbytes
            except (ValueError, OSError):
                # Match Go's tolerance: a closed/broken file does not crash callers.
                pass

    # --- Go's *log.Logger API ---------------------------------------------
    def Printf(self, format: str, *args: object) -> None:
        try:
            msg = format % args if args else format
        except (TypeError, ValueError):
            # Be forgiving like a best-effort port; fall back to a joined form.
            msg = format if not args else format + " " + " ".join(str(a) for a in args)
        self._emit(msg)

    def Print(self, *args: object) -> None:
        # Go's Print uses fmt.Sprint: operands concatenated, with a space added
        # between two operands only when neither is a string.
        self._emit(_go_sprint(args))

    def Println(self, *args: object) -> None:
        # Go's Println uses fmt.Sprintln: spaces between all operands, newline.
        self._emit(" ".join(str(a) for a in args))


def _go_sprint(args: tuple) -> str:
    """Approximate Go's fmt.Sprint spacing rule.

    fmt.Sprint adds a space between two operands only when neither is a
    string. The common single-argument case (``Print(err)``) is exact.
    """
    parts = []
    prev_was_string = None
    for a in args:
        is_string = isinstance(a, str)
        if prev_was_string is not None and not prev_was_string and not is_string:
            parts.append(" ")
        parts.append(str(a))
        prev_was_string = is_string
    return "".join(parts)


# Module-level loggers. Start as None (Go's nil) until Initialize() is called.
WarningLog: Optional[_Logger] = None
InfoLog: Optional[_Logger] = None
ErrorLog: Optional[_Logger] = None


def _rotate() -> Optional[TextIO]:
    """Close the current log, move it to ``<file>.1`` (keeping one backup) and
    open a fresh file. Repoints all three level loggers at the new handle.
    Returns the new file object, or None if it could not be reopened. Callers
    must hold ``_LOCK``."""
    global globalLogFile, _bytes_written
    try:
        if globalLogFile is not None:
            globalLogFile.close()
    except (OSError, ValueError):
        pass
    try:
        if os.path.exists(logFileName):
            os.replace(logFileName, logFileName + ".1")  # overwrites any old .1
    except OSError:
        pass  # couldn't rotate — reopen and keep appending rather than lose logs
    try:
        fd = os.open(logFileName, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o666)
        f = os.fdopen(fd, "a", encoding="utf-8")
    except OSError:
        globalLogFile = None
        _bytes_written = 0
        return None
    globalLogFile = f
    _bytes_written = 0
    for lg in (InfoLog, WarningLog, ErrorLog):
        if lg is not None:
            lg._out = f
    return f


def Initialize(daemon: bool) -> None:
    """Set up logging.

    Opens the log file (``O_CREATE|O_WRONLY|O_APPEND``, mode 0666) and creates
    the three level loggers. ``Close()`` should follow (deferred in Go). If the
    file cannot be opened a ``RuntimeError`` is raised (Go panics).
    """
    global WarningLog, InfoLog, ErrorLog, globalLogFile, _bytes_written

    try:
        fd = os.open(
            logFileName,
            os.O_CREAT | os.O_WRONLY | os.O_APPEND,
            0o666,
        )
        f = os.fdopen(fd, "a", encoding="utf-8")
    except OSError as err:
        raise RuntimeError("could not open log file: {}".format(err))

    # Seed the byte counter from the existing file so rotation accounts for
    # what's already there (the file is opened in append mode).
    try:
        _bytes_written = os.path.getsize(logFileName)
    except OSError:
        _bytes_written = 0

    fmt_s = "%s"
    if daemon:
        fmt_s = "[DAEMON] %s"

    InfoLog = _Logger(f, fmt_s % "INFO:")
    WarningLog = _Logger(f, fmt_s % "WARNING:")
    ErrorLog = _Logger(f, fmt_s % "ERROR:")

    globalLogFile = f


def Close() -> None:
    """Close the log file and print the log path to stdout."""
    global globalLogFile
    if globalLogFile is not None:
        try:
            globalLogFile.close()
        except (OSError, ValueError):
            # Go ignores the close error (`_ = globalLogFile.Close()`).
            pass
    # TODO: maybe only print if verbose flag is set?
    # fmt.Println adds a trailing newline.
    print("wrote logs to " + logFileName)


class Every:
    """Logs at most once every ``timeout`` (seconds, float)."""

    def __init__(self, timeout: float) -> None:
        # Go stores a time.Duration; we keep seconds as a float.
        self.timeout: float = timeout
        # Tracks the deadline (monotonic seconds) after which ShouldLog fires.
        # None mirrors Go's nil timer (first-call branch).
        self._deadline: Optional[float] = None

    def ShouldLog(self) -> bool:
        """Return True if the timeout has elapsed since the last True.

        Mirrors the Go state machine: the first call always returns True and
        arms the timer; subsequent calls return True only once the timer has
        fired, after which it is re-armed.
        """
        now = _time.monotonic()
        if self._deadline is None:
            # First call: create + reset timer, return True.
            self._deadline = now + self.timeout
            return True

        if now >= self._deadline:
            # Timer fired: reset and report.
            self._deadline = now + self.timeout
            return True
        return False


def NewEvery(timeout: float) -> Every:
    """Construct an :class:`Every` throttle with the given timeout (seconds)."""
    return Every(timeout)


# snake_case aliases for Pythonic call sites.
initialize = Initialize
close = Close
new_every = NewEvery
