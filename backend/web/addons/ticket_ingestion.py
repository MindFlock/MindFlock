"""MindFlock addon: start/stop the ticket-ingestion pipeline + stream its log.

Wraps the pipeline as a managed child process (``TicketIngestionController``) and exposes
it over ``/api/mindflock/*`` + a log-tail websocket. Satisfies the
:class:`ManagedProcess` protocol so a generic start/stop/logs UI can drive it.
"""

from __future__ import annotations

import asyncio
import datetime as _datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from backend import log
from backend.config import home_guard
from backend.session import provisioned as provisioning

from backend.web.core import pending as _pending
from backend.web.core import pipeline_root
from backend.web.core.terminal import pump_pty, spawn_tail

from .base import Addon, AppContext, FrontendDescriptor


def _resolve_repo_root() -> Path:
    """Where the pipeline keeps state.json / workspaces/ — see
    :func:`backend.web.core.pipeline_root.resolve_repo_root`."""
    return pipeline_root.resolve_repo_root(__file__)


def _ingestion_repo_available() -> bool:
    """Whether ingestion has a repo to provision into. True when the global
    ``[repository].url`` resolves (:func:`provisioning.provisioning_available`)
    OR any configured ticketing source names its own ``repo_url`` — the
    per-source repos are the primary path now that there is no global default."""
    if provisioning.provisioning_available():
        return True
    try:
        from backend.config import settings as _s

        return any(
            (getattr(src, "repo_url", "") or "").strip()
            for src in _s.load_settings().ticketing.sources
        )
    except Exception:  # noqa: BLE001 — never let a settings read break the gate
        return False


def _desired_running() -> bool:
    """The last state the ingestion toggle was set to (settings store).

    Written on every successful ``/api/mindflock/start|stop`` so a server
    restart can put the pipeline back in the state the user left it in."""
    try:
        from backend.config import settings as _s

        return bool(_s.load_settings().general.ingestion_autostart)
    except Exception:  # noqa: BLE001 — never let a settings read break status
        return False


def _record_desired_running(on: bool) -> None:
    """Persist the toggle state (best-effort — a failed write never fails the
    start/stop request itself)."""
    try:
        from backend.config import settings as _s

        if bool(_s.load_settings().general.ingestion_autostart) != on:
            _s.update_settings(general={"ingestion_autostart": on})
    except Exception:  # noqa: BLE001
        pass


def _may_restart_found_pipeline() -> bool:
    """Whether boot may restart a pipeline it finds running on an older
    build. Never under pytest: the repo root resolves to the developer's real
    checkout there, and its pipeline is theirs (tests patch this)."""
    return "pytest" not in sys.modules


#: Marks the pipeline child as the server's (``backend.ticket_ingestion.__main__``
#: records it in the lock file as ``owner``; same literal there).
PIPELINE_OWNER_ENV = "MINDFLOCK_PIPELINE_OWNER"


#: Seconds between re-checks of which device runs PR review / issue handling:
#: the answer follows the group's roster, which can change by gossip with no
#: event here (a device removed elsewhere), and the pipeline is wired with it
#: at boot.
AUTOMATION_RECHECK = 30.0


def _automation_here() -> bool:
    """Whether THIS device runs the GitHub halves (PR review, issue handling).
    Their settings follow the person to every one of their devices; one
    device of the group runs them (``settings_hooks.automation_here``: the
    synced ``github.automation_device``). A lone device always does."""
    try:
        from backend.web.core import settings_hooks

        return settings_hooks.automation_here()
    except Exception:  # noqa: BLE001
        return True


def _observe_automation(state_dir: Optional[Path] = None) -> bool:
    """:func:`_automation_here`, also recorded beside the pipeline's ledger
    (``state.note_automation``) so a pipeline that becomes the runner later
    knows PR review / issue handling moved here and from where — it then
    skips what the other device already handled. Never raises."""
    here = _automation_here()
    runner = ""
    if not here:
        try:
            from backend.web.core import settings_hooks

            runner = settings_hooks.automation_device()
        except Exception:  # noqa: BLE001
            runner = ""
    try:
        from backend.ticket_ingestion import state as _state

        _state.note_automation(
            state_dir if state_dir is not None else _resolve_repo_root(), here, runner
        )
    except Exception:  # noqa: BLE001 — a status read never fails on this
        pass
    return here


def _automation_status(state_dir: Optional[Path] = None) -> dict:
    """``{automation_here, automation_device}`` for the status payload:
    whether this device runs PR review / issue handling and, when it
    doesn't, which of your devices does (``""``: unknown)."""
    here = _observe_automation(state_dir)
    device = ""
    if not here:
        try:
            from backend.web.core import settings_hooks

            device = settings_hooks.automation_device()
        except Exception:  # noqa: BLE001 — status must never fail on this
            device = ""
    return {"automation_here": here, "automation_device": device}


def _pr_review_enabled() -> bool:
    """Whether the automated-PR-review half is switched on: ``github.enabled``
    (unset counts as on, matching the UI) AND at least one repo to watch AND
    this device is the one that runs it (:func:`_automation_here`)."""
    try:
        from backend.config import settings as _s

        gh = _s.load_settings().github
        return (gh.enabled is not False) and bool(gh.repos) and _automation_here()
    except Exception:  # noqa: BLE001 — never let a settings read break the gate
        return False


def _issue_handling_enabled() -> bool:
    """Whether the automated issue-handling half is switched on:
    ``github.issues_enabled`` (opt-in — unset counts as OFF, unlike PR review)
    AND at least one repo in its own ``issue_repos`` list AND this device is
    the one that runs it (:func:`_automation_here`)."""
    try:
        from backend.config import settings as _s

        gh = _s.load_settings().github
        return (
            (gh.issues_enabled is True)
            and bool(gh.issue_repo_list())
            and _automation_here()
        )
    except Exception:  # noqa: BLE001 — never let a settings read break the gate
        return False


#: The env var that pins the pipeline to ONE ticketing source built from the
#: environment (``MINDFLOCK_TICKET_TOKEN``… — see
#: ``config._resolve_ticketing_layer``). While it is set every source saved
#: in the Intake dialog is ignored, silently unless the status says so.
TICKET_PROVIDER_ENV = "MINDFLOCK_TICKET_PROVIDER"


def _env_override() -> Optional[dict]:
    """``{provider, hides_ui_sources: True}`` while :data:`TICKET_PROVIDER_ENV`
    pins the pipeline to an env-built source, else ``None`` — so the UI can
    say why the sources it shows aren't the ones being polled."""
    provider = (os.environ.get(TICKET_PROVIDER_ENV) or "").strip()
    if not provider:
        return None
    return {"provider": provider, "hides_ui_sources": True}


# Leading ISO timestamp on pipeline log lines ("2026-07-22T10:24:30 [ERROR] ...").
_LOG_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"
)

# Connectivity failures as they appear in the ingestion log (aiohttp/OS errors:
# DNS, refused/reset, unreachable, timeouts) — a network outage, which clears
# on its own. Auth failures are classified separately (_AUTH_ERROR_RE): they
# used to be dropped here as "misconfiguration, not an outage", which left a
# revoked token with no indicator at all.
_CONN_ERROR_RE = re.compile(
    r"cannot connect to host"
    r"|name resolution"
    r"|connection refused"
    r"|connection reset"
    r"|network is unreachable"
    r"|getaddrinfo"
    r"|clientconnector"
    r"|timed out",
    re.IGNORECASE,
)

# A tracker or GitHub refusing the credentials: the providers' own wording
# ("Shortcut rejected the token (HTTP 401)", "Jira rejected the credentials
# (HTTP 403)"), the monitors' ("GitHub /pulls returned 401 for o/r: Bad
# credentials") and the generic HTTP spellings. A bare "401"/"403" only
# counts next to an HTTP marker so a PR or ticket #403 never trips it.
_AUTH_ERROR_RE = re.compile(
    r"rejected the (?:token|credentials|api key)"
    r"|(?:HTTP|status|returned|code)\s+40[13]\b"
    r"|\b40[13]\s+(?:unauthori[sz]ed|forbidden)"
    r"|\bunauthori[sz]ed\b"
    r"|\bforbidden\b"
    r"|bad credentials"
    r"|invalid (?:api )?(?:token|key)"
    r"|authentication failed",
    re.IGNORECASE,
)

# Which source an error line is about, in order of confidence: the
# orchestrator quotes the source id ("Poll scan for 'sc' failed"), the GitHub
# monitors name the repo, a provider's own logger names the provider.
_SOURCE_QUOTED_RE = re.compile(r"(?:source|scan for|for source) '([^']+)'")
_GITHUB_REPO_RE = re.compile(r"GitHub /\w+ returned \d+ for (\S+?):")
_PROVIDER_LOGGER_RE = re.compile(r"backend\.ticket_ingestion\.providers\.(\w+)")
_PROVIDER_WORD_RE = re.compile(r"\b(Shortcut|Jira|Linear|Asana|GitHub)\b")

#: Seconds an error line in the log stays "current". The pipeline retries
#: every poll (~20 s tickets, ~60 s PRs), so a standing failure keeps
#: re-logging and holds; after recovery the last line ages out on its own.
ERROR_WINDOW = 180

#: Written by the pipeline (``backend.ticket_ingestion.__main__``) when it
#: exits over a configuration error or a crash: ``{pid, at, kind, message,
#: fields}``. Same literal there.
EXIT_RECORD = ".mindflock-pipeline-exit.json"


def _error_source(line: str) -> str:
    """The source / service an error line names (``""`` when it doesn't)."""
    m = _SOURCE_QUOTED_RE.search(line)
    if m:
        return m.group(1)
    m = _GITHUB_REPO_RE.search(line)
    if m:
        return "GitHub %s" % m.group(1)
    m = _PROVIDER_LOGGER_RE.search(line)
    if m:
        return m.group(1)
    m = _PROVIDER_WORD_RE.search(line)
    return m.group(1) if m else ""


def _newest_error(recent: dict) -> Optional[dict]:
    """The most recent of the classified error lines (auth wins a tie)."""
    if not recent:
        return None
    return max(
        recent.values(),
        key=lambda e: (str(e.get("at") or ""), e.get("kind") == "auth"),
    )


def _error_text(line: str) -> str:
    """An error line without its timestamp / level / logger prefix, capped."""
    text = _LOG_TS_RE.sub("", line, count=1).strip()
    text = re.sub(r"^\[\w+\]\s*", "", text)
    text = re.sub(r"^[\w.]+:\s+", "", text, count=1)
    return text[:240]


class TicketIngestionController:
    """Starts/stops the Shortcut-ingestion pipeline as a managed child process.

    The pipeline runs exactly as it does standalone (``python -m
    backend.ticket_ingestion`` from the repo root, where ``config.toml`` lives),
    in its own process group so we can stop the whole tree. Its sessions are
    created via the in-process engine bridge and persisted to ``state.json``;
    the engine reloader surfaces them in this server's grid.
    """

    def __init__(self) -> None:
        # Where config.toml + state.json live. Resolved robustly so an installed
        # copy (uv tool) doesn't spawn the pipeline in the interpreter lib dir with
        # a stray empty ledger — see :func:`_resolve_repo_root`.
        self._repo_root = _resolve_repo_root()
        self._log_path = self._repo_root / "logs" / "ticket-ingestion.log"
        self._proc: Optional[subprocess.Popen] = None
        self._started_at: Optional[float] = None
        #: When this server first noticed its own child gone (the child's
        #: exit time, near enough — Popen doesn't record one).
        self._exited_at: Optional[float] = None

    def _own_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _lock_path(self) -> Path:
        return self._repo_root / ".mindflock-pipeline.lock"

    def _external_lock_pid(self) -> Optional[int]:
        """PID of a pipeline holding the singleton lock, or None if not held.

        Authoritative via a non-blocking ``flock`` probe (a stale lockfile whose
        owner died is detectable because we can re-acquire it). Returns the PID
        recorded in the file for display / stop, or ``-1`` if held but the PID
        couldn't be read. This sees pipelines started by *anyone* (e.g. a
        standalone run, or an orphan surviving a backend.web restart) — not just our
        own child.
        """
        return self._lock_holder()[0]

    def _lock_holder(self) -> "tuple[Optional[int], Optional[dict]]":
        """``(pid, meta)`` of the pipeline holding the lock — see
        :meth:`_external_lock_pid` for the PID; ``meta`` is the build it
        recorded on line 2 (``{version, commit, owner}``), None for a lock
        written by a pipeline from before it recorded one."""
        p = self._lock_path()
        if not p.exists():
            return None, None
        try:
            fh = open(p, "a+")
        except OSError:
            return None, None
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    fh.seek(0)
                    lines = (fh.read() or "").splitlines()
                except OSError:
                    return -1, None
                try:
                    pid = int((lines[0] if lines else "").strip() or 0) or -1
                except ValueError:
                    pid = -1
                meta = None
                if len(lines) > 1:
                    try:
                        meta = json.loads(lines[1])
                    except ValueError:
                        meta = None
                return pid, meta if isinstance(meta, dict) else None
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # nobody holds it -> stale
                return None, None
        finally:
            fh.close()

    def stale_build(self) -> bool:
        """Whether the running pipeline is one this server started on an
        OLDER engine build than the one serving now (or one from before the
        lock recorded its build at all), so it must be restarted: the server
        re-execs onto an update, but the child is its own process group and
        keeps the previous code until someone stops it. A standalone run
        (started outside the server) is never touched. A lock with no
        metadata (a pipeline from before the lock recorded its build) is
        owner-unknown: stale here, but :meth:`IngestionAddon._boot_reconcile`
        restarts it only when the pipeline is wanted anyway."""
        pid, meta = self._lock_holder()
        if pid is None:
            return False
        if meta is None:
            return True  # an older pipeline: necessarily an older build
        if meta.get("owner") != "server":
            return False
        try:
            from backend.web.core import self_update as _su

            mine_v, mine_c = _su.installed_version(), _su.installed_commit()
        except Exception:  # noqa: BLE001
            return False
        theirs_c = str(meta.get("commit") or "")
        if mine_c and theirs_c:
            return mine_c != theirs_c
        return bool(mine_v) and str(meta.get("version") or "") != mine_v

    def is_running(self) -> bool:
        return self._own_running() or (self._external_lock_pid() is not None)

    def _python(self) -> str:
        """The SERVER'S own interpreter: the pipeline is part of the same
        ``backend`` package, so it runs the code this server runs.

        It used to prefer ``<repo root>/.venv/bin/python`` (from when the
        server's venv lacked the pipeline package). With the repo root a dev
        checkout on some old branch, that ran THAT checkout's code: a server
        on 0.9.4 kept provisioning ticket sessions with a pipeline reporting
        0.8.7, so no provisioning fix ever reached a ticket.
        """
        return sys.executable

    @staticmethod
    def _server_package_root() -> str:
        """The directory holding the ``backend`` package this server imported."""
        import backend

        return str(Path(backend.__file__).resolve().parent.parent)

    def _env(self) -> dict:
        """Env for the pipeline child: put the CS engine (``mindflock``) and
        the src-layout pipeline on PYTHONPATH so the in-process bridge imports
        regardless of which venv we picked.

        The child launches sessions through the engine too, so it also attaches
        their MindFlock MCP (:mod:`backend.providers.mcp_attach`) — and must
        point it at THIS server with THIS server's interpreter and package: its
        own ``sys.executable`` may be another venv (see :meth:`_python`), and a
        child started without the server's env has no ``UVICORN_PORT`` (its
        ``PORT``, from an agent shell, is a session dev port)."""
        env = dict(os.environ)
        # This child is the server's: restarted after an engine update when
        # its recorded build is older (see stale_build).
        env[PIPELINE_OWNER_ENV] = "server"
        extra = [self._server_package_root(), str(self._repo_root / "src")]
        if env.get("PYTHONPATH"):
            extra.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(extra)
        try:
            from backend.providers import mcp_attach

            env["MINDFLOCK_MCP_PYTHON"] = mcp_attach.mcp_python()
            env["MINDFLOCK_MCP_PYTHONPATH"] = mcp_attach.mcp_pythonpath()
            env["MINDFLOCK_SERVER_PORT"] = str(mcp_attach.server_port())
        except Exception:  # noqa: BLE001 — never block the pipeline over the MCP
            pass
        # Which device runs PR review / issue handling is the server's to
        # say: the child can't tell which of the user's devices it is on.
        try:
            from backend.ticket_ingestion.config import AUTOMATION_ENV

            env[AUTOMATION_ENV] = "1" if _observe_automation(self._repo_root) else "0"
        except Exception:  # noqa: BLE001
            pass
        # …and whether it is grouped with another live device: a PR review /
        # issue ledger that never ran here is then seeded before its first
        # scan (another member may have handled what is open).
        try:
            from backend.ticket_ingestion.config import FLEET_ENV, fleet_shared_now

            env[FLEET_ENV] = "1" if fleet_shared_now() else "0"
        except Exception:  # noqa: BLE001
            pass
        return env

    def _exit_record_path(self) -> Path:
        return self._repo_root / EXIT_RECORD

    def _exit_record(self) -> Optional[dict]:
        """What the last pipeline wrote on its way out (:data:`EXIT_RECORD`),
        or ``None``. Only trusted for OUR child when it names its pid."""
        try:
            raw = json.loads(self._exit_record_path().read_text())
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def start(self) -> bool:
        if self.is_running():
            return False
        # Under pytest, never spawn into a real checkout: the child only dies
        # on this same guard and leaves its EXIT_RECORD in the checkout (a
        # settings save in any test fires the background reconcile).
        try:
            home_guard.guard_ledger_dir(str(self._repo_root))
        except home_guard.RealHomeStoreError:
            return False
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # A fresh start owes the status a clean slate: the previous child's
        # exit reason must not outlive it.
        self._exited_at = None
        try:
            self._exit_record_path().unlink()
        except OSError:
            pass
        logf = open(self._log_path, "ab", buffering=0)
        try:
            logf.write(
                (
                    "\n==== MindFlock started %s ====\n"
                    % _datetime.datetime.now().astimezone().isoformat()
                ).encode()
            )
            self._proc = subprocess.Popen(
                # -P: the cwd (the repo root, maybe a checkout of other code) is
                # never put on sys.path; _env() puts the server's own package
                # root there instead.
                [self._python(), "-P", "-m", "backend.ticket_ingestion"],
                cwd=str(self._repo_root),
                env=self._env(),
                stdout=logf,
                stderr=logf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group for clean group-kill
            )
        finally:
            # The child holds its own dup of the fd — keeping the parent copy
            # open leaks one fd per start (and on Popen failure too).
            logf.close()
        self._started_at = time.time()
        return True

    def stop(self) -> bool:
        # Our own child first (we can wait on it).
        if self._own_running():
            proc = self._proc
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            self._proc = None
            return True
        self._proc = None
        # Otherwise an externally-started pipeline (e.g. an orphan that outlived a
        # previous backend.web) may hold the lock — stop it by the PID it recorded. It
        # ran in its own process group (start_new_session), so pgid == pid.
        pid = self._external_lock_pid()
        if pid and pid > 0:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except Exception:  # noqa: BLE001
                    try:
                        os.kill(pid, sig)
                    except Exception:  # noqa: BLE001
                        break
                if not self._external_lock_pid():
                    break
                time.sleep(1)
            return True
        return False

    def restart(self) -> bool:
        """Stop then start the pipeline so a startup-only setting takes effect.

        The PR monitor (and other config) is wired up once when the pipeline
        process boots, so flipping the automated-PR-review toggle on a live
        pipeline is otherwise ignored until a manual stop/start. Returns whether
        the pipeline is running afterward.
        """
        self.stop()
        # stop() waits for our own child to exit (releasing the singleton
        # flock); give any externally-owned pipeline a moment to release it too
        # so the start() below isn't blocked by a not-yet-cleared lock.
        for _ in range(15):
            if not self.is_running():
                break
            time.sleep(0.2)
        return self.start()

    def log_path(self) -> Path:
        return self._log_path

    def _recent_errors(self, window_seconds: int = ERROR_WINDOW) -> dict:
        """The most recent ``auth`` and ``network`` error lines in the log
        tail that are still inside ``window_seconds``, keyed by kind:
        ``{kind: {kind, message, at, source}}``. Untimestamped traceback
        lines inherit the nearest preceding timestamped line. An auth line
        that also reads like a connectivity one counts as auth."""
        try:
            with open(self._log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return {}
        latest: dict = {}
        current: Optional[_datetime.datetime] = None
        for line in tail.splitlines():
            m = _LOG_TS_RE.match(line)
            if m:
                try:
                    current = _datetime.datetime.fromisoformat(m.group(0))
                except ValueError:
                    pass
            if current is None:
                continue
            if _AUTH_ERROR_RE.search(line):
                kind = "auth"
            elif _CONN_ERROR_RE.search(line):
                kind = "network"
            else:
                continue
            prev = latest.get(kind)
            if prev is None or current >= prev["_at"]:
                latest[kind] = {
                    "kind": kind,
                    "message": _error_text(line),
                    "at": current.isoformat(),
                    "source": _error_source(line),
                    "_at": current,
                }
        out = {}
        for kind, entry in latest.items():
            at = entry.pop("_at")
            now = _datetime.datetime.now(at.tzinfo)
            if (now - at).total_seconds() <= window_seconds:
                out[kind] = entry
        return out

    def _recent_connection_error(self, window_seconds: int = ERROR_WINDOW) -> bool:
        """Whether the log tail shows a connectivity failure in the last few
        minutes (see :meth:`_recent_errors`)."""
        return "network" in self._recent_errors(window_seconds)

    def _exit_status(self) -> "tuple[Optional[str], Optional[dict]]":
        """``(exit_reason, last_error)`` for OUR child once it has exited —
        ``(None, None)`` while it runs or when we never started one.

        The reason is the pipeline's own word when it left one
        (:data:`EXIT_RECORD`: a ``config`` error with the missing fields, a
        ``crash`` with the exception), else read off the exit code: 0 is a
        deliberate no-op (another pipeline held the lock), a signal is a
        kill, anything else a crash named by the last error line logged."""
        proc = self._proc
        if proc is None or proc.poll() is None:
            return None, None
        if self._exited_at is None:
            self._exited_at = time.time()
        at = _datetime.datetime.fromtimestamp(self._exited_at).astimezone().isoformat()
        rc = proc.returncode
        rec = self._exit_record()
        if rec and rec.get("pid") == proc.pid and rec.get("kind"):
            kind = str(rec.get("kind"))
            fields = [str(f) for f in (rec.get("fields") or []) if f]
            message = str(rec.get("message") or "")
            if kind == "config" and fields:
                message = "missing or invalid: " + ", ".join(fields)
            err = {
                "kind": kind,
                "message": message,
                "at": str(rec.get("at") or at),
                "source": "",
            }
            if fields:
                err["fields"] = fields
            reason = "configuration error" if kind == "config" else "crashed"
            return reason, err
        if rc == 0:
            return "exited (another pipeline holds the lock)", None
        if rc is not None and rc < 0:
            reason = "killed by signal %d" % -rc
        else:
            reason = "exited with code %s" % rc
        err = _newest_error(self._recent_errors(window_seconds=10**9))
        if err is None:
            err = {"kind": "crash", "message": reason, "at": at, "source": ""}
        return reason, err

    def _activity(self) -> dict:
        """Parsed activity beacon written by the pipeline (its orchestrator
        mirrors in-flight ticket/PR counts to ``.mindflock-pipeline-activity.json``
        next to the singleton lock). ``{}`` when absent or unreadable."""
        try:
            raw = json.loads(
                (self._repo_root / ".mindflock-pipeline-activity.json").read_text()
            )
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def status(self) -> dict:
        own = self._own_running()
        ext = None if own else self._external_lock_pid()
        running = own or (ext is not None)
        if own and self._proc:
            pid = self._proc.pid
        elif ext is not None and ext > 0:
            pid = ext
        else:
            pid = None
        # Activity only counts while running AND when the beacon was written by
        # the pipeline that is running now (a dead run's file must not stick).
        act = self._activity() if running else {}
        if act and pid and act.get("pid") not in (None, pid):
            act = {}
        # Work this server is bringing in itself (forced starts + sessions still
        # provisioning) — independent of whether the pipeline runs at all.
        try:
            local = _pending.provisioning_kinds()
        except Exception:  # noqa: BLE001 — status must never fail on this
            local = set()
        meta = self._lock_holder()[1] if running else None
        # Why the child is gone, when it is ours and it is — plus the live
        # error classes: a skipped source (config, from the beacon), a tracker
        # or GitHub refusing the token (auth) or a connectivity failure
        # (network), the most actionable first.
        exit_reason, last_error = (None, None) if running else self._exit_status()
        config_errors = [
            e for e in (act.get("config_errors") or []) if isinstance(e, dict)
        ]
        recent = self._recent_errors() if running else {}
        if running:
            if config_errors:
                first = config_errors[0]
                last_error = {
                    "kind": "config",
                    "message": (
                        "%s: missing %s" % (first.get("source"), first.get("field"))
                        if first.get("field")
                        else "%s: %s" % (first.get("source"), first.get("message"))
                    ),
                    "at": str(act.get("updated") or ""),
                    "source": str(first.get("source") or ""),
                    "field": str(first.get("field") or ""),
                }
            else:
                last_error = _newest_error(recent)
        return {
            "running": running,
            "pid": pid,
            # The engine version the running pipeline was started on ("" =
            # not running, or too old to say) — "ingestion runs vX" when it
            # differs from the server's.
            "version": str((meta or {}).get("version") or ""),
            "since": (
                _datetime.datetime.fromtimestamp(self._started_at)
                .astimezone()
                .isoformat()
                if own and self._started_at
                else None
            ),
            "log": str(self._log_path),
            "available": _ingestion_repo_available(),
            # Only meaningful while running — a stopped pipeline can't have a
            # live connectivity problem, and stale errors shouldn't stick.
            "net_error": running and "network" in recent,
            # {kind: auth|config|network|crash, message, at, source[, field,
            # fields]} — the current problem, or null. While running: a
            # skipped source first, else the newest auth / network line in
            # the log (ERROR_WINDOW). After OUR child exited: why.
            "last_error": last_error,
            # Why our child is gone ("configuration error", "crashed",
            # "exited with code N", "killed by signal N", "exited (another
            # pipeline holds the lock)"); null while it runs / not ours.
            "exit_reason": exit_reason,
            # Every source the running pipeline skipped: [{source, field,
            # message}] (the beacon's list; [] when none or not running).
            "config_errors": config_errors,
            # MINDFLOCK_TICKET_PROVIDER pins the pipeline to one env-built
            # source and hides every source saved in the UI: {provider,
            # hides_ui_sources: true}, else null.
            "env_override": _env_override(),
            # The last state the TICKET toggle was set to (persisted across
            # restarts; the server auto-starts a desired-on pipeline on boot).
            # desired=on with running=false means it is starting up or died.
            "desired": _desired_running(),
            # Whether the PR-review half is switched on (github.enabled+repos).
            "pr_enabled": _pr_review_enabled(),
            # Whether issue handling is switched on (github.issues_enabled+repos).
            "issues_enabled": _issue_handling_enabled(),
            # Both of those also need THIS device to be the one that runs
            # them; where it isn't, Intake says "runs on <automation_device>"
            # instead of naming a switch that is on (and synced).
            **_automation_status(self._repo_root),
            # Live activity: True while a ticket / a PR batch / an issue is
            # actually being brought in (vs idle-waiting). The pipeline's beacon
            # only knows about the PIPELINE's queue, so a start forced from the
            # UI (the Intake dialog: Tickets / Pull requests / Issues) used to leave
            # the dot gold through its whole provisioning; `local` covers those.
            "tickets_active": bool(act.get("ticket_busy")) or "tix" in local,
            "pr_active": bool(act.get("pr_busy")) or "pr" in local,
            "issues_active": bool(act.get("issue_busy")) or "iss" in local,
            # {"live": n, "max": m} while the next ticket waits on the
            # concurrent-session cap (engine.max_sessions), else None.
            "held_for_slot": (
                act.get("held_for_slot")
                if isinstance(act.get("held_for_slot"), dict)
                else None
            ),
        }


class TicketIngestionAddon(Addon):
    # id stays "mindflock" so the API route prefix (/api/mindflock) and the existing
    # frontend bar wiring are unchanged (no functional change); the user-facing
    # label is "Ticket Ingestion" (the pipeline now ingests from any configured
    # ticketing provider, not just Shortcut).
    id = "mindflock"
    label = "Ticket Ingestion"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self.ctrl = TicketIngestionController()
        self._router = self._build_router()

    # --- ManagedProcess (delegated to the controller) -------------------- #
    def start(self) -> bool:
        return self.ctrl.start()

    def stop(self) -> bool:
        return self.ctrl.stop()

    def status(self) -> dict:
        return self.ctrl.status()

    def is_running(self) -> bool:
        return self.ctrl.is_running()

    # --- lifecycle: restore the toggle + react to the PR-review toggle ---- #
    async def on_startup(self, ctx: AppContext) -> None:
        """Restore the persisted toggle state, then watch the PR-review toggle.

        Reboot restore: ``/api/mindflock/start|stop`` records the desired
        state (``general.ingestion_autostart``); if the user left ingestion
        ON, start the pipeline with the server so a reboot lands in the same
        state they left it. Off-thread: ``start()`` blocks on subprocess I/O
        and startup must not wait on it. A pipeline someone already started
        (external lock) is left alone — ``start()`` no-ops when one runs.

        PR-review toggle: both switches gate halves of the SAME pipeline
        process — ticket polling (``general.ingestion_autostart``) and the PR
        monitor (``github.enabled``), each read once at pipeline boot. The
        Settings addon emits ``addon.settings.github_toggled`` on a real flip;
        we reconcile the process to the pair of toggles: bounce a pipeline WE
        own so the new setting takes effect, stop it when both halves are now
        off, and start one when PR review is switched on with nothing running.
        An operator's independently-started standalone run is never touched."""
        import threading

        threading.Thread(
            target=self._boot_reconcile,
            name="mindflock-ingestion-autostart",
            daemon=True,
        ).start()

        def _on_toggle(envelope: dict) -> None:
            import threading

            data = envelope.get("data") if isinstance(envelope, dict) else None
            changed = (data or {}).get("changed") if isinstance(data, dict) else None
            # stop()/start() block on subprocess I/O; the bus callback runs on
            # the emitting (request) thread, so offload to a short-lived thread.
            threading.Thread(
                target=self._reconcile_process,
                args=(list(changed) if isinstance(changed, list) else [],),
                name="mindflock-pr-toggle-reconcile",
                daemon=True,
            ).start()

        self._unsub_toggle = ctx.subscribe("addon.settings.github_toggled", _on_toggle)

        # Joining or leaving a group of devices, or the roster changing by
        # gossip, can flip whether THIS device runs the GitHub halves (the
        # group's one runner: github.automation_device, else the lowest-keyed
        # live member) without any settings save — reconcile when that
        # answer moved: on a device event, and every AUTOMATION_RECHECK s.
        self._automation = _observe_automation(self.ctrl._repo_root)

        def _on_devices(_envelope: dict) -> None:
            now = _observe_automation(self.ctrl._repo_root)
            if now == self._automation:
                return
            self._automation = now
            _on_toggle(_envelope)

        self._unsub_devices = [
            ctx.subscribe(name, _on_devices)
            for name in ("device.joined", "device.removed")
        ]

        async def _recheck() -> None:
            while True:
                await asyncio.sleep(AUTOMATION_RECHECK)
                try:
                    await asyncio.to_thread(_on_devices, {})
                except Exception as err:  # noqa: BLE001 — the loop must not die
                    if log.ErrorLog is not None:
                        log.ErrorLog.Printf("automation recheck failed: %v", err)

        self._recheck_task = asyncio.get_running_loop().create_task(_recheck())

    def _boot_reconcile(self) -> None:
        """At server start: a pipeline still running on an older engine build
        (this server just re-execed onto an update) is restarted onto the new
        one; otherwise the persisted toggle is restored (``start()`` no-ops
        when a pipeline already runs).

        Only while the pipeline is wanted (:meth:`_process_wanted`): a restart
        is a stop AND a start, and with every toggle off that start would turn
        on what the user left off. That is also what keeps an owner-unknown
        pipeline (a lock with no metadata — perhaps a standalone run) alone
        unless the toggle is on."""
        wanted = self._process_wanted()
        try:
            if wanted and _may_restart_found_pipeline() and self.ctrl.stale_build():
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf(
                        "ingestion: the running pipeline is an older build — restarting it"
                    )
                self.ctrl.restart()
                return
        except Exception as err:  # noqa: BLE001 — never block the autostart
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("ingestion stale-build check failed: %v", err)
        if wanted:
            self.ctrl.start()

    # --- toggle → process reconciliation ----------------------------------- #
    @staticmethod
    def _process_wanted() -> bool:
        """Whether the pipeline process should be running: ANY half is on
        and can actually run. Tickets need a repo to provision into; PR
        review and issue handling boot the pipeline on their own — the
        layered config loads with zero ticketing sources."""
        tickets_on = _desired_running() and _ingestion_repo_available()
        return tickets_on or _pr_review_enabled() or _issue_handling_enabled()

    def _reconcile_process(self, changed: Optional[list] = None) -> None:
        """Bring the pipeline process in line with the settings it reads at
        boot (``changed``: the restart-signature keys that moved, for the
        log — names only, never values).

        Own-running + still wanted → restart (the halves and every source
        are wired at boot, so a change only takes effect on a fresh
        process). Own-running + nothing wanted → stop. Not running + wanted
        → start. An externally-owned pipeline is left alone, matching the
        old toggle behaviour."""
        if self.ctrl._own_running():
            if self._process_wanted():
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf(
                        "ingestion: settings changed (%s) — restarting the pipeline",
                        ", ".join(str(c) for c in (changed or [])) or "switches",
                    )
                self.ctrl.restart()
            else:
                self.ctrl.stop()
        elif self._process_wanted() and not self.ctrl.is_running():
            self.ctrl.start()

    # --- routes ----------------------------------------------------------- #
    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/mindflock")
        ctrl = self.ctrl

        @router.get("/status")
        def mindflock_status() -> JSONResponse:
            return JSONResponse(ctrl.status())

        @router.post("/start")
        async def mindflock_start() -> JSONResponse:
            """Switch the TICKET half on. A pipeline we own that is already
            running (e.g. PR-review-only) is bounced so it picks the ticket
            loops up; otherwise one is started."""
            if not _ingestion_repo_available():
                return JSONResponse(
                    {
                        "error": "No repo to ingest into — add a Repo URL to a "
                        "ticketing source (Intake → Tickets)"
                    },
                    status_code=400,
                )
            was_on = _desired_running()
            # Record BEFORE acting: the (re)started pipeline reads the toggle
            # at boot, so it must already say "on".
            _record_desired_running(True)
            try:
                if ctrl._own_running():
                    # Already on and running -> idempotent no-op (don't bounce
                    # a healthy pipeline for a double-click).
                    started = (
                        await asyncio.to_thread(ctrl.restart) if not was_on else False
                    )
                else:
                    started = await asyncio.to_thread(ctrl.start)
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=500)
            return JSONResponse({"started": started, **ctrl.status()})

        @router.post("/stop")
        async def mindflock_stop() -> JSONResponse:
            """Switch the TICKET half off. When PR review or issue handling is
            still on, the process keeps running — it is bounced into a
            tickets-off mode instead of being stopped."""
            _record_desired_running(False)
            try:
                if ctrl._own_running() and (
                    _pr_review_enabled() or _issue_handling_enabled()
                ):
                    stopped = await asyncio.to_thread(ctrl.restart)
                else:
                    stopped = await asyncio.to_thread(ctrl.stop)
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=500)
            return JSONResponse({"stopped": stopped, **ctrl.status()})

        @router.websocket("/logs")
        async def mindflock_logs_ws(ws: WebSocket) -> None:
            await ws.accept()
            log_path = ctrl.log_path()
            # Ensure the file exists so `tail -F` streams immediately (and
            # survives the log being recreated when MindFlock restarts).
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.touch(exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                proc = spawn_tail(log_path, lines=500)
            except Exception as err:  # noqa: BLE001
                await ws.send_text(json.dumps({"type": "error", "message": str(err)}))
                await ws.close(code=4500)
                return
            await pump_pty(ws, proc, allow_input=False)

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # --- lifecycle -------------------------------------------------------- #
    async def on_shutdown(self, ctx: AppContext) -> None:
        task = getattr(self, "_recheck_task", None)
        if task is not None:
            task.cancel()
        # Stop only the child WE own on shutdown — never an operator's
        # independently-started standalone pipeline (matches stop()'s own/external
        # split). The old code leaked the child on server exit.
        if self.ctrl._own_running():
            try:
                await asyncio.to_thread(self.ctrl.stop)
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("mindflock shutdown stop failed: %v", err)

    # --- frontend --------------------------------------------------------- #
    def frontend(self):
        return [
            FrontendDescriptor(
                id="mindflock",
                label="Ticket Ingestion",
                where="sidebar-bar",
                module=None,  # hand-wired in app.js/index.html; no ES module
                api_base="/api/mindflock",
                ws_path="/api/mindflock/logs",
                poll_ms=4000,
                available_flag="available",
                order=10,
                builtin_ui=True,  # keeps its bespoke sidebar bar in app.js
            )
        ]
