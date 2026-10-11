"""Usage Optimizer — the pure analysis over recorded usage samples.

Everything here is a function of ``(samples, now)``: no I/O, no server
imports, so the maths behind the score and the advice can be pinned by unit
tests with hand-built sample lists. The sampler (``sampler.py``) records one
sample per windowed provider per tick; the store (``store.py``) keeps them;
the routes (``__init__.py``) call :func:`provider_report` per provider.

A sample is one dict::

    {"t": epoch, "p": "claude",
     "pct": 37.5 | None,        # percent of the active plan window used
     "end": epoch | None,       # when that window resets
     "wpct": 12.0 | None,       # weekly cap percent used
     "wend": epoch | None,      # when the weekly cap resets
     "src": "live" | "estimate" | "",
     "work": 2, "idle": 1, "lim": 0, "off": 0,   # sessions by activity
     "att": True,               # a person typed into MindFlock recently
     "tok": 123456 | None}      # tokens in the active window (Claude only)

The thesis the score encodes: a plan window is **use it or lose it**. Over a
period P a provider with an H-hour rolling window could have filled P/H
windows to 100 %; the capacity score is the share of that ceiling the
observed windows actually reached. A window the user never opened counts as
zero, exactly like one opened and left at 10 %.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

__all__ = [
    "HEARTBEAT_S",
    "MAX_GAP_S",
    "ATTENDED_S",
    "bucket_state",
    "group_windows",
    "capacity_score",
    "live_pace",
    "weekly_pace",
    "timeline",
    "time_split",
    "token_rate",
    "advice",
    "calibrate_capacity",
    "fill_pct",
    "provider_report",
]

#: The sampler writes a heartbeat at least this often even when nothing
#: changed, so a quiet stretch still proves the server was watching.
HEARTBEAT_S = 300.0
#: A sample stands for the time until the next one, but never longer than
#: this: past it the server was evidently not running (or the sampler was
#: wedged), and that time is "unknown", not a continuation of the last state.
MAX_GAP_S = 2 * HEARTBEAT_S
#: A person typing into any MindFlock window within this many seconds makes
#: the moment "attended" — the split between "you were working" and
#: "things ran on their own" (overnight, after hours, a looping queue).
ATTENDED_S = 600.0

_STATES = ("working", "idle", "limited", "offline", "unknown")


def calibrate_capacity(samples: Sequence[dict]) -> dict:
    """What one full window costs, for providers with no live meter.

    The transcript estimate knows a window's API-equivalent ``cost`` but not
    the plan's ceiling. Three sources, best first: the user's window budget
    (``budget`` on the sample, Settings → window budget); the cost windows
    reached when they ended in a lockout (``lim`` seen inside the window —
    the ceiling, observed); else the most expensive window seen so far, as a
    provisional yardstick (the score then reads "relative to your best
    window"). Returns ``{"capacity": usd | None, "basis": "budget" |
    "learned" | "provisional" | ""}``.
    """
    budget = 0.0
    by_end: Dict[float, dict] = {}
    for s in samples:
        b = _f(s.get("budget"))
        if b and b > budget:
            budget = b
        cost = _f(s.get("cost"))
        end = _f(s.get("end"))
        if cost is None or end is None:
            continue
        key = round(end / 60.0) * 60.0
        g = by_end.setdefault(key, {"cost": 0.0, "lim": False})
        g["cost"] = max(g["cost"], cost)
        if int(s.get("lim") or 0) > 0:
            g["lim"] = True
    if budget > 0:
        return {"capacity": budget, "basis": "budget"}
    locked = [g["cost"] for g in by_end.values() if g["lim"] and g["cost"] > 0]
    if locked:
        return {"capacity": sum(locked) / len(locked), "basis": "learned"}
    best = max((g["cost"] for g in by_end.values()), default=0.0)
    if best > 0:
        return {"capacity": best, "basis": "provisional"}
    return {"capacity": None, "basis": ""}


def fill_pct(samples: Sequence[dict]) -> List[dict]:
    """Copies of ``samples`` where a reading with a ``cost`` but no ``pct``
    gets one from :func:`calibrate_capacity` (``src`` set to the basis), so
    the rest of the analysis runs the same with or without a live meter."""
    cal = calibrate_capacity(samples)
    cap = cal["capacity"]
    out = []
    for s in samples:
        s = dict(s)
        if _f(s.get("pct")) is None and _f(s.get("cost")) is not None and cap:
            s["pct"] = round(min(100.0, 100.0 * _f(s["cost"]) / cap), 1)
            s["src"] = cal["basis"]
        out.append(s)
    return out


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _span(samples: Sequence[dict], i: int, now: float) -> float:
    """Seconds sample ``i`` stands for: until the next sample (or ``now``),
    capped at :data:`MAX_GAP_S`."""
    t = _f(samples[i].get("t")) or 0.0
    nxt = _f(samples[i + 1].get("t")) if i + 1 < len(samples) else now
    if nxt is None:
        nxt = now
    return max(0.0, min(nxt - t, MAX_GAP_S))


def bucket_state(s: dict) -> str:
    """One word for what the sessions were doing at a sample: ``limited``
    beats ``working`` beats ``idle`` beats ``offline`` (no live session)."""
    if int(s.get("lim") or 0) > 0:
        return "limited"
    if int(s.get("work") or 0) > 0:
        return "working"
    if int(s.get("idle") or 0) > 0:
        return "idle"
    return "offline"


def group_windows(
    samples: Sequence[dict], window_hours: float, now: float
) -> List[dict]:
    """Segment samples into plan windows by their reset time.

    Live readings carry the provider's own ``resets_at``; estimates carry the
    anchor + span. Either way every sample of one window shares (roughly) one
    ``end`` — rounded to the minute, and ends within two minutes of each other
    are merged, because a live meter re-reports a reset with second-level
    jitter. Returns windows oldest first::

        {"end", "start", "final_pct", "first_seen", "last_seen", "n",
         "attended_share", "live": bool, "src"}

    ``final_pct`` is the highest reading seen (percent only climbs inside a
    window). ``start`` is ``end - window_hours`` when the hours are known, else
    the first sighting. ``live`` marks the window that has not reset yet.
    Samples with no ``end`` (metered providers, or a reading the provider
    could not supply) are skipped.
    """
    groups: List[dict] = []
    for s in samples:
        end = _f(s.get("end"))
        t = _f(s.get("t"))
        if end is None or t is None:
            continue
        end = round(end / 60.0) * 60.0
        pct = _f(s.get("pct"))
        g = groups[-1] if groups else None
        if g is None or abs(g["end"] - end) > 120.0:
            g = {
                "end": end,
                "start": (end - window_hours * 3600.0) if window_hours > 0 else t,
                "final_pct": pct if pct is not None else 0.0,
                "has_pct": pct is not None,
                "first_seen": t,
                "last_seen": t,
                "n": 0,
                "_att": 0,
                "src": str(s.get("src") or ""),
            }
            groups.append(g)
        g["n"] += 1
        g["last_seen"] = max(g["last_seen"], t)
        g["first_seen"] = min(g["first_seen"], t)
        if pct is not None:
            g["has_pct"] = True
            g["final_pct"] = max(g["final_pct"], pct)
        if s.get("att"):
            g["_att"] += 1
        if s.get("src") == "live":
            g["src"] = "live"
    out = []
    for g in groups:
        att = g.pop("_att")
        g["attended_share"] = round(att / g["n"], 3) if g["n"] else 0.0
        g["final_pct"] = round(min(100.0, max(0.0, g["final_pct"])), 1)
        g["live"] = g["end"] > now
        out.append(g)
    return out


def capacity_score(
    windows: Sequence[dict],
    window_hours: float,
    period_s: float,
    now: float,
    observed_s: Optional[float] = None,
) -> dict:
    """The capacity score for one provider over the last ``period_s``.

    ``ceiling`` is how many windows could have been filled in the observed
    stretch (``observed_s / window_hours``; ``observed_s`` defaults to the
    period, so a server that was down scores the downtime as unused — pass the
    known-coverage seconds to score only what was watched). ``used`` is the
    sum of completed windows' final percentages plus the live window's
    reading, each as a fraction of a window. ``score`` is ``used / ceiling``
    on 0–100, capped at 100.

    Returns ``{"score", "windows", "avg_fill", "ceiling", "used", "basis"}`` —
    ``basis`` is ``"live"`` when every counted window had a live reading,
    ``"estimate"`` when any relied on the transcript estimate, ``""`` when
    nothing was observed (``score`` is then ``None``).
    """
    if window_hours <= 0 or period_s <= 0:
        return {
            "score": None,
            "windows": 0,
            "avg_fill": None,
            "ceiling": 0.0,
            "used": 0.0,
            "basis": "",
        }
    since = now - period_s
    counted = [w for w in windows if w.get("has_pct") and w["end"] >= since]
    if not counted:
        return {
            "score": None,
            "windows": 0,
            "avg_fill": None,
            "ceiling": 0.0,
            "used": 0.0,
            "basis": "",
        }
    span = period_s if observed_s is None else max(0.0, min(observed_s, period_s))
    ceiling = span / (window_hours * 3600.0)
    used = sum(w["final_pct"] / 100.0 for w in counted)
    completed = [w for w in counted if not w["live"]]
    avg_fill = (
        round(sum(w["final_pct"] for w in completed) / len(completed), 1)
        if completed
        else None
    )
    score = min(100.0, 100.0 * used / ceiling) if ceiling > 0 else None
    srcs = {w.get("src") or "" for w in counted}
    others = srcs - {"live"}
    basis = (
        "live"
        if not others
        else (next(iter(others)) if len(others) == 1 else "estimate")
    )
    return {
        "score": round(score, 1) if score is not None else None,
        "windows": len(completed),
        "avg_fill": avg_fill,
        "ceiling": round(ceiling, 2),
        "used": round(used, 2),
        "basis": basis,
    }


def _recent_rate(
    samples: Sequence[dict], end: float, now: float, lookback_s: float = 1800.0
) -> Optional[float]:
    """Percent-per-hour burn over the last ``lookback_s`` inside the live
    window (readings sharing ``end``), or None with fewer than two points."""
    pts = []
    for s in samples:
        e = _f(s.get("end"))
        t = _f(s.get("t"))
        pct = _f(s.get("pct"))
        if e is None or t is None or pct is None:
            continue
        if abs(round(e / 60.0) * 60.0 - end) > 120.0 or t < now - lookback_s:
            continue
        pts.append((t, pct))
    if len(pts) < 2:
        return None
    (t0, p0), (t1, p1) = pts[0], pts[-1]
    if t1 - t0 < 120.0:
        return None
    return max(0.0, (p1 - p0) / ((t1 - t0) / 3600.0))


def live_pace(
    samples: Sequence[dict], window_hours: float, now: float
) -> Optional[dict]:
    """Where the active window stands and where it is heading.

    ``ratio`` compares the share used with the share of the window elapsed:
    1.0 is "exactly on pace to finish the window at 100 %", above it is
    draining faster than time passes, below it is leaving capacity. ``rate``
    is the recent burn (percent per hour, from the last 30 minutes, else the
    window-average). ``eta_limit`` is when 100 % lands at that rate (None if
    never before the reset); ``projected`` is the percent expected at reset.
    None when no live window is in the samples.
    """
    windows = group_windows(samples, window_hours, now)
    live = next((w for w in reversed(windows) if w["live"] and w.get("has_pct")), None)
    if live is None:
        return None
    span = (live["end"] - live["start"]) if window_hours > 0 else None
    pct = live["final_pct"]
    elapsed = (now - live["start"]) if span else None
    remaining = max(0.0, live["end"] - now)
    elapsed_frac = (
        min(1.0, max(0.0, elapsed / span)) if span and elapsed is not None else None
    )
    rate = _recent_rate(samples, live["end"], now)
    if rate is None and elapsed and elapsed > 600:
        rate = pct / (elapsed / 3600.0)
    ratio = None
    if elapsed_frac is not None and elapsed_frac >= 0.05:
        ratio = round(pct / (100.0 * elapsed_frac), 2)
    eta_limit = None
    projected = pct
    if rate is not None and rate > 0:
        hours_to_full = (100.0 - pct) / rate
        if hours_to_full * 3600.0 < remaining:
            eta_limit = now + hours_to_full * 3600.0
        projected = min(100.0, pct + rate * (remaining / 3600.0))
    return {
        "pct": pct,
        "end": live["end"],
        "start": live["start"],
        "remaining_s": round(remaining),
        "elapsed_frac": round(elapsed_frac, 3) if elapsed_frac is not None else None,
        "ratio": ratio,
        "rate_pct_h": round(rate, 2) if rate is not None else None,
        "eta_limit": eta_limit,
        "projected": round(projected, 1),
        "src": live.get("src") or "",
    }


def weekly_pace(
    samples: Sequence[dict], weekly_hours: float, now: float
) -> Optional[dict]:
    """The weekly cap's standing, same vocabulary as :func:`live_pace`. The
    week's start is ``wend - weekly_hours``. None without a weekly reading."""
    last = None
    for s in reversed(samples):
        if _f(s.get("wpct")) is not None and _f(s.get("wend")) is not None:
            last = s
            break
    if last is None or weekly_hours <= 0:
        return None
    wpct = _f(last["wpct"]) or 0.0
    wend = _f(last["wend"]) or now
    span = weekly_hours * 3600.0
    start = wend - span
    elapsed_frac = min(1.0, max(0.0, (now - start) / span))
    remaining = max(0.0, wend - now)
    ratio = round(wpct / (100.0 * elapsed_frac), 2) if elapsed_frac >= 0.05 else None
    projected = min(100.0, wpct / elapsed_frac) if elapsed_frac >= 0.05 else wpct
    eta_limit = None
    if ratio and ratio > 1.0 and wpct < 100.0:
        rate = wpct / max(1.0, (now - start) / 3600.0)  # pct per hour, week average
        hours_to_full = (100.0 - wpct) / rate if rate > 0 else None
        if hours_to_full is not None and hours_to_full * 3600.0 < remaining:
            eta_limit = now + hours_to_full * 3600.0
    return {
        "pct": round(wpct, 1),
        "end": wend,
        "remaining_s": round(remaining),
        "elapsed_frac": round(elapsed_frac, 3),
        "ratio": ratio,
        "projected": round(projected, 1),
        "eta_limit": eta_limit,
    }


def timeline(
    samples: Sequence[dict], since: float, now: float, bucket_s: float
) -> List[dict]:
    """Fixed buckets from ``since`` to ``now``: the dominant state, whether a
    person was around, and the last window reading — the uptime strip.

    Dominance is by seconds: the state that covered most of the bucket wins,
    ``unknown`` filling whatever no sample covered. ``att`` is true when any
    attended sample touched the bucket.
    """
    if bucket_s <= 0 or now <= since:
        return []
    n = int(math.ceil((now - since) / bucket_s))
    buckets = [
        {
            "t": since + i * bucket_s,
            "state": "unknown",
            "att": False,
            "pct": None,
            "_cov": {k: 0.0 for k in _STATES},
        }
        for i in range(n)
    ]
    for i, s in enumerate(samples):
        t = _f(s.get("t"))
        if t is None:
            continue
        t_end = t + _span(samples, i, now)
        if t_end <= since or t >= now:
            continue
        state = bucket_state(s)
        pct = _f(s.get("pct"))
        lo = max(0, int((max(t, since) - since) // bucket_s))
        hi = min(n - 1, int((min(t_end, now) - since - 1e-9) // bucket_s))
        for b in range(lo, hi + 1):
            bk = buckets[b]
            b0, b1 = bk["t"], bk["t"] + bucket_s
            overlap = max(0.0, min(t_end, b1) - max(t, b0))
            if overlap <= 0:
                continue
            bk["_cov"][state] += overlap
            if s.get("att"):
                bk["att"] = True
            if pct is not None:
                bk["pct"] = pct
    for bk in buckets:
        cov = bk.pop("_cov")
        covered = sum(cov.values())
        cov["unknown"] = max(0.0, bucket_s - covered)
        bk["state"] = max(_STATES, key=lambda k: cov[k])
    return buckets


def time_split(samples: Sequence[dict], since: float, now: float) -> dict:
    """Seconds by (attended?, state) over ``[since, now]``, plus ``unknown``::

    {"attended": {"working", "idle", "limited", "offline"},
     "unattended": {...}, "unknown": s, "total": s}
    """
    out = {
        "attended": {k: 0.0 for k in _STATES[:4]},
        "unattended": {k: 0.0 for k in _STATES[:4]},
        "unknown": 0.0,
        "total": max(0.0, now - since),
    }
    covered = 0.0
    for i, s in enumerate(samples):
        t = _f(s.get("t"))
        if t is None:
            continue
        t_end = min(t + _span(samples, i, now), now)
        lo = max(t, since)
        if t_end <= lo:
            continue
        secs = t_end - lo
        who = "attended" if s.get("att") else "unattended"
        out[who][bucket_state(s)] += secs
        covered += secs
    out["unknown"] = max(0.0, out["total"] - covered)
    for who in ("attended", "unattended"):
        out[who] = {k: round(v) for k, v in out[who].items()}
    out["unknown"] = round(out["unknown"])
    out["total"] = round(out["total"])
    return out


def token_rate(
    samples: Sequence[dict], since: float, now: float, bucket_s: float
) -> List[dict]:
    """Tokens consumed per bucket from the per-window ``tok`` counter: the
    delta between consecutive readings of the same window; a new window
    starts the count over. Empty when no sample carries ``tok``."""
    if bucket_s <= 0 or now <= since:
        return []
    n = int(math.ceil((now - since) / bucket_s))
    out = [{"t": since + i * bucket_s, "tokens": 0} for i in range(n)]
    prev_tok: Optional[float] = None
    prev_end: Optional[float] = None
    any_tok = False
    for s in samples:
        t = _f(s.get("t"))
        tok = _f(s.get("tok"))
        end = _f(s.get("end"))
        if t is None:
            continue
        if tok is None:
            prev_tok, prev_end = None, None
            continue
        any_tok = True
        same_window = (
            prev_end is not None and end is not None and abs(prev_end - end) <= 120.0
        )
        delta = tok - prev_tok if (same_window and prev_tok is not None) else tok
        prev_tok, prev_end = tok, end
        if delta <= 0 or t < since or t >= now:
            continue
        out[min(n - 1, int((t - since) // bucket_s))]["tokens"] += int(delta)
    return out if any_tok else []


_CHEAPER = {
    "claude": "switch routine work to Sonnet (/model sonnet) or a lower effort level",
    "codex": "pick a smaller model or lower reasoning effort in /model",
    "antigravity": "move routine work to the model group with headroom",
}


def _h(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return "%dm" % max(1, round(seconds / 60.0))
    h = seconds / 3600.0
    if h >= 48:
        d, rh = divmod(int(round(h)), 24)
        return "%dd %dh" % (d, rh) if rh else "%dd" % d
    return ("%.1fh" % h).replace(".0h", "h")


def advice(
    provider: str,
    label: str,
    live: Optional[dict],
    weekly: Optional[dict],
    split_24h: dict,
    score: dict,
    attended_now: bool,
) -> List[dict]:
    """The recommendations, most urgent first: ``{"level", "title", "text"}``
    with level ``warn`` (you are about to lose working time), ``tip``
    (capacity is going unused) or ``ok``."""
    out: List[dict] = []
    cheaper = _CHEAPER.get(provider, "use a cheaper model for routine work")
    if live and live.get("eta_limit") and (live.get("ratio") or 0) > 1.15:
        out.append(
            {
                "level": "warn" if attended_now else "tip",
                "title": "%s is draining %.1f× faster than the window"
                % (label, live["ratio"]),
                "text": (
                    "At the recent pace (%s%%/h) the limit lands in about %s, with %s still "
                    "left before the reset. While you are at the keyboard, %s so the lockout "
                    "does not fall in the middle of your working time."
                )
                % (
                    (
                        ("%g" % live["rate_pct_h"])
                        if live.get("rate_pct_h") is not None
                        else "?"
                    ),
                    _h(live["eta_limit"] - (live["end"] - live["remaining_s"])),
                    _h(live["remaining_s"]),
                    cheaper,
                ),
            }
        )
    if weekly and weekly["pct"] >= 100:
        out.append(
            {
                "level": "warn",
                "title": "%s's weekly cap is used up" % label,
                "text": (
                    "Nothing more runs on this plan until the weekly reset in %s. "
                    "Move work that can't wait to another provider or account, and next "
                    "week %s earlier so the cap lasts the whole week."
                )
                % (_h(weekly["remaining_s"]), cheaper),
            }
        )
    if weekly and (weekly.get("ratio") or 0) > 1.15 and weekly["pct"] < 100:
        when = weekly.get("eta_limit")
        out.append(
            {
                "level": "warn" if weekly["ratio"] > 1.4 else "tip",
                "title": "Weekly cap: %.0f%% used with %s of the week left"
                % (weekly["pct"], _h(weekly["remaining_s"])),
                "text": (
                    "That is %.1f× the even pace%s. Keep the expensive model for the work "
                    "that needs it and %s for the rest, especially unattended runs."
                )
                % (
                    weekly["ratio"],
                    (
                        (
                            " — at this rate it runs out about %s before the reset"
                            % _h(weekly["end"] - when)
                        )
                        if when
                        else ""
                    ),
                    cheaper,
                ),
            }
        )
    if (
        live
        and live["remaining_s"] <= 3600
        and live["pct"] < 60
        and (live.get("rate_pct_h") or 0) < 5
    ):
        out.append(
            {
                "level": "tip",
                "title": "%.0f%% of this window will go unused" % (100 - live["pct"]),
                "text": (
                    "It resets in %s and nothing is burning it. Queue a prompt on a session "
                    "(the Queue tab, or a looping self-improvement prompt) so the agent fills "
                    "it, or launch a fast-track run."
                )
                % _h(live["remaining_s"]),
            }
        )
    un = split_24h.get("unattended") or {}
    un_idle = float(un.get("idle") or 0) + float(un.get("offline") or 0)
    un_total = sum(float(v) for v in un.values())
    if un_total >= 4 * 3600 and un_idle / max(1.0, un_total) >= 0.5:
        out.append(
            {
                "level": "tip",
                "title": "%s of unattended time sat idle in the last day" % _h(un_idle),
                "text": (
                    "Overnight and after-hours windows reset whether or not anything ran. "
                    "Leave a looping queue prompt or a fast-track ticket queue running so "
                    "that time fills them — a cheaper model is fine for work nobody is "
                    "waiting on."
                ),
            }
        )
    at = split_24h.get("attended") or {}
    at_lim = float(at.get("limited") or 0)
    if at_lim >= 1800:
        out.append(
            {
                "level": "warn",
                "title": "%s locked out while you were working (last 24h)" % _h(at_lim),
                "text": (
                    "The limit screen landed during attended time. Shift the heavy, "
                    "unattended work to cheaper models or to the hours after a reset so the "
                    "window is there when you need it."
                ),
            }
        )
    if not out:
        sc = score.get("score")
        locked = sum(
            float((split_24h.get(who) or {}).get("limited") or 0)
            for who in ("attended", "unattended")
        )
        if sc is not None and sc >= 75:
            out.append(
                {
                    "level": "ok",
                    "title": "Using %.0f%% of %s's capacity" % (sc, label),
                    "text": (
                        "Windows are filling and nothing locked you out in the last day. "
                        "Keep the loop going."
                        if locked < 60
                        else "Windows are filling. The lockouts in the last day fell "
                        "while nobody was at the keyboard, which costs nothing."
                    ),
                }
            )
        elif sc is not None:
            out.append(
                {
                    "level": "tip",
                    "title": "%.0f%% of %s's capacity used" % (sc, label),
                    "text": (
                        "Every window that resets below 100%% is capacity you cannot get back. "
                        "Queue more work for the hours you are away."
                    ),
                }
            )
        else:
            out.append(
                {
                    "level": "ok",
                    "title": "Watching %s" % label,
                    "text": "No completed window recorded yet — the score appears after the first reset.",
                }
            )
    return out


def provider_report(
    provider: str,
    label: str,
    samples: Sequence[dict],
    window_hours: float,
    weekly_hours: float,
    now: float,
    period_s: float,
    bucket_s: float,
    attended_now: bool = False,
    mode: str = "windowed",
) -> dict:
    """Everything the UI shows for one provider, from its samples."""
    samples = sorted(
        (s for s in samples if _f(s.get("t")) is not None), key=lambda s: s["t"]
    )
    calibration = calibrate_capacity(samples)
    samples = fill_pct(samples)
    since = now - period_s
    windows = group_windows(samples, window_hours, now)
    split = time_split(samples, since, now)
    # What was watched: the sampled coverage, but never less than the span of
    # the windows being scored — a window seen only at a few points still
    # stood for its whole length, or a sparse sampler would score itself up.
    observed = max(0.0, split["total"] - split["unknown"])
    for w in windows:
        if w.get("has_pct") and w["end"] >= since:
            observed = max(observed, min(w["end"], now) - max(w["start"], since))
    span_sum = sum(
        max(0.0, min(w["end"], now) - max(w["start"], since))
        for w in windows
        if w.get("has_pct") and w["end"] >= since
    )
    observed = max(observed, min(period_s, span_sum))
    score = capacity_score(windows, window_hours, period_s, now, observed_s=observed)
    live = live_pace(samples, window_hours, now)
    weekly = weekly_pace(samples, weekly_hours, now)
    split_24h = time_split(samples, now - 86400.0, now)
    tips = (
        advice(provider, label, live, weekly, split_24h, score, attended_now)
        if mode == "windowed"
        else []
    )
    return {
        "name": provider,
        "label": label,
        "mode": mode,
        "window_hours": window_hours,
        "weekly_hours": weekly_hours,
        "score": score,
        "live": live,
        "weekly": weekly,
        "windows": [w for w in windows if w["end"] >= since],
        "timeline": timeline(samples, since, now, bucket_s),
        "tokens": token_rate(samples, since, now, bucket_s),
        "split": split,
        "split_24h": split_24h,
        "advice": tips,
        "samples": len(samples),
        "calibration": calibration,
    }
