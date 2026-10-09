"""Name a failed ``git push`` (or ``ls-remote``) by its cause, with the fix.

A push that fails for want of a credential reads, in the shell, like a wall of
git's own text — and on a fresh machine or WSL distro it is the most likely
way the first push fails: no credential helper, no SSH key, no
``user.name``. The Push button types the push into the session's shell and
returns, so the only evidence is that text. :func:`classify` turns it into
one sentence and the one fix, for the stage watcher's toast
(:mod:`backend.web.core.live_stage`), the engine's own push error
(:mod:`backend.session.git.worktree_git`) and the push-credential preflight
(:mod:`backend.web.core.github_auth`).

Engine-neutral on purpose (stdlib only, no web imports): the session engine
and the CLI import it too.

Every push MindFlock runs sets ``GIT_TERMINAL_PROMPT=0`` (:data:`NO_PROMPT_ENV`),
so a missing HTTPS credential fails at once with "could not read Username"
instead of waiting forever on a prompt in a pane nobody may be looking at.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["NO_PROMPT_ENV", "classify"]

#: The env every non-interactive git network call gets (the shell push types
#: it as a prefix).
NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

#: Where Setup's "Connect GitHub" step lives — every HTTPS fix points there.
CONNECT_HINT = (
    "Connect GitHub in Setup (command palette → Open Setup checklist), then push again"
)

# (id, pattern, message, fix) — first match wins, most specific first.
_RULES = (
    (
        "identity",
        re.compile(
            r"Please tell me who you are|unable to auto-detect email address|"
            r"empty ident name",
            re.I,
        ),
        "git doesn't know who you are on this computer (no user.name / user.email)",
        "set your git name and email in Setup → Connect GitHub, or run "
        'git config --global user.name "Your Name" && '
        "git config --global user.email you@example.com",
    ),
    (
        "https_auth",
        re.compile(
            r"could not read Username|could not read Password|"
            r"terminal prompts disabled|Authentication failed for|"
            r"Invalid username or (password|token)|"
            r"Password authentication is not supported",
            re.I,
        ),
        "git has no GitHub sign-in on this computer for an HTTPS remote",
        CONNECT_HINT
        + " — or run `gh auth login --web && gh auth setup-git` in a terminal",
    ),
    (
        "ssh_auth",
        re.compile(
            r"Permission denied \(publickey|Host key verification failed|"
            r"no such identity|sign_and_send_pubkey",
            re.I,
        ),
        "this computer has no SSH key the remote accepts",
        "add this computer's SSH key to GitHub (ssh-keygen -t ed25519, then "
        "github.com/settings/keys), or switch the remote to HTTPS and "
        + CONNECT_HINT[0].lower()
        + CONNECT_HINT[1:],
    ),
    (
        "forbidden",
        re.compile(
            r"Permission to \S+ denied|The requested URL returned error: 403|"
            r"refusing to allow .* to create or update workflow",
            re.I,
        ),
        "your GitHub sign-in can't push to this repository",
        "check you have write access (or a fork), and that the token has the "
        "`repo` scope (workflow files also need `workflow`)",
    ),
    (
        "not_found",
        re.compile(
            r"Repository not found|does not appear to be a git repository", re.I
        ),
        "the remote repository wasn't found (or your sign-in can't see it)",
        'check `git remote -v` — a private repo also reads as "not found" '
        "without a sign-in that can see it",
    ),
)


def classify(text: str) -> Optional[dict]:
    """``{"id", "message", "fix"}`` for a push failure whose output is
    ``text``, or ``None`` when it names no cause we recognize (a rejected
    non-fast-forward is not an auth problem, and is git's own to explain)."""
    if not text:
        return None
    for rid, pattern, message, fix in _RULES:
        if pattern.search(text):
            return {"id": rid, "message": message, "fix": fix}
    return None
