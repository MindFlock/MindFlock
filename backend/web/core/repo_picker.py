"""Ranked repo suggestions for the folder picker: "which repo did you mean?".

The New Session dialog used to open on the HOME directory and leave a brand-new
user to Browse down a folder tree to reach their own project — several clicks of
navigation before the first session can even start. This module answers the
question that picker is really asking: of all the folders on this machine, which
few is the user most likely to mean? Repos their recent sessions used (most
recent first), the repo the server itself was launched from, then a SHALLOW
sweep of the handful of places people keep code.

That sweep answers "which repo did you mean?" but not "where is the one I am
thinking of?": it is depth-1, so a repo three levels down is invisible to it and
the user is back to typing a full path or clicking through Browse.
:func:`search_repos` is the answer to that second question — the same entries,
found by NAME with a bounded depth-3 walk.

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
import time
from collections import deque
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

# Bounds for the by-name SEARCH below, which is a different job from the sweep
# above and so needs different numbers. The sweep runs unasked on every dialog
# open and only has to surface a handful of obvious repos, which is why 400 names
# is plenty. A search runs once, because the user asked for it, and has to reach
# depth 3 — where the directory count multiplies — or it would find exactly what
# the depth-1 sweep already found and be pointless. Hence a bigger cap AND a
# wall-clock deadline beside it: a count bounds WORK, not TIME, and three thousand
# stats are milliseconds on a warm SSD but half a minute on a cold NFS mount or a
# spun-down external drive. Whichever bound trips first ends the walk and the
# answer is flagged ``truncated``, so the dialog can say "there may be more"
# instead of implying the folder isn't there.
#
# The cap counts DIRECTORIES EXAMINED, and the word directories is load-bearing:
# it was once charged per name a listing returned, files included, so a Downloads
# folder holding three thousand loose files spent the whole allowance without the
# walk descending anywhere — and because a tripped budget ends the walk, the repo
# already queued two cheap levels away came back as "nothing found". Files are
# never walked; they must not be able to end a walk either.
_MAX_SEARCH_SCANNED = 3000
_SEARCH_DEADLINE_SECONDS = 1.5
# Depth 3 counted from each scan root: ~/code/acme/backend is found, and the
# fourth level down is a Browse click. The depth-1 limit of the sweep is exactly
# what makes nested repos invisible, and undoing that is the point of the search.
_SEARCH_MAX_DEPTH = 3
# Two characters before we walk anything. One letter matches a large fraction of
# any real home directory, so the "results" would be a random sample of the
# machine delivered at the cost of the full scan budget — and the user is almost
# certainly still typing.
_SEARCH_MIN_QUERY = 2


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


def _walkable_child_names(path: str) -> list:
    """The subdirectory names under ``path`` a search may descend into, sorted.

    :func:`os.scandir` rather than :func:`os.listdir` because the walk has to
    tell a directory from a file BEFORE the name is queued, and scandir answers
    that from the directory entry the kernel already returned, where listdir
    would owe a stat per name. That distinction is the whole difference between a
    search budget spent on candidate directories and one spent on the three
    thousand loose files in somebody's Downloads folder — see the note beside
    ``_MAX_SEARCH_SCANNED``.

    Names that are never walked are dropped here, before the caller can charge
    for them, for the reason spelled out in :func:`_nearby_candidates`: dotted
    entries sort first and every real home directory has dozens of them.

    Per the module contract this cannot raise. An unreadable directory answers
    with an empty list, and a listing that dies part-way (a mount going away
    under a long scandir) keeps whatever it had already read.
    """
    names: list = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(".") or entry.name in _SKIP_NAMES:
                    continue
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:  # noqa: BLE001 — a dangling symlink is not a dir
                    continue
    except OSError:  # noqa: BLE001 — unreadable or vanished mid-walk: skip it
        pass
    # Sorted so the budgets truncate deterministically rather than at the mercy
    # of directory order — the same reason _nearby_candidates sorts.
    names.sort(key=str.lower)
    return names


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


def _home_relative(path: str, home: str) -> str:
    """``path`` with the ``home`` prefix removed, for matching and for display.

    The weakest match tier tests the query against the folder's PATH, and it has
    to be the home-RELATIVE path: every candidate this module ever produces sits
    under the same ``/home/<user>`` (or ``/Users/<user>``) prefix, so a user whose
    account is called ``code`` — or who keeps repos on a volume with a matching
    name — would see the query match literally every folder on the machine. What
    the user is searching within is not part of what they are searching for.
    """
    if home and path.startswith(home + os.sep):
        return path[len(home) + 1 :]
    return path


def _search_rank(needle: str, name: str, rel_path: str) -> Optional[int]:
    """How well one folder answers the query — lower is better, ``None`` misses.

    ``needle`` is already lower-cased and stripped by the caller. The order
    encodes what someone typing into a folder field means: they are naming a
    folder, so the basename is the thing being searched and the path is only a
    fallback for "I know it's under acme/ somewhere". Exact beats prefix beats
    substring, and every basename hit beats a path hit, because ``api`` matching
    the folder ``api`` is an answer while ``api`` matching ``.../rapid/build`` is
    a coincidence the user has to read past.
    """
    low_name = name.lower()
    if low_name == needle:
        return 0
    if low_name.startswith(needle):
        return 1
    if needle in low_name:
        return 2
    if needle in rel_path.lower():
        return 3
    return None


def search_repos(query: str, home: str = "", limit: int = 20) -> dict:
    """Find folders by NAME under ``home`` — the picker's "look it up" tier.

    Returns ``{"matches": [<entry>], "truncated": bool}``, entries in the same
    shape :func:`suggest_repos` produces (``source: "search"``). Both git repos
    and plain folders come back: the folder field accepts either, and ``is_git``
    is what lets the dialog say which one this is before the user commits to it.

    This exists because the other two ways into the folder field both assume the
    user already knows where the repo is. The suggestion list is a depth-1 sweep
    (see :func:`_nearby_candidates` for why it must stay that shallow) and so
    cannot see ``~/code/acme/services/api``; :func:`check_repo` only answers about
    a path already typed in full; Browse means clicking down a tree one level at
    a time. Searching by name is the missing third thing, and the only one of the
    three that costs a real walk — hence the budgets below, which are the whole
    engineering content of this function.

    The walk is breadth-first on purpose. When a budget trips mid-scan, a
    depth-first walk has spent everything inside whichever subtree it entered
    first (alphabetically ``~/code/aardvark``) and reports nothing about the rest
    of the machine; breadth-first has covered every root shallowly, which is
    where the answer usually is.

    Nothing here raises, per the module contract: an unreadable directory, a
    vanished symlink or a dead mount costs its own subtree and nothing more.
    """
    text = str(query or "").strip()
    # Expanduser + normpath, deliberately NOT realpath: the picker offers paths
    # the user recognises and will see again in the folder field, and resolving
    # symlinks here would hand back the mount-point spelling of a folder they
    # know by another name. Symlinks are resolved once, at the end, purely to
    # avoid listing the same folder twice.
    base = os.path.normpath(os.path.expanduser(home or "~"))
    if len(text) < _SEARCH_MIN_QUERY or limit <= 0:
        # Not an error — the user is mid-word. An empty result with
        # ``truncated`` false is the honest answer: nothing was skipped because
        # nothing was looked at.
        return {"matches": [], "truncated": False}
    needle = text.lower()

    deadline = time.monotonic() + _SEARCH_DEADLINE_SECONDS
    scanned = 0
    truncated = False
    # (rank, path length, lowered path, path, carries a .git entry)
    hits: list = []
    # ~/code is both a scan root and a child of ~, so without this the whole
    # subtree is walked twice and pays for itself twice out of the budget.
    visited: set = set()
    roots = [base] + [os.path.join(base, name) for name in _SCAN_SUBDIRS]
    queue: deque = deque((root, 0) for root in roots)

    while queue:
        # The clock is read HERE, once per popped directory, and not down beside
        # the enqueue below. Every early exit from this loop — the directory is a
        # repo, it sits at the depth limit, it vanished, it holds no walkable
        # children — used to return to the top without the clock ever being read,
        # so a queue full of sibling clones (a ~/code with five hundred of them)
        # drained itself entirely PAST an expired deadline at two stats apiece.
        # On the cold NFS mount the deadline exists for that is a minute of a
        # dialog that looks hung, and ``truncated`` said nothing about it. A
        # deadline that only fires on the one path that happens to reach it is
        # not a deadline.
        if time.monotonic() > deadline:
            truncated = True
            break
        path, depth = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        if not os.path.isdir(path):
            continue
        # Charged per directory EXAMINED — the stat above plus the listing below
        # are what this budget is paying for — and charged before either the
        # ranking or the descent, so no shape of tree can spend work without
        # spending budget. See ``_MAX_SEARCH_SCANNED`` for the accounting this
        # replaced, which charged per listed name and let plain files end the
        # walk.
        scanned += 1
        if scanned > _MAX_SEARCH_SCANNED:
            truncated = True
            break
        is_repo = _has_git_dir(path)
        if path != base:
            # The home directory itself is never the answer to a name search (it
            # is named after the user, not after their project), but a scan root
            # such as ~/work or ~/code is a perfectly good hit.
            rank = _search_rank(
                needle, os.path.basename(path) or path, _home_relative(path, base)
            )
            if rank is not None:
                hits.append((rank, len(path), path.lower(), path, is_repo))
        if depth >= _SEARCH_MAX_DEPTH:
            continue
        nested_only = is_repo and path != base
        # A repo's own source tree is not searchable ground: inside it live
        # node_modules, vendor/, target/ and .venv-shaped trees with tens of
        # thousands of directories that would eat the whole budget to produce
        # matches nobody wants. Skipping it is the correct ANSWER and not merely
        # the cheap one.
        #
        # But a repo's children are not ALWAYS its source tree. An umbrella
        # directory that happens to be a git repo — a dotfiles repo rooted at a
        # work folder, a meta-repo, anything with a committed README and a pile
        # of sibling checkouts under it — holds other people's repos, and those
        # are exactly what someone is searching for. Refusing to look cost this
        # search every repo under such a parent: a real machine had its whole
        # day's work in ~/Work (a repo) and a name search found none of it,
        # while the shallower suggestion tier had never reached that deep either.
        #
        # So a repo is descended into ONE level and only nested REPOS are kept.
        # That is one listing plus one cheap _has_git_dir per child — bounded by
        # the same budget as everything else — and it cannot wander into a source
        # tree, because a src/ or a node_modules/ is not a repo and is dropped
        # right here.
        #
        # Home is exempt from the rule entirely: keeping dotfiles in a git repo
        # rooted at ~ is common enough that honouring it there would end the
        # search before it examined a single folder.
        for name in _walkable_child_names(path):
            child = os.path.join(path, name)
            if nested_only and not _has_git_dir(child):
                continue
            # Only directories are queued, so the budget above only ever meets
            # things that could really have been walked, and the queue cannot be
            # inflated by a folder full of files into something the budget has to
            # chew through one pop at a time.
            queue.append((child, depth + 1))

    # Exact basename first, then prefix, then substring, then path; ties go to
    # the shortest path (the parent of two equally-named folders is the one the
    # user more likely means) and then alphabetically, so the same tree always
    # ranks the same way.
    hits.sort(key=lambda hit: (hit[0], hit[1], hit[2]))

    matches: list = []
    seen_real: set = set()
    for _rank, _length, _lowered, path, has_git in hits:
        # Deduped by realpath like suggest_repos: a symlinked ~/code/foo and its
        # target are one folder, and offering both invites the user to pick the
        # one their tooling does not use. The reported path is the one we walked
        # to, not the resolved one, because that is the path the user recognises
        # and the one that reads relative to home in the dialog.
        real = os.path.realpath(path)
        if real in seen_real:
            continue
        seen_real.add(real)
        if len(matches) >= limit:
            # More genuine matches than the caller asked for. Same flag as a
            # tripped budget: what the dialog needs to know is "this list is not
            # everything", and the fix the user needs is the same one (type more).
            truncated = True
            break
        # The authoritative (subprocess) git check runs only on the handful of
        # folders that actually made the list — see _has_git_dir for why it
        # cannot run on every folder the walk touched.
        matches.append(_entry(path, "search", is_git=has_git and _is_git_repo(path)))
    return {"matches": matches, "truncated": truncated}


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
