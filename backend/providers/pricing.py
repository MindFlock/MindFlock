"""Model $ pricing + context windows, sourced from the AI Pricing Guru feed.

Public endpoint: ``https://www.aipricing.guru/api/pricing.json`` — ~120 models
across a dozen providers, refreshed daily (04:00 UTC) and edge-cached 1h. We
cache the payload on disk under the assistant dir and refresh at most once/day.

Everything here is BEST-EFFORT and used only to turn raw token sums into a rough
cost estimate for the UI — never for billing. On any failure we degrade quietly:
live feed -> last good disk cache -> a small built-in table -> a Sonnet-class
default. Nothing in this module raises.

The feed gives ``inputPerM`` / ``cachedInputPerM`` (cache *read*) / ``outputPerM``
per model but no cache-*write* (cache-creation) price, so we derive cache-write as
1.25x input — Anthropic's standard 5-minute cache-write multiplier.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
import uuid
from typing import Optional

_ENDPOINT = "https://www.aipricing.guru/api/pricing.json"
_TTL = 24 * 60 * 60  # refresh the feed at most once/day
_FETCH_TIMEOUT = 4  # seconds; a miss costs this once, then we cache the outcome
_CACHE_WRITE_MULT = 1.25  # feed has no cache-write price; derive from input
_DEFAULT_WINDOW = 200_000

# Fallback table (per-million USD) keyed by a normalized model family, used when
# the feed and its disk cache are both unavailable. Mirrors the feed's shape:
# (input, cache_read, output). Cache-write is derived. Keep coarse — it only
# matters offline.
_FALLBACK = {
    "claudeopus": (5.0, 0.5, 25.0),
    "claudesonnet": (3.0, 0.3, 15.0),
    "claudehaiku": (1.0, 0.1, 5.0),
    "claudefable": (10.0, 1.0, 50.0),
}
_FALLBACK_DEFAULT = (3.0, 0.3, 15.0)  # Sonnet-class

_lock = threading.Lock()
# In-process memo: {"at": epoch, "table": {norm_id: {"in","cr","out","ctx"}}}.
_mem: dict = {"at": 0.0, "table": None}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _cache_path() -> str:
    home = os.environ.get(
        "MINDFLOCK_ASSISTANT_DIR",
        os.path.join(os.path.expanduser("~"), ".mindflock-assistant"),
    )
    return os.path.join(home, "pricing.json")


def _parse_feed(doc: dict) -> dict:
    """Feed JSON -> {normalized-id: {"in","cr","out","ctx"}} (per-million $)."""
    table: dict = {}
    for m in doc.get("models") or []:
        try:
            mid = _norm(m.get("id"))
            if not mid:
                continue
            p = m.get("pricing") or {}
            table[mid] = {
                "in": float(p.get("inputPerM", 0) or 0),
                "cr": float(p.get("cachedInputPerM", 0) or 0),
                "out": float(p.get("outputPerM", 0) or 0),
                "ctx": int(m.get("context") or 0) or None,
            }
        except (TypeError, ValueError):
            continue
    return table


def _fetch() -> Optional[dict]:
    """GET the feed and persist it to the disk cache. Returns parsed table or None."""
    try:
        req = urllib.request.Request(_ENDPOINT, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw = resp.read()
        doc = json.loads(raw)
        table = _parse_feed(doc)
        if not table:
            return None
        try:
            path = _cache_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Write-to-unique-tmp + atomic replace: a concurrent reader (or a
            # second writer in another process) never sees a half-written file.
            tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)  # don't leave orphaned tmp files behind
                except OSError:
                    pass
        except OSError:
            pass  # cache write is best-effort
        return table
    except Exception:  # noqa: BLE001 — network/JSON/anything: degrade quietly
        return None


def _load_disk() -> Optional[tuple]:
    """Return (table, mtime) from the disk cache, or None."""
    try:
        path = _cache_path()
        with open(path, "rb") as f:
            doc = json.loads(f.read())
        table = _parse_feed(doc)
        if not table:
            return None
        return table, os.path.getmtime(path)
    except Exception:  # noqa: BLE001
        return None


def _table() -> dict:
    """The current price table, refreshing from feed/disk at most once per TTL.

    Never blocks longer than one fetch timeout, and only that on a cold miss —
    the outcome (even failure) is memoized for a full TTL so hot paths are free.
    """
    now = time.time()
    with _lock:
        if _mem["table"] is not None and (now - _mem["at"]) < _TTL:
            return _mem["table"]
        # Prefer a fresh-enough disk cache before hitting the network.
        disk = _load_disk()
        if disk and (now - disk[1]) < _TTL:
            _mem["table"] = disk[0]
            _mem["at"] = now
            return disk[0]
        table = _fetch()
        if table is None:
            # Fall back to a stale disk cache if we have one, else the memo/empty.
            table = disk[0] if disk else (_mem["table"] or {})
        _mem["table"] = table
        _mem["at"] = now
        return table


def _lookup(model: str) -> Optional[dict]:
    """Best match for a transcript model id against the feed's normalized ids.

    Transcript ids use hyphens + dated suffixes (``claude-opus-4-8-20260101``);
    the feed uses dotted ids (``claude-opus-4.8``). Normalizing strips both to
    ``claudeopus48`` / ``claudeopus4820260101`` — so we match on the longest feed
    id that is a prefix of (or equal to) the normalized transcript id.
    """
    nm = _norm(model)
    if not nm:
        return None
    table = _table()
    if nm in table:
        return table[nm]
    best_key = None
    for key in table:
        if nm.startswith(key) or key.startswith(nm):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    return table[best_key] if best_key else None


def _fallback(model: str) -> tuple:
    nm = _norm(model)
    for key, val in _FALLBACK.items():
        if key in nm:
            return val
    return _FALLBACK_DEFAULT


def price_per_token(model: str) -> dict:
    """{"in","cache_write","cache_read","out"} in USD per single token."""
    hit = _lookup(model)
    # An all-zero hit means the feed listed the model but omitted its pricing
    # (``_parse_feed`` coerces missing prices to 0.0). Treat that as a miss so
    # the sane fallback table applies — otherwise a feed omission silently
    # prices every turn of that model at $0 in the cost panel.
    if hit and (hit["in"] or hit["cr"] or hit["out"]):
        p_in, p_cr, p_out = hit["in"], hit["cr"], hit["out"]
    else:
        p_in, p_cr, p_out = _fallback(model)
    return {
        "in": p_in / 1e6,
        "cache_write": p_in * _CACHE_WRITE_MULT / 1e6,
        "cache_read": p_cr / 1e6,
        "out": p_out / 1e6,
    }


def context_window(model: str) -> int:
    """The model's context-window size in tokens, or :data:`_DEFAULT_WINDOW`.

    Read from the feed's ``context`` field when known; falls back to the default
    for an unlisted model or one the feed carries with no context size.
    """
    hit = _lookup(model)
    if hit and hit.get("ctx"):
        return int(hit["ctx"])
    return _DEFAULT_WINDOW


def cost_from_price(tok: dict, price: dict) -> float:
    """USD cost for a raw token dict given a ``price_per_token`` result.

    The one place the token->dollar formula lives, so a new token class can't be
    added to one call site and silently missed by another. Tolerant of missing
    token classes: an absent or None count contributes 0.
    """
    return (
        (tok.get("in", 0) or 0) * price["in"]
        + (tok.get("out", 0) or 0) * price["out"]
        + (tok.get("cache_read", 0) or 0) * price["cache_read"]
        + (tok.get("cache_write", 0) or 0) * price["cache_write"]
    )


def estimate_cost(tok: dict, model: str) -> float:
    """Rough USD cost for a raw token dict ({"in","out","cache_read","cache_write"})."""
    return cost_from_price(tok, price_per_token(model))
