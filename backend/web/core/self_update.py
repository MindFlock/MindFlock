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
call back into the API would mean teaching it the auth token, so the server
restarts ITSELF instead: :func:`finish_state` reports a finished update once,
and both the server's own watcher (:mod:`backend.web.core.update_watch`, no
browser tab needed) and the ``/api/update/state`` route that a polling screen
hits act on it. A server that is already running the installed build (it
re-execed, or was restarted by hand) never restarts for it again — see
:func:`applied`.

What the installer DOES do after a good install is read the server's public
hello (no token: ``/api/remote/hello``) until the new build answers, and when
the server went down and never came back, put the previous commit back and
start it again (``rolled_back``) — a headless machine must not be left with an
engine that can't boot and nobody at a terminal to notice.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

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

#: After a good install, how long the installer waits for the restarted
#: server to answer with the new build before it puts the previous one back.
HEALTH_TIMEOUT_S = 90

#: What another device may ask this one to install: a release tag, nothing
#: else (no branch, no commit, no "main"). See :func:`check_remote_ref`.
_RELEASE_TAG = re.compile(r"^v?[0-9]+(\.[0-9]+){1,3}$")

_release_cache: dict = {"at": 0.0, "value": None}
_tag_cache: Dict[str, Tuple[float, Optional[dict]]] = {}

#: The installer this process spawned (``poll()`` reaps it, so a finished one
#: never lingers as a zombie that :func:`_pid_alive` would call alive).
_PROC: Optional[subprocess.Popen] = None


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


def _read_commit() -> str:
    """The commit this engine was installed from, per its dist-info's
    ``direct_url.json`` (``vcs_info.commit_id``) — "" for an editable or local
    install, or when the metadata can't be read. The build backend is
    uv_build, which stamps no VCS version into the package itself, so this is
    the one place the installed commit is recorded."""
    try:
        from importlib.metadata import distribution

        raw = distribution("mindflock").read_text("direct_url.json") or ""
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 — a version line never breaks a screen
        return ""
    commit = ((data or {}).get("vcs_info") or {}).get("commit_id") or ""
    commit = str(commit).strip().lower()
    return commit if re.fullmatch(r"[0-9a-f]{7,64}", commit) else ""


#: Read ONCE, when this process imports the module (the server does at boot).
#: ``uv tool install --force`` rewrites the dist-info on disk while the old
#: process is still serving, and a commit read later would name code this
#: process isn't running — the very stale-copy confusion it exists to expose.
_BOOT_COMMIT = _read_commit()


def installed_commit() -> str:
    """The commit THIS process is running, "" when unknown (see
    :data:`_BOOT_COMMIT`). Reported in the hello so two devices that both say
    0.7.4 but run different builds can tell."""
    return _BOOT_COMMIT


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


async def _github_release(path: str) -> Optional[dict]:
    """GET ``/repos/<UPDATE_REPO>/releases/<path>`` as a release dict, or None."""
    url = "https://api.github.com/repos/{}/releases/{}".format(UPDATE_REPO, path)
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
    return data if isinstance(data, dict) else None


async def published_release(tag: str) -> Optional[dict]:
    """The PUBLISHED release named ``tag``, or None (not one, or GitHub can't
    say). A tag alone is not enough: anyone with push access can cut a tag,
    and a draft or deleted release is not something to install remotely."""
    tag = str(tag or "").strip()
    if not _RELEASE_TAG.match(tag):
        return None
    now = time.time()
    hit = _tag_cache.get(tag)
    if hit is not None and hit[1] is not None and now - hit[0] < RELEASE_TTL_S:
        return hit[1]
    data = await _github_release("tags/" + tag)
    if not data or data.get("draft") or str(data.get("tag_name") or "") != tag:
        return None
    _tag_cache[tag] = (now, data)
    return data


def is_release_tag(ref: str) -> bool:
    """Whether ``ref`` is shaped like a release tag (``v0.7.4``)."""
    return bool(_RELEASE_TAG.match(str(ref or "").strip()))


async def check_remote_ref(ref: str) -> Tuple[str, int]:
    """Whether ANOTHER machine may have this engine install ``ref``:
    ``("", 200)`` when it may, else ``(reason, status)``.

    Only a published release tag at or above the running version. A branch,
    a commit or an older release is a developer's move to make at this
    machine's own keyboard (``/api/update/start`` from loopback keeps
    accepting any ref) — from anywhere else it is a downgrade or a jump onto
    unreviewed code by whoever could reach the port."""
    ref = str(ref or "").strip()
    if not is_release_tag(ref):
        return (
            "only a release tag (like v1.2.3) can be installed from another "
            "device — %r isn't one" % ref[:80],
            400,
        )
    current = installed_version()
    if current and is_newer(current, ref):
        return (
            "%s is older than this engine (v%s) — downgrades can only be "
            "started on this machine" % (ref, current),
            400,
        )
    if await published_release(ref) is None:
        return (
            "%s isn't a published MindFlock release (or GitHub couldn't be "
            "reached to check)" % ref,
            400,
        )
    return "", 200


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


def _pid_alive(pid: int) -> bool:
    """Whether process ``pid`` still runs (a zombie does not count)."""
    if _PROC is not None and _PROC.pid == pid:
        try:
            return _PROC.poll() is None
        except Exception:  # noqa: BLE001
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process now holds the pid; be cautious
    except OSError:
        return False
    # Our own child from before a re-exec is never reaped (the new image has
    # no Popen for it), so it can sit as a zombie: kill(0) succeeds on those.
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            if fh.read().rsplit(")", 1)[-1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass
    return True


def running() -> bool:
    """Whether an install is in flight.

    A ``started`` state whose process is gone (the machine slept, the script was
    killed) is not running — reporting it as such would leave the button
    disabled for ever. When the state names the installer's PID, a dead PID
    settles it at once (``failed``, ``error: "interrupted"`` — the screen says
    so and offers to try again); without one, the marker ages out after
    :data:`INSTALL_TIMEOUT_S`.
    """
    st = read_state()
    if st.get("state") != "started":
        return False
    started_at = float(st.get("started_at") or 0)
    if (time.time() - started_at) >= INSTALL_TIMEOUT_S:
        return False
    try:
        pid = int(st.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and not _pid_alive(pid):
        # Re-read first: the script writes its final state and THEN exits, so
        # a dead PID may just mean it finished a moment ago.
        now = read_state()
        if now.get("state") == "started" and now.get("started_at") == st.get(
            "started_at"
        ):
            write_state(
                **{
                    **now,
                    "state": "failed",
                    "error": "interrupted",
                    "finished_at": time.time(),
                }
            )
        return False
    return True


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


def _script(
    ref: str,
    commit: str,
    *,
    from_version: str = "",
    prev_commit: str = "",
    started_at: float = 0.0,
    health_url: str = "",
    health_timeout: int = HEALTH_TIMEOUT_S,
    relaunch: str = "",
) -> str:
    """The installer, as a shell script.

    Shell rather than Python on purpose: this runs while ``uv tool install
    --force`` is deleting and recreating the very venv a Python child would be
    importing from. ``/bin/sh`` has no such stake in the outcome.

    After a good install, and only when ``health_url`` (this server's public
    hello on loopback) answered just before the state flips to ``done`` — a
    server bound somewhere the script can't reach is never "down" — the
    script watches that hello for ``health_timeout`` seconds:

      * it answers with ``commit`` (or the new version): healthy;
      * it still answers with the OLD build: the server simply hasn't
        restarted yet — leave it, the update takes effect when it does;
      * it stopped answering and never came back: the new engine can't boot.
        Put ``prev_commit`` back (``rolled_back``) and start the server again
        with ``relaunch`` (the command line it was running with) unless
        something else already did.
    """
    spec = "mindflock[web] @ git+{}@{}".format(INSTALL_REPO, commit)
    prev_spec = (
        "mindflock[web] @ git+{}@{}".format(INSTALL_REPO, prev_commit)
        if prev_commit
        else ""
    )
    keep = json.dumps(
        {
            "commit": commit,
            "from_version": from_version,
            "prev_commit": prev_commit,
            "started_at": started_at,
        }
    )[1:-1]
    install = "uv tool install --force --python %s" % INSTALL_PYTHON
    return "\n".join(
        [
            "#!/bin/sh",
            "LOG=%s" % _sh_quote(str(log_path())),
            "STATE=%s" % _sh_quote(str(state_path())),
            "REF=%s" % _sh_quote(ref),
            "COMMIT=%s" % _sh_quote(commit),
            "VERSION=%s" % _sh_quote(ref.lstrip("vV")),
            "FROM=%s" % _sh_quote(from_version),
            "PREV=%s" % _sh_quote(prev_commit),
            "KEEP=%s" % _sh_quote(keep),
            "HEALTH_URL=%s" % _sh_quote(health_url),
            "HEALTH_TIMEOUT=%d" % int(health_timeout),
            "RELAUNCH=%s" % _sh_quote(relaunch),
            'printf "=== updating MindFlock to %s (%s) ===\\n" "$REF" "$COMMIT" >> "$LOG"',
            'PATH="$HOME/.local/bin:$PATH"; export PATH',
            # put STATE CODE [EXTRA]: the whole state file, atomically.
            "put() {",
            '  printf \'{"state":"%s","ref":"%s","version":"%s","code":%s,'
            '"finished_at":%s,%s%s}\' "$1" "$REF" "$VERSION" "$2" "$(date +%s)"'
            ' "$KEEP" "${3:-}" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"',
            "}",
            'hello() { curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null; }',
            '%s %s >> "$LOG" 2>&1' % (install, _sh_quote(spec)),
            "code=$?",
            'if [ "$code" -ne 0 ]; then',
            '  put failed "$code"',
            '  printf "=== failed (exit %s) ===\\n" "$code" >> "$LOG"',
            '  exit "$code"',
            "fi",
            'watch=""',
            'if [ -n "$HEALTH_URL" ] && command -v curl >/dev/null 2>&1 && hello >/dev/null; then watch=1; fi',
            "put done 0",
            'printf "=== done (exit 0) ===\\n" >> "$LOG"',
            '[ -n "$watch" ] || exit 0',
            'printf "=== waiting for the server to come back on %s ===\\n" "$REF" >> "$LOG"',
            'waited=0; seen=""',
            # The hello is compact JSON: `"commit":"<sha>"`, `"version":"x"`.
            'PAT_C=\'"commit":"\'"$COMMIT"\'"\'',
            'PAT_V=\'"version":"\'"$VERSION"\'"\'',
            'while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do',
            "  sleep 3; waited=$((waited + 3))",
            '  body="$(hello)" || body=""',
            '  case "$body" in *"$PAT_C"*) seen=ok; break ;; esac',
            '  if [ -n "$body" ] && [ "$VERSION" != "$FROM" ]; then',
            '    case "$body" in *"$PAT_V"*) seen=ok; break ;; esac',
            "  fi",
            '  if [ -n "$body" ]; then seen=old; else seen=down; fi',
            "done",
            'case "$seen" in',
            "  ok)",
            "    put done 0 ',\"healthy\":true'",
            '    printf "=== %s is up ===\\n" "$REF" >> "$LOG" ;;',
            "  old)",
            '    printf "=== the server has not restarted yet; it runs %s once it does ===\\n" "$REF" >> "$LOG" ;;',
            "  *)",
            '    if [ -z "$PREV" ]; then',
            '      put failed 1 \',"error":"the server did not come back after the update"\'',
            '      printf "=== the server did not come back ===\\n" >> "$LOG"',
            "      exit 1",
            "    fi",
            '    printf "=== the server did not come back on %s; putting %s back ===\\n" "$REF" "$PREV" >> "$LOG"',
            '    %s %s >> "$LOG" 2>&1' % (install, _sh_quote(prev_spec)),
            "    rc=$?",
            '    if [ "$rc" -ne 0 ]; then',
            '      put failed "$rc" \',"error":"the new version did not start, and putting the previous one back failed"\'',
            '      exit "$rc"',
            "    fi",
            '    if [ -n "$RELAUNCH" ] && ! hello >/dev/null; then',
            '      (eval "$RELAUNCH") >> "$LOG" 2>&1 < /dev/null &',
            "    fi",
            '    put rolled_back 0 \',"error":"the new version did not start, so the previous one was put back"\'',
            '    printf "=== rolled back to %s ===\\n" "$PREV" >> "$LOG"',
            "    exit 1 ;;",
            "esac",
            "exit 0",
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

    started_at = time.time()
    from_version = installed_version()
    script_path = _state_dir() / "update.sh"
    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            _script(
                ref,
                commit,
                from_version=from_version,
                prev_commit=installed_commit(),
                started_at=started_at,
                health_url=_health_url(),
                relaunch=_relaunch_command(),
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o700)
    except OSError as err:
        return {"ok": False, "error": "could not write the installer: %s" % err}

    fields = dict(
        state="started",
        ref=ref,
        version=ref.lstrip("vV"),
        commit=commit,
        from_version=from_version,
        # What a rollback reinstalls (the script carries it too): "" for an
        # install whose commit isn't recorded, which then can't roll back.
        prev_commit=installed_commit(),
        started_at=started_at,
    )
    write_state(**fields)
    global _PROC
    try:
        # Detached, and deliberately so: the last thing this update does is
        # restart the server, and a child in this process group would be
        # restarted with it — halfway through replacing its own venv.
        # ``start_new_session`` IS setsid(2); the setsid(1) wrapper this used
        # to add forks when its caller already leads a group, so the PID
        # recorded below would have been a parent that exits at once.
        proc = subprocess.Popen(  # noqa: S603
            ["/bin/sh", str(script_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(Path.home()),
        )
    except (OSError, subprocess.SubprocessError) as err:
        write_state(state="failed", ref=ref, error=str(err), finished_at=time.time())
        return {"ok": False, "error": "could not start the installer: %s" % err}
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and pid > 0:
        _PROC = proc
        # Only while it is still ours to describe (a very fast installer may
        # already have written its own final state).
        if read_state().get("state") == "started":
            write_state(**fields, pid=pid)
    return {"ok": True, "ref": ref, "commit": commit}


def _health_url() -> str:
    """This server's public hello on loopback, for the installer's health
    check — "" outside a serving process (a CLI-run update has no server to
    watch). Never the auth token: the hello needs none."""
    try:
        from backend.web.core import restart as _restart

        if not _restart.serving():
            return ""
        from backend.providers import mcp_attach

        return "http://127.0.0.1:%d/api/remote/hello" % int(mcp_attach.server_port())
    except Exception:  # noqa: BLE001
        return ""


def _relaunch_command() -> str:
    """How the installer starts this server again after a rollback, as one
    shell line ("" when this process isn't the server)."""
    try:
        from backend.web.core import restart as _restart

        if not _restart.serving():
            return ""
        argv = _restart.relaunch_argv()
        return "unset CS_WEB_MODE; cd %s && exec %s" % (
            _sh_quote(os.getcwd()),
            " ".join(_sh_quote(a) for a in argv),
        )
    except Exception:  # noqa: BLE001
        return ""


def applied(st: Optional[dict] = None) -> bool:
    """Whether THIS process already runs the build the last finished update
    installed — the commit matches, or (no commit to compare) the version is
    the new one. Such a process must never restart for that update again: it
    is the restart."""
    st = read_state() if st is None else st
    mine = installed_commit()
    theirs = str(st.get("commit") or "")
    if mine and theirs:
        return mine == theirs
    version = str(st.get("version") or "")
    return (
        bool(version)
        and version == installed_version()
        and (str(st.get("from_version") or "") != version)
    )


def restart_pending(st: Optional[dict] = None) -> bool:
    """A finished install this process isn't running yet (it restarts soon —
    the watcher's next tick — or is held while an install terminal runs)."""
    st = read_state() if st is None else st
    return st.get("state") == "done" and not applied(st)


def finish_state() -> Tuple[dict, bool]:
    """The current state, plus whether the caller should now restart the server.

    True is answered exactly once per successful update — the flag is cleared in
    the state file before returning — because the restart is a re-exec and the
    poll that triggers it will be repeated by every client that has the screen
    open (and by the server's own watcher). A process that already runs the
    installed build (:func:`applied`) gets False and marks it restarted: the
    installer rewrites the state after its health check, and that must not
    read as a second update to restart for.
    """
    st = read_state()
    st["log"] = log_tail()
    if st.get("state") == "done" and not st.get("restarted"):
        write_state(
            **{**{k: v for k, v in st.items() if k != "log"}, "restarted": True}
        )
        return st, not applied(st)
    return st, False
