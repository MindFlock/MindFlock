"""One small git runner shared by the engine, the config layer and the server.

Stdlib + subprocess only, and imports nothing from ``backend`` — ``config/``
and ``session/`` sit below ``web/``, so the timeout-safe runner lives here
rather than on top of ``backend.web.core.proc.run_capped`` (whose semantics it
mirrors: a hung or missing git is a *failed* result, never an exception).

* :func:`git` — ``git [-C cwd] <args>`` → ``CompletedProcess``. A timeout is
  returncode 124 and a git that cannot be launched is 127, the reason in
  ``stderr``; nothing raises unless ``check=True``.
* :func:`git_out` — stripped stdout, or ``""`` on any failure.
* :func:`parse_worktree_list` / :func:`worktree_list` /
  :func:`worktree_for_branch` — ``git worktree list --porcelain``.
* :func:`ref_exists` — does ``ref`` name a commit in ``repo``.

Output is read as bytes and decoded UTF-8 with ``replace`` (no newline
translation), so a stray non-UTF-8 byte in a path or commit message never
raises ``UnicodeDecodeError`` out of a probe.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence

__all__ = [
    "TIMEOUT_RC",
    "LAUNCH_RC",
    "GitResult",
    "git",
    "git_out",
    "parse_worktree_list",
    "worktree_list",
    "worktree_for_branch",
    "ref_exists",
]

#: Returncode of a git killed at its timeout (coreutils ``timeout``'s code).
TIMEOUT_RC = 124
#: Returncode of a git that could not be launched (missing binary, bad cwd).
LAUNCH_RC = 127


class GitResult(subprocess.CompletedProcess):
    """A ``CompletedProcess`` that also says WHY git produced no real exit:
    ``timed_out`` (killed at the timeout) or ``not_run`` (could not be
    launched), so a caller can keep its own wording for those cases instead
    of guessing from the returncode."""

    timed_out = False
    not_run = False


def _decode(v, text: bool):
    if v is None:
        return "" if text else b""
    if text and isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def git(
    args: Sequence[str],
    cwd: Optional[str] = None,
    *,
    timeout: float,
    check: bool = False,
    env: Optional[Mapping[str, str]] = None,
    text: bool = True,
    input: Optional[bytes] = None,
    stdin=None,
    stderr=subprocess.PIPE,
    new_session: bool = False,
) -> GitResult:
    """Run ``git -C <cwd> <args>`` (plain ``git <args>`` when ``cwd`` is None).

    ``env`` entries are laid over ``os.environ`` (``None`` inherits it as is).
    ``text`` decodes stdout/stderr to ``str``; ``stderr`` may be
    ``subprocess.DEVNULL``; ``stdin`` defaults to the parent's (pass
    ``subprocess.DEVNULL`` to keep git off a terminal); ``input`` feeds bytes
    on stdin. ``new_session`` starts git in its own process group and, on a
    timeout, kills the whole group (a fetch's ssh, a pull's rebase) instead
    of git alone.

    Never hangs past ``timeout`` and never raises unless ``check`` is true:
    a timeout is returncode :data:`TIMEOUT_RC` and a launch failure
    :data:`LAUNCH_RC`, the reason in ``stderr`` (and ``timed_out`` /
    ``not_run`` set on the :class:`GitResult`). With ``check`` any non-zero
    returncode raises ``subprocess.CalledProcessError``.
    """
    argv = ["git", "-C", cwd, *args] if cwd is not None else ["git", *args]
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    kw: Dict[str, object] = {"stdout": subprocess.PIPE, "stderr": stderr}
    if input is not None:
        kw["input"] = input
    elif stdin is not None:
        kw["stdin"] = stdin
    if full_env is not None:
        kw["env"] = full_env
    timed_out = not_run = False
    try:
        if new_session:
            cp = _run_group(argv, timeout, kw)
        else:
            cp = subprocess.run(argv, timeout=timeout, **kw)
        rc = cp.returncode
        out = _decode(cp.stdout, text)
        err = _decode(getattr(cp, "stderr", None), text)
    except subprocess.TimeoutExpired as exc:
        rc, timed_out = TIMEOUT_RC, True
        out = _decode(exc.output, text)
        note = "timed out after %gs: %s" % (timeout, " ".join(argv))
        err = note if text else note.encode("utf-8")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # ValueError: an argument with a NUL byte can't be passed to exec.
        rc, not_run = LAUNCH_RC, True
        out = _decode(None, text)
        err = str(exc) if text else str(exc).encode("utf-8")
    res = GitResult(argv, rc, stdout=out, stderr=err)
    res.timed_out, res.not_run = timed_out, not_run
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, argv, output=out, stderr=err)
    return res


def _run_group(argv, timeout, kw) -> subprocess.CompletedProcess:
    """``subprocess.run`` in a new session whose timeout kills the GROUP."""
    data = kw.pop("input", None)
    if data is not None:
        kw["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(argv, start_new_session=True, **kw)
    try:
        out, err = proc.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):  # AttributeError: no killpg (Windows)
            proc.kill()
        proc.communicate()
        raise subprocess.TimeoutExpired(argv, timeout) from exc
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def git_out(
    args: Sequence[str],
    cwd: Optional[str] = None,
    *,
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    stdin=None,
) -> str:
    """Stripped stdout of :func:`git`, or ``""`` on a non-zero exit, a
    timeout or a missing git. stderr is discarded."""
    cp = git(
        args,
        cwd,
        timeout=timeout,
        env=env,
        stdin=stdin,
        stderr=subprocess.DEVNULL,
    )
    return cp.stdout.strip() if cp.returncode == 0 else ""


def parse_worktree_list(text: str) -> List[Dict[str, object]]:
    """``git worktree list --porcelain`` → one dict per worktree.

    Each record has ``path`` (the ``worktree`` line), and as present
    ``head`` (sha), ``branch`` (the FULL ref, ``refs/heads/x``) and the flags
    ``bare`` / ``detached`` / ``locked`` / ``prunable`` (``True``, or the
    reason text when git gave one). A record starts at each ``worktree``
    line, so a missing blank separator is tolerated; attribute lines before
    the first ``worktree`` line are ignored.
    """
    out: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    for line in text.splitlines():
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur = {"path": value}
            out.append(cur)
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value
        elif key in ("bare", "detached", "locked", "prunable"):
            cur[key] = value or True
    return out


def worktree_list(
    repo: str, *, timeout: float = 60, env: Optional[Mapping[str, str]] = None
) -> Optional[List[Dict[str, object]]]:
    """:func:`parse_worktree_list` of ``repo``'s worktrees, or ``None`` when
    git could not answer (not a repo, timeout, git missing) — never an empty
    list for a failure."""
    cp = git(
        ["worktree", "list", "--porcelain"],
        repo,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if cp.returncode != 0:
        return None
    return parse_worktree_list(cp.stdout)


def worktree_for_branch(
    repo: str, branch: str, *, timeout: float = 60, env=None
) -> str:
    """Path of the worktree of ``repo`` that has local ``branch`` checked
    out, or ``""`` (none, or git could not answer)."""
    ref = "refs/heads/" + branch
    for wt in worktree_list(repo, timeout=timeout, env=env) or ():
        if wt.get("branch") == ref:
            return str(wt["path"])
    return ""


def ref_exists(repo: str, ref: str, *, timeout: float = 30, env=None) -> bool:
    """Whether ``ref`` resolves to a commit in ``repo``
    (``git rev-parse --verify --quiet <ref>^{commit}``). A ref that starts
    with ``-`` is never looked up (it would parse as an option)."""
    if not ref or ref.startswith("-"):
        return False
    cp = git(
        ["rev-parse", "--verify", "--quiet", ref + "^{commit}"],
        repo,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cp.returncode == 0
