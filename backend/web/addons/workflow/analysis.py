"""Workflow Optimizer — analysis (pure: rows in, report out; no I/O).

Turns the recorder's timeline (:mod:`.store` rows) into **work items** and
says where their time went.

* A **work item** is one branch's trip to a merged PR: everything recorded
  for the same ``repo@branch`` (two windows on one branch are one item) up to
  the merge. A long-lived session that ships PR after PR yields one item per
  PR — after a merge, the next item starts at the next real work (agent
  working, setup, a commit), never at the idle time in between.
* Each moment of an item is in exactly one **phase** (:data:`PHASES`). When
  several windows share the item, the busiest one speaks for it (an agent
  working in one window beats another window sitting idle).
* Time the recorder could not see — the server was down — is ``untracked``,
  never stretched out of the last phase seen.
* "Your turn" (the agent finished and nothing moved) is split by whether a
  person was at the keyboard: ``your_turn`` while you were around, ``away``
  while you weren't — the first is a review/steer bottleneck, the second is
  capacity left idle.

The headline is **time to PR** (first recorded work → PR opened), with the
phase breakdown of that span; what happens after (review, rework, merge) is
reported separately.
"""

from __future__ import annotations

import statistics
import time
from typing import Dict, Iterable, List, Optional, Tuple

#: (id, label, group, what it means). Groups say WHO is the bottleneck.
PHASES: List[Tuple[str, str, str, str]] = [
    ("setup", "Setup", "tooling", "workspace provisioning and setup commands"),
    ("agent", "Agent working", "agent", "the agent is mid-turn"),
    (
        "question",
        "Waiting on your answer",
        "you",
        "the agent asked a question or a permission",
    ),
    (
        "your_turn",
        "Your turn",
        "you",
        "the agent finished a turn and you were at the keyboard",
    ),
    (
        "away",
        "Idle while you were away",
        "idle",
        "the agent finished a turn and nobody was around",
    ),
    ("limit", "Usage limit", "limits", "locked out by the plan's usage limit"),
    (
        "precommit",
        "Pre-commit hooks",
        "tooling",
        "the commit's pre-commit hooks are running",
    ),
    (
        "hook_fix",
        "Recovering from failed hooks",
        "tooling",
        "a commit's hooks failed; fixing and retrying",
    ),
    ("to_push", "Committed, not pushed", "handoff", "commits waiting to be pushed"),
    (
        "to_pr",
        "Pushed, no PR yet",
        "handoff",
        "the branch is on the remote without a PR",
    ),
    (
        "review",
        "PR open — waiting",
        "review",
        "the PR is open and nothing is working on it",
    ),
    (
        "rework",
        "Agent reworking the PR",
        "agent",
        "the agent is working after the PR opened",
    ),
    ("offline", "Agent offline", "other", "the agent process is gone"),
    ("paused", "Paused", "other", "the session is paused"),
    (
        "untracked",
        "Untracked",
        "other",
        "MindFlock wasn't running, so nothing was recorded",
    ),
]
PHASE_IDS = [p[0] for p in PHASES]
GROUPS: List[Tuple[str, str]] = [
    ("agent", "Agent"),
    ("you", "You"),
    ("tooling", "Tooling"),
    ("handoff", "Handoffs"),
    ("review", "Review"),
    ("limits", "Limits"),
    ("idle", "Idle (away)"),
    ("other", "Other"),
]
GROUP_OF = {p[0]: p[2] for p in PHASES}

#: Which window speaks for a shared item at a moment (higher wins).
_PRIORITY = {
    "agent": 13,
    "rework": 12,
    "precommit": 11,
    "hook_fix": 10,
    "question": 9,
    "limit": 8,
    "setup": 7,
    "your_turn": 6,
    "to_push": 5,
    "to_pr": 4,
    "review": 3,
    "offline": 2,
    "paused": 1,
}
#: Phases that start a new item after a merge (real work, not idle).
_ACTIVE = frozenset({"agent", "rework", "setup", "precommit", "hook_fix", "question"})
#: Time that isn't the workflow's: nobody was around, or nothing was recorded.
#: "Active time to PR" leaves these out.
INACTIVE = frozenset({"away", "untracked"})
#: A heartbeat (or any row) proves the recorder was alive for this long after it.
ALIVE_S = 480.0
#: Work on these never becomes a PR, so it is no work item (an in-place
#: session sitting on main after a merge would read "pushed, no PR" forever).
BASE_BRANCHES = frozenset({"main", "master", "develop", "trunk"})
#: Two merge rows this close are the same merge seen by two windows.
_MERGE_DEDUPE_S = 120.0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _f(v, d: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def coverage(rows: Iterable[dict], now: float) -> List[Tuple[float, float]]:
    """When the recorder was alive: every row proves it for :data:`ALIVE_S`
    after its time (clipped at ``now``), merged into sorted intervals."""
    spans = sorted(
        (t, min(t + ALIVE_S, now)) for t in (_f(r.get("t")) for r in rows) if t <= now
    )
    out: List[List[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _clip(a: float, b: float, cov: List[Tuple[float, float]]):
    """``[a, b)`` split into (start, end, covered) pieces against ``cov``."""
    pieces = []
    cur = a
    for ca, cb in cov:
        if cb <= cur:
            continue
        if ca >= b:
            break
        if ca > cur:
            pieces.append((cur, min(ca, b), False))
            cur = min(ca, b)
        if cur >= b:
            break
        end = min(cb, b)
        if end > cur:
            pieces.append((cur, end, True))
            cur = end
        if cur >= b:
            break
    if cur < b:
        pieces.append((cur, b, False))
    return pieces


def item_key(meta: dict, session: str) -> str:
    repo = str(meta.get("repo") or "").strip()
    branch = str(meta.get("branch") or "").strip()
    if branch:
        return "%s@%s" % (repo or "?", branch)
    return "session:" + session


# --------------------------------------------------------------------------- #
# Rows → per-session segments
# --------------------------------------------------------------------------- #
def session_segments(
    rows: List[dict], now: float
) -> Tuple[List[dict], Dict[str, dict], Dict[str, List[float]]]:
    """``(segments, metas, merges)``.

    ``segments``: ``{s, key, ph, a, start, end}`` — one per stretch a session
    spent in a phase, split where the recorder was down (those pieces become
    ``untracked``) and where the session's branch changed. ``metas``: the
    newest meta per item key. ``merges``: merge epochs per item key."""
    cov = coverage(rows, now)
    by_s: Dict[str, List[dict]] = {}
    for r in rows:
        s = r.get("s")
        if s and r.get("k") in ("ph", "meta", "merge", "end"):
            by_s.setdefault(str(s), []).append(r)
    segs: List[dict] = []
    metas: Dict[str, dict] = {}
    merges: Dict[str, List[float]] = {}
    for s, srows in by_s.items():
        srows.sort(key=lambda r: _f(r.get("t")))
        meta: dict = {}
        key = "session:" + s
        cur: Optional[dict] = None  # {ph, a, start}

        def close(at: float) -> None:
            nonlocal cur
            if cur is None or at <= cur["start"]:
                return
            for a, b, covered in _clip(cur["start"], at, cov):
                segs.append(
                    {
                        "s": s,
                        "key": cur["key"],
                        "ph": cur["ph"] if covered else "untracked",
                        "a": bool(cur["a"]) if covered else False,
                        "start": a,
                        "end": b,
                    }
                )

        for r in srows:
            t = _f(r.get("t"))
            k = r.get("k")
            if k == "meta":
                meta = {
                    **meta,
                    **{x: v for x, v in r.items() if x not in ("t", "k", "s")},
                }
                nk = item_key(meta, s)
                metas[nk] = {**metas.get(nk, {}), **meta, "session": s}
                if nk != key and cur is not None:
                    close(t)
                    cur = {**cur, "start": t, "key": nk}
                key = nk
            elif k == "ph":
                ph = str(r.get("ph") or "")
                if ph not in _PRIORITY:
                    continue
                close(t)
                cur = {"ph": ph, "a": bool(r.get("a")), "start": t, "key": key}
            elif k == "merge":
                merges.setdefault(key, []).append(t)
                if r.get("pr"):
                    metas.setdefault(key, {})["pr"] = r.get("pr")
            elif k == "end":
                close(t)
                cur = None
        if cur is not None:
            close(now)
    return segs, metas, merges


# --------------------------------------------------------------------------- #
# Segments → work items
# --------------------------------------------------------------------------- #
def _combine(segs: List[dict]) -> List[dict]:
    """One timeline from several windows' segments: at each moment the
    highest-priority phase (untracked only where NO window was tracked).
    A sweep over the segment boundaries — a long-lived session has thousands
    of segments, so never compare every cut with every segment."""
    if not segs:
        return []
    marks: List[Tuple[float, int, int]] = []  # (time, 0=end / 1=start, index)
    for i, sg in enumerate(segs):
        if sg["end"] > sg["start"]:
            marks.append((sg["start"], 1, i))
            marks.append((sg["end"], 0, i))
    marks.sort()
    active: Dict[int, dict] = {}
    out: List[dict] = []
    k = 0
    while k < len(marks):
        t = marks[k][0]
        while k < len(marks) and marks[k][0] == t:
            _, kind, i = marks[k]
            if kind:
                active[i] = segs[i]
            else:
                active.pop(i, None)
            k += 1
        if not active or k >= len(marks):
            continue
        nxt = marks[k][0]
        tracked = [sg for sg in active.values() if sg["ph"] != "untracked"]
        if tracked:
            ph = max(tracked, key=lambda sg: _PRIORITY.get(sg["ph"], 0))["ph"]
            if ph == "your_turn" and not any(sg["a"] for sg in tracked):
                ph = "away"
        else:
            ph = "untracked"
        if out and out[-1]["ph"] == ph and abs(out[-1]["end"] - t) < 1e-6:
            out[-1]["end"] = nxt
        else:
            out.append({"ph": ph, "start": t, "end": nxt})
    return out


def _split_at(timeline: List[dict], at: float) -> Tuple[List[dict], List[dict]]:
    before, after = [], []
    for sg in timeline:
        if sg["end"] <= at:
            before.append(sg)
        elif sg["start"] >= at:
            after.append(sg)
        else:
            before.append({**sg, "end": at})
            after.append({**sg, "start": at})
    return before, after


def _dedupe(ts: List[float]) -> List[float]:
    out: List[float] = []
    for t in sorted(ts):
        if not out or t - out[-1] > _MERGE_DEDUPE_S:
            out.append(t)
    return out


def _totals(timeline: List[dict], a: float, b: float) -> Dict[str, float]:
    tot: Dict[str, float] = {}
    for sg in timeline:
        s, e = max(sg["start"], a), min(sg["end"], b)
        if e > s:
            tot[sg["ph"]] = tot.get(sg["ph"], 0.0) + (e - s)
    return tot


def build_items(
    rows: List[dict], now: float, live_sessions: Optional[Iterable[str]] = None
) -> List[dict]:
    """Every work item in ``rows``, newest first (see the module docstring).
    ``live_sessions``: titles that still exist (an item none of whose
    sessions does, and that never merged, is ``abandoned``)."""
    segs, metas, merges = session_segments(rows, now)
    live = set(live_sessions or [])
    by_key: Dict[str, List[dict]] = {}
    for sg in segs:
        by_key.setdefault(sg["key"], []).append(sg)
    items: List[dict] = []
    for key, ksegs in by_key.items():
        if key.rsplit("@", 1)[-1] in BASE_BRANCHES:
            continue
        timeline = _combine(ksegs)
        sessions = sorted({sg["s"] for sg in ksegs})
        meta = metas.get(key, {})
        rest = timeline
        cycles: List[Tuple[List[dict], Optional[float]]] = []
        for m in _dedupe(merges.get(key, [])):
            before, rest = _split_at(rest, m)
            cycles.append((before, m))
        cycles.append((rest, None))
        for idx, (tl, merged_at) in enumerate(cycles):
            if idx > 0:
                # After a merge the next item starts at real work.
                first = next(
                    (i for i, sg in enumerate(tl) if sg["ph"] in _ACTIVE), None
                )
                if first is None:
                    continue
                tl = tl[first:]
            if not tl:
                continue
            items.append(_item(key, idx, tl, merged_at, meta, sessions, live, now))
    items.sort(key=lambda it: it["end"], reverse=True)
    return items


def _item(key, idx, tl, merged_at, meta, sessions, live, now) -> dict:
    start = tl[0]["start"]
    end = merged_at if merged_at is not None else tl[-1]["end"]
    pr_at = next((sg["start"] for sg in tl if sg["ph"] in ("review", "rework")), None)
    to_pr = _totals(tl, start, pr_at if pr_at is not None else end)
    after = _totals(tl, pr_at, end) if pr_at is not None else {}
    commits = fails = 0
    prev = ""
    for sg in tl:
        if sg["ph"] == "precommit" and prev != "precommit":
            commits += 1
        if sg["ph"] == "hook_fix" and prev == "precommit":
            fails += 1
        prev = sg["ph"]
    alive = any(s in live for s in sessions)
    if merged_at is not None:
        status = "merged"
    elif pr_at is not None:
        status = "pr_open" if alive or not live else "closed"
    else:
        status = "in_progress" if alive or not live else "abandoned"
    branch = str(meta.get("branch") or "")
    return {
        "id": "%s#%d" % (key, idx),
        "key": key,
        "cycle": idx,
        "repo": str(meta.get("repo") or ""),
        "branch": branch,
        "ticket": str(meta.get("ticket") or ""),
        "name": str(meta.get("name") or ""),
        "pr": str(meta.get("pr") or ""),
        "sessions": sessions,
        "status": status,
        "start": start,
        "pr_at": pr_at,
        "merged_at": merged_at,
        "end": end,
        "lead_to_pr": (pr_at - start) if pr_at is not None else None,
        "active_to_pr": (
            round(sum(v for k, v in to_pr.items() if k not in INACTIVE), 1)
            if pr_at is not None
            else None
        ),
        "lead_to_merge": (merged_at - start) if merged_at is not None else None,
        "elapsed": end - start,
        "to_pr": {k: round(v, 1) for k, v in to_pr.items()},
        "after_pr": {k: round(v, 1) for k, v in after.items()},
        "commits": commits,
        "hook_failures": fails,
        "timeline": [
            {"ph": sg["ph"], "start": round(sg["start"], 1), "end": round(sg["end"], 1)}
            for sg in tl[-400:]
        ],
    }


# --------------------------------------------------------------------------- #
# Items → the report
# --------------------------------------------------------------------------- #
def _pct(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    i = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
    return v[i]


def phase_stats(items: List[dict], field: str = "to_pr") -> List[dict]:
    """Per phase over ``items``: total seconds, share of the total, median and
    p90 per item (over the items where it happened), and how many items."""
    totals: Dict[str, float] = {}
    per: Dict[str, List[float]] = {}
    for it in items:
        for ph, sec in (it.get(field) or {}).items():
            totals[ph] = totals.get(ph, 0.0) + sec
            per.setdefault(ph, []).append(sec)
    grand = sum(totals.values()) or 0.0
    out = []
    for pid, label, group, hint in PHASES:
        if pid not in totals:
            continue
        vals = per[pid]
        out.append(
            {
                "phase": pid,
                "label": label,
                "group": group,
                "hint": hint,
                "total_s": round(totals[pid], 1),
                "share": round(totals[pid] / grand, 4) if grand else 0.0,
                "median_s": round(statistics.median(vals), 1),
                "p90_s": round(_pct(vals, 0.9) or 0.0, 1),
                "items": len(vals),
            }
        )
    out.sort(key=lambda r: r["total_s"], reverse=True)
    return out


def group_stats(stats: List[dict]) -> List[dict]:
    tot: Dict[str, float] = {}
    for r in stats:
        tot[r["group"]] = tot.get(r["group"], 0.0) + r["total_s"]
    grand = sum(tot.values()) or 0.0
    return [
        {
            "group": g,
            "label": label,
            "total_s": round(tot[g], 1),
            "share": round(tot[g] / grand, 4) if grand else 0.0,
        }
        for g, label in GROUPS
        if g in tot
    ]


def _fmt(sec: float) -> str:
    """Durations the way the dashboard says them (its ``fmtDur``)."""
    s = max(0, int(round(float(sec))))
    if s < 60:
        return "%ds" % s
    m = int(round(s / 60.0))
    if m < 60:
        return "%dm" % m
    h, rm = divmod(m, 60)
    if h < 24:
        return "%dh %dm" % (h, rm) if rm else "%dh" % h
    d, rh = divmod(h, 24)
    return "%dd %dh" % (d, rh) if rh else "%dd" % d


def advice(items: List[dict], stats: List[dict]) -> List[dict]:
    """What to change, ranked by the time at stake: ``{phase, level, title,
    detail, at_stake_s}``. Only phases worth at least 5% of time to PR (or a
    hook failure rate that matters) say anything."""
    by = {r["phase"]: r for r in stats}
    out: List[dict] = []

    def add(phase: str, title: str, detail: str, level: str = "warn") -> None:
        r = by.get(phase)
        if r is None:
            return
        out.append(
            {
                "phase": phase,
                "level": level,
                "title": title,
                "detail": detail,
                "at_stake_s": r["total_s"],
                "share": r["share"],
            }
        )

    def big(phase: str, share: float = 0.05) -> bool:
        return phase in by and by[phase]["share"] >= share

    if big("your_turn"):
        r = by["your_turn"]
        add(
            "your_turn",
            "Finished work waits on you",
            "After the agent's turn ends, items sit %s (median per item) before anything moves — %d%% of time to PR. "
            "Arm autopilot to the PR (⏩, or a template's “when a run's turn ends”) so finished work ships "
            "without waiting for you, and review in the PR instead of the terminal."
            % (_fmt(r["median_s"]), round(r["share"] * 100)),
        )
    if big("question"):
        r = by["question"]
        add(
            "question",
            "Agents are blocked on your answers",
            "Questions and permission prompts hold items %s per item (%d%% of time to PR). Front-load the prompt "
            "(acceptance criteria, files to touch) and allow-list the tools it keeps asking about."
            % (_fmt(r["median_s"]), round(r["share"] * 100)),
        )
    commits = sum(it.get("commits", 0) for it in items)
    fails = sum(it.get("hook_failures", 0) for it in items)
    if big("precommit", 0.03):
        r = by["precommit"]
        per_commit = r["total_s"] / commits if commits else r["median_s"]
        add(
            "precommit",
            "Pre-commit hooks are slow",
            "Hooks take %s per commit across %d commit%s (%d%% of time to PR). Move slow checks (the test "
            "suite, type checks over the whole tree) to pre-push or CI, and run only changed-file hooks at commit."
            % (
                _fmt(per_commit),
                commits,
                "" if commits == 1 else "s",
                round(r["share"] * 100),
            ),
        )
    if commits and fails and (fails / commits >= 0.2 or big("hook_fix", 0.03)):
        r = by.get("hook_fix") or {"median_s": 0.0, "share": 0.0}
        add(
            "hook_fix",
            "Commits keep failing their hooks",
            "%d of %d commits failed pre-commit (%d%%), costing %s per item to recover. Put the formatter/linter "
            "commands in the agent's instructions (CLAUDE.md/AGENTS.md) so it runs them before committing."
            % (fails, commits, round(100 * fails / commits), _fmt(r["median_s"])),
        )
    if big("to_push") or big("to_pr"):
        tot = by.get("to_push", {}).get("total_s", 0) + by.get("to_pr", {}).get(
            "total_s", 0
        )
        phase = (
            "to_pr"
            if by.get("to_pr", {}).get("total_s", 0)
            >= by.get("to_push", {}).get("total_s", 0)
            else "to_push"
        )
        r = by[phase]
        add(
            phase,
            "Handoffs between commit, push and PR",
            "Committed or pushed work waits %s in total before a PR exists. Set the autopilot depth to “pr” so "
            "commit → push → PR happens in one go." % _fmt(tot),
        )
    if big("setup"):
        r = by["setup"]
        add(
            "setup",
            "Workspace setup is slow",
            "Provisioning and setup commands take %s per item. Use worktrees over full clones, and cache "
            "dependency installs in the setup script." % _fmt(r["median_s"]),
        )
    if big("limit"):
        r = by["limit"]
        add(
            "limit",
            "Usage limits stall work",
            "Items sat %s at a usage limit (%d%% of time to PR). Spread work across accounts/providers or "
            "schedule big runs for when the window resets (the Usage Optimizer shows when)."
            % (_fmt(r["total_s"]), round(r["share"] * 100)),
        )
    if big("away", 0.15):
        r = by["away"]
        add(
            "away",
            "Agents finish and sit idle while you're away",
            "%s of finished-but-idle time (%d%% of time to PR) happened with nobody at the keyboard. Give "
            "sessions an autopilot depth, or queue the next prompt / a template run so they keep going."
            % (_fmt(r["total_s"]), round(r["share"] * 100)),
            level="info",
        )
    if big("agent", 0.5):
        r = by["agent"]
        add(
            "agent",
            "Agent time dominates",
            "The agent itself is %d%% of time to PR (%s per item). Split tickets smaller and run them in "
            "parallel, or use a faster model for routine items."
            % (round(r["share"] * 100), _fmt(r["median_s"])),
            level="info",
        )
    if big("untracked", 0.2):
        add(
            "untracked",
            "Much of the time wasn't recorded",
            "MindFlock wasn't running for a large part of these items, so the breakdown is partial.",
            level="info",
        )
    # Things to fix first (by the time at stake), then context.
    out.sort(key=lambda a: (a["level"] == "info", -a["at_stake_s"]))
    return out


def weekly_trend(items: List[dict], now: float, weeks: int) -> List[dict]:
    """Median time to PR per week (by when the PR opened), oldest first."""
    out = []
    week = 7 * 86400.0
    for w in range(weeks - 1, -1, -1):
        b = now - w * week
        a = b - week
        vals = [
            it["lead_to_pr"]
            for it in items
            if it.get("pr_at") is not None and a <= it["pr_at"] < b
        ]
        out.append(
            {
                "start": a,
                "end": b,
                "items": len(vals),
                "median_to_pr": round(statistics.median(vals), 1) if vals else None,
            }
        )
    return out


def report(
    rows: List[dict],
    now: Optional[float] = None,
    period: float = 7 * 86400.0,
    live_sessions: Optional[Iterable[str]] = None,
) -> dict:
    """The dashboard payload for the last ``period`` seconds."""
    now = time.time() if now is None else now
    since = now - period
    all_items = build_items(rows, now, live_sessions)
    items = [it for it in all_items if it["end"] >= since]
    shipped = [
        it for it in items if it.get("pr_at") is not None and it["pr_at"] >= since
    ]
    merged = [
        it
        for it in items
        if it.get("merged_at") is not None and it["merged_at"] >= since
    ]
    basis = shipped if shipped else items
    stats = phase_stats(basis, "to_pr")
    after_stats = phase_stats(shipped, "after_pr")
    leads = [it["lead_to_pr"] for it in shipped]
    actives = [it["active_to_pr"] for it in shipped]
    merges = [it["lead_to_merge"] for it in merged]
    first = min((_f(r.get("t")) for r in rows), default=None)
    return {
        "now": now,
        "period_s": period,
        "tracking_since": first,
        "phases": [
            {"id": p[0], "label": p[1], "group": p[2], "hint": p[3]} for p in PHASES
        ],
        "groups": [{"id": g, "label": label} for g, label in GROUPS],
        "kpi": {
            "items": len(items),
            "prs_opened": len(shipped),
            "merged": len(merged),
            "median_to_pr": round(statistics.median(leads), 1) if leads else None,
            "p90_to_pr": round(_pct(leads, 0.9) or 0.0, 1) if leads else None,
            "median_active_to_pr": (
                round(statistics.median(actives), 1) if actives else None
            ),
            "median_to_merge": round(statistics.median(merges), 1) if merges else None,
            "in_progress": sum(1 for it in items if it["status"] == "in_progress"),
        },
        "basis": "shipped" if shipped else "all",
        "to_pr": stats,
        "to_pr_groups": group_stats(stats),
        "after_pr": after_stats,
        "advice": advice(basis, stats),
        "trend": weekly_trend(
            all_items, now, max(1, min(13, int(round(period / (7 * 86400.0))) or 1))
        ),
        "items": items[:200],
    }
