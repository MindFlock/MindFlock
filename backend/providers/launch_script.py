"""Shell fragments that start ONE agent CLI, shared by every launch path.

MindFlock launches an agent from three places, and they must agree about which
CLI they are launching and how:

  * the **provisioned workspace launcher** (:mod:`backend.session.provisioned`)
    — the ``.mindflock_launch.sh`` script a ticket/PR session runs,
  * the **standalone tmux launcher** (:mod:`backend.ticket_ingestion`) — the
    detached tmux session + OS terminal tab used when the engine bridge is off,
  * the **web relaunch path**, which re-runs the launcher script above.

Every CLI-specific spelling comes from the provider's
:class:`~backend.providers.base.LauncherSpec`, so adding a CLI stays a
data-only change and no launch path can quietly hardcode one vendor's flags
again — which is exactly how ingestion ended up Claude-only: the launcher
appended ``--dangerously-skip-permissions`` and resumed with ``--continue``
whatever the session's program actually was.

This module deliberately depends only on :mod:`backend.providers` (never on
:mod:`backend.session`), because the standalone launcher is the path that runs
when the engine half of the package is not installed at all.
"""

from __future__ import annotations

import shlex
from typing import Optional, Sequence

from .base import LauncherSpec

__all__ = [
    "SEED_FN",
    "spec_for",
    "apply_binary_override",
    "launch_program",
    "seed_by_keys_function",
    "launch_command",
    "env_exports",
    "local_overlay",
    "profile_overlay",
]

#: Name of the generated shell function that types the seed prompt into a CLI
#: that takes no prompt argument (see :func:`seed_by_keys_function`).
SEED_FN = "mf_seed_prompt"

#: tmux buffer the keystroke seeder pastes from. Named (not the anonymous top
#: buffer) so a paste can never pick up whatever the user last copied.
_SEED_BUFFER = "mf_prompt"

#: How long the keystroke seeder waits for a TUI to finish drawing, in 1s ticks.
_SEED_WAIT_TICKS = 60


def spec_for(program: str) -> LauncherSpec:
    """The launcher flag vocabulary for whichever provider claims ``program``.

    Best-effort: any resolution failure falls back to the provider-NEUTRAL
    default (:class:`LauncherSpec`), never to Claude's flags — the whole point of
    the spec is that a foreign CLI is not handed
    ``--dangerously-skip-permissions`` and ``--continue``, and a lookup error
    must not quietly reinstate that.
    """
    if not program:
        program = "claude"
    try:
        from . import resolve

        return resolve(program).launcher_spec()
    except Exception:  # noqa: BLE001 — never break a launch over a lookup
        return LauncherSpec()


def apply_binary_override(program: str) -> str:
    """Swap a program's executable for a user binary-path override.

    Given ``program`` (e.g. ``"aider --foo"`` or ``"/usr/bin/codex"``), resolve
    which provider claims it and look up that provider's binary override
    (settings ``coding_cli.binary_paths[name]`` / env
    ``MINDFLOCK_PROVIDER_BIN_<NAME>``). If an override is set, replace only the
    executable token (keeping any trailing args); otherwise return ``program``
    unchanged — so with no override the generated launcher is byte-identical to
    the pre-override behaviour.

    Best-effort: any resolution error leaves ``program`` untouched.
    """
    if not program:
        return program
    try:
        from . import resolve
        from .config import binary_override

        rest = program.split()[1:]
        override = binary_override(getattr(resolve(program), "name", "") or "")
        if not override:
            return program
        return " ".join([override, *rest])
    except Exception:  # noqa: BLE001 — never break launch over an override lookup
        return program


def launch_program(program: str, spec: LauncherSpec) -> str:
    """The executable (plus the caller's trailing args) a launch should run.

    Most CLIs are their own interactive entry point, so the program string is
    used verbatim (with a user binary-path override swapped in). Some are not:
    goose opens a chat with ``goose session``, cline needs ``cline -i``, and
    antigravity's binary is ``agy``. Those providers report the real command in
    :attr:`LauncherSpec.command`, which replaces the executable token here while
    keeping any args the caller appended to the program string.
    """
    if not spec.command:
        return apply_binary_override(program)
    rest = program.split()[1:]
    return " ".join([spec.command, *rest])


def seed_by_keys_function(prompt_path: str, fn_name: str = SEED_FN) -> str:
    """A shell function that TYPES the seed prompt into the agent's tmux pane.

    The fallback for a CLI that declares no ``prompt_arg`` — aider, opencode,
    cline and goose all take their first instruction interactively, so a session
    on one of them would otherwise start idle and the ingested ticket would just
    sit there unread. Passing the prompt as argv is still strongly preferred (no
    race, no readiness wait) and is what every ``prompt_arg`` provider does; this
    exists so the CLIs that can't are seeded at all rather than not at all.

    Two details make it survive real TUIs:

    * It waits for the pane to stop changing before typing, so the text is not
      swallowed by a splash screen or a still-drawing layout. The wait is capped
      (~60s) and a stable, non-empty pane breaks out early.
    * It pastes through a named tmux buffer with bracketed paste (``-p``) rather
      than ``send-keys -l``. A multi-line prompt sent as literal keys would have
      every newline read as "submit", firing the ticket at the agent one line at
      a time; a bracketed paste arrives as one block, and the explicit ``Enter``
      after it is what submits.

    Every step is best-effort (``|| return 0``): no tmux, no ``TMUX_PANE``, or a
    refused paste all leave the session running with the prompt still on disk.

    Uses only double quotes — callers embed the result in a single-quoted
    ``bash -ilc '…'`` string, where a single quote would turn into ``'"'"`` soup.
    """
    path = shlex.quote(prompt_path)
    return (
        f"{fn_name}() {{\n"
        '  [ -n "$TMUX_PANE" ] || return 0\n'
        "  local prev= cur= i=0\n"
        f"  while [ $i -lt {_SEED_WAIT_TICKS} ]; do\n"
        "    sleep 1\n"
        '    cur=$(tmux capture-pane -p -t "$TMUX_PANE" 2>/dev/null | tr -dc "[:graph:]")\n'
        '    [ -n "$cur" ] && [ "$cur" = "$prev" ] && break\n'
        "    prev=$cur\n"
        "    i=$((i+1))\n"
        "  done\n"
        f"  tmux load-buffer -b {_SEED_BUFFER} {path} 2>/dev/null || return 0\n"
        f'  tmux paste-buffer -b {_SEED_BUFFER} -p -d -t "$TMUX_PANE" 2>/dev/null || return 0\n'
        "  sleep 1\n"
        '  tmux send-keys -t "$TMUX_PANE" Enter 2>/dev/null || true\n'
        "}\n"
    )


def env_exports(env) -> str:
    """``export K=V`` lines for ``env`` (sorted, values shell-quoted), or ``""``.

    Sorted so the generated script is byte-stable across runs — the launcher is
    golden-tested and a dict-order change must not read as a behaviour change.
    """
    if not env:
        return ""
    return "".join(
        "export %s=%s\n" % (k, shlex.quote(str(v))) for k, v in sorted(env.items())
    )


def local_overlay(program: str) -> tuple[dict, tuple]:
    """``(env, launch_args)`` pointing ``program`` at the user's local model.

    ``({}, ())`` when local models are off, unconfigured, or unsupported for this
    CLI — so every launch path can apply the result unconditionally. Imported
    lazily and defensively: a settings read must never break a launch.
    """
    try:
        from . import local_models

        return local_models.launch_overlay(program)
    except Exception:  # noqa: BLE001
        return {}, ()


def profile_overlay(program: str, profile_id: str = "") -> tuple[dict, tuple]:
    """``(env, launch_args)`` running ``program`` under an auth profile.

    ``profile_id`` is the session's stored id (``""`` = inherit the global
    default profile; ``"default"`` = explicitly none — see
    :mod:`backend.providers.auth_profiles`). Same contract and same defensive
    posture as :func:`local_overlay`: ``({}, ())`` means "behave exactly as
    before", and a settings read must never break a launch.
    """
    try:
        from . import auth_profiles

        return auth_profiles.launch_overlay(program, profile_id)
    except Exception:  # noqa: BLE001
        return {}, ()


def launch_command(
    program: str,
    prompt_path: str = "",
    *,
    skip_permissions: bool = False,
    launch_args: Sequence[str] = (),
    spec: Optional[LauncherSpec] = None,
) -> tuple[str, str]:
    """A one-shot launch of ``program``, seeded with the prompt at ``prompt_path``.

    Returns ``(preamble, command)``: ``preamble`` is shell to emit *before*
    ``command`` (the keystroke-seeder function definition, or ``""``), and
    ``command`` starts the CLI. ``$?`` after ``command`` is the CLI's own exit
    code in both shapes, so callers can keep inspecting it.

    This is the *standalone* launch — one fresh start, no resume chain and no
    restart loop; :func:`backend.session.provisioned.write_launcher` builds the
    long-lived variant for engine sessions. ``prompt_path`` is used verbatim, so
    pass an absolute path when the caller's cwd is not the prompt's directory.
    """
    spec = spec if spec is not None else spec_for(program)
    base = launch_program(program, spec)
    # Local-model routing (Ollama / LM Studio / any OpenAI-compatible server):
    # its flags ride on the base command and its env is exported in the preamble,
    # so the CLI talks to localhost and nothing leaves the machine. Empty when
    # the feature is off. The auth-profile overlay composes the same way (this
    # standalone path has no per-session pin, so it runs under the app-wide
    # default profile).
    local_env, local_args = local_overlay(program)
    prof_env, prof_args = profile_overlay(program)
    args = tuple(local_args) + tuple(prof_args) + tuple(launch_args)
    if args:
        base = "%s %s" % (base, " ".join(shlex.quote(str(a)) for a in args))
    if skip_permissions and spec.skip_perms_flag:
        base = "%s %s" % (base, spec.skip_perms_flag)
    # On a key collision the LOCAL overlay wins, here and on every other launch
    # path: local models are the privacy feature, and an auth profile quietly
    # re-routing a session off the machine is the one outcome that story cannot
    # afford.
    preamble = env_exports({**prof_env, **local_env})
    if not prompt_path:
        return preamble, base
    if spec.prompt_arg:
        seed = spec.prompt_arg.format(prompt=f'"$(cat {shlex.quote(prompt_path)})"')
        return preamble, "%s %s" % (base, seed)
    # No prompt argument: background the keystroke seeder and run the CLI in the
    # foreground. `seeder & cli` keeps $? pointing at the CLI.
    return preamble + seed_by_keys_function(prompt_path), "%s & %s" % (SEED_FN, base)
