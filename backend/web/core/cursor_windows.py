"""Cursor/IDE window management — platform-shell glue for editor windows.

Lifted verbatim from ``server.py``: locating a workspace's editor window
(xdotool on native Linux; powershell.exe + the Win32 API under WSL, where the
IDE runs as a native Windows app invisible to xdotool), focusing / maximizing /
closing it, discovering the folders currently open in the IDE (VS Code-family
``storage.json``), and the continuous auto-adopt loop that turns those folders
into in-place sessions.

Cross-module seams (``_session_for_path``, ``_create_inplace_session``, the
git probes, and the ``_CURSOR_SEEN`` / ``_CURSOR_AUTOADOPT_ENABLED`` state
that stays in ``server.py`` with its other accessors and toggle route) are
resolved through the *server module's* attribute bindings at call time via
:func:`_server`. That is deliberate: tests (and the sidebar toggle) rebind
those names on ``server``, and this code must keep honoring that.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import time
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

from backend import log
from backend import osenv
from backend.config import ide as ide_cfg
from backend.web.core.engine import _load_disk_tombstones


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _cursor_title_terms(workspace_path: str) -> List[str]:
    """Window-title search terms for a workspace: the worktree dir name, plus the
    bare slug (worktrees are ``<slug>_<hex>``; a title may omit the hash)."""
    base = os.path.basename(os.path.normpath(workspace_path or ""))
    if not base:
        return []
    terms = [base]
    m = re.match(r"(.+)_[0-9a-f]{8,}$", base)
    if m:
        terms.append(m.group(1))
    return terms


def _find_cursor_windows(workspace_path: str) -> List[str]:
    """xdotool window ids whose title matches this workspace's folder name.

    Empty when xdotool is unavailable, the path is empty, or none are open.
    """
    if not workspace_path or shutil.which("xdotool") is None:
        return []
    seen: list = []
    try:
        for term in _server()._cursor_title_terms(workspace_path):
            found = subprocess.run(
                ["xdotool", "search", "--name", term],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            for wid in found.stdout.decode("utf-8", "replace").split():
                if wid not in seen:
                    seen.append(wid)
    except Exception:  # noqa: BLE001
        pass
    return seen


# Cursor here runs as a NATIVE WINDOWS app under Remote-WSL (its window is a
# Win32 window, invisible to xdotool), so we size it via powershell.exe + the
# Win32 API. On a native-Linux host (no powershell.exe) we fall back to xdotool.
def _powershell() -> Optional[str]:
    return shutil.which("powershell.exe")


def _ps_encoded(script: str) -> str:
    """PowerShell -EncodedCommand payload (base64 of UTF-16LE) — dodges all shell
    and quoting issues when passing a multi-line script."""
    import base64

    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ps_lit(s: str) -> str:
    """A single-quoted PowerShell string literal (embedded single quotes doubled)."""
    return "'%s'" % (s or "").replace("'", "''")


def _ps_terms_array(terms: List[str]) -> str:
    """A PowerShell ``@(...)`` string-array literal of the given title terms."""
    return "@(" + ",".join(_ps_lit(t) for t in terms) + ")"


def _win_title_condition(terms: List[str]) -> str:
    """PowerShell -like OR-clause matching an IDE window title for a workspace."""
    return " -or ".join(
        "$_.MainWindowTitle -like '*%s*'" % t.replace("'", "''") for t in terms
    )


def _win_app_condition() -> str:
    """PowerShell -like clause matching the configured IDE's window titles
    (e.g. ``*Cursor*``, ``*Visual Studio Code*``)."""
    needle = ide_cfg.ide_window_needle().replace("'", "''")
    return "$_.MainWindowTitle -like '*%s*'" % needle


# One PowerShell process that waits for the workspace's Cursor window to appear,
# then unmaximizes (SW_RESTORE = 9) and sizes it — re-applying for a few seconds
# because Electron restores its remembered (often maximized) bounds just after
# the window shows. Placeholders are substituted (not str.format — the script has
# literal { } braces).
_WIN_CURSOR_MAXIMIZE_PS = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type @"
using System; using System.Runtime.InteropServices;
public class MxWin {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
}
"@
# SW_MAXIMIZE = 3: maximize on the window's CURRENT monitor (no move, no resize,
# never jumps to the primary). Re-applied for a few seconds because Electron
# restores its remembered bounds just after the window appears.
$deadline=(Get-Date).AddSeconds(12)
$p=$null
while((Get-Date) -lt $deadline -and -not $p){
  $p=Get-Process | Where-Object { (__APP__) -and (__COND__) } | Select-Object -First 1
  if(-not $p){ Start-Sleep -Milliseconds 400 }
}
if($p){
  $end=(Get-Date).AddSeconds(5)
  while((Get-Date) -lt $end){
    [MxWin]::ShowWindow($p.MainWindowHandle,3) | Out-Null
    Start-Sleep -Milliseconds 600
    $p.Refresh()
  }
}
"""


# Shared Win32 helper: enumerate ALL top-level windows and return the handles
# whose title matches the IDE app needle AND one of the workspace title terms.
#
# Why not Get-Process/MainWindowTitle (the old approach)? .NET exposes only ONE
# window per process — the process's first top-level window — via MainWindowTitle
# / MainWindowHandle. Cursor/VS Code are Electron apps that run MANY windows
# under a single main process, so every workspace window except the process's
# "main" one is invisible to a MainWindowTitle filter. A minimized workspace
# window therefore never matched, so SW_RESTORE never ran and it only flashed.
# EnumWindows sees every top-level window (minimized ones stay WS_VISIBLE), so
# the right window is found regardless of which one the process calls "main".
# Terms are tried most-specific first so the full worktree basename wins over the
# looser de-hashed slug (which could otherwise match a sibling worktree).
_WIN_FGWIN_CS = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class FgWin {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k,byte s,uint f,UIntPtr e);
  private delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc cb, IntPtr l);
  private static string Title(IntPtr h){
    int n=GetWindowTextLength(h); if(n<=0){ return ""; }
    StringBuilder sb=new StringBuilder(n+1); GetWindowText(h, sb, sb.Capacity); return sb.ToString();
  }
  // Handles matching the app needle + the most-specific term that hits anything.
  public static List<IntPtr> Find(string app, string[] terms){
    foreach(string term in terms){
      if(term.Length==0){ continue; }
      List<IntPtr> hits=new List<IntPtr>();
      EnumWindows(delegate(IntPtr h, IntPtr l){
        if(!IsWindowVisible(h)){ return true; }
        string t=Title(h); if(t.Length==0){ return true; }
        if(app.Length>0 && t.IndexOf(app, StringComparison.OrdinalIgnoreCase)<0){ return true; }
        if(t.IndexOf(term, StringComparison.OrdinalIgnoreCase)>=0){ hits.Add(h); }
        return true;
      }, IntPtr.Zero);
      if(hits.Count>0){ return hits; }
    }
    return new List<IntPtr>();
  }
}
"@"""


# Restore + raise an ALREADY-open workspace window (the double-click "focus"
# story). The `cursor <path>` IPC open-folder call switches the folder inside a
# running window but does NOT un-minimize it, and Windows blocks a background
# process from stealing the foreground — so a minimized window just stays in the
# taskbar. This does the missing Win32 work: SW_RESTORE (9) then a
# SetForegroundWindow guarded by the synthetic-Alt trick that defeats the
# foreground lock. Placeholders substituted (script has literal { }).
_WIN_CURSOR_FOCUS_PS = _WIN_FGWIN_CS + r"""
$app=__APP_LIT__
$terms=__TERMS_LIT__
# Retry for a couple of seconds: the window may still be settling right after the
# IPC open-folder call switches its workspace.
$deadline=(Get-Date).AddSeconds(3)
$done=$false
while((Get-Date) -lt $deadline -and -not $done){
  $hits=[FgWin]::Find($app,$terms)
  if($hits.Count -gt 0){
    $h=$hits[0]
    # SW_RESTORE = 9: un-minimize a minimized window; a normal window is unchanged.
    [FgWin]::ShowWindow($h,9) | Out-Null
    # Synthetic Alt tap unlocks SetForegroundWindow for a background caller.
    [FgWin]::keybd_event(0x12,0,0,[UIntPtr]::Zero)
    [FgWin]::SetForegroundWindow($h) | Out-Null
    [FgWin]::keybd_event(0x12,0,2,[UIntPtr]::Zero)
    $done=$true
  } else {
    Start-Sleep -Milliseconds 300
  }
}
"""


def _activate_x11(wid: str) -> None:
    """Linux/xdotool fallback: un-minimize + raise + focus a window.

    ``windowactivate`` (unlike ``windowraise``) restores a minimized window and
    switches to its desktop, so this is the right primitive for the iconified
    case — the Linux analogue of the Win32 SW_RESTORE + SetForegroundWindow."""
    try:
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", wid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def _macos_app_name() -> Optional[str]:
    """The configured editor's macOS app-bundle name (for ``open -a``), or None
    (custom/unknown editors, where we can't map to a bundle)."""
    try:
        spec = ide_cfg.ide_spec()
    except Exception:  # noqa: BLE001
        return None
    return spec.macos_app if spec else None


def _focus_macos(workspace_path: str) -> None:
    """macOS restore+focus: ``open -a <App> <path>`` hands the folder to the
    running editor, which brings its (possibly minimized) window to the front
    and activates the app. Needs no Accessibility/Automation permission, unlike
    System Events UI scripting. No-op when the app bundle name is unknown (an
    ``open <dir>`` without ``-a`` would wrongly open Finder)."""
    app = _macos_app_name()
    if not app:
        return
    try:
        subprocess.run(
            ["open", "-a", app, workspace_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def _macos_ide_running() -> bool:
    """macOS: whether the configured editor app is running. A good enough proxy
    for "already open" — the focus primitive (``open -a <App> <path>``) targets
    the folder itself, so per-folder window matching isn't needed to prefer
    focus over a cold launch. ``pgrep -f`` matches the ``.app`` bundle path that
    appears in every one of the app's process argv, so it works for Electron
    editors whose helper processes have generic names."""
    app = _macos_app_name()
    if not app:
        return False
    try:
        r = subprocess.run(
            ["pgrep", "-f", "%s.app" % app],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _focus_cursor_window(workspace_path: str) -> None:
    """Restore + raise the already-open editor window for this workspace. Windows:
    one background PowerShell doing SW_RESTORE + guarded SetForegroundWindow.
    Linux: xdotool windowactivate. Only called when a window was already open.
    Never raises.
    """
    terms = _server()._cursor_title_terms(workspace_path)
    if not terms:
        return
    ps_exe = _server()._powershell()
    if ps_exe:
        script = _WIN_CURSOR_FOCUS_PS.replace(
            "__APP_LIT__", _ps_lit(ide_cfg.ide_window_needle())
        ).replace("__TERMS_LIT__", _ps_terms_array(terms))
        try:
            subprocess.Popen(
                [ps_exe, "-NoProfile", "-EncodedCommand", _ps_encoded(script)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    if osenv.os_kind() == "macos":
        _focus_macos(workspace_path)
        return
    # Native-Linux (X11) fallback. _find_cursor_windows searches the most-specific
    # title term first, so the first id is the best match — activate just that one
    # rather than every sibling worktree window that shares the slug.
    for wid in _server()._find_cursor_windows(workspace_path):
        _activate_x11(wid)
        break


def _cursor_windows_open(workspace_path: str) -> bool:
    """True if the editor is already showing this workspace (so opening it again
    just focuses — don't resize). PowerShell EnumWindows on WSL/Windows, the
    running-app proxy on macOS, xdotool title search on native Linux."""
    terms = _server()._cursor_title_terms(workspace_path)
    if not terms:
        return False
    ps_exe = _server()._powershell()
    if ps_exe:
        # Same EnumWindows sweep as the focus path — a Get-Process/MainWindowTitle
        # probe misses every Electron window except the process's "main" one, so a
        # minimized workspace window read as "not open", steering the caller to the
        # maximize-new branch instead of the restore-and-focus one.
        script = (
            _WIN_FGWIN_CS
            + "\n$app="
            + _ps_lit(ide_cfg.ide_window_needle())
            + "\n$terms="
            + _ps_terms_array(terms)
            + "\nif([FgWin]::Find($app,$terms).Count -gt 0){'YES'}else{'NO'}"
        )
        try:
            r = subprocess.run(
                [ps_exe, "-NoProfile", "-EncodedCommand", _ps_encoded(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            return b"YES" in r.stdout
        except Exception:  # noqa: BLE001
            return False
    if osenv.os_kind() == "macos":
        return _macos_ide_running()
    return bool(_server()._find_cursor_windows(workspace_path))


def _maximize_x11(wid: str) -> None:
    """Linux/xdotool fallback: maximize the window in place (wmctrl if present)."""
    try:
        if shutil.which("wmctrl") is not None:
            subprocess.run(
                ["wmctrl", "-ir", wid, "-b", "add,maximized_vert,maximized_horz"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return
        subprocess.run(
            [
                "xdotool",
                "windowactivate",
                "--sync",
                wid,
                "key",
                "--clearmodifiers",
                "super+Up",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def _maximize_new_cursor_window(workspace_path: str) -> None:
    """Wait for the freshly-opened Cursor window for this workspace, then maximize
    it (on its current monitor — no move/resize). Windows path: one background
    PowerShell process. Linux path: xdotool poll + maximize loop. Only called when
    no window was already open. Never raises.
    """
    terms = _server()._cursor_title_terms(workspace_path)
    if not terms:
        return
    ps_exe = _server()._powershell()
    if ps_exe:
        script = _WIN_CURSOR_MAXIMIZE_PS.replace(
            "__APP__", _win_app_condition()
        ).replace("__COND__", _win_title_condition(terms))
        try:
            subprocess.Popen(
                [ps_exe, "-NoProfile", "-EncodedCommand", _ps_encoded(script)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass
        return
    # Native-Linux fallback (xdotool / wmctrl).
    if shutil.which("xdotool") is None:
        return
    _x11_wait_and_maximize(workspace_path)


# The freshly-opened window can take a moment to appear; once it does, Electron
# may briefly re-assert its remembered (maximized) bounds, so the maximize is
# re-applied for a few seconds rather than fired once.
_X11_FIND_TIMEOUT = 10.0  # s to wait for the window to show up
_X11_APPLY_WINDOW = 5.0  # s to keep re-applying maximize after it appears
_X11_FIND_POLL = 0.4  # s between find attempts
_X11_APPLY_POLL = 0.7  # s between maximize re-applies


def _x11_wait_and_maximize(workspace_path: str) -> None:
    """Native-Linux path of :func:`_maximize_new_cursor_window`: poll xdotool for
    the workspace's window, then maximize it in place until Electron stops
    overriding the bounds. Assumes xdotool is present (the caller checks)."""
    find_deadline = time.time() + _X11_FIND_TIMEOUT
    targets: List[str] = []
    while time.time() < find_deadline and not targets:
        targets = _server()._find_cursor_windows(workspace_path)
        if targets:
            break
        time.sleep(_X11_FIND_POLL)
    apply_deadline = time.time() + _X11_APPLY_WINDOW
    while targets and time.time() < apply_deadline:
        for wid in targets:
            _maximize_x11(wid)
        time.sleep(_X11_APPLY_POLL)


def _close_cursor_window(workspace_path: str) -> None:
    """Best-effort: close any Cursor/editor window opened at this workspace dir.

    Uses xdotool to find windows whose title contains the workspace dir name and
    closes them (the dir name shows up in the editor's window title). No-op when
    xdotool is unavailable or the path is empty.
    """
    for wid in _server()._find_cursor_windows(workspace_path):
        try:
            subprocess.run(
                ["xdotool", "windowclose", wid],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            pass


def _cursor_storage_path() -> Optional[str]:
    """Path to the configured IDE's globalStorage ``storage.json`` (holds window
    state). Works for every VS Code-family editor (Cursor, VS Code, Windsurf…);
    editors without this storage return None and auto-adopt finds nothing.

    Location is OS-specific (``<dir>`` = the editor's user-data dir name):
      * **Linux**  — ``~/.config/<dir>/User/globalStorage/storage.json``
      * **macOS**  — ``~/Library/Application Support/<dir>/User/globalStorage/storage.json``
      * **WSL**    — the editor runs on Windows via Remote-WSL, so its state
        lives under ``/mnt/<drive>/Users/<user>/AppData/Roaming/<dir>/...``

    All candidates are probed regardless of the detected OS (cheap, and robust to
    unusual setups); the first that exists wins.
    """
    dirname = ide_cfg.ide_storage_dirname()
    if not dirname:
        return None  # editor without VS Code-style storage: nothing to discover
    cands = [
        os.path.expanduser("~/.config/%s/User/globalStorage/storage.json" % dirname),
        os.path.expanduser(
            "~/Library/Application Support/%s/User/globalStorage/storage.json" % dirname
        ),
    ]
    cands += sorted(
        glob.glob(
            "/mnt/*/Users/*/AppData/Roaming/%s/User/globalStorage/storage.json"
            % glob.escape(dirname)
        )
    )
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def _cursor_uri_to_path(uri) -> str:
    """Convert a Cursor/VS Code folder URI to a local path this WSL can open.

    Handles ``vscode-remote://wsl+<distro>/home/...`` (Remote-WSL) and ``file://``
    (local, incl. Windows ``/C:/...`` -> ``/mnt/c/...``). Returns "" for remotes
    that aren't this machine (SSH, dev containers, other hosts).
    """
    if not uri or not isinstance(uri, str):
        return ""
    u = urlparse(uri)
    if u.scheme == "vscode-remote":
        if unquote(u.netloc).lower().startswith("wsl+"):
            return unquote(u.path)
        return ""
    if u.scheme == "file":
        p = unquote(u.path)
        m = re.match(r"^/([A-Za-z]):/(.*)$", p)
        if m:
            return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2))
        return p
    return ""


def _cursor_open_folders() -> List[str]:
    """Local paths of folders currently open in Cursor windows (deduped, ordered)."""
    path = _cursor_storage_path()
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    ws = data.get("windowsState") or {}
    uris = []
    la = ws.get("lastActiveWindow") or {}
    if isinstance(la, dict) and la.get("folder"):
        uris.append(la["folder"])
    for w in ws.get("openedWindows") or []:
        if isinstance(w, dict) and w.get("folder"):
            uris.append(w["folder"])
    seen, out = set(), []
    for u in uris:
        p = _cursor_uri_to_path(u)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _under_managed_workspace(real: str, roots: List[str]) -> bool:
    """True when ``real`` is (or is nested under) a managed workspace root.

    Those folders are MindFlock-provisioned worktrees / clones — they already own
    a session and must never be auto-adopted as a second one."""
    for root in roots:
        try:
            if os.path.commonpath([real, root]) == root:
                return True
        except ValueError:  # different drives / relative vs absolute
            continue
    return False


def _cursor_autoadopt_tick(open_paths: List[str], tombs: dict) -> List[Tuple[str, str]]:
    """Decide which open-in-Cursor folders to auto-adopt this tick.

    Returns a list of ``(path, realpath)`` to create in-place sessions for.
    Updates ``_CURSOR_SEEN`` for folders that must NOT be (re-)adopted while they
    stay open — a MindFlock-provisioned workspace, already a session, or a
    recently-deleted (tombstoned) title — and prunes folders no longer open so
    reopening re-adopts. Pure decision (no session creation / event-loop work),
    so it is unit-testable.

    ``tombs`` is the pruned deletion-tombstone map (title -> deleted_ts). Honoring
    it makes an explicit delete survive a server restart: without it the
    in-memory ``_CURSOR_SEEN`` memo is lost on restart and a folder still open in
    Cursor gets resurrected as a session over and over.

    A folder under a managed workspace root is skipped outright: it is a
    provisioned worktree/clone that a session already owns. That session may have
    been created by the ingestion pipeline in a SEPARATE process (it persists to
    state.json but the running server's in-memory ``_session_for_path`` can't see
    it until a reload), so the session-exists guard alone would miss it and open
    a second window for a ticket that already has one — the exact one-ticket /
    two-tabs bug.
    """
    srv = _server()
    reals = set()
    to_adopt = []
    try:
        managed_roots = [os.path.realpath(r) for r in srv._workspace_roots()]
    except Exception:  # noqa: BLE001 — never let a config hiccup break adoption
        managed_roots = []
    for p in open_paths:
        try:
            real = os.path.realpath(p)
        except OSError:
            continue
        reals.add(real)
        if real in srv._CURSOR_SEEN:
            continue
        if _under_managed_workspace(real, managed_roots):
            srv._CURSOR_SEEN.add(real)  # MindFlock-managed worktree — never adopt
            continue
        try:
            ready = os.path.isdir(p) and srv._is_git_repo(p) and srv._git_has_commits(p)
        except Exception:  # noqa: BLE001
            ready = False
        if not ready:
            continue  # not a usable git repo yet — re-checked next tick
        if srv._session_for_path(real) is not None:
            srv._CURSOR_SEEN.add(real)  # already a session (e.g. a worktree one)
            continue
        base_title = os.path.basename(os.path.normpath(p)) or "session"
        if base_title in tombs:
            srv._CURSOR_SEEN.add(real)  # explicitly deleted — don't resurrect
            continue
        to_adopt.append((p, real))
    # Forget folders no longer open in Cursor so reopening re-adopts them. The
    # to-adopt reals are in `reals`, so they aren't pruned before the loop adds
    # them to the memo on a successful create.
    srv._CURSOR_SEEN.intersection_update(reals)
    return to_adopt


async def _cursor_autoadopt_loop() -> None:
    """Adopt folders open in Cursor as in-place sessions, continuously.

    A folder is adopted once, when it first appears in Cursor's open-window
    list. Killing its session does NOT respawn it while it stays open in Cursor
    (it's memoized in ``_CURSOR_SEEN``, and an explicit delete is remembered via
    the on-disk deletion tombstone so it survives a restart); closing the Cursor
    window drops it from the list, clearing the memo so reopening re-adopts.
    Cursor only rewrites its window state on save/open/close, so a just-opened
    folder can take a few seconds to appear.

    Tombstones are consulted only on the FIRST tick: they exist to re-seed the
    memo lost on restart (a deleted folder still open in Cursor must stay
    dead). After that the memo alone governs — a window the user closes and
    later reopens is a fresh request to work on the folder, so it re-adopts
    even inside the 24h tombstone TTL.
    """
    seed_tombs = True
    while True:
        await asyncio.sleep(6)
        if not _server()._CURSOR_AUTOADOPT_ENABLED:
            continue  # toggled off — keep existing sessions, adopt nothing new
        try:
            open_paths = await asyncio.to_thread(_cursor_open_folders)
        except Exception:  # noqa: BLE001
            continue
        tombs = {}
        if seed_tombs:
            try:
                tombs = await asyncio.to_thread(_load_disk_tombstones)
            except Exception:  # noqa: BLE001
                tombs = {}
        seed_tombs = False
        for p, real in _cursor_autoadopt_tick(open_paths, tombs):
            try:
                inst = _server()._create_inplace_session(p)
                _server()._CURSOR_SEEN.add(real)
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("cursor auto-adopt %s -> %s", p, inst.Title)
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("cursor auto-adopt failed %s: %v", p, err)
