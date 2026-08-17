"""Rolling-window token/cost totals across ALL Claude Code sessions.

Source of truth is Claude Code's transcripts (``~/.claude*/projects/*/*.jsonl``):
each assistant message's ``usage`` block is that turn's *incremental* tokens with
an ISO ``timestamp``. We make one pass over every transcript, summing entries into
rolling windows (last 24h / 7d / 30d / 365d) by timestamp, pricing each turn with
its own model via :mod:`backend.providers.pricing`.

To keep month/year windows working after Claude prunes old transcripts, we also
fold per-day totals into a durable on-disk ledger and back-fill windows from ledger
days that are no longer present in the transcripts. Results are cached ~60s.
Everything is best-effort and never raises.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from backend.providers import pricing
from backend.providers._timeparse import ts_epoch

# Rolling window sizes, in seconds.
_WINDOWS = {
    "day": 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "month": 30 * 24 * 60 * 60,
    "year": 365 * 24 * 60 * 60,
}
_CACHE_TTL = 60  # recompute the (expensive) full scan at most once/minute

# How far back the per-turn entry list (for rolling-window anchoring) reaches.
# Window chaining only needs history back to the last gap longer than the
# window span — 36h of turns is plenty for a 5h window unless someone has been
# continuously active for 36h straight (then the anchor is best-effort).
_RECENT_LOOKBACK = 36 * 60 * 60

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "windows": None, "recent": None, "accounts": None}

#: Account id the ambient ``~/.claude*`` roots fold into — every transcript
#: that does not live in an auth-profile account dir. Matches
#: :data:`backend.providers.auth_profiles.AMBIENT_ID`.
AMBIENT_ACCOUNT = "default"


def _zero() -> dict:
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def _add(dst: dict, tok: dict, cost: float) -> None:
    dst["in"] += tok["in"]
    dst["out"] += tok["out"]
    dst["cache_read"] += tok["cache_read"]
    dst["cache_write"] += tok["cache_write"]
    dst["cost"] += cost


def _ts_epoch(s):
    return ts_epoch(s)


def _roots() -> dict:
    """Every Claude Code config root, mapped to the ACCOUNT its usage belongs
    to: all ``~/.claude*`` dirs + ``$CLAUDE_CONFIG_DIR`` (the ambient login,
    :data:`AMBIENT_ACCOUNT`) plus each claude auth-profile account dir (that
    profile's id). ``{root_path: account_id}``."""
    roots: dict = {}
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots[cfg] = AMBIENT_ACCOUNT
    home = os.environ.get("HOME", "") or os.path.expanduser("~")
    if home and os.path.isdir(home):
        for name in os.listdir(home):
            if name.startswith(".claude"):
                d = os.path.join(home, name)
                if os.path.isdir(d):
                    roots[d] = AMBIENT_ACCOUNT
    try:
        from backend.providers import auth_profiles

        # Profile dirs override an ambient claim on the same path (a profile
        # pointed at ~/.claude-work must not double as "default").
        roots.update(auth_profiles.claude_account_root_map())
    except Exception:  # noqa: BLE001 — profiles are enrichment only
        pass
    return roots


def _iter_transcripts(roots):
    """Yield every ``*.jsonl`` transcript path under ``<root>/projects/*/``
    as ``(root, path)`` pairs."""
    for root in roots:
        base = os.path.join(root, "projects")
        if not os.path.isdir(base):
            continue
        try:
            projects = os.listdir(base)
        except OSError:
            continue
        for proj in projects:
            pdir = os.path.join(base, proj)
            if not os.path.isdir(pdir):
                continue
            try:
                names = os.listdir(pdir)
            except OSError:
                continue
            for fn in names:
                if fn.endswith(".jsonl"):
                    yield root, os.path.join(pdir, fn)


def _ledger_path() -> str:
    home = os.environ.get(
        "MINDFLOCK_ASSISTANT_DIR",
        os.path.join(os.path.expanduser("~"), ".mindflock-assistant"),
    )
    return os.path.join(home, "usage-history.json")


def _load_ledger() -> dict:
    try:
        with open(_ledger_path()) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("days"), dict):
            return doc
    except Exception:  # noqa: BLE001
        pass
    return {"days": {}}


def _save_ledger(doc: dict) -> None:
    try:
        path = _ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Unique tmp name per writer: the engine and the web server can save
        # concurrently, and a shared fixed ".tmp" lets their writes interleave
        # into a corrupt file that then gets renamed into place.
        tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(doc, f)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)  # don't leave orphaned tmp files behind
            except OSError:
                pass
    except OSError:
        pass  # best-effort


def _cost_for(tok: dict, model: str, memo: dict) -> float:
    p = memo.get(model)
    if p is None:
        p = pricing.price_per_token(model)
        memo[model] = p
    return pricing.cost_from_price(tok, p)


def _compute() -> tuple:
    """One full transcript scan -> (rolling-window totals, recent turn list,
    per-ACCOUNT rolling-window totals).

    The recent list is ``[(ts, cost, tok), ...]`` (ts-sorted) for turns in the
    last :data:`_RECENT_LOOKBACK` seconds — the raw material for anchoring the
    provider's active rolling usage window (:func:`current_window`). The
    account breakdown attributes each transcript to the config root it lives
    under (auth-profile account dirs vs the ambient ``~/.claude*`` login)."""
    now = time.time()
    cutoffs = {k: now - s for k, s in _WINDOWS.items()}
    acc = {k: _zero() for k in _WINDOWS}
    acc_accounts: dict = {}  # account id -> {window -> totals}
    day_totals: dict = {}  # "YYYY-MM-DD" (local) -> {..per-turn sums..}
    day_accounts: dict = {}  # account id -> {day -> totals}
    earliest_day = None
    earliest_by_account: dict = {}  # account id -> its own earliest scanned day
    price_memo: dict = {}
    recent: list = []  # (ts, cost, tok) within _RECENT_LOOKBACK, sorted below

    roots = _roots()
    for root, path in _iter_transcripts(roots):
        account = roots.get(root, AMBIENT_ACCOUNT)
        acc_a = acc_accounts.setdefault(account, {k: _zero() for k in _WINDOWS})
        days_a = day_accounts.setdefault(account, {})
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    ts = _ts_epoch(obj.get("timestamp"))
                    if ts is None:
                        continue
                    msg = obj.get("message") or {}
                    u = msg.get("usage") or obj.get("usage") or {}
                    tok = {
                        "in": int(u.get("input_tokens", 0) or 0),
                        "out": int(u.get("output_tokens", 0) or 0),
                        "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
                        "cache_write": int(
                            u.get("cache_creation_input_tokens", 0) or 0
                        ),
                    }
                    if not any(tok.values()):
                        continue
                    model = msg.get("model") or obj.get("model") or ""
                    cost = _cost_for(tok, model, price_memo)
                    if ts >= now - _RECENT_LOOKBACK:
                        recent.append((ts, cost, tok))
                    for k, cut in cutoffs.items():
                        if ts >= cut:
                            _add(acc[k], tok, cost)
                            _add(acc_a[k], tok, cost)
                    d = time.strftime("%Y-%m-%d", time.localtime(ts))
                    _add(day_totals.setdefault(d, _zero()), tok, cost)
                    _add(days_a.setdefault(d, _zero()), tok, cost)
                    if earliest_day is None or d < earliest_day:
                        earliest_day = d
                    ea = earliest_by_account.get(account)
                    if ea is None or d < ea:
                        earliest_by_account[account] = d
        except OSError:
            continue

    # Fold freshly-scanned days into the durable ledger (authoritative for any day
    # still present in the transcripts), then persist. The per-account fold rides
    # in a sibling "accounts" key; a pre-feature ledger simply has none.
    ledger = _load_ledger()
    ledger_days = ledger.get("days", {})
    for d, t in day_totals.items():
        ledger_days[d] = t
    ledger_accounts = ledger.get("accounts")
    if not isinstance(ledger_accounts, dict):
        ledger_accounts = {}
    for account, days_a in day_accounts.items():
        acct_days = ledger_accounts.setdefault(account, {})
        for d, t in days_a.items():
            acct_days[d] = t
    # Days beyond the longest rolling window can never contribute again —
    # prune them so the ledger (rewritten and re-iterated every refresh)
    # doesn't grow one entry per calendar day forever.
    horizon = time.strftime(
        "%Y-%m-%d", time.localtime(now - max(_WINDOWS.values()) - 5 * 86400)
    )
    for d in [d for d in ledger_days if d < horizon]:
        ledger_days.pop(d, None)
    for account in list(ledger_accounts):
        acct_days = ledger_accounts[account]
        if not isinstance(acct_days, dict):
            ledger_accounts.pop(account, None)
            continue
        for d in [d for d in acct_days if d < horizon]:
            acct_days.pop(d, None)
        if not acct_days:
            ledger_accounts.pop(account, None)
    ledger["days"] = ledger_days
    ledger["accounts"] = ledger_accounts
    ledger["updated"] = now
    _save_ledger(ledger)

    # Back-fill windows from ledger days the current scan didn't see (pruned
    # transcripts) — only days older than the earliest scanned day, to avoid
    # double-counting. Day granularity is fine for these tail days. The cutoff
    # day is PER TARGET: an account whose transcripts were pruned may have
    # ledger days newer than some other account's earliest scan, and gating
    # every account on the global earliest would drop those days from its own
    # rows (the "By account" split then wouldn't sum to the total).
    def _backfill(target: dict, ledgered: dict, earliest) -> None:
        for d, t in ledgered.items():
            if earliest is not None and d >= earliest:
                continue
            try:
                d_epoch = time.mktime(time.strptime(d, "%Y-%m-%d"))
            except (TypeError, ValueError):
                continue
            if not isinstance(t, dict):
                continue
            for k, cut in cutoffs.items():
                if d_epoch + 86400 >= cut:  # part of that day is in the window
                    _add(target[k], t, t.get("cost", 0.0))

    _backfill(acc, ledger_days, earliest_day)
    for account, acct_days in ledger_accounts.items():
        _backfill(
            acc_accounts.setdefault(account, {k: _zero() for k in _WINDOWS}),
            acct_days,
            earliest_by_account.get(account),
        )

    recent.sort(key=lambda e: e[0])
    return acc, recent, acc_accounts


def _refresh() -> None:
    """Recompute the scan into the cache if stale. Caller holds no lock."""
    now = time.time()
    with _lock:
        if _cache["windows"] is not None and (now - _cache["at"]) < _CACHE_TTL:
            return
        try:
            result, recent, accounts = _compute()
        except Exception:  # noqa: BLE001
            result, recent, accounts = {k: _zero() for k in _WINDOWS}, [], {}
        _cache["windows"] = result
        _cache["recent"] = recent
        _cache["accounts"] = accounts
        _cache["at"] = now


def windows() -> dict:
    """``{"day","week","month","year"}`` -> rolling totals, cached ~60s.

    Each total is ``{"in","out","cache_read","cache_write","cost"}``.
    """
    _refresh()
    with _lock:
        return _cache["windows"]


def windows_by_account() -> dict:
    """Per-account rolling totals: ``{account_id: {"day",…,"year"}}``.

    ``account_id`` is an auth-profile id, or :data:`AMBIENT_ACCOUNT` for
    everything under the ambient ``~/.claude*`` login. Only accounts with any
    recorded usage appear. Cached with the same ~60s scan as :func:`windows`.
    """
    _refresh()
    with _lock:
        return dict(_cache.get("accounts") or {})


def current_window(hours: float) -> "dict | None":
    """The ACTIVE rolling usage window, or None when idle past the window.

    Providers like Claude (subscription plans) meter usage in a rolling window
    that anchors on the first message sent after the previous window expired.
    We reproduce that anchoring from the transcript record: walk recent turns
    chronologically, starting a new window whenever a turn lands after the
    current window's end. Returns::

        {"anchor": epoch, "end": epoch, "cost": usd, "tokens": int}

    ``cost`` is the API-equivalent estimate for turns inside the window (the
    basis for a best-effort percent-used against a user-supplied budget);
    ``tokens`` is the in+out+cache total. Best-effort: the provider's true
    server-side window can differ — never present this as authoritative.
    """
    if not hours or hours <= 0:
        return None
    _refresh()
    with _lock:
        recent = list(_cache.get("recent") or ())
    if not recent:
        return None
    span = hours * 3600.0
    now = time.time()
    anchor = None
    for ts, _cost, _tok in recent:
        if anchor is None or ts >= anchor + span:
            anchor = ts
    if anchor is None or now >= anchor + span:
        return None  # no turn in-window — the next message starts a fresh one
    cost = 0.0
    tokens = 0
    for ts, c, tok in recent:
        if ts >= anchor:
            cost += c
            tokens += sum(tok.values())
    return {"anchor": anchor, "end": anchor + span, "cost": cost, "tokens": tokens}
