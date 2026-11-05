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
import urllib.error
import urllib.request
from typing import Optional

from . import _usage_cache
from ._timeparse import ts_epoch

_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_FETCH_TIMEOUT = 4  # seconds; only paid on a cold/expired reading
# The endpoint rate-limits per ACCOUNT: several MindFlock processes (live
# server, dev server, the ticket pipeline child) and other devices on the same
# login each polling on their own clock kept it answering 429 for good. So the
# reading is shared per machine (_usage_cache.SharedReading) and fetched at
# most once per _TTL by whichever process finds it stale.
_TTL = 60.0
_STALE_AFTER = 150.0  # older than this the UI says "as of N min ago"
_STALE_MAX = 6 * 3600.0  # past this the last good reading is dropped (estimate)
_BACKOFF_BASE = 120.0  # 429/5xx: 2 -> 4 -> 8 min ... (±20 % jitter)
_BACKOFF_CAP = 900.0  # ... up to 15 min; a Retry-After > 0 wins

_lock = threading.Lock()

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


def _window(obj) -> Optional[dict]:
    """``{percent_used, end}`` from one window object of the payload
    (``five_hour`` / ``seven_day`` / a ``limits[]`` entry), or None when it is
    null/absent. ``{}`` for a present object with nothing readable — the
    window EXISTS (a fresh one: no utilization, no reset yet)."""
    if not isinstance(obj, dict):
        return None
    w: dict = {}
    # The five-hour/seven-day objects spell it "utilization"; limits[] entries
    # have been observed with "percent" — accept either ("percent" wins).
    pct = obj.get("percent")
    if pct is None:
        pct = obj.get("utilization")
    if pct is not None:
        try:
            w["percent_used"] = float(pct)
        except (TypeError, ValueError):
            pass
    end = _iso_epoch(obj.get("resets_at"))
    if end:
        w["end"] = end
    return w


def _normalize(doc) -> Optional[dict]:
    """The endpoint's payload -> the normalized reading, or None when it
    carries no usable window at all.

    Normalized shape (``session``/``weekly`` None = the plan has no such
    window; other keys optional)::

        {"window_kind": "session" | "weekly",
         "percent_used": float, "end": epoch,      # the HEADLINE window
         "session": {"percent_used", "end"} | None,
         "weekly": {"percent_used", "end"} | None,
         "weekly_opus": {"percent_used", "end"},
         "weekly_models": [{"label", "percent_used", "end"}],  # per-model caps
         "extra": {"used": usd, "limit": usd, "currency": "USD"}}

    The headline is the 5-hour session window when the plan has one, else the
    WEEKLY window: a plan without a session limit (weekly-only, "ultra") must
    never be shown a 5-hour reset. Both payload shapes are read: the weekly cap
    as a ``limits[]`` entry with group "weekly", or as ``seven_day``.
    """
    if not isinstance(doc, dict):
        return None
    session = _window(doc.get("five_hour"))
    weekly = None
    # Weekly cap from limits[]: the account-wide row (kind "weekly_all",
    # scope null) — the live payload also carries per-model "weekly_scoped"
    # rows in the same group (observed 2026-11-05: a Fable-scoped row after the
    # all-models one), so "the first weekly entry" was right only by order.
    # Else the seven_day object.
    weekly_rows = [
        lim
        for lim in (doc.get("limits") or [])
        if isinstance(lim, dict) and str(lim.get("group")) == "weekly"
    ]
    head_row = next(
        (
            lim
            for lim in weekly_rows
            if lim.get("kind") == "weekly_all"
            or (not lim.get("scope") and lim.get("kind") != "weekly_scoped")
        ),
        weekly_rows[0] if weekly_rows else None,
    )
    if head_row is not None:
        weekly = _window(head_row)
    if not weekly:
        weekly = _window(doc.get("seven_day")) or weekly
    # Per-model weekly limits ("weekly_scoped" rows): their own meters.
    models = []
    for lim in weekly_rows:
        if lim is head_row or not lim.get("scope"):
            continue
        scope = lim.get("scope") or {}
        model = scope.get("model") if isinstance(scope, dict) else None
        label = (model or {}).get("display_name") if isinstance(model, dict) else None
        w = _window(lim)
        if label and w:
            w["label"] = str(label)
            models.append(w)
    opus = _window(doc.get("seven_day_opus"))
    out: dict = {"session": session, "weekly": weekly or None}
    if models:
        out["weekly_models"] = models
    if opus and not any("opus" in m["label"].lower() for m in models):
        out["weekly_opus"] = opus
    head = session if session is not None else (weekly or None)
    out["window_kind"] = "session" if session is not None else "weekly"
    if head:
        if head.get("percent_used") is not None:
            out["percent_used"] = head["percent_used"]
        if head.get("end"):
            out["end"] = head["end"]
    # Extra-usage credits: REAL billed dollars once the plan window is spent —
    # exactly the "only show cost when actually spending" signal. The endpoint
    # reports minor units ("decimal_places": 2 -> hundredths of a dollar), so
    # divide down to dollars: used_credits 6333 @ 2dp = $63.33.
    ex = doc.get("extra_usage") or {}
    if isinstance(ex, dict) and ex.get("is_enabled"):
        try:
            scale = 10.0 ** int(ex.get("decimal_places") or 0)
            out["extra"] = {
                "used": float(ex.get("used_credits") or 0) / scale,
                "limit": float(ex.get("monthly_limit") or 0) / scale,
                "currency": str(ex.get("currency") or "USD"),
            }
        except (TypeError, ValueError):
            pass
    if not (session or weekly or opus or models or out.get("extra")):
        return None
    return out


_shape_logged = False  # one debug line per process: which windows the plan has


def _log_shape(doc) -> None:
    """Log the payload's KEYS and which windows are null — once per process,
    at debug. Never a value beyond what the UI shows, never the token."""
    global _shape_logged
    if _shape_logged or not isinstance(doc, dict):
        return
    _shape_logged = True
    nulls = sorted(k for k, v in doc.items() if v is None)
    groups = sorted(
        str(lim.get("group"))
        for lim in (doc.get("limits") or [])
        if isinstance(lim, dict)
    )
    _logger.debug(
        "claude usage payload keys=%s null=%s limits.groups=%s",
        sorted(doc.keys()),
        nulls,
        groups,
    )


def _retry_after(headers) -> Optional[float]:
    try:
        v = headers.get("Retry-After") if headers is not None else None
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fetch_raw() -> tuple:
    """GET the usage endpoint -> ``(reading | None, status | None,
    retry_after | None)`` — the :class:`_usage_cache.SharedReading` fetch
    contract. ``status`` is the HTTP status of a failed request (None for no
    credentials / network / bad JSON), so a 429 can back off."""
    tok = _token()
    if not tok:
        return None, None, None
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
    except urllib.error.HTTPError as e:  # 401/429/5xx: the caller decides
        return None, int(getattr(e, "code", 0) or 0) or None, _retry_after(e.headers)
    except Exception:  # noqa: BLE001 — network/JSON: fall back quietly
        return None, None, None
    _log_shape(doc)
    return _normalize(doc), 200, None


def _fetch() -> Optional[dict]:
    """GET the usage endpoint -> normalized reading (see :func:`_normalize`),
    or None on any failure."""
    return _fetch_raw()[0]


def _plan_update(plan: dict, reading: dict, now: float) -> dict:
    """Fold a good reading into the remembered plan shape: when a session
    window was last seen, and the last known weekly reset — what a FALLBACK
    estimate needs to not invent 5-hour windows on a weekly-only plan."""
    plan = dict(plan or {})
    plan["checked_at"] = now
    if reading.get("session") is not None:
        plan["session_seen_at"] = now
    wk = reading.get("weekly") or {}
    if wk.get("end"):
        plan["weekly_end"] = wk["end"]
    return plan


#: A session window seen within this long means the plan has one (a single
#: between-windows payload with ``five_hour`` null must not flip a 5-hour
#: plan's fallback to weekly).
_SESSION_MEMORY = 8 * 86400.0


def plan_shape() -> Optional[dict]:
    """The plan's remembered shape from the last good reading on this machine,
    or None when no reading has ever landed::

        {"session": bool, "weekly_end": epoch | None}

    File read only — never fetches."""
    try:
        plan = _shared().plan()
    except Exception:  # noqa: BLE001
        return None
    if not plan.get("checked_at"):
        return None
    try:
        seen = float(plan.get("session_seen_at") or 0.0)
    except (TypeError, ValueError):
        seen = 0.0
    return {
        "session": bool(seen) and time.time() - seen < _SESSION_MEMORY,
        "weekly_end": plan.get("weekly_end") or None,
    }


def _shared_fetch() -> tuple:
    # Resolved at call time so tests can monkeypatch ``api._fetch_raw``.
    return _fetch_raw()


_SHARED: Optional[_usage_cache.SharedReading] = None


def _shared() -> _usage_cache.SharedReading:
    global _SHARED
    with _lock:
        if _SHARED is None:
            _SHARED = _usage_cache.SharedReading(
                "claude_usage",
                _shared_fetch,
                ttl=_TTL,
                stale_after=_STALE_AFTER,
                stale_max=_STALE_MAX,
                backoff_base=_BACKOFF_BASE,
                backoff_cap=_BACKOFF_CAP,
                plan_update=_plan_update,
            )
        return _SHARED


#: How old a reading a caller DECIDING on it (the queue's limit hold, the
#: usage-limit release) is served: the old in-memory grace. The sidebar shows
#: older ones (up to _STALE_MAX), labelled "as of …".
DECISION_MAX_AGE = 600.0


def live_usage(max_age: Optional[float] = None) -> Optional[dict]:
    """Normalized live plan usage — ONE reading per machine, shared through
    ``<run dir>/claude_usage.json`` by every MindFlock process (see
    :class:`_usage_cache.SharedReading`). Fetched at most once per ``_TTL``
    machine-wide; a 429/5xx backs off exponentially (a Retry-After > 0 wins)
    and every process honours it. Meanwhile the last good reading is served
    with ``stale: True`` and its ``fetched_at``; None only when no fetch has
    ever succeeded or the last success is older than ``max_age`` (default
    ``_STALE_MAX``)."""
    return _shared().get(max_age)
