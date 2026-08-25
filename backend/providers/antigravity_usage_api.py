"""Live plan-quota for the Google Antigravity CLI (``agy``).

agy does not persist quota to disk — its "Usage & Quota" screen asks the CLI's
embedded language server, which proxies cloudcode's
``retrieveUserQuotaSummary`` RPC (the cloud endpoint itself rejects callers
other than the CLI). That local server speaks ConnectRPC over plain HTTP on a
random localhost port and logs the port at startup, so we discover the port
from the newest log of a *live* agy process and call
``/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary``
ourselves (``{}`` request, JSON response). Localhost only; no auth token is
read or sent.

The response is per model GROUP (Gemini models / Claude+GPT models), each with
a weekly bucket carrying ``remainingFraction`` + ``resetTime``. We normalize to
the shared usage_live() shape: the most-used group is the headline
(``percent_used``/``end``) and every group rides along in ``groups`` for the
per-group strip in the UI.

Everything here is feature-detected, cached, and never raises — with no live
agy process (or any failure) callers fall back to "mode + note only".
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from . import _usage_cache
from ._timeparse import ts_epoch

_FETCH_TIMEOUT = 3  # seconds per port probe; localhost, so effectively instant
_TTL = 20.0  # under the UI's refresh cadence, so an event-driven refresh sees
# new numbers (see claude_usage_api._TTL). Cheap here: the probe
# is localhost.
_GRACE = 3600.0  # weekly quotas move slowly — keep the last good reading
# through an agy restart so the panel doesn't blank out

_RPC_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
# glog line stamped by the CLI at startup, e.g.
#   I0708 10:07:20.073070 49252 server.go:527] Language server listening on random port at 46031 for HTTP
_LISTEN_RE = re.compile(
    r"^[A-Z]\d{4} [\d:.]+\s+(\d+)\s+.*Language server listening on \w+ port at (\d+) for HTTP\s*$"
)

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "good_at": 0.0, "good": None}


def _log_dir() -> Path:
    """agy's log dir (same state root as antigravity.py's conversation store)."""
    env = os.environ.get("ANTIGRAVITY_CLI_DIR")
    base = (
        Path(env)
        if env
        else Path(os.path.expanduser("~")) / ".gemini" / "antigravity-cli"
    )
    return base / "log"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001 — EPERM etc.: it exists
        return True


def _live_ports(limit: int = 8) -> List[int]:
    """HTTP ports of language servers whose CLI process is still alive,
    newest log first (bounded). Never raises."""
    try:
        logs = sorted(
            _log_dir().glob("cli-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
    except Exception:  # noqa: BLE001 — no log dir: no live server
        return []
    ports: List[int] = []
    for log in logs:
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 200:  # the listen line is stamped at startup
                        break
                    m = _LISTEN_RE.match(line.strip())
                    if m and _pid_alive(int(m.group(1))):
                        port = int(m.group(2))
                        if port not in ports:
                            ports.append(port)
                        break
        except Exception:  # noqa: BLE001 — unreadable log: try the next
            continue
    return ports


def _iso_epoch(s) -> Optional[float]:
    return ts_epoch(s)


def _bucket_window(group: dict) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """``(percent_used, end)`` from a group's first quota bucket, or None.

    ``remainingFraction`` is the *remaining* share (the TUI's bar shows it
    directly); absent means unknown — never assume exhausted."""
    buckets = group.get("buckets")
    if not isinstance(buckets, list) or not buckets:
        return None
    b = buckets[0]
    if not isinstance(b, dict):
        return None
    pct = None
    rf = b.get("remainingFraction")
    if rf is not None:
        try:
            pct = max(0.0, min(100.0, round((1.0 - float(rf)) * 100.0, 1)))
        except (TypeError, ValueError):
            pct = None
    end = _iso_epoch(b.get("resetTime"))
    if pct is None and end is None:
        return None
    return (pct, end)


def _normalize(doc: dict) -> Optional[dict]:
    """RetrieveUserQuotaSummary JSON -> the shared usage_live() shape.

    ``{"percent_used", "end", "groups": [{"label","percent_used","end"}, …]}``
    — headline is the most-used group (the binding constraint), so the pill's
    "N% left · resets …" tracks whichever quota you're actually burning.
    """
    resp = doc.get("response") if isinstance(doc.get("response"), dict) else doc
    groups_in = resp.get("groups")
    if not isinstance(groups_in, list):
        return None
    groups: List[dict] = []
    for g in groups_in:
        if not isinstance(g, dict):
            continue
        win = _bucket_window(g)
        if win is None:
            continue
        pct, end = win
        entry: dict = {"label": str(g.get("displayName") or "Quota")}
        if pct is not None:
            entry["percent_used"] = pct
        if end is not None:
            entry["end"] = end
        groups.append(entry)
    if not groups:
        return None
    head = max(groups, key=lambda e: e.get("percent_used") or 0.0)
    out: dict = {"groups": groups}
    if head.get("percent_used") is not None:
        out["percent_used"] = head["percent_used"]
    if head.get("end") is not None:
        out["end"] = head["end"]
    return out


def _fetch() -> Optional[dict]:
    """Ask the first responsive live language server for the quota summary."""
    for port in _live_ports():
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, _RPC_PATH),
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as r:
                doc = json.loads(r.read())
        except Exception:  # noqa: BLE001 — stale port / server busy: next
            continue
        norm = _normalize(doc) if isinstance(doc, dict) else None
        if norm:
            return norm
    return None


def live_usage() -> Optional[dict]:
    """Normalized live plan quota, cached ~60s with a grace window (mirrors
    codex_usage_api.live_usage). Returns None when no agy session is (or was
    recently) running to ask."""
    return _usage_cache.serve(_cache, _lock, _TTL, _GRACE, _fetch)
