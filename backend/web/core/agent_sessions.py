"""tmux plumbing for a session's two panes: the agent CLI and the shell.

Naming (:func:`_shell_tmux_name`, :func:`_live_session_name`), lifecycle
(:func:`_ensure_shell_session`, :func:`_ensure_agent_session` — which decides
fresh-start vs ``--continue`` resume from the exit marker the launch wrapper
records — and the ``_kill_*`` teardown helpers), and typed input
(:func:`_send_to_shell`, :func:`_send_to_agent`).

Split out of ``backend.web.server`` (which re-imports these names — the
routes, the queue drain, the window-refresh keepalive, and tests reference
them through the server namespace).
"""

from __future__ import annotations

import os
import subprocess
import time

from backend import providers
from backend.session import provisioned as provisioning
from backend.session import tmux
from backend.web.core.terminal import (
    apply_scroll_speed,
    _clear_exit_marker,
    _read_exit_marker,
    _wrap_launch_cmd,
)


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _shell_tmux_name(title: str) -> str:
    """tmux session name for the interactive shell pane of an instance."""
    return tmux.to_mindflock_tmux_name(title) + "_sh"


def _live_session_name(name: str):
    """The session name if a tmux session with it exists, else None."""
    if (
        _server()
        ._run_capped(
            ["tmux", "has-session", "-t=" + name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        .returncode
        == 0
    ):
        return name
    return None


def _ensure_shell_session(title: str, wt: str):
    """Ensure the interactive shell tmux session exists in ``wt``.

    Returns ``(name, error_or_None)``.
    """
    srv = _server()
    name = srv._shell_tmux_name(title)
    live = srv._live_session_name(name)
    if live is not None:
        return live, None  # attach the existing session
    shell = os.environ.get("SHELL") or "/bin/bash"
    created = srv._run_capped(
        ["tmux", "new-session", "-d", "-s", name, "-c", wt, shell],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if created.returncode != 0:
        # Race: the shell websocket (opened by switchToTerminal) and the
        # commit both ensure this session at once; the loser gets "duplicate
        # session". If it exists now, that's success, not an error.
        if (
            srv._run_capped(
                ["tmux", "has-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        ):
            return name, None
        return name, created.stderr.decode("utf-8", "replace").strip()
    # mouse ON for wheel scroll / tmux copy-mode. Browser text selection is
    # still available via Shift+drag (xterm.js shouldForceSelection), which
    # the copy-on-select handler copies to the clipboard.
    for opt, val in (
        ("mouse", "on"),
        ("history-limit", "100000"),
        ("window-size", "latest"),
        ("alternate-screen", "off"),
    ):
        srv._run_capped(
            ["tmux", "set-option", "-t", name, opt, val],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    apply_scroll_speed()  # wheel speed (global; re-asserted per server)
    return name, None


def _ensure_agent_session(inst, title: str):
    """Ensure the agent tmux session exists, recreating (rebooting) it if it has
    died. Returns ``(name, error_or_None)``; a non-None error means the workspace
    is gone (caller should stop retrying).

    The session ALWAYS restarts on its own. What changes is whether it *resumes*
    the prior conversation: a clean quit (Ctrl+C / exit, codes 0/130) restarts
    fresh, while an unnatural death (kill/crash, or no exit marker) resumes via
    --continue. This is decided from the exit code the launch wrapper records.

    In-place sessions (copies, and any session running directly in an existing
    repo) are *borrowing* a worktree, so they never reuse a launcher left there
    by the worktree's owner — they always start the program fresh (a new thread,
    not a --continue resume of someone else's conversation)."""
    srv = _server()
    name = tmux.to_mindflock_tmux_name(title)
    # Attach an already-running session instead of spawning a duplicate agent
    # on the same worktree.
    live = srv._live_session_name(name)
    if live is not None:
        return live, None
    wt = inst.GetWorktreePath()
    if not wt or not os.path.isdir(wt):
        return name, "workspace no longer exists"
    provider = providers.resolve(inst.Program)
    # How did it die? Natural (clean quit) -> restart fresh; unnatural (kill /
    # crash / no marker) -> resume the conversation. The provider owns the
    # exit-code policy (claude: 0/130 = clean).
    resume = not provider.is_natural_exit(_read_exit_marker(name))
    # Auth-profile routing has to be re-derived HERE too, for the same reason
    # as the local-model overlay below: the engine put the profile env on the
    # first start's tmux session, but a relaunch builds a fresh command from
    # inst state. Re-resolving from inst.ProfileId is also exactly what makes a
    # profile SWAP take effect: kill the agent session, and the next ensure
    # relaunches under the new identity. No-op when no profile applies.
    prof_env: dict = {}
    prof_args: tuple = ()
    try:
        prof_env, prof_args = providers.launch_script.profile_overlay(
            inst.Program or "",
            getattr(inst, "ProfileId", "") or "",
            getattr(inst, "ProfileModel", "") or "",
        )
    except Exception:  # noqa: BLE001 — never block a relaunch over settings
        pass
    launcher = os.path.join(wt, provisioning.LAUNCHER_BASENAME)
    use_launcher = os.path.isfile(launcher) and not getattr(inst, "InPlace", False)
    if use_launcher:
        # The launcher handles --continue itself; just re-run it. The profile
        # env is exported in front (it is never baked into the script): the
        # exports survive the launcher's `exec bash -ilc` chain, so every link
        # of its resume loop runs under the profile.
        cmd = launcher
    else:
        # Plain / in-place session. The provider builds the launch command
        # (claude: resume via --resume <id>/--continue with a retried fallback;
        # a custom program runs bare — resuming a killed thread, starting clean
        # after a quit). ``None`` -> use the bare program.
        # Local-model routing has to be re-derived HERE, not inherited: the
        # engine applied it when the session first started, but a relaunch
        # rebuilds the command from inst.Program/LaunchArgs, which never carried
        # the overlay. Without this a rebooted local-model session would quietly
        # go back to the CLI's hosted API. No-op when the feature is off.
        local_env: dict = {}
        local_args: tuple = ()
        try:
            local_env, local_args = providers.launch_script.local_overlay(
                inst.Program or ""
            )
        except Exception:  # noqa: BLE001 — never block a relaunch over settings
            pass
        cmd = provider.build_launch_command(
            providers.LaunchContext(
                program=inst.Program or "",
                resume=resume,
                skip_permissions=False,
                in_place=bool(getattr(inst, "InPlace", False)),
                session_name=name,
                launch_args=tuple(local_args)
                + tuple(prof_args)
                + tuple(getattr(inst, "LaunchArgs", ()) or ()),
            )
        )
        if cmd is None:
            cmd = inst.Program
        # Export the env in FRONT of the command: this runs under `sh -c`, and
        # the `||` fallback chains mean a `K=V cmd` prefix would only cover the
        # first link of the chain.
        if local_env:
            cmd = providers.launch_script.env_exports(local_env) + cmd
    if prof_env:
        cmd = providers.launch_script.env_exports(prof_env) + cmd
    # (Re)install the provider's activity-reporting hooks with THIS session's
    # name right before launching, so the CLI announces working/idle/clarify
    # for the run we are about to start (Claude snapshots hook config at
    # process start; copies sharing a worktree each pin their own name here).
    try:
        provider.install_activity_hooks(wt, name)
    except Exception:  # noqa: BLE001 — activity hooks are best-effort
        pass
    # Launch through the exit-recording wrapper and drop any stale marker so the
    # next death is judged fresh.
    _clear_exit_marker(name)
    # A FRESH start begins a new conversation — drop the recorded thread id so
    # a later crash-resume can't target a conversation this run never had.
    # (A resume keeps it: that id is exactly what the launch command targets.)
    if not resume:
        try:
            from backend.providers import thread_markers

            thread_markers.clear(name)
        except Exception:  # noqa: BLE001 — markers are enrichment only
            pass
    wrapped = _wrap_launch_cmd(cmd, name)
    created = srv._run_capped(
        ["tmux", "new-session", "-d", "-s", name, "-c", wt, "sh", "-c", wrapped],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if created.returncode != 0:
        # Race: two terminal clients can ensure the agent session at once; the
        # loser gets "duplicate session". If it exists now, treat as success.
        if (
            srv._run_capped(
                ["tmux", "has-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        ):
            return name, None
        return name, created.stderr.decode("utf-8", "replace").strip()
    # alternate-screen off: don't honor the TUI's alt-screen request, so its
    # output scrolls into tmux history — that's what makes "Copy all" (and
    # wheel-scroll through real history) work for EVERY cli tool, not just ones
    # with their own transcript files.
    for opt, val in (
        ("mouse", "on"),
        ("history-limit", "10000"),
        ("window-size", "latest"),
        ("alternate-screen", "off"),
    ):
        srv._run_capped(
            ["tmux", "set-option", "-t", name, opt, val],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    apply_scroll_speed()  # wheel speed (global; re-asserted per server)
    return name, None


def _send_to_shell(name: str, command: str) -> None:
    """Type ``command`` into the shell tmux session and press Enter."""
    srv = _server()
    srv._run_capped(
        ["tmux", "send-keys", "-t", name, "-l", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    srv._run_capped(
        ["tmux", "send-keys", "-t", name, "Enter"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def _send_to_agent(name: str, text: str, submit: bool = True) -> bool:
    """Type ``text`` into the agent tmux session; press Enter when ``submit``.

    The same ``send-keys -l`` primitive the commit plumbing uses, but the Enter
    is a SEPARATE ``send-keys Enter`` a beat later — an agent TUI (claude) treats
    a text+newline burst as a paste and turns the ``\\r`` into a literal newline
    in its input box instead of submitting (the same subtlety the mobile compose
    box handles). Returns False if the session doesn't exist or tmux errored, so
    callers (the single-send endpoint, the queue drain) can report/retry."""
    srv = _server()
    if not name:
        return False
    exists = (
        srv._run_capped(
            ["tmux", "has-session", "-t=" + name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode
        == 0
    )
    if not exists:
        return False
    typed = srv._run_capped(
        ["tmux", "send-keys", "-t", name, "-l", text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if typed.returncode != 0:
        return False
    if submit:
        time.sleep(0.15)  # end the paste burst so Enter submits, not newlines
        srv._run_capped(
            ["tmux", "send-keys", "-t", name, "Enter"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    return True


def _send_escape_to_agent(name: str) -> bool:
    """Send a single Escape keypress to the agent tmux session (best-effort).

    Used by the queue drain to dismiss a lingering usage-limit menu the CLI
    leaves on screen after the window reopens — pressing Esc drops back to a
    normal prompt so the next queued message lands instead of selecting a menu
    entry. Returns False if the session is gone or tmux errored."""
    srv = _server()
    if not name:
        return False
    exists = (
        srv._run_capped(
            ["tmux", "has-session", "-t=" + name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode
        == 0
    )
    if not exists:
        return False
    rc = srv._run_capped(
        ["tmux", "send-keys", "-t", name, "Escape"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    return rc.returncode == 0


def _kill_named_session(name: str) -> None:
    """Kill the tmux session ``name`` (best-effort)."""
    _server()._run_capped(
        ["tmux", "kill-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def _kill_shell_session(title: str) -> None:
    srv = _server()
    srv._kill_named_session(srv._shell_tmux_name(title))


def _kill_agent_session(title: str) -> None:
    """Kill just the agent tmux session (does NOT touch the git worktree)."""
    _server()._kill_named_session(tmux.to_mindflock_tmux_name(title))
