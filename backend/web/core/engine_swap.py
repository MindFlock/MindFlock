"""Replace the installed engine on Windows without ever leaving a broken one.

``uv tool install --force`` (and ``pipx install --force``) rebuild the tool
venv IN PLACE. On Linux and macOS that is harmless: a running process keeps
the files it opened. On Windows it is not — every process running from the
venv (the server, its ticket-pipeline child, the session host
``backend/session/winmux/``, each session's MCP server, a ``mindflock``
command) maps ``Scripts\\python.exe``, the trampoline ``mindflock.exe`` and
the ``.pyd`` files it loaded, and Windows refuses to delete those. When uv
decides to recreate the venv it deletes what it can, hits a mapped file and
gives up — ``Lib`` gone, ``Scripts`` left, ``uv tool list`` answering
"Failed find package `mindflock` in tool environment" and every process that
then imports anything failing (2026-11-13: a real user's 0.14.15 auto-update).

What Windows DOES allow is renaming a directory, or an exe, while a process
runs from it (checked on a real box: a running venv python, a loaded
``.pyd``, a running trampoline exe). The processes keep their mapped images.
So the swap here never deletes anything in use:

  1. **stage** — the same install into a scratch tool dir beside the real one
     (``<tools>-mindflock-next``), while the live engine keeps serving: the
     download, the build and the resolve happen here, and the staged engine
     is import-checked. A failure stops here with nothing touched.
  2. **move aside** — the ``mindflock.exe`` shim to ``mindflock.exe.old-<stamp>``
     first (it is the one that can be held — a ``mindflock`` command
     running — and nothing has changed yet if it can't move), then the live
     venv to ``<tools>-mindflock-old-<stamp>`` (beside the tools dir: uv
     errors on strangers inside it). Running sessions keep working from the
     moved files.
  3. **install** — the real install into the now-empty place, from the warm
     cache (seconds), then :func:`check_engine`: ``mindflock.exe --version``
     and an import check of the venv's own python.
  4. **put back** on any failure: the half-built venv goes, the old one is
     renamed back. Success clears the stage; the old copy is deleted when
     nothing holds it any more (now, or by the next update's cleanup).

Stdlib only, and runnable as a script (``python -I engine_swap.py --venv …
-- uv tool install …``): the server's in-app updater copies this file out of
the venv and runs it with the base interpreter, so the swap itself never
imports from the venv it is replacing. ``mindflock update`` imports it.
``install.ps1`` (the desktop app's and the manual installer) carries the same
steps in PowerShell — keep the two in step (docs/windows.md, "Updating the
engine on Windows").
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional

#: What a healthy engine imports: the CLI, the session package (the one the
#: 2026-11-13 server failed on), and the updater. None of them builds the
#: engine or writes state (importing ``backend.web.server`` would).
IMPORT_CHECK = (
    "import backend.cli, backend.session, backend.session.preflight, "
    "backend.web.core.self_update"
)

#: Suffixes beside the tools dir (``…\uv\tools`` -> ``…\uv\tools-mindflock-old-…``).
STAGE_SUFFIX = "-mindflock-next"
OLD_SUFFIX = "-mindflock-old-"
FAILED_SUFFIX = "-mindflock-failed-"
EXE_OLD = ".old-"
EXE_FAILED = ".failed-"

#: Exit code when the live engine could not be moved aside (nothing changed).
EXIT_BUSY = 75

#: How long a busy shim gets to free up (a ``mindflock`` command an agent or
#: hook ran) before the swap gives up, nothing changed.
SHIM_WAIT_S = 15

Runner = Callable[..., "subprocess.CompletedProcess"]


def _win(path: str) -> bool:
    return os.name == "nt" or (len(path) > 2 and path[1] == ":")


def venv_python(venv: str) -> str:
    """The venv's own interpreter (``Scripts\\python.exe`` on Windows)."""
    if _win(venv):
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def stage_root(venv: str) -> str:
    tools = os.path.dirname(os.path.normpath(venv))
    return tools + STAGE_SUFFIX


def stage_env(
    method: str, venv: str, env: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """The environment that points the installer at the stage instead of the
    live tool dir (uv: ``UV_TOOL_DIR``/``UV_TOOL_BIN_DIR``; pipx:
    ``PIPX_HOME``/``PIPX_BIN_DIR``)."""
    out = dict(os.environ if env is None else env)
    root = stage_root(venv)
    if method == "pipx":
        out["PIPX_HOME"] = os.path.join(root, "pipx")
        out["PIPX_BIN_DIR"] = os.path.join(root, "bin")
    else:
        out["UV_TOOL_DIR"] = os.path.join(root, "tools")
        out["UV_TOOL_BIN_DIR"] = os.path.join(root, "bin")
    return out


def staged_venv(method: str, venv: str) -> str:
    name = os.path.basename(os.path.normpath(venv))
    root = stage_root(venv)
    if method == "pipx":
        return os.path.join(root, "pipx", "venvs", name)
    return os.path.join(root, "tools", name)


def staged_exe(venv: str, exe: str) -> str:
    """The stage's shim for ``exe`` ("" when there is no shim to check)."""
    if not exe:
        return ""
    return os.path.join(stage_root(venv), "bin", os.path.basename(exe))


def find_exe(env: Optional[Dict[str, str]] = None) -> str:
    """The ``mindflock`` shim uv/pipx linked (``UV_TOOL_BIN_DIR``, then
    ``~/.local/bin``, then PATH), or ""."""
    env = dict(os.environ if env is None else env)
    name = "mindflock.exe" if os.name == "nt" else "mindflock"
    dirs = [env.get("UV_TOOL_BIN_DIR", ""), env.get("XDG_BIN_HOME", "")]
    home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
    dirs.append(os.path.join(home, ".local", "bin"))
    for d in dirs:
        if d and os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    return shutil.which("mindflock") or ""


def check_engine(venv: str, exe: str = "", *, run: Runner = subprocess.run) -> str:
    """ "" when the engine at ``venv`` (and its shim ``exe``) works, else why
    not: the venv's python must import :data:`IMPORT_CHECK`, and the shim must
    answer ``--version``."""
    py = venv_python(venv)
    if not os.path.isfile(py):
        return "%s is missing" % py
    checks = [([py, "-c", IMPORT_CHECK], "the engine's packages don't import")]
    if exe:
        checks.append(([exe, "--version"], "%s --version fails" % exe))
    for argv, why in checks:
        try:
            cp = run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180
            )
        except (OSError, subprocess.SubprocessError) as err:
            return "%s (%s)" % (why, err)
        if cp.returncode != 0:
            out = cp.stdout or b""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            last = [ln for ln in str(out).splitlines() if ln.strip()][-1:]
            return why + (": " + last[0].strip() if last else "")
    return ""


def _rmtree(path: str) -> bool:
    """Delete ``path`` as far as Windows lets us; True when it is gone."""
    if not os.path.lexists(path):
        return True
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass
    return not os.path.lexists(path)


def cleanup_old(venv: str, exe: str = "") -> List[str]:
    """Delete earlier swaps' leftovers nothing holds any more (a session that
    still runs from one keeps it until it ends). Returns what is left."""
    tools = os.path.dirname(os.path.normpath(venv))
    left = []
    for path in glob.glob(glob.escape(tools) + OLD_SUFFIX + "*") + glob.glob(
        glob.escape(tools) + FAILED_SUFFIX + "*"
    ):
        if not _rmtree(path):
            left.append(path)
    if exe:
        for path in glob.glob(glob.escape(exe) + EXE_OLD + "*") + glob.glob(
            glob.escape(exe) + EXE_FAILED + "*"
        ):
            if not _rmtree(path):
                left.append(path)
    return left


def swap_install(
    argv: List[str],
    *,
    venv: str,
    exe: str = "",
    method: str = "uv-tool",
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
    env: Optional[Dict[str, str]] = None,
    stamp: str = "",
    stage: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the install ``argv`` (``uv tool install --force ...``) so that the
    engine at ``venv`` is replaced whole or not at all. Returns the exit code:
    0 on success; otherwise the engine that was there before is back in place
    (``log`` says so, or says it could not be)."""
    venv = os.path.normpath(venv)
    stamp = stamp or time.strftime("%Y%m%d%H%M%S")
    base_env = dict(os.environ if env is None else env)
    live = os.path.isdir(venv)
    left = cleanup_old(venv, exe)
    if left:
        log("still in use from an earlier update (kept): %s" % ", ".join(left))

    if live and stage:
        root = stage_root(venv)
        _rmtree(root)
        log("building the new engine beside the running one (%s)..." % root)
        code = run(argv, env=stage_env(method, venv, base_env)).returncode
        why = "" if code == 0 else "the install failed (exit %d)" % code
        if not why:
            why = check_engine(
                staged_venv(method, venv), staged_exe(venv, exe), run=run
            )
        if why:
            _rmtree(root)
            log("%s - nothing was changed; the engine you have keeps running" % why)
            return code or 1

    old_venv = os.path.dirname(venv) + OLD_SUFFIX + stamp
    old_exe = exe + EXE_OLD + stamp if exe else ""
    moved_venv = moved_exe = False
    # The shim first: it is the one that can be held (an engine before the
    # sys.path fix in backend/__init__ pins its own; a `mindflock` command
    # may be running), and nothing has changed yet if it can't move.
    if exe and os.path.lexists(exe):
        busy: Optional[OSError] = None
        for _ in range(max(1, int(SHIM_WAIT_S))):
            try:
                os.rename(exe, old_exe)
                moved_exe, busy = True, None
                break
            except OSError as e:
                busy = e
                sleep(1)
        if not moved_exe:
            _rmtree(stage_root(venv))
            log(
                "%s is in use by a running MindFlock (%s) - nothing was changed; "
                "stop it and update again" % (exe, busy)
            )
            return EXIT_BUSY
    if live:
        try:
            os.rename(venv, old_venv)
            moved_venv = True
        except OSError as err2:
            if moved_exe:
                try:
                    os.rename(old_exe, exe)
                except OSError:
                    pass
            _rmtree(stage_root(venv))
            log(
                "couldn't move the running engine aside (%s) - nothing was "
                "changed; the engine you have keeps running" % err2
            )
            return EXIT_BUSY

    try:
        code = run(argv, env=base_env).returncode
        why = "" if code == 0 else "the install failed (exit %d)" % code
        if not why:
            why = check_engine(
                venv, exe if (exe and os.path.isfile(exe)) else "", run=run
            )
    except Exception as crash:  # noqa: BLE001 - the old engine must come back
        code, why = 1, "the install could not run (%s)" % crash
    if not why:
        _rmtree(stage_root(venv))
        if moved_venv and not _rmtree(old_venv):
            log(
                "the previous engine stays at %s until the sessions using it end"
                % old_venv
            )
        if moved_exe:
            _rmtree(old_exe)
        return 0

    log("%s - putting the previous engine back" % why)
    _rmtree(stage_root(venv))
    restored = True
    if moved_venv:
        if os.path.lexists(venv):
            failed = os.path.dirname(venv) + FAILED_SUFFIX + stamp
            try:
                os.rename(venv, failed)
            except OSError:
                failed = venv
            _rmtree(failed)
        restored = False
        if not os.path.lexists(venv):
            try:
                os.rename(old_venv, venv)
                restored = True
            except OSError as back_err:
                log("couldn't put %s back (%s)" % (old_venv, back_err))
    if moved_exe:
        if os.path.lexists(exe) and not _rmtree(exe):
            try:
                os.rename(exe, exe + EXE_FAILED + stamp)
            except OSError:
                pass
        try:
            os.rename(old_exe, exe)
        except OSError as back_err:
            log("couldn't put %s back (%s)" % (exe, back_err))
            restored = False
    if not live:
        log("there was no engine here before; nothing to put back")
    elif restored:
        log("the previous engine is back in place")
    else:
        log("the previous engine could NOT be put back: reinstall MindFlock")
    return code or 1


def main(args: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="engine_swap", description=__doc__.split("\n")[0])
    p.add_argument("--venv", required=True)
    p.add_argument("--exe", default="")
    p.add_argument("--method", default="uv-tool")
    p.add_argument("--no-stage", action="store_true")
    p.add_argument("install", nargs=argparse.REMAINDER)
    ns = p.parse_args(args)
    argv = ns.install[1:] if ns.install[:1] == ["--"] else ns.install
    if not argv:
        p.error("no install command after --")

    def say(msg: str) -> None:
        # ASCII whatever the console's code page: a print that raised here
        # would stop the swap halfway.
        line = "=== %s ===" % msg
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)

    return swap_install(
        argv, venv=ns.venv, exe=ns.exe, method=ns.method, log=say, stage=not ns.no_stage
    )


if __name__ == "__main__":  # pragma: no cover - run by the in-app updater on Windows
    sys.exit(main())
