"""Session display aliases (tab renames), synced from the SPA.

Renaming a tab is a client-side act: the sidebar stores ``title -> label`` in
localStorage (``mf_aliases``, frontend/src/state/store.ts) and every React
surface renders through ``displayName()``. The server never needed to know —
until ntfy: a phone push that says "shortcut-21129 needs your input" about the
tab renamed to "(tix) rebuild-scans" makes the reader hunt for a session that
doesn't appear to exist (the same reasoning as EventToasts' displayName rule).

So the SPA mirrors every rename here (``POST /api/aliases``, fire-and-forget
delta) and the ntfy channel formats titles through :func:`label_for`. The
browser remains the source of truth for its OWN rendering — this store only
feeds server-originated text. Deltas (not whole maps) so two browsers with
different localStorage don't clobber each other's renames; last writer wins
per title, which is also what the user watching two screens expects.

Persisted as ``<config dir>/session_aliases.json`` (the budget_overrides.json
pattern): one process writes, so an in-memory dict loaded once is safe.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional

from backend import config, log

_LOCK = threading.Lock()
_ALIASES: Optional[Dict[str, str]] = None  # title -> display label


def _path() -> str:
    return os.path.join(config.GetConfigDir(), "session_aliases.json")


def _load() -> Dict[str, str]:
    global _ALIASES
    if _ALIASES is None:
        try:
            with open(_path()) as f:
                d = json.load(f)
            _ALIASES = (
                {str(k): str(v) for k, v in d.items() if v}
                if isinstance(d, dict)
                else {}
            )
        except (OSError, ValueError):
            _ALIASES = {}
    return _ALIASES


def _persist() -> None:
    try:
        with open(_path(), "w") as f:
            json.dump(_ALIASES or {}, f)
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to save session aliases: %v", err)


def set_alias(title: str, alias: str) -> None:
    """Record one rename (empty ``alias`` = the rename was cleared)."""
    title = str(title or "").strip()
    if not title:
        return
    with _LOCK:
        aliases = _load()
        if alias:
            aliases[title] = str(alias)
        else:
            aliases.pop(title, None)
        _persist()


def merge(aliases: Dict[str, str]) -> None:
    """Fold in a browser's whole alias map — set/overwrite only, never delete.

    Used by the SPA's boot-time seed, which exists for renames made before the
    server mirror did (they live only in that browser's localStorage until the
    next rename). Merge-only so one browser's seed can't erase renames another
    browser synced; clearing a rename goes through :func:`set_alias` deltas.
    """
    if not isinstance(aliases, dict):
        return
    with _LOCK:
        store = _load()
        changed = False
        for title, alias in aliases.items():
            title, alias = str(title or "").strip(), str(alias or "")
            if title and alias and store.get(title) != alias:
                store[title] = alias
                changed = True
        if changed:
            _persist()


def drop(title: str) -> None:
    """Forget a deleted session's alias (missing is fine)."""
    with _LOCK:
        aliases = _load()
        if title in aliases:
            aliases.pop(title, None)
            _persist()


def label_for(title: str) -> str:
    """The session's display label: its alias if one was synced, else the raw
    title. Never raises — display naming must never break a push."""
    try:
        with _LOCK:
            return _load().get(str(title or "")) or str(title or "")
    except Exception:  # noqa: BLE001
        return str(title or "")


# --------------------------------------------------------------------------- #
# The label a session wears when nobody renamed it
# --------------------------------------------------------------------------- #
# A PORT, and it has to stay one. `frontend/src/lib/sessionLabel.ts` is the
# original and the sidebar renders through it; this exists so a push about a
# session can call it what the rail calls it. Sessions a pipeline created are
# titled by a machine slug — `sc-12345`, `pr-app-42` — which says nothing about
# what the work IS, and the feature name is already sitting in the branch. A
# notification naming a window the reader cannot find in the rail is the one
# mistake this channel cannot make.
#
# Keep the two in step: `tests/unit/test_aliases.py` pins the same examples the
# TypeScript doc comment carries.


def _feature_branch_name(branch: str, slug: str) -> str:
    """The ``<name>`` of a ``feature/<slug>/<name>`` branch, else ``""``."""
    prefix = "feature/%s/" % slug
    return branch[len(prefix) :] if slug and branch.startswith(prefix) else ""


def _branch_tail(branch: str) -> str:
    """Last path segment of a branch ref — a PR's ``fix/login-crash`` reads as
    "login-crash"."""
    parts = [p for p in branch.split("/") if p]
    return parts[-1] if parts else ""


def _split_kind(title: str):
    """``(kind, slug)`` for a title a pipeline made, else ``None``."""
    if title.startswith("pr-"):
        return ("pr", title[3:])
    if title.startswith("issue-"):
        return ("iss", title[6:])
    return None


def session_label(title: str, branch: str = "") -> str:
    """The sidebar's label for a session: ``(tix) add-dark-mode/sc-12345``.

    Titles no pipeline created come back untouched — a hand-made "my-refactor"
    session has nothing to reformat. Display only: ``title`` remains the identity
    every route, tmux name and workspace dir is keyed by.
    """
    title, branch = str(title or ""), str(branch or "")
    if not title:
        return ""
    split = _split_kind(title)
    # A ticket session is titled by its provider slug with no kind prefix of its
    # own; the branch it owns (feature/<title>/…) is what identifies it as one.
    ticket_name = "" if split else _feature_branch_name(branch, title)
    if not split and not ticket_name:
        return title
    kind, slug = split if split else ("tix", title)
    if not slug:
        return title
    # PR/issue sessions: the feature name comes from the branch under their own
    # slug when the pipeline made one, else from the PR's head ref.
    name = (
        (_feature_branch_name(branch, title) or _branch_tail(branch))
        if split
        else ticket_name
    )
    # A branch that just restates the slug adds nothing worth the width.
    if name and name != slug and name != title:
        return "(%s) %s/%s" % (kind, name, slug)
    return "(%s) %s" % (kind, slug)


def display_name(title: str, branch: str = "") -> str:
    """What this session is CALLED — the rename, else the sidebar's label, else
    the raw title. The server-side twin of ``frontend/src/lib/windowName.ts``.

    :func:`label_for` answers the first and last of those three and is kept for
    callers that have no branch to offer; this is the one a notification wants,
    because "whatever the window is called" is the only name the reader can act
    on. Never raises — display naming must never break a push.
    """
    try:
        alias = label_for(title)
        if alias and alias != str(title or ""):
            return alias
        return session_label(title, branch) or str(title or "")
    except Exception:  # noqa: BLE001
        return str(title or "")


def all_aliases() -> Dict[str, str]:
    with _LOCK:
        return dict(_load())
