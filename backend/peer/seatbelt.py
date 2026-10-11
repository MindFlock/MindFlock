"""macOS Seatbelt (``sandbox-exec``) sandbox for a peer-link shared session.

The Linux sandbox (:mod:`backend.peer.sandbox`) builds a private filesystem out
of bubblewrap binds. macOS has no mount namespaces, so here the agent runs on
the real filesystem under a deny-by-default Seatbelt profile that names what it
may touch — the same set the bwrap binds expose, at the same absolute paths:

* system code, read-only: Apple's ``system.sb`` baseline (``/System``,
  ``/usr/lib``, ``/usr/share``, dyld, a few core services) plus ``/usr/bin``,
  ``/usr/sbin``, ``/usr/libexec``, ``/bin``, ``/sbin``, the shell selector, the
  Command Line Tools (``/usr/bin/git`` is a shim into them) and a small
  ``/private/etc`` allow-list;
* the agent's own runtime (executable, install dir, interpreter, Python, the
  ``backend`` package; Homebrew's ``Cellar``/``opt``/``lib`` when the CLI or
  its interpreter comes from Homebrew), read-only;
* the share: ``work/`` read-write except its ``.git`` gitfile, ``repo.git/``
  read-only, ``home/`` read-write (``TMPDIR`` lives in it), ``run/``
  read-only (its sockets stay connectable);
* the pane's own tty.

Everything else is denied, including:

* reading the rest of the user's files. Only the ancestors of the allowed
  paths can be ``stat``-ed;
* writing anywhere else (``/tmp`` included);
* network, except binding/connecting ``localhost:<bridge port>`` (the
  in-sandbox HTTP bridge) and the two unix sockets in ``run/``. There is no
  DNS and no other host or local port, the MindFlock server included;
* reading other processes' argv/environment (``kern.procargs*`` sysctls,
  which Apple's baseline allows) or task info;
* the keychain and every other mach service ``system.sb`` doesn't list.

The pane stays OUT of the sandbox's session: :mod:`backend.peer.sandbox_exec`
forks, the child ``setsid()``s (so it has no controlling tty, and
``TIOCSTI`` keystroke injection into the pane fails) and execs
``sandbox-exec``, and the parent forwards resize and signals to it.

See ``docs/peer-link.md`` ("The sandbox — CONTRACT").
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: System locations every CLI needs (Apple's ``system.sb`` covers the rest of
#: the OS: /System, /usr/lib, /usr/share, dyld, timezone data).
SYSTEM_READ_SUBPATHS = (
    "/usr/bin",
    "/usr/sbin",
    "/usr/libexec",
    "/bin",
    "/sbin",
    "/private/var/select",  # /bin/sh is a shim that reads its target here
    "/Library/Developer/CommandLineTools",  # /usr/bin/git, python3, … shims
    "/private/etc/ssl",
)
SYSTEM_READ_LITERALS = (
    "/private/var/db/xcode_select_link",
    "/private/etc/group",
    "/private/etc/hosts",
    "/private/etc/shells",
    "/private/etc/localtime",
)
#: Homebrew prefixes: a CLI (or its interpreter) installed there links
#: dylibs from ``<prefix>/opt`` → ``<prefix>/Cellar``.
HOMEBREW_PREFIXES = ("/opt/homebrew", "/usr/local")
HOMEBREW_READ = ("Cellar", "opt", "lib")

_SAFE_CHARS = re.compile(r"^[^\x00-\x1f\x7f\"\\]+\Z")


class SeatbeltError(Exception):
    """The profile can't be built safely. Callers fail closed."""


def _q(s: str) -> str:
    """An SBPL string literal. Paths come from the share layout and the
    runtime discovery — refuse anything that would need escaping rather than
    trust an escaper with the profile."""
    if not isinstance(s, str) or not _SAFE_CHARS.match(s):
        raise SeatbeltError(f"unsafe path for the sandbox profile: {s!r}")
    return '"' + s + '"'


def available() -> tuple[bool, str]:
    """``(True, path)`` when ``sandbox-exec`` exists AND enforces here (a real
    deny of a file read is observed), else ``(False, reason)``."""
    if sys.platform != "darwin":
        return False, "not macOS"
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return False, f"{SANDBOX_EXEC} not found"
    with tempfile.TemporaryDirectory(prefix="mf-sbx-") as tmp:
        probe = os.path.join(os.path.realpath(tmp), "probe")
        with open(probe, "w") as fh:
            fh.write("x")
        profile = (
            "(version 1)(allow default)" f"(deny file-read-data (literal {_q(probe)}))"
        )
        try:
            allowed = subprocess.run(
                [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/bin/cat", probe],
                capture_output=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
            denied = subprocess.run(
                [SANDBOX_EXEC, "-p", profile, "/bin/cat", probe],
                capture_output=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"sandbox-exec self-test failed: {type(exc).__name__}"
    if allowed.returncode != 0 or allowed.stdout != b"x":
        msg = allowed.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, "sandbox-exec self-test failed: " + (
            msg[-1][:200] if msg else f"exit {allowed.returncode}"
        )
    if denied.returncode == 0 or denied.stdout:
        return False, "sandbox-exec self-test failed: a denied read went through"
    return True, SANDBOX_EXEC


def homebrew_paths(paths: list[str]) -> list[str]:
    """``<prefix>/{Cellar,opt,lib}`` for each Homebrew prefix one of
    ``paths`` lives under (only dirs that exist)."""
    out: list[str] = []
    for p in paths:
        for prefix in HOMEBREW_PREFIXES:
            if p == prefix or p.startswith(prefix + "/"):
                for sub in HOMEBREW_READ:
                    d = os.path.join(prefix, sub)
                    if os.path.isdir(d) and d not in out:
                        out.append(os.path.realpath(d))
    return out


def build_profile(
    *,
    dirs: dict,
    runtime: list[str],
    bridge_port: int,
    tty: str | None = None,
) -> str:
    """The SBPL profile for one shared session.

    ``dirs`` is :func:`backend.peer.sandbox.share_dirs` (canonical, checked);
    ``runtime`` the read-only runtime paths (realpaths, a file or a dir);
    ``tty`` the pane's tty device (``/dev/ttysNNN``) or None.
    """
    if not isinstance(bridge_port, int) or not 1024 <= bridge_port <= 65535:
        raise SeatbeltError("bad bridge port")
    work, gitfile = dirs["work"], dirs["gitfile"]
    home, run, gitdir = dirs["home"], dirs["run"], dirs["gitdir"]

    reads: list[str] = []
    ancestors: list[str] = []
    for p in runtime:
        reads.append(f"(subpath {_q(p)})" if os.path.isdir(p) else f"(literal {_q(p)})")
        ancestors.append(f"(path-ancestors {_q(p)})")
    for p in (work, gitdir, home, run):
        reads.append(f"(subpath {_q(p)})")
        ancestors.append(f"(path-ancestors {_q(p)})")
    sys_reads = [f"(subpath {_q(p)})" for p in SYSTEM_READ_SUBPATHS]
    sys_reads += [f"(literal {_q(p)})" for p in SYSTEM_READ_LITERALS]

    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        # system.sb allows every sysctl read, which includes other
        # processes' argv + environment (kern.procargs2) and the process
        # table. Take those back; the agent may only look at its own tree.
        "(deny sysctl-read"
        ' (sysctl-name-prefix "kern.procargs")'
        ' (sysctl-name-prefix "kern.proc."))',
        "(deny process-info*)",
        "(allow process-info* (target same-sandbox))",
        "(allow process-fork)",
        "(allow process-exec)",  # only what it may read is reachable at all
        "(allow signal (target same-sandbox))",
        # New ptys for tools that open their own (script, expect, a pty lib).
        "(allow pseudo-tty)",
        '(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))',
        "(allow file-read* file-write* file-ioctl"
        ' (require-all (regex #"^/dev/ttys[0-9]+$")'
        ' (extension "com.apple.sandbox.pty")))',
        '(allow file-ioctl (literal "/dev/null") (literal "/dev/zero"))',
        "(allow ipc-posix-sem)",  # Python multiprocessing locks
        "(allow file-read* " + " ".join(sys_reads) + ")",
        "(allow file-read* " + " ".join(reads) + ")",
        "(allow file-read-metadata " + " ".join(ancestors) + ")",
        # work/ read-write except the gitfile (repo.git stays read-only: it
        # is only ever READ above); home/ read-write. Neither top-level dir
        # itself may be removed or renamed.
        "(allow file-write*"
        f" (require-all (subpath {_q(work)})"
        f" (require-not (literal {_q(work)}))"
        f" (require-not (literal {_q(gitfile)})))"
        f" (require-all (subpath {_q(home)})"
        f" (require-not (literal {_q(home)}))))",
        "(allow network-bind network-inbound" f' (local ip "localhost:{bridge_port}"))',
        "(allow network-outbound"
        f' (remote ip "localhost:{bridge_port}")'
        f" (remote unix-socket (path-literal {_q(os.path.join(run, 'egress.sock'))}))"
        f" (remote unix-socket (path-literal {_q(os.path.join(run, 'agent.sock'))})))",
    ]
    if tty:
        if not re.fullmatch(r"/dev/ttys[0-9]{1,6}", tty):
            raise SeatbeltError(f"unexpected tty {tty!r}")
        lines.append(f"(allow file-read* file-write* file-ioctl (literal {_q(tty)}))")
    return "\n".join(lines) + "\n"
