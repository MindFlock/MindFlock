"""Repair the process ``PATH`` so a GUI-launched backend can find user CLIs.

A MindFlock backend started from a desktop launcher (Electron, a ``.desktop``
file, Finder, ``launchd``/systemd) inherits a *minimal* ``PATH`` — it never
sources the user's shell profile — so ``shutil.which("claude")`` and friends
come back empty even though the tools are installed and work fine in the user's
terminal. That is why Settings → Agent CLIs can report "not installed" for a CLI
the user runs every day. It is the same "works in the terminal, not in the GUI
app" gap that :mod:`backend.web.core.ide_launch` already works around for
editor CLI shims, generalised to every provider and helper binary.

The fix mirrors what VS Code / GitHub Desktop do (the ``fix-path`` / ``shell-env``
trick): ask the user's real *login + interactive* shell what ``PATH`` it has,
then union those directories — plus a set of well-known per-user install
locations — into the process ``PATH``. Install detection and every subprocess we
spawn afterwards (tmux → provider CLIs, git, gh, node …) then see the same
``PATH`` the terminal does.

Call :func:`ensure_enriched` once at startup. It is idempotent, never raises, and
only *adds* directories — existing entries keep their resolution priority, so a
tool already on ``PATH`` still resolves exactly as before.
"""

from __future__ import annotations

import functools
import os
import subprocess

from backend import log, osenv

__all__ = ["enriched_path", "ensure_enriched", "login_shell_dirs", "well_known_dirs"]

# Delimiter framing the ``env`` dump so we can recover ``PATH`` even when the
# user's rc files print banners/noise to stdout. Parsing ``env`` output (rather
# than ``echo $PATH``) is shell-agnostic: the OS environment is colon-joined the
# same way under bash, zsh, and fish.
_DELIM = "__MINDFLOCK_PATH_PROBE__"
# Bound the shell probe: heavy profiles (nvm/conda/asdf) can take a beat, but a
# hung rc must never block server startup.
_PROBE_TIMEOUT = 4.0


@functools.lru_cache(maxsize=1)
def login_shell_dirs() -> tuple[str, ...]:
    """``PATH`` directories as seen by the user's login+interactive shell.

    Runs ``$SHELL -ilc env`` once (cached) and extracts the ``PATH`` line, so
    directories the user adds in ``~/.zshrc`` / ``~/.bash_profile`` / nvm / asdf
    etc. are recovered. Returns ``()`` on non-Unix hosts, when no shell is
    known, or on any failure — callers fall back to :func:`well_known_dirs`.
    """
    if not osenv.is_unix_like():
        return ()
    if os.environ.get("MINDFLOCK_PATH_PROBE"):  # reentrancy guard
        return ()
    shell = os.environ.get("SHELL", "").strip()
    if not shell:
        return ()
    # -i (interactive) so ~/.zshrc / ~/.bashrc run — many users set PATH there,
    # not only in the login-only profile files; -l (login) covers the rest.
    script = 'printf %%s "%s"; env; printf %%s "%s"' % (_DELIM, _DELIM)
    try:
        proc = subprocess.run(
            [shell, "-ilc", script],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            env={**os.environ, "MINDFLOCK_PATH_PROBE": "1"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    out = proc.stdout or ""
    start = out.find(_DELIM)
    if start < 0:
        return ()
    block = out[start + len(_DELIM) :]
    end = block.find(_DELIM)
    if end >= 0:
        block = block[:end]
    for line in block.splitlines():
        if line.startswith("PATH="):
            raw = line[len("PATH=") :]
            return tuple(d for d in raw.split(os.pathsep) if d)
    return ()


@functools.lru_cache(maxsize=1)
def well_known_dirs() -> tuple[str, ...]:
    """Existing well-known per-user CLI install directories (order = priority).

    A best-effort fallback for when the shell probe can't run (no ``$SHELL``,
    a locked-down host). Only directories that actually exist are returned.
    """
    candidates = (
        "~/.local/bin",  # pip --user, pipx, claude installer
        "~/bin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/opt/homebrew/bin",  # Apple-silicon Homebrew
        "/opt/homebrew/sbin",
        "/home/linuxbrew/.linuxbrew/bin",  # Linux Homebrew
        "~/.npm-global/bin",  # npm global prefix override
        "~/.cargo/bin",  # rustup / cargo install
        "~/.bun/bin",  # bun
        "~/.deno/bin",  # deno
        "~/.volta/bin",  # volta-managed node
        "~/.yarn/bin",
        "~/go/bin",  # go install
        "~/.asdf/shims",  # asdf version manager
        "/snap/bin",
    )
    out: list[str] = []
    for c in candidates:
        p = os.path.expanduser(c)
        if p not in out and os.path.isdir(p):
            out.append(p)
    return tuple(out)


def enriched_path(base: str | None = None) -> str:
    """``base`` (default: current ``PATH``) unioned with the login-shell and
    well-known directories, de-duplicated, existing entries kept first."""
    base = os.environ.get("PATH", "") if base is None else base
    seen: set[str] = set()
    out: list[str] = []
    for group in (
        [d for d in base.split(os.pathsep) if d],
        login_shell_dirs(),
        well_known_dirs(),
    ):
        for d in group:
            if d and d not in seen:
                seen.add(d)
                out.append(d)
    return os.pathsep.join(out)


@functools.lru_cache(maxsize=1)
def ensure_enriched() -> tuple[str, ...]:
    """Repair ``os.environ['PATH']`` in place (idempotent). Returns the added dirs.

    Set ``MINDFLOCK_NO_PATH_ENRICH=1`` to disable. Never raises: a probe failure
    just leaves ``PATH`` as-is.
    """
    if os.environ.get("MINDFLOCK_NO_PATH_ENRICH"):
        return ()
    try:
        before = os.environ.get("PATH", "")
        merged = enriched_path(before)
        if merged == before:
            return ()
        os.environ["PATH"] = merged
        had = set(d for d in before.split(os.pathsep) if d)
        added = tuple(d for d in merged.split(os.pathsep) if d and d not in had)
        if added and log.InfoLog is not None:
            log.InfoLog.Printf(
                "pathenv: added %d dir(s) to PATH for CLI detection: %s",
                len(added),
                os.pathsep.join(added),
            )
        return added
    except Exception:  # noqa: BLE001 — PATH repair must never break startup
        return ()
