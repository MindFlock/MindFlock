"""Update a development checkout from its own branch.

A server running from a git checkout (an editable install,
:func:`~backend.web.core.self_update.install_kind` ``== "editable"``) used to
be told about every release and handed git lines to type. Now it updates the
way a release install does — one click, or by itself in ``auto`` mode — but
from git, never from the installer:

* **What is new** is the checkout's own default branch moving
  (:func:`status`): it is on the branch ``origin/HEAD`` names (else
  ``main``/``master``), and that branch's upstream has commits it does not
  (``behind > 0``). Cheap to ask: ``git ls-remote`` against the local
  remote-tracking ref, and a ``git fetch`` only when they differ
  (:func:`_sync_remote`, at most once per :data:`REMOTE_TTL_S` unless forced).
  No GitHub release lookup.
* **When it can update**: on that branch, nothing uncommitted in tracked
  files, and not diverged (a fast-forward must be possible). Otherwise
  :func:`status` says why in one plain sentence (``why_not``) and nothing
  anywhere nags about it.
* **No branch checked out** (a detached HEAD — left at a release commit by
  hand, say) can update too when nothing would be lost: HEAD is an ancestor
  of (or equal to) the default branch's upstream tip, nothing uncommitted in
  tracked files, and a local default branch, if there is one, is an
  ancestor of HEAD or equal to it. The update first ATTACHES the checkout to
  that branch at the current commit (:func:`attach_plan`: the branch is
  created there, or fast-forwarded to HEAD — files never move, nothing is
  discarded), then runs the ordinary fast-forward. A rollback puts it back
  exactly: detached at the old commit, the local branch where it was (or
  gone again).
* **The update** (:func:`install_lines`, run by self_update's detached
  script, and :func:`apply_here` for ``mindflock update``): ``git fetch``,
  ``git merge --ff-only <the fetched tip>``, then refresh the install
  (:func:`refresh_command`) so a NEW dependency is added — without removing
  anything that is in the venv but not in the lock (voice packages, drivers):
  never ``uv sync``. Then the same restart and health check as a release.
  When the new code does not come back up, the branch is moved back with
  ``git reset --keep <old sha>`` — only while the tree is still clean, never
  ``--hard`` (:func:`rollback_lines`).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

#: Cap on one local git call (status, rev-parse…).
GIT_TIMEOUT_S = 20

#: Cap on the network calls (``ls-remote``, ``fetch``).
NET_TIMEOUT_S = 120

#: How long a remote look is reused (like the release lookup's cache): every
#: open settings screen asks, and the answer moves a few times a day.
REMOTE_TTL_S = 15 * 60

#: The keys of :func:`status` that ``GET /api/update/check`` serves as ``dev``.
PUBLIC_KEYS = (
    "branch",
    "default_branch",
    "dirty",
    "behind",
    "ahead",
    "can_update",
    "why_not",
)

#: checkout -> when its remote was last looked at.
_REMOTE_AT: Dict[str, float] = {}

_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def _env() -> dict:
    # Never a credential prompt from a background check: a remote that
    # wants one just fails, like being offline.
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")


def _git(checkout: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> Tuple[int, str]:
    """``git -C checkout args`` → ``(returncode, stripped stdout)``; 127 when
    git can't run at all. Never raises."""
    try:
        cp = subprocess.run(
            ["git", "-C", checkout, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()


def default_branch(checkout: str, remote: str = "origin") -> str:
    """The branch ``<remote>/HEAD`` names, else ``main`` / ``master`` (the
    first that exists locally or on the remote), else ""."""
    rc, out = _git(
        checkout, "symbolic-ref", "-q", "--short", "refs/remotes/%s/HEAD" % remote
    )
    if rc == 0 and out.startswith(remote + "/"):
        return out[len(remote) + 1 :]
    for name in ("main", "master"):
        for ref in ("refs/heads/" + name, "refs/remotes/%s/%s" % (remote, name)):
            if _git(checkout, "rev-parse", "--verify", "-q", ref)[0] == 0:
                return name
    return ""


def _upstream(checkout: str, branch: str, default: str) -> Tuple[str, str]:
    """``(remote, remote branch)`` the checkout follows: the branch's
    configured upstream, else ``origin/<default branch>``."""
    if branch:
        rc, out = _git(
            checkout, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )
        if rc == 0 and "/" in out:
            remote, rbranch = out.split("/", 1)
            # A remote whose name has a "/" in it is not worth guessing at.
            if _git(checkout, "remote", "get-url", remote)[0] == 0:
                return remote, rbranch
    if default and _git(checkout, "remote", "get-url", "origin")[0] == 0:
        return "origin", default
    return "", ""


def _sync_remote(checkout: str, remote: str, rbranch: str, force: bool) -> None:
    """Bring ``<remote>/<rbranch>`` up to date when the remote moved:
    ``ls-remote`` (one round trip), and a fetch only when it differs from the
    local remote-tracking ref. Throttled per checkout unless ``force``."""
    now = time.time()
    if not force and now - _REMOTE_AT.get(checkout, 0.0) < REMOTE_TTL_S:
        return
    _REMOTE_AT[checkout] = now
    rc, out = _git(
        checkout, "ls-remote", remote, "refs/heads/" + rbranch, timeout=NET_TIMEOUT_S
    )
    theirs = out.split()[0] if rc == 0 and out else ""
    if not theirs:
        return  # offline, or the branch is gone there: keep what we know
    rc, mine = _git(
        checkout,
        "rev-parse",
        "-q",
        "--verify",
        "refs/remotes/%s/%s" % (remote, rbranch),
    )
    if rc == 0 and mine == theirs:
        return
    _git(checkout, "fetch", "--quiet", remote, timeout=NET_TIMEOUT_S)


def _pyproject_version(checkout: str, rev: str) -> str:
    """``[project] version`` of ``pyproject.toml`` at ``rev``, "" when unreadable."""
    rc, text = _git(checkout, "show", "%s:pyproject.toml" % rev)
    if rc != 0 or not text:
        return ""
    try:
        import tomllib

        project = tomllib.loads(text).get("project") or {}
    except Exception:  # noqa: BLE001
        return ""
    return str(project.get("version") or "").strip()


def checked_out_elsewhere(checkout: str, branch: str) -> bool:
    """Whether ``branch`` is checked out in a worktree of this repository
    other than ``checkout``. Unknown (git failed) counts as yes."""
    rc, out = _git(checkout, "worktree", "list", "--porcelain")
    if rc != 0:
        return True
    here = os.path.realpath(checkout)
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree ") :])
        elif line == "branch refs/heads/" + branch and path != here:
            return True
    return False


def published_release_at(checkout: str, remote: str, sha: str) -> str:
    """The published release tag (``v0.14.25`` on ``remote``) whose commit
    is ``sha``, "" when none — a checkout "parked on a release". One
    ``ls-remote``; "" when the remote can't be asked."""
    import re as _re

    rc, out = _git(
        checkout, "ls-remote", "--tags", remote or "origin", timeout=NET_TIMEOUT_S
    )
    if rc != 0 or not _SHA.match(sha or ""):
        return ""
    release = _re.compile(r"^v?[0-9]+(\.[0-9]+){1,3}$")
    hits: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/") :]
        peeled = name.endswith("^{}")
        name = name[:-3] if peeled else name
        if not release.match(name):
            continue
        # An annotated tag's peeled line names its commit; it wins.
        if peeled or name not in hits:
            hits[name] = parts[0]
    for name, commit in sorted(hits.items()):
        if commit == sha:
            return name
    return ""


def attach_plan(checkout: str, head: str, target: str, default: str) -> dict:
    """For a checkout with NO branch checked out: whether the update may
    attach it to ``default`` first → ``{"ok": True, "branch", "create",
    "from"}`` or ``{"ok": False, "why"}`` (one plain sentence saying what to
    do). Dirty tracked files are checked by the caller.

    Safe means nothing is left behind and no file moves: HEAD is reachable
    from the upstream tip ``target`` (so it holds no commit of its own), and
    a local ``default`` branch, when there is one, is HEAD or behind it (so
    moving it up to HEAD only fast-forwards it). ``from`` is that branch's
    sha now ("" = it doesn't exist and is created)."""
    q = shlex.quote
    if not default or not _SHA.match(head or "") or not _SHA.match(target or ""):
        return {
            "ok": False,
            "why": "This development checkout has no branch checked out; switch "
            "it to %s (git switch %s) to get updates."
            % (default or "main", q(default or "main")),
        }
    if _git(checkout, "merge-base", "--is-ancestor", head, target)[0] != 0:
        return {
            "ok": False,
            "why": "This development checkout has no branch checked out and is "
            "at a commit that is not on %s; switch it to %s (git switch %s) to "
            "get updates." % (default, default, q(default)),
        }
    if checked_out_elsewhere(checkout, default):
        # Moving a branch another worktree has checked out would move that
        # worktree's HEAD under its index and files (git's own switch
        # refuses this; update-ref + symbolic-ref would not).
        return {
            "ok": False,
            "why": "This development checkout has no branch checked out, and "
            "%s is checked out in another worktree; update from that worktree "
            "instead." % default,
        }
    rc, local = _git(checkout, "rev-parse", "-q", "--verify", "refs/heads/" + default)
    local = local if rc == 0 and _SHA.match(local) else ""
    if local and local != head:
        if _git(checkout, "merge-base", "--is-ancestor", local, head)[0] != 0:
            return {
                "ok": False,
                "why": "This development checkout has no branch checked out, and "
                "its own %s has commits that are not here; switch to it (git "
                "switch %s) and update from there." % (default, q(default)),
            }
    return {"ok": True, "branch": default, "create": not local, "from": local}


def why_not_sentence(
    branch: str, default: str, dirty: bool, diverged: bool, remote: bool
) -> str:
    """The one plain line Settings shows when a checkout can't update here."""
    target = default or "main"
    tail = "Updates apply when it is on %s with nothing uncommitted." % target
    if not remote:
        return "This development checkout has no remote to update from."
    if not branch:
        if dirty:
            return (
                "This development checkout has no branch checked out and has "
                "uncommitted changes; commit or undo them, then switch it to "
                "%s (git switch %s) to get updates." % (target, shlex.quote(target))
            )
        return "This is a development checkout with no branch checked out. " + tail
    if default and branch != default:
        return "This is a development checkout on branch %s. %s" % (branch, tail)
    if dirty:
        return "This development checkout has uncommitted changes. " + tail
    if diverged:
        return (
            "This development checkout has commits that are not on the remote "
            "%s, so it cannot move forward by itself." % target
        )
    return ""


def status(
    checkout: str = "", *, refresh: bool = False, force: bool = False
) -> Optional[dict]:
    """Where a development checkout stands, or None when this isn't one.

    ``{branch, default_branch, dirty, behind, ahead, can_update, why_not}``
    (:data:`PUBLIC_KEYS`) plus what the update needs: ``checkout``,
    ``remote``, ``upstream`` (``origin/main``), ``head``, ``target`` (the
    upstream's sha) and ``target_version``. ``refresh`` looks at the remote
    first (throttled — :data:`REMOTE_TTL_S` — unless ``force``); without it
    only the local refs are read. Never raises."""
    if not checkout:
        from backend.web.core import self_update

        checkout = self_update.install_checkout()
    if not checkout:
        return None
    rc, inside = _git(checkout, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or inside != "true":
        return {
            "checkout": checkout,
            "branch": "",
            "default_branch": "",
            "dirty": False,
            "behind": 0,
            "ahead": 0,
            "can_update": False,
            "why_not": "This development checkout has no remote to update from.",
            "remote": "",
            "upstream": "",
            "head": "",
            "target": "",
            "target_version": "",
        }
    rc, branch = _git(checkout, "symbolic-ref", "-q", "--short", "HEAD")
    branch = branch if rc == 0 else ""
    default = default_branch(checkout)
    remote, rbranch = _upstream(checkout, branch, default)
    if refresh and remote:
        _sync_remote(checkout, remote, rbranch, force)
    upstream = "%s/%s" % (remote, rbranch) if remote else ""
    rc, porcelain = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    dirty = rc != 0 or bool(porcelain)
    head = _git(checkout, "rev-parse", "HEAD")[1]
    target = ""
    ahead = behind = 0
    if upstream:
        rc, target = _git(
            checkout, "rev-parse", "-q", "--verify", "refs/remotes/" + upstream
        )
        target = target if rc == 0 else ""
    if target:
        rc, counts = _git(
            checkout, "rev-list", "--left-right", "--count", "HEAD..." + target
        )
        parts = counts.split()
        if rc == 0 and len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])
    diverged = ahead > 0 and behind > 0
    why_not = why_not_sentence(branch, default, dirty, diverged, bool(target))
    attach: Optional[dict] = None
    if not branch and target and not dirty:
        plan = attach_plan(checkout, head, target, default)
        if plan.get("ok"):
            attach = {k: plan[k] for k in ("branch", "create", "from")}
            why_not = ""
        else:
            why_not = str(plan.get("why") or why_not)
    return {
        "checkout": checkout,
        "branch": branch,
        "default_branch": default,
        "dirty": dirty,
        "behind": behind,
        "ahead": ahead,
        "can_update": not why_not,
        "why_not": why_not,
        "remote": remote,
        "upstream": upstream,
        "head": head,
        "target": target,
        "target_version": _pyproject_version(checkout, target) if behind > 0 else "",
        # No branch checked out, but it may attach to its default branch
        # first (attach_plan): {branch, create, from}. None otherwise.
        "attach": attach,
    }


def public(st: Optional[dict]) -> Optional[dict]:
    """The ``dev`` object of ``GET /api/update/check`` (None: not a checkout)."""
    if not st:
        return None
    return {k: st.get(k) for k in PUBLIC_KEYS}


# --------------------------------------------------------------------------- #
# The update itself
# --------------------------------------------------------------------------- #
def refresh_command(checkout: str, python: str = "") -> Optional[List[str]]:
    """What refreshes the editable install after the checkout moved: its
    metadata (the version the package reports) and any NEW dependency.

    ``uv pip install -e <checkout>[web] --reinstall-package mindflock`` into
    THIS interpreter — or pip, when there is no uv. Never ``uv sync``: that
    removes every package the lock doesn't name (the voice models, database
    drivers and whatever else a developer put in the venv). ``pip install``
    only adds and upgrades what the project needs. None when neither tool is
    there (the version still reads right: ``backend.__version__`` comes from
    the checkout's pyproject)."""
    python = python or sys.executable
    target = "%s[web]" % checkout
    uv = shutil.which("uv") or shutil.which(
        "uv", path=os.path.join(os.path.expanduser("~"), ".local", "bin")
    )
    if uv:
        return [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            python,
            "--reinstall-package",
            "mindflock",
            "-e",
            target,
        ]
    if python == sys.executable and importlib.util.find_spec("pip") is None:
        return None
    return [python, "-m", "pip", "install", "--quiet", "-e", target]


def _q(argv: List[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def install_lines(st: dict, refresh: Optional[List[str]]) -> List[str]:
    """The script lines that move the checkout to ``$COMMIT`` and refresh the
    install. They leave ``code`` (0 = good) and ``ERR`` (a plain sentence on
    failure) for the shared script; ``PREV`` must be the old HEAD.

    Re-checks what :func:`status` checked — minutes may have passed (the
    automatic path waits for a quiet moment) and someone may have switched
    branches or started editing since."""
    attach = st.get("attach") or None
    branch = st.get("branch") or (attach or {}).get("branch") or ""
    remote = st.get("remote") or "origin"
    refresh_line = _q(refresh) if refresh else ""
    lines = [
        "code=0; ERR=''",
        *_undo_attach_fn(attach),
        'if ! cd %s; then code=1; ERR="the checkout is gone"; fi'
        % shlex.quote(st["checkout"]),
        'if [ "$code" -eq 0 ]; then',
        '  git fetch --quiet %s >> "$LOG" 2>&1 || printf "=== fetch failed; using what is already here ===\\n" >> "$LOG"'
        % shlex.quote(remote),
        "fi",
        *_attach_lines(attach, st),
        'if [ "$code" -eq 0 ]; then',
        # The branch is quoted for the test, and never put into ERR: it ends
        # up inside a double-quoted string and then the state file's JSON.
        '  if [ "$(git symbolic-ref -q --short HEAD)" != %s ]; then code=3; ERR="the checkout is no longer on its branch"; fi'
        % shlex.quote(branch),
        "fi",
        'if [ "$code" -eq 0 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then',
        '  code=3; ERR="the checkout has uncommitted changes now"',
        "fi",
        'if [ "$code" -eq 0 ] && [ "$(git rev-parse HEAD)" != "$PREV" ]; then',
        '  code=3; ERR="the checkout moved while the update waited"',
        "fi",
        'if [ "$code" -eq 0 ]; then',
        '  git merge --ff-only "$COMMIT" >> "$LOG" 2>&1 || { code=$?; ERR="the checkout could not move forward"; }',
        "fi",
        # Refused after the attach: back to no branch, as it was found.
        'if [ "$code" -ne 0 ] && [ -n "$ATTACHED" ]; then undo_attach; fi',
    ]
    if refresh_line:
        lines += [
            'if [ "$code" -eq 0 ]; then',
            '  %s >> "$LOG" 2>&1 || {' % refresh_line,
            "    code=$?",
            '    ERR="the new dependencies could not be installed, so the checkout was moved back"',
            '    if git reset --quiet --keep "$PREV" >> "$LOG" 2>&1; then undo_attach; else ATTACHED=\'\'; ERR="the new dependencies could not be installed"; fi',
            "  }",
            "fi",
        ]
    else:
        lines += [
            '[ "$code" -eq 0 ] && printf "=== no uv or pip here: the install was not refreshed ===\\n" >> "$LOG"',
        ]
    return lines


def _undo_attach_fn(attach: Optional[dict]) -> List[str]:
    """The shell function ``undo_attach`` the rollbacks call after moving
    the branch back to ``$PREV``: for a checkout the update attached, detach
    HEAD at ``$PREV`` again (same commit: no file moves) and put the local
    branch back — deleted when the update created it, else reset to where it
    was. A no-op for a checkout that was already on its branch, and until
    the attach actually happened (``ATTACHED``)."""
    if not attach:
        return ["undo_attach() { :; }"]
    ref = "refs/heads/" + str(attach["branch"])
    restore = (
        'git branch --quiet -D %s >> "$LOG" 2>&1' % shlex.quote(str(attach["branch"]))
        if attach.get("create")
        else 'git update-ref %s %s >> "$LOG" 2>&1'
        % (shlex.quote(ref), shlex.quote(str(attach.get("from") or "")))
    )
    return [
        "ATTACHED=''",
        "undo_attach() {",
        '  [ -n "$ATTACHED" ] || return 0',
        # Only ever from the very commit it was attached at: no file moves.
        '  [ "$(git rev-parse HEAD)" = "$PREV" ] || return 1',
        '  git checkout --quiet --detach "$PREV" >> "$LOG" 2>&1 || return 1',
        "  %s || return 1" % restore,
        "  ATTACHED=''",
        "}",
    ]


def _attach_lines(attach: Optional[dict], st: dict) -> List[str]:
    """The lines that attach a no-branch checkout to its default branch at
    ``$PREV`` before the fast-forward — after re-checking what
    :func:`attach_plan` checked (still no branch, still at ``$PREV``, still
    clean, the local branch still where it was). Never moves a file:
    the branch is made to point at the very commit HEAD is on, then HEAD is
    pointed at the branch (``git symbolic-ref``)."""
    if not attach:
        return []
    name = str(attach["branch"])
    ref = "refs/heads/" + name
    upstream = "%s/%s" % (st.get("remote") or "origin", name)
    if st.get("upstream"):
        upstream = str(st["upstream"])
    lines = [
        'if [ "$code" -eq 0 ] && git symbolic-ref -q HEAD >/dev/null 2>&1; then',
        '  code=3; ERR="the checkout is on a branch now; nothing was changed"',
        "fi",
        'if [ "$code" -eq 0 ] && [ "$(git rev-parse HEAD)" != "$PREV" ]; then',
        '  code=3; ERR="the checkout moved while the update waited"',
        "fi",
        'if [ "$code" -eq 0 ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then',
        '  code=3; ERR="the checkout has uncommitted changes now"',
        "fi",
        # This checkout has no branch: any worktree listing the branch is
        # another one (moving it would move that worktree under its files).
        'if [ "$code" -eq 0 ] && git worktree list --porcelain | grep -qxF %s; then'
        % shlex.quote("branch " + ref),
        '  code=3; ERR="the branch is checked out in another worktree; nothing was changed"',
        "fi",
    ]
    if attach.get("create"):
        lines += [
            'if [ "$code" -eq 0 ] && git rev-parse -q --verify %s >/dev/null 2>&1; then'
            % shlex.quote(ref),
            '  code=3; ERR="a local branch appeared while the update waited; nothing was changed"',
            "fi",
            'if [ "$code" -eq 0 ]; then',
            # Create it AT the current commit; then point HEAD at it.
            '  git update-ref %s "$PREV" "" >> "$LOG" 2>&1 || { code=3; ERR="could not create the local branch"; }'
            % shlex.quote(ref),
            "fi",
        ]
    else:
        lines += [
            'if [ "$code" -eq 0 ]; then',
            # Compare-and-swap: only from where status saw it (a fast-forward
            # up to HEAD — attach_plan checked it is an ancestor).
            '  git update-ref %s "$PREV" %s >> "$LOG" 2>&1 || { code=3; ERR="the local branch moved while the update waited; nothing was changed"; }'
            % (shlex.quote(ref), shlex.quote(str(attach.get("from") or ""))),
            "fi",
        ]
    lines += [
        'if [ "$code" -eq 0 ]; then',
        '  git symbolic-ref HEAD %s >> "$LOG" 2>&1 || code=3' % shlex.quote(ref),
        '  if [ "$code" -eq 0 ]; then ATTACHED=1; else ERR="could not put the checkout on its branch"; fi',
        "fi",
    ]
    if attach.get("create"):
        lines += [
            '[ "$code" -eq 0 ] && { git branch --quiet --set-upstream-to=%s %s >> "$LOG" 2>&1 || true; }'
            % (shlex.quote(upstream), shlex.quote(name)),
        ]
    lines += [
        '[ "$code" -eq 0 ] && printf "=== attached the checkout to %%s at %%s ===\\n" %s "$PREV" >> "$LOG"'
        % shlex.quote(name),
        # A failure after the attach must leave it as it was found.
        'if [ "$code" -ne 0 ] && [ -n "$ATTACHED" ]; then undo_attach; fi',
    ]
    return lines


def rollback_lines(refresh: Optional[List[str]]) -> List[str]:
    """The script lines that move the checkout back to ``$PREV`` after the
    new code failed to come up. They leave ``rc`` (0 = moved back) and may
    ``put failed`` and exit themselves when the tree is no longer clean: a
    ``reset --keep`` then could take someone's edits with it."""
    lines = [
        'if [ -n "$(git status --porcelain --untracked-files=no)" ]; then',
        '  put failed 1 \',"error":"the new version did not start, and the checkout has changes now, so it was left on the new version"\'',
        '  printf "=== the checkout has changes; not moving it back ===\\n" >> "$LOG"',
        "  exit 1",
        "fi",
        'git reset --quiet --keep "$PREV" >> "$LOG" 2>&1',
        "rc=$?",
        # A checkout that was attached to its branch by the update goes back
        # to no branch at the old commit (undo_attach: install_lines).
        '[ "$rc" -eq 0 ] && { undo_attach || rc=$?; }',
    ]
    if refresh:
        lines.append('[ "$rc" -eq 0 ] && { %s >> "$LOG" 2>&1 || true; }' % _q(refresh))
    return lines


# --------------------------------------------------------------------------- #
# The same, in the foreground (``mindflock update``)
# --------------------------------------------------------------------------- #
def _run(argv: List[str], cwd: str, timeout: float = NET_TIMEOUT_S) -> int:
    print("$ " + _q(argv))
    try:
        return int(
            subprocess.run(
                argv, cwd=cwd, check=False, timeout=timeout, env=_env()
            ).returncode
        )
    except (OSError, subprocess.SubprocessError) as err:
        print("error: couldn't run %s: %s" % (argv[0], err), file=sys.stderr)
        return 1


def attach_here(st: dict) -> Tuple[bool, str]:
    """Attach a no-branch checkout to its default branch at the current
    commit (``st["attach"]``, :func:`attach_plan`) — re-checking first that
    nothing changed since :func:`status` looked. Never moves a file: the
    branch is pointed at HEAD's commit, then HEAD at the branch.
    ``(ok, sentence)``; ``(True, "")`` when there is nothing to attach."""
    attach = st.get("attach") or None
    if not attach:
        return True, ""
    checkout, prev = st["checkout"], st.get("head") or ""
    name = str(attach["branch"])
    ref = "refs/heads/" + name
    if _git(checkout, "symbolic-ref", "-q", "HEAD")[0] == 0:
        return False, "the checkout is on a branch now; nothing was changed"
    if head(checkout) != prev:
        return False, "the checkout moved since it was checked; nothing was changed"
    rc, porcelain = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    if rc != 0 or porcelain:
        return False, "the checkout has uncommitted changes now; nothing was changed"
    if checked_out_elsewhere(checkout, name):
        return (
            False,
            "%s is checked out in another worktree; nothing was changed" % name,
        )
    if _git(checkout, "update-ref", ref, prev, str(attach.get("from") or ""))[0] != 0:
        return (
            False,
            "the local %s moved since it was checked; nothing was changed" % name,
        )
    if _git(checkout, "symbolic-ref", "HEAD", ref)[0] != 0:
        _restore_branch(checkout, attach)
        return False, "could not put the checkout on %s; nothing was changed" % name
    if attach.get("create") and st.get("upstream"):
        _git(checkout, "branch", "--quiet", "--set-upstream-to=" + st["upstream"], name)
    print(
        "put the checkout on %s at %s (it had no branch checked out)" % (name, prev[:7])
    )
    return True, ""


def _restore_branch(checkout: str, attach: dict) -> bool:
    name = str(attach["branch"])
    if attach.get("create"):
        return _git(checkout, "branch", "--quiet", "-D", name)[0] == 0
    return (
        _git(
            checkout, "update-ref", "refs/heads/" + name, str(attach.get("from") or "")
        )[0]
        == 0
    )


def detach_back(checkout: str, prev: str, attach: Optional[dict]) -> bool:
    """Undo :func:`attach_here` once the branch is back at ``prev``: HEAD
    detached at ``prev`` again (the same commit: no file moves) and the local
    branch deleted (it was created) or put back where it was. False — and
    nothing touched — when HEAD is not at ``prev``."""
    if not attach:
        return True
    if head(checkout) != prev:
        return False
    if _git(checkout, "checkout", "--quiet", "--detach", prev)[0] != 0:
        return False
    return _restore_branch(checkout, attach)


def apply_here(st: dict) -> Tuple[bool, str]:
    """Move the checkout ``st`` describes to its upstream's tip and refresh
    the install, in the foreground. ``(ok, sentence)``; on a failed refresh
    the checkout is moved back (and a checkout that had no branch checked
    out goes back to having none)."""
    checkout = st["checkout"]
    target = st.get("target") or ""
    prev = st.get("head") or ""
    attach = st.get("attach") or None
    if not st.get("can_update") or not _SHA.match(target):
        return False, st.get("why_not") or "nothing to update to"
    if _run(["git", "fetch", "--quiet", st.get("remote") or "origin"], checkout) != 0:
        print("fetch failed; using what is already here")
    ok, why = attach_here(st)
    if not ok:
        return False, why
    if _run(["git", "merge", "--ff-only", target], checkout) != 0:
        detach_back(checkout, prev, attach)
        return False, "the checkout could not move forward"
    refresh = refresh_command(checkout)
    if refresh and _run(refresh, checkout, timeout=30 * 60) != 0:
        moved = move_back(checkout, prev, refresh=False, attach=attach)
        return False, (
            "the new dependencies could not be installed"
            + (", so the checkout was moved back" if moved else "")
        )
    return True, ""


def move_back(
    checkout: str, prev: str, refresh: bool = True, attach: Optional[dict] = None
) -> bool:
    """``git reset --keep <prev>`` when the tree is clean (then refresh the
    install again); with ``attach`` (the update attached a no-branch
    checkout) HEAD is detached at ``prev`` again and the branch put back.
    False when it wasn't clean or the reset failed."""
    if not _SHA.match(prev or ""):
        return False
    rc, porcelain = _git(checkout, "status", "--porcelain", "--untracked-files=no")
    if rc != 0 or porcelain:
        return False
    if _run(["git", "reset", "--quiet", "--keep", prev], checkout) != 0:
        return False
    detach_back(checkout, prev, attach)
    cmd = refresh_command(checkout) if refresh else None
    if cmd:
        _run(cmd, checkout, timeout=30 * 60)
    return True


def head(checkout: str) -> str:
    """The checkout's HEAD sha, "" when unreadable."""
    rc, out = _git(checkout, "rev-parse", "HEAD")
    return out if rc == 0 and _SHA.match(out) else ""
