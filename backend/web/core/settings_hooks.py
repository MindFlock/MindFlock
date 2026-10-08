"""What has to happen after settings change underneath the running server.

A Settings save in the UI goes through ``POST /api/settings``, which does its
own follow-ups inline (drop the cached GitHub token, nudge the ticket
pipeline…). A value that arrives by settings sync (:mod:`settings_sync`) is
written straight to the store, so none of that ran: a synced GitHub token kept
the old cached one, a synced ticket source never reached the pipeline, a
synced scroll speed changed a field nothing reads. :func:`after_settings_change`
is the one place those effects live, keyed on the changed paths (sync unit
ids: ``group.field``, ``group.field#key``, ``store:<name>#key``).

Also home to :func:`provider_installed` — "is this agent CLI on this machine"
— which both the Settings save guard and sync's deferral ask — and to
:func:`automation_here` — "does THIS device run PR review and issue handling":
their settings follow the person, so without a per-device answer every one of
their devices would review the same PRs.
"""

from __future__ import annotations

import os
import shutil
import threading
from typing import Iterable, Optional, Tuple

from backend import log

#: Seconds a burst of synced ticketing/GitHub changes is collapsed into one
#: pipeline reconcile — a pass that adopts five fields must not restart the
#: ticket pipeline five times.
PIPELINE_DEBOUNCE = 5.0

#: The event the ticket-ingestion addon reconciles its child process on
#: (start / stop / restart) — what a Settings save's GitHub toggle emits.
PIPELINE_EVENT = "addon.settings.github_toggled"

_TIMER_LOCK = threading.Lock()
_pipeline_timer: Optional[threading.Timer] = None
#: The pipeline signature last seen by :func:`after_settings_change` (the
#: baseline when a caller doesn't pass its own "before").
_last_sig: Optional[tuple] = None


# --------------------------------------------------------------------------- #
# Does this device run PR review / issue handling?
# --------------------------------------------------------------------------- #
def automation_here() -> bool:
    """Whether PR review and issue handling run on THIS device.

    ``github.run_here`` (device-local) decides when set. Unset: yes on a lone
    device (what every install did before devices could be grouped), no once
    this device is one of two or more of your devices — the GitHub settings
    sync, so each of them would otherwise review the same PRs. Never raises."""
    try:
        from backend.config import settings as _settings

        run_here = _settings.load_settings().github.run_here
    except Exception:  # noqa: BLE001
        run_here = None
    if run_here is not None:
        return bool(run_here)
    try:
        from backend.web.core import fleet as _fleet

        return not (_fleet.in_fleet() and len(_fleet.live_members()) >= 2)
    except Exception:  # noqa: BLE001 — no fleet store = a lone device
        return True


def pipeline_signature() -> Tuple:
    """What the ticket pipeline reads when it boots, reduced to what would
    change its behaviour: the PR/issue switches, their repos, whether there's
    a token, each ticket source's identity/queue/repo/agent, and whether this
    device runs the GitHub halves. A synced change that leaves this alone
    (a label, a grace period…) doesn't restart anything. Never raises."""
    try:
        from backend.config import settings as _settings

        _settings.invalidate()
        s = _settings.load_settings()
        gh = s.github
        sources = tuple(
            sorted(
                (
                    src.id,
                    src.provider,
                    src.project,
                    src.workflow_state,
                    str(src.workflow_state_id),
                    src.repo_url,
                    src.agent,
                )
                for src in s.ticketing.sources
            )
        )
        return (
            gh.enabled is not False,
            gh.issues_enabled is True,
            tuple(gh.repo_list()),
            tuple(gh.issue_repo_list()),
            bool(gh.token),
            sources,
            automation_here(),
        )
    except Exception:  # noqa: BLE001
        return ()


# --------------------------------------------------------------------------- #
# Provider installed?
# --------------------------------------------------------------------------- #
def installed_path(binary: str) -> str:
    """Resolve a CLI ``binary`` to the executable path in effect, or ``""``.

    An explicit path override (contains ``os.sep``) is used directly when it is
    an executable file; otherwise the name is looked up on ``$PATH``. An empty
    ``binary`` (or an unresolved one) yields ``""`` — i.e. "not installed"."""
    if not binary:
        return ""
    if os.sep in binary:  # explicit path override — check the file directly
        return binary if (os.path.isfile(binary) and os.access(binary, os.X_OK)) else ""
    return shutil.which(binary) or ""


def provider_installed(name: str) -> bool:
    """Whether provider ``name``'s CLI binary is present on this machine.

    ``False`` for a name the registry doesn't know: ``providers.resolve``
    falls back to claude for an unknown name, so asking it blindly about a
    custom provider that only exists on another device would answer "is
    claude installed" — and sync would then adopt a default agent this
    machine can't launch. Never raises."""
    from backend import providers
    from backend.providers import config as provider_config

    try:
        key = (name or "").strip()
        if not key or key == "generic" or providers.get(key) is None:
            return False
        p = providers.resolve(key)
        cfg = getattr(p, "cfg", None)
        binary = provider_config.resolve_provider_binary(getattr(p, "name", key), cfg)
        return bool(installed_path(binary))
    except Exception:  # noqa: BLE001 — a probe failure is "not installed", not a crash
        return False


# --------------------------------------------------------------------------- #
# After a change
# --------------------------------------------------------------------------- #
def _emit(event: str, data: Optional[dict] = None) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit(event, data=data)
    except Exception as err:  # noqa: BLE001 — an event is best-effort
        _log_error("settings hooks: emitting %s failed: %v", event, err)


def _log_error(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        log.ErrorLog.Printf(fmt, *args)


def _fire_pipeline() -> None:
    global _pipeline_timer
    with _TIMER_LOCK:
        _pipeline_timer = None
    _emit(PIPELINE_EVENT)


def _schedule_pipeline() -> None:
    """One reconcile :data:`PIPELINE_DEBOUNCE` s after the LAST change of a
    burst (each call pushes the timer back)."""
    global _pipeline_timer
    with _TIMER_LOCK:
        if _pipeline_timer is not None:
            _pipeline_timer.cancel()
        t = threading.Timer(PIPELINE_DEBOUNCE, _fire_pipeline)
        t.daemon = True
        _pipeline_timer = t
        t.start()


def _apply_scroll_speed() -> None:
    """Make a synced ``ui.scroll_speed`` live: the terminals read the
    scroll-speed FILE (and tmux's bindings), not settings.json — the same two
    writes ``POST /api/scroll-speed`` does."""
    from backend.config import settings as _settings
    from backend.web.core import terminal

    _settings.invalidate()
    speed = _settings.load_settings().ui.scroll_speed
    written = terminal.save_scroll_speed(
        speed if speed is not None else terminal._SCROLL_SPEED_DEFAULT
    )
    terminal.apply_scroll_speed(written)


def after_settings_change(
    paths: Iterable[str],
    source: str = "",
    *,
    pipeline_before: Optional[tuple] = None,
) -> None:
    """Run the side effects of ``paths`` having changed underneath the server
    (sync unit ids), then announce ``settings.synced`` so open UIs refetch.
    ``source`` is the device the values came from; ``pipeline_before`` the
    :func:`pipeline_signature` from before the write (the pipeline is only
    reconciled when it moved). Never raises."""
    global _last_sig
    try:
        changed = sorted({str(p) for p in (paths or []) if p})
    except Exception:  # noqa: BLE001
        changed = []
    if not changed:
        return
    if "github.token" in changed:
        try:
            from backend.ticket_ingestion import github_auth

            github_auth.invalidate()
        except Exception as err:  # noqa: BLE001
            _log_error("settings hooks: github token refresh failed: %v", err)
    if any(p.startswith(("ticketing.", "github.")) for p in changed):
        try:
            sig = pipeline_signature()
            before = pipeline_before if pipeline_before is not None else _last_sig
            _last_sig = sig
            if before is None or sig != before:
                _schedule_pipeline()
        except Exception as err:  # noqa: BLE001
            _log_error("settings hooks: pipeline nudge failed: %v", err)
    if "ui.scroll_speed" in changed:
        try:
            _apply_scroll_speed()
        except Exception as err:  # noqa: BLE001
            _log_error("settings hooks: scroll speed failed: %v", err)
    if any(p.startswith("store:providers") for p in changed):
        try:
            from backend import providers

            providers.rebuild_registry()
        except Exception as err:  # noqa: BLE001
            _log_error("settings hooks: provider registry rebuild failed: %v", err)
    _emit("settings.synced", {"paths": changed, "from": source or ""})
