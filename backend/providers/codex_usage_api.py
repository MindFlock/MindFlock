"""Live plan-usage + per-session telemetry for the OpenAI Codex CLI.

Codex records everything we need on disk — no network call. Every turn the CLI
appends a ``token_count`` event to the active session's rollout file under
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``; that event carries both a
``rate_limits`` snapshot (the same numbers Codex's own status line shows) and a
cumulative ``total_token_usage`` block. We read the newest file's last snapshot
for the plan window, and match files by their ``session_meta.cwd`` for
per-session token telemetry.

Everything here is feature-detected, cached, and never raises — on any failure
(no sessions dir, unexpected shape, unreadable file) callers fall back to
"mode + note only" exactly as before this module existed. OAuth/API tokens in
``auth.json`` are never read for their value or logged; we only inspect
``auth_mode`` to decide metered-vs-plan.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from . import _usage_cache
from ._timeparse import ts_epoch as _iso_epoch

_FETCH_TIMEOUT = 4  # unused (no network) — kept for symmetry with claude_usage_api
_TTL = 20.0  # under the UI's refresh cadence, so an event-driven refresh sees
# new numbers (see claude_usage_api._TTL). Free here: no network,
# this reads a local file.
_GRACE = 600.0  # keep serving the last known-good reading through a blip

_logger = logging.getLogger(__name__)
_auth_diag_logged = False  # one-time "why is live usage dark" breadcrumb

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "good_at": 0.0, "good": None}


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".codex"


def _sessions_dir() -> Path:
    return _codex_home() / "sessions"


def _codex_homes() -> List[Path]:
    """Every Codex config root whose rollouts belong to this machine's user:
    the ambient one, plus each codex ``account`` auth profile's isolated
    ``CODEX_HOME``.

    A session pinned to such a profile writes its rollouts there and nowhere
    else, so a scan of the ambient root alone reports it as having no tokens,
    no context occupancy and no thread — the session looks dead in the UI
    while it is working. Best-effort: a settings problem degrades to the
    ambient root rather than breaking the scan.
    """
    roots = [_codex_home()]
    try:
        from . import auth_profiles

        for d in auth_profiles.codex_account_root_map():
            p = Path(d)
            if p not in roots:
                roots.append(p)
    except Exception:  # noqa: BLE001 — profiles are enrichment only
        pass
    return roots


def _sessions_dirs(ambient_only: bool = False) -> List:
    """Every ``<root>/sessions`` to scan. The ambient one always comes from
    :func:`_sessions_dir`, which stays the single seam the tests patch."""
    dirs = [_sessions_dir()]
    if ambient_only:
        return dirs
    ambient_home = _codex_home()
    for home in _codex_homes():
        if home != ambient_home:
            dirs.append(home / "sessions")
    return dirs


def _iter_rollouts_newest_first(
    limit: int = 200, ambient_only: bool = False
) -> List[Path]:
    """Rollout jsonl paths, newest mtime first (bounded), across every config
    root — or only the ambient one when ``ambient_only``.

    ``ambient_only`` exists for the plan-usage meter: a rate-limit snapshot
    describes ONE subscription, so merging accounts there would report
    whichever identity happened to write last. Per-session and rolling-total
    scans want every root, because those are about this machine's work.
    """
    roots = _sessions_dirs(ambient_only)
    files: List[Path] = []
    for root in roots:
        try:
            files.extend(p for p in root.rglob("rollout-*.jsonl") if p.is_file())
        except Exception:  # noqa: BLE001 — no sessions dir / permission: no data
            continue

    def _mtime(p: Path) -> float:
        # A file can vanish between the glob and the stat (Codex pruning a
        # session mid-scan); treat it as oldest so one vanishing file doesn't
        # abandon the whole sort and return the list unordered.
        try:
            return p.stat().st_mtime
        except Exception:  # noqa: BLE001
            return 0.0

    files.sort(key=_mtime, reverse=True)
    return files[:limit]


def _read_jsonl(path: Path) -> Iterable[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:  # noqa: BLE001 — skip a torn/partial line
                    continue
    except Exception:  # noqa: BLE001 — unreadable file: yield nothing
        return


def _rate_limits_of(doc: dict) -> Optional[dict]:
    """Extract a ``rate_limits`` block from a rollout entry, or None."""
    if not isinstance(doc, dict):
        return None
    if doc.get("type") != "event_msg":
        return None
    payload = doc.get("payload") or {}
    if payload.get("type") != "token_count":
        return None
    rl = payload.get("rate_limits")
    return rl if isinstance(rl, dict) else None


def _window_from(entry: Optional[dict]) -> Optional[dict]:
    """``{"percent_used", "end"}`` from a primary/secondary rate-limit entry."""
    if not isinstance(entry, dict):
        return None
    out: dict = {}
    up = entry.get("used_percent")
    if up is not None:
        try:
            out["percent_used"] = float(up)
        except (TypeError, ValueError):
            pass
    # ``resets_at`` is absolute epoch seconds; older builds used
    # ``resets_in_seconds`` (relative) — support both.
    ra = entry.get("resets_at")
    if ra is not None:
        try:
            out["end"] = float(ra)
        except (TypeError, ValueError):
            pass
    if "end" not in out and entry.get("resets_in_seconds") is not None:
        try:
            out["end"] = time.time() + float(entry["resets_in_seconds"])
        except (TypeError, ValueError):
            pass
    return out or None


def _normalize_rate_limits(rl: dict) -> Optional[dict]:
    """Codex ``rate_limits`` -> the shared usage_live() shape.

    ``{"percent_used", "end", "weekly": {"percent_used","end"}, "plan"}`` — the
    primary window is the headline, the secondary window (when present) is the
    weekly/monthly cap. All keys optional; returns None if nothing usable.
    """
    out: dict = {}
    primary = _window_from(rl.get("primary"))
    if primary:
        out.update(primary)
    secondary = _window_from(rl.get("secondary"))
    if secondary:
        out["weekly"] = secondary
    plan = rl.get("plan_type")
    if plan:
        out["plan"] = str(plan)
    return out or None


def _fetch() -> Optional[dict]:
    """Newest rollout file's last rate-limit snapshot, normalized, or None."""
    for path in _iter_rollouts_newest_first(limit=8, ambient_only=True):
        last_rl = None
        for doc in _read_jsonl(path):
            rl = _rate_limits_of(doc)
            if rl:
                last_rl = rl
        if last_rl:
            norm = _normalize_rate_limits(last_rl)
            if norm:
                return norm
    return None


def live_usage() -> Optional[dict]:
    """Normalized live plan usage, cached ~60s with a grace window (mirrors
    claude_usage_api.live_usage so a single unreadable-file blip doesn't blank
    the plan-usage % in the UI). Returns None when Codex has recorded no
    rate-limit snapshot yet."""
    return _usage_cache.serve(_cache, _lock, _TTL, _GRACE, _fetch)


def usage_mode() -> Optional[str]:
    """``"windowed"`` on a ChatGPT plan (auth_mode == chatgpt), ``"metered"``
    when running on an ``OPENAI_API_KEY``, or None when auth is unknown (let the
    caller fall back to its window-kind default)."""
    try:
        with open(_codex_home() / "auth.json", "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:  # noqa: BLE001 — no/corrupt auth: unknown
        global _auth_diag_logged
        if not _auth_diag_logged:
            _auth_diag_logged = True
            # One-time breadcrumb saying WHY auth is unknown — never key values.
            _logger.debug(
                "codex live usage unavailable: cannot read auth.json (%s)",
                type(e).__name__,
            )
        return None
    mode = (doc.get("auth_mode") or "").strip().lower()
    if mode == "chatgpt":
        return "windowed"
    if mode in ("apikey", "api_key", "api-key"):
        return "metered"
    # No explicit auth_mode: an API key present means metered.
    if doc.get("OPENAI_API_KEY"):
        return "metered"
    return None


# --------------------------------------------------------------------------- #
# Per-session token telemetry (parity with Claude's transcript scan).
# --------------------------------------------------------------------------- #
def _meta_of(path: Path) -> Tuple[Optional[str], Optional[float]]:
    """``(cwd, start_epoch)`` from a rollout file's ``session_meta`` (its first
    line), or ``(None, None)``. Cheap: reads only until the meta line."""
    for doc in _read_jsonl(path):
        if not isinstance(doc, dict):
            continue
        if doc.get("type") == "session_meta":
            payload = doc.get("payload") or {}
            cwd = payload.get("cwd")
            ts = _iso_epoch(payload.get("timestamp") or doc.get("timestamp"))
            return (cwd, ts)
        # session_meta is always first; bail after the first typed record.
        if doc.get("type"):
            return (None, None)
    return (None, None)


def find_thread_id(workdir: str, since_ts: Optional[float], exclude=frozenset()) -> str:
    """The session id of the newest rollout for ``workdir`` started at/after
    ``since_ts`` (the window's current tmux launch), skipping ids other windows
    have already claimed — so each MindFlock window binds to ITS OWN codex
    conversation. Returns ``""`` when nothing matches. Never raises."""
    try:
        workdir = os.path.realpath(workdir or "")
        for path in _iter_rollouts_newest_first(limit=60):
            cwd, start = _meta_of(path)
            if not cwd or os.path.realpath(cwd) != workdir:
                continue
            # 5s slack: the rollout is stamped moments before tmux reports the
            # pane's creation time.
            if since_ts is not None and (start is None or start < since_ts - 5.0):
                continue
            for doc in _read_jsonl(path):
                if isinstance(doc, dict) and doc.get("type") == "session_meta":
                    sid = str((doc.get("payload") or {}).get("id") or "")
                    if sid and sid not in exclude:
                        return sid
                    break
                if isinstance(doc, dict) and doc.get("type"):
                    break
    except Exception:  # noqa: BLE001 — discovery is enrichment only
        pass
    return ""


def _session_totals(path: Path) -> Optional[dict]:
    """Cumulative token usage + last-turn context + model from one rollout.

    Returns ``{"in","out","cache_read","cache_write","ctx","ctx_window","model"}``
    or None if the file carried no ``token_count`` event. ``in`` is REAL
    (uncached) input to match Claude's figure — Codex reports ``input_tokens``
    inclusive of the cached subset, so we subtract ``cached_input_tokens``.
    """
    total = None  # cumulative across the session
    last = None  # most-recent turn (for context occupancy)
    ctx_window = 0
    model = ""
    for doc in _read_jsonl(path):
        if not isinstance(doc, dict):
            continue
        t = doc.get("type")
        if t == "turn_context":
            m = (doc.get("payload") or {}).get("model")
            if m:
                model = str(m)
        elif t == "event_msg":
            payload = doc.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            tot = info.get("total_token_usage")
            if isinstance(tot, dict):
                total = tot
            lt = info.get("last_token_usage")
            if isinstance(lt, dict):
                last = lt
            cw = info.get("model_context_window")
            if cw:
                try:
                    ctx_window = int(cw)
                except (TypeError, ValueError):
                    pass
    if total is None:
        return None

    def _i(d, k):
        try:
            return int(d.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    cached = _i(total, "cached_input_tokens")
    raw_in = _i(total, "input_tokens")
    real_in = max(0, raw_in - cached)
    # Context occupancy = the last turn's full prompt (input incl. cached).
    ctx = _i(last, "input_tokens") if last else 0
    return {
        "in": real_in,
        "out": _i(total, "output_tokens"),
        "cache_read": cached,
        "cache_write": 0,  # Codex doesn't report cache creation separately
        "ctx": ctx,
        "ctx_window": ctx_window,
        "model": model,
    }


# --------------------------------------------------------------------------- #
# Rolling-window period totals (parity with usage_history.windows()).
# --------------------------------------------------------------------------- #
_WINDOWS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
_WIN_TTL = 60.0
_win_lock = threading.Lock()
_win_cache: dict = {"at": 0.0, "windows": None}


def _zero_period() -> dict:
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def _add_period(dst: dict, tok: dict, cost: float) -> None:
    dst["in"] += tok["in"]
    dst["out"] += tok["out"]
    dst["cache_read"] += tok["cache_read"]
    dst["cache_write"] += tok["cache_write"]
    dst["cost"] += cost


def _compute_windows() -> dict:
    """One scan of every rollout -> ``{day,week,month,year}`` rolling totals.

    ``total_token_usage`` is cumulative per session, so we diff consecutive
    ``token_count`` events within a file to recover each turn's incremental
    tokens, attribute them to the event's timestamp, price them by the turn's
    model, and bucket into the rolling windows. Best-effort (bounded to the
    newest rollouts; Codex keeps them under ~/.codex/sessions and doesn't prune
    aggressively, so this covers ordinary history)."""
    from . import pricing

    now = time.time()
    cutoffs = {k: now - s for k, s in _WINDOWS.items()}
    acc = {k: _zero_period() for k in _WINDOWS}
    price_memo: dict = {}

    def _i(d, k):
        try:
            return int((d or {}).get(k) or 0)
        except (TypeError, ValueError):
            return 0

    for path in _iter_rollouts_newest_first(limit=2000):
        model = ""
        prev = {"in": 0, "cached": 0, "out": 0}
        for doc in _read_jsonl(path):
            if not isinstance(doc, dict):
                continue
            t = doc.get("type")
            if t == "turn_context":
                m = (doc.get("payload") or {}).get("model")
                if m:
                    model = str(m)
            elif t == "event_msg":
                payload = doc.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                tot = (payload.get("info") or {}).get("total_token_usage")
                if not isinstance(tot, dict):
                    continue
                ts = _iso_epoch(doc.get("timestamp"))
                cur = {
                    "in": _i(tot, "input_tokens"),
                    "cached": _i(tot, "cached_input_tokens"),
                    "out": _i(tot, "output_tokens"),
                }
                # Per-turn delta from the cumulative counters (clamp at 0 in case
                # a session's counters ever reset).
                d_in_raw = max(0, cur["in"] - prev["in"])
                d_cached = max(0, cur["cached"] - prev["cached"])
                d_out = max(0, cur["out"] - prev["out"])
                prev = cur
                tok = {
                    "in": max(0, d_in_raw - d_cached),
                    "out": d_out,
                    "cache_read": d_cached,
                    "cache_write": 0,
                }
                if ts is None or not any(tok.values()):
                    continue
                p = price_memo.get(model)
                if p is None:
                    p = pricing.price_per_token(model)
                    price_memo[model] = p
                # tok always carries cache_write=0, so cost_from_price's extra
                # cache_write term contributes nothing — same number as before.
                cost = pricing.cost_from_price(tok, p)
                for k, cut in cutoffs.items():
                    if ts >= cut:
                        _add_period(acc[k], tok, cost)
    return acc


def windows() -> dict:
    """``{day,week,month,year}`` rolling Codex token+cost totals, cached ~60s.
    Each total is ``{in,out,cache_read,cache_write,cost}`` (shape matches
    usage_history.windows() so the two can be summed for a combined view)."""
    now = time.time()
    with _win_lock:
        if _win_cache["windows"] is not None and (now - _win_cache["at"]) < _WIN_TTL:
            return _win_cache["windows"]
    try:
        result = _compute_windows()
    except Exception:  # noqa: BLE001 — history is optional
        result = {k: _zero_period() for k in _WINDOWS}
    with _win_lock:
        _win_cache["windows"] = result
        _win_cache["at"] = now
    return result


def session_usage(
    cwd: str,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
) -> Optional[dict]:
    """Summed token telemetry for Codex sessions started in ``cwd``.

    Mirrors Claude's per-session scan: match rollout files whose
    ``session_meta.cwd`` equals ``cwd`` and whose start falls in
    ``[since_ts, until_ts)`` (either bound may be None = open-ended), then sum
    their cumulative totals. Context occupancy + window + model come from the
    NEWEST matching session (the live one). Returns None when nothing matches so
    the caller can fall back to zeros.
    """
    if not cwd:
        return None
    try:
        target = os.path.realpath(cwd)
    except Exception:  # noqa: BLE001
        target = cwd
    agg = {
        "in": 0,
        "out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "ctx": 0,
        "ctx_window": 0,
        "model": "",
    }
    matched = False
    newest_start = None
    for path in _iter_rollouts_newest_first(limit=400):
        mcwd, start = _meta_of(path)
        if not mcwd:
            continue
        try:
            same = os.path.realpath(mcwd) == target
        except Exception:  # noqa: BLE001
            same = mcwd == cwd
        if not same:
            continue
        # ~2s slack: the meta timestamp can slightly precede the instance's
        # own creation stamp (process spawn vs first rollout write). A file whose
        # start timestamp is unparseable (``start is None``) can't be proven to
        # belong to a bounded window, so it is EXCLUDED whenever a bound is set —
        # matching find_thread_id. Otherwise such a file leaks into EVERY
        # sibling session's totals (it passes both guards for all of them),
        # inflating each session's cost with a conversation that isn't theirs.
        if since_ts is not None and (start is None or start < since_ts - 2.0):
            continue
        if until_ts is not None and (start is None or start >= until_ts):
            continue
        totals = _session_totals(path)
        if not totals:
            continue
        matched = True
        agg["in"] += totals["in"]
        agg["out"] += totals["out"]
        agg["cache_read"] += totals["cache_read"]
        agg["cache_write"] += totals["cache_write"]
        # Newest matching file wins for context/window/model (files come newest
        # first, so the first match is the live session).
        if newest_start is None or (start or 0) >= newest_start:
            newest_start = start or 0
            agg["ctx"] = totals["ctx"]
            agg["ctx_window"] = totals["ctx_window"]
            agg["model"] = totals["model"]
    return agg if matched else None
