"""Share settings across the user's devices — two-way, last edit wins.

Every MindFlock server keeps its own ``settings.json``. With sync on, the
devices paired for remote control (:mod:`backend.web.core.remote`) keep the
SHAREABLE part of it identical: ticket sources, GitHub repos and options,
notifications, agent limits, the accent colour, trusted Tailscale accounts…
Machine-specific fields (paths, bind mode, the access token, the IDE, the
local model, peer links, signed-in accounts) never leave the machine —
:data:`SYNCED` vs :data:`LOCAL`, and a test fails until every new field is
put in one of them.

**Last edit wins, per field.** Each shared field carries a stamp
``{ts, by, h}`` (when it last changed, on which device, a hash of its value)
in ``settings_sync.json`` beside ``settings.json``. A change is noticed by
the hash — a Settings save calls :func:`scan_local` at once, and the sync
loop rescans every :data:`INTERVAL` s, which also catches an edit made by
hand or by another process. Every :data:`INTERVAL` s each device pulls every
other sync-enabled device's export and adopts any field whose stamp is newer
(device key breaks an exact tie), keeping the remote stamp — so all devices
converge on the same value and the same stamp.

**Joining.** :func:`enable` either starts from THIS device (its values are
stamped now and spread from it) or from another device: that device's
shareable values are adopted wholesale, under its stamps (or, if it isn't
syncing yet, stamps older than any real edit, so its first sync wins over
nothing). That is how the user's older machine leads at first.

**Secrets travel only with a token.** An export includes the secret-bearing
fields (:data:`SECRET_PATHS`) only when the caller presented this device's
access token — a device whose gate is off still never hands its GitHub token
to an anonymous tailnet caller. A device paired without a token gets the rest,
and the withheld fields are named so the UI can say why.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backend import log

#: Seconds between sync passes (rescan local, pull every device).
INTERVAL = 30.0

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

#: Shared fields that carry a credential (ticket sources hold API tokens).
SECRET_PATHS = frozenset(
    {"github.token", "notifications.ntfy_token", "ticketing.sources"}
)

#: The stamp a joined device gives values it took from a device that wasn't
#: syncing yet: older than any real edit, so that device's first pass wins.
_SEED_TS = 1.0

_LOCK = threading.RLock()
_peers: Dict[str, dict] = {}  # device key -> last pass result (memory only)


def paths() -> List[str]:
    return ["%s.%s" % (g, f) for g, fields in SYNCED.items() for f in fields]


def _state_path() -> Path:
    from backend.config import settings as _settings

    return _settings.settings_path().parent / "settings_sync.json"


def _load() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    stamps = data.get("stamps")
    data["stamps"] = stamps if isinstance(stamps, dict) else {}
    data["enabled"] = bool(data.get("enabled"))
    data["joined_from"] = str(data.get("joined_from") or "")
    return data


def _save(data: dict) -> None:
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


def _self_key() -> str:
    from backend.web.core import remote as _remote

    return _remote.self_identity()["key"]


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _current_values() -> Dict[str, object]:
    """Every shared field's stored value (``None`` = unset / default)."""
    from backend.config import settings as _settings

    _settings.invalidate()  # another process may have written the file
    doc = _settings.load_settings().to_dict()
    out: Dict[str, object] = {}
    for group, fields in SYNCED.items():
        g = doc.get(group) if isinstance(doc.get(group), dict) else {}
        for f in fields:
            out["%s.%s" % (group, f)] = g.get(f)
    return out


def _apply(values: Dict[str, object]) -> None:
    """Write shared fields in one save (``None`` clears to the default)."""
    from backend.config import settings as _settings

    _settings.invalidate()
    merged = _settings.load_settings().to_dict()
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


def _newer(a: Optional[dict], b: Optional[dict]) -> bool:
    """Whether stamp ``a`` is strictly newer than ``b``."""
    if not a:
        return False
    if not b:
        return True
    return (float(a.get("ts") or 0), str(a.get("by") or "")) > (
        float(b.get("ts") or 0),
        str(b.get("by") or ""),
    )


# --------------------------------------------------------------------------- #
# local side
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return _load()["enabled"]


def scan_local(now: Optional[float] = None) -> List[str]:
    """Stamp every shared field whose value changed since its stamp (a save
    here, a hand edit, another process). Returns the changed paths."""
    with _LOCK:
        data = _load()
        if not data["enabled"]:
            return []
        now = time.time() if now is None else now
        me = _self_key()
        changed = []
        for path, value in _current_values().items():
            h = _hash(value)
            st = data["stamps"].get(path)
            if not isinstance(st, dict) or st.get("h") != h:
                data["stamps"][path] = {"ts": now, "by": me, "h": h}
                changed.append(path)
        if changed:
            _save(data)
        return changed


def export(include_secrets: bool) -> dict:
    """What another device pulls: stamps + values of every shared field.
    Without ``include_secrets`` the credential-bearing fields are left out
    and named in ``withheld``."""
    scan_local()
    data = _load()
    values = _current_values()
    withheld = []
    if not include_secrets:
        for p in SECRET_PATHS:
            values.pop(p, None)
            withheld.append(p)
    return {
        "device": _self_key(),
        "enabled": data["enabled"],
        "stamps": {p: data["stamps"][p] for p in values if p in data["stamps"]},
        "values": values,
        "withheld": sorted(withheld),
    }


def merge(remote: dict) -> List[str]:
    """Adopt every field ``remote`` (an :func:`export`) has a newer stamp
    for. Returns the adopted paths."""
    if not isinstance(remote, dict) or not remote.get("enabled"):
        return []
    rstamps = remote.get("stamps") if isinstance(remote.get("stamps"), dict) else {}
    rvalues = remote.get("values") if isinstance(remote.get("values"), dict) else {}
    with _LOCK:
        scan_local()  # a local edit not yet stamped must not lose to an older remote
        data = _load()
        if not data["enabled"]:
            return []
        take: Dict[str, object] = {}
        stamps: Dict[str, dict] = {}
        for path in paths():
            rs = rstamps.get(path)
            if path not in rvalues or not isinstance(rs, dict):
                continue
            if not _newer(rs, data["stamps"].get(path)):
                continue
            stamps[path] = {
                "ts": float(rs.get("ts") or 0),
                "by": str(rs.get("by") or ""),
            }
            if _hash(rvalues[path]) != (data["stamps"].get(path) or {}).get("h"):
                take[path] = rvalues[path]
        if not stamps:
            return []
        if take:
            _apply(take)
        current = _current_values()
        for path, st in stamps.items():
            # The hash of what was STORED (normalisation can reshape a value),
            # so the next scan doesn't read the adoption as a local edit.
            data["stamps"][path] = {**st, "h": _hash(current.get(path))}
        _save(data)
        return sorted(take)


async def enable(start_from: str) -> dict:
    """Turn sync on. ``start_from`` is ``""``/this device (its values lead) or
    a connected device's key (its values are adopted first)."""
    from backend.web.core import remote as _remote

    me = _self_key()
    if not start_from or start_from == me:
        with _LOCK:
            data = _load()
            now = time.time()
            data.update(enabled=True, joined_from=me)
            data["stamps"] = {
                p: {"ts": now, "by": me, "h": _hash(v)}
                for p, v in _current_values().items()
            }
            _save(data)
        return {"enabled": True, "adopted": [], "withheld": []}

    dev = next((d for d in _remote.connected_devices() if d["key"] == start_from), None)
    if dev is None:
        raise LookupError(
            "%s isn't connected — check remote control on it" % start_from
        )
    status, body = await _remote.get_json(
        dev, "/api/settings/sync/export", timeout=10.0
    )
    if status != 200 or not isinstance(body, dict):
        raise LookupError(
            "couldn't read settings from %s (%s)"
            % (dev.get("host") or start_from, status or "unreachable")
        )
    rvalues = body.get("values") if isinstance(body.get("values"), dict) else {}
    rstamps = body.get("stamps") if isinstance(body.get("stamps"), dict) else {}
    take = {p: rvalues[p] for p in paths() if p in rvalues}
    with _LOCK:
        if take:
            _apply(take)
        current = _current_values()
        data = _load()
        data.update(enabled=True, joined_from=start_from)
        stamps = {}
        for p in paths():
            rs = rstamps.get(p) if body.get("enabled") else None
            if p in take and isinstance(rs, dict):
                ts, by = float(rs.get("ts") or _SEED_TS), str(
                    rs.get("by") or start_from
                )
            else:
                ts, by = _SEED_TS, start_from
            stamps[p] = {"ts": ts, "by": by, "h": _hash(current.get(p))}
        data["stamps"] = stamps
        _save(data)
    return {
        "enabled": True,
        "adopted": sorted(take),
        "withheld": [p for p in body.get("withheld") or [] if isinstance(p, str)],
    }


def disable() -> None:
    with _LOCK:
        data = _load()
        data.update(enabled=False, joined_from="", stamps={})
        _save(data)
    _peers.clear()


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
async def sync_once() -> List[str]:
    """One pass: rescan, then pull every connected device. Returns what was
    adopted."""
    from backend.web.core import remote as _remote

    if not enabled():
        return []
    await asyncio.to_thread(scan_local)
    adopted: List[str] = []
    for dev in _remote.connected_devices():
        status, body = await _remote.get_json(
            dev, "/api/settings/sync/export", timeout=10.0
        )
        entry = {
            "label": dev.get("host") or dev["key"],
            "at": time.time(),
            "ok": status == 200 and isinstance(body, dict),
            "enabled": bool(isinstance(body, dict) and body.get("enabled")),
            "withheld": [],
            "error": "",
        }
        if status == 404:
            entry["error"] = "that device's MindFlock is too old to sync settings"
        elif not entry["ok"]:
            entry["error"] = "unreachable" if not status else "HTTP %s" % status
        else:
            entry["withheld"] = [
                p for p in body.get("withheld") or [] if isinstance(p, str)
            ]
            got = await asyncio.to_thread(merge, body)
            if got:
                entry["adopted"] = got
                adopted.extend(got)
                if log.InfoLog is not None:
                    log.InfoLog.Printf(
                        "settings sync: took %s from %s", ", ".join(got), entry["label"]
                    )
        _peers[dev["key"]] = {**(_peers.get(dev["key"]) or {}), **entry}
    return adopted


async def sync_loop() -> None:
    while True:
        try:
            await sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — the loop must never die
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("settings sync failed: %v", err)
        await asyncio.sleep(INTERVAL)


def status() -> dict:
    """Settings → Security's sync section."""
    from backend.web.core import remote as _remote

    data = _load()
    devices = []
    for dev in _remote.connected_devices():
        p = _peers.get(dev["key"]) or {}
        devices.append(
            {
                "key": dev["key"],
                "label": dev.get("host") or dev["key"],
                "syncing": bool(p.get("enabled")),
                "last_sync": p.get("at") if p.get("ok") else None,
                "withheld": p.get("withheld") or [],
                "error": p.get("error") or "",
            }
        )
    return {
        "enabled": data["enabled"],
        "device": _self_key(),
        "joined_from": data["joined_from"],
        "devices": devices,
    }
