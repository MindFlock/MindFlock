"""Subprocess helpers shared by the server and the core modules.

``backend.web.server`` re-exports :func:`run_capped` as ``_run_capped`` for its
own call sites; core modules call it here (``proc.run_capped(...)``, read off
the module so a test's ``monkeypatch.setattr(proc, "run_capped", …)`` reaches
them).

A tmux argv (``args[0] == "tmux"``) goes through the multiplexer door,
:func:`backend.session.mux.run` — ``subprocess.run`` itself on Linux/macOS,
the MindFlock session host on native Windows (docs/windows.md).
"""

from __future__ import annotations

import subprocess

from backend import compat
from backend.session import mux


def shell(name: str = "sh") -> str:
    """argv[0] for a ``<shell> -c <string>`` MindFlock runs: ``name`` itself
    on Linux/macOS (the argv stays exactly as it was); on native Windows Git
    for Windows' ``bash.exe`` (:func:`backend.compat.posix_shell`), which runs
    the same shell strings."""
    if not compat.IS_WINDOWS:
        return name
    return compat.posix_shell(bash=True)  # pragma: no cover - Windows


def run_capped(args, *, timeout, **kw):
    """``subprocess.run`` with a hard timeout that never raises on expiry.

    A hung child is killed and reported as a *failed* ``CompletedProcess``
    (returncode 124, the timeout note appended to stdout/stderr), so every
    call site's existing non-zero-exit handling applies unchanged.
    """
    run = (
        mux.run
        if not isinstance(args, (str, bytes)) and args and args[0] == "tmux"
        else subprocess.run
    )
    try:
        return run(args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as err:
        msg = "timed out after %gs: %s" % (timeout, " ".join(str(a) for a in args))
        text = bool(kw.get("text") or kw.get("universal_newlines"))

        def _coerce(v):
            if v is None:
                return msg if text else msg.encode("utf-8")
            if isinstance(v, bytes):
                return (
                    (v.decode("utf-8", "replace") + "\n" + msg)
                    if text
                    else v + b"\n" + msg.encode("utf-8")
                )
            return v + "\n" + msg

        return subprocess.CompletedProcess(
            args, 124, stdout=_coerce(err.output), stderr=_coerce(err.stderr)
        )


def _ps_token(tok: str) -> str:
    # ps prints args space-joined; a Windows path with a space in it
    # (``C:\Program Files\…``) would split into two tokens there.
    return "_".join(tok.split()) or "''"


def ps_table(columns: str) -> subprocess.CompletedProcess:
    """``ps -e -o <columns>``'s output (``columns`` as given to ``-o``, of
    ``pid=``, ``ppid=``, ``comm=``, ``args=``) read from psutil — for native
    Windows, which has no ``ps``. ``comm`` drops ``.exe`` like a ps comm has
    no suffix; ``args`` is the command line space-joined, a token's own
    blanks turned to ``_``. A failed read is returncode 1, as ps's would be."""
    cols = [c.strip().rstrip("=") for c in columns.split(",") if c.strip()]
    try:
        import psutil

        rows = []
        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            info = p.info
            vals = []
            for c in cols:
                if c == "pid":
                    vals.append(str(info.get("pid") or 0))
                elif c == "ppid":
                    vals.append(str(info.get("ppid") or 0))
                elif c == "comm":
                    name = info.get("name") or ""
                    if name.lower().endswith(".exe"):
                        name = name[:-4]
                    vals.append(name or "?")
                elif c == "args":
                    argv = info.get("cmdline") or [info.get("name") or ""]
                    vals.append(" ".join(_ps_token(t) for t in argv))
                else:
                    vals.append("-")
            rows.append(" ".join(vals))
    except Exception as err:  # noqa: BLE001 — psutil missing or the table unreadable
        return subprocess.CompletedProcess(
            ["ps"], 1, stdout=b"", stderr=str(err).encode()
        )
    out = ("\n".join(rows) + "\n").encode("utf-8", "replace")
    return subprocess.CompletedProcess(["ps"], 0, stdout=out, stderr=b"")
