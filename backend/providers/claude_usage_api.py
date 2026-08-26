"""Live plan-usage from Anthropic's OAuth usage endpoint (best-effort).

Claude Code's ``/usage`` screen reads this same endpoint with the user's OAuth
token (``~/.claude/.credentials.json``). We reuse it READ-ONLY to show the real
window utilization + reset time instead of a transcript-derived estimate —
the estimate's window-chaining drifts (it can only see 36h of history), while
this is the provider's own meter.

Undocumented API: everything here is feature-detected, cached, and never
raises — on any failure (no creds, expired token, network, shape change)
callers fall back to the transcript estimate. The token is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Optional

from . import _usage_cache
from ._timeparse import ts_epoch

_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_FETCH_TIMEOUT = 4  # seconds; only paid on a cold/expired cache
_TTL = 20.0  # see _usage_cache.serve; bounds how stale a UI refresh can be
# (was 60s, "matching" the pill's poll — which meant a
# refresh triggered by a finished turn could still be
# answered from a cache filled a minute earlier, i.e. the
# event-driven refresh bought nothing. Kept well UNDER the
# poll cadence for that reason. Still at most one upstream
# fetch per _TTL no matter how many callers arrive.)
_GRACE = 600.0  # keep serving the last known-good reading through transient
# fetch failures for up to this long, so a single blip (401,
# timeout, network hiccup, or a between-windows payload) does
# not blank the plan-usage % in the UI. Still only one fetch
# per _TTL — no retry storm.

_lock = threading.Lock()
# ``good``/``good_at`` hold the last SUCCESSFUL reading; ``at`` is when we last
# attempted a fetch (throttles retries to _TTL whether or not it succeeded).
_cache: dict = {"at": 0.0, "good_at": 0.0, "good": None}

_logger = logging.getLogger(__name__)
_creds_diag_logged = False  # one-time "why is live usage dark" breadcrumb


def _creds_diag(reason: str) -> None:
    """Log the credential-gate failure once, at debug level. Never the token."""
    global _creds_diag_logged
    if not _creds_diag_logged:
        _creds_diag_logged = True
        _logger.debug("claude live usage unavailable: %s", reason)


#: Keychain service name Claude Code stores its credentials under on macOS.
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_TIMEOUT = 5  # seconds; a denied/prompting lookup must not wedge /api/usage


def _keychain_doc() -> Optional[dict]:
    """The credentials document from the macOS login Keychain, or None.

    On macOS Claude Code keeps its OAuth credentials in the Keychain rather than
    in ``~/.claude/.credentials.json``, so the file read below finds nothing and
    live plan usage was permanently dark on every Mac — the UI fell back to the
    transcript estimate, which yields a reset countdown but no percentage unless
    a window budget is configured. The stored blob is the same JSON as the file.

    ``security`` may raise a one-time "MindFlock wants to use your Keychain"
    prompt, since the item was created by a different binary; a denial (or a
    headless session, where no prompt can be answered) just returns None and
    behaviour is exactly as before. Timeout-bounded so an unanswered prompt
    can't hold the usage request open.
    """
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_KEYCHAIN_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 — no `security`, timeout, denial
        _creds_diag("keychain lookup failed (%s)" % type(e).__name__)
        return None
    if proc.returncode != 0 or not proc.stdout:
        _creds_diag("keychain has no %s item" % _KEYCHAIN_SERVICE)
        return None
    try:
        doc = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        _creds_diag("keychain item is not JSON")
        return None
    return doc if isinstance(doc, dict) else None


def _token() -> Optional[str]:
    """The Claude Code OAuth access token, or None. Never logged.

    Read from ``.credentials.json`` (Linux/WSL, and any install that keeps a
    file) with the macOS Keychain as the fallback — see :func:`_keychain_doc`.
    """
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    doc: Optional[dict] = None
    try:
        with open(os.path.join(root, ".credentials.json")) as f:
            doc = json.load(f)
    except Exception as e:  # noqa: BLE001 — missing/corrupt creds: try the keychain
        _creds_diag("cannot read .credentials.json (%s)" % type(e).__name__)
    if not isinstance(doc, dict):
        doc = _keychain_doc()
    if not isinstance(doc, dict):
        return None
    tok = (doc.get("claudeAiOauth") or {}).get("accessToken") or None
    if not tok:
        _creds_diag("credentials carry no OAuth access token")
    return tok


def _iso_epoch(s) -> Optional[float]:
    return ts_epoch(s)


def _fetch() -> Optional[dict]:
    """GET the usage endpoint -> normalized dict, or None on any failure.

    Normalized shape (all keys optional)::

        {"percent_used": float, "end": epoch,
         "weekly": {"percent_used": float, "end": epoch},
         "extra": {"used": usd, "limit": usd, "currency": "USD"}}
    """
    tok = _token()
    if not tok:
        return None
    req = urllib.request.Request(
        _ENDPOINT,
        headers={
            "Authorization": "Bearer " + tok,
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as r:
            doc = json.loads(r.read())
    except Exception:  # noqa: BLE001 — 401/network/JSON: fall back quietly
        return None
    out: dict = {}
    fh = doc.get("five_hour") or {}
    if fh.get("utilization") is not None:
        try:
            out["percent_used"] = float(fh["utilization"])
        except (TypeError, ValueError):
            pass
    end = _iso_epoch(fh.get("resets_at"))
    if end:
        out["end"] = end
    # Weekly cap: the first "weekly" entry in limits[] (the endpoint may also
    # carry per-model weekly scopes; the group row is the headline one).
    for lim in doc.get("limits") or []:
        if str(lim.get("group")) == "weekly":
            wk: dict = {}
            # The five-hour window spells this "utilization"; limits[] entries
            # have been observed with "percent" — accept either spelling.
            pct = lim.get("percent")
            if pct is None:
                pct = lim.get("utilization")
            try:
                wk["percent_used"] = float(pct or 0)
            except (TypeError, ValueError):
                pass
            wend = _iso_epoch(lim.get("resets_at"))
            if wend:
                wk["end"] = wend
            if wk:
                out["weekly"] = wk
            break
    # Extra-usage credits: REAL billed dollars once the plan window is spent —
    # exactly the "only show cost when actually spending" signal. The endpoint
    # reports minor units ("decimal_places": 2 -> hundredths of a dollar), so
    # divide down to dollars: used_credits 6333 @ 2dp = $63.33.
    ex = doc.get("extra_usage") or {}
    if ex.get("is_enabled"):
        try:
            scale = 10.0 ** int(ex.get("decimal_places") or 0)
            out["extra"] = {
                "used": float(ex.get("used_credits") or 0) / scale,
                "limit": float(ex.get("monthly_limit") or 0) / scale,
                "currency": str(ex.get("currency") or "USD"),
            }
        except (TypeError, ValueError):
            pass
    return out or None


def live_usage() -> Optional[dict]:
    """Normalized live plan usage, cached ~60s. Through a transient fetch
    failure the last known-good reading is reused for up to _GRACE seconds so
    the plan-usage % doesn't flicker in and out; returns None only when a fetch
    has never succeeded or the last success is older than _GRACE."""
    return _usage_cache.serve(_cache, _lock, _TTL, _GRACE, _fetch)
