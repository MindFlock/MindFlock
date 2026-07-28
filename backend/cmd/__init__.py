"""Port of the Go ``cmd`` package.

A thin abstraction layer over external process execution, mirroring the Go
``cmd`` package (``cmd/cmd.go`` + ``cmd/cmd_test/testutils.go``).

The Go code builds ``*exec.Cmd`` objects with ``exec.Command(name, args...)``
and passes them to an ``Executor`` whose two methods (``Run`` / ``Output``)
delegate to ``cmd.Run()`` / ``cmd.Output()``. ``ToString`` renders a command by
joining ``cmd.Args`` with spaces.

Python mirror:

- :func:`command` -> ``exec.Command``: builds a :class:`Cmd`.
- :class:`Cmd` -> ``*exec.Cmd``: holds ``args`` (full argv incl. program name)
  and an optional ``dir`` (process working directory; Go's ``cmd.Dir``).
- :class:`Executor` -> the Go ``Executor`` interface (``run`` / ``output``).
- :class:`Exec` -> the production ``Exec{}`` implementation.
- :func:`make_executor` -> ``MakeExecutor``.
- :func:`to_string` -> ``ToString``.
- :class:`MockCmdExec` -> the test double from ``testutils.go``.
- :class:`ExitError` -> Go's ``*exec.ExitError`` (exposes :meth:`exit_code`),
  so callers such as ``tmux.CleanupSessions`` can branch on exit code 1.

Error convention (matches Go's ``error`` return value): methods *return* an
error object (an ``Exception`` instance) rather than raising it. Success is
signalled by returning ``None`` for ``run`` and ``(output, None)`` for
``output``. This lets call sites write ``cmd_exec.run(cmd) is None`` exactly as
Go writes ``t.cmdExec.Run(cmd) == nil``.
"""

from __future__ import annotations

import subprocess
from typing import Callable, List, Optional, Sequence, Tuple


class Cmd:
    """Python mirror of Go's ``*exec.Cmd``.

    Only the surface the ``cmd``/``tmux``/``git`` code relies on is modelled:

    - ``args``: the full argument vector, *including* the program name as
      element 0 (matches ``exec.Cmd.Args``). ``ToString`` joins these.
    - ``dir``: the working directory in which to run the process (Go's
      ``cmd.Dir``). ``None`` means inherit the current process CWD. This is
      what the bare ``git push`` invocation relies on.
    - ``path``: the resolved program name (``args[0]``), mirroring
      ``exec.Cmd.Path`` loosely; kept for parity/debugging.
    """

    __slots__ = ("args", "dir", "path")

    def __init__(self, args: Sequence[str], dir: Optional[str] = None) -> None:
        self.args: List[str] = list(args)
        self.dir: Optional[str] = dir
        self.path: str = self.args[0] if self.args else ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Cmd(args={self.args!r}, dir={self.dir!r})"


def command(name: str, *args: str) -> Cmd:
    """Build a :class:`Cmd`, mirroring ``exec.Command(name, args...)``.

    The program name becomes ``args[0]`` so that ``to_string`` and the
    underlying subprocess invocation see the same argument vector Go does.
    """
    return Cmd([name, *args])


class ExitError(Exception):
    """Python mirror of Go's ``*exec.ExitError``.

    Carries the process exit code so callers can replicate Go's
    ``if exitErr, ok := err.(*exec.ExitError); ok && exitErr.ExitCode() == 1``
    check (used by ``tmux.CleanupSessions``).
    """

    def __init__(self, code: int, stderr: bytes = b"", cmd_str: str = ""):
        self._code = code
        self.stderr = stderr
        self.cmd_str = cmd_str
        if cmd_str:
            super().__init__(f"{cmd_str}: exit status {code}")
        else:
            super().__init__(f"exit status {code}")

    def exit_code(self) -> int:
        """Return the process exit code (Go's ``(*exec.ExitError).ExitCode()``)."""
        return self._code


class Executor:
    """Interface for command execution (Go ``Executor`` interface).

    Implemented as a base class so both :class:`Exec` and :class:`MockCmdExec`
    can subclass it, but duck typing (a structural/Protocol match) is all the
    consuming code requires: any object with ``run`` and ``output`` works.

    Methods:
        run(cmd) -> Optional[Exception]:
            Execute ``cmd`` and wait. Returns ``None`` on success, or an
            error object on failure (mirrors Go ``error``).
        output(cmd) -> Tuple[bytes, Optional[Exception]]:
            Execute ``cmd``, capture stdout. Returns ``(stdout_bytes, None)``
            on success, ``(captured_or_empty, error)`` on failure.
    """

    def run(self, cmd: Cmd) -> Optional[Exception]:  # pragma: no cover - interface
        raise NotImplementedError

    def output(self, cmd: Cmd) -> Tuple[bytes, Optional[Exception]]:  # pragma: no cover
        raise NotImplementedError


#: Default wall-clock budget for a subprocess (seconds). These commands are
#: local tmux/git invocations; anything slower than this has hung.
DEFAULT_TIMEOUT: float = 60.0


class Exec(Executor):
    """Production :class:`Executor` backed by :mod:`subprocess`.

    Stateless (mirrors Go's zero-value ``Exec{}`` struct). Each method runs the
    real external process described by ``cmd``.
    """

    def run(
        self, cmd: Cmd, timeout: Optional[float] = DEFAULT_TIMEOUT
    ) -> Optional[Exception]:
        """Run ``cmd`` to completion (mirrors ``exec.Cmd.Run``).

        Returns ``None`` on a clean (exit 0) run, an :class:`ExitError` on a
        non-zero exit, or the raised ``OSError``/``Exception`` if the process
        could not be started (e.g. executable not found). If the process runs
        longer than ``timeout`` seconds it is killed and a ``RuntimeError``
        describing the timeout is returned (same return-an-error convention).
        """
        try:
            completed = subprocess.run(
                cmd.args,
                cwd=cmd.dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run already killed the child.
            return RuntimeError(
                "{}: timed out after {:g}s".format(to_string(cmd), timeout)
            )
        except Exception as e:  # FileNotFoundError, OSError, etc.
            return e
        if completed.returncode != 0:
            return ExitError(completed.returncode, b"", to_string(cmd))
        return None

    def output(
        self, cmd: Cmd, timeout: Optional[float] = DEFAULT_TIMEOUT
    ) -> Tuple[bytes, Optional[Exception]]:
        """Run ``cmd`` and capture stdout (mirrors ``exec.Cmd.Output``).

        stderr is discarded, matching Go's ``cmd.Output()``. Returns
        ``(stdout, None)`` on success. On a non-zero exit returns the captured
        stdout together with an :class:`ExitError` (Go returns the captured
        output alongside the ``*exec.ExitError``). On a start failure returns
        ``(b"", error)``. If the process runs longer than ``timeout`` seconds
        it is killed and any captured output is returned alongside a
        ``RuntimeError`` describing the timeout.
        """
        try:
            completed = subprocess.run(
                cmd.args,
                cwd=cmd.dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            # subprocess.run already killed the child.
            out = e.output if e.output is not None else b""
            return out, RuntimeError(
                "{}: timed out after {:g}s".format(to_string(cmd), timeout)
            )
        except Exception as e:  # FileNotFoundError, OSError, etc.
            return b"", e
        out = completed.stdout if completed.stdout is not None else b""
        if completed.returncode != 0:
            return out, ExitError(completed.returncode, b"", to_string(cmd))
        return out, None


def make_executor() -> Executor:
    """Return a production :class:`Exec` (mirrors ``MakeExecutor``)."""
    return Exec()


def to_string(cmd: Optional[Cmd]) -> str:
    """Render a command to a string (mirrors ``ToString``).

    Returns the literal ``"<nil>"`` for ``None`` (Go's nil ``*exec.Cmd``),
    otherwise joins ``cmd.args`` with single spaces. No shell escaping is
    applied (naive join, exactly like ``strings.Join(cmd.Args, " ")``).
    """
    if cmd is None:
        return "<nil>"
    return " ".join(cmd.args)


class MockCmdExec(Executor):
    """Test double for :class:`Executor` (Go ``cmd_test.MockCmdExec``).

    Records nothing on its own; behaviour is injected via callables, mirroring
    the Go struct's ``RunFunc`` / ``OutputFunc`` function fields. Tests pass an
    instance wherever a :class:`cmd.Executor` is expected (e.g.
    ``NewTmuxSessionWithDeps``) and inspect the :class:`Cmd` objects forwarded
    to the funcs.

    If a func field is unset and the corresponding method is called, an
    ``AttributeError`` is raised -- the Python analogue of Go panicking on a nil
    function pointer.
    """

    def __init__(
        self,
        run_func: Optional[Callable[[Cmd], Optional[Exception]]] = None,
        output_func: Optional[
            Callable[[Cmd], Tuple[bytes, Optional[Exception]]]
        ] = None,
    ) -> None:
        self.run_func = run_func
        self.output_func = output_func

    def run(self, cmd: Cmd) -> Optional[Exception]:
        """Delegate to ``run_func`` unchanged (Go ``MockCmdExec.Run``)."""
        if self.run_func is None:
            raise AttributeError("MockCmdExec.run called but run_func is not set")
        return self.run_func(cmd)

    def output(self, cmd: Cmd) -> Tuple[bytes, Optional[Exception]]:
        """Delegate to ``output_func`` unchanged (Go ``MockCmdExec.Output``)."""
        if self.output_func is None:
            raise AttributeError("MockCmdExec.output called but output_func is not set")
        return self.output_func(cmd)


__all__ = [
    "Cmd",
    "command",
    "ExitError",
    "Executor",
    "Exec",
    "make_executor",
    "to_string",
    "MockCmdExec",
]
