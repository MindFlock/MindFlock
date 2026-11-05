"""The session host's client side — what ``backend.session.mux`` calls on
Windows: :func:`run` and :func:`popen` (``subprocess``'s shapes for a tmux
argv), :func:`attach` (a ``ptyprocess.PtyProcess``-shaped client on a
session) and :func:`console_attach` (a foreground client in this console).

Like tmux's client, it starts the host only for commands that start a
server (``new-session``, ``start-server``); anything else with no host
running answers ``no server running on …`` with exit status 1, without
starting one.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from backend import compat
from backend.session.winmux import proto

__all__ = ["run", "popen", "attach", "console_attach", "Terminal", "Popen"]

_STARTERS = ("new-session", "start-server")
#: What a request that does not start a server sends of the caller's env:
#: just what resolves the "current" session.
_CTX_ENV = ("TMUX", "TMUX_PANE")

_bad: set = set()  # (pid, port, token) of hosts that stopped answering


def _info_path() -> str:
    return os.path.join(proto.run_dir(), "winmux.json")


def _read_info() -> Optional[dict]:
    try:
        with open(_info_path(), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not info.get("port") or not info.get("token"):
        return None
    key = (info.get("pid"), info.get("port"), info.get("token"))
    if key in _bad:
        return None
    if not _pid_alive(int(info.get("pid") or 0)):
        # Checked before connecting: on Windows a connect to a closed
        # localhost port takes seconds to be refused.
        return None
    return info


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False
    except ImportError:  # pragma: no cover - psutil is a dependency
        if compat.IS_WINDOWS:
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def _forget(info: dict) -> None:
    _bad.add((info.get("pid"), info.get("port"), info.get("token")))


def _connect(info: dict, timeout: Optional[float]) -> socket.socket:
    sock = socket.create_connection(
        ("127.0.0.1", int(info["port"])), timeout=timeout if timeout else 10.0
    )
    sock.settimeout(timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    return sock


def _ping(info: dict) -> bool:
    try:
        sock = _connect(info, 3.0)
    except OSError:
        return False
    try:
        proto.send_json(sock, {"op": "ping", "token": info["token"]})
        resp = json.loads(proto.recv_line(sock, bytearray()))
        return bool(resp.get("ok"))
    except (OSError, ValueError, ConnectionError):
        return False
    finally:
        sock.close()


def _root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _spawn_host(run_dir: str, env: Dict[str, str]) -> None:
    from backend.session.winmux import _bootstrap

    argv = [sys.executable, "-c", _bootstrap.HOST_CODE, _root(), run_dir]
    kw: Dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=run_dir,
        env=env,
        close_fds=True,
    )
    if compat.IS_WINDOWS:  # pragma: no cover - Windows
        # No console (DETACHED_PROCESS), own group, and out of the caller's
        # job when allowed: Electron / a terminal may kill their job on exit,
        # and the host must outlive them like a tmux server.
        flags = 0x00000008 | 0x00000200 | 0x08000000
        try:
            subprocess.Popen(argv, creationflags=flags | 0x01000000, **kw)
            return
        except OSError:
            subprocess.Popen(argv, creationflags=flags, **kw)
            return
    subprocess.Popen(argv, start_new_session=True, **kw)


def _start_host(env: Dict[str, str]) -> dict:
    """Start the host (once, under the start lock: two clients racing here
    get one host) and return its rendezvous info."""
    run_dir = proto.run_dir()
    os.makedirs(run_dir, exist_ok=True)
    lock_path = os.path.join(run_dir, "winmux.lock")
    with open(lock_path, "a+b") as lock:
        compat.flock(lock, compat.LOCK_EX)
        try:
            info = _read_info()
            if info is not None and _ping(info):
                return info
            if info is not None:
                _forget(info)
            host_env = {k: v for k, v in env.items() if k not in _CTX_ENV}
            _spawn_host(run_dir, host_env)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                info = _read_info()
                if info is not None and _ping(info):
                    return info
                time.sleep(0.02)
            raise RuntimeError(
                "the MindFlock session host did not start (see %s)"
                % os.path.join(run_dir, "winmux.log")
            )
        finally:
            compat.flock(lock, compat.LOCK_UN)


def _request(
    payload: dict, start: bool, timeout: Optional[float], env: Dict[str, str]
) -> Optional[dict]:
    """Send ``payload`` to the host (starting it if ``start``). ``None`` when
    no host runs and none may be started."""
    deadline = None if timeout is None else time.monotonic() + timeout
    for _ in range(4):
        info = _read_info()
        if info is None:
            if not start:
                return None
            info = _start_host(env)
        left = None if deadline is None else max(0.01, deadline - time.monotonic())
        try:
            sock = _connect(info, left)
        except socket.timeout:
            raise
        except OSError:
            _forget(info)
            continue
        try:
            try:
                proto.send_json(sock, dict(payload, token=info["token"]))
            except OSError:
                _forget(info)
                continue
            try:
                resp = json.loads(proto.recv_line(sock, bytearray(), limit=1 << 31))
            except socket.timeout:
                raise
            except (OSError, ConnectionError, ValueError):
                # Sent but unanswered: the command may have run, so it is not
                # retried (tmux's client says the same when its server dies).
                return {"rc": 1, "err": proto.b64(b"server exited unexpectedly\n")}
        finally:
            sock.close()
        if resp.get("retry"):
            _forget(info)
            time.sleep(0.05)
            continue
        if resp.get("error") == "bad token":
            _forget(info)
            continue
        return resp
    if not start:
        return None
    raise RuntimeError("the MindFlock session host is not answering")


def execute(
    argv: List[str],
    stdin: bytes = b"",
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Tuple[int, bytes, bytes]:
    """A tmux argv (``argv[0] == "tmux"``) → ``(returncode, stdout,
    stderr)``. Raises ``socket.timeout`` past ``timeout``."""
    args = [str(a) for a in argv[1:]]
    try:
        flags, cmdv = proto.parse_top(args)
    except proto.CmdError as e:
        return 1, b"", (str(e) + "\n").encode()
    if "V" in flags:
        return 0, (proto.VERSION_LINE + "\n").encode(), b""
    ns = proto.namespace_of(flags)
    chain = proto.split_chain(cmdv) or [["new-session"]]
    start = any(proto.canonical(c[0]) in _STARTERS for c in chain)
    full_env = dict(os.environ if env is None else env)
    payload = {
        "op": "run",
        "argv": args,
        "cwd": os.path.abspath(cwd) if cwd else os.getcwd(),
        "env": (
            full_env if start else {k: full_env[k] for k in _CTX_ENV if k in full_env}
        ),
        "pid": os.getpid(),
    }
    if stdin:
        payload["stdin"] = proto.b64(stdin)
    resp = _request(payload, start, timeout, full_env)
    if resp is None:
        return 1, b"", ("no server running on %s\n" % proto.socket_label(ns)).encode()
    return (
        int(resp.get("rc", 1)),
        proto.unb64(resp.get("out")),
        proto.unb64(resp.get("err")),
    )


# -- subprocess shapes -------------------------------------------------------------


def _decode(data: bytes, text: bool, encoding: Optional[str], errors: Optional[str]):
    if not text:
        return data
    s = data.decode(encoding or "utf-8", errors or "strict")
    return s.replace("\r\n", "\n")


def _deliver(dest: Any, data: bytes, std) -> Optional[bytes]:
    """Route one output stream as ``subprocess`` would: ``PIPE`` captures,
    ``DEVNULL`` drops, ``None`` inherits (this process's stream), a file or
    fd receives it."""
    if dest == subprocess.PIPE:
        return data
    if dest == subprocess.DEVNULL or not data:
        return None
    if dest is None:
        target = std
        try:
            target.flush()
        except Exception:  # noqa: BLE001
            pass
        buf = getattr(target, "buffer", None)
        if buf is not None:
            buf.write(data)
            buf.flush()
        else:
            target.write(data.decode("utf-8", "replace"))
        return None
    if isinstance(dest, int):
        os.write(dest, data)
        return None
    try:
        dest.write(data)
    except TypeError:
        dest.write(data.decode("utf-8", "replace"))
    try:
        dest.flush()
    except Exception:  # noqa: BLE001
        pass
    return None


def _input_bytes(input: Any, stdin: Any) -> bytes:
    if input is not None:
        return input.encode("utf-8") if isinstance(input, str) else bytes(input)
    if stdin not in (None, subprocess.PIPE, subprocess.DEVNULL):
        try:
            if isinstance(stdin, int):
                chunks = []
                while True:
                    b = os.read(stdin, 1 << 16)
                    if not b:
                        break
                    chunks.append(b)
                return b"".join(chunks)
            data = stdin.read()
            return data.encode("utf-8") if isinstance(data, str) else data
        except (OSError, ValueError):
            return b""
    return b""


def run(argv, **kw) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, **kw)`` for a tmux argv, answered by the host."""
    argv = list(argv)
    input = kw.pop("input", None)
    capture = kw.pop("capture_output", False)
    stdin = kw.pop("stdin", None)
    stdout = kw.pop("stdout", None)
    stderr = kw.pop("stderr", None)
    timeout = kw.pop("timeout", None)
    check = kw.pop("check", False)
    env = kw.pop("env", None)
    cwd = kw.pop("cwd", None)
    encoding = kw.pop("encoding", None)
    errors = kw.pop("errors", None)
    text = bool(
        kw.pop("text", None) or kw.pop("universal_newlines", None) or encoding or errors
    )
    if input is not None and stdin is not None:
        raise ValueError("stdin and input arguments may not both be used.")
    if capture:
        if stdout is not None or stderr is not None:
            raise ValueError(
                "stdout and stderr arguments may not be used with capture_output."
            )
        stdout = stderr = subprocess.PIPE
    try:
        rc, out, err = execute(
            argv, _input_bytes(input, stdin), env=env, cwd=cwd, timeout=timeout
        )
    except socket.timeout:
        raise subprocess.TimeoutExpired(argv, timeout)
    if stderr == subprocess.STDOUT:
        out, err = out + err, b""
        stderr = None if stdout != subprocess.PIPE else subprocess.DEVNULL
    got_out = _deliver(stdout, out, sys.stdout)
    got_err = (
        _deliver(stderr, err, sys.stderr) if stderr != subprocess.DEVNULL else None
    )
    res_out = (
        None
        if got_out is None and stdout != subprocess.PIPE
        else _decode(got_out or b"", text, encoding, errors)
    )
    res_err = (
        None
        if got_err is None and stderr != subprocess.PIPE
        else _decode(got_err or b"", text, encoding, errors)
    )
    cp = subprocess.CompletedProcess(argv, rc, res_out, res_err)
    if check and rc:
        raise subprocess.CalledProcessError(rc, argv, res_out, res_err)
    return cp


class Popen:
    """``subprocess.Popen``'s shape for a tmux argv: the request runs in a
    thread (once ``stdin``'s input is known); ``stdout`` / ``stderr`` are real
    pipes when ``PIPE`` was asked for."""

    def __init__(self, argv, **kw) -> None:
        self.args = list(argv)
        self.pid = None
        self.returncode: Optional[int] = None
        self._kw_stdin = kw.get("stdin")
        self._stdout_dest = kw.get("stdout")
        self._stderr_dest = kw.get("stderr")
        self._env = kw.get("env")
        self._cwd = kw.get("cwd")
        self._text = bool(
            kw.get("text")
            or kw.get("universal_newlines")
            or kw.get("encoding")
            or kw.get("errors")
        )
        self._encoding = kw.get("encoding")
        self._errors = kw.get("errors")
        self._done = threading.Event()
        self._out = b""
        self._err = b""
        self._exc: Optional[BaseException] = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self._pipes: Dict[str, int] = {}
        for name, dest in (
            ("stdout", self._stdout_dest),
            ("stderr", self._stderr_dest),
        ):
            if dest == subprocess.PIPE:
                r, w = os.pipe()
                self._pipes[name] = w
                f = os.fdopen(r, "rb")
                if self._text:
                    f = io.TextIOWrapper(
                        f,
                        encoding=self._encoding or "utf-8",
                        errors=self._errors or "strict",
                    )
                setattr(self, name, f)
        self._input = b""
        if self._kw_stdin == subprocess.PIPE:
            self.stdin = _StdinPipe(self)
        else:
            self._input = _input_bytes(None, self._kw_stdin)
            self._start()

    def _start(self) -> None:
        if getattr(self, "_thread", None) is not None:
            return
        self._thread = threading.Thread(target=self._go, daemon=True)
        self._thread.start()

    def _go(self) -> None:
        try:
            rc, out, err = execute(self.args, self._input, env=self._env, cwd=self._cwd)
        except BaseException as e:  # noqa: BLE001
            rc, out, err, self._exc = 1, b"", (str(e) + "\n").encode(), e
        if self._stderr_dest == subprocess.STDOUT:
            out, err = out + err, b""
        for name, data, std in (
            ("stdout", out, sys.stdout),
            ("stderr", err, sys.stderr),
        ):
            w = self._pipes.get(name)
            if w is not None:
                try:
                    view = memoryview(data)
                    while view:
                        n = os.write(w, view)
                        view = view[n:]
                except OSError:
                    pass
                os.close(w)
            elif name == "stdout" and self._stdout_dest is not subprocess.PIPE:
                _deliver(self._stdout_dest, data, std)
            elif name == "stderr" and self._stderr_dest not in (
                subprocess.PIPE,
                subprocess.STDOUT,
            ):
                _deliver(self._stderr_dest, data, std)
        self._out, self._err = out, err
        self.returncode = rc
        self._done.set()

    def poll(self) -> Optional[int]:
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout: Optional[float] = None) -> int:
        self._start()
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode  # type: ignore[return-value]

    def communicate(self, input=None, timeout: Optional[float] = None):
        if input is not None:
            self._input = (
                input.encode("utf-8") if isinstance(input, str) else bytes(input)
            )
        self._start()
        self.wait(timeout)
        out = self.stdout.read() if self.stdout is not None else None
        err = self.stderr.read() if self.stderr is not None else None
        return out, err

    def kill(self) -> None:
        return

    terminate = kill

    def send_signal(self, sig) -> None:
        return

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        for f in (self.stdout, self.stderr):
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
        self.wait()


class _StdinPipe:
    def __init__(self, p: Popen) -> None:
        self._p = p
        self._buf = bytearray()
        self.closed = False

    def write(self, data) -> int:
        b = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._buf += b
        return len(data)

    def flush(self) -> None:
        return

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._p._input = bytes(self._buf)
            self._p._start()


def popen(argv, **kw) -> Popen:
    return Popen(argv, **kw)


# -- attach ------------------------------------------------------------------------------


class Terminal:
    """An attached client, shaped like ``ptyprocess.PtyProcess`` for the
    callers (``mux.Terminal``): ``fd`` is ``None`` — read from a thread;
    :meth:`read` blocks and raises ``EOFError`` once the client is gone.
    ``pid`` is ``None``: there is no client process to signal."""

    fd = None
    pid = None

    def __init__(
        self,
        session: str,
        readonly: bool = False,
        dimensions: Tuple[int, int] = (24, 80),
        ns: str = "default",
        detach_others: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.session = session
        self.readonly = readonly
        self._rows, self._cols = int(dimensions[0]), int(dimensions[1])
        self._buf = bytearray()
        self._closed = False
        self._eof = False
        self._wlock = threading.Lock()
        self.sock: Optional[socket.socket] = None
        self.exitstatus: Optional[int] = None
        e = dict(os.environ if env is None else env)
        req = {
            "op": "attach",
            "ns": ns,
            "target": session,
            "readonly": readonly,
            "rows": self._rows,
            "cols": self._cols,
            "pid": os.getpid(),
            "detach_others": detach_others,
            "env": {k: e[k] for k in _CTX_ENV if k in e},
            "termname": e.get("TERM", "xterm-256color"),
        }
        info = _read_info()
        message = None
        if info is None:
            message = "no server running on %s" % proto.socket_label(ns)
        else:
            try:
                sock = _connect(info, 10.0)
                proto.send_json(sock, dict(req, token=info["token"]))
                resp = json.loads(proto.recv_line(sock, self._buf))
            except (OSError, ConnectionError, ValueError) as err:
                message = "no server running on %s (%s)" % (proto.socket_label(ns), err)
            else:
                if resp.get("ok"):
                    sock.settimeout(None)
                    self.sock = sock
                    self.session = resp.get("session") or session
                else:
                    sock.close()
                    message = str(resp.get("error") or "attach failed")
        if self.sock is None:
            # What a failed `tmux attach` shows in its pty before exiting.
            self._buf = bytearray((message + "\r\n").encode())
            self._eof = True
            self.exitstatus = 1

    @property
    def attached(self) -> bool:
        return self.sock is not None and not self._closed

    def read(self, size: int = 1024) -> bytes:
        while not self._buf:
            if self._eof or self.sock is None:
                raise EOFError("winmux client closed")
            try:
                chunk = self.sock.recv(max(size, 1 << 14))
            except OSError:
                chunk = b""
            if not chunk:
                self._eof = True
                if self.exitstatus is None:
                    self.exitstatus = 0
                continue
            self._buf.extend(chunk)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def _send(self, data: bytes) -> None:
        if self.sock is None or self._closed:
            raise OSError("winmux client closed")
        with self._wlock:
            self.sock.sendall(data)

    def write(self, data: bytes) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        if data:
            self._send(proto.frame(b"d", bytes(data)))
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        self._rows, self._cols = int(rows), int(cols)
        try:
            self._send(proto.resize_frame(self._rows, self._cols))
        except OSError:
            pass

    def getwinsize(self) -> Tuple[int, int]:
        return self._rows, self._cols

    def isalive(self) -> bool:
        return self.sock is not None and not self._closed and not self._eof

    def detach(self) -> None:
        try:
            self._send(proto.frame(b"x"))
        except OSError:
            pass

    def terminate(self, force: bool = False) -> bool:
        self.close()
        return True

    def close(self, force: bool = True) -> None:
        if self._closed:
            return
        self.detach()
        self._closed = True
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def attach(
    session: str,
    readonly: bool = False,
    dimensions: Tuple[int, int] = (24, 80),
    ns: str = "default",
    env: Optional[Dict[str, str]] = None,
) -> Terminal:
    return Terminal(session, readonly=readonly, dimensions=dimensions, ns=ns, env=env)


# -- console client ----------------------------------------------------------------------


def console_attach(
    session: Optional[str],
    readonly: bool = False,
    ns: str = "default",
    detach_others: bool = False,
) -> int:
    """A foreground client in this console — ``tmux attach`` for a person:
    the console in raw mode, ``Ctrl-B d`` detaches (tmux's default prefix
    and binding; ``Ctrl-B Ctrl-B`` sends one ``Ctrl-B``). Returns the exit
    status."""
    cols, rows = _console_size()
    term = Terminal(
        session or "",
        readonly=readonly,
        dimensions=(rows, cols),
        ns=ns,
        detach_others=detach_others,
    )
    out = getattr(sys.stdout, "buffer", sys.stdout)
    if not term.attached:
        sys.stderr.write(bytes(term._buf).decode("utf-8", "replace").rstrip() + "\n")
        return 1
    restore = _raw_console()
    detached = threading.Event()

    def pump_in():
        prefix = False
        while not detached.is_set():
            try:
                data = _read_console()
            except (OSError, ValueError):
                return
            if not data:
                return
            send = bytearray()
            for b in data:
                if prefix:
                    prefix = False
                    if b in (ord("d"), ord("D")):
                        detached.set()
                        term.detach()
                        return
                    send.append(b)
                    continue
                if b == 0x02:
                    prefix = True
                    continue
                send.append(b)
            if send:
                try:
                    term.write(bytes(send))
                except OSError:
                    return

    def watch_size():
        last = (rows, cols)
        while term.isalive() and not detached.is_set():
            time.sleep(0.25)
            c, r = _console_size()
            if (r, c) != last:
                last = (r, c)
                term.setwinsize(r, c)

    threading.Thread(target=pump_in, daemon=True).start()
    threading.Thread(target=watch_size, daemon=True).start()
    tail = b""
    try:
        while True:
            try:
                data = term.read(1 << 16)
            except EOFError:
                break
            tail = (tail + data)[-200:]
            out.write(data)
            out.flush()
    finally:
        restore()
        term.close()
    msg = "[detached (from session %s)]" % term.session if detached.is_set() else ""
    if not msg:
        text = tail.decode("utf-8", "replace")
        msg = "" if "[" in text[-60:] else "[exited]"
    out.write(("\x1b[?1049l\x1b[0m\r\n" + msg + "\r\n").encode())
    out.flush()
    return 0


def _console_size() -> Tuple[int, int]:
    import shutil

    sz = shutil.get_terminal_size((80, 24))
    return sz.columns, sz.lines


def _read_console() -> bytes:
    return os.read(sys.stdin.fileno(), 1 << 14)


def _raw_console():
    """Put the console in raw mode; returns the function that restores it."""
    if compat.IS_WINDOWS:  # pragma: no cover - Windows
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hin = k32.GetStdHandle(-10)
        hout = k32.GetStdHandle(-11)
        old_in, old_out = wintypes.DWORD(), wintypes.DWORD()
        k32.GetConsoleMode(hin, ctypes.byref(old_in))
        k32.GetConsoleMode(hout, ctypes.byref(old_out))
        old_cp_in, old_cp_out = k32.GetConsoleCP(), k32.GetConsoleOutputCP()
        # Input: no line editing / echo / Ctrl-C processing, VT input on.
        k32.SetConsoleMode(hin, (old_in.value & ~0x0007) | 0x0200)
        # Output: VT processing on, no auto-CRLF translation.
        k32.SetConsoleMode(hout, old_out.value | 0x0004 | 0x0008)
        k32.SetConsoleCP(65001)
        k32.SetConsoleOutputCP(65001)

        def restore():
            k32.SetConsoleMode(hin, old_in.value)
            k32.SetConsoleMode(hout, old_out.value)
            k32.SetConsoleCP(old_cp_in)
            k32.SetConsoleOutputCP(old_cp_out)

        return restore
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
    except Exception:  # noqa: BLE001 - not a terminal
        return lambda: None

    def restore():
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return restore
