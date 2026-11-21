"""Which branch a repository's work goes into: ONE resolver for every caller.

A flock spans repositories whose trunks differ (one forks from ``staging``,
another only has ``main``), while the flat ``repository.base_branch`` /
``repository.pr_base_branch`` settings name one branch for all of them. Handing
git a branch the repository does not have ends in ``ambiguous argument
'origin/staging...feature'`` — so every caller that needs "the base" or "the
PR target" for a repository asks here, and the answer is, in order:

1. **Your setting for this repository** (``repository.repo_branches``) —
   explicit, used as written.
2. **The flat setting**, but ONLY when that branch exists on this repository
   (its remote-tracking ref, a local branch, or the remote itself says so).
   Skipped otherwise, and the skip is reported once in plain words
   (:attr:`BaseChoice.note`): "Using main: this repository has no staging
   branch".
3. **The repository's own default branch**: ``origin/HEAD``, else the remote
   is asked (``git ls-remote --symref``), cached in-process.
4. **main / master**, whichever the repository has.

Nothing resolving is a plain sentence with the remedy (:attr:`BaseChoice.error`),
never a git message. When the remote cannot be reached, a flat setting is
kept: "missing" is only ever said on evidence.

A ``location`` is a local checkout/worktree path (its ``origin`` is the remote)
or a clone URL (provisioning, before any checkout exists). Blocking (git, and
at most one ``ls-remote`` per remote per :data:`PROBE_TTL`): call it off the
event loop. ``network=False`` answers from local refs alone, for paths that run
on every poll.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

from backend import gitutil

#: Resolution sources, in the order they are tried.
SOURCE_REPO = "repo"  # repository.repo_branches
SOURCE_SETTING = "setting"  # the flat repository.(pr_)base_branch
SOURCE_DEFAULT = "default"  # the remote's own default branch
SOURCE_GUESS = "guess"  # main / master, by existence
SOURCE_SESSION = "session"  # the branch a session started from (PR base only)

#: What each source reads as in Settings ("main · the repository's default").
WHY = {
    SOURCE_REPO: "your setting for this repository",
    SOURCE_SETTING: "your setting",
    SOURCE_DEFAULT: "the repository's default",
    SOURCE_GUESS: "the repository has it",
    SOURCE_SESSION: "where this session started",
}

#: How long one ``ls-remote`` answer is reused per remote.
PROBE_TTL = 600.0
#: ...and how long a failed probe (offline, no access) is remembered.
PROBE_FAIL_TTL = 60.0
#: Wall-clock cap on one ``ls-remote``.
PROBE_TIMEOUT = 15.0

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}

#: remote -> (expires_at, (default, heads) | None)
_PROBES: Dict[str, Tuple[float, Optional[Tuple[str, FrozenSet[str]]]]] = {}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class BaseChoice:
    """The resolved branch and why. ``branch`` is ``""`` when nothing resolved."""

    branch: str = ""
    source: str = ""
    #: The flat setting that was passed over because this repository lacks it.
    skipped: str = ""

    @property
    def why(self) -> str:
        """``"main · the repository's default"``; ``""`` when unresolved."""
        if not self.branch:
            return ""
        return "%s · %s" % (self.branch, WHY.get(self.source, self.source))

    @property
    def note(self) -> str:
        """The one plain sentence a skipped flat setting earns, else ``""``."""
        if not (self.skipped and self.branch):
            return ""
        return "Using %s: this repository has no %s branch" % (
            self.branch,
            self.skipped,
        )

    @property
    def error(self) -> str:
        """The plain sentence (with the remedy) when nothing resolved."""
        if self.branch:
            return ""
        return unresolved_message(self.skipped)


def unresolved_message(skipped: str = "") -> str:
    """Why no base branch could be chosen, and what to do about it."""
    lead = "MindFlock could not tell which branch this repository's work goes into"
    if skipped and skipped not in ("main", "master"):
        lead += (
            ": it has no %s branch, its remote names no default branch, and it "
            "has no main or master" % skipped
        )
    else:
        lead += ": its remote names no default branch and it has no main or master"
    return (
        lead + ". Set a base branch for this repository in Settings → Workspace → "
        "Per-repo branches."
    )


# --------------------------------------------------------------------------- #
# git probes
# --------------------------------------------------------------------------- #
def _git_out(cwd: Optional[str], *args: str, timeout: float = 10.0) -> Tuple[int, str]:
    cp = gitutil.git(
        list(args),
        cwd,
        timeout=timeout,
        env=_GIT_ENV,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cp.returncode, (cp.stdout or "").strip()


def _is_path(location: str) -> bool:
    loc = os.path.expanduser(location or "")
    return bool(loc) and os.path.isdir(loc)


def _is_repo(path: str) -> bool:
    rc, _ = _git_out(path, "rev-parse", "--git-dir")
    return rc == 0


def _origin_of(path: str) -> str:
    rc, out = _git_out(path, "remote", "get-url", "origin")
    return out if rc == 0 else ""


def _ref_ok(path: str, ref: str) -> bool:
    if not ref or ref.startswith("-"):
        return False
    rc, _ = _git_out(path, "show-ref", "--verify", "--quiet", ref)
    return rc == 0


def _local_has(path: str, branch: str, *, remote_only: bool = False) -> bool:
    """``branch`` as ``origin/<branch>`` (or, unless ``remote_only``, a local
    head) in the checkout at ``path``."""
    if _ref_ok(path, "refs/remotes/origin/" + branch):
        return True
    return not remote_only and _ref_ok(path, "refs/heads/" + branch)


def _origin_head(path: str) -> str:
    """``origin/HEAD``'s branch name in the checkout at ``path``, or ``""``."""
    rc, out = _git_out(
        path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if rc != 0 or not out:
        return ""
    return out[len("origin/") :] if out.startswith("origin/") else out


def probe_remote(remote: str) -> Optional[Tuple[str, FrozenSet[str]]]:
    """``(default branch, branch names)`` the remote answers, or None when it
    could not be asked. One ``git ls-remote --symref`` per remote per
    :data:`PROBE_TTL` (a failure is remembered for :data:`PROBE_FAIL_TTL`)."""
    if not remote or remote.startswith("-"):
        return None
    now = time.time()
    with _LOCK:
        hit = _PROBES.get(remote)
        if hit and hit[0] > now:
            return hit[1]
    cp = gitutil.git(
        ["ls-remote", "--symref", remote, "HEAD", "refs/heads/*"],
        None,
        timeout=PROBE_TIMEOUT,
        env=_GIT_ENV,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        new_session=True,
    )
    answer: Optional[Tuple[str, FrozenSet[str]]] = None
    if cp.returncode == 0:
        default, heads = "", set()
        for line in (cp.stdout or "").splitlines():
            left, _, right = line.partition("\t")
            right = right.strip()
            if left.startswith("ref: ") and right == "HEAD":
                target = left[len("ref: ") :].strip()
                if target.startswith("refs/heads/"):
                    default = target[len("refs/heads/") :]
            elif right.startswith("refs/heads/"):
                heads.add(right[len("refs/heads/") :])
        answer = (default, frozenset(heads))
    with _LOCK:
        _PROBES[remote] = (
            now + (PROBE_TTL if answer is not None else PROBE_FAIL_TTL),
            answer,
        )
    return answer


def forget(remote: str = "") -> None:
    """Drop the cached probe for ``remote`` (every remote when blank)."""
    with _LOCK:
        if remote:
            _PROBES.pop(remote, None)
        else:
            _PROBES.clear()


def _remote_for(location: str) -> Tuple[str, str]:
    """``(checkout path or "", remote to ask or "")`` for ``location``."""
    loc = (location or "").strip()
    if _is_path(loc):
        path = os.path.expanduser(loc)
        return path, _origin_of(path)
    return "", loc


def branch_exists(
    location: str, branch: str, *, network: bool = True
) -> Optional[bool]:
    """Whether ``branch`` exists on ``location``'s repository.

    True/False on evidence; None when the remote could not be asked and the
    local refs do not show it (offline: "unknown", never "missing"). A checkout
    with no ``origin`` is judged by its own branches."""
    branch = (branch or "").strip()
    if not branch or branch.startswith("-"):
        return False
    path, remote = _remote_for(location)
    if path:
        if not _is_repo(path):
            return None
        if not remote:
            return _ref_ok(path, "refs/heads/" + branch)
        if _local_has(path, branch, remote_only=True):
            return True
    if not network or not remote:
        if path and _ref_ok(path, "refs/heads/" + branch):
            return True
        return None
    probed = probe_remote(remote)
    if probed is not None:
        return branch in probed[1]
    if path and _ref_ok(path, "refs/heads/" + branch):
        return True
    return None


def default_branch(location: str, *, network: bool = True) -> str:
    """The repository's own default branch (``origin/HEAD``, else the remote's
    answer), or ``""``. A checkout with no ``origin`` has none."""
    path, remote = _remote_for(location)
    if path:
        head = _origin_head(path)
        if head:
            return head
    if not network or not remote:
        return ""
    probed = probe_remote(remote)
    return probed[0] if probed is not None else ""


def guess_branch(location: str, *, network: bool = True) -> str:
    """``main`` or ``master``, whichever the repository has, else ``""``."""
    for cand in ("main", "master"):
        if branch_exists(location, cand, network=network):
            return cand
    return ""


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def configured(location: str, kind: str = "pr", settings=None) -> Tuple[str, str]:
    """``(per-repo branch, flat branch)`` the settings name for ``location``.

    ``kind`` "pr": the per-repo PR base (else its base) and the flat
    ``pr_base_branch``. ``kind`` "base": the per-repo base and the flat
    ``base_branch`` (else ``github.base_branch``). No git beyond reading the
    origin for the slug; never raises."""
    try:
        from backend.config import settings as _s
        from backend.session.provisioned import repo_slug_for

        st = settings if settings is not None else _s.load_settings()
        repo = st.repository
        per = _s.repo_branches_for(repo_slug_for(location), st)
        if kind == "base":
            flat = repo.base_branch or st.github.base_branch or ""
            return per[0], flat.strip()
        return per[1], (repo.pr_base_branch or "").strip()
    except Exception:  # noqa: BLE001 — a settings fault must never block git work
        return "", ""


def resolve(
    location: str,
    kind: str = "pr",
    *,
    settings=None,
    flat: Optional[str] = None,
    per_repo: Optional[str] = None,
    fallback: bool = True,
    network: bool = True,
) -> BaseChoice:
    """The branch ``location``'s work goes into — see the module docstring.

    ``flat`` / ``per_repo`` replace the settings' values (provisioning has its
    own env/config layers). ``fallback=False`` stops after the two settings
    steps: a caller with its own next step (a session's recorded fork point)
    still learns about a skipped flat value through ``skipped``."""
    if per_repo is None or flat is None:
        got_per, got_flat = configured(location, kind, settings)
        per_repo = got_per if per_repo is None else per_repo
        flat = got_flat if flat is None else flat
    per_repo, flat = (per_repo or "").strip(), (flat or "").strip()
    if per_repo:
        return BaseChoice(per_repo, SOURCE_REPO)
    skipped = ""
    if flat:
        if branch_exists(location, flat, network=network) is not False:
            return BaseChoice(flat, SOURCE_SETTING)
        skipped = flat
    if not fallback:
        return BaseChoice("", "", skipped)
    head = default_branch(location, network=network)
    if head:
        return BaseChoice(head, SOURCE_DEFAULT, skipped)
    guess = guess_branch(location, network=network)
    if guess:
        return BaseChoice(guess, SOURCE_GUESS, skipped)
    return BaseChoice("", "", skipped)
