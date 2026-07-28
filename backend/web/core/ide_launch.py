"""Cross-platform IDE launching + installed-IDE detection.

The one place a workspace gets opened in the configured editor. Historically
every call site did a naive ``Popen(ide_argv() + [path])``, which only works
for GUI editors whose CLI shim is on PATH. This module handles the rest:

* **GUI editors** — ``Popen(argv + [path])`` detached; on macOS, when the CLI
  shim is missing but the app bundle exists, fall back to
  ``open -a <App> <path>`` (which also activates/focuses the app — the v1
  macOS "focus" story).
* **Terminal editors** (nvim/vim/emacs/hx/micro) — wrap the editor in a
  terminal emulator per OS, mirroring
  :func:`backend.ticket_ingestion.terminal_launch.build_terminal_tab_argv`:
  WSL → ``wt.exe`` + ``wsl.exe -d <distro>``, macOS → Terminal.app via
  ``osascript``, Linux → first available emulator
  (``$MINDFLOCK_TERMINAL`` wins).

Failures raise :class:`IdeLaunchError` whose message includes remediation
("install X" / "set your IDE in Settings → Advanced") so callers can surface
it directly to the user.
"""

from __future__ import annotations

import glob
import os
import shlex
import shutil
import socket
import subprocess
from typing import List, Optional

from backend import log, osenv
from backend.config import ide as ide_cfg
from backend.ticket_ingestion.terminal_launch import (
    _linux_terminal,
    wsl_distro,
    wsl_interop_available,
    wt_command,
)

__all__ = ["IdeLaunchError", "detect_ides", "ide_installed", "launch_ide"]


class IdeLaunchError(RuntimeError):
    """The configured IDE could not be launched; the message says how to fix it."""


# --------------------------------------------------------------------------- #
# Detection (D2)
# --------------------------------------------------------------------------- #


def _macos_app_bundle(app: str) -> Optional[str]:
    """Path of ``<app>.app`` under /Applications or ~/Applications, or None."""
    for root in ("/Applications", os.path.expanduser("~/Applications")):
        candidate = os.path.join(root, app + ".app")
        if os.path.isdir(candidate):
            return candidate
    return None


def ide_installed(spec: ide_cfg.IdeSpec) -> bool:
    """Is this editor launchable here? PATH probe, plus the same off-PATH
    fallbacks ``launch_ide`` uses: the macOS app-bundle probe (a GUI app can be
    installed without its CLI shim on PATH) and the Remote-WSL CLI shim probe
    (the shim is only on PATH inside the editor's own integrated terminal)."""
    if shutil.which(spec.command):
        return True
    if osenv.os_kind() == "macos" and spec.macos_app:
        return _macos_app_bundle(spec.macos_app) is not None
    if osenv.os_kind() == "wsl" and spec.storage_dirname is not None:
        return _wsl_remote_cli(spec.command) is not None
    return False


def detect_ides() -> List[ide_cfg.IdeSpec]:
    """The known IDEs that are actually installed on this host."""
    return [spec for spec in ide_cfg.known_ide_specs() if ide_installed(spec)]


# --------------------------------------------------------------------------- #
# Launch (D3 / D4)
# --------------------------------------------------------------------------- #


def _popen_detached(argv: List[str], env: Optional[dict] = None) -> None:
    """Fire-and-forget launch, detached from our process group.

    ``env`` (when given) fully replaces the child environment — callers pass a
    copy of ``os.environ`` with additions, not a sparse dict."""
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


# --------------------------------------------------------------------------- #
# WSL Remote-WSL fallback (D6)
#
# Under Remote-WSL the editor's CLI (`cursor` / `code`) is a per-connection shim
# living in ``~/.<name>-server/bin/<commit>/bin/remote-cli/`` — it is injected
# onto PATH *and* handed a ``VSCODE_IPC_HOOK_CLI`` socket only inside the
# editor's own integrated terminal. A MindFlock server started detached (Electron
# launcher, plain shell, systemd) inherits neither, so ``shutil.which("cursor")``
# fails and the open-in-IDE action silently 400s. We recover both pieces here:
# glob the shim back onto the argv, and forward the newest live IPC socket.
# --------------------------------------------------------------------------- #


def _wsl_remote_cli(command: str) -> Optional[str]:
    """Path to a VS Code-family Remote-WSL CLI shim (``cursor``/``code``/…) when
    one is installed but not on PATH, or ``None``. Newest server build wins."""
    base = os.path.basename(command)
    home = os.path.expanduser("~")
    # ``~/.cursor-server``, ``~/.vscode-server``, ``~/.vscode-server-insiders``,
    # ``~/.windsurf-server`` … — match any ``*-server*`` layout, then confirm the
    # shim basename equals the launch command so we never cross the wires.
    pattern = os.path.join(home, ".*-server*", "bin", "*", "bin", "remote-cli", base)
    matches = [p for p in glob.glob(pattern) if os.access(p, os.X_OK)]
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def _log_route(fmt: str, *args: object) -> None:
    """Launch decisions into the server log — 'open in IDE' failures used to be
    invisible (fired from a silent double-click), so every route is recorded."""
    if log.InfoLog is not None:
        log.InfoLog.Printf("ide-launch: " + fmt, *args)


# The Remote-WSL server directory each editor's per-window server runs from.
# All VS Code-family forks name their IPC sockets identically (vscode-ipc-
# *.sock), so the socket path alone can't say WHICH editor a live socket
# belongs to — only its owning process (cmdline contains this dir) can.
_SERVER_DIRNAMES = {
    "cursor": ".cursor-server",
    "code": ".vscode-server",
    "code-insiders": ".vscode-server-insiders",
    "codium": ".vscodium-server",
    "windsurf": ".windsurf-server",
}


def _server_dir_marker(command: str, shim: Optional[str]) -> Optional[str]:
    """The ``~/.<x>-server`` dirname identifying the configured editor's WSL
    server processes. The resolved shim's own path is ground truth (it lives in
    that dir); a PATH-resolved shim falls back to the known-forks table. ``None``
    disables socket-ownership filtering (unknown editor — old behavior)."""
    for part in (shim or "").split(os.sep):
        if part.startswith(".") and "-server" in part:
            return part
    return _SERVER_DIRNAMES.get(os.path.basename(command))


def _sock_owner_cmdline(sock_path: str) -> Optional[str]:
    """Cmdline of a process holding this unix socket (via /proc/net/unix inode →
    /proc/*/fd scan), or ``None`` when it can't be identified (non-Linux /proc,
    permissions). Same-user editor servers are always identifiable under WSL."""
    try:
        inodes = set()
        with open("/proc/net/unix", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                cols = line.split()
                if len(cols) >= 8 and cols[7] == sock_path:
                    inodes.add("socket:[%s]" % cols[6])
        if not inodes:
            return None
        for fd_dir in glob.glob("/proc/[0-9]*/fd"):
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink(os.path.join(fd_dir, fd)) not in inodes:
                        continue
                    pid = fd_dir.split("/")[2]
                    with open("/proc/%s/cmdline" % pid, "rb") as ch:
                        return ch.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except OSError:
                    continue
    except OSError:
        return None
    return None


def _sock_owned_by_other_editor(sock_path: str, marker: Optional[str]) -> bool:
    """True when the socket's owning process is identified AND does not belong
    to the configured editor's server. Unknown owners are NOT rejected — a
    failed attribution must degrade to the old accept-any behavior, not break
    launching outright."""
    if not marker:
        return False
    owner = _sock_owner_cmdline(sock_path)
    return owner is not None and marker not in owner


def _sock_alive(path: str) -> bool:
    """Whether a unix socket accepts connections (a closed editor window's
    socket file lingers on disk forever and refuses)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wsl_ipc_hook(marker: Optional[str] = None) -> Optional[str]:
    """Newest LIVE ``vscode-ipc-*.sock`` (the socket the Remote-WSL CLI talks
    to the running editor over), or ``None`` when no editor window is
    connected. Liveness is probed, not assumed: socket files from closed
    windows are never cleaned up, so the newest-by-mtime file is routinely a
    dead one — handing the CLI a dead hook makes it exit silently and the
    "open in IDE" click does nothing.

    ``marker`` (a ``.<x>-server`` dirname) additionally skips live sockets
    that provably belong to a DIFFERENT VS Code-family editor: every fork
    names its sockets identically, and the configured editor's CLI aimed at a
    foreign fork's socket is another silent no-op."""
    roots = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        roots.append(xdg)
    roots.append("/run/user/%d" % os.getuid())
    roots.append(os.environ.get("TMPDIR") or "/tmp")
    seen: set = set()
    socks: List[str] = []
    for root in roots:
        if not root or root in seen:
            continue
        seen.add(root)
        socks.extend(glob.glob(os.path.join(root, "vscode-ipc-*.sock")))
    socks.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for sock in socks:
        if not _sock_alive(sock):
            continue
        if _sock_owned_by_other_editor(sock, marker):
            _log_route(
                "skipping live socket %s: owned by another editor (want %s)",
                sock,
                marker,
            )
            continue
        return sock
    return None


def _live_ipc_hook(marker: Optional[str] = None) -> Optional[str]:
    """A VERIFIED-live IPC hook for the Remote-WSL CLI, or ``None`` when no
    editor window is connected. Our own env's hook is preferred but validated
    too — a server inherited from an old editor terminal carries a dead one.
    ``marker`` filters out sockets owned by other VS Code-family editors (see
    :func:`_wsl_ipc_hook`)."""
    hook = os.environ.get("VSCODE_IPC_HOOK_CLI")
    if hook and _sock_alive(hook) and not _sock_owned_by_other_editor(hook, marker):
        return hook
    return _wsl_ipc_hook(marker)


def _windows_editor_cli(command: str) -> Optional[str]:
    """The WINDOWS-side launcher script of a VS Code-family editor, or None.

    Every family install (Cursor, VS Code, Codium, Windsurf, …) ships a
    POSIX ``resources/app/bin/<cmd>`` script designed to be run from WSL: it
    execs the Windows binary with translated paths, so it can open a fresh
    window without any IPC socket. Discovered generically — any user profile,
    per-user or system-wide install, any drive — no machine-specific paths.
    Newest build wins.
    """
    base = os.path.basename(command)
    patterns = (
        "/mnt/*/Users/*/AppData/Local/Programs/*/resources/app/bin/%s" % base,
        "/mnt/*/Program Files*/*/resources/app/bin/%s" % base,
    )
    matches: List[str] = []
    for pattern in patterns:
        matches.extend(p for p in glob.glob(pattern) if os.access(p, os.X_OK))
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def _applescript_quote(command_argv: List[str]) -> str:
    """A shell command line, escaped for embedding in an AppleScript string."""
    line = shlex.join(command_argv)
    return line.replace("\\", "\\\\").replace('"', '\\"')


def _terminal_wrap_argv(title: str, command: List[str]) -> Optional[List[str]]:
    """Argv that runs ``command`` inside a new terminal window/tab on this OS.

    Mirrors ``build_terminal_tab_argv``'s per-OS branching (which is
    tmux-attach-specific) for an arbitrary command. Returns ``None`` when no
    terminal emulator is available (native Windows, or bare Linux).
    """
    kind = osenv.os_kind()

    if kind == "wsl":
        # Interop can be dead even with wt.exe on PATH (binfmt entry flushed
        # by a Docker/qemu binfmt reset) — spawning any .exe then raises
        # OSError(ENOEXEC), so report "no terminal available" instead.
        if not wsl_interop_available():
            return None
        return [
            wt_command(),
            "-w",
            "0",
            "nt",
            "--title",
            title,
            "wsl.exe",
            "-d",
            wsl_distro(),
            "--",
            *command,
        ]

    if kind == "macos":
        script = 'tell application "Terminal" to do script "%s"' % _applescript_quote(
            command
        )
        return ["osascript", "-e", script]

    if kind == "linux":
        return _linux_terminal_argv(title, command)

    return None


def _linux_terminal_argv(title: str, command: List[str]) -> Optional[List[str]]:
    """Argv running ``command`` in a new window of the first available Linux
    terminal emulator (``$MINDFLOCK_TERMINAL`` wins), or ``None`` when none is
    found. Each emulator has its own title / exec-flag conventions."""
    term = _linux_terminal()
    if term is None:
        return None
    base = os.path.basename(term)
    if base == "gnome-terminal":
        return [term, "--title", title, "--", *command]
    if base in ("konsole", "xfce4-terminal"):
        return [term, "-e", shlex.join(command)]
    if base == "kitty":
        return [term, "--title", title, *command]
    if base == "alacritty":
        return [term, "--title", title, "-e", *command]
    # xterm and generic fallbacks.
    return [term, "-T", title, "-e", *command]


def _launch_gui(spec: Optional[ide_cfg.IdeSpec], argv: List[str], path: str) -> None:
    # VS Code-family editor under Remote-WSL is its own world (IPC hooks,
    # Windows-side installs) — ``storage_dirname`` is the family marker
    # (cursor/code/codium/windsurf/…).
    is_vscode_family = spec is not None and spec.storage_dirname is not None
    if is_vscode_family and osenv.os_kind() == "wsl":
        _launch_gui_wsl_family(spec, argv, path)
        return

    if shutil.which(argv[0]):
        _popen_detached(argv + [path])
        return
    # macOS: GUI apps are routinely installed without their CLI shim; `open -a`
    # both launches and activates (focuses) the app.
    if osenv.os_kind() == "macos" and spec is not None and spec.macos_app:
        if _macos_app_bundle(spec.macos_app) is not None:
            _popen_detached(["open", "-a", spec.macos_app, path])
            return
    name = spec.name if spec else argv[0]
    raise IdeLaunchError(
        "`%s` is not on PATH — install %s's shell command, or set your IDE in "
        "Settings → Advanced" % (argv[0], name)
    )


def _launch_gui_wsl_family(spec: ide_cfg.IdeSpec, argv: List[str], path: str) -> None:
    """A VS Code-family editor under Remote-WSL. Two live routes:

    1. **An editor window is connected** to this distro (a verified-live IPC
       socket exists): reach it through a CLI shim — PATH first, else the
       ``~/.<name>-server`` remote CLI — so the folder opens in / focuses the
       running editor.
    2. **No connected window**: the remote CLI would silently no-op (that was
       the "double-click does nothing" bug), so launch the Windows-side
       editor directly and let it open a fresh window.

    A PATH hit that resolves onto a Windows mount (/mnt/…) IS the Windows
    launcher script and works standalone; a Linux-side hit is launched as-is.
    """
    base = argv[0]
    on_path = shutil.which(base)
    shim = on_path or _wsl_remote_cli(base)
    hook = _live_ipc_hook(_server_dir_marker(base, shim)) if shim else None
    if hook and shim:
        _log_route("open %s via shim %s + hook %s", path, shim, hook)
        env = dict(os.environ)
        env["VSCODE_IPC_HOOK_CLI"] = hook
        _popen_detached([shim, *argv[1:], path], env=env)
        return
    # No connected window (or no shim to talk to one): Windows-side launcher.
    windows_cli = (
        on_path if (on_path or "").startswith("/mnt/") else _windows_editor_cli(base)
    )
    if windows_cli is not None:
        _log_route(
            "open %s via windows launcher %s (no live IPC hook)", path, windows_cli
        )
        _popen_detached([windows_cli, *argv[1:], path])
        return
    if on_path:
        # A Linux-side install with no IPC story — launch it plain.
        _log_route("open %s via plain PATH launch %s", path, on_path)
        _popen_detached(argv + [path])
        return
    raise IdeLaunchError(
        "no running %s window is connected to WSL and no Windows-side "
        "install was found — open %s once (so its window connects), or "
        "install its shell command, or set your IDE in Settings → Advanced"
        % (spec.name, spec.name)
    )


def _launch_terminal(spec: ide_cfg.IdeSpec, argv: List[str], path: str) -> None:
    if shutil.which(argv[0]) is None:
        raise IdeLaunchError(
            "`%s` is not on PATH — install %s, or set your IDE in "
            "Settings → Advanced" % (argv[0], spec.name)
        )
    title = os.path.basename(os.path.normpath(path)) or spec.name
    wrapped = _terminal_wrap_argv(title, argv + [path])
    if wrapped is None:
        raise IdeLaunchError(
            "no terminal emulator found to run `%s` — install gnome-terminal/"
            "konsole/xterm (or set $MINDFLOCK_TERMINAL), or pick a GUI IDE in "
            "Settings → Advanced" % argv[0]
        )
    _popen_detached(wrapped)


def launch_ide(path: str, argv: Optional[List[str]] = None) -> None:
    """Open ``path`` in the configured IDE (or an explicit ``argv`` override).

    GUI editors get a detached ``Popen`` (with the macOS ``open -a`` fallback);
    terminal editors are wrapped in a per-OS terminal emulator window. Raises
    :class:`IdeLaunchError` with remediation text when launching is impossible.
    """
    argv = list(argv) if argv else ide_cfg.ide_argv()
    spec = ide_cfg.spec_for(argv[0])
    path = str(path)
    try:
        if spec is not None and spec.kind == "terminal":
            _launch_terminal(spec, argv, path)
        else:
            _launch_gui(spec, argv, path)
    except IdeLaunchError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("ide-launch: %s failed for %s: %v", argv[0], path, err)
        raise
    except Exception as err:  # noqa: BLE001 — e.g. Popen OSError
        name = spec.name if spec else argv[0]
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("ide-launch: %s failed for %s: %v", argv[0], path, err)
        raise IdeLaunchError("failed to launch %s: %s" % (name, err)) from err
