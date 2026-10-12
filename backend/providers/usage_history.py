"""Rolling-window token/cost totals across ALL Claude Code sessions.

Source of truth is Claude Code's transcripts (``~/.claude*/projects/**/*.jsonl``
— the conversation files AND the Agent-tool subagent transcripts nested under
``<project>/<conversation-id>/subagents/``): each assistant API response's
``usage`` block is that turn's *incremental* tokens with an ISO ``timestamp``.

Claude Code writes ONE API response as several lines (one per content block:
thinking / text / tool_use), each repeating the same ``message.usage``, and a
resumed or forked conversation copies earlier lines into a new file. Summing
every line double-counted (2.25x on real data), so turns are deduplicated by
``(message.id, requestId)`` across every file before they are summed — see
:func:`read_turns`. Each turn is priced with its own model via
:mod:`backend.providers.pricing`.

To keep month/year windows working after Claude prunes old transcripts, per-day
totals are folded into a durable on-disk ledger (``version`` 2). A ledger day
never shrinks because files vanished: it keeps whichever of (scanned, ledgered)
costs more, and every window is back-filled with ``ledger - scanned`` for each
day it covers. Days carried over from a version-1 ledger (computed before the
dedupe/subagent fixes, and no longer recomputable) are listed in
``approx_days`` so the UI can say the oldest part of the year is an estimate.
Results are cached ~60s. Everything is best-effort and never raises.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from typing import Optional

from backend.fsutil import atomic_write_json
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

#: On-disk ledger format. Version 1 (no ``version`` key) summed duplicate lines
#: and missed subagent transcripts; see :func:`_migrate_v1`.
LEDGER_VERSION = 2

_TOKEN_FIELDS = ("in", "out", "cache_read", "cache_write")

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


# --------------------------------------------------------------------------- #
# One transcript file -> its unique API turns (shared with providers/claude.py)
# --------------------------------------------------------------------------- #

#: Per-file parse memo: path -> ((mtime_ns, size), (turns, first_ts)).
#: Transcripts are append-only and the full scan revisits ~5k files every
#: minute; an unchanged file is never re-read. Entries for files the full scan
#: no longer sees are dropped after each scan (:func:`_compute`).
_FILE_MEMO: dict = {}
_FILE_MEMO_MAX = 50000


def _parse_turns(path: str) -> Optional[tuple]:
    """Parse one ``.jsonl`` transcript into ``(turns, first_ts)``.

    ``turns`` is a list of ``(key, ts, in, out, cache_read, cache_write,
    model)`` — ONE per API response: the lines Claude Code writes per content
    block share ``(message.id, requestId)`` and are collapsed here, keeping the
    copy with the most output tokens (the final block). ``key`` is
    message id + requestId (NUL-joined), or a hash of the raw line when the
    message has no id. Lines without a usage block are ignored. ``first_ts`` is
    the earliest timestamp among usage lines (the conversation's birth).
    Returns ``None`` when the file can't be opened.
    """
    turns: dict = {}
    first_ts = None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    msg = {}
                u = msg.get("usage") or obj.get("usage")
                if not isinstance(u, dict):
                    continue  # "usage" only appeared inside content / a tool result
                ts = _ts_epoch(obj.get("timestamp"))
                if ts is not None and (first_ts is None or ts < first_ts):
                    first_ts = ts
                mid = msg.get("id")
                if mid:
                    key = "%s\x00%s" % (mid, obj.get("requestId") or "")
                else:
                    key = "\x01%x" % (hash(line.strip()) & 0xFFFFFFFFFFFFFFFF)
                try:
                    turn = (
                        key,
                        ts,
                        int(u.get("input_tokens", 0) or 0),
                        int(u.get("output_tokens", 0) or 0),
                        int(u.get("cache_read_input_tokens", 0) or 0),
                        int(u.get("cache_creation_input_tokens", 0) or 0),
                        sys.intern(str(msg.get("model") or obj.get("model") or "")),
                    )
                except (TypeError, ValueError):
                    continue
                prev = turns.get(key)
                if prev is None or turn[3] > prev[3]:
                    turns[key] = turn
    except OSError:
        return None
    return list(turns.values()), first_ts


def read_turns(path: str) -> Optional[tuple]:
    """:func:`_parse_turns`, memoized on the file's (mtime, size). Never raises."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _FILE_MEMO.get(path)
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        parsed = _parse_turns(path)
    except Exception:  # noqa: BLE001 — a bad file is skipped, never raised
        return None
    if parsed is None:
        return None
    if len(_FILE_MEMO) >= _FILE_MEMO_MAX:
        # Drop the oldest-inserted half — simple, rare, keeps the dict bounded.
        for k in list(_FILE_MEMO)[: _FILE_MEMO_MAX // 2]:
            _FILE_MEMO.pop(k, None)
    _FILE_MEMO[path] = (sig, parsed)
    return parsed


def better_turn(new: tuple, old: Optional[tuple]) -> bool:
    """Whether ``new`` should replace ``old`` for the same turn key: the copy
    with more output tokens is the final content block of that response."""
    return old is None or new[3] > old[3]


def iter_jsonl(top: str):
    """Every ``*.jsonl`` under ``top``, recursively (subagent transcripts live
    at ``<project>/<conversation>/subagents/[workflows/<id>/]agent-*.jsonl``).
    Symlinked dirs are not followed. Never raises."""
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.name.endswith(".jsonl"):
                    yield e.path
            except OSError:
                continue


# --------------------------------------------------------------------------- #
# Roots, ledger
# --------------------------------------------------------------------------- #


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
    """Yield every ``*.jsonl`` transcript under ``<root>/projects/<proj>/``
    (recursively — subagent transcripts included) as ``(root, path)`` pairs."""
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
            for path in iter_jsonl(pdir):
                yield root, path


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
        # Unique tmp name per writer: the engine and the web server can save
        # concurrently, and a shared fixed ".tmp" lets their writes interleave
        # into a corrupt file that then gets renamed into place.
        atomic_write_json(
            path,
            doc,
            indent=None,
            newline=False,
            ensure_ascii=True,
            prefix=os.path.basename(path) + ".",
        )
    except OSError:
        pass  # best-effort


def _migrate_v1(ledger: dict, earliest, earliest_by_account: dict) -> None:
    """Upgrade a version-1 ledger in place.

    v1 day totals summed duplicate lines and missed subagent transcripts, so a
    v1 day that the current scan still covers (>= the earliest scanned day —
    per account for the account split) is DROPPED and recomputed. Older v1 days
    are kept as the best available figure (their transcripts are gone) and
    listed in ``approx_days``. The v1 file is backed up once beside the ledger
    as ``usage-history.v1.json`` before it is rewritten.
    """
    path = _ledger_path()
    backup = os.path.join(os.path.dirname(path), "usage-history.v1.json")
    try:
        if os.path.isfile(path) and not os.path.exists(backup):
            shutil.copy2(path, backup)
    except OSError:
        pass
    approx = set()
    days = ledger.get("days")
    if not isinstance(days, dict):
        days = {}
    for d in list(days):
        if earliest is not None and d >= earliest:
            days.pop(d, None)
        else:
            approx.add(d)
    ledger["days"] = days
    accounts = ledger.get("accounts")
    if isinstance(accounts, dict):
        for account, acct_days in list(accounts.items()):
            if not isinstance(acct_days, dict):
                accounts.pop(account, None)
                continue
            cut = earliest_by_account.get(account)
            for d in list(acct_days):
                if cut is not None and d >= cut:
                    acct_days.pop(d, None)
                else:
                    approx.add(d)
    ledger["approx_days"] = sorted(approx)
    ledger["version"] = LEDGER_VERSION


def _merge_days(ledgered: dict, scanned: dict, approx: Optional[set] = None) -> None:
    """Fold scanned day totals into ``ledgered``: per day, keep whichever of
    the two costs more (all fields from that one source), so a day never
    shrinks because Claude pruned some of its transcripts. A day the scan wins
    is no longer an estimate (dropped from ``approx``)."""
    for d, t in scanned.items():
        old = ledgered.get(d)
        try:
            old_cost = (
                float(old.get("cost", 0.0) or 0.0) if isinstance(old, dict) else None
            )
        except (TypeError, ValueError):
            old_cost = None
        if old_cost is None or old_cost <= t["cost"]:
            ledgered[d] = dict(t)
            if approx is not None:
                approx.discard(d)


def _ledger_delta(t, s: Optional[dict]) -> Optional[dict]:
    """``ledger - scanned`` for one day, per field and floored at 0 (None when
    nothing is missing from the scan)."""
    if not isinstance(t, dict):
        return None
    s = s or {}
    out = {}
    try:
        for f in _TOKEN_FIELDS:
            out[f] = max(0, int(t.get(f, 0) or 0) - int(s.get(f, 0) or 0))
        cost = max(
            0.0, float(t.get("cost", 0.0) or 0.0) - float(s.get("cost", 0.0) or 0.0)
        )
    except (TypeError, ValueError):
        return None
    if cost <= 0 and not any(out.values()):
        return None
    out["cost"] = cost
    return out


def _cost_for(tok: dict, model: str, memo: dict) -> float:
    p = memo.get(model)
    if p is None:
        p = pricing.price_per_token(model)
        memo[model] = p
    return pricing.cost_from_price(tok, p)


def _compute() -> tuple:
    """One full transcript scan -> (rolling-window totals, recent turn list,
    per-ACCOUNT rolling-window totals, ``estimated_before`` date or None).

    The recent list is ``[(ts, cost, tok, account), ...]`` (ts-sorted) for turns in the
    last :data:`_RECENT_LOOKBACK` seconds — the raw material for anchoring the
    provider's active rolling usage window (:func:`current_window`). The
    account breakdown attributes each transcript to the config root it lives
    under (auth-profile account dirs vs the ambient ``~/.claude*`` login)."""
    now = time.time()
    cutoffs = {k: now - s for k, s in _WINDOWS.items()}

    # Pass 1: every unique API turn across every file (a turn copied into a
    # resumed/forked conversation's file is the same key — counted once).
    roots = _roots()
    seen: dict = {}  # key -> (turn, account)
    visited = set()
    for root, path in _iter_transcripts(roots):
        visited.add(path)
        parsed = read_turns(path)
        if parsed is None:
            continue
        account = roots.get(root, AMBIENT_ACCOUNT)
        for t in parsed[0]:
            if t[1] is None or not (t[2] or t[3] or t[4] or t[5]):
                continue
            prev = seen.get(t[0])
            if better_turn(t, prev[0] if prev else None):
                seen[t[0]] = (t, account)
    for p in [p for p in list(_FILE_MEMO) if p not in visited]:
        _FILE_MEMO.pop(p, None)

    # Pass 2: fold the unique turns into windows + per-day totals.
    acc = {k: _zero() for k in _WINDOWS}
    acc_accounts: dict = {}  # account id -> {window -> totals}
    day_totals: dict = {}  # "YYYY-MM-DD" (local) -> {..per-turn sums..}
    day_accounts: dict = {}  # account id -> {day -> totals}
    earliest_day = None
    earliest_by_account: dict = {}  # account id -> its own earliest scanned day
    price_memo: dict = {}
    recent: list = []  # (ts, cost, tok, account) in _RECENT_LOOKBACK, sorted below
    for t, account in seen.values():
        _key, ts, t_in, t_out, t_cr, t_cw, model = t
        tok = {"in": t_in, "out": t_out, "cache_read": t_cr, "cache_write": t_cw}
        cost = _cost_for(tok, model, price_memo)
        acc_a = acc_accounts.setdefault(account, {k: _zero() for k in _WINDOWS})
        days_a = day_accounts.setdefault(account, {})
        if ts >= now - _RECENT_LOOKBACK:
            recent.append((ts, cost, tok, account))
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

    # Fold the scanned days into the durable ledger, then persist. The
    # per-account fold rides in a sibling "accounts" key.
    ledger = _load_ledger()
    persist = True
    if ledger.get("version") != LEDGER_VERSION:
        if earliest_day is None and (ledger.get("days") or ledger.get("accounts")):
            # An empty scan can't tell which v1 days are recomputable; leave
            # the v1 file alone until a scan sees transcripts again.
            persist = False
        else:
            _migrate_v1(ledger, earliest_day, earliest_by_account)
    ledger_days = ledger.get("days", {})
    approx = ledger.get("approx_days")
    approx = set(approx) if isinstance(approx, list) else set()
    _merge_days(ledger_days, day_totals, approx)
    ledger_accounts = ledger.get("accounts")
    if not isinstance(ledger_accounts, dict):
        ledger_accounts = {}
    for account, days_a in day_accounts.items():
        acct_days = ledger_accounts.get(account)
        if not isinstance(acct_days, dict):
            acct_days = ledger_accounts[account] = {}
        _merge_days(acct_days, days_a)
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
    approx = {d for d in approx if d >= horizon}
    ledger["days"] = ledger_days
    ledger["accounts"] = ledger_accounts
    if persist:
        ledger["approx_days"] = sorted(approx)
        ledger["updated"] = now
        _save_ledger(ledger)

    # Back-fill every window with what the ledger remembers beyond the scan
    # (pruned transcripts): ``ledger - scanned`` per field, for EVERY day the
    # window touches. Day granularity is fine — only the 24h window can split
    # a day. Done per account too, so the split still sums to the total.
    def _backfill(target: dict, ledgered: dict, scanned: dict) -> None:
        for d, t in ledgered.items():
            delta = _ledger_delta(t, scanned.get(d))
            if delta is None:
                continue
            try:
                d_epoch = time.mktime(time.strptime(d, "%Y-%m-%d"))
            except (TypeError, ValueError):
                continue
            for k, cut in cutoffs.items():
                if d_epoch + 86400 >= cut:  # part of that day is in the window
                    _add(target[k], delta, delta["cost"])

    _backfill(acc, ledger_days, day_totals)
    for account, acct_days in ledger_accounts.items():
        _backfill(
            acc_accounts.setdefault(account, {k: _zero() for k in _WINDOWS}),
            acct_days,
            day_accounts.get(account, {}),
        )

    # The newest carried-over v1 day inside the year window: everything before
    # the day after it is an estimate.
    estimated_before = None
    year_cut = cutoffs["year"]
    for d in sorted(approx, reverse=True):
        try:
            d_epoch = time.mktime(time.strptime(d, "%Y-%m-%d"))
        except (TypeError, ValueError):
            continue
        if d_epoch + 86400 >= year_cut:
            estimated_before = time.strftime(
                "%Y-%m-%d", time.localtime(d_epoch + 36 * 3600)
            )
        break

    recent.sort(key=lambda e: e[0])
    return acc, recent, acc_accounts, estimated_before


def _refresh() -> None:
    """Recompute the scan into the cache if stale. Caller holds no lock."""
    now = time.time()
    with _lock:
        if _cache["windows"] is not None and (now - _cache["at"]) < _CACHE_TTL:
            return
        try:
            result, recent, accounts, estimated = _compute()
        except Exception:  # noqa: BLE001
            result, recent, accounts, estimated = (
                {k: _zero() for k in _WINDOWS},
                [],
                {},
                None,
            )
        _cache["windows"] = result
        _cache["recent"] = recent
        _cache["accounts"] = accounts
        _cache["estimated_before"] = estimated
        _cache["at"] = now


def windows() -> dict:
    """``{"day","week","month","year"}`` -> rolling totals, cached ~60s.

    Each total is ``{"in","out","cache_read","cache_write","cost"}``.
    """
    _refresh()
    with _lock:
        return _cache["windows"]


def estimated_before() -> Optional[str]:
    """``"YYYY-MM-DD"`` when the year window includes days carried over from a
    version-1 ledger (approximate figures, transcripts long gone): every day
    before it is an estimate. ``None`` when the whole year is from transcripts
    or this ledger's own exact history. Cached with :func:`windows`."""
    _refresh()
    with _lock:
        return _cache.get("estimated_before")


def windows_by_account() -> dict:
    """Per-account rolling totals: ``{account_id: {"day",…,"year"}}``.

    ``account_id`` is an auth-profile id, or :data:`AMBIENT_ACCOUNT` for
    everything under the ambient ``~/.claude*`` login. Only accounts with any
    recorded usage appear. Cached with the same ~60s scan as :func:`windows`.
    """
    _refresh()
    with _lock:
        return dict(_cache.get("accounts") or {})


def current_window(hours: float, account: "str | None" = None) -> "dict | None":
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

    ``account`` restricts the walk to one identity's transcripts
    (:data:`AMBIENT_ACCOUNT` for the CLI's own login). A plan window belongs to
    ONE subscription: merging every account's turns into it would price a
    personal plan's window with work billed to a work account, and anchor it on
    a message the plan never saw. ``None`` keeps the old machine-wide walk for
    callers that want the aggregate.
    """
    if not hours or hours <= 0:
        return None
    _refresh()
    with _lock:
        recent = list(_cache.get("recent") or ())
    if account is not None:
        recent = [
            e for e in recent if (e[3] if len(e) > 3 else AMBIENT_ACCOUNT) == account
        ]
    if not recent:
        return None
    span = hours * 3600.0
    now = time.time()
    anchor = None
    for entry in recent:
        ts = entry[0]
        if anchor is None or ts >= anchor + span:
            anchor = ts
    if anchor is None or now >= anchor + span:
        return None  # no turn in-window — the next message starts a fresh one
    cost = 0.0
    tokens = 0
    for entry in recent:
        ts, c, tok = entry[0], entry[1], entry[2]
        if ts >= anchor:
            cost += c
            tokens += sum(tok.values())
    return {"anchor": anchor, "end": anchor + span, "cost": cost, "tokens": tokens}
