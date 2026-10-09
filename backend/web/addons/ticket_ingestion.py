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
from backend.session import provisioned as provisioning

from backend.web.core import pending as _pending
from backend.web.core.terminal import pump_pty, spawn_tail

from .base import Addon, AppContext, FrontendDescriptor


def _resolve_repo_root() -> Path:
    """Directory the pipeline must run in — where ``config.toml`` + ``state.json``
    live. State is stored CWD-relative (``_STATE_DIR = Path(".")`` in the pipeline),
    so getting this wrong splits the processed-story ledger and re-ingests already
    done tickets (windows re-open, promptless).

    The old ``Path(__file__).resolve().parents[4]`` only landed on the repo root
    for a src-layout dev checkout (``<repo>/backend/web/addons/…``). For an
    installed copy (``uv tool install`` → ``…/lib/pythonX/site-packages/backend/…``)
    ``parents[4]`` is the interpreter lib dir, NOT the repo — so the pipeline ran
    with a stray, empty ledger there. Resolve robustly instead:

      1. ``MINDFLOCK_REPO_ROOT`` env override (explicit wins).
      2. walk up from this file for a dir containing ``config.toml`` (dev /
         editable installs, where the package lives under the repo).
      3. the process cwd if it holds ``config.toml`` (installed copy launched from
         the repo root, as ``mindflock serve`` is).
      4. fall back to the process cwd.
    """
    env = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.toml").is_file():
            return parent

    cwd = Path.cwd()
    if (cwd / "config.toml").is_file():
        return cwd
    return cwd


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


def _ticketing_configured() -> bool:
    """Whether the pipeline process can boot at all: its config loader requires
    a ``[ticketing]`` source even when the ticket half is switched off, so a
    PR-only run still needs one configured."""
    try:
        from backend.config import settings as _s

        return any(s.provider for s in _s.load_settings().ticketing.sources)
    except Exception:  # noqa: BLE001
        return False


# Leading ISO timestamp on pipeline log lines ("2026-07-22T10:24:30 [ERROR] ...").
_LOG_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"
)

# Connectivity failures as they appear in the ingestion log (aiohttp/OS errors:
# DNS, refused/reset, unreachable, timeouts). Deliberately excludes auth/SSL
# certificate errors — those are misconfiguration, not a network outage.
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
        """Python that can import ``backend.ticket_ingestion``.

        The web server often runs from the mindflock venv (which has the CS
        engine + backend.web but NOT the MindFlock package), so ``sys.executable`` can't
        run the pipeline. Prefer the MindFlock repo's own ``.venv`` when present.
        """
        cand = self._repo_root / ".venv" / "bin" / "python"
        if cand.exists():
            return str(cand)
        return sys.executable

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
        extra = [str(self._repo_root / "src")]
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

    def start(self) -> bool:
        if self.is_running():
            return False
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(self._log_path, "ab", buffering=0)
        try:
            logf.write(
                (
                    "\n==== MindFlock started %s ====\n"
                    % _datetime.datetime.now().astimezone().isoformat()
                ).encode()
            )
            self._proc = subprocess.Popen(
                [self._python(), "-m", "backend.ticket_ingestion"],
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

    def _recent_connection_error(self, window_seconds: int = 180) -> bool:
        """Whether the log tail shows a connectivity failure in the last few
        minutes. The pipeline retries every poll interval (~20s), so an ongoing
        outage keeps re-logging and holds this true; after recovery the last
        error ages out of the window and it clears on its own. Untimestamped
        traceback lines inherit the nearest preceding timestamped line."""
        try:
            with open(self._log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return False
        latest: Optional[_datetime.datetime] = None
        current: Optional[_datetime.datetime] = None
        for line in tail.splitlines():
            m = _LOG_TS_RE.match(line)
            if m:
                try:
                    current = _datetime.datetime.fromisoformat(m.group(0))
                except ValueError:
                    pass
            if _CONN_ERROR_RE.search(line) and current is not None:
                if latest is None or current > latest:
                    latest = current
        if latest is None:
            return False
        now = _datetime.datetime.now(latest.tzinfo)
        return (now - latest).total_seconds() <= window_seconds

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
            "net_error": running and self._recent_connection_error(),
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

        def _on_toggle(_envelope: dict) -> None:
            import threading

            # stop()/start() block on subprocess I/O; the bus callback runs on
            # the emitting (request) thread, so offload to a short-lived thread.
            threading.Thread(
                target=self._reconcile_process,
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
        (and can actually run — tickets need a repo to provision into, a
        PR-only / issue-only run still needs a ticketing source for the
        config to load)."""
        tickets_on = _desired_running() and _ingestion_repo_available()
        pr_on = _pr_review_enabled() and _ticketing_configured()
        issues_on = _issue_handling_enabled() and _ticketing_configured()
        return tickets_on or pr_on or issues_on

    def _reconcile_process(self) -> None:
        """Bring the pipeline process in line with the two toggles.

        Own-running + still wanted → restart (the halves are wired at boot, so
        a flip only takes effect on a fresh process). Own-running + nothing
        wanted → stop. Not running + wanted → start. An externally-owned
        pipeline is left alone, matching the old toggle behaviour."""
        if self.ctrl._own_running():
            if self._process_wanted():
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
