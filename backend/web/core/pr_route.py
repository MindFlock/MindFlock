"""The route ("flight plan") a pull request flies — ONE answer for every way a
PR is started (docs/flights.md "How a ticket gets its route", "Which route a
PR flies"): Intake → Pull requests' Begin review (``pr_review.launch``), the
pipeline's automatic PR intake (handed to the server by
``session_runner.SessionRunner.hand_to_server``, or armed in the pipeline
itself when the server is out of reach), and Run the PR review
(``pr_review.run_pr_review``).

After a start's own pick (``server._intake_plan``):

1. the repo's own PR route (``github.repo_settings[repo].plan``), while that
   saved route exists;
2. the repo's **Goes as far as** rung: Commit / Push / Open PR fly the
   built-in cut there (``builtin:pr@<rung>``), **Agent only** (``agent``)
   flies no route;
3. the flock's PR route, ``flights.pr_review_plan``: a saved route (by id, or
   its name), or ``"off"`` = no route — a PR is then started with the plain
   review prompt, as before PRs had routes;
4. the built-in default, :data:`DEFAULT_PLAN` (*Pull request*: the agent
   addresses the comments → Commit → Push → Reviews settle → CI checks), so a
   user who configured nothing still has their PR carried on.

Stdlib + ``flights`` only: the ticket pipeline (a separate process) imports
it too. Settings are read through :func:`_settings`, which drops the
per-process cache first, so a route picked in the UI applies to the next PR
the pipeline sees. Never raises: an unreadable store is "no route".
"""

from __future__ import annotations

import logging
from typing import Optional

_logger = logging.getLogger(__name__)

#: The built-in route a PR flies when nothing is configured.
DEFAULT_PLAN = "builtin:pr"
#: ``flights.pr_review_plan`` = this → PRs fly no route (the plain prompt).
OFF = "off"
#: Spellings of :data:`OFF` a hand-edited settings file may hold.
_OFF_WORDS = frozenset({"off", "none", "no", "false"})


def _settings(settings=None):
    """``settings.json`` as it is on disk NOW (``settings`` when the caller
    already has a fresh copy). The cache is per-process: the pipeline must not
    keep reading what was saved before its last PR."""
    if settings is not None:
        return settings
    from backend.config import settings as _st

    _st.invalidate()
    return _st.load_settings()


def is_off(value) -> bool:
    """Whether a ``flights.pr_review_plan`` value means "no route"."""
    return str(value or "").strip().lower() in _OFF_WORDS


def find_plan(ref: str) -> Optional[dict]:
    """A saved route by id, else by name (case aside), or None."""
    from backend.web.core import flights as _flights

    ref = str(ref or "").strip()
    if not ref:
        return None
    plan = _flights.get_plan(ref)
    if plan is not None:
        return plan
    low = ref.lower()
    return next(
        (
            p
            for p in _flights.list_plans()
            if str(p.get("name") or "").strip().lower() == low
        ),
        None,
    )


def flock_plan(settings=None) -> str:
    """The flock's PR route: ``flights.pr_review_plan`` resolved to a saved
    route's id; ``""`` when it is :data:`OFF`; :data:`DEFAULT_PLAN` when it is
    unset or names a route no longer saved (logged)."""
    try:
        ref = str(_settings(settings).flights.pr_review_plan or "").strip()
    except Exception:  # noqa: BLE001 — unreadable settings: the default
        ref = ""
    if is_off(ref):
        return ""
    try:
        if ref:
            plan = find_plan(ref)
            if plan is not None and plan.get("steps"):
                return str(plan["id"])
            _logger.warning(
                "PR route %r (flights.pr_review_plan) is not a saved route — "
                "PRs fly the built-in %s",
                ref,
                DEFAULT_PLAN,
            )
        return DEFAULT_PLAN if find_plan(DEFAULT_PLAN) is not None else ""
    except Exception:  # noqa: BLE001 — an unreadable store is "no route"
        return ""


def cap_depth(depth) -> str:
    """A repo card's stored depth as intake reads it (``server.
    _cap_source_depth``): ``""`` (unset, ``off``), ``agent``, or a rung a
    source may default to (``merge`` and junk read as ``pr``)."""
    from backend.web.core import autopilot as _autopilot

    d = _autopilot.normalize_depth(depth)
    if d in ("", "off"):
        return ""
    return d if d in _autopilot.SOURCE_DEPTHS else "pr"


def plan_for(repo_plan: str = "", repo_depth: str = "", settings=None) -> str:
    """The route a PR flies when its start picked none: the repo's ``plan``
    (while saved), else its capped ``depth`` (a rung → the built-in cut there,
    ``agent`` → no route), else :func:`flock_plan`. ``""`` = no route."""
    from backend.web.core import flights as _flights

    try:
        repo_plan = str(repo_plan or "").strip()
        if repo_plan and _flights.get_plan(repo_plan) is not None:
            return repo_plan
        if repo_plan:
            _logger.warning(
                "PR route %s (set on the repo) is not a saved route — "
                "using the next setting",
                repo_plan,
            )
        depth = cap_depth(repo_depth)
        if depth:
            return _flights.plan_for_depth("pr", depth)
    except Exception:  # noqa: BLE001 — an unreadable store is "no route"
        return ""
    return flock_plan(settings)


def _repo_block(repo: str, settings=None):
    """``(settings, repo's github.repo_settings block)`` — matched exactly,
    then case aside; ``(None, {})`` when unreadable."""
    try:
        st = _settings(settings)
        table = st.github.repo_settings or {}
        block = table.get(repo)
        if block is None:
            want = str(repo or "").strip().lower()
            block = next(
                (v for k, v in table.items() if str(k).strip().lower() == want), None
            )
        return st, (block if isinstance(block, dict) else {})
    except Exception:  # noqa: BLE001
        return None, {}


def for_repo(repo: str, settings=None) -> str:
    """:func:`plan_for` with ``repo``'s own card read from the settings."""
    st, block = _repo_block(repo, settings)
    return plan_for(
        str(block.get("plan") or ""), str(block.get("depth") or ""), settings=st
    )


def for_repo_plan(repo: str, settings=None) -> str:
    """``repo``'s own PR route (its card's ``plan``) while it is saved, else
    ``""`` — the repo step of :func:`plan_for` alone."""
    from backend.web.core import flights as _flights

    _, block = _repo_block(repo, settings)
    ref = str(block.get("plan") or "").strip()
    try:
        return ref if ref and _flights.get_plan(ref) is not None else ""
    except Exception:  # noqa: BLE001
        return ""
