"""One-click provider login.

A throwaway tmux session that runs a coding-CLI's *own* login flow (``codex
login``, ``opencode auth login``, or just the CLI itself for the ones that
prompt on first launch) so the user can authenticate the provider straight from
the browser — the same PTY<->websocket bridge every other MindFlock terminal
uses (:func:`backend.web.core.terminal.pump_pty`).

Deliberately minimal: the session runs in the user's HOME (login is never
repo-specific) and holds the pane open after the command returns so the result
stays on screen. It is not tracked as an instance — it's a disposable helper the
Settings → Providers panel opens and closes.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional, Tuple

from backend import providers

# tmux session name prefix — kept distinct from instance/shell/assistant names.
_LOGIN_PREFIX = "mindflock_login_"
_DN = subprocess.DEVNULL


def login_session_name(name: str, profile_id: str = "") -> str:
    """Deterministic tmux session name for provider ``name``'s login pane
    (suffixed per auth profile, so logging into a work account never reuses
    the personal login's pane)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", (name or "").strip().lower())
    session = _LOGIN_PREFIX + (safe or "cli")
    if profile_id:
        session += "_" + re.sub(r"[^A-Za-z0-9_.-]", "_", profile_id.strip().lower())
    return session


def _login_command_for(name: str) -> Optional[str]:
    """The provider's login command, or None when ``name`` isn't a real
    provider (the catch-all ``generic`` fallback doesn't count — it's not a
    selectable CLI, so there's nothing to log into)."""
    p = providers.get(name)
    if p is None or p.name == "generic":
        return None
    try:
        return p.login_command()
    except Exception:  # noqa: BLE001 — a provider quirk must not 500 the route
        return None


def ensure_login_session(name: str, profile_id: str = "") -> Tuple[str, Optional[str]]:
    """Ensure a tmux session running ``name``'s login command exists in HOME.

    Returns ``(session_name, error_or_None)``. An unknown provider (or one with
    no login command) is an error, not a spawned empty shell. Idempotent: an
    already-running login session is reused.

    With ``profile_id``, the login runs under that auth profile's isolation env
    (e.g. ``CLAUDE_CONFIG_DIR`` pointed at the profile's account dir, created
    here if needed) so the credential lands in the profile's own store instead
    of the ambient login's.
    """
    session = login_session_name(name, profile_id)
    cmd = _login_command_for(name)
    if not cmd:
        return session, "no login flow for provider '%s'" % name
    if profile_id:
        try:
            from backend.providers import auth_profiles

            profile = auth_profiles.get_profile(profile_id)
            if profile is None:
                return session, "unknown account '%s'" % profile_id
            env = auth_profiles.login_env(profile)
            if not env:
                return session, (
                    "account '%s' has nothing to log into — only 'account'-kind "
                    "profiles carry their own CLI login" % profile_id
                )
            if profile.kind == "account":
                os.makedirs(
                    auth_profiles.account_dir(profile), mode=0o700, exist_ok=True
                )
            import shlex

            exports = "; ".join(
                "export %s=%s" % (k, shlex.quote(v)) for k, v in sorted(env.items())
            )
            # Exported (not a K=V prefix) so the shell the pane drops into after
            # login keeps the profile env — running the CLI right there to
            # verify the login talks to the same account that just logged in.
            cmd = "%s; %s" % (exports, cmd)
        except Exception as err:  # noqa: BLE001 — a profile quirk must not 500
            return session, str(err)
    try:
        if (
            subprocess.run(
                ["tmux", "has-session", "-t=" + session],
                stdout=_DN,
                stderr=_DN,
                timeout=10,
            ).returncode
            == 0
        ):
            return session, None
    except subprocess.TimeoutExpired:
        return session, "tmux timed out after 10s"
    home = os.path.expanduser("~")
    # Keep the pane alive after login returns so the user sees success/failure
    # instead of the session vanishing the instant the command exits.
    wrapped = (
        "%s; echo; echo '[mindflock] login command finished — "
        "you can close this terminal'; exec ${SHELL:-/bin/sh}" % cmd
    )
    try:
        created = subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session,
                "-c",
                home,
                "sh",
                "-c",
                wrapped,
            ],
            stdout=_DN,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return session, "tmux new-session timed out after 10s"
    if created.returncode != 0:
        # Race: two clients opened the login terminal at once; the loser gets
        # "duplicate session". If it exists now, that's success.
        try:
            if (
                subprocess.run(
                    ["tmux", "has-session", "-t=" + session],
                    stdout=_DN,
                    stderr=_DN,
                    timeout=10,
                ).returncode
                == 0
            ):
                return session, None
        except subprocess.TimeoutExpired:
            pass
        return (
            session,
            created.stderr.decode("utf-8", "replace").strip() or "tmux failed",
        )
    for opt, val in (
        ("mouse", "on"),
        ("history-limit", "10000"),
        ("window-size", "latest"),
    ):
        try:
            subprocess.run(
                ["tmux", "set-option", "-t", session, opt, val],
                stdout=_DN,
                stderr=_DN,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            pass  # cosmetic; never block on a wedged tmux
    return session, None


def kill_login_session(name: str, profile_id: str = "") -> None:
    """Tear down provider ``name``'s login session (best-effort)."""
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t=" + login_session_name(name, profile_id)],
            stdout=_DN,
            stderr=_DN,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pass
