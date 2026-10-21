"""Name a failed ``git push`` (or ``ls-remote``) by its cause, with the fix —
and the git environment in which MindFlock's own GitHub sign-in answers.

A push that fails for want of a credential reads, in the shell, like a wall of
git's own text — and on a fresh machine or WSL distro it is the most likely
way the first push fails: no credential helper, no SSH key, no
``user.name``. The Push button types the push into the session's shell and
returns, so the only evidence is that text. :func:`classify` turns it into
one sentence and the one fix, for the stage watcher's toast
(:mod:`backend.web.core.live_stage`), the engine's own push error
(:mod:`backend.session.git.worktree_git`) and the push-credential preflight
(:mod:`backend.web.core.github_auth`).

Engine-neutral on purpose (stdlib only at import time, no web imports): the
session engine and the CLI import it too. :func:`github_signin` and
:func:`github_env` reach for the web layer's credential resolver lazily and
degrade to the plain environment without it.

Every push MindFlock runs sets ``GIT_TERMINAL_PROMPT=0`` (:data:`NO_PROMPT_ENV`),
so a missing HTTPS credential fails at once with "could not read Username"
instead of waiting forever on a prompt in a pane nobody may be looking at.

**The sign-in reaches git** through :func:`with_credential_helper`: two
``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` pairs (git's per-process config)
that make ``mindflock git-credential`` the ONE credential helper for
https://github.com — an empty value first, which resets the helper list git
built from the system, global and repo config (macOS's ``osxkeychain``, Git
Credential Manager, ``store``), then ours. That is exactly what ``gh auth
setup-git`` writes to the global config, done per process instead: no git
config file is written, every other host keeps the user's own helpers, and
the OS keychain is never consulted for github.com — which is what used to
throw "git-credential-osxkeychain wants to use your confidential information"
at every clone, fetch and push on a Mac whose keychain item was made by a
different git. Every git call MindFlock makes against GitHub gets it
(:func:`github_env`), and so does git inside a session (the agent's own push,
the Push button's) through the tmux env (:func:`github_signin` of ``{}``).
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import Mapping, Optional

__all__ = [
    "NO_PROMPT_ENV",
    "HELPER_KEY",
    "classify",
    "helper_command",
    "with_credential_helper",
    "github_signin",
    "github_env",
]

#: The env every non-interactive git network call gets (the shell push types
#: it as a prefix).
NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

#: git's config key naming the credential helpers for github.com over HTTPS.
HELPER_KEY = "credential.https://github.com.helper"


def helper_command() -> str:
    """The ``!…`` credential-helper command that runs THIS install's
    ``mindflock git-credential``: the interpreter the server runs under, in
    isolated mode (``-I`` — the worktree git runs in may hold a ``backend/``
    of its own, which must not shadow the installed package), so it is the
    same code and needs no PATH. ``""`` without an interpreter."""
    exe = sys.executable or ""
    if not exe:
        return ""
    return "!%s -I -m backend.cli git-credential" % shlex.quote(exe)


def with_credential_helper(
    env: Optional[Mapping[str, str]] = None, helper: str = ""
) -> dict:
    """A copy of ``env`` plus git's per-process config making ``helper``
    (default :func:`helper_command`) the one credential helper for
    https://github.com: the two pairs described in the module docstring,
    appended after any ``GIT_CONFIG_*`` pairs ``env`` already carries (git ≥
    2.31; an older git ignores them and keeps its own helpers). Unchanged
    when there is no helper command, or when ``env`` already carries this
    helper (so laying it on twice is harmless)."""
    out = dict(env or {})
    helper = helper or helper_command()
    if not helper:
        return out
    try:
        n = int(out.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    for i in range(n):
        if (
            out.get("GIT_CONFIG_KEY_%d" % i) == HELPER_KEY
            and out.get("GIT_CONFIG_VALUE_%d" % i) == helper
        ):
            return out
    for i, value in enumerate(("", helper), start=n):
        out["GIT_CONFIG_KEY_%d" % i] = HELPER_KEY
        out["GIT_CONFIG_VALUE_%d" % i] = value
    out["GIT_CONFIG_COUNT"] = str(n + 2)
    return out


def github_signin(env: Optional[Mapping[str, str]] = None) -> dict:
    """A copy of ``env`` with MindFlock's GitHub sign-in as git's helper for
    github.com (:func:`with_credential_helper`) when one is connected —
    :func:`backend.web.core.github_auth.git_env` decides. ``env`` as is
    without the web layer, or when nothing is connected: git's own helpers
    then answer as before. ``github_signin({})`` is the overlay alone, for a
    tmux session's extra env."""
    try:
        from backend.web.core import github_auth
    except Exception:  # noqa: BLE001 — the engine without the web layer
        return dict(env or {})
    try:
        return github_auth.git_env(env or {})
    except Exception:  # noqa: BLE001 — never fail a git call over this
        return dict(env or {})


def github_env(base: Optional[Mapping[str, str]] = None) -> dict:
    """The full environment for a git subprocess that may reach github.com:
    ``base`` laid over this process's environment, through
    :func:`github_signin`."""
    env = dict(os.environ)
    env.update(base or {})
    return github_signin(env)


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
