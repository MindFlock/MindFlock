"""The stores outside settings.json that settings sync carries, entry by entry.

Registered with :func:`settings_sync.register_store` at import (settings sync
imports this module the first time it needs its stores):

- ``templates`` — session templates (``addons/templates.py``), keyed by
  lower-cased name. A template's ``repo_path`` is a local checkout, so it
  travels as that checkout's origin URL and lands as this machine's checkout
  of the same repo (or the URL, when there's none).
- ``red_zones`` — each repo's red zones, Plan-first flag and companion
  patterns, keyed by repo id (``host/owner/repo``). Only repos identified by
  a forge origin: a ``path:`` id names a folder on one machine. The
  ``worktrees`` section (waivers, green zones, session fences) is per
  checkout and never syncs. Zone ids are per device, so the synced value
  carries patterns, not ids.
- ``providers`` — custom agent providers: the TOML text of each user file in
  the providers dir, keyed by provider name. A TOML's ``binary_path`` can
  still differ per device: ``coding_cli.binary_paths`` (LOCAL) overrides it.

Every ``list_fn`` RAISES when its store exists but can't be read: an empty
answer would read as "every entry was deleted" and tombstone them on every
device.

Writes go through each store's own API (never a raw rewrite of its file), so
the store's normalisation, locking and side effects — red-zone tamper
detection, live guard resync — still hold. A write that can't fully land
RAISES (after landing what it could) so sync reports it and doesn't retry it
every pass.

Known gap (red zones): the red-zone store is read from its file, so an edit
made behind MindFlock's back — what ``red_zone_monitor`` alerts on as
tampering, on the device where it happened — is stamped and spreads like a
Settings edit, and the receiving devices write it through ``route_write()``
without an alert of their own. Telling a route write from a tamper needs the
monitor to keep the last route-written snapshot; until it does, the alert on
the originating device is the signal, and re-adding the zones in Settings
spreads the fix back out.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from typing import Dict

from backend import log
from backend.web.core import settings_sync

#: A provider name (same rule as the Settings → Agent providers CRUD).
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _log_error(fmt: str, *args) -> None:
    if log.ErrorLog is not None:
        log.ErrorLog.Printf(fmt, *args)


def _read_json_strict(path: str) -> object:
    """The file's JSON, ``None`` when it doesn't exist; raises when it exists
    but can't be read or parsed (see the module docstring)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def _templates():
    from backend.web.addons import templates

    return templates


def templates_list() -> Dict[str, dict]:
    data = _read_json_strict(_templates().templates_path())
    items = data.get("templates") if isinstance(data, dict) else None
    out: Dict[str, dict] = {}
    for tpl in items if isinstance(items, list) else []:
        if not isinstance(tpl, dict):
            continue
        key = str(tpl.get("name") or "").strip().lower()
        if key and key not in out:
            out[key] = dict(tpl)
    return out


def templates_canon(value: object) -> object:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if out.get("repo_path"):
        out["repo_path"] = settings_sync.canonical_url(out["repo_path"])
    return out


def templates_localize(incoming: object, current: object) -> object:
    if not isinstance(incoming, dict):
        return incoming
    out = dict(incoming)
    if out.get("repo_path"):
        cur = current.get("repo_path") if isinstance(current, dict) else ""
        out["repo_path"] = settings_sync.localize_url(out["repo_path"], cur or "")
    return out


def templates_write(key: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("a template must be an object")
    if str(value.get("name") or "").strip().lower() != key:
        raise ValueError("template name doesn't match its key %r" % key)
    _templates().save_template(value)


def templates_delete(key: str) -> None:
    _templates().delete_template(key)


# --------------------------------------------------------------------------- #
# red zones
# --------------------------------------------------------------------------- #
def _zone_tuple(z: dict):
    return (
        str(z.get("pattern") or ""),
        str(z.get("name") or ""),
        str(z.get("note") or ""),
    )


def _repo_value(repo: dict, rid: str) -> dict:
    zones = sorted(
        (
            {"pattern": p, "name": n, "note": t}
            for p, n, t in (
                _zone_tuple(z)
                for z in (repo.get("zones") or [])
                if isinstance(z, dict) and z.get("kind", "red") == "red"
            )
            if p
        ),
        key=lambda z: (z["pattern"], z["name"], z["note"]),
    )
    return {
        "label": str(repo.get("label") or rid),
        "zones": zones,
        "plan_first": bool(repo.get("plan_first")),
        "companions": [
            str(p) for p in (repo.get("companions") or []) if isinstance(p, str) and p
        ],
    }


def red_zones_list() -> Dict[str, dict]:
    from backend.config import red_zones

    data = _read_json_strict(red_zones.store_path())
    repos = data.get("repos") if isinstance(data, dict) else None
    out: Dict[str, dict] = {}
    for rid, repo in (repos if isinstance(repos, dict) else {}).items():
        if not isinstance(rid, str) or not rid or rid.startswith("path:"):
            continue
        if not isinstance(repo, dict):
            continue
        value = _repo_value(repo, rid)
        # An entry with nothing in it is what a delete leaves behind (the
        # store has no "drop a repo" call) — it is "absent", not a value.
        if value["zones"] or value["plan_first"] or value["companions"]:
            out[rid] = value
    return out


def _resync_repo(repo_id: str) -> None:
    """Re-sync the guards of every live worktree of ``repo_id`` NOW, like the
    red-zone routes do after a write."""
    from backend.web.core import red_zone_monitor

    srv = sys.modules.get("backend.web.server")
    roots = []
    if srv is not None and hasattr(srv, "_live_repo_roots"):
        try:
            roots = srv._live_repo_roots(repo_id)
        except Exception as err:  # noqa: BLE001
            _log_error("settings sync: live roots for %s: %v", repo_id, err)
    if roots:
        red_zone_monitor.resync(roots, repo_id)


def red_zones_write(key: str, value: object) -> None:
    from backend.config import red_zones
    from backend.web.core import red_zone_monitor

    if not isinstance(value, dict) or key.startswith("path:"):
        raise ValueError("not a portable red-zone entry")
    label = str(value.get("label") or "")
    want = [
        _zone_tuple(z)
        for z in (value.get("zones") or [])
        if isinstance(z, dict) and z.get("pattern")
    ]
    refused = []
    with red_zone_monitor.route_write():
        current = red_zones.all_repos().get(key) or {}
        have = set()
        for z in current.get("zones") or []:
            if _zone_tuple(z) in want:
                have.add(_zone_tuple(z))
            else:
                red_zones.remove_zone(z.get("id") or "")
        for pattern, name, note in want:
            if (pattern, name, note) in have:
                continue
            try:
                red_zones.add_zone(
                    "repo", key, pattern, name=name, note=note, label=label
                )
            except red_zones.ZoneConflict as err:
                # The pattern is a GREEN zone in a worktree here — a real local
                # conflict the user resolves; the rest of the entry still lands.
                _log_error("settings sync: red zone %s skipped: %v", pattern, err)
                refused.append(pattern)
        companions = [
            str(p) for p in (value.get("companions") or []) if isinstance(p, str)
        ]
        red_zones.set_companions(key, companions, label=label)
        red_zones.set_plan_first(key, bool(value.get("plan_first")), label=label)
    _resync_repo(key)
    if refused:
        raise ValueError(
            "%s %s a green zone in a worktree here"
            % (
                ", ".join(refused),
                "is" if len(refused) == 1 else "are",
            )
        )


def red_zones_delete(key: str) -> None:
    from backend.config import red_zones
    from backend.web.core import red_zone_monitor

    with red_zone_monitor.route_write():
        current = red_zones.all_repos().get(key) or {}
        for z in current.get("zones") or []:
            red_zones.remove_zone(z.get("id") or "")
        if current:
            red_zones.set_companions(key, [])
            red_zones.set_plan_first(key, False)
    _resync_repo(key)


# --------------------------------------------------------------------------- #
# custom providers
# --------------------------------------------------------------------------- #
def _providers_dir():
    from backend.providers import config as provider_config

    return provider_config._providers_dir()


def providers_list() -> Dict[str, str]:
    d = _providers_dir()
    if not d.is_dir():
        return {}
    out: Dict[str, str] = {}
    for f in sorted(d.glob("*.toml")):
        if _PROVIDER_NAME_RE.match(f.stem):
            out[f.stem] = f.read_text(encoding="utf-8")
    return out


def providers_write(key: str, value: object) -> None:
    import tomllib

    if not _PROVIDER_NAME_RE.match(key or ""):
        raise ValueError("bad provider name %r" % key)
    if not isinstance(value, str):
        raise ValueError("a provider must be TOML text")
    tomllib.loads(value)  # refuse a file the registry would skip anyway
    d = _providers_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".prov.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(tmp, d / ("%s.toml" % key))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def providers_delete(key: str) -> None:
    if not _PROVIDER_NAME_RE.match(key or ""):
        raise ValueError("bad provider name %r" % key)
    try:
        (_providers_dir() / ("%s.toml" % key)).unlink()
    except FileNotFoundError:
        pass


settings_sync.register_store(
    "templates",
    templates_list,
    templates_write,
    templates_delete,
    canon_fn=templates_canon,
    localize_fn=templates_localize,
    label="Session templates",
)
settings_sync.register_store(
    "red_zones",
    red_zones_list,
    red_zones_write,
    red_zones_delete,
    label="Red zones",
)
settings_sync.register_store(
    "providers",
    providers_list,
    providers_write,
    providers_delete,
    label="Custom agent providers",
)
