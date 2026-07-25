"""A config-driven provider: any coding CLI described by a :class:`ProviderConfig`.

Used for the bundled aider/codex providers and any user-defined CLI. It
launches the program directly (no workspace launcher script — that is
Claude/MindFlock-specific), resuming with the configured flag.
"""

from __future__ import annotations

import shlex
from typing import Optional

from .base import BaseProvider, LaunchContext, TrustSpec, seed_prompt_expr
from .config import ProviderConfig


class GenericProvider(BaseProvider):
    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.program_aliases = tuple(cfg.program_aliases)

    # --- launch ----------------------------------------------------------- #
    def build_launch_command(self, ctx: LaunchContext) -> Optional[str]:
        cfg = self.cfg
        # A CLI with its own hooks engine (declared in its config) gets the
        # activity-reporting marker hooks installed right before launch. The
        # engine's plain/in-place launch path drives every provider through
        # build_launch_command, so this is where Codex (and any hook-capable
        # user CLI) picks them up — the same lifecycle spot Claude installs its
        # own. No-op for CLIs without a hooks file. Best-effort: the web launch
        # path also calls install_activity_hooks() (idempotent merge).
        if ctx.workdir and ctx.session_name:
            self.install_activity_hooks(ctx.workdir, ctx.session_name)
        # ``resolved_binary`` honors a user binary-path override (settings/env),
        # falling back to ``command``/``name`` — so with no override this is
        # byte-identical to the pre-override behaviour.
        base = cfg.resolved_binary()
        launch_args = cfg.launch_args_shell()
        if launch_args:
            base = "%s %s" % (base, launch_args)
        if ctx.launch_args:
            base = "%s %s" % (base, " ".join(shlex.quote(a) for a in ctx.launch_args))
        if ctx.skip_permissions and cfg.skip_perms_flag:
            base = "%s %s" % (base, cfg.skip_perms_flag)
        # Seed an initial prompt as a launch argument when the provider declares
        # how to pass one (``prompt_arg`` template). ``fresh`` is the base with
        # that seed appended, so a first launch — and a resume that has nothing
        # to continue (the ``|| fresh`` fallbacks below) — starts the agent on
        # the prompt instead of idle. Without ``prompt_arg`` this is exactly the
        # base command, so providers that can't be seeded are unchanged.
        fresh = base
        if cfg.prompt_arg:
            expr = seed_prompt_expr(ctx.session_name, ctx.prompt)
            if expr:
                fresh = "%s %s" % (base, cfg.prompt_arg.format(prompt=expr))
        if ctx.resume:
            # Resume THIS window's own conversation when one was recorded —
            # the bulk resume flag (`resume --last` / `--continue`) picks the
            # directory's newest thread, wrong when several windows share the
            # workdir. A vanished id falls back to a FRESH launch (not the bulk
            # flag, which would steal a sibling's conversation).
            if cfg.resume_id_flag:
                tid = (
                    self.resume_thread_id(ctx.session_name) if ctx.session_name else ""
                )
                if tid:
                    by_id = "%s %s" % (
                        base,
                        cfg.resume_id_flag.format(id=shlex.quote(tid)),
                    )
                    return "%s || %s" % (by_id, fresh)
            if cfg.resume_flag:
                resume_cmd = "%s %s" % (base, cfg.resume_flag)
                if cfg.resume_fallback:
                    return "%s || %s" % (resume_cmd, fresh)
                return resume_cmd
            # No resume flag -> relaunch fresh.
            return fresh
        return fresh

    # Generic providers are launched directly; the workspace launcher script is
    # Claude/MindFlock-specific, so they never own one.
    def owns_launcher(self, ctx: LaunchContext) -> bool:
        return False

    # --- exit / resume policy --------------------------------------------- #
    def is_natural_exit(self, code) -> bool:
        return code in self.cfg.natural_codes

    # --- terminal classification ------------------------------------------ #
    def trust_prompt(self) -> Optional[TrustSpec]:
        if not self.cfg.trust_patterns:
            return None
        return TrustSpec(
            patterns=tuple(self.cfg.trust_patterns),
            keystroke=self.cfg.keystroke_bytes(),
        )

    def idle_prompt_pattern(self) -> Optional[str]:
        return self.cfg.idle_pattern or None

    def waiting_prompt_patterns(self) -> tuple:
        return tuple(self.cfg.waiting_patterns)

    def working_pane_patterns(self) -> tuple:
        return tuple(self.cfg.working_patterns)

    def progress_token_pattern(self) -> Optional[str]:
        return self.cfg.progress_pattern or None

    # --- activity signal --------------------------------------------------- #
    def install_activity_hooks(self, workdir: str, session_name: str) -> None:
        """Install per-session ``{state, ts}`` marker hooks into the CLI's own
        hooks config, when this provider's config declares one
        (:attr:`ProviderConfig.activity_hooks_file`).

        Data-driven mirror of :meth:`ClaudeProvider.install_activity_hooks`: the
        file path and event→state map come from the config, so Codex (and any
        hook-capable user CLI) reports ``working``/``idle``/``clarify`` the same
        authoritative way Claude does — superseding the pane regexes. No-op for
        providers whose config names no hooks file. Best-effort: a launch must
        never break over hook install.

        ``record_thread=False``: a config-driven provider records its own
        resume-thread id its way (Codex reads it from its rollout files for the
        ``resume {id}`` flag), so the hook must not also write a thread marker
        from the payload's ``session_id`` and clobber it.
        """
        cfg = self.cfg
        if not cfg.activity_hooks_file or not cfg.activity_hook_events:
            return
        import os
        from pathlib import Path

        from . import activity_markers

        try:
            if not workdir or not session_name or not os.path.isdir(workdir):
                return
            settings_path = Path(workdir) / cfg.activity_hooks_file
            activity_markers.merge_activity_hooks(
                settings_path,
                cfg.activity_hook_events,
                session_name,
                record_thread=False,
            )
            activity_markers.ensure_git_excluded(workdir, cfg.activity_hooks_file)
        except Exception:  # noqa: BLE001 — never break a launch over hook install
            return

    def activity_state(self, session_name: str) -> Optional[str]:
        # Only trust a marker when this CLI actually writes one (hooks declared);
        # otherwise no signal, so the web layer uses pane inspection unchanged.
        if not self.cfg.activity_hooks_file:
            return None
        from . import activity_markers

        return activity_markers.read_activity_marker(session_name)

    def activity_state_age(self, session_name: str) -> Optional[float]:
        if not self.cfg.activity_hooks_file:
            return None
        from . import activity_markers

        return activity_markers.read_activity_marker_age(session_name)

    # --- usage-limit detection (roadmap D) -------------------------------- #
    def usage_limit_patterns(self):
        if self.cfg.usage_limit_patterns:
            return self.cfg.usage_limit_patterns
        return super().usage_limit_patterns()

    # --- usage-window knowledge (roadmap E) ------------------------------- #
    def usage_window(self) -> dict:
        return self.cfg.usage_window()

    def usage_panel_visible(self) -> bool:
        return self.cfg.usage_visible

    # --- connection: install + login -------------------------------------- #
    def install_hint(self) -> str:
        return self.cfg.install_hint

    def login_command(self) -> Optional[str]:
        # A configured login command wins; otherwise fall back to running the
        # CLI itself (base default) so there is always something to open.
        return self.cfg.login_command or super().login_command()

    def auth_evidence(self) -> str:
        from .config import detect_auth

        return detect_auth(self.cfg.auth_files, self.cfg.auth_env)
