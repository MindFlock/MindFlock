"""Usage Optimizer — the sampler (the impure half).

Every :data:`TICK_S` it asks each plan-windowed provider where its window
stands (the same computation the cost panel's ``/api/usage`` runs —
``core.usage_api._usage_window_for``, which reads the provider's live meter
when it has one and the transcript estimate otherwise), counts the live
sessions by activity per provider from the ``/api/instances`` snapshot, and
asks the server whether a person typed into any window lately
(``server._HUMAN_INPUT_AT``, the stamp every web keystroke, /send and queue
send-now leaves). One sample per provider goes to the store when anything
changed, and at least every :data:`analysis.HEARTBEAT_S` regardless, so a
quiet night still proves the server was watching.

The loop runs on the server's event loop and pushes the probing to a worker
thread (the window computation can shell out and hit the network behind its
own caches); one bad provider or one failed tick never stops the loop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from . import analysis, store

__all__ = [
    "TICK_S",
    "collect",
    "sample_once",
    "loop",
    "attended_now",
    "providers_watched",
]

#: How often the sampler looks. The provider meters are cached ~60 s
#: upstream, so looking more often would only re-read the cache.
TICK_S = 60.0
#: A window whose reset time is this far in the past is not the active
#: window but a leftover snapshot; it is recorded as "no reading".
STALE_S = 120.0

_last_written: Dict[str, dict] = {}


def _server():
    from backend.web import server

    return server


def attended_now(now: Optional[float] = None) -> bool:
    """Whether a person typed into any MindFlock window within
    :data:`analysis.ATTENDED_S`. Keystrokes that bypass MindFlock (a raw
    ``tmux attach``) are invisible here, so this errs toward "unattended"."""
    now = now or time.time()
    try:
        stamps = _server()._HUMAN_INPUT_AT
    except Exception:  # noqa: BLE001 — a server without the stamp: unattended
        return False
    try:
        latest = max(stamps.values()) if stamps else 0.0
    except Exception:  # noqa: BLE001
        return False
    return now - latest < analysis.ATTENDED_S


def providers_watched() -> list:
    """The providers the optimizer tracks: the ones the cost panel shows
    (``usage_panel_visible``) — plan-windowed ones get a window reading,
    metered ones only a session-activity timeline."""
    from backend import providers

    out = []
    for p in providers.all_providers():
        try:
            if p.usage_panel_visible():
                out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def _provider_of(program: str) -> str:
    from backend import providers

    try:
        return providers.resolve(program or "").name
    except Exception:  # noqa: BLE001
        return ""


def _session_counts(rows: List[dict]) -> Dict[str, Dict[str, int]]:
    """``{provider: {"work", "idle", "lim", "off"}}`` from the session rows.
    ``clarify`` (waiting on a human) counts as idle: no tokens are burning."""
    out: Dict[str, Dict[str, int]] = {}
    for r in rows:
        name = _provider_of(str(r.get("program") or ""))
        if not name:
            continue
        c = out.setdefault(name, {"work": 0, "idle": 0, "lim": 0, "off": 0})
        act = str(r.get("activity") or "")
        status = str(r.get("status") or "")
        if status == "paused":
            continue
        if act == "working":
            c["work"] += 1
        elif act == "limit" or r.get("limited"):
            c["lim"] += 1
        elif act in ("idle", "clarify"):
            c["idle"] += 1
        else:
            c["off"] += 1
    return out


def collect(now: Optional[float] = None) -> List[dict]:
    """One sample per watched provider, built from live server state. Pure
    with respect to the store (nothing is written). Never raises."""
    now = now or time.time()
    srv = _server()
    try:
        rows = srv._events.sessions_snapshot()
    except Exception:  # noqa: BLE001
        rows = []
    counts = _session_counts(rows)
    att = attended_now(now)
    out: List[dict] = []
    for p in providers_watched():
        s: dict = {
            "t": round(now, 1),
            "p": p.name,
            "pct": None,
            "end": None,
            "wpct": None,
            "wend": None,
            "src": "",
        }
        try:
            mode = p.usage_mode()
        except Exception:  # noqa: BLE001
            mode = "metered"
        if mode == "windowed":
            try:
                win = srv._usage_window_for(p) or None
            except Exception:  # noqa: BLE001
                win = None
            # A reading whose reset time has already passed is a stale
            # snapshot (Codex's last rollout file from weeks ago, say), not a
            # window: recording it would score a dead window at 0 % forever.
            if win and win.get("end") and float(win["end"]) < now - STALE_S:
                win = None
            if win:
                pct = win.get("percent_used")
                s["pct"] = round(float(pct), 1) if pct is not None else None
                s["end"] = float(win["end"]) if win.get("end") else None
                s["src"] = str(win.get("source") or "")
                wk = win.get("weekly") or {}
                if wk.get("end") and float(wk["end"]) < now - STALE_S:
                    wk = {}
                if wk.get("percent_used") is not None:
                    s["wpct"] = round(float(wk["percent_used"]), 1)
                if wk.get("end"):
                    s["wend"] = float(wk["end"])
                if win.get("tokens") is not None and p.name == "claude":
                    s["tok"] = int(win["tokens"])
                # The transcript estimate carries the window's API-equivalent
                # cost and the user's window budget: with no live meter the
                # analysis derives a percent from them (``analysis.fill_pct``).
                if win.get("cost") is not None:
                    s["cost"] = round(float(win["cost"]), 4)
                    s["budget"] = round(float(win.get("budget") or 0.0), 2)
                if s["pct"] is None and win.get("groups"):
                    # Per-group quotas (Antigravity): the headline is the
                    # fullest group — that is the one that locks you out.
                    try:
                        pcts = [
                            float(g["percent_used"])
                            for g in win["groups"]
                            if g.get("percent_used") is not None
                        ]
                        if pcts:
                            s["pct"] = round(max(pcts), 1)
                    except (TypeError, ValueError, KeyError):
                        pass
        c = counts.get(p.name) or {"work": 0, "idle": 0, "lim": 0, "off": 0}
        s.update(c)
        s["att"] = bool(att)
        out.append(s)
    return out


_DIFF_KEYS = (
    "pct",
    "end",
    "wpct",
    "wend",
    "src",
    "cost",
    "work",
    "idle",
    "lim",
    "off",
    "att",
)


def _changed(new: dict, old: Optional[dict]) -> bool:
    if old is None:
        return True
    return any(new.get(k) != old.get(k) for k in _DIFF_KEYS)


def sample_once(now: Optional[float] = None, force: bool = False) -> List[dict]:
    """Collect and store: write a provider's sample when it differs from the
    last written one, when the heartbeat is due, or when ``force``. Returns
    the samples written."""
    now = now or time.time()
    fresh = collect(now)
    if not _last_written:
        try:
            _last_written.update(store.last_by_provider())
        except Exception:  # noqa: BLE001
            pass
    to_write = []
    for s in fresh:
        old = _last_written.get(s["p"])
        due = old is None or (now - float(old.get("t") or 0)) >= analysis.HEARTBEAT_S
        if force or due or _changed(s, old):
            to_write.append(s)
    if to_write:
        store.append(to_write)
        for s in to_write:
            _last_written[s["p"]] = s
    return to_write


async def loop() -> None:
    """Drive the sampler forever (registered by the addon's ``on_startup``)."""
    while True:
        try:
            await asyncio.to_thread(sample_once)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — the loop must never die
            try:
                from backend import log

                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("usage optimizer sample failed: %v", err)
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(TICK_S)
