"""Bubblewrap sandbox for a peer-link shared session.

The shared-folder agent is the one process a remote peer can talk to, so it
is assumed hostile once prompt-injected. It runs under ``bwrap`` with its own
mount, PID, IPC, UTS, cgroup, user and network namespaces, every capability
dropped and nested user namespaces disabled. Inside it sees only:

* ``/usr`` (+ the merged-usr symlinks) and a small ``/etc`` allow-list, all
  read-only;
* a fresh ``/proc``, a minimal ``/dev``, and tmpfs ``/tmp``, ``/home``,
  ``/run`` (and ``/root``, ``/mnt``, ``/media``, ``/srv``);
* the agent's own runtime (executable, install dir, interpreter, Python, the
  ``backend`` package), read-only;
* the share: ``work/`` read-write, ``work/.git`` and ``repo.git/`` read-only
  (bound *after* ``work/`` so they cover it), ``home/`` read-write and
  ``run/`` read-only (its sockets stay connectable).

The environment is cleared and rebuilt from a whitelist. Network goes only
through ``run/egress.sock`` (see :mod:`backend.peer.egress`) via the
in-sandbox :mod:`backend.peer.bridge`.

See ``docs/peer-link.md`` ("The sandbox — CONTRACT").
"""

from __future__ import annotations

import functools
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys

__all__ = [
    "SandboxError",
    "PROFILES",
    "DEFAULT_BRIDGE_PORT",
    "available",
    "find_bwrap",
    "build_argv",
    "build_options",
    "prepare_home",
    "egress_allow",
    "safe_write",
    "seccomp_program",
    "open_seccomp_fd",
    "share_dirs",
]

DEFAULT_BRIDGE_PORT = 3128
FALLBACK_BWRAP = "~/.local/opt/bwrap/root/usr/bin/bwrap"
UNAVAILABLE_OS = "peer sandbox needs Linux + bubblewrap"

ETC_ALLOW = (
    "ssl",
    "ca-certificates",
    "passwd",
    "group",
    "hosts",
    "nsswitch.conf",
    "localtime",
    "alternatives",
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "resolv.conf",
)
MERGED_USR = ("bin", "sbin", "lib", "lib64", "lib32", "libx32")
EXTRA_TMPFS = ("root", "mnt", "media", "srv")
# In-sandbox dir (on the /run tmpfs) holding symlinks to the agent runtime.
SANDBOX_BIN = "/run/mindflock-bin"

PROFILES: dict[str, dict] = {
    "claude": {
        "bin": "claude",
        "egress": [
            "api.anthropic.com",
            "console.anthropic.com",
            "platform.claude.com",
            "claude.ai",
            "statsig.anthropic.com",
        ],
        "passthrough": ["ANTHROPIC_API_KEY"],
    },
    "codex": {
        "bin": "codex",
        "egress": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
        "passthrough": ["OPENAI_API_KEY"],
    },
}

# Never let these into the sandbox, even through the explicit ``env`` dict.
_ENV_DENY_EXACT = {
    "TMUX",
    "TMUX_PANE",
    "TMUX_TMPDIR",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "XDG_RUNTIME_DIR",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "WSL_INTEROP",
    "MINDFLOCK_AUTH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
}
_ENV_DENY_PREFIX = (
    "TMUX_",
    "DBUS_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCP_",
    "GPG_",
    "GNUPG",
    "KUBE",
    "DOCKER_",
    "MINDFLOCK_AUTH",
    "MINDFLOCK_WEB",
    "CS_WEB",
    "LD_",
)
# Set by build_argv itself; the explicit env may not override them.
_ENV_RESERVED = {
    "HOME",
    "PATH",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "ALL_PROXY",
    "all_proxy",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
}
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_LC_RE = re.compile(r"^LC_[A-Z_]{1,32}\Z")
_HOST_RE = re.compile(
    r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]\Z"
)

_MAX_SOURCE_JSON = 64 * 1024 * 1024
_MAX_CREDENTIAL = 1024 * 1024


class SandboxError(Exception):
    """The sandbox can't be built safely. Callers fail closed."""


# --------------------------------------------------------------------------
# availability


def find_bwrap() -> str | None:
    """``$MINDFLOCK_BWRAP``, then ``PATH``, then the per-user fallback."""
    cand = os.environ.get("MINDFLOCK_BWRAP", "").strip()
    if cand:
        return cand if os.path.isabs(cand) and os.access(cand, os.X_OK) else None
    found = shutil.which("bwrap")
    if found:
        return os.path.abspath(found)
    fb = os.path.expanduser(FALLBACK_BWRAP)
    if os.path.isfile(fb) and os.access(fb, os.X_OK):
        return fb
    return None


def available() -> tuple[bool, str]:
    """``(True, path)`` when bubblewrap works here, else ``(False, reason)``."""
    if not sys.platform.startswith("linux"):
        return False, UNAVAILABLE_OS
    bwrap = find_bwrap()
    if not bwrap:
        return False, "bubblewrap (bwrap) not found; set MINDFLOCK_BWRAP or install it"
    try:
        proc = subprocess.run(
            [bwrap, "--unshare-all", "--ro-bind", "/", "/", "true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"bwrap self-test failed: {type(exc).__name__}"
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, "bwrap self-test failed: " + (
            msg[-1][:200] if msg else f"exit {proc.returncode}"
        )
    return True, bwrap


@functools.lru_cache(maxsize=8)
def _bwrap_supports(bwrap: str, flag: str) -> bool:
    try:
        out = subprocess.run(
            [bwrap, "--help"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return flag.encode() in out.stdout + out.stderr


def _tiocsti_disabled() -> bool:
    try:
        with open("/proc/sys/dev/tty/legacy_tiocsti") as fh:
            return fh.read().strip() == "0"
    except OSError:
        return False


# --------------------------------------------------------------------------
# seccomp
#
# A small classic-BPF filter, loaded by ``bwrap --seccomp FD``. It blocks the
# TTY input-injection ioctls (TIOCSTI, TIOCLINUX) so the sandbox can keep the
# tmux pane as its controlling terminal (``--new-session`` would stop SIGWINCH,
# breaking TUI resize), plus a few syscalls that are pure kernel attack
# surface for a coding agent (the same ones Docker's default profile blocks).
# socket() is limited to AF_UNIX/INET/INET6/NETLINK: network namespaces do
# not cover AF_VSOCK, which reaches the hypervisor host (on WSL2 a vsock
# connect to CID 2 succeeds from inside a fresh netns) and would bypass the
# egress proxy. Any non-native ABI (i386 via int 0x80, x32) gets ENOSYS for
# everything.

_SECCOMP_ARCH = {
    # machine: (AUDIT_ARCH, ioctl nr, socket nr, {blocked nr}, x32 bit check)
    "x86_64": (
        0xC000003E,
        16,
        41,
        {250, 248, 249, 321, 298, 323, 246, 320, 175, 313, 176, 425, 426, 427},
        True,
    ),
    "aarch64": (
        0xC00000B7,
        29,
        198,
        {219, 217, 218, 280, 241, 282, 104, 294, 105, 273, 106, 425, 426, 427},
        False,
    ),
}
_TIOCSTI = 0x5412
_TIOCLINUX = 0x541C
_EPERM = 1
_ENOSYS = 38
_EAFNOSUPPORT = 97
_SOCKET_FAMILIES = (1, 2, 10, 16)  # AF_UNIX, AF_INET, AF_INET6, AF_NETLINK


def seccomp_program(machine: str | None = None) -> bytes | None:
    """The compiled filter (``struct sock_filter[]``) for this machine, or
    None when the architecture isn't supported (then build_options falls
    back to ``--new-session``)."""
    import struct

    machine = machine or os.uname().machine
    spec = _SECCOMP_ARCH.get(machine)
    if spec is None:
        return None
    audit_arch, ioctl_nr, socket_nr, blocked, x32 = spec
    LD_ABS, JEQ, JGE, RET = 0x20, 0x15, 0x35, 0x06
    ALLOW = 0x7FFF0000
    # Assemble with symbolic jump targets, then resolve to relative offsets.
    prog: list[tuple] = [
        (LD_ABS, 0, 0, 4),  # seccomp_data.arch
        (JEQ, 0, "nosys", audit_arch),
        (LD_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    if x32:
        prog.append((JGE, "nosys", 0, 0x40000000))
    prog.append((JEQ, "ioctl", 0, ioctl_nr))
    prog.append((JEQ, "socket", 0, socket_nr))
    for nr in sorted(blocked):
        prog.append((JEQ, "eperm", 0, nr))
    prog.append((RET, 0, 0, ALLOW))
    labels = {"ioctl": len(prog)}
    prog += [
        (LD_ABS, 0, 0, 24),  # low 32 bits of args[1] (the ioctl request; LE)
        (JEQ, "eperm", 0, _TIOCSTI),
        (JEQ, "eperm", 0, _TIOCLINUX),
        (RET, 0, 0, ALLOW),
    ]
    labels["socket"] = len(prog)
    prog.append((LD_ABS, 0, 0, 16))  # low 32 bits of args[0] (the family)
    for fam in _SOCKET_FAMILIES:
        prog.append((JEQ, "allow", 0, fam))
    prog.append((RET, 0, 0, 0x00050000 | _EAFNOSUPPORT))
    labels["allow"] = len(prog)
    prog.append((RET, 0, 0, ALLOW))
    labels["eperm"] = len(prog)
    prog.append((RET, 0, 0, 0x00050000 | _EPERM))
    labels["nosys"] = len(prog)
    prog.append((RET, 0, 0, 0x00050000 | _ENOSYS))
    out = b""
    for i, (code, jt, jf, k) in enumerate(prog):
        jt = labels[jt] - i - 1 if isinstance(jt, str) else jt
        jf = labels[jf] - i - 1 if isinstance(jf, str) else jf
        if not (0 <= jt < 256 and 0 <= jf < 256):
            raise SandboxError("seccomp jump out of range")
        out += struct.pack("=HBBI", code, jt, jf, k)
    return out


def open_seccomp_fd() -> int | None:
    """An inheritable memfd holding :func:`seccomp_program`, positioned at 0,
    for ``build_options(..., seccomp_fd=fd)``; None if unsupported. The
    caller closes it (or lets the exec inherit it)."""
    prog = seccomp_program()
    if prog is None:
        return None
    fd = os.memfd_create("mindflock-seccomp", 0)
    os.write(fd, prog)
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return fd


# --------------------------------------------------------------------------
# share validation


def share_dirs(share) -> dict:
    """Normalize a Share object or dict into checked absolute paths.

    Every share dir must be a real directory (no symlink anywhere in its
    path), owned by us, inside ``paths.shares_dir()``; ``work/.git`` must be a
    regular file. Anything else raises :class:`SandboxError`.
    """
    from backend.peer import paths

    def get(key):
        val = share.get(key) if isinstance(share, dict) else getattr(share, key, None)
        if not isinstance(val, str) or not val:
            raise SandboxError(f"share has no {key}")
        return val

    d = {k: get(k) for k in ("root", "work", "gitdir", "home", "run")}
    shares = os.path.realpath(paths.shares_dir())
    root = d["root"]
    if not (os.path.isabs(root) and os.path.realpath(root) == root):
        raise SandboxError("share root must be a canonical absolute path")
    if os.path.dirname(root) != shares or not paths.SHARE_ID_RE.match(
        os.path.basename(root)
    ):
        raise SandboxError("share root is not under the peer shares dir")
    uid = os.getuid()
    for key, p in d.items():
        if os.path.realpath(p) != p:
            raise SandboxError(f"share {key} is not a canonical path")
        if key != "root" and os.path.dirname(p) != root:
            raise SandboxError(f"share {key} is not directly under the share root")
        try:
            st = os.lstat(p)
        except OSError:
            raise SandboxError(f"share {key} is missing") from None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
            raise SandboxError(f"share {key} is not a directory we own")
    gitfile = os.path.join(d["work"], ".git")
    try:
        st = os.lstat(gitfile)
    except OSError:
        raise SandboxError("share work/.git is missing") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
        raise SandboxError("share work/.git is not a regular gitfile")
    d["gitfile"] = gitfile
    return d


# --------------------------------------------------------------------------
# agent runtime discovery


def _user_home() -> str:
    try:
        return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        return os.path.realpath(os.path.expanduser("~"))


def _generic_dirs() -> set[str]:
    homes = {_user_home(), os.path.realpath(os.path.expanduser("~"))}
    out = {"/", "/usr", "/usr/local", "/opt", "/usr/bin", "/usr/local/bin", "/bin"}
    for h in homes:
        out |= {
            h,
            h + "/.local",
            h + "/.local/bin",
            h + "/bin",
            h + "/.local/share",
            h + "/.local/lib",
        }
    return out


def _install_dir(real: str) -> str | None:
    """The directory an executable was installed into, or None when it sits
    in a generic bin dir (then only the file itself is bound)."""
    parts = real.split(os.sep)
    if "node_modules" in parts:
        idx = len(parts) - 1 - parts[::-1].index("node_modules")
        end = idx + 2
        if end < len(parts) and parts[idx + 1].startswith("@"):
            end += 1
        if end < len(parts):
            return os.sep.join(parts[:end])
    d = os.path.dirname(real)
    generic = _generic_dirs()
    if os.path.basename(d) == "bin" and os.path.dirname(d) not in generic:
        d = os.path.dirname(d)
    return None if d in generic else d


def _shebang(path: str) -> list[str] | None:
    try:
        with open(path, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    line = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    return line.split() or None


def _interpreter(script: str, host_path: str) -> str | None:
    sb = _shebang(script)
    if not sb:
        return None
    prog, args = sb[0], sb[1:]
    if os.path.basename(prog) == "env":
        names = [a for a in args if not a.startswith("-") and "=" not in a]
        if not names:
            return None
        found = shutil.which(names[0], path=host_path)
        return os.path.realpath(found) if found else None
    return (
        os.path.realpath(prog) if os.path.isabs(prog) and os.path.exists(prog) else None
    )


def _check_runtime_path(p: str, share_root: str) -> None:
    """Refuse binding anything that would expose more than the runtime."""
    from backend.peer import paths

    if not os.path.isabs(p) or not os.path.exists(p):
        raise SandboxError(f"runtime path missing: {p}")
    if p == "/" or os.path.dirname(p) == "/" and p != "/usr":
        raise SandboxError(f"refusing to expose top-level dir {p}")
    for bad in ("/proc", "/sys", "/dev", "/run", "/etc", "/var/run", "/boot"):
        if p == bad or p.startswith(bad + "/"):
            raise SandboxError(f"refusing to expose {p}")
    protect = [
        _user_home(),
        os.path.realpath(os.path.expanduser("~")),
        paths.peer_root(),
        share_root,
    ]
    for q in protect:
        if q == p or q.startswith(p.rstrip("/") + "/"):
            raise SandboxError(f"refusing to expose {p}: it contains {q}")
    root = paths.peer_root()
    if p.startswith(root + "/"):
        raise SandboxError(f"refusing to expose {p}: inside the peer root")


def _under(p: str, base: str) -> bool:
    return p == base or p.startswith(base.rstrip("/") + "/")


def _runtime(provider: str, share_root: str) -> tuple[list[str], dict[str, str]]:
    """Read-only paths for the agent runtime, and in-sandbox PATH symlinks
    ``{name: realpath}``."""
    prof = PROFILES[provider]
    host_path = os.environ.get("PATH", os.defpath)
    exe = shutil.which(prof["bin"], path=host_path)
    if not exe:
        raise SandboxError(f"{prof['bin']} not found on PATH")
    real = os.path.realpath(exe)
    binds = [real]
    links = {prof["bin"]: real}
    inst = _install_dir(real)
    if inst:
        binds.append(inst)
    interp = _interpreter(real, host_path)
    if interp:
        binds.append(interp)
        links[os.path.basename(interp)] = interp
        sb = _shebang(real) or []
        if sb and os.path.basename(sb[0]) == "env":
            names = [a for a in sb[1:] if not a.startswith("-") and "=" not in a]
            if names:
                links[names[0]] = interp
        idir = _install_dir(interp)
        if idir:
            binds.append(idir)
    py = os.path.realpath(sys.executable)
    binds += [py, os.path.realpath(sys.base_prefix), os.path.realpath(sys.prefix)]
    links.setdefault("python3", py)
    # Only the backend package itself, never the checkout/site dir around it
    # (a dev checkout holds config.toml, state.json, logs/ ...).
    binds.append(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

    out: list[str] = []
    for p in binds:
        p = os.path.realpath(p)
        if _under(p, "/usr"):
            continue
        _check_runtime_path(p, share_root)
        if p not in out:
            out.append(p)
    # Drop paths already covered by a bound ancestor.
    out = [p for p in out if not any(q != p and _under(p, q) for q in out)]
    return out, links


# --------------------------------------------------------------------------
# argv


def _env_ok(key: str) -> bool:
    if not _ENV_KEY_RE.match(key):
        return False
    up = key.upper()
    if up in _ENV_DENY_EXACT or key in _ENV_RESERVED:
        return False
    return not any(up.startswith(p) for p in _ENV_DENY_PREFIX)


def build_argv(
    share,
    inner_argv: list[str],
    provider: str,
    env: dict | None = None,
    *,
    bridge_port: int = DEFAULT_BRIDGE_PORT,
    bwrap: str | None = None,
    seccomp_fd: int | None = None,
) -> list[str]:
    """The full ``bwrap … -- <inner_argv>`` argv for a shared session."""
    exe, opts = build_options(
        share,
        inner_argv,
        provider,
        env,
        bridge_port=bridge_port,
        bwrap=bwrap,
        seccomp_fd=seccomp_fd,
    )
    return [exe, *opts, "--", *inner_argv]


def build_options(
    share,
    inner_argv: list[str],
    provider: str,
    env: dict | None = None,
    *,
    bridge_port: int = DEFAULT_BRIDGE_PORT,
    bwrap: str | None = None,
    seccomp_fd: int | None = None,
) -> tuple[str, list[str]]:
    """``(bwrap path, options)`` — :func:`build_argv` without the inner
    command, for callers that pass the options via ``--args FD``."""
    if provider not in PROFILES:
        raise SandboxError(f"provider {provider} has no sandbox profile")
    if not inner_argv or not all(
        isinstance(a, str) and "\0" not in a for a in inner_argv
    ):
        raise SandboxError("bad inner argv")
    if not isinstance(bridge_port, int) or not 1024 <= bridge_port <= 65535:
        raise SandboxError("bad bridge port")
    bwrap = bwrap or find_bwrap()
    if not bwrap:
        raise SandboxError("bubblewrap (bwrap) not found")
    d = share_dirs(share)
    runtime, links = _runtime(provider, d["root"])

    a: list[str] = ["--die-with-parent", "--unshare-all", "--cap-drop", "ALL"]
    if _bwrap_supports(bwrap, "--disable-userns"):
        a += ["--unshare-user", "--disable-userns"]
    if seccomp_fd is not None:
        # The filter blocks TIOCSTI/TIOCLINUX, so the pane can stay the
        # controlling tty (keeps SIGWINCH / TUI resize working).
        a += ["--seccomp", str(seccomp_fd)]
    elif not _tiocsti_disabled():
        a.append("--new-session")
    a += ["--hostname", "mindflock-peer"]

    # System: /usr, merged-usr links, the /etc allow-list.
    a += ["--ro-bind", "/usr", "/usr"]
    for name in MERGED_USR:
        p = "/" + name
        if os.path.islink(p):
            a += ["--symlink", os.readlink(p), p]
        elif os.path.isdir(p):
            a += ["--ro-bind", p, p]
    for name in ETC_ALLOW:
        p = "/etc/" + name
        if os.path.islink(p):
            a += ["--symlink", os.readlink(p), p]
        elif os.path.exists(p):
            a += ["--ro-bind", p, p]
    a += ["--proc", "/proc", "--dev", "/dev"]
    for p in ("/tmp", "/home", "/run"):
        a += ["--tmpfs", p]
    for name in EXTRA_TMPFS:
        if os.path.isdir("/" + name):
            a += ["--tmpfs", "/" + name]
    a += ["--dir", SANDBOX_BIN]
    for name, target in sorted(links.items()):
        a += ["--symlink", target, f"{SANDBOX_BIN}/{name}"]

    # Agent runtime, read-only.
    for p in runtime:
        a += ["--ro-bind", p, p]

    # The share. The read-only gitfile/gitdir binds MUST follow the rw work
    # bind so they cover it.
    a += ["--bind", d["work"], d["work"]]
    a += ["--ro-bind", d["gitfile"], d["gitfile"]]
    a += ["--ro-bind", d["gitdir"], d["gitdir"]]
    a += ["--bind", d["home"], d["home"]]
    a += ["--ro-bind", d["run"], d["run"]]

    # Environment: cleared, then whitelisted.
    proxy = f"http://127.0.0.1:{bridge_port}"
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = "peer"
    path = ":".join(
        [
            SANDBOX_BIN,
            os.path.dirname(sys.executable),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
    )
    final: dict[str, str] = {
        "HOME": d["home"],
        "PATH": path,
        "TMPDIR": "/tmp",
        "USER": user,
        "LOGNAME": user,
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }
    for key, val in os.environ.items():
        if key in ("TERM", "COLORTERM", "LANG", "TZ") or _LC_RE.match(key):
            final[key] = val
    if provider == "claude":
        final["CLAUDE_CONFIG_DIR"] = os.path.join(d["home"], ".claude")
        final["DISABLE_AUTOUPDATER"] = "1"
    elif provider == "codex":
        final["CODEX_HOME"] = os.path.join(d["home"], ".codex")
    for key in PROFILES[provider]["passthrough"]:
        if os.environ.get(key):
            final[key] = os.environ[key]
    for key, val in (env or {}).items():
        if (
            not isinstance(key, str)
            or not isinstance(val, str)
            or "\0" in val
            or not _env_ok(key)
        ):
            raise SandboxError(f"env var {key!r} may not enter the sandbox")
        final[key] = val
    a.append("--clearenv")
    for key, val in final.items():
        if "\0" in val:
            raise SandboxError(f"env var {key} contains NUL")
        a += ["--setenv", key, val]

    a += ["--chdir", d["work"]]
    return bwrap, a


# --------------------------------------------------------------------------
# provider home


def safe_write(base: str, rel: str, data: bytes, mode: int = 0o600) -> str:
    """Write ``base/rel`` without following any symlink under ``base``.

    ``base`` is trusted; everything below it may have been shaped by the
    sandboxed agent (``home/`` is writable inside), so each component is
    opened with ``O_NOFOLLOW`` and the leaf is replaced atomically by
    ``rename`` (which replaces a planted symlink instead of writing through
    it).
    """
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise SandboxError("bad relative path")
    uid = os.getuid()
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for comp in parts[:-1]:
            try:
                os.mkdir(comp, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                nfd = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError:
                raise SandboxError(f"{comp} is not a plain directory") from None
            os.close(fd)
            fd = nfd
            if os.fstat(fd).st_uid != uid:
                raise SandboxError(f"{comp} is not ours")
            os.fchmod(fd, 0o700)
        leaf = parts[-1]
        tmp = f".{leaf}.mf-{secrets.token_hex(6)}"
        wfd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=fd,
        )
        try:
            os.fchmod(wfd, mode)
            view = memoryview(data)
            while view:
                n = os.write(wfd, view)
                view = view[n:]
            os.fsync(wfd)
        finally:
            os.close(wfd)
        try:
            os.replace(tmp, leaf, src_dir_fd=fd, dst_dir_fd=fd)
        except OSError:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise SandboxError(f"cannot replace {leaf}") from None
    finally:
        os.close(fd)
    return os.path.join(base, *parts)


def _read_capped(path: str, cap: int) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read(cap + 1)
    except OSError:
        return None
    return data if len(data) <= cap else None


def _claude_minimal_json(work: str) -> bytes:
    src = os.environ.get("MINDFLOCK_CLAUDE_JSON", "").strip()
    if not src:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        src = (
            os.path.join(cfg, ".claude.json")
            if cfg
            else os.path.expanduser("~/.claude.json")
        )
    out: dict = {"hasCompletedOnboarding": True}
    raw = _read_capped(src, _MAX_SOURCE_JSON)
    if raw:
        try:
            real = json.loads(raw)
        except ValueError:
            real = None
        if isinstance(real, dict):
            if isinstance(real.get("oauthAccount"), dict):
                out["oauthAccount"] = real["oauthAccount"]
            if isinstance(real.get("userID"), str):
                out["userID"] = real["userID"]
    # Our own trust record for the share (never the user's projects map),
    # as MindFlock pre-trusts every workdir it launches claude in.
    out["projects"] = {work: {"hasTrustDialogAccepted": True}}
    return json.dumps(out, indent=2).encode()


def prepare_home(share, provider: str) -> list[str]:
    """Seed ``<home>`` with only what ``provider`` needs to log in (0600).

    Returns the written paths. Never copies history, projects, settings or
    any other provider state.
    """
    if provider not in PROFILES:
        raise SandboxError(f"provider {provider} has no sandbox profile")
    d = share_dirs(share)
    home = d["home"]
    written: list[str] = []
    if provider == "claude":
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.expanduser(
            "~/.claude"
        )
        cred = _read_capped(os.path.join(cfg, ".credentials.json"), _MAX_CREDENTIAL)
        if cred is not None:
            written.append(safe_write(home, ".claude/.credentials.json", cred))
        body = _claude_minimal_json(d["work"])
        # CLAUDE_CONFIG_DIR moves claude's global config to <dir>/.claude.json;
        # seed both so either lookup finds the minimal one.
        written.append(safe_write(home, ".claude.json", body))
        written.append(safe_write(home, ".claude/.claude.json", body))
    elif provider == "codex":
        cfg = os.environ.get("CODEX_HOME", "").strip() or os.path.expanduser("~/.codex")
        cred = _read_capped(os.path.join(cfg, "auth.json"), _MAX_CREDENTIAL)
        if cred is not None:
            written.append(safe_write(home, ".codex/auth.json", cred))
    return written


# --------------------------------------------------------------------------
# egress


def _valid_allow_entry(entry: str) -> str | None:
    if not isinstance(entry, str):
        return None
    e = entry.strip().lower()
    if e.startswith("."):
        body = e[1:]
        # A suffix needs two labels: ".com" or "." would allow the world.
        return e if _HOST_RE.match(body) and body.count(".") >= 1 else None
    return e if _HOST_RE.match(e) else None


def egress_allow(provider: str, extra: list | None = None) -> list[str]:
    """The provider's default egress hosts plus ``extra`` (the user's
    ``peer.egress_allow`` setting, passed in by the caller). Invalid entries
    (IP literals, bare TLDs, wildcards) are dropped."""
    if provider not in PROFILES:
        raise SandboxError(f"provider {provider} has no sandbox profile")
    out: list[str] = []
    for e in list(PROFILES[provider]["egress"]) + list(extra or []):
        v = _valid_allow_entry(e)
        if v and v not in out:
            out.append(v)
    return out
