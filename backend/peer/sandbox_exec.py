"""Host-side launcher: exec the shared-session agent inside the sandbox —
bubblewrap on Linux, ``sandbox-exec`` (Seatbelt) on macOS.

    python -P -m backend.peer.sandbox_exec --share <id> --provider <name> \
        [--port N] [--env KEY=VALUE ...] -- <argv...>

Prepares the share's home, copies :mod:`backend.peer.bridge` into ``run/``,
builds the bwrap argv (:func:`backend.peer.sandbox.build_argv`) and replaces
this process with bwrap. The agent runs as::

    sh -c '<python> -I <run>/bridge.py <run>/egress.sock <port> || exit 70; shift 4; exec "$@"' \
        sh <python> <bridge> <sock> <port> <argv...>

(paths are passed as positional args, never spliced into the script).

On macOS this process stays (outside the sandbox) as a small supervisor: the
child ``setsid()``s — so the pane is not its controlling tty and ``TIOCSTI``
injection into it fails — and execs ``sandbox-exec``; the parent forwards
resize and termination signals to it and exits with its status. The bridge
port is chosen free on the host's loopback when none is given (there is no
private network namespace to make the default port safe to share).

Fails closed: exit 78 (EX_CONFIG) with a message on stderr when the sandbox
is unavailable or can't be built.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys

from backend.peer import paths, sandbox

EX_USAGE = 64
EX_CONFIG = 78

_LAUNCH = '"$1" -I "$2" "$3" "$4" || exit 70; shift 4; exec "$@"'


def _fail(msg: str, code: int = EX_CONFIG) -> int:
    sys.stderr.write(f"mindflock: peer sandbox: {msg}\n")
    sys.stderr.flush()
    return code


def inner_argv(share: dict, port: int, argv: list[str]) -> list[str]:
    run = share["run"]
    py = os.path.realpath(sys.executable)
    return [
        "sh",
        "-c",
        _LAUNCH,
        "sh",
        py,
        os.path.join(run, "bridge.py"),
        os.path.join(run, "egress.sock"),
        str(port),
        *argv,
    ]


def install_bridge(share: dict) -> str:
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
    with open(src, "rb") as fh:
        data = fh.read()
    return sandbox.safe_write(share["run"], "bridge.py", data, 0o600)


def prepare(
    share_id: str, provider: str, port: int, argv: list[str], env: dict
) -> tuple[list[str], list[int]]:
    """Everything up to the exec: returns ``(exec argv, fds it must
    inherit)``. The fds are an ``--args`` memfd and (where supported) the
    seccomp filter memfd."""
    share = paths.share_paths(share_id)
    sandbox.share_dirs(share)
    sandbox.prepare_home(share, provider)
    install_bridge(share)
    inner = inner_argv(share, port, argv)
    sfd = sandbox.open_seccomp_fd()
    try:
        exe, opts = sandbox.build_options(
            share, inner, provider, env, bridge_port=port, seccomp_fd=sfd
        )
    except BaseException:
        if sfd is not None:
            os.close(sfd)
        raise
    exec_argv, afd = args_via_fd(exe, opts, inner)
    return exec_argv, [fd for fd in (afd, sfd) if fd is not None]


def args_via_fd(exe: str, opts: list[str], inner: list[str]) -> tuple[list[str], int]:
    """Move bwrap's options (``--setenv`` values included) off the command
    line, which any local user can read in ``/proc/<pid>/cmdline``, into an
    inheritable memfd read by ``bwrap --args FD``. The inner command stays on
    the command line (bwrap requires it there)."""
    fd = os.memfd_create("mindflock-bwrap-args", 0)
    data = b"".join(a.encode() + b"\0" for a in opts)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return [exe, "--args", str(fd), "--", *inner], fd


def close_fds_except(keep: list[int]) -> None:
    """Close every fd >= 3 not in ``keep``. bwrap does not close inherited
    fds, and an fd on a host directory would be an openat() escape hatch."""
    lo = 3
    for fd in sorted(set(k for k in keep if k >= 3)):
        os.closerange(lo, fd)
        lo = fd + 1
    os.closerange(lo, 1 << 20)


def free_port() -> int:
    """A free TCP port on 127.0.0.1 for the in-sandbox bridge (macOS)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pane_tty() -> str | None:
    """The tty on stdin (the tmux pane), or None."""
    try:
        return os.path.realpath(os.ttyname(0)) if os.isatty(0) else None
    except OSError:
        return None


def prepare_seatbelt(
    share_id: str, provider: str, port: int, argv: list[str], env: dict
) -> tuple[list[str], dict[str, str], str]:
    """macOS counterpart of :func:`prepare`: ``(sandbox-exec argv, its
    environment, the working dir)``, with ``run/bin`` and ``home/.tmp`` in
    place."""
    share = paths.share_paths(share_id)
    d = sandbox.share_dirs(share)
    sandbox.prepare_home(share, provider)
    install_bridge(share)
    inner = inner_argv(share, port, argv)
    exec_argv, exec_env, links = sandbox.build_seatbelt(
        share, inner, provider, env, bridge_port=port, tty=pane_tty()
    )
    sandbox.install_links(d["run"], links)
    hfd = os.open(d["home"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(sandbox.SEATBELT_TMP, 0o700, dir_fd=hfd)
        except FileExistsError:
            pass
    finally:
        os.close(hfd)
    return exec_argv, exec_env, d["work"]


#: Forwarded from the supervisor to the sandboxed process group.
FORWARD_SIGNALS = (
    signal.SIGWINCH,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGUSR1,
    signal.SIGUSR2,
)


def supervise(exec_argv: list[str], exec_env: dict[str, str], cwd: str) -> int:
    """Fork; the child leaves the pane's session and execs the sandbox, the
    parent relays signals and returns the child's exit status."""
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.setsid()
            os.chdir(cwd)
            os.umask(0o077)
            close_fds_except([])
            os.execve(exec_argv[0], exec_argv, exec_env)
        finally:
            os._exit(EX_CONFIG)

    def relay(signum, _frame):
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass

    for sig in FORWARD_SIGNALS:
        signal.signal(sig, relay)
    while True:
        try:
            _pid, status = os.waitpid(pid, 0)
        except ChildProcessError:
            return EX_CONFIG
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)


def main(args: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backend.peer.sandbox_exec")
    ap.add_argument("--share", required=True)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--port", type=int, default=sandbox.DEFAULT_BRIDGE_PORT)
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    ns = ap.parse_args(args)
    argv = ns.argv[1:] if ns.argv[:1] == ["--"] else ns.argv
    if not argv:
        return _fail("no command given", EX_USAGE)
    env: dict[str, str] = {}
    for item in ns.env:
        key, sep, val = item.partition("=")
        if not sep:
            return _fail(f"bad --env {key!r}", EX_USAGE)
        env[key] = val

    ok, reason = sandbox.available()
    if not ok:
        return _fail(f"unavailable: {reason}")
    if sys.platform == "darwin":
        port = ns.port
        if port == sandbox.DEFAULT_BRIDGE_PORT:
            port = free_port()
        try:
            exec_argv, exec_env, cwd = prepare_seatbelt(
                ns.share, ns.provider, port, argv, env
            )
        except (sandbox.SandboxError, ValueError, OSError) as exc:
            return _fail(str(exc) or type(exc).__name__)
        return supervise(exec_argv, exec_env, cwd)
    try:
        exec_argv, keep = prepare(ns.share, ns.provider, ns.port, argv, env)
    except (sandbox.SandboxError, ValueError, OSError) as exc:
        return _fail(str(exc) or type(exc).__name__)

    sys.stdout.flush()
    sys.stderr.flush()
    os.umask(0o077)
    close_fds_except(keep)
    os.execve(exec_argv[0], exec_argv, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    return EX_CONFIG  # unreachable


if __name__ == "__main__":
    sys.exit(main())
