"""``mindflock slot`` — a machine-wide gate for heavy commands.

Agents rarely run the machine out of memory by STARTING: it is the work inside
sessions that are already running. A repo whose pre-commit hook runs two
``pytest -n auto`` suites, a documentarian that starts two more agent CLIs and
a code-graph index costs ~8 GB per commit — and when four agents commit at
once, the OOM killer takes the flock down mid-turn. The memory guard
(:mod:`backend.memory_pressure`) only gates agent SPAWNS; this gates the heavy
commands themselves::

    mindflock slot [--pool NAME] [--max N] [--timeout SECS]
                   [--wait-memory/--no-wait-memory] -- CMD [ARGS...]

runs CMD once it holds one of N machine-wide slots for the pool (default pool
``heavy``) and, by default, once memory reads ok. It needs no server.

The semaphore is N lock files, ``<run dir>/slots/<pool>/slot-<i>.lock``
(``$MINDFLOCK_RUN_DIR``, else ``~/.mindflock/run``), each taken with an
exclusive ``flock``. The kernel drops a flock when its holder dies, however it
dies, so a crashed holder never leaks a slot. Holder details (pid, command,
cwd, tmux pane, start time) sit next to the lock in ``slot-<i>.json``, for the
one-line "waiting for…" message and ``GET /api/system/slots``.

Waiters queue first-come first-served: each holds a flock on its own
``wait-<ns>-<pid>.json`` (so a dead waiter is told apart from a live one by
probing that lock, never by pid), and only the oldest N live waiters try for a
free slot. A newcomer joins the queue when anyone is already in it.

THIS process holds the slot while CMD runs as its child: SIGTERM/SIGHUP are
forwarded, SIGINT too unless the terminal already delivered it to the child,
the child's exit status is passed through exactly (a signal death is re-raised
on ourselves), and stdin/stdout/stderr are the child's own. The child does not
inherit the lock fd — a daemon CMD leaves behind must not keep the slot.

Never a deadlock: crashed holders release by construction; the memory wait is
capped (:data:`MEMORY_MAX_WAIT_SECONDS`, then CMD runs with a warning, as the
ticket pipeline's own memory hold does); ``--timeout`` caps the slot wait the
same way; a nested ``mindflock slot`` for a pool its parent already holds runs
straight through (``$MINDFLOCK_SLOT_HELD``). ``MINDFLOCK_SLOT=0`` bypasses
the gate entirely (exec, nothing else).

N: ``--max``, else ``$MINDFLOCK_HEAVY_SLOTS``, else ``general.heavy_slots`` in
settings.json, else :func:`default_slots` (1 below ~32 GB of RAM, else 2).

Imports stay stdlib-light on purpose: this wraps every commit, so it must not
pay for the settings/config layer (``backend.config`` alone is ~130 ms).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shlex
import signal
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence

__all__ = [
    "DEFAULT_POOL",
    "MEMORY_MAX_WAIT_SECONDS",
    "Slot",
    "acquire",
    "configured_slots",
    "default_slots",
    "main",
    "pool_dir",
    "run",
    "slots_root",
    "status",
]

DEFAULT_POOL = "heavy"

#: Pools nested inside a holder's command run straight through (comma list).
HELD_ENV = "MINDFLOCK_SLOT_HELD"

#: How often a waiter re-tries (seconds); jittered a little per process.
POLL_SECONDS = 0.5

#: How long a held slot waits for memory to read ok before running anyway.
MEMORY_MAX_WAIT_SECONDS = 600.0
MEMORY_POLL_SECONDS = 5.0

#: Below this much RAM a machine gets one heavy slot by default (a nominal
#: 32 GB box reports ~31 GiB, hence the margin).
_TWO_SLOTS_FROM_BYTES = 30 * 1024**3

_POOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_CMD_MAX = 300

_GiB = 1024**3


# --------------------------------------------------------------------------- #
# Paths and the slot count
# --------------------------------------------------------------------------- #
def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _guard(path: str, what: str) -> str:
    # The home guard lives under backend.config, whose package import is the
    # slow part — only pay for it where it matters.
    if _under_pytest():
        from backend.config.home_guard import guard

        return guard(path, what)
    return path


def _config_dir() -> str:
    home = os.environ.get("HOME", "") or os.path.expanduser("~")
    return os.path.join(home, ".mindflock")


def slots_root() -> str:
    """``$MINDFLOCK_RUN_DIR/slots``, else ``~/.mindflock/run/slots`` — the same
    run dir the MCP run files use, so ``uninstall --purge`` sweeps it."""
    env = (os.environ.get("MINDFLOCK_RUN_DIR") or "").strip()
    base = env or os.path.join(_config_dir(), "run")
    return _guard(os.path.join(base, "slots"), "slot dir")


def valid_pool(pool: str) -> bool:
    return bool(_POOL_RE.match(pool or ""))


def pool_dir(pool: str) -> str:
    if not valid_pool(pool):
        raise ValueError("bad pool name %r (letters, digits, '-', '_', '.')" % pool)
    return os.path.join(slots_root(), pool)


def _total_memory() -> int:
    """Physical RAM in bytes (Linux and macOS), 0 when unknown."""
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def default_slots(total_bytes: Optional[int] = None) -> int:
    """1 heavy slot below ~32 GB of RAM, else 2.

    Measured 2026-10: one commit in a repo with two xdist test suites, a
    documentarian and a code-graph index peaks around 8 GB. A 24 GB machine
    with a dozen agent CLIs resident (~0.3-0.5 GB each) holds one such job
    with headroom and not two; from 32 GB, two fit. Raise it per machine with
    ``general.heavy_slots``."""
    total = _total_memory() if total_bytes is None else int(total_bytes)
    return 2 if total >= _TWO_SLOTS_FROM_BYTES else 1


def _settings_path() -> str:
    env = os.environ.get("MINDFLOCK_SETTINGS_FILE")
    if env:
        return _guard(env, "settings store")
    return _guard(os.path.join(_config_dir(), "settings.json"), "settings store")


def _pos_int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def configured_slots() -> Dict[str, object]:
    """``{"slots": N, "source": "env"|"setting"|"default"}`` — what a
    ``mindflock slot`` without ``--max`` uses. Reads settings.json directly
    (no settings layer: see the module docstring); never raises."""
    n = _pos_int(os.environ.get("MINDFLOCK_HEAVY_SLOTS"))
    if n is not None:
        return {"slots": n, "source": "env"}
    try:
        with open(_settings_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
        n = _pos_int(((doc or {}).get("general") or {}).get("heavy_slots"))
    except Exception:  # noqa: BLE001 — missing/corrupt store = the default
        n = None
    if n is not None:
        return {"slots": n, "source": "setting"}
    return {"slots": default_slots(), "source": "default"}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def _record(pool: str, n: int, cmd: Sequence[str]) -> dict:
    """Who is asking: shown to waiters and by ``/api/system/slots``."""
    text = shlex.join([str(c) for c in cmd])
    if len(text) > _CMD_MAX:
        text = text[: _CMD_MAX - 1] + "…"
    tmux = os.environ.get("TMUX") or ""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    return {
        "pid": os.getpid(),
        "pool": pool,
        "max": int(n),
        "cmd": text,
        "cwd": cwd,
        "since": time.time(),
        "session": (os.environ.get("MINDFLOCK_SESSION_TITLE") or "").strip(),
        "tmux_pane": (os.environ.get("TMUX_PANE") or "").strip() if tmux else "",
        "tmux_socket": tmux.split(",", 1)[0] if tmux else "",
    }


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: str, doc: dict) -> None:
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _probe_locked(path: str) -> Optional[bool]:
    """Whether someone holds the flock on ``path`` (None: no such file).

    Takes a SHARED lock for an instant when it is free — an acquirer's
    non-blocking attempt in that instant just tries again next poll."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as err:
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return True
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# The semaphore
# --------------------------------------------------------------------------- #
class Slot:
    """A held slot. :meth:`release` (or process death) frees it."""

    def __init__(self, pool: str, index: int, fd: int, directory: str) -> None:
        self.pool = pool
        self.index = index
        self.fd = fd
        self.dir = directory

    @property
    def lock_path(self) -> str:
        return os.path.join(self.dir, "slot-%d.lock" % self.index)

    @property
    def record_path(self) -> str:
        return os.path.join(self.dir, "slot-%d.json" % self.index)

    def release(self) -> None:
        if self.fd < 0:
            return
        # Record first, lock last: while the fd is held nobody else can have
        # written this record, so it is ours to remove.
        try:
            os.unlink(self.record_path)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = -1

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _ensure_dir(directory: str) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)


def try_acquire(pool: str, n: int, record: Optional[dict] = None) -> Optional[Slot]:
    """Take a free slot among the first ``n`` without waiting, or None."""
    directory = pool_dir(pool)
    _ensure_dir(directory)
    for i in range(max(1, int(n))):
        path = os.path.join(directory, "slot-%d.lock" % i)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            os.close(fd)
            if err.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                continue
            raise
        slot = Slot(pool, i, fd, directory)
        if record is not None:
            try:
                _write_json_atomic(slot.record_path, dict(record, slot=i))
            except OSError:
                pass  # the lock is what counts; the record is for display
        return slot
    return None


class _Waiter:
    """Our place in the queue: a flock'd ``wait-<ns>-<pid>.json``."""

    def __init__(self, directory: str, record: dict) -> None:
        self.dir = directory
        self.name = "wait-%020d-%d.json" % (time.time_ns(), os.getpid())
        self.path = os.path.join(directory, self.name)
        # Lock under a temp name, then rename into place: the queue never
        # sees an unlocked (= dead-looking) file of a live waiter.
        tmp = os.path.join(directory, ".tmp-" + self.name)
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, json.dumps(record).encode("utf-8"))
        os.rename(tmp, self.path)
        self.fd = fd

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = -1


def _live_waiters(directory: str, *, reap: bool = True) -> List[str]:
    """Names of live waiter files, oldest first. A waiter file nobody holds
    a lock on belongs to a dead process: removed when ``reap``."""
    try:
        names = sorted(
            f
            for f in os.listdir(directory)
            if f.startswith("wait-") and f.endswith(".json")
        )
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(directory, name)
        held = _probe_locked(path)
        if held:
            out.append(name)
        elif held is False and reap:
            try:
                os.unlink(path)
            except OSError:
                pass
    return out


def holders(pool: str, n: Optional[int] = None) -> List[dict]:
    """Records of the live holders (by pid) of slots ``0..n-1`` (every slot
    file when ``n`` is None), slot order."""
    directory = pool_dir(pool)
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in sorted(names):
        m = re.match(r"^slot-(\d+)\.json$", name)
        if not m or (n is not None and int(m.group(1)) >= n):
            continue
        rec = _read_json(os.path.join(directory, name))
        if rec and _pid_alive(rec.get("pid")):
            out.append(dict(rec, slot=int(m.group(1))))
    return out


def acquire(
    pool: str,
    n: int,
    *,
    record: Optional[dict] = None,
    timeout: float = 0.0,
    on_wait: Optional[Callable[[List[dict], int], None]] = None,
    poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[Slot]:
    """Block until one of ``n`` slots is ours; None after ``timeout`` seconds
    (``0`` = no limit). ``on_wait(holders, ahead)`` is called once, when we
    first have to wait."""
    n = max(1, int(n))
    directory = pool_dir(pool)
    _ensure_dir(directory)
    if not _live_waiters(directory):
        slot = try_acquire(pool, n, record)
        if slot is not None:
            return slot
    waiter = _Waiter(directory, dict(record or {}, waiting=True))
    start = time.monotonic()
    told = False
    # A little per-process jitter so waiters don't poll in lockstep.
    step = poll * (1.0 + (os.getpid() % 7) / 20.0)
    try:
        while True:
            queue = _live_waiters(directory)
            ahead = queue.index(waiter.name) if waiter.name in queue else 0
            if ahead < n:
                slot = try_acquire(pool, n, record)
                if slot is not None:
                    return slot
            if not told:
                told = True
                if on_wait is not None:
                    on_wait(holders(pool, n), ahead)
            if timeout and time.monotonic() - start >= timeout:
                return None
            sleep(step)
    finally:
        waiter.close()


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def _tmux_session_of(rec: dict) -> str:
    """The tmux session name of a record's pane ("" when not in tmux, or the
    lookup fails). Always ``-S socket -t pane``: a bare tmux call answers for
    whatever window the human has focused."""
    pane, sock = rec.get("tmux_pane") or "", rec.get("tmux_socket") or ""
    if not pane or not sock:
        return ""
    import subprocess

    try:
        cp = subprocess.run(
            [
                "tmux",
                "-S",
                sock,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_name}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


def _label(rec: dict) -> str:
    """``session "fix-auth"`` / ``tmux "dev"`` / ``a job in sitecheck-bot7``."""
    if rec.get("session"):
        return 'session "%s"' % rec["session"]
    name = _tmux_session_of(rec)
    if name.startswith("mindflock_"):
        name = name[len("mindflock_") :]
        if name.endswith("_sh"):
            name = name[: -len("_sh")]
        return 'session "%s"' % name
    if name:
        return 'tmux "%s"' % name
    cwd = rec.get("cwd") or ""
    return "a job in %s" % (os.path.basename(cwd.rstrip("/")) or cwd or "?")


def _short_cmd(text: str, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())  # one line, whatever the command
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clock(epoch) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(epoch)))
    except (TypeError, ValueError, OverflowError):
        return "?"


def waiting_line(pool: str, held: List[dict], ahead: int) -> str:
    """The ONE line printed while waiting."""
    what = "a heavy-job slot" if pool == DEFAULT_POOL else "a %r job slot" % pool
    if held:
        first = held[0]
        who = "%s, pid %s: %s since %s" % (
            _label(first),
            first.get("pid"),
            _short_cmd(first.get("cmd")),
            _clock(first.get("since")),
        )
        if len(held) > 1:
            who += " (+%d more)" % (len(held) - 1)
    else:
        who = "another job"
    tail = "; %d ahead of you" % ahead if ahead else ""
    return (
        "mindflock slot: waiting for %s (held by %s%s) — it runs when one frees; "
        "MINDFLOCK_SLOT=0 skips the gate" % (what, who, tail)
    )


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
def wait_for_memory(
    *,
    cap: float = MEMORY_MAX_WAIT_SECONDS,
    poll: float = MEMORY_POLL_SECONDS,
    err=None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Hold until :mod:`backend.memory_pressure` stops reading low/critical.
    True when memory was (or became) ok, False when ``cap`` ran out first —
    the caller then runs anyway: a misreading must never wedge every commit.
    ``unknown`` (non-Linux, guard off) never waits."""
    from backend import memory_pressure

    err = err if err is not None else sys.stderr
    if not memory_pressure.is_low(fresh=True):
        return True
    print(
        "mindflock slot: %s — holding until it recovers (at most %d min)"
        % (memory_pressure.describe(), int(cap // 60) or 1),
        file=err,
        flush=True,
    )
    start = time.monotonic()
    while memory_pressure.is_low(fresh=True):
        if time.monotonic() - start >= cap:
            print(
                "mindflock slot: running anyway after %d min held for memory (%s)"
                % (int(cap // 60) or 1, memory_pressure.describe() or "still low"),
                file=err,
                flush=True,
            )
            return False
        sleep(poll)
    return True


# --------------------------------------------------------------------------- #
# Running the command
# --------------------------------------------------------------------------- #
def _tty_delivered_sigint() -> bool:
    """Whether a SIGINT we got also reached the child straight from the
    terminal: we are in the terminal's foreground process group, and so is
    the child (it inherits our group). Forwarding it again would interrupt
    the child twice — pytest reads a second ^C as "abort teardown"."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd) and os.tcgetpgrp(fd) == os.getpgrp():
                return True
        except OSError:
            continue
    return False


def _exec(cmd: Sequence[str], err) -> int:
    """Replace this process with ``cmd`` (the bypass path). Returns only on
    failure: 127 not found, 126 not runnable — the shell's codes."""
    try:
        os.execvp(cmd[0], list(cmd))
    except FileNotFoundError:
        print("mindflock slot: command not found: %s" % cmd[0], file=err)
        return 127
    except OSError as e:
        print("mindflock slot: cannot run %s: %s" % (cmd[0], e.strerror or e), file=err)
        return 126
    return 126  # pragma: no cover — execvp never returns on success


def _run_child(
    cmd: Sequence[str], env: Dict[str, str], slot: Optional[Slot], err
) -> int:
    """Run ``cmd`` as our child, forwarding signals; release ``slot`` when it
    exits; return its exit code (a signal death is re-raised on ourselves)."""
    import subprocess

    forwarded = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    child: List[Optional[subprocess.Popen]] = [None]
    pending: List[int] = []

    def _forward(signum, _frame):
        proc = child[0]
        if proc is None:  # not started yet: it gets the signal once it is
            pending.append(signum)
            return
        if signum == signal.SIGINT and _tty_delivered_sigint():
            return
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    previous = {}
    for s in forwarded:
        try:
            previous[s] = signal.signal(s, _forward)
        except (OSError, ValueError):  # not the main thread (tests): no forwarding
            pass
    try:
        try:
            proc = subprocess.Popen(list(cmd), env=env)
        except FileNotFoundError:
            print("mindflock slot: command not found: %s" % cmd[0], file=err)
            return 127
        except OSError as e:
            print(
                "mindflock slot: cannot run %s: %s" % (cmd[0], e.strerror or e),
                file=err,
            )
            return 126
        child[0] = proc
        for s in pending:
            try:
                proc.send_signal(s)
            except OSError:
                pass
        rc = proc.wait()
    finally:
        if slot is not None:
            slot.release()
        for s, h in previous.items():
            try:
                signal.signal(s, h)
            except (OSError, ValueError):
                pass
    if rc < 0:
        # Died of a signal: die of the same one, so whoever waits on us
        # (pre-commit, a shell) sees exactly what it would have seen.
        sig = -rc
        try:
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
        except (OSError, ValueError):
            pass
        return 128 + sig
    return rc


def run(
    cmd: Sequence[str],
    *,
    pool: str = DEFAULT_POOL,
    n: Optional[int] = None,
    timeout: float = 0.0,
    wait_memory: bool = True,
    memory_cap: float = MEMORY_MAX_WAIT_SECONDS,
    err=None,
) -> int:
    """The body of ``mindflock slot``: gate, run, pass the exit code on."""
    err = err if err is not None else sys.stderr
    if not cmd:
        print("mindflock slot: no command given (mindflock slot -- CMD ...)", file=err)
        return 2
    held = [p for p in (os.environ.get(HELD_ENV) or "").split(",") if p]
    if os.environ.get("MINDFLOCK_SLOT", "1") == "0" or pool in held:
        # Bypassed, or nested inside a holder of this very pool (with one slot
        # the inner call would wait on its own parent forever).
        err.flush()
        return _exec(cmd, err)
    if n is None:
        n = int(configured_slots()["slots"])

    def _told(holding: List[dict], ahead: int) -> None:
        print(waiting_line(pool, holding, ahead), file=err, flush=True)

    try:
        slot = acquire(
            pool, n, record=_record(pool, n, cmd), timeout=timeout, on_wait=_told
        )
    except OSError as e:
        # A broken slot dir must never be why a commit fails.
        print("mindflock slot: gate unavailable (%s); running ungated" % e, file=err)
        slot = None
    else:
        if slot is None:
            print(
                "mindflock slot: running anyway after waiting %ds for a slot" % timeout,
                file=err,
                flush=True,
            )
    try:
        if wait_memory:
            wait_for_memory(cap=memory_cap, err=err)
    except BaseException:
        if slot is not None:
            slot.release()
        raise
    env = dict(os.environ)
    env[HELD_ENV] = ",".join(held + [pool])
    return _run_child(cmd, env, slot, err)


# --------------------------------------------------------------------------- #
# Status (the CLI's --status and GET /api/system/slots)
# --------------------------------------------------------------------------- #
def _pane_sessions(sockets: Sequence[str]) -> Dict[tuple, str]:
    """``{(socket, pane_id): tmux session name}`` via one ``list-panes -a``
    per socket (always ``-S``: never the ambient server)."""
    import subprocess

    out: Dict[tuple, str] = {}
    for sock in sorted(set(s for s in sockets if s)):
        if not os.path.exists(sock):
            continue
        try:
            cp = subprocess.run(
                [
                    "tmux",
                    "-S",
                    sock,
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_id}\t#{session_name}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if cp.returncode != 0:
            continue
        for line in (cp.stdout or "").splitlines():
            pane, _, name = line.partition("\t")
            if pane and name:
                out[(sock, pane)] = name
    return out


def _session_for(rec: dict, panes: Dict[tuple, str], sessions: Sequence[dict]) -> str:
    """The MindFlock session title a record belongs to, "" when none.
    ``sessions``: ``[{"title", "tmux_name", "folder"}]``."""
    if rec.get("session"):
        return str(rec["session"])
    name = panes.get((rec.get("tmux_socket") or "", rec.get("tmux_pane") or ""), "")
    if name:
        for want in (name, name[:-3] if name.endswith("_sh") else None):
            if not want:
                continue
            hits = [s["title"] for s in sessions if s.get("tmux_name") == want]
            if len(hits) == 1:
                return hits[0]
    cwd = os.path.realpath(rec.get("cwd") or "") if rec.get("cwd") else ""
    best, best_len = "", -1
    for s in sessions:
        folder = s.get("folder") or ""
        if not cwd or not folder:
            continue
        root = os.path.realpath(folder)
        if (cwd == root or cwd.startswith(root + os.sep)) and len(root) > best_len:
            best, best_len = str(s.get("title") or ""), len(root)
    return best


def _public(rec: dict) -> dict:
    keep = ("pid", "pool", "max", "cmd", "cwd", "since", "slot", "session")
    return {k: rec.get(k) for k in keep if k in rec}


def status(
    sessions: Optional[Sequence[dict]] = None, *, resolve_panes: bool = True
) -> dict:
    """Every pool's holders and waiters::

        {"slots": N, "source": "env"|"setting"|"default",
         "pools": [{"name", "holders": [{"slot", "pid", "cmd", "cwd",
                    "since", "max", "session"}], "waiters": [{...,
                    "position": 1}]}],
         "running": n, "waiting": n}

    A holder is a slot whose lock is held right now (its record may be
    missing for an instant after the take: ``pid`` None). ``sessions``
    (``[{"title", "tmux_name", "folder"}]``) maps a record to the MindFlock
    session it runs in — by baked title, tmux pane, then working folder.
    Never raises."""
    cfg = configured_slots()
    out: dict = {"slots": cfg["slots"], "source": cfg["source"], "pools": []}
    try:
        root = slots_root()
        pools = sorted(p for p in os.listdir(root) if valid_pool(p))
    except OSError:
        pools = []
    raw = []
    for pool in pools:
        directory = os.path.join(root, pool)
        hold, wait = [], []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            m = re.match(r"^slot-(\d+)\.lock$", name)
            if not m:
                continue
            if not _probe_locked(os.path.join(directory, name)):
                continue
            i = int(m.group(1))
            rec = _read_json(os.path.join(directory, "slot-%d.json" % i)) or {}
            if rec and not _pid_alive(rec.get("pid")):
                rec = {}
            hold.append(dict(rec, slot=i) if rec else {"slot": i, "pid": None})
        for pos, name in enumerate(_live_waiters(directory, reap=False), 1):
            rec = _read_json(os.path.join(directory, name)) or {}
            wait.append(dict(rec, position=pos))
        hold.sort(key=lambda r: r.get("slot", 0))
        raw.append((pool, hold, wait))
    panes: Dict[tuple, str] = {}
    if resolve_panes and sessions:
        panes = _pane_sessions(
            [r.get("tmux_socket") or "" for _, h, w in raw for r in h + w]
        )
    running = waiting = 0
    for pool, hold, wait in raw:
        if not hold and not wait:
            continue
        for r in hold + wait:
            r["session"] = _session_for(r, panes, sessions or [])
        out["pools"].append(
            {
                "name": pool,
                "holders": [_public(r) for r in hold],
                "waiters": [dict(_public(r), position=r["position"]) for r in wait],
            }
        )
        running += len(hold)
        waiting += len(wait)
    out["running"] = running
    out["waiting"] = waiting
    return out


def _print_status(out) -> int:
    st = status()
    print("heavy-job slots: %d (%s)" % (st["slots"], st["source"]), file=out)
    if not st["pools"]:
        print("nothing running or waiting", file=out)
    for p in st["pools"]:
        print("pool %s:" % p["name"], file=out)
        for h in p["holders"]:
            print(
                "  running  slot %s  pid %s  since %s  %s  (%s)"
                % (
                    h.get("slot"),
                    h.get("pid"),
                    _clock(h.get("since")),
                    h.get("cmd") or "?",
                    h.get("cwd") or "?",
                ),
                file=out,
            )
        for w in p["waiters"]:
            print(
                "  waiting  #%s     pid %s  since %s  %s  (%s)"
                % (
                    w.get("position"),
                    w.get("pid"),
                    _clock(w.get("since")),
                    w.get("cmd") or "?",
                    w.get("cwd") or "?",
                ),
                file=out,
            )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_VALUE_OPTS = ("--pool", "--max", "--timeout", "--memory-timeout")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mindflock slot",
        description=(
            "Run CMD once it holds one of N machine-wide slots for a pool (and "
            "memory reads ok), so heavy jobs — test suites, builds, indexers — "
            "from many sessions take turns instead of running the machine out "
            "of memory together. No server needed. MINDFLOCK_SLOT=0 bypasses it."
        ),
        usage="mindflock slot [options] -- CMD [ARGS...]",
    )
    p.add_argument(
        "--pool", default=DEFAULT_POOL, help="slot pool name (default: heavy)"
    )
    p.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="slots in the pool (default: general.heavy_slots, else 1 below 32 GB RAM, 2 above)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECS",
        help="stop waiting for a slot after SECS and run anyway (default 0 = wait for one)",
    )
    p.add_argument(
        "--wait-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="after taking the slot, hold while memory reads low (default on; capped)",
    )
    p.add_argument(
        "--memory-timeout",
        type=float,
        default=MEMORY_MAX_WAIT_SECONDS,
        metavar="SECS",
        help="longest memory hold before running anyway (default 600)",
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="print who holds and waits for slots, and exit",
    )
    return p


def split_argv(argv: Sequence[str]) -> tuple:
    """``(our options, CMD)``: CMD starts after ``--``, or at the first word
    that is neither an option nor an option's value."""
    argv = list(argv)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return argv[:i], argv[i + 1 :]
        if not a.startswith("-"):
            return argv[:i], argv[i:]
        if a in _VALUE_OPTS:
            i += 2
            continue
        i += 1
    return argv, []


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[2:] if argv is None else argv)
    opts, cmd = split_argv(argv)
    args = build_parser().parse_args(opts)
    if args.status:
        return _print_status(sys.stdout)
    if not valid_pool(args.pool):
        print("mindflock slot: bad pool name %r" % args.pool, file=sys.stderr)
        return 2
    if args.max is not None and args.max < 1:
        print("mindflock slot: --max must be at least 1", file=sys.stderr)
        return 2
    try:
        return run(
            cmd,
            pool=args.pool,
            n=args.max,
            timeout=max(0.0, args.timeout),
            wait_memory=args.wait_memory,
            memory_cap=max(0.0, args.memory_timeout),
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # python -m backend.slot -- CMD ...
    raise SystemExit(main(sys.argv[1:]))
