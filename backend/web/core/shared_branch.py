"""Two people (or two machines) on ONE pushed branch: ``continue_branch``,
the divergence check before every push, and ``rebase-remote``.

Three pieces, one module:

* **The flag.** A session created with ``continue_branch: "x"`` works ON
  ``x`` (tracking ``<remote>/x``) instead of on a branch of its own. That is
  recorded here as ``title -> {"remote", "branch"}`` — web metadata, NOT
  engine state (the Instance JSON is byte-exact and pinned by goldens; the
  engine already records the branch itself as ``Branch``/``BaseBranch`` and,
  because it is an existing branch, never deletes it on cleanup). Rows carry
  it as ``shared_branch``. Persisted as ``<config dir>/session_shared_branch
  .json``, the on-disk pattern of :mod:`backend.web.core.hidden_sessions`.

* **Divergence.** :func:`divergence` fetches ``<remote> <branch>`` (bounded,
  :data:`FETCH_TIMEOUT` s) and counts what the remote has that ``HEAD``
  lacks. The push route answers 409 ``diverged`` on it rather than letting
  ``git push`` reject the push in a shell nobody reads. The count is kept in
  memory (:data:`_BEHIND`) so rows can show "N behind" without a fetch per
  poll; shared sessions re-fetch at most every :data:`BG_FETCH_EVERY` s, in a
  background thread, from the row build.

* **Rebase.** :func:`rebase_remote` is ``git pull --rebase <remote>
  <branch>``; on conflict it aborts, leaving the tree exactly as it was, and
  names the conflicting files. Nothing here ever force-pushes.

Blocking throughout: call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from backend import config, git_auth_hints, gitutil, log
from backend.web.core import title_store

#: Bound on every fetch this module runs (the push check, create, background).
FETCH_TIMEOUT = 20.0
#: How long a ``git pull --rebase`` may take before it is treated as failed.
REBASE_TIMEOUT = 120.0
#: Background re-fetch interval for shared sessions (seconds).
BG_FETCH_EVERY = 300.0
#: Most author names a divergence answer lists.
MAX_AUTHORS = 8

_LOCK = threading.Lock()
_SHARED: Optional[Dict[str, Dict[str, str]]] = None  # title -> {remote, branch}
#: worktree -> {"remote", "branch", "behind", "authors", "at"}: what the last
#: fetch (push check, rebase, create, background) found. In memory only.
_BEHIND: Dict[str, dict] = {}
#: worktree -> monotonic time of the last background fetch started.
_BG_AT: Dict[str, float] = {}
_BG_RUNNING: set = set()


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def _git(path: str, *args: str, timeout: float = 15.0) -> Tuple[int, str, str]:
    """``git -C path args…`` → ``(rc, stdout, stderr)``; never raises (a
    timeout or a missing git is rc 124 / 127)."""
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_EDITOR": "true",
        "GCM_INTERACTIVE": "never",
    }
    # SSH would still ask (host key, passphrase) on a tty: never let it.
    if "GIT_SSH_COMMAND" not in os.environ:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    # A new session: a timeout kills git AND its children (pull's rebase,
    # ssh), not just the parent. github_signin: the connected GitHub
    # sign-in answers for github.com (the fetch behind the divergence check).
    cp = gitutil.git(
        args,
        path,
        timeout=timeout,
        env=git_auth_hints.github_signin(env),
        stdin=subprocess.DEVNULL,
        new_session=True,
    )
    if cp.timed_out:
        return 124, "", "timed out after %ds" % int(timeout)
    return cp.returncode, cp.stdout, cp.stderr


def _ref_ok(name: str) -> bool:
    return bool(name) and not name.startswith("-") and ".." not in name


def fetch(path: str, remote: str, branch: str) -> Tuple[str, str]:
    """``git fetch <remote> +refs/heads/<branch>:refs/remotes/<remote>/<branch>``.

    → ``("ok", "")``, ``("missing", why)`` when the remote has no such
    branch, or ``("failed", why)`` (offline, auth, timeout)."""
    if not (_ref_ok(remote) and _ref_ok(branch)):
        return "failed", "invalid remote or branch name"
    rc, _out, err = _git(
        path,
        "fetch",
        "--no-tags",
        "--quiet",
        remote,
        "+refs/heads/%s:refs/remotes/%s/%s" % (branch, remote, branch),
        timeout=FETCH_TIMEOUT,
    )
    if rc == 0:
        return "ok", ""
    text = (err or "").strip()
    if "couldn't find remote ref" in text or "could not find remote ref" in text:
        return "missing", text
    return "failed", text or "git fetch exited %d" % rc


def remote_names(path: str) -> List[str]:
    rc, out, _ = _git(path, "remote")
    return [r.strip() for r in out.splitlines() if r.strip()] if rc == 0 else []


def split_remote_ref(path: str, ref: str) -> Tuple[str, str]:
    """``origin/feature/x`` → ``("origin", "feature/x")`` when ``origin`` is a
    remote of ``path`` (the longest matching remote name wins; a
    ``refs/remotes/`` prefix is accepted), else ``("", "")``."""
    ref = (ref or "").strip()
    if ref.startswith("refs/remotes/"):
        ref = ref[len("refs/remotes/") :]
    best = ""
    for r in remote_names(path):
        if ref.startswith(r + "/") and len(r) > len(best):
            best = r
    if not best:
        return "", ""
    return best, ref[len(best) + 1 :]


def checked_out_in(path: str, branch: str) -> str:
    """The worktree path that has ``branch`` checked out, or ``""``."""
    return gitutil.worktree_for_branch(path, branch, timeout=15.0)


def _rev(path: str, ref: str) -> str:
    rc, out, _ = _git(path, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    return out.strip() if rc == 0 else ""


def prepare_continue(repo: str, base_ref: str, branch: str) -> Tuple[int, str, str]:
    """Validate and ready ``continue_branch`` for a worktree of ``repo``.

    ``base_ref`` must be ``<remote>/<branch>`` for a remote of ``repo``; the
    remote is fetched and the branch must exist there afterwards (offline is
    tolerated when the remote-tracking ref is already known). ``branch``
    must not be checked out in any worktree of the repo (409, naming it).
    Then the local branch is made ready: created tracking the remote one
    when absent; when present, given that upstream if it has none and
    fast-forwarded when it is strictly behind (a diverged local branch is
    left alone — the push check catches it).

    → ``(0, remote, "")`` on success, else ``(status, "", error)``.
    """
    branch = (branch or "").strip()
    if not branch or branch.startswith("-"):
        return 400, "", "continue_branch must name a branch"
    rc, _o, _e = _git(repo, "check-ref-format", "--branch", branch)
    if rc != 0:
        return 400, "", "invalid continue_branch: %s" % branch
    remote, rbranch = split_remote_ref(repo, base_ref)
    if not remote:
        return (
            400,
            "",
            "continue_branch needs base_ref to be a remote-tracking branch "
            "(<remote>/%s), got %r" % (branch, base_ref),
        )
    if rbranch != branch:
        return (
            400,
            "",
            "continue_branch %r does not match base_ref %r" % (branch, base_ref),
        )
    if remote != "origin":
        # Push, the pre-push divergence check and rebase-remote all go through
        # origin; a branch shared on another remote would be pushed elsewhere.
        return (
            400,
            "",
            "only origin's branches can be shared for now (got %s/%s)"
            % (remote, branch),
        )
    state, why = fetch(repo, remote, branch)
    tracking = "refs/remotes/%s/%s" % (remote, branch)
    if state == "missing":
        return 400, "", "%s/%s does not exist on %s" % (remote, branch, remote)
    remote_sha = _rev(repo, tracking)
    if not remote_sha:
        return (
            400,
            "",
            "%s/%s is not a remote-tracking branch of this repo%s"
            % (remote, branch, " (fetch failed: %s)" % why if why else ""),
        )
    where = checked_out_in(repo, branch)
    if where:
        return (
            409,
            "",
            "%s is already checked out in %s — work in that session, or close "
            "it first" % (branch, where),
        )
    local_sha = _rev(repo, "refs/heads/" + branch)
    if not local_sha:
        rc, _o, err = _git(repo, "branch", "--track", branch, tracking)
        if rc != 0:
            return (
                400,
                "",
                "could not create %s from %s: %s"
                % (
                    branch,
                    tracking,
                    err.strip(),
                ),
            )
    else:
        rc, _o, _e = _git(
            repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", branch + "@{u}"
        )
        if rc != 0:
            _git(repo, "branch", "--set-upstream-to=%s/%s" % (remote, branch), branch)
        if local_sha != remote_sha:
            rc, _o, _e = _git(
                repo, "merge-base", "--is-ancestor", local_sha, remote_sha
            )
            if rc == 0:
                _git(
                    repo,
                    "update-ref",
                    "refs/heads/" + branch,
                    remote_sha,
                    local_sha,
                )
    return 0, remote, ""


# --------------------------------------------------------------------------- #
# Divergence
# --------------------------------------------------------------------------- #
def _count_behind(wt: str, remote: str, branch: str) -> Tuple[int, List[str]]:
    """Commits on ``<remote>/<branch>`` that ``HEAD`` lacks, and their
    authors (first-seen order) — local refs only, no network."""
    tracking = "refs/remotes/%s/%s" % (remote, branch)
    # `--cherry-pick --right-only` drops remote commits whose patch is already
    # in HEAD: after the session rebased or amended its OWN pushed commits the
    # remote looks "ahead" but holds nothing new — that is a rewrite (git's own
    # non-fast-forward refusal still stands), not someone else's push.
    rc, out, _ = _git(
        wt,
        "log",
        "--format=%an",
        "--cherry-pick",
        "--right-only",
        "--no-merges",
        "HEAD..." + tracking,
    )
    if rc != 0:
        return 0, []
    names = [n.strip() for n in out.splitlines() if n.strip()]
    authors: List[str] = []
    for n in names:
        if n not in authors:
            authors.append(n)
    return len(names), authors[:MAX_AUTHORS]


def _record(wt: str, remote: str, branch: str, behind: int, authors: List[str]):
    with _LOCK:
        _BEHIND[wt] = {
            "remote": remote,
            "branch": branch,
            "behind": int(behind),
            "authors": list(authors),
            "at": time.time(),
        }


def divergence(wt: str, remote: str, branch: str) -> Optional[dict]:
    """Fetch ``<remote> <branch>`` and say whether it moved on past ``HEAD``.

    → ``{"behind", "authors", "remote", "branch"}`` (``behind`` may be 0), or
    ``None`` when there is nothing to compare: the remote has no such branch
    yet (a first push), or the fetch failed (offline — the push itself will
    say so; this check fails open)."""
    if not wt or not branch:
        return None
    state, _why = fetch(wt, remote, branch)
    if state != "ok":
        return None
    behind, authors = _count_behind(wt, remote, branch)
    _record(wt, remote, branch, behind, authors)
    return {"behind": behind, "authors": authors, "remote": remote, "branch": branch}


def diverged_message(d: dict) -> str:
    n = int(d.get("behind") or 0)
    authors = d.get("authors") or []
    return "%s moved on %s: %d new commit%s by %s" % (
        d.get("branch"),
        d.get("remote"),
        n,
        "" if n == 1 else "s",
        ", ".join(authors) if authors else "someone else",
    )


# --------------------------------------------------------------------------- #
# Rebase onto the remote
# --------------------------------------------------------------------------- #
def _in_rebase(wt: str) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        rc, out, _ = _git(wt, "rev-parse", "--git-path", name)
        if rc == 0 and out.strip():
            p = out.strip()
            if not os.path.isabs(p):
                p = os.path.join(wt, p)
            if os.path.exists(p):
                return True
    return False


def rebase_remote(wt: str, remote: str, branch: str) -> Tuple[int, dict]:
    """``git pull --rebase <remote> <branch>`` in ``wt``.

    → ``(200, {"ok", "head", "behind": 0})`` on success; ``(409, {"error",
    "conflicts": [...]})`` when it conflicts — the rebase is aborted first,
    so the tree and ``HEAD`` are exactly as they were; ``(409, …)`` for a
    tree with uncommitted tracked changes or a rebase already in progress;
    ``(502, {"error"})`` when git failed for another reason (offline)."""
    if not (_ref_ok(remote) and _ref_ok(branch)):
        return 400, {"error": "invalid remote or branch name"}
    if _in_rebase(wt):
        return 409, {
            "error": "a rebase is already in progress in this worktree — finish "
            "or abort it first"
        }
    rc, out, _ = _git(wt, "status", "--porcelain", "--untracked-files=no")
    if rc == 0 and out.strip():
        return 409, {
            "error": "commit or stash your changes before rebasing onto %s/%s"
            % (remote, branch),
            "dirty": True,
        }
    before = _rev(wt, "HEAD")
    rc, out, err = _git(
        wt,
        "-c",
        "core.editor=true",
        "pull",
        "--rebase",
        "--no-autostash",
        remote,
        branch,
        timeout=REBASE_TIMEOUT,
    )
    if rc == 0:
        head = _rev(wt, "HEAD")
        behind, authors = _count_behind(wt, remote, branch)
        _record(wt, remote, branch, behind, authors)
        return 200, {"ok": True, "head": head, "behind": behind}
    if _in_rebase(wt):
        crc, cout, _ = _git(wt, "diff", "--name-only", "--diff-filter=U")
        conflicts = [c for c in cout.splitlines() if c.strip()] if crc == 0 else []
        arc, _ao, aerr = _git(wt, "rebase", "--abort", timeout=60.0)
        restored = (
            arc == 0
            and not _in_rebase(wt)
            and (not before or _rev(wt, "HEAD") == before)
            and bool(_git(wt, "symbolic-ref", "-q", "HEAD")[1].strip())
        )
        if not restored:
            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "rebase-remote: %s is still mid-rebase after abort (%s)", wt, aerr
                )
            return 500, {
                "error": "rebasing onto %s/%s conflicted and the abort did not "
                "finish — the worktree is mid-rebase; run `git rebase --abort` "
                "there (or ask the agent) before anything else" % (remote, branch),
                "conflicts": conflicts,
                "mid_rebase": True,
            }
        return 409, {
            "error": "rebasing onto %s/%s conflicts in %s — the rebase was "
            "aborted and nothing changed; resolve it by hand (or ask the agent)"
            % (
                remote,
                branch,
                ", ".join(conflicts[:5]) if conflicts else "some files",
            ),
            "conflicts": conflicts,
            "remote": remote,
            "branch": branch,
        }
    text = (err or out or "").strip().splitlines()
    return 502, {"error": "git pull --rebase failed: %s" % (text[-1] if text else rc)}


# --------------------------------------------------------------------------- #
# The flag (web metadata)
# --------------------------------------------------------------------------- #
def _path() -> str:
    return os.path.join(config.GetConfigDir(), "session_shared_branch.json")


def _load() -> Dict[str, Dict[str, str]]:
    global _SHARED
    if _SHARED is None:
        _SHARED = title_store.load(
            _path,
            lambda d: {
                str(k): {
                    "remote": str(v.get("remote") or "origin"),
                    "branch": str(v["branch"]),
                }
                for k, v in d.items()
                if isinstance(v, dict) and v.get("branch")
            },
        )
    return _SHARED


def _persist() -> None:
    title_store.persist(_path, _SHARED, "shared-branch sessions")


def set_shared(title: str, remote: str = "", branch: str = "") -> None:
    """Record ``title`` as working on ``<remote>/<branch>``; a blank
    ``branch`` forgets it (a reused title starts unshared)."""
    title = str(title or "").strip()
    if not title:
        return
    with _LOCK:
        shared = _load()
        if branch:
            entry = {"remote": remote or "origin", "branch": branch}
            if shared.get(title) == entry:
                return
            shared[title] = entry
        elif title not in shared:
            return
        else:
            shared.pop(title, None)
        _persist()


def shared(title: str) -> Optional[Dict[str, str]]:
    """``{"remote", "branch"}`` for a shared-branch session, else None.
    Never raises."""
    try:
        with _LOCK:
            v = _load().get(str(title or ""))
            return dict(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def drop(title: str) -> None:
    """Forget a deleted session (missing is fine)."""
    set_shared(title, "", "")


def forget_worktree(wt: str) -> None:
    with _LOCK:
        _BEHIND.pop(wt, None)
        _BG_AT.pop(wt, None)


def _bg_fetch(wt: str, remote: str, branch: str) -> None:
    try:
        divergence(wt, remote, branch)
    except Exception:  # noqa: BLE001 — a background refresh never raises
        pass
    finally:
        with _LOCK:
            _BG_RUNNING.discard(wt)


def row_fields(title: str, wt: str, branch: str, cheap: bool = False) -> dict:
    """``{"shared_branch", "behind"}`` for a session row. Never raises.

    ``behind`` is read from LOCAL refs (one ``git log``) for a shared session
    or for one whose last check found it behind; any other session answers
    0 without touching git. A shared session whose last fetch is older than
    :data:`BG_FETCH_EVERY` gets a background fetch (never on this thread).
    ``cheap`` reads only the memo."""
    try:
        s = shared(title)
        out = {"shared_branch": bool(s), "behind": 0}
        if not wt:
            return out
        with _LOCK:
            memo = dict(_BEHIND.get(wt) or {})
        if s:
            remote, br = s["remote"], branch or s["branch"]
        elif memo.get("behind"):
            remote, br = memo["remote"], memo["branch"]
        else:
            return out
        if cheap:
            out["behind"] = int(memo.get("behind") or 0)
            return out
        if os.path.isdir(wt):
            behind, authors = _count_behind(wt, remote, br)
            out["behind"] = behind
            if not s and not behind:
                # It caught up (pulled by hand): stop looking.
                forget_worktree(wt)
            elif memo.get("behind") != behind:
                _record(wt, remote, br, behind, authors)
        if s:
            now = time.monotonic()
            with _LOCK:
                due = (
                    wt not in _BG_RUNNING
                    and now - _BG_AT.get(wt, -BG_FETCH_EVERY) >= BG_FETCH_EVERY
                )
                if due:
                    _BG_RUNNING.add(wt)
                    _BG_AT[wt] = now
            if due:
                threading.Thread(
                    target=_bg_fetch, args=(wt, remote, br), daemon=True
                ).start()
        return out
    except Exception:  # noqa: BLE001 — a row never fails over this
        return {"shared_branch": False, "behind": 0}
