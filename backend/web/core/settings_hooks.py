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

import hashlib
import json
import os
import shutil
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from backend import log

#: Seconds a burst of ticketing/GitHub changes — synced in, or a run of
#: blur-saves from the Intake dialog — is collapsed into one pipeline
#: reconcile: a pass that adopts five fields, or a form that saves on every
#: field, must not restart the ticket pipeline five times.
PIPELINE_DEBOUNCE = 2.0

#: The event the ticket-ingestion addon reconciles its child process on
#: (start / stop / restart). Its ``data.changed`` lists the signature keys
#: (:func:`pipeline_signature`) that moved since the last reconcile — names
#: only, never values — so the addon can log why it restarted.
PIPELINE_EVENT = "addon.settings.github_toggled"

_TIMER_LOCK = threading.Lock()
_pipeline_timer: Optional[threading.Timer] = None
#: Signature keys changed since the pending reconcile was scheduled.
_pending_changes: set = set()
#: The pipeline signature last seen by :func:`after_settings_change` (the
#: baseline when a caller doesn't pass its own "before").
_last_sig: Optional[tuple] = None

#: Ticket-source fields that are secrets: they enter the signature as a
#: digest, so a rotated token still restarts the pipeline without the
#: signature (which is logged nowhere, but lives in memory) holding plaintext.
_SOURCE_SECRET_FIELDS = ("api_token",)


# --------------------------------------------------------------------------- #
# Does this device run PR review / issue handling?
# --------------------------------------------------------------------------- #
def _live_members() -> Dict[str, dict]:
    """This device's group of devices (live members, itself included), or
    ``{}`` when it isn't in one. Never raises."""
    try:
        from backend.web.core import fleet as _fleet

        if not _fleet.in_fleet():
            return {}
        live = _fleet.live_members()
        return dict(live) if isinstance(live, dict) else {}
    except Exception:  # noqa: BLE001 — no fleet store = a lone device
        return {}


def _self_key() -> str:
    try:
        from backend.web.core import remote as _remote

        return str(_remote.self_identity().get("key") or "")
    except Exception:  # noqa: BLE001
        return ""


def automation_runner(live: Optional[Dict[str, dict]] = None) -> str:
    """The key of the device that runs PR review and issue handling for the
    group (``live``: its live members; read when not given): the synced
    ``github.automation_device`` while it names a live member, else the live
    member with the lowest key — every device computes the same answer from
    the same roster, so exactly one runs them. ``""`` outside a group of two
    or more. Never raises."""
    if live is None:
        live = _live_members()
    if len(live) < 2:
        return ""
    try:
        from backend.config import settings as _settings

        chosen = (_settings.load_settings().github.automation_device or "").strip()
    except Exception:  # noqa: BLE001
        chosen = ""
    return chosen if chosen in live else min(live)


def automation_here() -> bool:
    """Whether PR review and issue handling run on THIS device.

    A lone device (no group, or a group of one) always runs them — what every
    install did before devices could be grouped. In a group of two or more,
    only :func:`automation_runner` does: their settings (``github.*``) follow
    the person to every device, so each would otherwise review the same PRs.
    Never raises."""
    live = _live_members()
    if len(live) < 2:
        return True
    me = _self_key()
    return bool(me) and automation_runner(live) == me


def automation_device() -> str:
    """The name of the OTHER device that runs PR review and issue handling
    (:func:`automation_runner`, by its host), or ``""`` when it is this one
    or there's no group — what Intake names ("runs on <device>") where this
    one doesn't. Never raises."""
    live = _live_members()
    key = automation_runner(live)
    if not key or key == _self_key():
        return ""
    host = str((live.get(key) or {}).get("host") or "")
    if not host:
        try:
            from backend.web.core import remote as _remote

            for dev in _remote.fleet_devices():
                if isinstance(dev, dict) and dev.get("key") == key:
                    host = str(dev.get("host") or "")
                    break
        except Exception:  # noqa: BLE001
            pass
    return host or key


def _digest(value: object) -> str:
    """A stable short hash of any JSON-able value."""
    raw = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _source_fingerprint(src) -> str:
    """One ticket source reduced to a digest of EVERY field it carries —
    identity, queue, scope, labels, base URL, email, repo, agent, effort,
    depth, start state… — with each secret replaced by its own digest first,
    so the plaintext token never sits in the signature. The whole normalized
    dict is hashed rather than a hand-picked tuple: a field added to
    :class:`~backend.config.settings.TicketingSource` later is covered
    without anyone remembering to list it here."""
    try:
        d = dict(src.to_dict())
    except Exception:  # noqa: BLE001 — a hand-built object: its public fields
        d = {k: v for k, v in vars(src).items() if not k.startswith("_")}
    for key in _SOURCE_SECRET_FIELDS:
        if d.get(key):
            d[key] = "sha256:" + hashlib.sha256(str(d[key]).encode("utf-8")).hexdigest()
    return _digest(d)


def pipeline_signature() -> Tuple:
    """What the ticket pipeline reads when it boots, as sorted ``(key,
    digest)`` pairs — one per thing whose change needs a restart:

    * ``ticketing.<id>`` — every field of that ticket source (secrets as a
      hash; see :func:`_source_fingerprint`), so a new or rotated token, a
      changed assignee scope, labels, base URL, email, queue or repo all count;
    * ``github`` — the PR-review and issue-handling switches, their repo
      lists, and whether/which token is set (as a hash);
    * ``automation`` — whether this device runs the GitHub halves.

    A change that leaves every digest alone (a GitHub grace period, a skip
    author — both read per poll, or a UI accent) doesn't restart anything.
    Compare two signatures with :func:`signature_changes`. Never raises."""
    try:
        from backend.config import settings as _settings

        _settings.invalidate()
        s = _settings.load_settings()
        gh = s.github
        pairs: List[Tuple[str, str]] = []
        for idx, src in enumerate(s.ticketing.sources):
            key = "ticketing.%s" % (str(getattr(src, "id", "") or "") or idx)
            pairs.append((key, _source_fingerprint(src)))
        token = str(gh.token or "")
        pairs.append(
            (
                "github",
                _digest(
                    {
                        "enabled": gh.enabled is not False,
                        "issues_enabled": gh.issues_enabled is True,
                        "repos": list(gh.repo_list()),
                        "issue_repos": list(gh.issue_repo_list()),
                        "token": (
                            hashlib.sha256(token.encode("utf-8")).hexdigest()
                            if token
                            else ""
                        ),
                    }
                ),
            )
        )
        pairs.append(("automation", _digest(automation_here())))
        return tuple(sorted(pairs))
    except Exception:  # noqa: BLE001
        return ()


def signature_changes(before: Optional[tuple], after: Optional[tuple]) -> List[str]:
    """The signature keys that differ between two :func:`pipeline_signature`
    results (added, removed or re-digested), sorted. ``None`` on either side
    means "unknown": every key of the other side counts as changed."""
    a = dict(before or ())
    b = dict(after or ())
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


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
        changed = sorted(_pending_changes)
        _pending_changes.clear()
    _emit(PIPELINE_EVENT, {"changed": changed})


def _schedule_pipeline(changed: Iterable[str] = ()) -> None:
    """One reconcile :data:`PIPELINE_DEBOUNCE` s after the LAST change of a
    burst (each call pushes the timer back); ``changed`` — the signature keys
    that moved — accumulates across the burst and rides on the event."""
    global _pipeline_timer
    with _TIMER_LOCK:
        _pending_changes.update(str(c) for c in changed if c)
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
    """Run the side effects of ``paths`` having changed (sync unit ids), then
    announce ``settings.synced`` so open UIs refetch. Runs for every write of
    a setting the pipeline reads — synced in from another device, or saved
    locally by ``POST /api/settings`` / ``PUT /api/settings/ticketing/sources``
    (``source`` is the device the values came from, ``""`` for a local write).
    ``pipeline_before`` is the :func:`pipeline_signature` from before the
    write: the pipeline is reconciled (restarted, if it is wanted) only when
    the signature moved, :data:`PIPELINE_DEBOUNCE` s after the last change of
    a burst. Never raises."""
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
                _schedule_pipeline(signature_changes(before, sig))
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
