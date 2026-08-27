"""Port of the Go ``session/tmux/tmux.go``.

Manages the lifecycle of a tmux session that runs an AI program (claude,
aider). Faithfully mirrors the Go ``TmuxSession`` type: byte-exact tmux argv,
session-name sanitization, trust-prompt handling, pane capture, PTY keystrokes,
attach/detach concurrency and cleanup, plus the package-level ``CleanupSessions``.

Dependency mapping:
- ``mindflock/cmd`` (Executor / exec.Command / ToString) -> :mod:`backend.cmd`.
- ``mindflock/log`` (InfoLog/ErrorLog/NewEvery) -> :mod:`backend.log`.
- ``github.com/creack/pty`` (Start / Setsize / Winsize) -> :mod:`.pty`.
- Go goroutines + context.Context + sync.WaitGroup -> :mod:`threading`
  (:class:`threading.Thread`, an :class:`threading.Event` for ctx, and a
  counting condition for the WaitGroup).
- ``crypto/sha256`` -> :mod:`hashlib`.

The Python convention from :mod:`backend.cmd` is preserved: ``Executor``
methods *return* an error object (or ``None``), so call sites read
``self._cmd_exec.run(c) is None`` exactly as Go reads ``Run(cmd) == nil``.
"""

from __future__ import annotations

import hashlib
import os
import re
import select
import shlex
import sys
import threading
import time
from typing import List, Optional, Tuple

from backend import cmd as cmd_pkg
from backend.session import secret_env
from backend import log

from . import pty as pty_pkg
from .platform import monitor_window_size

ProgramClaude = "claude"
ProgramAider = "aider"

TmuxPrefix = "mindflock_"

# Any run of whitespace; collapsed away entirely when sanitizing a session name.
_whitespace_regex = re.compile(r"\s+")


def to_mindflock_tmux_name(s: str) -> str:
    """Sanitize ``s`` into a tmux session name.

    1. Collapse every run of whitespace (``\\s+``) to the empty string.
    2. Replace ``.`` with ``_`` (tmux itself replaces all ``.`` with ``_``).
    3. Prepend ``mindflock_``.

    Example: ``"a sd f . . asdf"`` -> ``"mindflock_asdf__asdf"``.
    """
    s = _whitespace_regex.sub("", s)
    s = s.replace(".", "_")
    return "{}{}".format(TmuxPrefix, s)


class _StatusMonitor:
    """Tracks pane-content hash to detect changes (Go ``statusMonitor``)."""

    __slots__ = ("prev_output_hash",)

    def __init__(self) -> None:
        # nil []byte in Go.
        self.prev_output_hash: Optional[bytes] = None

    def hash(self, s: str) -> bytes:
        """SHA256 of ``s`` (mirrors ``(*statusMonitor).hash``)."""
        h = hashlib.sha256()
        h.update(s.encode("utf-8"))
        return h.digest()


def _new_status_monitor() -> _StatusMonitor:
    return _StatusMonitor()


class TmuxSession:
    """Managed tmux session (Go ``TmuxSession``).

    Constructed via :func:`NewTmuxSession` (production deps) or
    :func:`NewTmuxSessionWithDeps` / :func:`new_tmux_session` (injected deps for
    testing). After :meth:`start` succeeds, ``ptmx`` is a valid PTY handle.
    """

    def __init__(
        self,
        sanitized_name: str,
        program: str,
        pty_factory: pty_pkg.PtyFactory,
        cmd_exec: cmd_pkg.Executor,
    ) -> None:
        # Initialized by NewTmuxSession.
        self.sanitized_name = sanitized_name
        self.program = program
        # Optional override for the command tmux actually launches. When set,
        # ``start`` runs this instead of ``program`` — while ``program`` stays
        # the classification string (e.g. "claude") used by has_updated() /
        # check_and_handle_trust_prompt(). Used by provisioned mode to launch via
        # a wrapper script without breaking claude detection.
        self.launch_command: Optional[str] = None
        # Extra env vars exported to the launched program (O4 port block).
        # Applied by ``start`` via an ``env(1)`` prefix on the launch command
        # (tmux runs it through the shell) plus ``tmux set-environment`` so
        # later panes/windows in the session inherit the same values.
        self.extra_env: dict = {}
        self._pty_factory = pty_factory
        self._cmd_exec = cmd_exec

        # Initialized by Start or Restore.
        self.ptmx = None  # PtyFile (never None after Start succeeds)
        self._monitor: Optional[_StatusMonitor] = None

        # Initialized by Attach; deinitialized by Detach.
        self._attach_ch: Optional[threading.Event] = None
        # ctx / cancel modelled with a single cancellation Event.
        self._ctx: Optional[threading.Event] = None
        self._cancel = None  # callable
        # WaitGroup modelled with a counter guarded by a condition.
        self._wg_lock = threading.Lock()
        self._wg_cond = threading.Condition(self._wg_lock)
        self._wg_count = 0
        self._wg_active = False  # whether a WaitGroup is in use this attach

    # --- WaitGroup / ctx helpers (used by platform.py and attach/detach) ----
    def _wg_add(self, n: int) -> None:
        with self._wg_cond:
            self._wg_count += n

    def _wg_done(self) -> None:
        with self._wg_cond:
            self._wg_count -= 1
            if self._wg_count <= 0:
                self._wg_cond.notify_all()

    def _wg_wait(self) -> None:
        with self._wg_cond:
            while self._wg_count > 0:
                self._wg_cond.wait()

    def _ctx_done(self) -> bool:
        return self._ctx is None or self._ctx.is_set()

    def _ctx_wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for ctx cancellation; True if cancelled."""
        if self._ctx is None:
            time.sleep(timeout)
            return False
        return self._ctx.wait(timeout)

    # --- Start --------------------------------------------------------------
    def start(self, work_dir: str) -> Optional[Exception]:
        """Create, start, and attach to a new tmux session (mirrors ``Start``)."""
        # Check if the session already exists.
        if self.does_session_exist():
            return Exception(
                "tmux session already exists: {}".format(self.sanitized_name)
            )

        # Create a new detached tmux session and start the program in it.
        # ``launch_command`` (if set) overrides the literal command run, while
        # ``program`` remains the classification string used elsewhere.
        launch = self.launch_command or self.program
        if self.extra_env:
            # Credentials never ride argv. `env KEY=… cmd` puts the value in
            # the child's /proc/<pid>/cmdline for the whole life of the session
            # and in this tmux client's while it runs, so any other local user
            # can read it out of `ps`. Those values go to a 0600 file the shell
            # sources; only its PATH is visible. Everything else (the port
            # block, cache tags, the profile id) keeps the env(1) prefix —
            # unchanged, and byte-identical for a session with no credentials.
            plain, secret = secret_env.split(self.extra_env)
            if plain:
                # tmux runs the command string through the shell, so an env(1)
                # prefix reaches the program; values are shell-quoted. The
                # session env is ALSO set below (post-create) for future
                # panes/windows.
                pairs = " ".join(
                    "%s=%s" % (k, shlex.quote(str(v))) for k, v in sorted(plain.items())
                )
                launch = "env %s %s" % (pairs, launch)
            secret_path = secret_env.write(self.sanitized_name, secret)
            if secret_path:
                launch = secret_env.source_prefix(secret_path) + launch
        c = cmd_pkg.command(
            "tmux",
            "new-session",
            "-d",
            "-s",
            self.sanitized_name,
            "-c",
            work_dir,
            launch,
        )

        ptmx, err = self._pty_factory.start(c)
        if err is not None:
            # Cleanup any partially created session if any exists.
            if self.does_session_exist():
                cleanup_cmd = cmd_pkg.command(
                    "tmux", "kill-session", "-t", self.sanitized_name
                )
                cleanup_err = self._cmd_exec.run(cleanup_cmd)
                if cleanup_err is not None:
                    err = Exception("{} (cleanup error: {})".format(err, cleanup_err))
            return Exception("error starting tmux session: {}".format(err))

        # Poll for session existence with exponential backoff.
        deadline = time.monotonic() + 2.0  # 2 * time.Second
        sleep_duration = 0.005  # 5 * time.Millisecond
        while not self.does_session_exist():
            if time.monotonic() >= deadline:
                cleanup_err = self.close()
                if cleanup_err is not None:
                    err = Exception("{} (cleanup error: {})".format(err, cleanup_err))
                return Exception(
                    "timed out waiting for tmux session {}: {}".format(
                        self.sanitized_name, err
                    )
                )
            time.sleep(sleep_duration)
            # Exponential backoff up to 50ms max.
            if sleep_duration < 0.050:
                sleep_duration *= 2

        ptmx.close()

        # Session-scoped env for panes/windows created later (the initial
        # program already got the values via the env(1) prefix above).
        # Secrets are deliberately excluded: `tmux set-environment` would put
        # the value in this client's argv, and a shell pane opened later has no
        # business holding the agent's API key anyway.
        for k, v in sorted(secret_env.split(self.extra_env)[0].items()):
            eerr = self._cmd_exec.run(
                cmd_pkg.command(
                    "tmux",
                    "set-environment",
                    "-t",
                    self.sanitized_name,
                    str(k),
                    str(v),
                )
            )
            if eerr is not None and log.InfoLog is not None:
                log.InfoLog.Printf(
                    "Warning: failed to set %s for session %s: %v",
                    k,
                    self.sanitized_name,
                    eerr,
                )

        # Set history limit to enable scrollback (hardcoded 10000).
        history_cmd = cmd_pkg.command(
            "tmux", "set-option", "-t", self.sanitized_name, "history-limit", "10000"
        )
        herr = self._cmd_exec.run(history_cmd)
        if herr is not None and log.InfoLog is not None:
            log.InfoLog.Printf(
                "Warning: failed to set history-limit for session %s: %v",
                self.sanitized_name,
                herr,
            )

        # Enable mouse scrolling for the session.
        mouse_cmd = cmd_pkg.command(
            "tmux", "set-option", "-t", self.sanitized_name, "mouse", "on"
        )
        merr = self._cmd_exec.run(mouse_cmd)
        if merr is not None and log.InfoLog is not None:
            log.InfoLog.Printf(
                "Warning: failed to enable mouse scrolling for session %s: %v",
                self.sanitized_name,
                merr,
            )

        rerr = self.restore()
        if rerr is not None:
            cleanup_err = self.close()
            if cleanup_err is not None:
                rerr = Exception("{} (cleanup error: {})".format(rerr, cleanup_err))
            return Exception("error restoring tmux session: {}".format(rerr))

        return None

    # --- Trust prompt -------------------------------------------------------
    def check_and_handle_trust_prompt(self) -> bool:
        """Dismiss a trust prompt if present (mirrors ``CheckAndHandleTrustPrompt``).

        The trust patterns + the keystroke to dismiss them now come from the
        session's coding provider, so a new CLI declares its own gate instead of
        this code hardcoding per-program English strings. (Claude: its per-folder
        trust + MCP prompts dismissed with Enter; others: an "Open documentation
        url" gate dismissed with 'D' then Enter — the generic default.)
        """
        content, err = self.capture_pane_content()
        if err is not None:
            return False

        from backend import providers  # lazy: avoid import cycle

        spec = providers.resolve(self.program).trust_prompt()
        if spec and any(p in content for p in spec.patterns):
            try:
                self.ptmx.write(spec.keystroke)
            except Exception as e:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("could not dismiss trust/MCP screen: %v", e)
            return True
        return False

    # --- Restore ------------------------------------------------------------
    def restore(self) -> Optional[Exception]:
        """Attach to an existing session and reset the monitor (mirrors ``Restore``)."""
        ptmx, err = self._pty_factory.start(
            cmd_pkg.command("tmux", "attach-session", "-t", self.sanitized_name)
        )
        if err is not None:
            return Exception("error opening PTY: {}".format(err))
        self.ptmx = ptmx
        self._monitor = _new_status_monitor()
        return None

    # --- Keystrokes ---------------------------------------------------------
    def tap_enter(self) -> Optional[Exception]:
        """Send a single Enter (0x0D) keystroke (mirrors ``TapEnter``)."""
        try:
            self.ptmx.write(bytes([0x0D]))
        except Exception as e:  # noqa: BLE001
            return Exception("error sending enter keystroke to PTY: {}".format(e))
        return None

    def send_keys(self, keys: str) -> Optional[Exception]:
        """Write ``keys`` to the PTY unchanged (mirrors ``SendKeys``; no wrap)."""
        try:
            self.ptmx.write(keys.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return e
        return None

    # --- Change detection ---------------------------------------------------
    def has_updated(self) -> Tuple[bool, bool]:
        """Detect pane change + prompt (mirrors ``HasUpdated``).

        Returns ``(updated, has_prompt)``.
        """
        content, err = self.capture_pane_content()
        if err is not None:
            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "error capturing pane content in status monitor: %v", err
                )
            return False, False

        # The "is the agent waiting at a prompt?" pattern comes from the
        # session's coding provider (claude / aider / a config-defined
        # CLI), replacing the hardcoded per-program English strings.
        from backend import providers  # lazy: avoid import cycle

        pat = providers.resolve(self.program).idle_prompt_pattern()
        has_prompt = bool(pat) and (pat in content)

        new_hash = self._monitor.hash(content)
        if new_hash != self._monitor.prev_output_hash:
            self._monitor.prev_output_hash = self._monitor.hash(content)
            return True, has_prompt
        return False, has_prompt

    # --- Attach -------------------------------------------------------------
    def attach(self) -> Tuple[threading.Event, Optional[Exception]]:
        """Attach: spawn I/O + resize threads, return (attach_ch, None).

        Mirrors ``Attach``: an attach-channel (Event) is returned immediately;
        the goroutines (threads) run in the background until :meth:`detach`.
        """
        self._attach_ch = threading.Event()

        self._wg_active = True
        with self._wg_cond:
            self._wg_count = 0
        # One slot for _copy_out, one for _read_in — detach waits for BOTH, so
        # restore() can never rebind ptmx while a stale reader might still
        # write into it.
        self._wg_add(2)

        # context.WithCancel(context.Background())
        self._ctx = threading.Event()

        def _cancel():
            if self._ctx is not None:
                self._ctx.set()

        self._cancel = _cancel

        # Goroutine 1: copy PTY -> stdout. Exits when the PTY closes (EOF).
        def _copy_out():
            try:
                while True:
                    try:
                        data = self.ptmx.read(4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    try:
                        buf = sys.stdout.buffer
                        buf.write(data)
                        buf.flush()
                    except Exception:  # noqa: BLE001
                        break
                # When the copy returns the connection was closed. If ctx is not
                # done, it was an abnormal termination (Ctrl-D): warn in red.
                if not self._ctx_done():
                    try:
                        sys.stderr.write(
                            "\n\033[31mError: Session terminated without "
                            "detaching. Use Ctrl-Q to properly detach from "
                            "tmux sessions.\033[0m\n"
                        )
                        sys.stderr.flush()
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                self._wg_done()

        # Goroutine 2: read stdin; nuke first ~50ms of control sequences; on
        # Ctrl-Q (0x11) detach; otherwise forward to the PTY. Registered in
        # the WaitGroup and cancelled via ctx so it exits promptly on detach
        # (a bare blocking os.read would survive the detach and write its next
        # keystroke into the NEW attachment after restore() rebinds ptmx).
        def _read_in():
            # Capture THIS attach's PTY + ctx: a late write must go to the old
            # (closed) handle — never to a newer attachment.
            ptmx = self.ptmx
            ctx = self._ctx
            timeout_deadline = time.monotonic() + 0.050  # 50ms window
            detach_requested = False
            try:
                # Read the raw stdin fd directly (os.read) so each keystroke is
                # forwarded as soon as it is typed. sys.stdin.buffer.read(32) is a
                # buffered read that blocks until 32 bytes/EOF, which would stall
                # interactive input (arrows, single chars, the Ctrl-Q detach).
                try:
                    in_fd = sys.stdin.fileno()
                except Exception:  # noqa: BLE001
                    return
                while not (ctx is None or ctx.is_set()):
                    # Poll with a timeout so ctx cancellation is noticed even
                    # when no input arrives.
                    try:
                        ready, _, _ = select.select([in_fd], [], [], 0.5)
                    except (OSError, ValueError):
                        break
                    if not ready:
                        continue
                    try:
                        buf = os.read(in_fd, 32)
                    except OSError:
                        # A persistently-erroring fd (EIO on a disconnected
                        # PTY) would spin a tight loop — treat as EOF.
                        break
                    if len(buf) == 0:
                        # EOF.
                        break
                    nr = len(buf)

                    # First 50ms: nuke (ignore) the bytes; log them. After the
                    # window, forward them.
                    if time.monotonic() < timeout_deadline:
                        if log.InfoLog is not None:
                            log.InfoLog.Printf("nuked first stdin: %s", buf[:nr])
                        continue

                    # Check for Ctrl+q (ASCII 17).
                    if nr == 1 and buf[0] == 17:
                        detach_requested = True
                        return

                    # Forward other input to tmux (the captured handle only).
                    try:
                        ptmx.write(buf[:nr])
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                # Leave the WaitGroup BEFORE detaching — detach() waits on the
                # WaitGroup, so doing it the other way round would deadlock on
                # our own exit.
                self._wg_done()
                if detach_requested:
                    self.detach()

        t1 = threading.Thread(target=_copy_out, daemon=True)
        t2 = threading.Thread(target=_read_in, daemon=True)
        t1.start()
        t2.start()

        # Goroutine 3: window size monitor (platform-specific).
        monitor_window_size(self)
        return self._attach_ch, None

    # --- DetachSafely -------------------------------------------------------
    def detach_safely(self) -> Optional[Exception]:
        """Non-panicking detach (mirrors ``DetachSafely``)."""
        if self._attach_ch is None:
            return None  # Already detached.

        errs: List[Exception] = []

        # Close the attached pty session.
        if self.ptmx is not None:
            try:
                self.ptmx.close()
            except Exception as e:  # noqa: BLE001
                errs.append(Exception("error closing attach pty session: {}".format(e)))
            self.ptmx = None

        # Clean up attach state.
        if self._attach_ch is not None:
            self._attach_ch.set()
            self._attach_ch = None

        if self._cancel is not None:
            self._cancel()
            self._cancel = None

        if self._wg_active:
            self._wg_wait()
            self._wg_active = False

        self._ctx = None

        if errs:
            return Exception("errors during detach: {}".format(_format_errs(errs)))
        return None

    # --- Detach -------------------------------------------------------------
    def detach(self) -> None:
        """Detach; panics on fatal errors (mirrors ``Detach``)."""
        try:
            # Close the attached pty session.
            try:
                self.ptmx.close()
            except Exception as e:  # noqa: BLE001
                msg = "error closing attach pty session: {}".format(e)
                if log.ErrorLog is not None:
                    log.ErrorLog.Println(msg)
                raise RuntimeError(msg)

            # Attach threads should die on EOF due to the ptmx closing.
            # Restore to set a new t.ptmx.
            rerr = self.restore()
            if rerr is not None:
                msg = "error closing attach pty session: {}".format(rerr)
                if log.ErrorLog is not None:
                    log.ErrorLog.Println(msg)
                raise RuntimeError(msg)

            # Cancel goroutines created by Attach.
            if self._cancel is not None:
                self._cancel()
            if self._wg_active:
                self._wg_wait()
        finally:
            # defer: close attachCh and nil everything out.
            if self._attach_ch is not None:
                self._attach_ch.set()
            self._attach_ch = None
            self._cancel = None
            self._ctx = None
            self._wg_active = False

    # --- Close --------------------------------------------------------------
    def close(self) -> Optional[Exception]:
        """Kill the session and free resources (mirrors ``Close``)."""
        errs: List[Exception] = []

        if self.ptmx is not None:
            try:
                self.ptmx.close()
            except Exception as e:  # noqa: BLE001
                errs.append(Exception("error closing PTY: {}".format(e)))
            self.ptmx = None

        c = cmd_pkg.command("tmux", "kill-session", "-t", self.sanitized_name)
        kerr = self._cmd_exec.run(c)
        if kerr is not None:
            errs.append(Exception("error killing tmux session: {}".format(kerr)))

        # The session is gone; its credential file has nothing left to serve.
        secret_env.clear(self.sanitized_name)

        if len(errs) == 0:
            return None
        if len(errs) == 1:
            return errs[0]

        err_msg = "multiple errors occurred during cleanup:"
        for e in errs:
            err_msg += "\n  - " + str(e)
        return Exception(err_msg)

    # --- Sizing -------------------------------------------------------------
    def set_detached_size(self, width: int, height: int) -> Optional[Exception]:
        """Resize while detached (mirrors ``SetDetachedSize``)."""
        return self.update_window_size(width, height)

    def update_window_size(self, cols: int, rows: int) -> Optional[Exception]:
        """Resize the PTY (mirrors ``updateWindowSize``).

        Calls ``pty.Setsize(t.ptmx, &pty.Winsize{Rows, Cols, X:0, Y:0})`` with
        the creack/pty field order preserved.
        """
        return pty_pkg.set_size(
            self.ptmx,
            pty_pkg.Winsize(rows=rows & 0xFFFF, cols=cols & 0xFFFF, x=0, y=0),
        )

    # --- Existence / capture ------------------------------------------------
    def does_session_exist(self) -> bool:
        """Exact-match existence check (mirrors ``DoesSessionExist``).

        Uses ``-t=<name>`` (with the ``=``) for an exact match.
        """
        exists_cmd = cmd_pkg.command(
            "tmux", "has-session", "-t={}".format(self.sanitized_name)
        )
        return self._cmd_exec.run(exists_cmd) is None

    def capture_pane_content(self) -> Tuple[str, Optional[Exception]]:
        """Capture the pane content (mirrors ``CapturePaneContent``)."""
        c = cmd_pkg.command(
            "tmux", "capture-pane", "-p", "-e", "-J", "-t", self.sanitized_name
        )
        output, err = self._cmd_exec.output(c)
        if err is not None:
            return "", Exception("error capturing pane content: {}".format(err))
        return _to_str(output), None

    def capture_pane_content_with_options(
        self, start: str, end: str
    ) -> Tuple[str, Optional[Exception]]:
        """Capture a pane range (mirrors ``CapturePaneContentWithOptions``).

        ``start``/``end`` are passed verbatim (use ``"-"`` for start/end of
        history).
        """
        c = cmd_pkg.command(
            "tmux",
            "capture-pane",
            "-p",
            "-e",
            "-J",
            "-S",
            start,
            "-E",
            end,
            "-t",
            self.sanitized_name,
        )
        output, err = self._cmd_exec.output(c)
        if err is not None:
            return "", Exception(
                "failed to capture tmux pane content with options: {}".format(err)
            )
        return _to_str(output), None


def _to_str(output) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def _format_errs(errs: List[Exception]) -> str:
    """Render an error slice the way Go's ``%v`` renders ``[]error``.

    Go prints ``[err1 err2]`` (space-separated inside brackets).
    """
    return "[" + " ".join(str(e) for e in errs) + "]"


# --- Constructors -----------------------------------------------------------
def new_tmux_session(
    name: str,
    program: str,
    pty_factory: pty_pkg.PtyFactory,
    cmd_exec: cmd_pkg.Executor,
) -> TmuxSession:
    """Internal constructor (mirrors ``newTmuxSession``)."""
    return TmuxSession(
        sanitized_name=to_mindflock_tmux_name(name),
        program=program,
        pty_factory=pty_factory,
        cmd_exec=cmd_exec,
    )


def NewTmuxSession(name: str, program: str) -> TmuxSession:
    """Production constructor (mirrors ``NewTmuxSession``)."""
    return new_tmux_session(
        name, program, pty_pkg.MakePtyFactory(), cmd_pkg.make_executor()
    )


def NewTmuxSessionWithDeps(
    name: str,
    program: str,
    pty_factory: pty_pkg.PtyFactory,
    cmd_exec: cmd_pkg.Executor,
) -> TmuxSession:
    """Constructor with injected deps for testing (mirrors ``NewTmuxSessionWithDeps``)."""
    return new_tmux_session(name, program, pty_factory, cmd_exec)


# --- Package-level cleanup --------------------------------------------------
def cleanup_sessions(cmd_exec: cmd_pkg.Executor) -> Optional[Exception]:
    """Kill all tmux sessions that start with the prefix (mirrors ``CleanupSessions``)."""
    # First try to list sessions.
    c = cmd_pkg.command("tmux", "ls")
    output, err = cmd_exec.output(c)

    # If there's an error and it's because no server is running, that's fine.
    # Exit code 1 typically means no sessions exist.
    if err is not None:
        if isinstance(err, cmd_pkg.ExitError) and err.exit_code() == 1:
            return None  # No sessions to clean up.
        return Exception("failed to list tmux sessions: {}".format(err))

    text = _to_str(output)
    pattern = re.compile("{}.*:".format(re.escape(TmuxPrefix)))
    matches = pattern.findall(text)
    # Strip the trailing ':' (Go: match[:strings.Index(match, ":")]).
    matches = [m[: m.index(":")] for m in matches]

    for match in matches:
        if log.InfoLog is not None:
            log.InfoLog.Printf("cleaning up session: %s", match)
        kill = cmd_pkg.command("tmux", "kill-session", "-t", match)
        kerr = cmd_exec.run(kill)
        if kerr is not None:
            return Exception("failed to kill tmux session {}: {}".format(match, kerr))
    return None


# Go-cased alias.
CleanupSessions = cleanup_sessions


__all__ = [
    "ProgramClaude",
    "ProgramAider",
    "TmuxPrefix",
    "to_mindflock_tmux_name",
    "TmuxSession",
    "new_tmux_session",
    "NewTmuxSession",
    "NewTmuxSessionWithDeps",
    "cleanup_sessions",
    "CleanupSessions",
]
