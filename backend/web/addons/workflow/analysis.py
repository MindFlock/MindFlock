"""Workflow Optimizer — analysis (pure: rows in, report out; no I/O).

Turns the recorder's timeline (:mod:`.store` rows) into **units of work** and
says where their time went.

* A **unit** of work ends at a **commit**: a commit is what finishes a piece
  of work, so the headline is **time to commit** (first real work → the
  commit). Everything recorded for the same ``repo@branch`` shares one
  timeline (two windows on one branch are one unit), and the next unit
  starts at the next real work after the commit (a new agent turn, setup,
  hooks, a question) — never at the idle time in between.
* Commits come from the recorder's ``commit`` rows (the branch's HEAD moved
  to a commit authored just now; merges, pulls, rebases and amends don't
  count). Rows from before the recorder watched HEAD have none, so there the
  commit is **estimated** from the phases: pre-commit hooks that passed, or
  the branch first showing a commit. Units closed that way say so.
* Each moment is in exactly one **phase** (:data:`PHASES`). When several
  windows share the timeline, the busiest one speaks for it (an agent
  working in one window beats another window sitting idle). Inside a unit,
  an idle agent is the person's move whatever the branch's stage (a branch
  with an earlier commit reads "committed, not pushed" between turns): those
  stretches are ``your_turn`` / ``away``, and ``rework`` is ``agent``.
* What happens AFTER the commit — waiting to be pushed, pushed without a PR,
  the PR open — is reported separately (``after_commit``), and only the
  waits that ended in the next handoff (a push, a PR, a merge) or are still
  waiting now count; a stage the work sat in until the next agent turn was
  the person's time, not a handoff.
* Time the recorder could not see — the server was down — is ``untracked``,
  never stretched out of the last phase seen.
* "Your turn" is split by whether a person was at the keyboard: ``your_turn``
  while you were around, ``away`` while you weren't.

**Your own time is left out by default** (``include_me=False``): the phases
in :data:`PERSON_PHASES` are cut out of the clock — every duration, median,
p90, share and the weekly trend is computed as if the person had answered
instantly, so the bottleneck ranking is about the agent, the tooling, limits
and the handoffs after the commit. ``include_me=True`` is the full wall clock.
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
    (
        "to_push",
        "Committed, not pushed",
        "handoff",
        "after the commit: waiting to be pushed",
    ),
    (
        "to_pr",
        "Pushed, no PR yet",
        "handoff",
        "after the commit: on the remote without a PR",
    ),
    (
        "review",
        "PR open — waiting",
        "review",
        "after the commit: the PR is open and nothing is working on it",
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

#: The person's own time: a question or permission waiting on an answer, the
#: agent's turn ended with you at the keyboard, or with nobody around. Unless
#: the person opts in (``include_me``), the analysis assumes they move at max
#: speed — time waiting on them is not a process bottleneck — so these are
#: cut out of the clock (the timeline compressed, not just the rows hidden).
#: ``review`` after the commit (PR open, nothing working) stays: it waits on
#: reviewers and CI, not on your turn in the terminal. ``untracked`` stays:
#: it is nobody's.
PERSON_PHASES = frozenset({"question", "your_turn", "away"})

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
#: Phases that start a new unit after a commit (real work, not idle).
_ACTIVE = frozenset({"agent", "rework", "setup", "precommit", "hook_fix", "question"})
#: The handoffs after a commit, in the order work moves through them (a merge
#: is 4). A wait counts only when the work later reached a higher one.
_HANDOFF_RANK = {"to_push": 1, "to_pr": 2, "review": 3}
#: What a phase says about the branch's stage when the commit has to be
#: estimated (no ``commit`` rows yet): 0 = nothing committed on it (an idle
#: agent on a branch with no commit reads "your turn"), else its handoff
#: rank. Phases that hide the stage (the agent working, hooks…) say nothing.
_STAGE_RANK = {"your_turn": 0, "away": 0, **_HANDOFF_RANK, "rework": 3}
#: Time that isn't the workflow's: nobody was around, or nothing was recorded.
#: "Active time to PR" leaves these out (``away`` is gone already unless the
#: person's own time is counted — :data:`PERSON_PHASES`).
INACTIVE = frozenset({"away", "untracked"})
#: A heartbeat (or any row) proves the recorder was alive for this long after it.
ALIVE_S = 480.0
#: On these only units that reached a commit are listed: an in-place session
#: on main is often just a conversation, which must not read "in progress"
#: forever.
BASE_BRANCHES = frozenset({"main", "master", "develop", "trunk"})


def hidden_phases(raw: Iterable[str]) -> frozenset:
    """The known phase ids in ``raw`` (junk and unknown ids dropped)."""
    if isinstance(raw, str) or raw is None:
        return frozenset()
    return frozenset(p for p in raw if isinstance(p, str) and p in GROUP_OF)


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
def session_segments(rows: List[dict], now: float) -> dict:
    """``{segs, metas, merges, commits, exact}`` from the rows.

    ``segs``: ``{s, key, ph, a, start, end}`` — one per stretch a session
    spent in a phase, split where the recorder was down (those pieces become
    ``untracked``) and where the session's branch changed. ``metas``: the
    newest meta per item key. ``merges``: merge epochs per key. ``commits``:
    ``{t, sha, n, merge}`` per key (the recorder's ``commit`` rows).
    ``exact``: per key, when the recorder started watching its HEAD (its
    first ``head`` / ``commit`` row) — commits before that are estimated."""
    cov = coverage(rows, now)
    by_s: Dict[str, List[dict]] = {}
    for r in rows:
        s = r.get("s")
        if s and r.get("k") in ("ph", "meta", "merge", "end", "head", "commit"):
            by_s.setdefault(str(s), []).append(r)
    segs: List[dict] = []
    metas: Dict[str, dict] = {}
    merges: Dict[str, List[float]] = {}
    commits: Dict[str, List[dict]] = {}
    exact: Dict[str, float] = {}
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
            elif k in ("head", "commit"):
                exact[key] = min(exact.get(key, t), t)
                if k == "commit":
                    commits.setdefault(key, []).append(
                        {
                            "t": t,
                            "sha": str(r.get("sha") or ""),
                            "n": int(_f(r.get("n"))),
                            "merge": bool(r.get("merge")),
                        }
                    )
            elif k == "end":
                close(t)
                cur = None
        if cur is not None:
            close(now)
    return {
        "segs": segs,
        "metas": metas,
        "merges": merges,
        "commits": commits,
        "exact": exact,
    }


# --------------------------------------------------------------------------- #
# Segments → units of work
# --------------------------------------------------------------------------- #
def _combine(segs: List[dict]) -> List[dict]:
    """One timeline from several windows' segments: at each moment the
    highest-priority phase (untracked only where NO window was tracked), and
    ``a`` — whether the person was at the keyboard — from any window.
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
        att = any(sg["a"] for sg in tracked)
        if tracked:
            ph = max(tracked, key=lambda sg: _PRIORITY.get(sg["ph"], 0))["ph"]
            if ph == "your_turn" and not att:
                ph = "away"
        else:
            ph = "untracked"
        if (
            out
            and out[-1]["ph"] == ph
            and out[-1]["a"] == att
            and abs(out[-1]["end"] - t) < 1e-6
        ):
            out[-1]["end"] = nxt
        else:
            out.append({"ph": ph, "a": att, "start": t, "end": nxt})
    return out


def _estimated_commits(
    timeline: List[dict], until: float, merges: Iterable[float] = ()
) -> List[float]:
    """When commits happened before ``until``, read off the phases (for rows
    from before the recorder watched HEAD): where pre-commit hooks finished
    without failing, where a branch with nothing committed (an idle agent
    reading "your turn") next shows a commit, a push or a PR, and — for work
    on a branch that already had one (a PR's rework) — at the end of the last
    real work before a merge. Other later commits on an already-committed
    branch without hooks are invisible here."""
    out: List[float] = []
    # The branch's last known stage rank. A branch is taken to start with
    # nothing committed: a worker that commits in its first turn (no hooks)
    # first shows up already "committed, not pushed".
    rank = 0
    hooked = False  # a hook run already marked the commit this rise shows
    prev = ""
    for sg in timeline:
        if sg["start"] >= until:
            break
        ph = sg["ph"]
        if prev == "precommit" and ph not in ("precommit", "hook_fix"):
            out.append(sg["start"])
            hooked = True
        r = _STAGE_RANK.get(ph)
        if r is not None:
            if rank == 0 and r >= 1 and not hooked:
                out.append(sg["start"])
            rank, hooked = r, False
        prev = ph
    for m in merges:
        if m >= until:
            continue
        last = [sg["end"] for sg in timeline if sg["ph"] in _ACTIVE and sg["end"] <= m]
        if last:
            out.append(last[-1])
    return out


def _commit_marks(
    timeline: List[dict],
    commits: List[dict],
    exact_since: Optional[float],
    merges: Iterable[float] = (),
) -> List[Tuple[float, bool]]:
    """``(time, estimated)`` for every commit that can close a unit: the
    recorder's commits that brought new work (``n`` > 0, not a merge commit;
    the same sha seen by two windows once), plus the estimated ones before
    the recorder watched HEAD."""
    out: List[Tuple[float, bool]] = []
    seen = set()
    for c in sorted(commits, key=lambda c: c["t"]):
        if c["merge"] or c["n"] <= 0 or (c["sha"] and c["sha"] in seen):
            continue
        seen.add(c["sha"])
        out.append((c["t"], False))
    until = exact_since if exact_since is not None else float("inf")
    out += [(t, True) for t in _estimated_commits(timeline, until, merges)]
    out.sort()
    return out


def _clip_tl(timeline: List[dict], a: float, b: float) -> List[dict]:
    out = []
    for sg in timeline:
        s, e = max(sg["start"], a), min(sg["end"], b)
        if e > s:
            out.append({**sg, "start": s, "end": e})
    return out


def _in_unit(timeline: List[dict]) -> List[dict]:
    """A unit's own stretch: before its commit, a handoff stage only means
    an EARLIER commit sits on the branch — the agent is idle, so it is the
    person's move (``your_turn`` / ``away``); a working agent is ``agent``."""
    out: List[dict] = []
    for sg in timeline:
        ph = sg["ph"]
        if ph == "rework":
            ph = "agent"
        elif ph in _HANDOFF_RANK:
            ph = "your_turn" if sg["a"] else "away"
        if out and out[-1]["ph"] == ph and abs(out[-1]["end"] - sg["start"]) < 1e-6:
            out[-1] = {**out[-1], "end": sg["end"]}
        else:
            out.append({"ph": ph, "start": sg["start"], "end": sg["end"]})
    return out


def _after_commit(gap: List[dict], merged: bool, still_open: bool) -> dict:
    """Seconds per handoff phase after a unit's commit, up to the next unit
    or the merge (``merged``: the gap ends at one). A wait counts when the
    work later reached a further handoff (a push, a PR, the merge) — or when
    the gap runs to now and it is still waiting. A stage the work merely sat
    in until the next agent turn was the person's move, not a handoff.
    Downtime inside a wait doesn't split it."""
    tot: Dict[str, float] = {}
    for i, sg in enumerate(gap):
        r = _HANDOFF_RANK.get(sg["ph"])
        if r is None:
            continue
        rest = [x["ph"] for x in gap[i + 1 :] if x["ph"] != "untracked"]
        later = [_HANDOFF_RANK.get(ph, -1) for ph in rest] + ([4] if merged else [])
        waiting = still_open and all(ph == sg["ph"] for ph in rest)
        if waiting or (later and max(later) > r):
            tot[sg["ph"]] = tot.get(sg["ph"], 0.0) + (sg["end"] - sg["start"])
    return tot


def _totals(timeline: List[dict], a: float, b: float) -> Dict[str, float]:
    tot: Dict[str, float] = {}
    for sg in timeline:
        s, e = max(sg["start"], a), min(sg["end"], b)
        if e > s:
            tot[sg["ph"]] = tot.get(sg["ph"], 0.0) + (e - s)
    return tot


def build_items(
    rows: List[dict],
    now: float,
    live_sessions: Optional[Iterable[str]] = None,
    include_me: bool = False,
    hidden: Iterable[str] = (),
) -> List[dict]:
    """Every unit of work in ``rows``, newest first (see the module
    docstring). ``live_sessions``: titles that still exist (an open unit none
    of whose sessions does is ``abandoned``). ``include_me`` False cuts
    :data:`PERSON_PHASES` out of every unit's clock; ``hidden`` (phase ids
    the person chose not to see) is cut out the same way. The units
    themselves (ids, boundaries, status, epochs) are the same either way."""
    skip = frozenset() if include_me else PERSON_PHASES
    hide = hidden_phases(hidden) - skip
    seg = session_segments(rows, now)
    live = set(live_sessions or [])
    by_key: Dict[str, List[dict]] = {}
    for sg in seg["segs"]:
        by_key.setdefault(sg["key"], []).append(sg)
    items: List[dict] = []
    for key, ksegs in by_key.items():
        timeline = _combine(ksegs)
        if not timeline:
            continue
        base = key.rsplit("@", 1)[-1] in BASE_BRANCHES
        sessions = sorted({sg["s"] for sg in ksegs})
        meta = seg["metas"].get(key, {})
        merges = seg["merges"].get(key, [])
        marks = _commit_marks(
            timeline, seg["commits"].get(key, []), seg["exact"].get(key), merges
        )
        end_all = timeline[-1]["end"]
        # A wait at the end of the record is "still waiting" only while a
        # window is open (no live list: can't tell, so it is).
        alive = not live or any(s in live for s in sessions)

        def next_start(after: float, tl=timeline) -> Optional[float]:
            """The next unit begins at the next stretch of real work that
            starts AFTER the commit — the rest of the committing turn (the
            agent summing up, hooks passing mid-turn) is no unit of its own."""
            for sg in tl:
                if sg["ph"] in _ACTIVE and sg["start"] > after + 1e-6:
                    return sg["start"]
            return None

        start = next((sg["start"] for sg in timeline if sg["ph"] in _ACTIVE), None)
        idx = 0
        for t, est in marks:
            if start is None:
                break
            if t <= start:
                continue
            nxt = next_start(t)
            gap_end = nxt if nxt is not None else end_all
            items.append(
                _item(
                    key,
                    idx,
                    timeline,
                    (start, t, est, gap_end, nxt is None and alive),
                    merges,
                    meta,
                    sessions,
                    live,
                    skip,
                    hide,
                )
            )
            idx += 1
            start = nxt
        # The open unit at the end. On a base branch it isn't listed (often
        # just a conversation); nor is it once every window is gone after an
        # earlier commit — the leftovers of a finished branch (a last report,
        # a question afterwards) are no abandoned work.
        if start is not None and not base and not (idx and not alive):
            items.append(
                _item(
                    key,
                    idx,
                    timeline,
                    (start, None, False, end_all, True),
                    merges,
                    meta,
                    sessions,
                    live,
                    skip,
                    hide,
                )
            )
    items.sort(key=lambda it: it["end"], reverse=True)
    return items


def _compress(tl: List[dict], skip: frozenset) -> List[dict]:
    """``tl`` with the ``skip`` phases cut out and everything after each cut
    moved earlier by its length (so the strip has no holes where the person
    was). ``at`` keeps each piece's real start, for its tooltip."""
    out: List[dict] = []
    shift = 0.0
    for sg in tl:
        if sg["ph"] in skip:
            shift += sg["end"] - sg["start"]
            continue
        a, b = sg["start"] - shift, sg["end"] - shift
        if out and out[-1]["ph"] == sg["ph"] and abs(out[-1]["end"] - a) < 1e-6:
            out[-1]["end"] = b
        else:
            out.append({"ph": sg["ph"], "start": a, "end": b, "at": sg["start"]})
    return out


def _item(
    key,
    idx,
    timeline,
    bounds,
    merges,
    meta,
    sessions,
    live,
    skip=frozenset(),
    hide=frozenset(),
) -> dict:
    """One unit. ``bounds``: ``(start, commit_at, estimated, gap_end,
    still_open)`` — ``commit_at`` None for the open unit at the end, and the
    gap after the commit runs to ``gap_end`` (the next unit's start, or the
    end of the record when ``still_open``). ``skip`` (the person's phases)
    and ``hide`` (phases hidden from view) are both cut out of the clock;
    their seconds are ``your_time_s`` and ``hidden_s``."""
    start, commit_at, estimated, gap_end, still_open = bounds
    end = commit_at if commit_at is not None else gap_end
    tl = _in_unit(_clip_tl(timeline, start, end))
    to_commit = _totals(tl, start, end)
    after: Dict[str, float] = {}
    if commit_at is not None:
        # The handoffs end at the merge: what follows it is no wait.
        merged = [m for m in merges if commit_at <= m <= gap_end + 1e-6]
        if merged:
            gap_end, still_open = min(merged), False
        after = _after_commit(
            _clip_tl(timeline, commit_at, gap_end), bool(merged), still_open
        )
    mine = sum(v for k, v in to_commit.items() if k in skip)
    hid = sum(v for k, v in to_commit.items() if k in hide)
    cut = skip | hide
    to_commit = {k: v for k, v in to_commit.items() if k not in cut}
    after = {k: v for k, v in after.items() if k not in hide}
    elapsed = max(0.0, round(end - start - mine - hid, 1))
    runs = fails = 0
    prev = ""
    for sg in tl:
        if sg["ph"] == "precommit" and prev != "precommit":
            runs += 1
        if sg["ph"] == "hook_fix" and prev == "precommit":
            fails += 1
        prev = sg["ph"]
    alive = any(s in live for s in sessions)
    if commit_at is not None:
        status = "committed"
    else:
        status = "in_progress" if alive or not live else "abandoned"
    return {
        "id": "%s#%d" % (key, idx),
        "key": key,
        "cycle": idx,
        "repo": str(meta.get("repo") or ""),
        "branch": str(meta.get("branch") or ""),
        "ticket": str(meta.get("ticket") or ""),
        "name": str(meta.get("name") or ""),
        "pr": str(meta.get("pr") or ""),
        "sessions": sessions,
        "status": status,
        "start": start,
        "commit_at": commit_at,
        "estimated": bool(estimated),
        "end": end,
        "lead_to_commit": elapsed if commit_at is not None else None,
        "active_to_commit": (
            round(sum(v for k, v in to_commit.items() if k not in INACTIVE), 1)
            if commit_at is not None
            else None
        ),
        "elapsed": elapsed,
        "to_commit": {k: round(v, 1) for k, v in to_commit.items()},
        "after_commit": {k: round(v, 1) for k, v in after.items()},
        "hook_runs": runs,
        "hook_failures": fails,
        "timeline": (
            [
                {
                    "ph": sg["ph"],
                    "start": round(sg["start"], 1),
                    "end": round(sg["end"], 1),
                }
                for sg in tl[-400:]
            ]
            if not cut
            else [
                {
                    "ph": sg["ph"],
                    "start": round(sg["start"], 1),
                    "end": round(sg["end"], 1),
                    "at": round(sg["at"], 1),
                }
                for sg in _compress(tl, cut)[-400:]
            ]
        ),
        **({"your_time_s": round(mine, 1)} if skip else {}),
        **({"hidden_s": round(hid, 1)} if hide else {}),
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


def phase_stats(items: List[dict], field: str = "to_commit") -> List[dict]:
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


def advice(
    items: List[dict],
    stats: List[dict],
    skip: Iterable[str] = (),
    after: Iterable[dict] = (),
) -> List[dict]:
    """What to change, ranked by the time at stake: ``{phase, level, title,
    detail, at_stake_s, share}``. Only phases worth at least 5% of time to
    commit (or a hook failure rate that matters) say anything; a ``skip``
    phase (the person's own time, when it isn't counted) never does. The
    handoffs AFTER the commit (``after``, their own stats) are never part of
    time to commit: they get one context line when they add up."""
    skip = frozenset(skip)
    by = {r["phase"]: r for r in stats if r["phase"] not in skip}
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
            "Work waits on your next prompt",
            "Between the agent's turns, units sit %s (median per unit) before anything moves — %d%% of time to "
            "commit. Queue the next prompt while the agent works, or put the whole task (acceptance criteria, "
            "files to touch, “commit when done”) in the first prompt so it runs to the commit in one go."
            % (_fmt(r["median_s"]), round(r["share"] * 100)),
        )
    if big("question"):
        r = by["question"]
        add(
            "question",
            "Agents are blocked on your answers",
            "Questions and permission prompts hold units %s per unit (%d%% of time to commit). Front-load the "
            "prompt (acceptance criteria, files to touch) and allow-list the tools it keeps asking about."
            % (_fmt(r["median_s"]), round(r["share"] * 100)),
        )
    runs = sum(it.get("hook_runs", 0) for it in items)
    fails = sum(it.get("hook_failures", 0) for it in items)
    if big("precommit", 0.03):
        r = by["precommit"]
        per_run = r["total_s"] / runs if runs else r["median_s"]
        add(
            "precommit",
            "Pre-commit hooks are slow",
            "Hooks take %s per commit across %d commit%s (%d%% of time to commit). Move slow checks (the test "
            "suite, type checks over the whole tree) to pre-push or CI, and run only changed-file hooks at commit."
            % (
                _fmt(per_run),
                runs,
                "" if runs == 1 else "s",
                round(r["share"] * 100),
            ),
        )
    if runs and fails and (fails / runs >= 0.2 or big("hook_fix", 0.03)):
        r = by.get("hook_fix") or {"median_s": 0.0, "share": 0.0}
        add(
            "hook_fix",
            "Commits keep failing their hooks",
            "%d of %d commits failed pre-commit (%d%%), costing %s per unit to recover. Put the formatter/linter "
            "commands in the agent's instructions (CLAUDE.md/AGENTS.md) so it runs them before committing."
            % (fails, runs, round(100 * fails / runs), _fmt(r["median_s"])),
        )
    if big("setup"):
        r = by["setup"]
        add(
            "setup",
            "Workspace setup is slow",
            "Provisioning and setup commands take %s per unit. Use worktrees over full clones, and cache "
            "dependency installs in the setup script." % _fmt(r["median_s"]),
        )
    if big("limit"):
        r = by["limit"]
        add(
            "limit",
            "Usage limits stall work",
            "Units sat %s at a usage limit (%d%% of time to commit). Spread work across accounts/providers or "
            "schedule big runs for when the window resets (the Usage Optimizer shows when)."
            % (_fmt(r["total_s"]), round(r["share"] * 100)),
        )
    if big("away", 0.15):
        r = by["away"]
        add(
            "away",
            "Agents finish and sit idle while you're away",
            "%s of finished-but-idle time (%d%% of time to commit) happened with nobody at the keyboard. Give "
            "sessions an autopilot depth, or queue the next prompt / a template run so they keep going."
            % (_fmt(r["total_s"]), round(r["share"] * 100)),
            level="info",
        )
    if big("agent", 0.5):
        r = by["agent"]
        add(
            "agent",
            "Agent time dominates",
            "The agent itself is %d%% of time to commit (%s per unit). Split tickets smaller and run them in "
            "parallel, or use a faster model for routine items."
            % (round(r["share"] * 100), _fmt(r["median_s"])),
            level="info",
        )
    if big("untracked", 0.2):
        add(
            "untracked",
            "Much of the time wasn't recorded",
            "MindFlock wasn't running for a large part of these units, so the breakdown is partial.",
            level="info",
        )
    # After the commit: context, never a time-to-commit bottleneck.
    hand = {r["phase"]: r for r in after if r["phase"] in ("to_push", "to_pr")}
    tot = sum(r["total_s"] for r in hand.values())
    if tot >= 15 * 60:
        phase = max(hand, key=lambda p: hand[p]["total_s"])
        out.append(
            {
                "phase": phase,
                "level": "info",
                "title": "Committed work waits to ship",
                "detail": "After their commit, units waited %s in total to be pushed or get a PR. It isn't part "
                "of time to commit; if you want commits to ship on their own, set the autopilot depth to "
                "“push” or “pr”." % _fmt(tot),
                "at_stake_s": round(tot, 1),
                "share": round(sum(hand[p]["share"] for p in hand), 4),
            }
        )
    # Things to fix first (by the time at stake), then context.
    out.sort(key=lambda a: (a["level"] == "info", -a["at_stake_s"]))
    return out


def weekly_trend(items: List[dict], now: float, weeks: int) -> List[dict]:
    """Median time to commit per week (by when the commit landed), oldest
    first."""
    out = []
    week = 7 * 86400.0
    for w in range(weeks - 1, -1, -1):
        b = now - w * week
        a = b - week
        vals = [
            it["lead_to_commit"]
            for it in items
            if it.get("commit_at") is not None and a <= it["commit_at"] < b
        ]
        out.append(
            {
                "start": a,
                "end": b,
                "items": len(vals),
                "median_to_commit": (
                    round(statistics.median(vals), 1) if vals else None
                ),
            }
        )
    return out


def report(
    rows: List[dict],
    now: Optional[float] = None,
    period: float = 7 * 86400.0,
    live_sessions: Optional[Iterable[str]] = None,
    include_me: bool = False,
    hidden: Iterable[str] = (),
) -> dict:
    """The dashboard payload for the last ``period`` seconds.

    The basis is the units committed in the period (every unit in it when
    none was). ``include_me`` False (the default) leaves the person's own
    time (:data:`PERSON_PHASES`) out of everything — durations, stats,
    groups, advice, the trend and the ``phases`` / ``groups`` lists — and
    adds ``excluded: {phases, total_s}`` (how much of it the basis units
    had). True is the full wall clock, the report unchanged.

    ``hidden`` phases (the person's own pick of what not to look at) are cut
    out the same way, but stay in ``phases`` so the legend can offer them
    back; the report adds ``hidden: {phases, total_s}``. Empty, the report
    is exactly what it was without it."""
    now = time.time() if now is None else now
    since = now - period
    skip = frozenset() if include_me else PERSON_PHASES
    hide = hidden_phases(hidden) - skip
    all_items = build_items(rows, now, live_sessions, include_me, hide)
    items = [it for it in all_items if it["end"] >= since]
    committed = [
        it
        for it in items
        if it.get("commit_at") is not None and it["commit_at"] >= since
    ]
    basis = committed if committed else items
    stats = phase_stats(basis, "to_commit")
    after_stats = phase_stats(committed, "after_commit")
    leads = [it["lead_to_commit"] for it in committed]
    actives = [it["active_to_commit"] for it in committed]
    first = min((_f(r.get("t")) for r in rows), default=None)
    kept_groups = {GROUP_OF[p] for p in PHASE_IDS if p not in skip}
    out = {
        "now": now,
        "period_s": period,
        "tracking_since": first,
        "phases": [
            {"id": p[0], "label": p[1], "group": p[2], "hint": p[3]}
            for p in PHASES
            if p[0] not in skip
        ],
        "groups": [
            {"id": g, "label": label} for g, label in GROUPS if g in kept_groups
        ],
        "kpi": {
            "items": len(items),
            "committed": len(committed),
            "estimated": sum(1 for it in committed if it.get("estimated")),
            "median_to_commit": (round(statistics.median(leads), 1) if leads else None),
            "p90_to_commit": round(_pct(leads, 0.9) or 0.0, 1) if leads else None,
            "median_active_to_commit": (
                round(statistics.median(actives), 1) if actives else None
            ),
            "in_progress": sum(1 for it in items if it["status"] == "in_progress"),
        },
        "basis": "committed" if committed else "all",
        "to_commit": stats,
        "to_commit_groups": group_stats(stats),
        "after_commit": after_stats,
        "advice": advice(basis, stats, skip | hide, after_stats),
        "trend": weekly_trend(
            all_items, now, max(1, min(13, int(round(period / (7 * 86400.0))) or 1))
        ),
        "items": items[:200],
    }
    if skip:
        out["excluded"] = {
            "phases": [p for p in PHASE_IDS if p in skip],
            "total_s": round(sum(it.get("your_time_s", 0.0) for it in basis), 1),
        }
    if hide:
        out["hidden"] = {
            "phases": [p for p in PHASE_IDS if p in hide],
            "total_s": round(sum(it.get("hidden_s", 0.0) for it in basis), 1),
        }
    return out
