"""Scheduled window-refresh keepalive (roadmap E).

Some agent CLIs (Claude, Codex) meter usage on a *rolling* window that anchors
on your first message and then resets a fixed span later (Claude: 5 hours). If
you send that first message at an awkward time, your window resets at an awkward
time. This feature sends a tiny 1-token message to a dedicated, connection-free
session for a provider every N hours, so the window anchors on a schedule you
choose — and stays warm across quiet stretches.

This module owns only the *config + scheduling arithmetic* (pure, testable):
what's enabled, how often, which providers, and when each is next due. The
actual "spin a minimal session and send the ping" lives in the web server
(``_fire_window_refresh``), which has the tmux/launch machinery.

State persists in ``<config dir>/window_refresh.json`` (``$MINDFLOCK_WINDOW_REFRESH_FILE``
overrides, for tests). ``last_fired`` is a per-provider epoch map.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from typing import List, Optional, Tuple

from backend.config.config import GetConfigDir

_FileName = "window_refresh.json"
_LOCK = threading.Lock()

#: Default cadence when enabled without an explicit interval. 5h matches the
#: Claude/Codex rolling window (see providers' usage_window()).
DEFAULT_INTERVAL_HOURS = 5.0


def config_path() -> str:
    env = os.environ.get("MINDFLOCK_WINDOW_REFRESH_FILE")
    if env:
        return env
    return os.path.join(GetConfigDir(), _FileName)


def _load() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".wr.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(s) -> Optional[Tuple[int, int]]:
    """``"HH:MM"`` (24h) -> ``(hour, minute)``, or None if empty/invalid."""
    m = _HHMM_RE.match(str(s or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _today_anchor_epoch(now: float, hh: int, mm: int) -> float:
    """Local-time epoch for ``hh:mm`` on the same calendar day as ``now``."""
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))


def _normalize(raw: dict) -> dict:
    enabled = bool(raw.get("enabled", False))
    try:
        interval = float(raw.get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_HOURS
    interval = max(0.25, min(interval, 168.0))  # 15 min .. 1 week
    # Optional daily anchor time ("HH:MM" local). When set it takes precedence
    # over the interval: the ping fires once a day at this time so a fresh usage
    # window begins then (e.g. the start of your work day). "" = interval mode.
    anchor_time = ""
    if parse_hhmm(raw.get("anchor_time")):
        anchor_time = str(raw.get("anchor_time")).strip()
    providers = raw.get("providers", [])
    if not isinstance(providers, list):
        providers = []
    providers = [str(p) for p in providers if str(p).strip()]
    last = raw.get("last_fired", {})
    if not isinstance(last, dict):
        last = {}
    last = {str(k): float(v) for k, v in last.items() if _is_num(v)}
    return {
        "enabled": enabled,
        "interval_hours": interval,
        "anchor_time": anchor_time,
        "providers": providers,
        "last_fired": last,
    }


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def get_config() -> dict:
    with _LOCK:
        return _normalize(_load())


def set_config(
    enabled: Optional[bool] = None,
    interval_hours: Optional[float] = None,
    anchor_time: Optional[str] = None,
    providers: Optional[List[str]] = None,
) -> dict:
    """Patch and persist the config (only the fields passed). Returns the new one.

    ``anchor_time`` is an ``"HH:MM"`` local time (or ``""`` to clear it and fall
    back to interval mode); an invalid value is ignored."""
    with _LOCK:
        cur = _normalize(_load())
        if enabled is not None:
            cur["enabled"] = bool(enabled)
        if interval_hours is not None:
            try:
                cur["interval_hours"] = max(0.25, min(float(interval_hours), 168.0))
            except (TypeError, ValueError):
                pass
        if anchor_time is not None:
            s = str(anchor_time).strip()
            if s == "" or parse_hhmm(s):
                cur["anchor_time"] = s
        if providers is not None:
            cur["providers"] = [str(p) for p in providers if str(p).strip()]
        _save(cur)
        return cur


def record_fired(program: str, now: float) -> None:
    with _LOCK:
        cur = _normalize(_load())
        cur["last_fired"][str(program)] = float(now)
        _save(cur)


def due_providers(now: float, cfg: Optional[dict] = None) -> List[str]:
    """Providers whose next refresh is due at ``now`` (empty when disabled).

    Daily-anchor mode (``anchor_time`` set): a provider is due once ``now`` has
    reached today's anchor time and it hasn't fired since. Interval mode: due
    when ``interval_hours`` have elapsed since its last fire."""
    cfg = cfg or get_config()
    if not cfg["enabled"] or not cfg["providers"]:
        return []
    hhmm = parse_hhmm(cfg.get("anchor_time"))
    out = []
    if hhmm:
        anchor = _today_anchor_epoch(now, hhmm[0], hhmm[1])
        if now < anchor:
            return []  # today's anchor hasn't arrived yet
        for p in cfg["providers"]:
            if cfg["last_fired"].get(p, 0.0) < anchor:  # not fired since today's anchor
                out.append(p)
        return out
    span = cfg["interval_hours"] * 3600.0
    for p in cfg["providers"]:
        last = cfg["last_fired"].get(p, 0.0)
        if now - last >= span:
            out.append(p)
    return out


def next_fire_at(
    program: str, cfg: Optional[dict] = None, now: Optional[float] = None
) -> Optional[float]:
    """Epoch of the next scheduled refresh for ``program`` (None if disabled)."""
    cfg = cfg or get_config()
    if not cfg["enabled"] or program not in cfg["providers"]:
        return None
    hhmm = parse_hhmm(cfg.get("anchor_time"))
    if hhmm:
        now = time.time() if now is None else now
        anchor = _today_anchor_epoch(now, hhmm[0], hhmm[1])
        last = cfg["last_fired"].get(program, 0.0)
        # If today's anchor is still ahead and we haven't fired for it, that's
        # next; otherwise it fires at tomorrow's anchor.
        if now < anchor and last < anchor:
            return anchor
        return anchor + 86400.0
    last = cfg["last_fired"].get(program, 0.0)
    return last + cfg["interval_hours"] * 3600.0
