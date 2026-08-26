"""Force-starts that have been accepted but are not sessions yet.

A forced PR review / issue / ticket answers 202 and then does the slow work
(clone the head, run setup, launch the agent) before it can register an
instance — tens of seconds during which the sidebar showed nothing at all and
the start looked like it had failed. Every accepted start is recorded here the
moment the request comes in, and :func:`rows` renders each as a provisioning
row the UI already knows how to draw (status ``loading`` + stage
``provisioning``, exactly like a session whose ``Start`` is still running).
The entry is dropped as soon as the real instance exists, so the row is
replaced rather than duplicated.

Three consumers, which is why this is its own module rather than more of
``server.py``:

* ``GET /api/instances`` — appends :func:`rows` to the sessions snapshot.
* The three force-start endpoints — the registry doubles as their
  double-click guard, and the panels' ``has_session`` annotation reads it.
* The ingestion addon's status — :func:`provisioning_kinds` is what turns the
  sidebar bars' dots green while work is being brought in by THIS server
  (the pipeline's own beacon only knows about the pipeline's own queue).
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Set

from backend import providers
from backend.session.storage import Loading
from backend.web.core import autopilot as _autopilot
from backend.web.core.engine import get_engine

# title -> {"kind": "pr"|"iss"|"tix", "since": epoch, **display meta}
_PENDING: Dict[str, dict] = {}
_LOCK = threading.Lock()


def add(title: str, kind: str, **meta) -> None:
    """Record (or enrich) an accepted force-start.

    Called twice per start: once from the request path with only what the
    payload knows, then again once the upstream lookup has filled in the
    branch. ``since`` survives the second call so it stays the moment the user
    actually asked for the work.
    """
    if not title:
        return
    with _LOCK:
        prev = _PENDING.get(title) or {}
        _PENDING[title] = {
            "kind": kind,
            "since": prev.get("since") or time.time(),
            **{k: v for k, v in prev.items() if k not in ("kind", "since")},
            **meta,
        }


def drop(title: str) -> None:
    """Forget a start (it registered its session, or it failed). Tolerates ""
    so callers can drop an unregistered provisional title unconditionally."""
    if not title:
        return
    with _LOCK:
        _PENDING.pop(title, None)


def has(title: str) -> bool:
    """Whether a start for ``title`` is in flight — the double-click guard."""
    return bool(title) and title in _PENDING


def snapshot() -> Dict[str, dict]:
    with _LOCK:
        return {t: dict(m) for t, m in _PENDING.items()}


def session_kind(title: str, branch: str) -> str:
    """``"pr"`` | ``"iss"`` | ``"tix"`` | ``""`` for a session title+branch.

    The pipeline titles PR sessions ``pr-<slug>`` and issue sessions
    ``issue-<slug>``; a ticket session is titled by its bare provider slug and
    is recognised by the branch it owns. Mirrors the sidebar's own label logic
    (``frontend/src/lib/sessionLabel.ts``) — keep the two in step.
    """
    if not title:
        return ""
    if title.startswith("pr-"):
        return "pr"
    if title.startswith("issue-"):
        return "iss"
    if branch.startswith(f"feature/{title}/"):
        return "tix"
    return ""


def _is_provisioning(inst) -> bool:
    """A session that exists but is still being provisioned (same test the
    sidebar snapshot uses to report stage ``provisioning``)."""
    try:
        return not inst.Started() and inst.Status == Loading
    except Exception:  # noqa: BLE001 — a half-built instance is not our problem
        return False


def provisioning_kinds(engine=None) -> Set[str]:
    """Kinds of work being brought in by THIS server right now.

    A kind is included while a force-start of it has no session yet, or while
    the session it created is still provisioning. This is what makes the
    sidebar bars' dots green for work started from the UI: the pipeline's
    activity beacon only reports the pipeline's own queue, so without this a
    forced ticket provisioned for two minutes with the light showing idle.
    """
    kinds = {(m.get("kind") or "") for m in snapshot().values()}
    engine = engine if engine is not None else get_engine()
    for inst in list(engine.instances.values()):
        if not _is_provisioning(inst):
            continue
        kinds.add(session_kind(inst.Title or "", getattr(inst, "Branch", "") or ""))
    return {k for k in kinds if k}


def rows(engine=None) -> list:
    """Sidebar entries for accepted starts with no instance yet.

    Same key set as ``_instance_json`` plus ``pending``, which tells the UI the
    row has no tmux session behind it yet (so it offers no actions on it).
    """
    engine = engine if engine is not None else get_engine()
    program = engine.default_program()
    provider = providers.resolve(program).name
    out = []
    for title, meta in snapshot().items():
        # The instance exists now: the real snapshot entry is the truth.
        if title in engine.instances:
            continue
        out.append(
            {
                "title": title,
                "branch": meta.get("branch", ""),
                "repo": meta.get("repo", ""),
                "folder": "",
                "folder_label": "",
                "program": program,
                "provider": provider,
                # Auth profile: a pending row has no instance yet, so it can
                # only report "not pinned". Present-but-empty, not absent —
                # these rows must carry the same key set as a live row.
                "profile_id": "",
                "profile_effective": "",
                "profile_label": "",
                "profile_model": "",
                "path": "",
                "status": "loading",
                "started": False,
                "tmux_name": "",
                "provisioned": True,
                "workspace_strategy": meta.get("workspace_strategy", "clone"),
                "in_place": False,
                "diff_stat": None,
                "workspace_missing": False,
                "has_origin": False,
                "stage": "provisioning",
                "pr_url": None,
                "merge_state": None,
                "failed_step": None,
                "failed_hook": None,
                # A provisioning row must carry it too: intake arms BEFORE the
                # session exists, so without this the fast-track toggle showed OFF
                # for the whole clone+provision window — exactly when you would
                # want to change your mind and turn it off.
                "autopilot": _autopilot.dto(title),
                "queue": None,
                "tokens": 0,
                "tokens_in": 0,
                "tokens_cache_read": 0,
                "tokens_cache_write": 0,
                "tokens_ctx": 0,
                "tokens_ctx_window": 0,
                "tokens_cost": 0.0,
                "tokens_model": "",
                "budget": None,
                "activity": "offline",
                "activity_since": 0.0,
                "last_turn": None,
                "last_prompt": None,
                "last_prompt_full": None,
                "setup": None,
                "check": None,
                "ports": None,
                "pending": True,
            }
        )
    return out


def cached_row(cache: dict, list_key: str, match) -> Optional[dict]:
    """The cached panel row for one item, or ``None`` if nothing is cached.

    The panels annotate each row with everything an action on it needs (the
    session title it maps to, the branch it takes, where it provisions), so a
    request that acts on a row the user just saw can read that row instead of
    re-deriving it from the upstream — which is both slow (a provider round
    trip) and a second implementation waiting to drift.
    """
    entry: Optional[tuple] = cache.get("v")
    data = entry[1] if entry else None
    if not isinstance(data, dict):
        return None
    for item in data.get(list_key) or []:
        if isinstance(item, dict) and match(item):
            return item
    return None


def cached_session_title(cache: dict, list_key: str, match) -> str:
    """The session title a settings panel's cached list already computed for an
    item, or ``""`` when nothing usable is cached.

    The panels annotate every entry with ``session`` — the exact output of the
    module-level ``session_title()`` the force-start will use — so a row
    registered from it can't disagree with the session that turns up. Reading
    it here is what lets the provisioning row go up BEFORE the upstream lookup
    (a GitHub/provider round trip) that the title would otherwise wait on.
    Slug derivation is deliberately NOT reimplemented: providers set their own
    (Shortcut hardcodes ``sc-<id>``), so a second implementation would drift.
    """
    row = cached_row(cache, list_key, match)
    return str(row.get("session") or "") if row else ""
