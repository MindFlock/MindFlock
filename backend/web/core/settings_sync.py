"""Share settings across the user's own devices — two-way, last edit wins.

Every MindFlock server keeps its own ``settings.json``. With sync on, the
devices in this device's FLEET (:mod:`backend.web.core.fleet` — "Your
devices": every member holds the fleet key) keep the SHAREABLE part of it
identical: ticket sources, GitHub repos and options, notifications, agent
limits, the accent colour, UI preferences, session templates, red zones,
custom agent providers… Machine-specific fields (paths, bind mode, the access
token, the IDE, the local model, peer links, signed-in accounts) never leave
the machine — :data:`SYNCED` vs :data:`LOCAL`, and a test fails until every
new field is put in one of them.

**Only fleet members.** Peers come from :func:`remote.fleet_devices` and
nothing else, and every request carries the fleet key. A tailnet node merely
"connected" (a gate-off node counts with no token at all) is never pulled —
adopting its export would let it push ``coding_cli.default_launch_args``,
which prefixes every new session here.

**Units.** Everything synced is a *unit* with its own stamp, so two edits to
different parts of one list both survive:

- ``group.field`` — a plain field;
- ``group.field#<key>`` — one entry of a keyed list (:data:`KEYED`: a ticket
  source by id, a prompt preset by lower-cased name);
- ``store:<name>#<key>`` — one entry of a registered store outside
  settings.json (:func:`register_store`; :mod:`sync_stores` adds templates,
  red zones and custom providers).

**Last edit wins, per unit.** A stamp is ``{ts, by, h}`` (when it last
changed, on which device, a hash of its canonical value) in
``settings_sync.json`` beside ``settings.json``; a deleted entry keeps a
tombstone stamp (``deleted: true``) so a stale copy elsewhere can't bring it
back. A change is noticed by the hash — a Settings save calls
:func:`scan_local` at once (and nudges the other devices to pull now), the
loop rescans every :data:`INTERVAL` s. Stamps are hybrid-logical: a change
gets ``max(now, previous stamp + 1 ms)``, so a value adopted from a device
whose clock runs fast can still be overwritten by a later edit here.

**Canonical, then local.** A value that names a local checkout (``repository
.url``, a ticket source's ``repo_url``, a template's ``repo_path``) travels as
the checkout's origin URL and is turned back into THIS machine's checkout of
the same repo on adoption (:func:`canonical` / :func:`localize`).

**Deferral.** A field naming an agent CLI that isn't installed here is not
adopted (and its stamp not taken) until it is — :data:`DEFER_PATHS`.

**Pinning.** "Keep different on this device": a pinned base is neither
exported, adopted nor stamped (:func:`set_pinned`).

**Joining.** :func:`enable` either starts from THIS device (its values are
stamped now and spread from it — or, ``seed=True``, stamped older than any
real edit, so they only spread where nobody has a value) or from another
fleet device: that device's shareable values are adopted first, under its
stamps (or, if it isn't syncing yet, stamps older than any real edit, so its
first sync wins over nothing). A plain field the device joined doesn't have
set never clears one set here: this device's value is kept and spreads.

**Never from a broken file.** A ``settings.json`` that exists but doesn't
parse pauses sync (nothing scanned, exported or adopted — read as empty it
would delete every source and clear every token on every device), and a
checkout whose origin git can't tell right now is left out of the pass rather
than shipped as this machine's path (the last origin seen is remembered in
``settings_sync.json``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from backend import log

#: Seconds between sync passes (rescan local, pull every device).
INTERVAL = 30.0

#: Export format. Bumped from 1 (whole-field stamps, any connected device)
#: when sync moved to units + fleets; a payload of another protocol is ignored.
PROTOCOL = 2

#: Where a device's export is served (addons/settings.py).
EXPORT_PATH = "/api/settings/sync/export"
#: Where a device is told "something changed — pull now".
NUDGE_PATH = "/api/settings/sync/nudge"

#: Seconds a nudge waits before pulling, so a burst of saves is one pass.
NUDGE_DELAY = 1.0

#: Shareable fields per group — the same everywhere once sync is on.
SYNCED: Dict[str, Tuple[str, ...]] = {
    "coding_cli": ("default_provider", "assistant_provider", "default_launch_args"),
    "ticketing": ("sources",),
    "repository": (
        "url",
        "base_branch",
        "pr_base_branch",
        "live_branch",
        "git_transport",
        "fasttrack_depth",
        "precommit_retry_hooks",
        "verify_repos",
        "verify_enabled",
        "verify_use_conversation",
        "deploy_delay_minutes",
        "verify_repo_settings",
    ),
    "github": (
        "token",
        "base_branch",
        "enabled",
        "issues_enabled",
        "min_age_minutes",
        "poll_interval_seconds",
        "skip_authors",
        "repos",
        "issue_repos",
        "issue_min_age_minutes",
        "issue_poll_interval_seconds",
        "issue_skip_authors",
        "agent",
        "issue_agent",
        "repo_settings",
        "issue_repo_settings",
    ),
    "engine": ("skip_permissions", "agent"),
    "ui": ("scroll_speed", "accent"),
    "general": (
        "session_budget_usd",
        "window_budget_usd",
        "tailnet_trusted_logins",
        "resume_on_usage_reset",
        "agent_mcp",
        "agent_mcp_scope",
        "agent_max_children",
        "agent_max_spawn_depth",
        "agent_max_spawned",
    ),
    "notifications": (
        "muted_rules",
        "enabled_rules",
        "ntfy_enabled",
        "ntfy_server",
        "ntfy_topic",
        "ntfy_token",
    ),
    "extensions": ("disabled",),
    "prefs": (
        "keymap",
        "prompt_presets",
        "theme",
        "diff_mode",
        "diff_base",
        "hidden_bars",
        "bar_order",
        "reduce_motion",
        "break_on",
        "break_every",
        "idle_flock",
        "idle_after",
        "hints",
    ),
}

#: Machine-specific fields — never synced. Listed (not just "everything not
#: in SYNCED") so a new field has to be classified on purpose.
LOCAL: Dict[str, Tuple[str, ...]] = {
    "coding_cli": ("binary_paths",),
    "auth_profiles": ("profiles", "default_profile"),
    "repository": ("workspace_dir",),
    "engine": ("enabled", "mode", "open_cursor", "max_sessions"),
    "local_model": ("enabled", "runtime", "base_url", "model"),
    "ui": ("cursor_autoadopt", "surface"),
    "platform": ("wsl_distro", "wt_command", "ide_command"),
    "general": (
        "auth_token",
        "auth_mode",
        "onboarded",
        "last_repo_path",
        "remote_control",
        "serve_mode",
        "shared_link",
        "ingestion_autostart",
    ),
    "notifications": ("ntfy_click_url",),
    "github": ("run_here",),
    "peer": (
        "enabled",
        "listen_host",
        "listen_port",
        "display_name",
        "advertise_host",
        "egress_allow",
        "relay",
        "relay_url",
        "relay_port",
    ),
}

#: Synced list fields that sync PER ENTRY: base path -> the entry's key field.
KEYED: Dict[str, str] = {"ticketing.sources": "id", "prefs.prompt_presets": "name"}
#: Keyed bases whose keys compare case-insensitively (a preset is its name).
_CASELESS_KEYS = frozenset({"prefs.prompt_presets"})

#: Shared fields that carry a credential (ticket sources hold API tokens).
#: Documentation now: every caller of the export holds the fleet key or this
#: device's own token, so secrets always travel.
SECRET_PATHS = frozenset(
    {"github.token", "notifications.ntfy_token", "ticketing.sources"}
)

#: Fields naming an agent CLI: never adopted while that CLI isn't installed
#: here (the default agent must always be launchable).
DEFER_PATHS = frozenset(
    {
        "coding_cli.default_provider",
        "coding_cli.assistant_provider",
        "engine.agent",
        "github.agent",
        "github.issue_agent",
    }
)

#: The stamp a joined device gives values it took from a device that wasn't
#: syncing yet: older than any real edit, so that device's first pass wins.
_SEED_TS = 1.0

#: Seconds a checkout's origin URL is remembered (canonical form).
_ORIGIN_TTL = 60.0

#: Seconds a stamp from another device may lie ahead of this clock: a later
#: one is clamped (a stamp in the far future would freeze its unit forever —
#: no later edit could ever be newer).
_MAX_SKEW = 300.0

#: What the UI shows while settings.json can't be read.
UNREADABLE = "settings.json couldn't be read — sync paused"

#: Human names for the pin picker ("Keep different on this device").
GROUP_LABELS: Dict[str, str] = {
    "coding_cli": "Agents",
    "ticketing": "Tickets",
    "repository": "Repository",
    "github": "GitHub",
    "engine": "Ticket sessions",
    "ui": "Appearance",
    "general": "General",
    "notifications": "Notifications",
    "extensions": "Extensions",
    "prefs": "Preferences",
    "store": "Saved items",
}

LABELS: Dict[str, str] = {
    "coding_cli.default_provider": "Default agent",
    "coding_cli.assistant_provider": "Assistant agent",
    "coding_cli.default_launch_args": "Agent launch flags",
    "ticketing.sources": "Ticket sources",
    "repository.url": "Repository",
    "repository.base_branch": "Base branch",
    "repository.pr_base_branch": "Pull request base branch",
    "repository.live_branch": "Live branch",
    "repository.git_transport": "Git transport (SSH/HTTPS)",
    "repository.fasttrack_depth": "Fast-track depth",
    "repository.precommit_retry_hooks": "Pre-commit retry hooks",
    "repository.verify_repos": "Verify: repositories",
    "repository.verify_enabled": "Verify on/off",
    "repository.verify_use_conversation": "Verify: use the conversation",
    "repository.deploy_delay_minutes": "Verify: deploy delay",
    "repository.verify_repo_settings": "Verify: per-repo settings",
    "github.token": "GitHub token",
    "github.base_branch": "GitHub base branch",
    "github.enabled": "Pull request review on/off",
    "github.issues_enabled": "Issue handling on/off",
    "github.min_age_minutes": "PR review: minimum age",
    "github.poll_interval_seconds": "PR review: poll interval",
    "github.skip_authors": "PR review: skipped authors",
    "github.repos": "PR review: repositories",
    "github.issue_repos": "Issues: repositories",
    "github.issue_min_age_minutes": "Issues: minimum age",
    "github.issue_poll_interval_seconds": "Issues: poll interval",
    "github.issue_skip_authors": "Issues: skipped authors",
    "github.agent": "PR review agent",
    "github.issue_agent": "Issue agent",
    "github.repo_settings": "PR review: per-repo settings",
    "github.issue_repo_settings": "Issues: per-repo settings",
    "engine.skip_permissions": "Skip permission prompts",
    "engine.agent": "Ticket agent",
    "ui.scroll_speed": "Terminal scroll speed",
    "ui.accent": "Accent colour",
    "general.session_budget_usd": "Session budget",
    "general.window_budget_usd": "Window budget",
    "general.tailnet_trusted_logins": "Trusted Tailscale accounts",
    "general.resume_on_usage_reset": "Resume when usage resets",
    "general.agent_mcp": "Agent-to-agent tools",
    "general.agent_mcp_scope": "Agent-to-agent scope",
    "general.agent_max_children": "Agent limit: children",
    "general.agent_max_spawn_depth": "Agent limit: spawn depth",
    "general.agent_max_spawned": "Agent limit: total spawned",
    "notifications.muted_rules": "Muted notifications",
    "notifications.enabled_rules": "Enabled notifications",
    "notifications.ntfy_enabled": "Phone notifications on/off",
    "notifications.ntfy_server": "ntfy server",
    "notifications.ntfy_topic": "ntfy topic",
    "notifications.ntfy_token": "ntfy token",
    "extensions.disabled": "Disabled extensions",
    "prefs.keymap": "Keyboard shortcuts",
    "prefs.prompt_presets": "Prompt presets",
    "prefs.theme": "Light/dark theme",
    "prefs.diff_mode": "Diff layout",
    "prefs.diff_base": "Diff base",
    "prefs.hidden_bars": "Hidden toolbars",
    "prefs.bar_order": "Toolbar order",
    "prefs.reduce_motion": "Reduce motion",
    "prefs.break_on": "Break reminders on/off",
    "prefs.break_every": "Break reminder interval",
    "prefs.idle_flock": "Idle flock on/off",
    "prefs.idle_after": "Idle flock delay",
    "prefs.hints": "Hints",
}

_LOCK = threading.RLock()
_peers: Dict[str, dict] = {}  # device key -> last pass result (memory only)
#: unit -> {"path", "value", "reason"}: newer values not adopted (yet).
_deferred: Dict[str, dict] = {}
#: unit -> "<what> couldn't be applied here: <why>" (a write that failed).
_warnings: Dict[str, str] = {}
#: unit -> (ts, by) of a remote version whose write didn't land here — not
#: retried until that device has a newer one (memory only).
_unlanded: Dict[str, Tuple[float, str]] = {}
#: Units left out of the last scan: their checkout's origin is unknown now.
_unresolved: Set[str] = set()
_ORIGINS: Dict[str, Tuple[float, str]] = {}  # checkout path -> (read at, origin)
#: checkout path -> last origin git gave for it (persisted as "canon"): what
#: the path stands for while git can't answer, or after the checkout moved.
_CANON: Dict[str, str] = {}
_unreadable_logged = ""

#: The loop the server runs sync on — a Settings save (a threadpool route)
#: schedules its nudge there.
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_soon: Optional[asyncio.TimerHandle] = None


# --------------------------------------------------------------------------- #
# stores (data outside settings.json)
# --------------------------------------------------------------------------- #
@dataclass
class _Store:
    name: str
    list_fn: Callable[[], Dict[str, object]]
    write_fn: Callable[[str, object], None]
    delete_fn: Callable[[str], None]
    canon_fn: Optional[Callable[[object], object]] = None
    localize_fn: Optional[Callable[[object, object], object]] = None
    label: str = ""


_STORES: Dict[str, _Store] = {}
_BUILTIN_STORES_LOADED = False


def register_store(
    name: str,
    list_fn: Callable[[], Dict[str, object]],
    write_fn: Callable[[str, object], None],
    delete_fn: Callable[[str], None],
    canon_fn: Optional[Callable[[object], object]] = None,
    localize_fn: Optional[Callable[[object, object], object]] = None,
    label: str = "",
) -> None:
    """Sync a store outside settings.json, entry by entry.

    ``list_fn()`` -> ``{key: value}`` (RAISE when the store can't be read —
    an empty answer would tombstone every entry on every device);
    ``write_fn(key, value)`` / ``delete_fn(key)`` change one entry through
    the store's own API; ``canon_fn(value)`` -> the portable form hashed and
    exported; ``localize_fn(incoming, current_or_None)`` -> what to write
    here. ``label`` names the store in the pin picker."""
    _STORES[name] = _Store(
        name, list_fn, write_fn, delete_fn, canon_fn, localize_fn, label or name
    )


def unregister_store(name: str) -> None:
    _STORES.pop(name, None)


def _stores() -> Dict[str, _Store]:
    """The registered stores, loading the built-in ones on first use (a lazy
    import: :mod:`sync_stores` imports this module to register)."""
    global _BUILTIN_STORES_LOADED
    if not _BUILTIN_STORES_LOADED:
        _BUILTIN_STORES_LOADED = True
        try:
            from backend.web.core import sync_stores  # noqa: F401 — registers
        except Exception as err:  # noqa: BLE001 — sync works without them
            _log_error("settings sync: built-in stores unavailable: %v", err)
    return _STORES


# --------------------------------------------------------------------------- #
# paths, units, labels
# --------------------------------------------------------------------------- #
def paths() -> List[str]:
    """Every synced settings field (``group.field``), keyed bases included."""
    return ["%s.%s" % (g, f) for g, fields in SYNCED.items() for f in fields]


def _simple_paths() -> List[str]:
    return [p for p in paths() if p not in KEYED]


def bases() -> List[str]:
    """Everything pinnable: every settings field + ``store:<name>``."""
    return paths() + ["store:%s" % n for n in _stores()]


def _split(unit: str) -> Tuple[str, Optional[str]]:
    """``(base, key)`` — key ``None`` for a plain field."""
    base, sep, key = unit.partition("#")
    return base, (key if sep else None)


def _valid_unit(unit: object) -> bool:
    if not isinstance(unit, str):
        return False
    base, key = _split(unit)
    if key is None:
        return base in _simple_paths()
    if not key:
        return False
    if base in KEYED:
        return True
    return base.startswith("store:") and base[len("store:") :] in _stores()


def _entry_key(base: str, item: object) -> str:
    if not isinstance(item, dict):
        return ""
    key = str(item.get(KEYED[base]) or "").strip()
    return key.lower() if base in _CASELESS_KEYS else key


def _label_for(base: str) -> str:
    if base.startswith("store:"):
        st = _stores().get(base[len("store:") :])
        return st.label if st else base
    return LABELS.get(base) or base.partition(".")[2].replace("_", " ").capitalize()


def syncable() -> List[dict]:
    """The pin picker's choices: ``[{path, label, group}]``."""
    out = []
    for base in bases():
        group = "store" if base.startswith("store:") else base.partition(".")[0]
        out.append(
            {
                "path": base,
                "label": _label_for(base),
                "group": GROUP_LABELS.get(group, group),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# state file
# --------------------------------------------------------------------------- #
def _state_path() -> Path:
    from backend.config import settings as _settings

    return _settings.settings_path().parent / "settings_sync.json"


def _clean_ts(raw: object, cap: float) -> Optional[float]:
    """A stamp time as a usable float: ``None`` when it isn't a finite,
    non-negative number; clamped to ``cap``."""
    try:
        ts = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts < 0:
        return None
    return min(ts, cap)


def _remote_ts(rs: object) -> Optional[float]:
    """The time of another device's stamp ``rs``, clamped to now +
    :data:`_MAX_SKEW` (``None``: unusable — skip the unit)."""
    if not isinstance(rs, dict):
        return None
    cap = time.time() + _MAX_SKEW
    ts = _clean_ts(rs.get("ts") or 0, cap)
    if ts is not None and ts == cap:
        _log_error("settings sync: clamped a future stamp from %v", rs.get("by"))
    return ts


def _clean_stamps(raw: object) -> Dict[str, dict]:
    """Stored stamps, minus what this version can't use: a unit id it
    doesn't sync (a v1 whole-list stamp like ``ticketing.sources``), a
    malformed stamp, a time that isn't a finite number. A time too far ahead
    is clamped, so a device that already took one recovers."""
    if not isinstance(raw, dict):
        return {}
    cap = time.time() + _MAX_SKEW
    out: Dict[str, dict] = {}
    for unit, st in raw.items():
        if not isinstance(st, dict) or not _valid_unit(unit):
            continue
        ts = _clean_ts(st.get("ts") or 0, cap)
        if ts is None:
            continue
        out[unit] = {**st, "ts": ts}
    return out


def _load() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["stamps"] = _clean_stamps(data.get("stamps"))
    data["enabled"] = bool(data.get("enabled"))
    canon = data.get("canon")
    for path, url in (canon.items() if isinstance(canon, dict) else []):
        if isinstance(path, str) and isinstance(url, str) and path and url:
            _CANON.setdefault(path, url)
    data["joined_from"] = str(data.get("joined_from") or "")
    pinned = data.get("pinned")
    data["pinned"] = (
        sorted({p for p in pinned if isinstance(p, str) and p})
        if isinstance(pinned, list)
        else []
    )
    return data


def _save(data: dict) -> None:
    data["canon"] = dict(_CANON)
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sync.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _log_error(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        log.ErrorLog.Printf(fmt, *args)


def _self_key() -> str:
    from backend.web.core import remote as _remote

    return _remote.self_identity()["key"]


def _fleet_id() -> str:
    try:
        from backend.web.core import fleet as _fleet

        return str(_fleet.fleet_id() or "")
    except Exception:  # noqa: BLE001 — no fleet module / store = no fleet
        return ""


def _in_fleet() -> bool:
    try:
        from backend.web.core import fleet as _fleet

        return bool(_fleet.in_fleet())
    except Exception:  # noqa: BLE001
        return False


def _fleet_devices() -> List[dict]:
    from backend.web.core import remote as _remote

    try:
        return [d for d in _remote.fleet_devices() if isinstance(d, dict)]
    except Exception:  # noqa: BLE001
        return []


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _newer(a: Optional[dict], b: Optional[dict]) -> bool:
    """Whether stamp ``a`` is strictly newer than ``b``."""
    if not a:
        return False
    if not isinstance(b, dict) or not b:
        return True
    return (float(a.get("ts") or 0), str(a.get("by") or "")) > (
        float(b.get("ts") or 0),
        str(b.get("by") or ""),
    )


def _tick(now: float, prev: Optional[dict]) -> float:
    """The stamp time for a change made now: never at or before the stamp it
    replaces (a value adopted from a fast-clock device must still lose to a
    later edit here)."""
    if not isinstance(prev, dict):
        return now
    return max(now, float(prev.get("ts") or 0) + 0.001)


# --------------------------------------------------------------------------- #
# canonical form: local checkout <-> origin URL
# --------------------------------------------------------------------------- #
def _fs_path(value: str) -> str:
    p = value[len("file://") :] if value.startswith("file://") else value
    return os.path.expanduser(p)


class _Unresolved(Exception):
    """A checkout's origin can't be told right now (git failed or timed out)
    and none is remembered: the unit sits this pass out — exporting the raw
    path would point every other device at a folder only this one has."""


def _remember_origin(path: str, url: str) -> None:
    if _CANON.get(path) == url:
        return
    if len(_CANON) > 512:
        _CANON.clear()
    _CANON[path] = url


def _origin_of(path: str) -> Optional[str]:
    """``path``'s ``origin`` URL; ``""`` when it definitely has none (not a
    git checkout, no ``origin`` remote, a folder that isn't there and never
    had one); ``None`` when git couldn't be asked (timeout, crash) and no
    origin is remembered for it. Answers are cached :data:`_ORIGIN_TTL` s —
    the export and every scan canonicalise, and git is a process spawn — and
    a real origin is remembered in :data:`_CANON` (persisted), which also
    answers for a checkout that has since moved away."""
    if not path:
        return ""
    now = time.time()
    hit = _ORIGINS.get(path)
    if hit is not None and now - hit[0] < _ORIGIN_TTL:
        return hit[1]
    if not os.path.isdir(path):
        url = _CANON.get(path, "")
    else:
        try:
            cp = subprocess.run(
                ["git", "-C", path, "remote", "get-url", "origin"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
        except Exception:  # noqa: BLE001 — no git / timeout: don't know
            return _CANON.get(path)  # not cached: ask again next time
        if cp.returncode == 0 and cp.stdout.strip():
            url = cp.stdout.strip()
            _remember_origin(path, url)
        elif cp.returncode == 2 or not os.path.exists(os.path.join(path, ".git")):
            url = ""  # "no such remote" / not a checkout at all
        else:
            return _CANON.get(path)  # a checkout git choked on: don't know
    if len(_ORIGINS) > 512:
        _ORIGINS.clear()
    _ORIGINS[path] = (now, url)
    return url


def canonical_url(value: object) -> object:
    """A repo reference as it travels: a local checkout becomes its origin URL
    (a forge URL means the same repo on every machine; a path doesn't).
    Anything else — a URL, a folder that isn't a checkout with a forge
    origin — is unchanged. Raises :class:`_Unresolved` when git can't tell
    right now and no origin is remembered for the path."""
    from backend.session.git import remote_url

    if not isinstance(value, str) or not value or not remote_url.is_local_path(value):
        return value
    url = _origin_of(_fs_path(value))
    if url is None:
        raise _Unresolved(value)
    return url if url and not remote_url.is_local_path(url) else value


def known_checkouts() -> List[str]:
    """Local checkouts a synced repo URL can be mapped onto: the repos of the
    sessions open here, the last repo used, and the immediate git subdirs of
    the workspace dir. Best-effort; never raises."""
    out: List[str] = []

    def _add(p: object) -> None:
        if isinstance(p, str) and p and p not in out and len(out) < 200:
            out.append(p)

    try:
        srv = sys.modules.get("backend.web.server")
        engine = getattr(srv, "ENGINE", None) if srv is not None else None
        for inst in list((getattr(engine, "instances", None) or {}).values()):
            _add(getattr(inst, "Path", ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.config import settings as _settings

        s = _settings.load_settings()
        _add(s.general.last_repo_path)
        ws = s.repository.workspace_dir
        if ws and os.path.isdir(os.path.expanduser(ws)):
            root = os.path.expanduser(ws)
            for name in sorted(os.listdir(root))[:200]:
                sub = os.path.join(root, name)
                if os.path.isdir(os.path.join(sub, ".git")) or os.path.isfile(
                    os.path.join(sub, ".git")
                ):
                    _add(sub)
    except Exception:  # noqa: BLE001
        pass
    return out


def localize_url(incoming: object, current: object = "") -> object:
    """What to store here for a synced repo reference ``incoming``: this
    machine's checkout of that repo when there is one (the path already
    stored, when it still points there; else a known checkout), else the
    URL itself."""
    from backend.session.git import remote_url

    if not isinstance(incoming, str) or not incoming:
        return incoming
    if remote_url.is_local_path(incoming):
        # Another machine's folder (a checkout without a forge origin, or a
        # plain directory): never overwrite a path here with one that doesn't
        # exist on this machine.
        if (
            isinstance(current, str)
            and current
            and not os.path.isdir(_fs_path(incoming))
        ):
            return current
        return incoming
    try:
        if isinstance(current, str) and current and remote_url.is_local_path(current):
            cur = canonical_url(current)
            if cur == incoming or (
                isinstance(cur, str) and remote_url.same_repo(cur, incoming)
            ):
                return current
        for path in known_checkouts():
            origin = _origin_of(path)
            if origin and remote_url.same_repo(origin, incoming):
                return path
    except Exception:  # noqa: BLE001 — a URL is always a valid answer
        pass
    return incoming


def canonical(unit: str, value: object) -> object:
    """``value`` (unit ``unit``) in the form that's hashed and exported."""
    base, _key = _split(unit)
    try:
        if base == "repository.url":
            return canonical_url(value)
        if base == "ticketing.sources" and isinstance(value, dict):
            out = dict(value)
            if out.get("repo_url"):
                out["repo_url"] = canonical_url(out["repo_url"])
            return out
        if base.startswith("store:"):
            st = _stores().get(base[len("store:") :])
            if st is not None and st.canon_fn is not None:
                return st.canon_fn(value)
    except _Unresolved:
        raise  # the unit sits this pass out (see _Unresolved)
    except Exception as err:  # noqa: BLE001 — the raw value still syncs
        _log_error("settings sync: canonical %s failed: %v", unit, err)
    return value


def localize(unit: str, incoming: object, current: object = None) -> object:
    """What to write here for an adopted canonical ``incoming`` (``current``
    = this device's raw value of the unit, or None)."""
    base, _key = _split(unit)
    try:
        if base == "repository.url":
            return localize_url(incoming, current)
        if base == "ticketing.sources" and isinstance(incoming, dict):
            out = dict(incoming)
            if out.get("repo_url"):
                cur = current.get("repo_url") if isinstance(current, dict) else ""
                out["repo_url"] = localize_url(out["repo_url"], cur or "")
            return out
        if base.startswith("store:"):
            st = _stores().get(base[len("store:") :])
            if st is not None and st.localize_fn is not None:
                return st.localize_fn(incoming, current)
    except Exception as err:  # noqa: BLE001
        _log_error("settings sync: localize %s failed: %v", unit, err)
    return incoming


# --------------------------------------------------------------------------- #
# reading + writing the local units
# --------------------------------------------------------------------------- #
def _settings_doc() -> dict:
    """settings.json as a dict, read strictly: raises
    ``settings.SettingsUnreadable`` when the file exists but can't be read —
    sync must never mistake a broken file for an empty one (that reads as
    "every source deleted, every token cleared" and spreads to every device,
    and a write would save over the file the user could still fix)."""
    from backend.config import settings as _settings

    _settings.invalidate()  # another process may have written the file
    return _settings.load_settings_strict().to_dict()


def readable() -> bool:
    """Whether settings.json can be read (a missing file can)."""
    from backend.config import settings as _settings

    try:
        _settings.load_settings_strict()
        return True
    except _settings.SettingsUnreadable:
        return False


def _note_unreadable(err: Exception) -> None:
    """Log a paused pass once per distinct problem."""
    global _unreadable_logged
    if str(err) != _unreadable_logged:
        _unreadable_logged = str(err)
        _log_error("settings sync paused: %v", err)


def _source_id(item: dict, taken: Set[str]) -> str:
    """A stable id for a ticket source saved without one (hand-edited):
    ``<provider>-<6 hex of what identifies it>``, ``-2``… when taken."""
    ident = "|".join(
        str(item.get(f) or "").strip()
        for f in ("provider", "base_url", "project", "label")
    )
    stem = "%s-%s" % (
        str(item.get("provider") or "source").strip().lower() or "source",
        hashlib.sha1(ident.encode("utf-8")).hexdigest()[:6],
    )
    sid, n = stem, 2
    while sid in taken:
        sid, n = "%s-%d" % (stem, n), n + 1
    return sid


def _ensure_source_ids(doc: dict) -> bool:
    """Give every ticket source without an id a stable one (in ``doc``), so
    it syncs as an entry like any other instead of staying on this device.
    Returns whether anything changed (the caller writes it back once)."""
    items = (doc.get("ticketing") or {}).get("sources")
    if not isinstance(items, list):
        return False
    taken = {
        str(i.get("id") or "").strip()
        for i in items
        if isinstance(i, dict) and str(i.get("id") or "").strip()
    }
    changed = False
    for i, item in enumerate(items):
        if isinstance(item, dict) and not str(item.get("id") or "").strip():
            sid = _source_id(item, taken)
            taken.add(sid)
            items[i] = {**item, "id": sid}
            changed = True
    return changed


def _snapshot() -> Tuple[Dict[str, object], Set[str]]:
    """``({unit: raw local value}, {store bases that couldn't be read})``.

    A plain field is always present (``None`` = unset); a keyed or store
    entry only while it exists. An unreadable store is reported, not read as
    empty — that would tombstone its every entry fleet-wide; an unreadable
    settings.json raises ``SettingsUnreadable`` for the same reason."""
    from backend.config import settings as _settings

    doc = _settings_doc()
    if _ensure_source_ids(doc):
        _settings.save_settings(_settings.Settings.from_dict(doc))
        doc = _settings_doc()
    raw: Dict[str, object] = {}
    for group, fields in SYNCED.items():
        g = doc.get(group) if isinstance(doc.get(group), dict) else {}
        for f in fields:
            path = "%s.%s" % (group, f)
            if path not in KEYED:
                raw[path] = g.get(f)
                continue
            items = g.get(f)
            for item in items if isinstance(items, list) else []:
                key = _entry_key(path, item)
                if key and "%s#%s" % (path, key) not in raw:
                    raw["%s#%s" % (path, key)] = item
    failed: Set[str] = set()
    for name, st in list(_stores().items()):
        try:
            entries = st.list_fn()
            if not isinstance(entries, dict):
                raise TypeError("list_fn returned %s" % type(entries).__name__)
        except Exception as err:  # noqa: BLE001
            failed.add("store:" + name)
            _log_error("settings sync: can't read %s: %v", name, err)
            continue
        for key, value in entries.items():
            if key:
                raw["store:%s#%s" % (name, key)] = value
    return raw, failed


def _apply(values: Dict[str, object]) -> None:
    """Write settings fields in one save (``None`` clears to the default)."""
    from backend.config import settings as _settings

    merged = _settings_doc()  # never save over a file that didn't parse
    for path, value in values.items():
        group, _, field = path.partition(".")
        g = dict(merged.get(group) or {})
        if value is None:
            g.pop(field, None)
        else:
            g[field] = value
        if g:
            merged[group] = g
        else:
            merged.pop(group, None)
    _settings.save_settings(_settings.Settings.from_dict(merged))


def _rebuild(
    base: str,
    items: List[object],
    put: Dict[str, object],
    drop: Set[str],
    order: Dict[str, int],
) -> List[object]:
    """A keyed list after adoption: local order for the entries that
    survive (replaced in place), tombstoned ones dropped, new ones appended
    in the remote's order. Entries without a key are local-only and stay."""
    out: List[object] = []
    seen: Set[str] = set()
    for item in items:
        key = _entry_key(base, item)
        if key and key in drop:
            continue
        if key and key in put:
            if key not in seen:
                out.append(put[key])
                seen.add(key)
            continue
        out.append(item)
        if key:
            seen.add(key)
    new = [k for k in put if k not in seen]
    new.sort(key=lambda k: order.get("%s#%s" % (base, k), len(order)))
    out.extend(put[k] for k in new)
    return out


def _write(
    take: Dict[str, object], drop: Set[str], order: Dict[str, int]
) -> Dict[str, str]:
    """Write adopted values (already localized) and deletions. Returns
    ``{unit: why}`` for the writes that failed."""
    simple: Dict[str, object] = {}
    keyed: Dict[str, Tuple[Dict[str, object], Set[str]]] = {}
    failed: Dict[str, str] = {}
    stores = _stores()
    for unit in list(take) + sorted(drop):
        base, key = _split(unit)
        deleting = unit in drop
        if key is None:
            simple[unit] = take[unit]
        elif base in KEYED:
            put, gone = keyed.setdefault(base, ({}, set()))
            if deleting:
                gone.add(key)
            else:
                put[key] = take[unit]
        else:
            st = stores.get(base[len("store:") :])
            try:
                if st is None:
                    raise LookupError("no store %s" % base)
                if deleting:
                    st.delete_fn(key)
                else:
                    st.write_fn(key, take[unit])
            except Exception as err:  # noqa: BLE001 — one entry, not the pass
                failed[unit] = str(err) or type(err).__name__
                _log_error("settings sync: writing %s failed: %v", unit, err)
    if keyed:
        doc = _settings_doc()
        for base, (put, gone) in keyed.items():
            group, _, field = base.partition(".")
            items = (doc.get(group) or {}).get(field)
            simple[base] = _rebuild(
                base, items if isinstance(items, list) else [], put, gone, order
            )
    if simple:
        _apply(simple)
    return failed


# --------------------------------------------------------------------------- #
# local side
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return _load()["enabled"]


def _field_default(path: str) -> object:
    from backend.config import settings as _settings

    group, _, field = path.partition(".")
    return getattr(getattr(_settings.Settings(), group, None), field, None)


def _is_unset(unit: str, value: object) -> bool:
    """Whether a plain field's value means "not set" (``None`` — what an
    export carries for a field at its default — or the default itself)."""
    if value is None:
        return True
    try:
        default = _field_default(unit)
    except Exception:  # noqa: BLE001
        return False
    return default is not None and value == default


def _unit_label(unit: str) -> str:
    """``unit`` for a person: "Ticket sources “sc”", "GitHub token"."""
    base, key = _split(unit)
    label = _label_for(base)
    return label if key is None else "%s “%s”" % (label, key)


def scan_local(now: Optional[float] = None) -> List[str]:
    """Stamp every unit whose value changed since its stamp (a save here, a
    hand edit, another process) and tombstone keyed/store entries that are
    gone. Returns the changed units. Nothing at all while settings.json
    can't be read; a unit whose checkout origin git can't tell right now is
    skipped (:class:`_Unresolved`)."""
    from backend.config import settings as _settings

    with _LOCK:
        data = _load()
        if not data["enabled"]:
            return []
        now = time.time() if now is None else now
        me = _self_key()
        pinned = set(data["pinned"])
        try:
            raw, failed = _snapshot()
        except _settings.SettingsUnreadable as err:
            _note_unreadable(err)
            return []
        stamps = data["stamps"]
        changed = []
        unresolved: Set[str] = set()
        for unit, value in raw.items():
            if _split(unit)[0] in pinned:
                continue
            try:
                h = _hash(canonical(unit, value))
            except _Unresolved:
                unresolved.add(unit)
                continue
            st = stamps.get(unit)
            if isinstance(st, dict) and not st.get("deleted") and st.get("h") == h:
                continue
            stamps[unit] = {"ts": _tick(now, st), "by": me, "h": h}
            changed.append(unit)
        for unit, st in list(stamps.items()):
            base, key = _split(unit)
            if key is None or unit in raw or base in pinned or base in failed:
                continue
            if unit in _unlanded:
                continue  # never landed here: that isn't a delete
            if isinstance(st, dict) and st.get("deleted"):
                continue
            stamps[unit] = {"ts": _tick(now, st), "by": me, "h": "-", "deleted": True}
            changed.append(unit)
        _unresolved.clear()
        _unresolved.update(unresolved)
        if changed:
            _save(data)
        return sorted(changed)


def export() -> dict:
    """What another fleet device pulls: stamps + canonical values of every
    unit that isn't pinned here (tombstones included). Secrets are always
    in it — the route only answers a caller holding the fleet key or this
    device's token. Raises ``SettingsUnreadable`` while settings.json can't
    be read (the route answers 503: an empty export would read as "all
    deleted"). A unit whose checkout origin is unknown right now is left out."""
    scan_local()
    with _LOCK:
        data = _load()
        pinned = set(data["pinned"])
        raw, _failed = _snapshot()
        values = {}
        for u, v in raw.items():
            if _split(u)[0] in pinned:
                continue
            try:
                values[u] = canonical(u, v)
            except _Unresolved:
                continue
        stamps = {
            u: st
            for u, st in data["stamps"].items()
            if isinstance(st, dict)
            and _valid_unit(u)
            and _split(u)[0] not in pinned
            and (u in values or st.get("deleted"))
        }
    return {
        "protocol": PROTOCOL,
        "fleet": _fleet_id(),
        "device": _self_key(),
        "enabled": data["enabled"],
        "stamps": stamps,
        "values": values,
        "withheld": [],
    }


def _defer(unit: str, value: object) -> bool:
    """Record (and report) that ``value`` for ``unit`` can't be adopted here
    yet — an agent CLI this machine doesn't have."""
    if unit not in DEFER_PATHS or not isinstance(value, str) or not value.strip():
        return False
    from backend.web.core import settings_hooks

    if settings_hooks.provider_installed(value.strip()):
        return False
    _deferred[unit] = {
        "path": unit,
        "value": value,
        "reason": "%s isn't installed on this device" % value.strip(),
    }
    return True


def _payload_ok(remote: object) -> bool:
    if not isinstance(remote, dict) or remote.get("protocol") != PROTOCOL:
        return False
    fid = _fleet_id()
    return bool(fid) and remote.get("fleet") == fid


def _pipeline_signature() -> Optional[tuple]:
    try:
        from backend.web.core import settings_hooks

        return settings_hooks.pipeline_signature()
    except Exception:  # noqa: BLE001
        return None


def _note_failed(unit: str, why: str) -> None:
    _warnings[unit] = "%s couldn't be applied here: %s" % (_unit_label(unit), why)


def merge(remote: dict) -> List[str]:
    """Adopt every unit ``remote`` (an :func:`export` of a device in this
    fleet) has a newer stamp for. Returns the adopted units. Nothing while
    settings.json can't be read."""
    from backend.config import settings as _settings

    if not _payload_ok(remote) or not remote.get("enabled"):
        return []
    rstamps = remote.get("stamps") if isinstance(remote.get("stamps"), dict) else {}
    rvalues = remote.get("values") if isinstance(remote.get("values"), dict) else {}
    source = str(remote.get("device") or "")
    with _LOCK:
        scan_local()  # a local edit not yet stamped must not lose to an older remote
        data = _load()
        if not data["enabled"]:
            return []
        pinned = set(data["pinned"])
        try:
            raw, failed = _snapshot()
        except _settings.SettingsUnreadable as err:
            _note_unreadable(err)
            return []
        sig_before = _pipeline_signature()
        take: Dict[str, object] = {}
        drop: Set[str] = set()
        stamps: Dict[str, dict] = {}
        for unit, rs in rstamps.items():
            if not _valid_unit(unit) or not isinstance(rs, dict):
                continue
            base, key = _split(unit)
            if base in pinned or base in failed:
                continue
            ts = _remote_ts(rs)
            if ts is None:
                continue
            by = str(rs.get("by") or "")
            if not _newer({"ts": ts, "by": by}, data["stamps"].get(unit)):
                _deferred.pop(unit, None)
                _unlanded.pop(unit, None)
                continue
            if _unlanded.get(unit) == (ts, by):
                continue  # this version already failed to land here
            try:
                here = _hash(canonical(unit, raw[unit])) if unit in raw else None
            except _Unresolved:
                continue  # can't tell what's here: never adopt over it
            st = {"ts": ts, "by": by}
            if rs.get("deleted"):
                if key is None:
                    continue  # a plain field is cleared, never deleted
                stamps[unit] = {**st, "h": "-", "deleted": True}
                if unit in raw:
                    drop.add(unit)
                continue
            if unit not in rvalues:
                continue
            value = rvalues[unit]
            if _defer(unit, value):
                continue
            _deferred.pop(unit, None)
            stamps[unit] = st
            if _hash(value) != here:
                take[unit] = localize(unit, value, raw.get(unit))
        if not stamps:
            return []
        order = {u: i for i, u in enumerate(rvalues)}
        try:
            errors = _write(take, drop, order) if (take or drop) else {}
            after, _failed = _snapshot()
        except _settings.SettingsUnreadable as err:
            _note_unreadable(err)
            return []
        missed: Set[str] = set()
        for unit, st in stamps.items():
            if st.get("deleted"):
                if unit not in after:
                    data["stamps"][unit] = st
                    _unlanded.pop(unit, None)
                    _warnings.pop(unit, None)
                else:
                    # Still here: a tombstone stamp would make the next scan
                    # re-add it everywhere. Remember, don't retry.
                    _unlanded[unit] = (st["ts"], st["by"])
                    _note_failed(unit, errors.get(unit) or "it couldn't be removed")
                    missed.add(unit)
                continue
            if unit in after:
                try:
                    h = _hash(canonical(unit, after[unit]))
                except _Unresolved:
                    continue
                # The hash of what was STORED (normalisation, a local path
                # for a URL), so the next scan doesn't read the adoption as a
                # local edit — and, when the write failed, so it isn't
                # retried every pass nor spread from here as an edit.
                data["stamps"][unit] = {**st, "h": h}
                _unlanded.pop(unit, None)
                if unit in errors:
                    _note_failed(unit, errors[unit])
                    missed.add(unit)
                else:
                    _warnings.pop(unit, None)
            elif unit in take:
                # It didn't land (a write that failed, or one this device's
                # own rules refused). No stamp: a live stamp for an absent
                # entry reads as a delete on the next scan.
                _unlanded[unit] = (st["ts"], st["by"])
                _note_failed(unit, errors.get(unit) or "it didn't land")
                missed.add(unit)
        _save(data)
        adopted = sorted((set(take) | drop) - missed)
    if adopted:
        _after_change(adopted, source, sig_before)
    return adopted


def _after_change(
    units: List[str], source: str, pipeline_before: Optional[tuple] = None
) -> None:
    try:
        from backend.web.core import settings_hooks

        settings_hooks.after_settings_change(
            units, source=source, pipeline_before=pipeline_before
        )
    except Exception as err:  # noqa: BLE001
        _log_error("settings sync: after-change hooks failed: %v", err)


def _source_identity(item: object) -> Tuple[str, str, str]:
    """What makes two ticket sources the same source (an id is just a slug
    each device seeds on its own: every device's first Shortcut source is
    ``sc``)."""
    if not isinstance(item, dict):
        return ("", "", "")
    return (
        str(item.get("provider") or "").strip().lower(),
        str(item.get("project") or "").strip().lower(),
        str(item.get("base_url") or "").strip().rstrip("/").lower(),
    )


def _rename_clashing_sources(
    rvalues: Dict[str, object], raw: Dict[str, object], me: str
) -> Dict[str, str]:
    """Before a join's union: a ticket source here whose id the device
    joined also uses for a DIFFERENT source (provider/project/workspace) is
    renamed ``<id>-<this device>`` so both survive. Returns ``{old: new}``."""
    base = "ticketing.sources"
    clash: Set[str] = set()
    for unit, value in rvalues.items():
        b, key = _split(unit)
        if b != base or key is None or unit not in raw:
            continue
        if _source_identity(raw[unit]) != _source_identity(value):
            clash.add(key)
    if not clash:
        return {}
    taken = {_split(u)[1] for u in list(raw) + list(rvalues) if _split(u)[0] == base}
    items = (_settings_doc().get("ticketing") or {}).get("sources")
    renames: Dict[str, str] = {}
    out: List[object] = []
    for item in items if isinstance(items, list) else []:
        key = _entry_key(base, item)
        if key in clash and key not in renames and isinstance(item, dict):
            new, n = "%s-%s" % (key, me), 2
            while new in taken:
                new, n = "%s-%s-%d" % (key, me, n), n + 1
            taken.add(new)
            renames[key] = new
            item = {**item, "id": new}
        out.append(item)
    if renames:
        _apply({base: out})
        _log_error(
            "settings sync: renamed ticket sources %v so both devices' survive",
            renames,
        )
    return renames


def _adopt_all(body: dict, start_from: str) -> Tuple[List[str], Optional[tuple]]:
    """Join from ``start_from``: take every unit it has (it leads), keep
    entries only this device has (they spread from here), and stamp so the
    leader's values hold — except a plain field the leader has unset and
    this device has set: that stays, as a fresh edit here, so it spreads.
    Returns ``(adopted, pipeline signature before)``. Raises
    ``SettingsUnreadable`` while settings.json can't be read."""
    rvalues = body.get("values") if isinstance(body.get("values"), dict) else {}
    lead_syncing = bool(body.get("enabled"))
    rstamps = (
        body.get("stamps")
        if lead_syncing and isinstance(body.get("stamps"), dict)
        else {}
    )
    me = _self_key()
    with _LOCK:
        data = _load()
        pinned = set(data["pinned"])
        raw, failed = _snapshot()
        sig_before = _pipeline_signature()
        if _rename_clashing_sources(rvalues, raw, me):
            raw, failed = _snapshot()

        def _skip(unit: str) -> bool:
            return not _valid_unit(unit) or _split(unit)[0] in pinned | failed

        take: Dict[str, object] = {}
        drop: Set[str] = set()
        held: Set[str] = set()
        kept: Set[str] = set()
        for unit, value in rvalues.items():
            if _skip(unit):
                continue
            if (
                _split(unit)[1] is None
                and _is_unset(unit, value)
                and not _is_unset(unit, raw.get(unit))
            ):
                kept.add(unit)  # the leader has nothing there: keep ours
                continue
            if _defer(unit, value):
                held.add(unit)
                continue
            _deferred.pop(unit, None)
            try:
                here = _hash(canonical(unit, raw[unit])) if unit in raw else None
            except _Unresolved:
                held.add(unit)  # can't tell what's here: never adopt over it
                continue
            if _hash(value) != here:
                take[unit] = localize(unit, value, raw.get(unit))
        tombs = {
            u: rs
            for u, rs in rstamps.items()
            if isinstance(rs, dict)
            and rs.get("deleted")
            and not _skip(u)
            and _split(u)[1] is not None
        }
        drop = {u for u in tombs if u in raw}
        order = {u: i for i, u in enumerate(rvalues)}
        errors = _write(take, drop, order) if (take or drop) else {}
        bad = set(errors)
        for unit, why in errors.items():
            _note_failed(unit, why)
        after, _failed = _snapshot()
        now = time.time()
        stamps: Dict[str, dict] = {}
        for unit, value in after.items():
            if _split(unit)[0] in pinned:
                continue
            try:
                h = _hash(canonical(unit, value))
            except _Unresolved:
                continue  # stamped by a later scan, once git answers
            rs = rstamps.get(unit)
            rts = _remote_ts(rs)
            if unit in kept:
                stamps[unit] = {
                    "ts": _tick(now, {"ts": rts} if rts is not None else None),
                    "by": me,
                    "h": h,
                }
            elif (
                unit in rvalues
                and unit not in held
                and unit not in bad
                and isinstance(rs, dict)
                and not rs.get("deleted")
                and rts is not None
            ):
                stamps[unit] = {
                    "ts": rts or _SEED_TS,
                    "by": str(rs.get("by") or start_from),
                    "h": h,
                }
            else:
                # Held back / only here / the leader isn't syncing yet: older
                # than any real edit, so whatever the fleet has wins.
                by = start_from if unit in rvalues else me
                stamps[unit] = {"ts": _SEED_TS, "by": by, "h": h}
        for unit, rs in tombs.items():
            if unit not in after:
                stamps[unit] = {
                    "ts": _remote_ts(rs) or _SEED_TS,
                    "by": str(rs.get("by") or start_from),
                    "h": "-",
                    "deleted": True,
                }
        data.update(enabled=True, joined_from=start_from)
        data["stamps"] = stamps
        _save(data)
    return sorted((set(take) | drop) - bad), sig_before


def _lead_from_here(me: str, seed: bool) -> None:
    """Sync on with THIS device's values leading. Stamped now — except a
    plain field that's unset (it never outranks a value set elsewhere) — or,
    ``seed``, all older than any real edit: they spread only where no other
    device has a value (turning sync on as a side effect of admitting a
    device must not revert what the others edited meanwhile). A seeded
    enable on a device already syncing changes nothing."""
    with _LOCK:
        data = _load()
        if seed and data["enabled"]:
            return
        now = time.time()
        pinned = set(data["pinned"])
        raw, _failed = _snapshot()
        stamps: Dict[str, dict] = {}
        for u, v in raw.items():
            if _split(u)[0] in pinned:
                continue
            try:
                h = _hash(canonical(u, v))
            except _Unresolved:
                continue  # stamped by a later scan, once git answers
            plain_unset = _split(u)[1] is None and _is_unset(u, v)
            ts = _SEED_TS if (seed or plain_unset) else now
            stamps[u] = {"ts": ts, "by": me, "h": h}
        data.update(enabled=True, joined_from=me)
        data["stamps"] = stamps
        _save(data)


async def enable(start_from: str = "", *, seed: bool = False) -> dict:
    """Turn sync on. ``start_from`` is ``""``/this device (its values lead —
    see :func:`_lead_from_here` for ``seed``) or a fleet device's key (its
    values are adopted first). LookupError when this device isn't in a
    fleet, ``start_from`` isn't a reachable member, or settings.json can't
    be read."""
    from backend.config import settings as _settings
    from backend.web.core import remote as _remote

    _remember_loop()
    if not _in_fleet():
        raise LookupError("join your other devices first (Settings → Devices)")
    me = _self_key()
    if not start_from or start_from == me:
        try:
            await asyncio.to_thread(_lead_from_here, me, seed)
        except _settings.SettingsUnreadable as err:
            _note_unreadable(err)
            raise LookupError(UNREADABLE) from err
        _deferred.clear()
        return {"enabled": True, "adopted": [], "withheld": [], "deferred": []}

    dev = next((d for d in _fleet_devices() if d.get("key") == start_from), None)
    if dev is None:
        raise LookupError(
            "%s isn't one of your devices, or it's offline right now" % start_from
        )
    label = dev.get("host") or start_from
    status, body = await _remote.get_json(dev, EXPORT_PATH, timeout=10.0)
    if status != 200 or not isinstance(body, dict):
        raise LookupError(
            "couldn't read settings from %s (%s)" % (label, status or "unreachable")
        )
    if body.get("protocol") != PROTOCOL:
        raise LookupError("update MindFlock on %s to sync settings" % label)
    if not _payload_ok(body):
        raise LookupError("%s is in a different group of devices" % label)
    try:
        adopted, sig_before = await asyncio.to_thread(_adopt_all, body, start_from)
    except _settings.SettingsUnreadable as err:
        _note_unreadable(err)
        raise LookupError(UNREADABLE) from err
    if adopted:
        _after_change(adopted, start_from, sig_before)
    return {
        "enabled": True,
        "adopted": adopted,
        "withheld": [],
        "deferred": sorted(_deferred),
    }


def disable() -> None:
    """Turn sync off (what's pinned stays pinned for next time)."""
    with _LOCK:
        data = _load()
        data.update(enabled=False, joined_from="", stamps={})
        _save(data)
    _peers.clear()
    _deferred.clear()
    _warnings.clear()
    _unlanded.clear()
    _unresolved.clear()


def set_pinned(base: str, pinned: bool) -> List[str]:
    """Keep ``base`` (``group.field`` or ``store:<name>``) different on this
    device, or stop. Un-pinning restamps its current units as older than any
    real edit, so the fleet's value wins on the next pass. Returns the pinned
    list. ValueError for something that doesn't sync (or, un-pinning, while
    settings.json can't be read)."""
    from backend.config import settings as _settings

    if base not in bases():
        raise ValueError("%s isn't something settings sync shares" % base)
    with _LOCK:
        data = _load()
        current = set(data["pinned"])
        if pinned:
            current.add(base)
        elif base in current:
            try:
                raw, _failed = _snapshot()
            except _settings.SettingsUnreadable as err:
                raise ValueError(UNREADABLE) from err
            current.discard(base)
            me = _self_key()
            stamps = data["stamps"]
            for unit in [u for u in stamps if _split(u)[0] == base]:
                stamps.pop(unit)
            for unit, value in raw.items():
                if _split(unit)[0] != base:
                    continue
                try:
                    h = _hash(canonical(unit, value))
                except _Unresolved:
                    continue
                stamps[unit] = {"ts": _SEED_TS, "by": me, "h": h}
        data["pinned"] = sorted(current)
        _save(data)
        for unit in [u for u in _deferred if _split(u)[0] == base]:
            _deferred.pop(unit, None)
        return list(data["pinned"])


# --------------------------------------------------------------------------- #
# nudges: "something changed here — pull now"
# --------------------------------------------------------------------------- #
def _remember_loop() -> None:
    global _LOOP
    try:
        _LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass


def _spawn(coro) -> bool:
    """Run ``coro`` on the server loop from any thread; False (and the
    coroutine closed) when there's no loop to run it on."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(coro)
        return True
    target = _LOOP
    if target is not None and target.is_running() and not target.is_closed():
        asyncio.run_coroutine_threadsafe(coro, target)
        return True
    coro.close()
    return False


async def nudge_peers() -> List[str]:
    """Tell every fleet device to pull now (fleet-key auth). Returns the
    devices that took it; never raises."""
    from backend.web.core import remote as _remote

    devs = _fleet_devices()
    if not devs:
        return []

    async def _one(dev: dict) -> Optional[str]:
        try:
            status, _body = await _remote.post_json(dev, NUDGE_PATH, {}, timeout=3.0)
        except Exception:  # noqa: BLE001
            return None
        return dev.get("key") if status == 200 else None

    got = await asyncio.gather(*(_one(d) for d in devs))
    return [k for k in got if k]


def local_change() -> List[str]:
    """A Settings save here: stamp now (so "last edit wins" orders by when the
    user really changed it) and, when that stamped anything, nudge the other
    devices. Returns the stamped units; never raises."""
    try:
        changed = scan_local()
    except Exception as err:  # noqa: BLE001 — a settings save must never fail on this
        _log_error("settings sync: stamping a save failed: %v", err)
        return []
    if changed:
        try:
            _spawn(nudge_peers())
        except Exception:  # noqa: BLE001
            pass
    return changed


def nudged() -> bool:
    """Another device changed something: pull in :data:`NUDGE_DELAY` s (one
    pass for a burst). Must run on the server loop. Returns whether a pass
    was scheduled (False: one already is, or sync is off)."""
    global _soon
    _remember_loop()
    if not enabled():
        return False
    if _soon is not None:
        return False
    loop = asyncio.get_running_loop()

    def _go() -> None:
        global _soon
        _soon = None
        loop.create_task(_quiet_pass())

    _soon = loop.call_later(NUDGE_DELAY, _go)
    return True


async def _quiet_pass() -> None:
    try:
        await sync_once()
    except Exception as err:  # noqa: BLE001
        _log_error("settings sync (nudged) failed: %v", err)


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
def _refused_text(label: str, body) -> str:
    """Why a device refused this one's fleet key. Its 401 names its key epoch
    (backend.web.core.fleet.unauthorized_body): an OLDER one means IT missed a
    key change (gossip hands it the new key), a newer one that this device
    did. Never raises."""
    try:
        from backend.web.core import fleet as _fleet

        here = _fleet.unauthorized_body()  # this device's id + epoch
        theirs = int(body.get("epoch"))
        mine = int(here["epoch"])
        same = bool(here["id"]) and body.get("id") == here["id"]
    except Exception:  # noqa: BLE001 — an old peer: no epoch in its 401
        theirs = mine = None
        same = False
    if same and theirs is not None and theirs < mine:
        return "%s hasn't picked up the latest key yet — it will shortly" % label
    if same and theirs is not None and theirs > mine:
        return "%s has a newer key — this device missed a change" % label
    return "%s refused this device's key — it may have been removed; ask to rejoin" % (
        label
    )


async def sync_once() -> List[str]:
    """One pass: rescan, then pull every fleet device. Returns what was
    adopted. Nothing at all outside a fleet."""
    from backend.web.core import remote as _remote

    _remember_loop()
    if not enabled() or not _in_fleet():
        return []
    if not await asyncio.to_thread(readable):
        return []  # paused: status() says why
    await asyncio.to_thread(scan_local)
    adopted: List[str] = []
    for dev in _fleet_devices():
        label = dev.get("host") or dev.get("key") or "?"
        status, body = await _remote.get_json(dev, EXPORT_PATH, timeout=10.0)
        ok = status == 200 and isinstance(body, dict)
        entry = {
            "label": label,
            "at": time.time(),
            "ok": ok,
            "enabled": bool(ok and body.get("enabled")),
            "error": "",
        }
        if status == 404:
            entry["error"] = "that device's MindFlock is too old to sync settings"
        elif status == 503:
            entry["error"] = (
                "%s's settings file can't be read — sync is paused there" % label
            )
        elif status == 401:
            entry["error"] = _refused_text(label, body)
        elif not ok:
            entry["error"] = "unreachable" if not status else "HTTP %s" % status
        elif body.get("protocol") != PROTOCOL:
            entry["ok"] = False
            entry["error"] = "update MindFlock on %s to sync settings" % label
        elif not _payload_ok(body):
            entry["ok"] = False
            entry["error"] = "%s is in a different group of devices" % label
        else:
            got = await asyncio.to_thread(merge, body)
            if got:
                entry["adopted"] = got
                adopted.extend(got)
                if log.InfoLog is not None:
                    log.InfoLog.Printf(
                        "settings sync: took %s from %s", ", ".join(got), label
                    )
        _peers[dev["key"]] = {**(_peers.get(dev["key"]) or {}), **entry}
    return adopted


async def sync_loop() -> None:
    _remember_loop()
    while True:
        try:
            await sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — the loop must never die
            _log_error("settings sync failed: %v", err)
        await asyncio.sleep(INTERVAL)


def status() -> dict:
    """Settings → Devices' sync section."""
    data = _load()
    in_fleet = _in_fleet()
    ok = readable()
    devices = []
    for dev in _fleet_devices():
        p = _peers.get(dev["key"]) or {}
        devices.append(
            {
                "key": dev["key"],
                "label": dev.get("host") or dev["key"],
                "syncing": bool(p.get("enabled")),
                "last_sync": p.get("at") if p.get("ok") else None,
                "withheld": [],
                "error": p.get("error") or "",
            }
        )
    warnings = []
    if data["enabled"] and not in_fleet:
        # Also what a v1 state file (sync on, from before devices were
        # grouped) reads as: off until this device joins the others.
        warnings.append(
            "Settings sync was on here, but this device isn't one of your devices "
            "yet — nothing is shared until you join them (Settings → Devices)."
        )
    if not ok:
        warnings.append(
            UNREADABLE + " — fix the file (or restore it) and sync picks up again."
        )
    if data["enabled"] and in_fleet and _unresolved:
        warnings.append(
            "Not shared right now (git couldn't tell which repository the folder "
            "is): %s — retried every pass."
            % ", ".join(_unit_label(u) for u in sorted(_unresolved))
        )
    warnings.extend(v for _k, v in sorted(_warnings.items()))
    return {
        "enabled": data["enabled"] and in_fleet,
        "error": "" if ok else UNREADABLE,
        "device": _self_key(),
        "joined_from": data["joined_from"],
        "in_fleet": in_fleet,
        "devices": devices,
        "pinned": list(data["pinned"]),
        "deferred": [dict(v) for _k, v in sorted(_deferred.items())],
        "warnings": warnings,
        "syncable": syncable(),
    }
