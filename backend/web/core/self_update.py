"""Update the installed engine to the newest release (Settings → Advanced).

The desktop shell has had this since it shipped (``electron/main.js``'s
``engine:install``), but the shell is the one client that can do it that way.
Everyone else — a browser on the tailnet, the mobile view, a second machine
pointed at this server — could be told a newer version exists and had no way to
act on it short of finding the terminal that owns the install. So the server
does it to itself, running the same step ``install.sh`` runs: resolve the newest
release tag to a commit, and hand that spec to ``uv tool install --force``,
which is an in-place upgrade of the very tool venv this process is running out
of.

Three things make that safe to do from inside the process being replaced:

  * the installer runs **detached** (its own session via ``setsid`` where there
    is one), so it outlives the restart at the end rather than being killed by
    it;
  * progress lives in a **file**, not this process's memory, so a UI polling
    across the restart still learns how the update ended;
  * a **dev checkout is refused outright**. ``uv tool install --force`` would
    replace a developer's editable install with a release build, and no button
    in a settings screen should be able to quietly do that to someone's working
    tree — see :func:`install_kind`.

The restart itself is deliberately NOT the installer's job. Having the script
call back into the API would mean teaching it the port and the auth token, for
a request the UI is already making: :func:`finish_state` reports a finished
update once, and the route that serves it re-execs. That also keeps the failure
mode right — an update that succeeded while the tab was closed simply takes
effect at the next restart instead of leaving a half-authenticated curl in a
log somewhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Optional, Tuple

from backend import log
from backend.config import config as _config

#: Where release metadata is read from (``owner/repo``), and where the engine is
#: installed from. Both overridable for a fork or a staging repo, matching the
#: names ``install.sh`` and ``electron/main.js`` already honor.
UPDATE_REPO = os.environ.get("MINDFLOCK_UPDATE_REPO", "MindFlock/MindFlock")
INSTALL_REPO = os.environ.get(
    "MINDFLOCK_INSTALL_REPO", "https://github.com/MindFlock/MindFlock"
)

#: The interpreter uv pins the tool venv to (kept in step with ``install.sh``).
INSTALL_PYTHON = "3.12"

#: How long a release lookup is reused. GitHub's unauthenticated limit is 60
#: requests an hour and this endpoint is polled by every open settings screen,
#: so the answer is cached; "Check again" passes ``force`` to bypass it.
RELEASE_TTL_S = 15 * 60

#: Cap on the whole install, after which the script gives up and says so. The
#: desktop shell allows thirty minutes for the same work (a cold uv cache on a
#: slow link), and there is no reason to be stricter here.
INSTALL_TIMEOUT_S = 30 * 60

_release_cache: dict = {"at": 0.0, "value": None}


def _state_dir() -> Path:
    return Path(_config.GetConfigDir())


def state_path() -> Path:
    """The update's progress file. Outlives the server restart it triggers."""
    return _state_dir() / "update.json"


def log_path() -> Path:
    """Full installer output, for Settings → System logs and for support."""
    return _state_dir() / "update.log"


# --------------------------------------------------------------------------- #
# What is installed, and how
# --------------------------------------------------------------------------- #
def installed_version() -> str:
    """This engine's version, or "" when the package metadata is unreadable."""
    try:
        import backend

        return str(getattr(backend, "__version__", "") or "").strip()
    except Exception:  # noqa: BLE001 — a version line never breaks a screen
        return ""


def parse_version(text: str) -> Tuple[int, ...]:
    """``"v0.3.2"`` -> ``(0, 3, 2)``. Mirrors ``cmpVersion`` in the shell.

    Non-numeric junk becomes 0 rather than raising: a tag nobody expected must
    leave the button alone, not 500 the screen that asks about it.
    """
    cleaned = str(text or "").strip().lstrip("vV")
    parts = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is a version worth offering over ``current``.

    An unreadable version on either side answers False. "I don't know what you
    are running" is not grounds for offering to replace it.
    """
    if not latest or not current:
        return False
    a, b = parse_version(latest), parse_version(current)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return a > b


def install_kind() -> str:
    """How this engine is installed: ``uv-tool``, ``editable`` or ``other``.

    ``editable`` is the one that matters, and it is why this is a path check
    rather than a metadata check: an editable install leaves ``backend`` OUTSIDE
    site-packages (a ``.pth`` points at the developer's checkout), so the import
    that is running right now is the surest evidence of what kind of install
    this is. Reinstalling over it would swap that checkout for a release build
    and silently end the dev loop, so the update refuses.
    """
    try:
        import backend

        pkg = Path(str(backend.__file__)).resolve().parent
    except Exception:  # noqa: BLE001
        return "other"
    try:
        purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    except Exception:  # noqa: BLE001
        return "other"
    if purelib not in pkg.parents:
        return "editable"
    prefix = str(Path(sys.prefix).resolve()).replace(os.sep, "/")
    return "uv-tool" if "/uv/tools/" in prefix else "other"


def blocked_reason() -> str:
    """Why an update cannot be started here, or "" when it can.

    Answered before anything is spawned so the UI can say what is wrong while
    the button is still on screen, rather than after a detached script has
    already begun.
    """
    kind = install_kind()
    if kind == "editable":
        return (
            "This is a development checkout (an editable install), so updates "
            "come from git rather than the installer — `git pull` in the "
            "checkout, then restart the server."
        )
    if kind == "other":
        return (
            "This engine was not installed with `uv tool install`, so the "
            "in-app updater doesn't know how to replace it. Re-run install.sh, "
            "or update it the way it was installed."
        )
    if shutil.which("uv") is None:
        return "`uv` is not on the server's PATH, so the installer can't run."
    return ""


# --------------------------------------------------------------------------- #
# What is released
# --------------------------------------------------------------------------- #
async def latest_release(force: bool = False) -> Optional[dict]:
    """The newest published release, or ``None`` on ANY failure.

    Never raises and never reports a failure as "up to date" — a null answer is
    "couldn't tell", which the caller renders as exactly that. Unauthenticated
    on purpose: the releases of a public repo need no token, and an update check
    that depends on the user's GitHub credentials would break for the people
    most likely to need it.
    """
    now = time.time()
    if not force and _release_cache["value"] is not None:
        if now - float(_release_cache["at"]) < RELEASE_TTL_S:
            return _release_cache["value"]
    url = "https://api.github.com/repos/{}/releases/latest".format(UPDATE_REPO)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MindFlock-Engine",
    }
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — offline / DNS / TLS / rate limit
        return None
    tag = str((data or {}).get("tag_name") or "").strip()
    if not tag:
        return None
    value = {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "url": str((data or {}).get("html_url") or ""),
        "published_at": str((data or {}).get("published_at") or ""),
        "notes": str((data or {}).get("body") or "")[:4000],
    }
    _release_cache["at"] = now
    _release_cache["value"] = value
    return value


# --------------------------------------------------------------------------- #
# Progress, across the restart
# --------------------------------------------------------------------------- #
def read_state() -> dict:
    """The last known update state. ``{"state": "idle"}`` when there is none."""
    try:
        raw = state_path().read_text(encoding="utf-8")
    except OSError:
        return {"state": "idle"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"state": "idle"}
    return data if isinstance(data, dict) else {"state": "idle"}


def write_state(**fields) -> None:
    """Replace the state file atomically (the installer writes it too)."""
    path = state_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(fields), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("update: could not write %s (%v)", path, err)


def log_tail(limit: int = 60) -> list:
    """The last ``limit`` lines of installer output, for the UI's detail fold."""
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-limit:]


def running() -> bool:
    """Whether an install is in flight.

    A ``started`` state whose process is gone (the machine slept, the script was
    killed) is not running — reporting it as such would leave the button
    disabled for ever, so a stale marker is aged out instead.
    """
    st = read_state()
    if st.get("state") != "started":
        return False
    started_at = float(st.get("started_at") or 0)
    return (time.time() - started_at) < INSTALL_TIMEOUT_S


# --------------------------------------------------------------------------- #
# Doing it
# --------------------------------------------------------------------------- #
def _resolve_commit(ref: str) -> str:
    """The commit ``ref`` names on the install repo, or "" if it names none.

    Peeled tags first, exactly as ``install.sh`` does: an ANNOTATED tag's own
    ref points at the tag object rather than the commit, and installing that
    sha is a coin flip on whether the server will serve it.
    """
    for spec in ("refs/tags/{}^{{}}", "refs/tags/{}", "refs/heads/{}"):
        try:
            cp = subprocess.run(
                ["git", "ls-remote", INSTALL_REPO, spec.format(ref)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        out = cp.stdout.decode("utf-8", "replace").strip()
        if cp.returncode == 0 and out:
            return out.split()[0].strip()
    return ""


def _script(ref: str, commit: str) -> str:
    """The installer, as a shell script.

    Shell rather than Python on purpose: this runs while ``uv tool install
    --force`` is deleting and recreating the very venv a Python child would be
    importing from. ``/bin/sh`` has no such stake in the outcome.
    """
    spec = "mindflock[web] @ git+{}@{}".format(INSTALL_REPO, commit)
    return "\n".join(
        [
            "#!/bin/sh",
            "LOG=%s" % _sh_quote(str(log_path())),
            "STATE=%s" % _sh_quote(str(state_path())),
            "REF=%s" % _sh_quote(ref),
            "COMMIT=%s" % _sh_quote(commit),
            'printf "=== updating MindFlock to %s (%s) ===\\n" "$REF" "$COMMIT" >> "$LOG"',
            'PATH="$HOME/.local/bin:$PATH"; export PATH',
            'uv tool install --force --python %s %s >> "$LOG" 2>&1'
            % (
                INSTALL_PYTHON,
                _sh_quote(spec),
            ),
            "code=$?",
            'if [ "$code" -eq 0 ]; then st=done; else st=failed; fi',
            'printf \'{"state":"%s","ref":"%s","version":"%s","code":%s,'
            '"finished_at":%s}\' "$st" "$REF" "${REF#v}" "$code" "$(date +%s)"'
            ' > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"',
            'printf "=== %s (exit %s) ===\\n" "$st" "$code" >> "$LOG"',
            'exit "$code"',
        ]
    )


def _sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def start_update(ref: str) -> dict:
    """Begin the update to ``ref``. Returns ``{"ok": bool, "error": str}``.

    Everything that can be checked cheaply is checked here, while there is still
    a request to answer with: a dev checkout, a missing ``uv``, an install
    already running, a tag that resolves to nothing. Past that point the script
    owns the outcome and the state file is the only channel.
    """
    reason = blocked_reason()
    if reason:
        return {"ok": False, "error": reason}
    if running():
        return {"ok": False, "error": "an update is already running"}
    ref = str(ref or "").strip()
    if not ref or any(c.isspace() for c in ref):
        return {"ok": False, "error": "no release to update to"}
    commit = _resolve_commit(ref)
    if not commit:
        return {
            "ok": False,
            "error": "could not resolve %s on %s" % (ref, INSTALL_REPO),
        }

    script_path = _state_dir() / "update.sh"
    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_script(ref, commit), encoding="utf-8")
        script_path.chmod(0o700)
    except OSError as err:
        return {"ok": False, "error": "could not write the installer: %s" % err}

    write_state(
        state="started",
        ref=ref,
        version=ref.lstrip("vV"),
        commit=commit,
        from_version=installed_version(),
        started_at=time.time(),
    )
    try:
        # Detached, and deliberately so: the last thing this update does is
        # restart the server, and a child in this process group would be
        # restarted with it — halfway through replacing its own venv.
        argv = ["/bin/sh", str(script_path)]
        if shutil.which("setsid"):
            argv = ["setsid"] + argv
        subprocess.Popen(  # noqa: S603
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(Path.home()),
        )
    except (OSError, subprocess.SubprocessError) as err:
        write_state(state="failed", ref=ref, error=str(err), finished_at=time.time())
        return {"ok": False, "error": "could not start the installer: %s" % err}
    return {"ok": True, "ref": ref, "commit": commit}


def finish_state() -> Tuple[dict, bool]:
    """The current state, plus whether the caller should now restart the server.

    True is answered exactly once per successful update — the flag is cleared in
    the state file before returning — because the restart is a re-exec and the
    poll that triggers it will be repeated by every client that has the screen
    open.
    """
    st = read_state()
    st["log"] = log_tail()
    if st.get("state") == "done" and not st.get("restarted"):
        write_state(
            **{**{k: v for k, v in st.items() if k != "log"}, "restarted": True}
        )
        return st, True
    return st, False
