"""Settings → Workspace → "Per-repo branches": what each known repository uses.

``GET /api/repos/bases`` answers, per repository MindFlock knows about (the
ones live sessions work in, the configured ``repository.url``, and any with a
``repository.repo_branches`` row), the base branch new sessions fork from and
the branch Open PR targets — each with WHY, from :mod:`backend.repo_base`:
``"main · the repository's default"``, ``"staging · your setting"``, and the
note when a flat setting was passed over because that repository lacks it.

Read-only: changing a repository's branches is the screen's existing rows
(``repository.repo_branches``). Blocking (git, at most one cached
``ls-remote`` per remote): call it off the event loop.
"""

from __future__ import annotations

import os
from typing import Dict, List

from backend import repo_base

#: Most repositories one answer covers (each may cost an ``ls-remote``).
MAX_REPOS = 12


def _side(choice: repo_base.BaseChoice) -> dict:
    return {
        "branch": choice.branch,
        "source": choice.source,
        "why": choice.why,
        "note": choice.note,
        "error": choice.error,
    }


def _known(engine, settings) -> Dict[str, str]:
    """``{slug or path: location}``: one location per repository, a local
    checkout preferred (its refs answer without the network)."""
    from backend.session.provisioned import repo_slug_for

    out: Dict[str, str] = {}
    try:
        insts = list((getattr(engine, "instances", None) or {}).values())
    except Exception:  # noqa: BLE001
        insts = []
    for inst in insts:
        loc = ""
        for cand in (getattr(inst, "Path", ""), _worktree(inst)):
            if cand and os.path.isdir(str(cand)):
                loc = str(cand)
                break
        if not loc:
            continue
        key = repo_slug_for(loc) or os.path.realpath(loc)
        out.setdefault(key, loc)
        if len(out) >= MAX_REPOS:
            return out
    url = str(getattr(settings.repository, "url", "") or "").strip()
    if url:
        out.setdefault(repo_slug_for(url) or url, url)
    for slug in (settings.repository.repo_branches or {}).keys():
        if len(out) >= MAX_REPOS:
            break
        if not any(k.lower() == str(slug).lower() for k in out):
            out[str(slug)] = ""
    return out


def _worktree(inst) -> str:
    try:
        return str(inst.GetWorktreePath() or "")
    except Exception:  # noqa: BLE001
        return ""


def list_bases(engine) -> dict:
    """``{"repos": [{repo, base: {...}, pr: {...}}]}`` — see the module doc."""
    from backend.config import settings as _s

    st = _s.load_settings()
    rows: List[dict] = []
    for repo, loc in _known(engine, st).items():
        if loc:
            base = repo_base.resolve(loc, "base", settings=st)
            # Open PR's own fallback after the settings is the session's start
            # point; for a repository as a whole that is its base branch.
            pr = repo_base.resolve(loc, "pr", settings=st, fallback=False)
            if not pr.branch:
                pr = repo_base.BaseChoice(base.branch, base.source, pr.skipped)
        else:
            # A per-repo row with no checkout here: the setting is the answer.
            per = _s.repo_branches_for(repo, st)
            base = repo_base.BaseChoice(per[0], repo_base.SOURCE_REPO)
            pr = repo_base.BaseChoice(per[1], repo_base.SOURCE_REPO)
        rows.append({"repo": repo, "base": _side(base), "pr": _side(pr)})
    rows.sort(key=lambda r: r["repo"].lower())
    return {"repos": rows}
