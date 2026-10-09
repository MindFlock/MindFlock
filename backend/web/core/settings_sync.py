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
gets ``max(now, previous stamp + 1 ms)``, so an edit made here after seeing
a value from a device whose clock runs fast still wins over it — nothing is
held back for a clock (holding only delayed the comparison, then reverted
the later edit), and nothing warns about a clock that is merely ahead: a
stamp carried forward by :func:`_tick` would name the wrong device. Only a
stamp that can't be a real time (not a finite number, negative, more than
:data:`_MAX_AHEAD` s ahead) is skipped, and its device named in a warning.

**Canonical, then local.** A value that names a local checkout (``repository
.url``, a ticket source's ``repo_url``, a template's ``repo_path``) travels as
the checkout's origin URL and is turned back into THIS machine's checkout of
the same repo on adoption (:func:`canonical` / :func:`localize`).

**Deferral.** A field naming an agent CLI that isn't installed here is not
adopted (and its stamp not taken) until it is — :data:`DEFER_PATHS`.

**Pinning.** "Keep different on this device": a pinned base is neither
exported, adopted nor stamped (:func:`set_pinned`); so is a pinned unit (a
ticket source kept separate at join).

**Joining.** :func:`enable` either starts from THIS device (its values are
stamped now and spread from it — or, ``seed=True``, stamped older than any
real edit, so they only spread where nobody has a value) or from another
fleet device: that device's shareable values are adopted first, under its
stamps (or, if it isn't syncing yet, stamps older than any real edit, so its
first sync wins over nothing). A plain field the device joined doesn't have
set — and never really edited — never clears one set here: this device's
value is kept and spreads. Ids are never rewritten (a source's id is its
ticket slug prefix): a ticket source the device joined has, or deleted,
under the same id as a different one here is kept separate on this device.

**Never from a broken file.** A ``settings.json`` that exists but doesn't
parse pauses sync (nothing scanned, exported or adopted — read as empty it
would delete every source and clear every token on every device), and a
checkout whose origin git can't tell right now is left out of the pass rather
than shipped as this machine's path (the last origin seen is remembered in
``settings_sync.json``). Writers outside sync refuse too
(``settings.update_settings`` raises instead of saving defaults over it). A
scan that would clear most of what this device has set at once — a reset or
replaced file — pauses sync until the person says whose values to keep
(:func:`resume`). What a route just saved is the person's own edit and never
counts toward that (:func:`local_change` names it); a clear anywhere else
does, whichever scan finds it first.

**State file.** ``settings_sync.json`` carries ``"v": 2``. One without it was
written by v1 (whole-field stamps, every shared field stamped at a real time
— unset ones too): it is read as a fresh state (sync off, no stamps), or its
stamps would make every field it never set wipe the other devices' values.
If sync was on in it and this device is already in a group of two or more,
sync is turned straight back on, seeded (no join is coming to do it).
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
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

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
        "automation_device",
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

#: Shared fields that can't be kept different on one device: they are one
#: answer for the whole group (pinned on one device, two of them could each
#: believe they run PR review and review every PR twice).
UNPINNABLE = frozenset({"github.automation_device"})

#: The stamp a joined device gives values it took from a device that wasn't
#: syncing yet: older than any real edit, so that device's first pass wins.
_SEED_TS = 1.0
#: The stamp of a plain field that isn't set, given without a real edit (a
#: new field, a seeded start): below :data:`_SEED_TS`, so a value seeded on
#: another device always beats "not set" here — never a tie broken by key.
_UNSET_TS = 0.5

#: Seconds a checkout's origin URL is remembered (canonical form).
_ORIGIN_TTL = 60.0

#: Seconds a stamp may lie ahead of this clock and still be a real time. A
#: later one (or a non-finite / negative one) is skipped, with a warning; a
#: stored one is clamped on load. Anything closer is adopted as it is: a
#: change made here after it is stamped later still (:func:`_tick`).
_MAX_AHEAD = 365 * 24 * 3600.0

#: The ``settings_sync.json`` format; a file without it is v1 (see the
#: module doc's **State file**).
STATE_VERSION = 2

#: What the UI shows while settings.json can't be read.
UNREADABLE = "settings.json couldn't be read — sync paused"

#: What the UI shows while a scan that would clear most of this device's
#: settings at once is held back (:func:`scan_local`, :func:`resume`).
PAUSED = "This device's settings look reset — sync paused"
#: :func:`resume`'s choices: take the other devices' values back, or spread
#: this device's (reset) ones.
RESUME_CHOICES = ("theirs", "mine")
#: A background scan that would delete/clear at least this many units, and
#: at least half of what this device has set, pauses instead of stamping…
_MASS_MIN = 3
#: …once this device has at least this many units set (below it a bulk
#: delete is just an edit: a lightly configured device has little to lose).
_MASS_FLOOR = 8

#: How many checkout -> origin answers are remembered (least recently used
#: go first), in memory and in settings_sync.json.
_CANON_MAX = 200

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
    "github.automation_device": "Device that runs PR review and issues",
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
#: checkout path -> (read at, origin; None = git couldn't answer — also
#: cached, so a git that times out isn't asked again every call).
_ORIGINS: Dict[str, Tuple[float, Optional[str]]] = {}
#: checkout path -> last origin git gave for it (persisted as "canon"): what
#: the path stands for while git can't answer, or after the checkout moved.
#: Least recently used first; at most :data:`_CANON_MAX`.
_CANON: Dict[str, str] = {}
_unreadable_logged = ""
_v1_logged = False

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
    """Everything pinnable: every settings field (but :data:`UNPINNABLE`) +
    ``store:<name>``."""
    return [p for p in paths() if p not in UNPINNABLE] + [
        "store:%s" % n for n in _stores()
    ]


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
    except (TypeError, ValueError, OverflowError):  # 1e400 as a JSON int
        return None
    if not math.isfinite(ts) or ts < 0:
        return None
    return min(ts, cap)


def _remote_ts(rs: object, ahead: Optional[Dict[str, float]] = None) -> Optional[float]:
    """The time of another device's stamp ``rs``; ``None`` — skip the unit —
    when it isn't a finite, non-negative number or lies more than
    :data:`_MAX_AHEAD` s ahead of this clock — ``ahead`` records, per device
    that made such a stamp, its biggest lead (the warning). Any closer stamp
    is taken as it is, unremarked: nothing waits for a clock, and a lead
    carried forward by :func:`_tick` says nothing about whose clock is off."""
    if not isinstance(rs, dict):
        return None
    ts = _clean_ts(rs.get("ts") or 0, math.inf)
    if ts is None:
        return None
    lead = ts - time.time()
    if lead > _MAX_AHEAD:
        if ahead is not None:
            by = str(rs.get("by") or "")
            ahead[by] = max(ahead.get(by, 0.0), lead)
        return None
    return ts


def _clean_stamps(raw: object) -> Dict[str, dict]:
    """Stored stamps, minus what this version can't use: a unit id it
    doesn't sync (a v1 whole-list stamp like ``ticketing.sources``), a
    malformed stamp, a time that isn't a finite number. A time that can't be
    real (more than :data:`_MAX_AHEAD` s ahead) is clamped."""
    if not isinstance(raw, dict):
        return {}
    cap = time.time() + _MAX_AHEAD
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
    global _v1_logged
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    unmarked = bool(data) and data.get("v") != STATE_VERSION
    if unmarked:
        # v1: every shared field stamped at a real time, unset ones too — kept,
        # those stamps would make what this device never set clear the other
        # devices' values the moment they join it. Start fresh (sync off; a
        # join turns it on, seeded — and so does already being in a group,
        # below); only the remembered origins are kept.
        data = {"canon": data.get("canon"), "v1": bool(data.get("enabled"))}
    data["v"] = STATE_VERSION
    data["v1"] = bool(data.get("v1"))
    data["stamps"] = _clean_stamps(data.get("stamps"))
    data["enabled"] = bool(data.get("enabled"))
    _seed_canon(data.get("canon"))
    data["joined_from"] = str(data.get("joined_from") or "")
    pinned = data.get("pinned")
    data["pinned"] = (
        sorted({p for p in pinned if isinstance(p, str) and p})
        if isinstance(pinned, list)
        else []
    )
    separate = data.get("separate")
    data["separate"] = {
        u: str(v or "")
        for u, v in (separate.items() if isinstance(separate, dict) else [])
        if isinstance(u, str) and u in data["pinned"]
    }
    paused = data.get("paused")
    data["paused"] = paused if isinstance(paused, dict) else None
    if unmarked and not (data["v1"] and _resume_migrated(data)) and not _v1_logged:
        _v1_logged = True
        _log_error(
            "settings sync: %v is from an earlier version — starting fresh "
            "(sync turns on again when this device joins your others)",
            str(_state_path()),
        )
    return data


def _resume_migrated(data: dict) -> bool:
    """A state file from an earlier version had sync on, and this device is
    already in a group of two or more: no join is coming to turn sync back
    on, so turn it on now, seeded (:func:`_lead_from_here`'s ``seed``: what's
    here spreads only where no other device has a value) — instead of
    stopping without a word. Left off (and retried on the next load) while
    settings.json can't be read. Returns whether it did."""
    if _live_count() < 2:
        return False
    me = _self_key()
    if not me:
        return False
    with _LOCK:
        try:
            stamps = _lead_stamps(data, me, True)
        except Exception:  # noqa: BLE001 — SettingsUnreadable: next load retries
            return False
        data.update(stamps=stamps, enabled=True, joined_from=me, paused=None)
        _save(data)  # marked "v": 2 — migrated once
        data["v1"] = False  # _save drops it from the dict
    _log_error(
        "settings sync: %v was from an earlier version; this device is in a "
        "group of devices, so sync is back on — seeded: where another device "
        "has a value, it wins",
        str(_state_path()),
    )
    return True


def _live_count() -> int:
    """How many live members this device's group has (itself included);
    0 when it isn't in one."""
    try:
        from backend.web.core import fleet as _fleet

        if not _fleet.in_fleet():
            return 0
        live = _fleet.live_members()
        return len(live) if isinstance(live, dict) else 0
    except Exception:  # noqa: BLE001 — no fleet store = a lone device
        return 0


def _seed_canon(canon: object) -> None:
    """Take the persisted origins this process doesn't know as the LEAST
    recently used ones (what this process looked up since is fresher), then
    cap — so the file never grows past :data:`_CANON_MAX`."""
    if not isinstance(canon, dict):
        return
    older = {
        p: u
        for p, u in canon.items()
        if isinstance(p, str) and isinstance(u, str) and p and u and p not in _CANON
    }
    if not older:
        return
    fresher = dict(_CANON)
    _CANON.clear()
    _CANON.update(older)
    _CANON.update(fresher)
    _trim_canon()


def _trim_canon() -> None:
    while len(_CANON) > _CANON_MAX:
        _CANON.pop(next(iter(_CANON)), None)


def _save(data: dict) -> None:
    _trim_canon()
    data["canon"] = dict(_CANON)
    data["v"] = STATE_VERSION
    data.pop("v1", None)  # in memory only: "v1 sync was on here" (status())
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


def _canon_get(path: str) -> Optional[str]:
    """The remembered origin of ``path`` (marked as just used)."""
    url = _CANON.pop(path, None)
    if url is not None:
        _CANON[path] = url
    return url


def _remember_origin(path: str, url: str) -> None:
    _CANON.pop(path, None)
    _CANON[path] = url
    _trim_canon()


def _origin_of(path: str, *, remember: bool = True) -> Optional[str]:
    """``path``'s ``origin`` URL; ``""`` when it definitely has none (not a
    git checkout, no ``origin`` remote, a folder that isn't there and never
    had one); ``None`` when git couldn't be asked (timeout, crash) and no
    origin is remembered for it. Answers — "git couldn't answer" included,
    or a git that times out would cost 3 s per call while sync holds its
    lock — are cached :data:`_ORIGIN_TTL` s (the export and every scan
    canonicalise, and git is a process spawn); a real origin is remembered in
    :data:`_CANON` (persisted; ``remember=False`` for a mere probe), which
    also answers for a checkout that has since moved away."""
    if not path:
        return ""
    now = time.time()
    hit = _ORIGINS.get(path)
    if hit is not None and now - hit[0] < _ORIGIN_TTL:
        return hit[1] if hit[1] is not None else _canon_get(path)
    url: Optional[str]
    if not os.path.isdir(path):
        url = _canon_get(path) or ""
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
            cp = None
        if cp is None:
            url = None
        elif cp.returncode == 0 and cp.stdout.strip():
            url = cp.stdout.strip()
            if remember:
                _remember_origin(path, url)
        elif cp.returncode == 2 or not os.path.exists(os.path.join(path, ".git")):
            url = ""  # "no such remote" / not a checkout at all
        else:
            url = None  # a checkout git choked on: don't know
    if len(_ORIGINS) > 512:
        _ORIGINS.clear()
    _ORIGINS[path] = (now, url)
    return url if url is not None else _canon_get(path)


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
            # A probe: a session worktree is never a synced value, so its
            # origin isn't worth remembering after it's gone.
            origin = _origin_of(path, remember=False)
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


def _is_pinned(unit: str, pinned: Set[str]) -> bool:
    """Whether ``unit`` stays different here: its base is pinned ("Keep
    different on this device"), or the unit itself is — a ticket source kept
    separate at join (:func:`_separate_sources`)."""
    return unit in pinned or _split(unit)[0] in pinned


def _snapshot() -> Tuple[Dict[str, object], Set[str]]:
    """``({unit: raw local value}, {store bases that couldn't be read})``.

    A plain field is always present (``None`` = unset); a keyed or store
    entry only while it exists. An unreadable store is reported, not read as
    empty — that would tombstone its every entry fleet-wide; an unreadable
    settings.json raises ``SettingsUnreadable`` for the same reason. A
    keyed entry without a key (a ticket source saved with no id) isn't a
    unit: it stays on this device (its id is its ticket slug prefix — making
    one up would re-ingest every ticket it already brought in)."""
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


def _unset_hashes(unit: str) -> Set[str]:
    """The hashes a plain field's stamp has while it isn't set."""
    out = {_hash(None)}
    try:
        out.add(_hash(_field_default(unit)))
    except Exception:  # noqa: BLE001
        pass
    return out


def _set_here(unit: str, st: object) -> bool:
    """Whether stamp ``st`` says ``unit`` holds a value here (a live keyed or
    store entry, a plain field that isn't at its default)."""
    if not isinstance(st, dict) or st.get("deleted"):
        return False
    return _split(unit)[1] is not None or st.get("h") not in _unset_hashes(unit)


def paused() -> bool:
    """Whether sync is held back because this device's settings look reset
    (:func:`scan_local`); :func:`resume` says what to do."""
    return bool(_load()["paused"])


def scan_local(
    now: Optional[float] = None,
    *,
    force: bool = False,
    attributed: Iterable[str] = (),
) -> List[str]:
    """Stamp every unit whose value changed since its stamp (a save here, a
    hand edit, another process) and tombstone keyed/store entries that are
    gone. Returns the changed units. Nothing at all while settings.json
    can't be read; a unit whose checkout origin git can't tell right now is
    skipped (:class:`_Unresolved`). A plain field seen for the first time
    while it is unset is stamped older than any real edit: a new field has
    nothing to spread yet.

    A scan whose unexplained clears (outside ``attributed`` — a change
    nobody here made through MindFlock) would delete or clear
    :data:`_MASS_MIN`+ units
    AND at least half of what this device has set (once that's
    :data:`_MASS_FLOOR`+ units) looks like a reset file (replaced, restored
    from an old backup), not edits: nothing is stamped, sync pauses
    (:data:`PAUSED`, announced as ``settings.sync_paused``) until
    :func:`resume` — ``force`` is its "keep mine". ``attributed`` names the
    bases a route just saved (:func:`local_change`): what this scan clears
    under them is the person's own edit and never counts toward a pause;
    anything else it clears (a file replaced or deleted since the last scan)
    still does."""
    paused_now: Optional[dict] = None
    if isinstance(attributed, str):
        attributed = (attributed,)
    mine = frozenset(p for p in attributed if isinstance(p, str) and p)
    try:
        with _LOCK:
            changed, paused_now = _scan_locked(now, force, mine)
    finally:
        if paused_now is not None:
            _emit_paused(paused_now)
    return changed


def _emit_paused(info: dict) -> None:
    try:
        from backend.web.core import events as _events

        _events.BUS.emit("settings.sync_paused", data=info)
    except Exception as err:  # noqa: BLE001 — an event is best-effort
        _log_error("settings sync: announcing the pause failed: %v", err)


def _attributed(unit: str, paths: FrozenSet[str]) -> bool:
    """Whether ``unit`` lies under one of the bases a route saved: the unit
    itself, its keyed/store base (``ticketing.sources``, ``store:providers``)
    or a settings group / field above it (``github``)."""
    base = _split(unit)[0]
    return any(unit == p or base == p or base.startswith(p + ".") for p in paths)


def _scan_locked(
    now: Optional[float], force: bool, attributed: FrozenSet[str]
) -> Tuple[List[str], Optional[dict]]:
    """:func:`scan_local` under :data:`_LOCK`: ``(changed, pause info when
    this scan paused sync)``."""
    from backend.config import settings as _settings

    data = _load()
    if not data["enabled"] or (data["paused"] and not force):
        return [], None
    now = time.time() if now is None else now
    me = _self_key()
    pinned = set(data["pinned"])
    try:
        raw, failed = _snapshot()
    except _settings.SettingsUnreadable as err:
        _note_unreadable(err)
        return [], None
    # A unit kept separate whose entry is gone here (renamed, deleted)
    # has nothing left to keep apart: the fleet's entry may come in.
    dropped = [
        u
        for u in pinned
        if _split(u)[1] is not None and u not in raw and _split(u)[0] not in failed
    ]
    for u in dropped:
        pinned.discard(u)
        data["separate"].pop(u, None)
        data["stamps"].pop(u, None)
    data["pinned"] = sorted(pinned)
    stamps = dict(data["stamps"])
    changed = []
    cleared = []
    unresolved: Set[str] = set()
    for unit, value in raw.items():
        if _is_pinned(unit, pinned):
            continue
        try:
            h = _hash(canonical(unit, value))
        except _Unresolved:
            unresolved.add(unit)
            continue
        st = stamps.get(unit)
        if isinstance(st, dict) and not st.get("deleted") and st.get("h") == h:
            continue
        plain_unset = _split(unit)[1] is None and h in _unset_hashes(unit)
        if plain_unset and _set_here(unit, st):
            cleared.append(unit)
        if st is None and plain_unset:
            # First sight of a field that isn't set (a new field, a device
            # that never had it): nothing to spread — older than any edit,
            # and than any seeded value elsewhere.
            stamps[unit] = {"ts": _UNSET_TS, "by": me, "h": h}
        else:
            stamps[unit] = {"ts": _tick(now, st), "by": me, "h": h}
        changed.append(unit)
    for unit, st in list(stamps.items()):
        base, key = _split(unit)
        if key is None or unit in raw or _is_pinned(unit, pinned):
            continue
        if base in failed:
            continue
        if unit in _unlanded:
            continue  # never landed here: that isn't a delete
        if isinstance(st, dict) and st.get("deleted"):
            continue
        stamps[unit] = {"ts": _tick(now, st), "by": me, "h": "-", "deleted": True}
        changed.append(unit)
        cleared.append(unit)
    _unresolved.clear()
    _unresolved.update(unresolved)
    held = sum(
        1
        for u, st in data["stamps"].items()
        if not _is_pinned(u, pinned) and _split(u)[0] not in failed and _set_here(u, st)
    )
    # What a route just saved is the person's edit; the rest is unexplained.
    unexplained = [u for u in cleared if not _attributed(u, attributed)]
    if (
        not force
        and len(unexplained) >= _MASS_MIN
        and held >= _MASS_FLOOR
        and 2 * len(unexplained) >= held
    ):
        data["paused"] = {"at": now, "units": sorted(cleared)}
        _save(data)  # the pause (and dropped pins) only — no stamps
        _log_error(
            "settings sync paused: one scan would clear %v of %v settings here",
            len(cleared),
            held,
        )
        return [], {
            "cleared": len(cleared),
            "held": held,
            "detail": PAUSED + " — choose whose settings to keep in Settings "
            "→ Devices",
        }
    if force:
        data["paused"] = None
    data["stamps"] = stamps
    if changed or dropped or force:
        _save(data)
    return sorted(changed), None


def export() -> dict:
    """What another fleet device pulls: stamps + canonical values of every
    unit that isn't pinned here (tombstones included). Secrets are always
    in it — the route only answers a caller holding the fleet key or this
    device's token. Raises ``SettingsUnreadable`` while settings.json can't
    be read (the route answers 503: an empty export would read as "all
    deleted") — and, the same way, while sync is paused here (:data:`PAUSED`:
    what's here looks reset, so nobody may start from it). A unit whose
    checkout origin is unknown right now is left out."""
    from backend.config import settings as _settings

    scan_local()
    with _LOCK:
        data = _load()
        if data["paused"]:
            raise _settings.SettingsUnreadable(PAUSED)
        pinned = set(data["pinned"])
        raw, _failed = _snapshot()
        values = {}
        for u, v in raw.items():
            if _is_pinned(u, pinned):
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
            and not _is_pinned(u, pinned)
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


def _device_label(key: str) -> str:
    """A fleet device's name for a person (its host), else its key."""
    for dev in _fleet_devices():
        if dev.get("key") == key:
            return str(dev.get("host") or key)
    return key or "another device"


def _note_clocks(rstamps: Dict[str, object], ahead: Dict[str, float]) -> None:
    """Name each device that made a stamp more than :data:`_MAX_AHEAD` s
    ahead of this clock (:func:`_remote_ts`): it can't be a real time, so
    those changes are skipped. Clear the warning for every device in
    ``rstamps`` that no longer does."""
    seen = {str(rs.get("by") or "") for rs in rstamps.values() if isinstance(rs, dict)}
    for by in seen - set(ahead):
        _warnings.pop("clock:" + by, None)
    for by in ahead:
        _warnings["clock:" + by] = (
            "%s's clock is far ahead — its changes are ignored until it's fixed"
            % _device_label(by)
        )


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
        if not data["enabled"] or data["paused"]:
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
        ahead: Dict[str, float] = {}
        for unit, rs in rstamps.items():
            if not _valid_unit(unit) or not isinstance(rs, dict):
                continue
            base, key = _split(unit)
            if _is_pinned(unit, pinned) or base in failed:
                continue
            ts = _remote_ts(rs, ahead)
            if ts is None:
                continue  # not a real time (junk, years ahead): skipped
            by = str(rs.get("by") or "")
            if not _newer({"ts": ts, "by": by}, data["stamps"].get(unit)):
                _deferred.pop(unit, None)
                skipped = _unlanded.get(unit)
                if skipped is not None and not _newer(
                    {"ts": skipped[0], "by": skipped[1]}, data["stamps"].get(unit)
                ):
                    # What's here is now at least that version: moot. (An
                    # older stamp from another device says nothing about it.)
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
        _note_clocks(rstamps, ahead)
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
                # for a URL), so the next scan doesn't read the adoption — or
                # a write that only partly landed — as a local edit.
                if unit in errors:
                    # Didn't land: keep this device's OWN stamp time. Under
                    # the remote's stamp, the old (or partial) value here
                    # would be served as that version, and a device pulling
                    # here first would never take the real one.
                    prev = data["stamps"].get(unit)
                    if isinstance(prev, dict) and not prev.get("deleted"):
                        data["stamps"][unit] = {**prev, "h": h}
                    else:
                        data["stamps"][unit] = {
                            "ts": _SEED_TS,
                            "by": _self_key(),
                            "h": h,
                        }
                    _unlanded[unit] = (st["ts"], st["by"])  # not retried
                    _note_failed(unit, errors[unit])
                    missed.add(unit)
                else:
                    data["stamps"][unit] = {**st, "h": h}
                    _unlanded.pop(unit, None)
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


def _source_identity(item: object) -> Tuple[str, str, str, str]:
    """What makes two ticket sources the same source — an id is just a slug
    each device seeds on its own (every device's first Shortcut source is
    ``sc``): provider, base URL, project, and (the workspace, for Shortcut)
    a short hash of the API token, or ``""`` when there's none."""
    if not isinstance(item, dict):
        return ("", "", "", "")
    tok = str(item.get("api_token") or "").strip()
    return (
        str(item.get("provider") or "").strip().lower(),
        str(item.get("base_url") or "").strip().rstrip("/").lower(),
        str(item.get("project") or "").strip().lower(),
        hashlib.sha256(tok.encode("utf-8")).hexdigest()[:8] if tok else "",
    )


def _same_source(a: object, b: object) -> bool:
    """Whether two ticket-source entries are the same source (a token only
    counts when both have one)."""
    ia, ib = _source_identity(a), _source_identity(b)
    return ia[:3] == ib[:3] and (not (ia[3] and ib[3]) or ia[3] == ib[3])


def _separate_sources(
    rvalues: Dict[str, object], rstamps: Dict[str, object], raw: Dict[str, object]
) -> List[str]:
    """At join: this device's ticket sources the device joined has under the
    same id as a DIFFERENT source, or has deleted. Taking its entry (or its
    delete) would lose this one and its token; renaming either would change
    its ticket slugs and re-ingest every ticket it brought in. So they stay
    here, kept separate (a unit pin) until someone gives one another id."""
    out = []
    for unit, value in raw.items():
        base, key = _split(unit)
        if base != "ticketing.sources" or key is None:
            continue
        rs = rstamps.get(unit)
        if isinstance(rs, dict) and rs.get("deleted"):
            out.append(unit)
        elif unit in rvalues and not _same_source(value, rvalues[unit]):
            out.append(unit)
    return sorted(out)


def _adopt_all(body: dict, start_from: str) -> Tuple[List[str], Optional[tuple]]:
    """Join from ``start_from``: take every unit it has (it leads), keep
    entries only this device has (they spread from here), and stamp so the
    leader's values hold — except a plain field the leader has unset and
    never really edited (no stamp, or one older than any edit) while this
    device has it set: that stays, as a fresh edit here, so it spreads. A
    value the leader set back to its default on purpose wins like any other.
    A ticket source the leader has (or deleted) under the same id as a
    different one here is kept separate (:func:`_separate_sources`).
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
        raw, failed = _snapshot()
        sig_before = _pipeline_signature()
        separate = _separate_sources(rvalues, rstamps, raw)
        if separate:
            label = _device_label(start_from)
            data["pinned"] = sorted(set(data["pinned"]) | set(separate))
            for unit in separate:
                data["separate"][unit] = label
            _log_error(
                "settings sync: kept ticket sources %v separate from %v's",
                separate,
                label,
            )
        pinned = set(data["pinned"])

        def _skip(unit: str) -> bool:
            return (
                not _valid_unit(unit)
                or _is_pinned(unit, pinned)
                or _split(unit)[0] in failed
            )

        take: Dict[str, object] = {}
        drop: Set[str] = set()
        held: Set[str] = set()
        kept: Set[str] = set()
        ahead: Dict[str, float] = {}
        for unit, value in rvalues.items():
            if _skip(unit):
                continue
            rs = rstamps.get(unit)
            rts = _remote_ts(rs, ahead) if rs is not None else None
            if rs is not None and rts is None:
                held.add(unit)  # not a real time (junk, years ahead): skipped
                continue
            if (
                _split(unit)[1] is None
                and _is_unset(unit, value)
                and not _is_unset(unit, raw.get(unit))
                and (rts is None or rts <= _SEED_TS)
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
            and _remote_ts(rs, ahead) is not None
        }
        _note_clocks(rstamps, ahead)
        drop = {u for u in tombs if u in raw}
        order = {u: i for i, u in enumerate(rvalues)}
        errors = _write(take, drop, order) if (take or drop) else {}
        bad = set(errors)
        for unit, why in errors.items():
            _note_failed(unit, why)
            rts = _remote_ts(rstamps.get(unit))
            if rts is not None:
                # Not retried by the next merge until the leader has newer.
                _unlanded[unit] = (rts, str(rstamps[unit].get("by") or ""))
        after, _failed = _snapshot()
        now = time.time()
        stamps: Dict[str, dict] = {}
        for unit, value in after.items():
            if _is_pinned(unit, pinned):
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
        data.update(enabled=True, joined_from=start_from, paused=None)
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
        data["stamps"] = _lead_stamps(data, me, seed)
        data.update(enabled=True, joined_from=me, paused=None)
        _save(data)


def _lead_stamps(data: dict, me: str, seed: bool) -> Dict[str, dict]:
    """:func:`_lead_from_here`'s stamps for what's here now (raises
    ``SettingsUnreadable``): a plain field that's unset at
    :data:`_UNSET_TS`, the rest now — or, ``seed``, at :data:`_SEED_TS`."""
    now = time.time()
    pinned = set(data["pinned"])
    raw, _failed = _snapshot()
    stamps: Dict[str, dict] = {}
    for u, v in raw.items():
        if _is_pinned(u, pinned):
            continue
        try:
            h = _hash(canonical(u, v))
        except _Unresolved:
            continue  # stamped by a later scan, once git answers
        if _split(u)[1] is None and _is_unset(u, v):
            ts = _UNSET_TS
        else:
            ts = _SEED_TS if seed else now
        stamps[u] = {"ts": ts, "by": me, "h": h}
    return stamps


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
        data.update(enabled=False, joined_from="", stamps={}, paused=None)
        _save(data)
    _peers.clear()
    _deferred.clear()
    _warnings.clear()
    _unlanded.clear()
    _unresolved.clear()


def set_pinned(base: str, pinned: bool) -> List[str]:
    """Keep ``base`` (``group.field`` or ``store:<name>``, or one unit — a
    ticket source kept separate at join, ``ticketing.sources#<id>``)
    different on this device, or stop. Un-pinning restamps its current units
    as older than any real edit, so the fleet's value wins on the next pass.
    Returns the pinned list. ValueError for something that doesn't sync (or,
    un-pinning, while settings.json can't be read)."""
    from backend.config import settings as _settings

    unit_pin = _split(base)[1] is not None and _valid_unit(base)
    if base not in bases() and not unit_pin:
        raise ValueError("%s isn't something settings sync shares" % base)

    def _covered(unit: str) -> bool:
        return unit == base if unit_pin else _split(unit)[0] == base

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
            data["separate"].pop(base, None)
            me = _self_key()
            stamps = data["stamps"]
            for unit in [u for u in stamps if _covered(u)]:
                stamps.pop(unit)
            for unit, value in raw.items():
                if not _covered(unit) or _is_pinned(unit, current):
                    continue
                try:
                    h = _hash(canonical(unit, value))
                except _Unresolved:
                    continue
                stamps[unit] = {"ts": _SEED_TS, "by": me, "h": h}
        data["pinned"] = sorted(current)
        _save(data)
        for unit in [u for u in _deferred if _covered(u)]:
            _deferred.pop(unit, None)
        return list(data["pinned"])


def _reseed() -> None:
    """:func:`resume`'s "theirs": restamp what's here as older than any real
    edit and forget the rest (a tombstone here would delete it everywhere),
    so the next pass takes the fleet's values back."""
    with _LOCK:
        data = _load()
        pinned = set(data["pinned"])
        raw, _failed = _snapshot()
        me = _self_key()
        stamps: Dict[str, dict] = {}
        for unit, value in raw.items():
            if _is_pinned(unit, pinned):
                continue
            try:
                h = _hash(canonical(unit, value))
            except _Unresolved:
                continue
            stamps[unit] = {"ts": _SEED_TS, "by": me, "h": h}
        data.update(stamps=stamps, paused=None)
        _save(data)


async def resume(keep: str) -> dict:
    """Sync was paused because this device's settings look reset
    (:data:`PAUSED`). ``keep="theirs"``: take the other devices' values back
    (what's here is stamped older than any edit, then a pass runs);
    ``"mine"``: what's here is the change — stamp it (it spreads) and go on.
    Returns ``{"adopted": [...]}``. ValueError for another choice;
    LookupError while settings.json can't be read."""
    from backend.config import settings as _settings

    if keep not in RESUME_CHOICES:
        raise ValueError("keep must be one of: %s" % ", ".join(RESUME_CHOICES))
    _remember_loop()
    try:
        if keep == "theirs":
            await asyncio.to_thread(_reseed)
        else:
            changed = await asyncio.to_thread(lambda: scan_local(force=True))
            if changed:
                _spawn(nudge_peers())
    except _settings.SettingsUnreadable as err:
        _note_unreadable(err)
        raise LookupError(UNREADABLE) from err
    return {"adopted": await sync_once()}


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


def local_change(paths: Iterable[str] = ()) -> List[str]:
    """A Settings save here: stamp now (so "last edit wins" orders by when the
    user really changed it) and, when that stamped anything, nudge the other
    devices. ``paths`` are the bases the save wrote (``github``,
    ``prefs.theme``, ``ticketing.sources``, ``store:providers``): whatever it
    clears under them is the person's own edit and never pauses sync; a
    clear anywhere else (settings.json replaced or deleted before this scan)
    still counts toward the pause (``attributed``). Returns the stamped
    units; never raises."""
    try:
        changed = scan_local(attributed=paths)
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
    if await asyncio.to_thread(paused):
        return []  # settings look reset here: resume() decides
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
            why = body.get("error") if isinstance(body, dict) else ""
            if why == PAUSED:
                entry["error"] = (
                    "%s paused sync — its settings look reset; answer it in "
                    "Settings → Devices there" % label
                )
            else:
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


def _idless_sources() -> int:
    """How many ticket sources here have no id (never synced). Never raises."""
    try:
        from backend.config import settings as _settings

        return sum(
            1
            for src in _settings.load_settings().ticketing.sources
            if not str(src.id or "").strip()
        )
    except Exception:  # noqa: BLE001
        return 0


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
    if (data["enabled"] or data["v1"]) and not in_fleet:
        # Also what a v1 state file (sync on, from before devices were
        # grouped) reads as: off until this device joins the others.
        warnings.append(
            "Settings sync was on here, but this device isn't one of your devices "
            "yet — nothing is shared until you join them (Settings → Devices)."
        )
    if data["enabled"] and in_fleet and _unresolved:
        warnings.append(
            "Not shared right now (git couldn't tell which repository the folder "
            "is): %s — retried every pass."
            % ", ".join(_unit_label(u) for u in sorted(_unresolved))
        )
    if data["enabled"] and in_fleet:
        for unit in data["pinned"]:
            base, key = _split(unit)
            if base == "ticketing.sources" and key is not None:
                warnings.append(
                    "“%s” differs between %s and this device — kept separate; "
                    "give one a different id to sync it"
                    % (key, data["separate"].get(unit) or "your other device")
                )
        if _idless_sources():
            warnings.append(
                "A ticket source without an id isn't synced — give it an id "
                "(Settings → Tickets) to share it."
            )
    warnings.extend(v for _k, v in sorted(_warnings.items()))
    is_paused = bool(data["paused"]) and data["enabled"]
    return {
        "enabled": data["enabled"] and in_fleet,
        "error": "" if ok else UNREADABLE,
        "paused": PAUSED if is_paused else "",
        "choices": list(RESUME_CHOICES) if is_paused else [],
        "device": _self_key(),
        "joined_from": data["joined_from"],
        "in_fleet": in_fleet,
        "devices": devices,
        "pinned": list(data["pinned"]),
        # A pin kept separate at join → the device whose different entry
        # shares its id (by label): the Unpin confirm names it.
        "separate": dict(data["separate"]),
        "deferred": [dict(v) for _k, v in sorted(_deferred.items())],
        "warnings": warnings,
        "syncable": syncable(),
    }
