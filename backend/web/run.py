#!/usr/bin/env python3
"""Launch the MindFlock server by running this script directly.

    python backend/web/run.py              # local mode (DEFAULT): binds 127.0.0.1,
                                         #   reachable only from this machine
    python backend/web/run.py tailscale    # bind 0.0.0.0 for phone/tailnet access
    python backend/web/run.py 9000         # custom port (still local mode)
    python backend/web/run.py tailscale 9000   # tailnet access, custom port

Mode and port may be given in either order; env vars also work
(``CS_WEB_MODE=tailscale``, ``PORT=9000``).

Run it from inside the git repository you want to manage (same requirement as
the `mindflock` CLI). The clients are the MindFlock desktop app (Electron — the one
supported desktop client, which auto-starts this server itself when it isn't
already running) and the phone UI at ``/m`` (the server prints its mobile URL
+ a QR). This script never opens a browser.

Security note: the default (local) mode binds 127.0.0.1 — nothing off this
machine can reach the server. Phone/tailnet access is an explicit opt-in:
``mindflock serve tailscale`` binds ALL interfaces (0.0.0.0), so on a machine
with a LAN interface the port is reachable from that LAN too, not only the
tailnet. Any non-local bind auto-enables the auth gate: clients need the
printed access token (see ``core/auth.py``), so an unauthenticated LAN client
gets 401.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import List, Optional

# Make the repo root (the dir holding the `backend` package) importable
# no matter where this script is launched from, so `import backend.web...` works.
_SRC_ROOT = Path(__file__).resolve().parents[2]  # backend/web/run.py -> repo root
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# Distribution packages of the web dependency group; a ModuleNotFoundError for
# one of these means "web extras not installed", not a bug — say so plainly.
_WEB_DEPS = ("uvicorn", "fastapi", "starlette", "ptyprocess", "pyperclip", "segno")
WEB_DEPS_HINT = (
    "Web dependencies not installed. Reinstall with the web extra:\n"
    '  uv tool install --force "mindflock[web] @ git+https://github.com/MindFlock/MindFlock"\n'
    "  (or, in a source checkout:  uv sync --group web)"
)


def _port_squatter(host: str, port: int) -> str:
    """What's on ``host:port``: ``""`` (free), ``"mindflock"``, or ``"other"``.

    Advisory only (a race is possible, uvicorn still errors then); never
    raises. MindFlock is recognized by its ``/api/doctor`` endpoint answering
    with HTTP 200 (open) or 401/403 (auth gate on)."""
    import socket
    import urllib.request

    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        return ""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/doctor", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return "mindflock" if resp.status == 200 else "other"
    except urllib.error.HTTPError as err:
        return "mindflock" if err.code in (401, 403) else "other"
    except Exception:  # noqa: BLE001 — anything non-HTTP on the port
        return "other"


def _is_onboarded() -> bool:
    """Whether a session has ever been created (the web UI's first-run flag).
    Never raises — an unreadable settings store just means 'show the hint'."""
    try:
        from backend.config.settings import load_settings

        return bool(load_settings().general.onboarded)
    except Exception:  # noqa: BLE001 — advisory only
        return False


def _is_mindflock_source_repo(cwd: Path) -> bool:
    """True when ``cwd`` is the MindFlock source checkout itself (the usual
    "forgot to cd into my project" mistake). Never raises."""
    try:
        if (cwd / "src" / "mindflock").is_dir():
            return True
        py = cwd / "pyproject.toml"
        if py.is_file():
            import tomllib

            raw = tomllib.loads(py.read_text(encoding="utf-8"))
            return (raw.get("project", {}) or {}).get("name", "") == "mindflock"
    except Exception:  # noqa: BLE001 — the guard is advisory only
        pass
    return False


def _norm_mode(raw: Optional[str]) -> str:
    """Normalize a mode string to ``"local"`` / ``"tailscale"`` / ``""`` (unset).

    Unknown values map to ``"local"`` — the safe bind — never to exposure."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if s in ("tailscale", "ts", "all"):
        return "tailscale"
    return "local"


def _settings_serve_mode() -> str:
    """The persisted Settings → Mobile serve mode, or ``""``. Never raises —
    an unreadable settings store must not stop the server from booting."""
    try:
        from backend.config.settings import load_settings

        return str(load_settings().general.serve_mode or "")
    except Exception:  # noqa: BLE001 — advisory only
        return ""


def main(argv: Optional[List[str]] = None) -> None:
    try:
        import uvicorn
        from backend.web.server import (
            app,
        )  # noqa: WPS433 (import after sys.path setup)
    except ModuleNotFoundError as err:
        if err.name in _WEB_DEPS:
            print(WEB_DEPS_HINT, file=sys.stderr)
            raise SystemExit(1) from err
        raise

    # Mode + port from env first, then CLI tokens (either order); a bare
    # `run.py 9000` means port 9000. When neither the CLI nor the env
    # picks a mode, the persisted Settings → Mobile choice
    # (general.serve_mode) decides — that's how the desktop app's bare
    # `mindflock serve` auto-start comes up in tailscale mode after the user
    # flips the toggle. Final default is local (bind 127.0.0.1): exposure
    # beyond this machine is an explicit opt-in, and any non-local bind
    # auto-enables the auth gate.
    mode = _norm_mode(os.environ.get("CS_WEB_MODE"))
    port = int(os.environ.get("PORT") or os.environ.get("UVICORN_PORT") or 8765)
    for arg in (sys.argv[1:] if argv is None else argv):
        a = arg.strip().lower()
        if a in ("local", "localhost"):
            mode = "local"
        elif a in ("tailscale", "ts", "all"):
            mode = "tailscale"
        elif a.isdigit():
            port = int(a)
        # anything else is ignored (keeps the launcher forgiving)
    if not mode:
        mode = _norm_mode(_settings_serve_mode()) or "local"

    host = "127.0.0.1" if mode == "local" else "0.0.0.0"

    # Friendly double-launch handling: if the port is already taken, say what
    # is squatting on it instead of dying in uvicorn's raw "address already in
    # use". A MindFlock server already answering there is not an error — the
    # user's goal (a running server) is already met.
    squatter = _port_squatter("127.0.0.1", port)
    if squatter == "mindflock":
        print(
            f"A MindFlock server is already running at http://127.0.0.1:{port} "
            "— nothing to start. (Want a second one? mindflock serve --port 9000)"
        )
        raise SystemExit(0)
    if squatter == "other":
        print(
            f"Port {port} is already in use by something that isn't MindFlock.\n"
            f"Pick another port: mindflock serve --port {port + 1}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Export the resolved mode + port (CLI args win over the env) so the
    # server's startup banner knows local mode — skipping the tailnet URLs +
    # QR that a 127.0.0.1 bind can never serve (F7) — and prints the right
    # port when it was given positionally rather than via env/--port.
    os.environ["CS_WEB_MODE"] = mode
    os.environ["UVICORN_PORT"] = str(port)
    # 0.0.0.0 isn't a connectable address, so show a usable one. The address is
    # what the desktop app connects to (MINDFLOCK_URL); the server's own
    # startup banner prints the /m mobile URL(s) + a QR for tailscale mode.
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{shown}:{port}"
    cwd = Path(os.getcwd())
    print(f"MindFlock server  ->  {url}   (mode: {mode}, bind: {host})")
    # Sharp edge: a non-loopback bind with the auth gate resolved OFF (someone
    # set Settings -> Security -> Off, or MINDFLOCK_AUTH=0) puts the control API
    # — create sessions, drive agent terminals with repo write access — on the
    # LAN with no token. Exposure without auth must never be silent; warn loudly
    # (never abort: an explicit Off is the operator's call to make).
    if host != "127.0.0.1":
        try:
            from backend.web.core import auth as _auth

            gate_on = _auth.auth_enabled()
        except Exception:  # noqa: BLE001 — a broken check must not block serving
            gate_on = True
        if not gate_on:
            print(
                "\n\033[1;31m[mindflock] SECURITY WARNING:\033[0m bound to "
                f"{host} with the access-token gate OFF.\n"
                "  The control API is reachable UNAUTHENTICATED by anything that "
                "can route to\n  this host. Turn auth on before exposing it: "
                "Settings -> Security -> Always on\n  (or run with MINDFLOCK_AUTH=1).\n",
                file=sys.stderr,
            )
    print(f"Managing git repo    :  {cwd}")
    if _is_mindflock_source_repo(cwd):
        print(
            "note: managing the MindFlock repo itself — cd into your project "
            "first if this isn't what you want."
        )
    if not _is_onboarded():
        # First run (no session ever created): print the 3-step getting
        # started instead of leaving a stranger staring at a bare address.
        # The desktop app shows the same checklist as a setup card.
        print()
        print("First run? Three steps:")
        print("  1. open the MindFlock desktop app — it connects to this")
        print("     server automatically (to get it: electron/README.md)")
        print('  2. click "+ New" — that creates a session: an isolated git')
        print("     worktree + a tmux session running your coding agent")
        print("  3. click the session tile and type to talk to the agent")
        print("  (if anything is missing, `mindflock doctor` prints the fix)")
    print("Press Ctrl-C to stop.")

    # Fast preflight (background thread so binding isn't delayed): the checks
    # a session can't start without. A failure prints what's wrong, why the
    # tool needs it, and the one command that fixes it — instead of a cryptic
    # FileNotFoundError at first session-create.
    def _fast_preflight() -> None:
        try:
            from backend import doctor

            for fn in (doctor.check_git, doctor.check_tmux, doctor.check_agent_cli):
                c = fn()
                if c.status == "fail":
                    print(f"! {c.label}: {c.detail}", file=sys.stderr)
                    if c.fix:
                        print(f"  fix: {c.fix}", file=sys.stderr)
        except Exception:  # noqa: BLE001 — preflight must never break serving
            pass

    threading.Thread(target=_fast_preflight, daemon=True).start()

    # Never open a browser: the desktop app (Electron) is the client, and it
    # auto-starts/connects to this server itself. (The retired CS_WEB_OPEN
    # browser auto-open left with browser mode.)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
