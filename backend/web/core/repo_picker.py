"""Ranked repo suggestions for the folder picker: "which repo did you mean?".

The New Session dialog used to open on the HOME directory and leave a brand-new
user to Browse down a folder tree to reach their own project — several clicks of
navigation before the first session can even start. This module answers the
question that picker is really asking: of all the folders on this machine, which
few is the user most likely to mean? Repos their recent sessions used (most
recent first), the repo the server itself was launched from, then a SHALLOW
sweep of the handful of places people keep code.

Deliberately pure — no FastAPI, no engine, no state imports — so the CLI's init
wizard (which has no server to ask) can offer the same list, and so the ranking
is testable without a running app. The caller supplies the "recent" paths in the
order it wants them ranked, because only the caller knows the history (the
settings store, the live session registry, the closed-session undo store).

Nothing in here raises. A suggestion list is a convenience: an unreadable
directory, a repo that has since been deleted, or a home directory full of
symlinks into a dead network mount must all degrade to a shorter list, never to
an error toast in front of someone who is merely trying to start a session.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from backend.web.core.git_ops import _git_has_commits, _is_git_repo

# Where people actually keep repos. The home directory itself is a root too —
# plenty of machines have ~/myproject and nothing else.
_SCAN_SUBDIRS = (
    "code",
    "projects",
    "dev",
    "src",
    "repos",
    "work",
    "git",
    "workspace",
    "Development",
    "Documents",
)

# Never the repo the user meant, yet present in bulk on real machines. Dotted
# names are skipped wholesale (``.venv`` is listed anyway so the intent reads
# plainly); ``Library`` is the macOS one, which alone holds thousands of dirs.
_SKIP_NAMES = frozenset({"node_modules", "venv", ".venv", "__pycache__", "Library"})

# Hard work bounds. This list is built while the user waits on a dialog, and a
# home directory can hold thousands of folders (a well-used Documents, a
# downloads-turned-junk-drawer): examine at most _MAX_PER_ROOT names under any
# one root and _MAX_SCANNED across all of them, then stop with what we have. A
# missed suggestion costs the user one Browse click; a scan that takes ten
# seconds reads as a broken dialog, which is the failure we are here to remove.
_MAX_PER_ROOT = 60
_MAX_SCANNED = 400


def _has_git_dir(path: str) -> bool:
    """Cheap negative filter: does ``path`` carry a ``.git`` entry at all?

    :func:`_is_git_repo` is the authoritative answer but costs a git subprocess,
    and the nearby sweep looks at hundreds of folders — spawning git for every
    one of them would make this endpoint slower than the tree-walking it
    replaces. Every work tree has a ``.git`` (a directory, or the file a
    worktree/submodule uses), so a folder without one can be dropped for the
    price of a stat and the real check saved for the few candidates left.
    """
    return os.path.exists(os.path.join(path, ".git"))


def _entry(path: str, source: str, *, is_git: bool) -> dict:
    """One suggestion in the shape the picker consumes."""
    return {
        "path": path,
        "name": os.path.basename(path) or path,
        "is_git": is_git,
        "source": source,
    }


def _nearby_candidates(home: str) -> list:
    """Folders under ``home`` that look like repos, depth 1, within the caps.

    Each root contributes itself plus its DIRECT children — no recursion. A deep
    walk of a home directory is exactly the kind of thing that makes a dialog
    feel hung, and a repo nested three levels down is a Browse click, not a
    reason to stat 20,000 paths. Names are examined in sorted order so the caps
    truncate deterministically rather than at the mercy of directory order.
    """
    roots = [home] + [os.path.join(home, name) for name in _SCAN_SUBDIRS]
    found: list = []
    examined = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        if _has_git_dir(root):
            found.append(root)
        try:
            names = sorted(os.listdir(root), key=str.lower)
        except OSError:  # noqa: BLE001 — unreadable or vanished mid-scan: skip it
            continue
        # Drop the names we would never offer BEFORE the budget is spent, not
        # after. "." sorts ahead of every letter and digit, so a home directory
        # with sixty-odd dotted entries — which is every home directory that has
        # been used — handed the entire _MAX_PER_ROOT allowance to ~/.cache and
        # friends and the sweep never reached the repos sitting right beside
        # them. That returned an empty "nearby" tier to exactly the first-run
        # user this module exists for, whose other two tiers are empty too.
        names = [n for n in names if not n.startswith(".") and n not in _SKIP_NAMES]
        for name in names[:_MAX_PER_ROOT]:
            examined += 1
            if examined > _MAX_SCANNED:
                return found
            full = os.path.join(root, name)
            if os.path.isdir(full) and _has_git_dir(full):
                found.append(full)
    return found


def suggest_repos(
    recent_paths: Iterable[str] = (),
    cwd: Optional[str] = None,
    limit: int = 12,
) -> list:
    """The ranked candidate folders for the picker, best guess first.

    Three tiers, in this order: ``recent`` (``recent_paths`` verbatim — the
    caller has already put them in recency order), ``cwd`` (the directory the
    server was started in, when it is a git repo: very often *the* repo the user
    wants), then ``nearby`` (the depth-1 sweep, alphabetical by folder name).

    Deduped by ``os.path.realpath`` keeping the highest-priority occurrence, so
    a repo that is both the launch directory and the last session's folder shows
    once, as "recent". Paths are reported resolved for the same reason: a
    symlinked ~/code/foo and its target are one suggestion, not two. A recent
    path that no longer exists is dropped rather than offered — a suggestion the
    user cannot pick is worse than a shorter list.
    """
    if limit <= 0:
        return []
    out: list = []
    seen: set = set()

    def _add(path: str, source: str, *, is_git: bool) -> None:
        if path in seen:
            return
        seen.add(path)
        out.append(_entry(path, source, is_git=is_git))

    for raw in recent_paths or ():
        if len(out) >= limit:
            return out
        text = str(raw or "").strip()
        if not text:
            continue
        full = os.path.realpath(os.path.expanduser(text))
        if full in seen:
            # Dedup before the git probe, not inside _add. The caller's recency
            # list repeats one path per live session and one per closed one, so a
            # user who works in a single checkout hands us that folder forty
            # times; probing as an argument to _add spawned a rev-parse
            # subprocess for every copy — forty of them, every time the New
            # Session dialog opened, to produce one suggestion.
            continue
        if not os.path.isdir(full):
            continue
        _add(full, "recent", is_git=_is_git_repo(full))

    if cwd and len(out) < limit:
        full = os.path.realpath(os.path.expanduser(str(cwd)))
        # A non-repo cwd is noise (someone ran `mindflock serve` from ~), so the
        # launch directory earns its slot only when it is really a repo.
        if os.path.isdir(full) and _is_git_repo(full):
            _add(full, "cwd", is_git=True)

    if len(out) < limit:
        candidates = _nearby_candidates(os.path.expanduser("~"))
        candidates.sort(key=lambda p: (os.path.basename(p).lower(), p))
        for cand in candidates:
            if len(out) >= limit:
                break
            full = os.path.realpath(cand)
            if full in seen:
                continue
            # The authoritative (subprocess) check runs only on candidates that
            # can still make the list — see _has_git_dir for why that matters.
            if not _is_git_repo(full):
                continue
            _add(full, "nearby", is_git=True)
    return out


def check_repo(path: str) -> dict:
    """Report what one folder the user typed actually is, without judging it.

    The picker calls this on every keystroke, so a path that does not exist yet
    is a normal answer (``exists: False``) and not an error: the user is
    mid-word, or is about to have MindFlock create the folder. ``is_git`` and
    ``has_commits`` are what lets the dialog say up front whether the session
    gets the worktree/diff/PR features, or is a fresh ``git init`` with no HEAD
    to fork a worktree from (see
    :func:`backend.web.core.plain_repo._prepare_plain_repo`, which draws exactly
    that distinction when the session is created).
    """
    text = str(path or "").strip()
    if not text:
        # ``realpath("")`` is the process working directory, so answering a blank
        # path would report on a folder nobody asked about.
        return {
            "path": "",
            "exists": False,
            "is_dir": False,
            "is_git": False,
            "has_commits": False,
        }
    try:
        # Expanduser and nothing more, because that is exactly what the folder is
        # later CREATED with: /api/browse and
        # :func:`backend.web.core.plain_repo._prepare_plain_repo` both stop at
        # expanduser. Expanding ``$HOME/MindFlock`` here reported "exists, git
        # repo, has commits" about one directory while Create resolved the same
        # typed string literally and made a ``$HOME`` folder tree under the
        # server's cwd — a git-less session in a directory nobody asked for,
        # announced as a repo.
        full = os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError):  # a path too malformed to resolve is not a crash
        return {
            "path": text,
            "exists": False,
            "is_dir": False,
            "is_git": False,
            "has_commits": False,
        }
    is_dir = os.path.isdir(full)
    is_git = is_dir and _is_git_repo(full)
    return {
        "path": full,
        "exists": os.path.exists(full),
        "is_dir": is_dir,
        "is_git": is_git,
        "has_commits": is_git and _git_has_commits(full),
    }
