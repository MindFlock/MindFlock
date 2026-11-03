"""The landing strip's data (docs/flights.md "Round 15" → ``objects``, "As
built: round 15 objects"): per repo in the sky, its default branch, the
checks of that branch's head, the last merge's time, and **landed today** —
the flights finished ``done`` whose last ship step merged, in the last 24 h.

``GET /api/sky/landing`` (``routes/sky.py``) answers with :func:`landing`:

    {"repos": [{root, name, branch, head, checks, last_merge_at,
                landed: [{flight, title, pr_url, at}]}], "at": <unix s>}

The repos are the distinct repo roots of this machine's sessions (never a
remote device's row, never a peer's shared folder). The git questions are
cheap local commands; the checks come from the GitHub REST helpers
(:func:`backend.web.core.github_pr.pr_checks`) and are the only network
call, so the whole answer is cached for :data:`CACHE_TTL_S` (a minute) —
the strip polls at the same pace. Everything git- or network-shaped is
guarded: a repo with no origin says ``none`` for its checks, a flight with
no PR has an empty ``pr_url``.
"""

from __future__ import annotations

import asyncio
import copy
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_GIT_TIMEOUT_S = 8
#: How long one answer serves the strip's polls.
CACHE_TTL_S = 60.0
#: "Landed today": finished within this many seconds of now.
LANDED_WINDOW_S = 24 * 3600

_CHECK_TONES = {"ok": "green", "failed": "red", "pending": "pending"}


# --------------------------------------------------------------------------- #
# Repos in the sky
# --------------------------------------------------------------------------- #
def repo_roots(instances) -> List[str]:
    """The distinct repo roots of this machine's sessions, in the order the
    sessions appear: a worktree's main repo, else the session's own folder.
    A remote device's row (``dev::title``) and a peer's shared folder are
    skipped; so is a folder that is gone."""
    out: List[str] = []
    for title, inst in (instances or {}).items():
        if "::" in str(title or ""):
            continue
        if getattr(inst, "PeerShare", ""):
            continue
        root = ""
        try:
            root = inst.GetGitWorktree().GetRepoPath() or ""
        except Exception:  # noqa: BLE001 — raises before Start finished
            root = ""
        if not root:
            try:
                root = inst.GetWorktreePath() or ""
            except Exception:  # noqa: BLE001
                root = ""
        if not root:
            root = str(getattr(inst, "Path", "") or "")
        root = os.path.abspath(root) if root else ""
        if root and os.path.isdir(root) and root not in out:
            out.append(root)
    return out


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #
def _git(root: str, *args: str) -> str:
    """``git -C root args…``'s stdout, stripped; "" on any failure."""
    try:
        cp = subprocess.run(
            ["git", "-C", root, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def default_branch(root: str) -> str:
    """The repo's default branch: what ``origin/HEAD`` points at, else
    ``main`` or ``master`` when one exists, else the checked-out branch."""
    ref = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        return ref.split("/", 1)[1] if ref.startswith("origin/") else ref
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", "refs/heads/" + name):
            return name
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return "" if head == "HEAD" else head


def branch_ref(root: str, branch: str) -> str:
    """``origin/<branch>`` when the remote branch is known (what the forge
    sees), else the local branch."""
    if not branch:
        return ""
    if _git(root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/" + branch):
        return "origin/" + branch
    return branch


def head_of(root: str, ref: str) -> str:
    return _git(root, "rev-parse", ref) if ref else ""


def last_merge(root: str, ref: str) -> Tuple[Optional[float], Optional[str]]:
    """When the branch last took a merge commit (unix seconds) and ``"merge"``;
    with no merge commit, its last commit and ``"commit"``; ``(None, None)``
    for an empty branch. The kind lets the strip say "merged" only when a
    merge is what it saw (a fresh repo has commits, not merges)."""
    if not ref:
        return None, None
    for kind, args in (
        ("merge", ("log", "-1", "--merges", "--format=%ct", ref)),
        ("commit", ("log", "-1", "--format=%ct", ref)),
    ):
        out = _git(root, *args)
        if out:
            try:
                return float(out.splitlines()[0]), kind
            except ValueError:
                continue
    return None, None


def last_merge_at(root: str, ref: str) -> Optional[float]:
    """When the branch last took a merge commit (unix seconds); with no
    merge commit, its last commit; None for an empty branch."""
    return last_merge(root, ref)[0]


def repo_facts(root: str) -> Dict[str, Any]:
    """The git half of a repo's row (no network)."""
    branch = default_branch(root)
    ref = branch_ref(root, branch)
    at, kind = last_merge(root, ref)
    return {
        "root": root,
        "name": os.path.basename(root.rstrip(os.sep)) or root,
        "branch": branch,
        "head": head_of(root, ref),
        "last_merge_at": at,
        "last_merge_kind": kind,
    }


# --------------------------------------------------------------------------- #
# Landed today
# --------------------------------------------------------------------------- #
def last_done_ship(flight: dict) -> Optional[dict]:
    """The flight's last ship step that is done, or None."""
    states = (
        flight.get("step_states") if isinstance(flight.get("step_states"), dict) else {}
    )
    steps = flight.get("steps") if isinstance(flight.get("steps"), list) else []
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get("kind") != "ship":
            continue
        st = states.get(str(step.get("id") or ""), {})
        if isinstance(st, dict) and st.get("state") == "done":
            return step
    return None


def _pr_url(flight: dict, step: dict) -> str:
    states = (
        flight.get("step_states") if isinstance(flight.get("step_states"), dict) else {}
    )
    st = states.get(str(step.get("id") or ""), {})
    detail = (
        st.get("detail")
        if isinstance(st, dict) and isinstance(st.get("detail"), dict)
        else {}
    )
    for key in ("pr_url", "url"):
        v = detail.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def landed(
    flights: List[dict], root: str, now: float, window: float = LANDED_WINDOW_S
) -> List[dict]:
    """The flights of ``root`` that landed in the window: finished ``done``,
    their last done ship step's lane ``merge``; newest first."""
    out: List[dict] = []
    for f in flights or []:
        if not isinstance(f, dict) or f.get("state") != "done":
            continue
        try:
            fin = float(f.get("finished") or 0.0)
        except (TypeError, ValueError):
            continue
        if not fin or fin < now - window:
            continue
        if root and os.path.abspath(str(f.get("repo_root") or "")) != root:
            continue
        step = last_done_ship(f)
        if step is None:
            continue
        cfg = step.get("config") if isinstance(step.get("config"), dict) else {}
        if str(cfg.get("lane") or "push") != "merge":
            continue
        out.append(
            {
                "flight": str(f.get("id") or ""),
                "title": str(f.get("session") or ""),
                "pr_url": _pr_url(f, step),
                "at": fin,
            }
        )
    out.sort(key=lambda r: -r["at"])
    return out


def checks_tone(verdict: str) -> str:
    """``pr_checks``'s verdict as the strip's tone: ok → green, failed → red,
    pending → pending, none / unknown → none."""
    return _CHECK_TONES.get(str(verdict or ""), "none")


# --------------------------------------------------------------------------- #
# The answer, cached
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"key": None, "at": 0.0, "data": None}


def _cached(key: Tuple[str, ...], now: float) -> Optional[dict]:
    with _LOCK:
        if (
            _CACHE["key"] == key
            and _CACHE["data"] is not None
            and now - _CACHE["at"] < CACHE_TTL_S
        ):
            return copy.deepcopy(_CACHE["data"])
    return None


def _remember(key: Tuple[str, ...], now: float, data: dict) -> None:
    with _LOCK:
        _CACHE.update(key=key, at=now, data=copy.deepcopy(data))


def forget() -> None:
    """Drop the cache (tests)."""
    with _LOCK:
        _CACHE.update(key=None, at=0.0, data=None)


async def _checks(root: str, head: str) -> str:
    if not head:
        return "none"
    try:
        from backend.web.core import github_pr as _gh

        return checks_tone(await _gh.pr_checks(root, head))
    except Exception:  # noqa: BLE001 — the network is never a reason to fail the strip
        return "none"


async def landing(
    instances, flights: Optional[List[dict]] = None, now: Optional[float] = None
) -> dict:
    """The strip's answer for these sessions and flights (see the module
    docstring); one answer per set of repos per :data:`CACHE_TTL_S`."""
    now = time.time() if now is None else now
    roots = repo_roots(instances)
    key = tuple(roots)
    hit = _cached(key, now)
    if hit is not None:
        return hit
    if flights is None:
        from backend.web.core import flights as _flights

        flights = await asyncio.to_thread(_flights.list_flights)
    facts = await asyncio.to_thread(lambda: [repo_facts(r) for r in roots])
    tones = await asyncio.gather(*(_checks(f["root"], f["head"]) for f in facts))
    repos = []
    for f, tone in zip(facts, tones):
        repos.append({**f, "checks": tone, "landed": landed(flights, f["root"], now)})
    data = {"repos": repos, "at": now}
    _remember(key, now, data)
    return data
