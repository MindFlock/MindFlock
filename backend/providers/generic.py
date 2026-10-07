"""A config-driven provider: any coding CLI described by a :class:`ProviderConfig`.

Used for the bundled aider/codex providers and any user-defined CLI. It
launches the program directly (no workspace launcher script — that is
Claude/MindFlock-specific), resuming with the configured flag.
"""

from __future__ import annotations

import shlex
from typing import Optional

from .base import (
    BaseProvider,
    EffortSpec,
    LauncherSpec,
    LaunchContext,
    TrustSpec,
    oneshot_command,
    seed_prompt_expr,
)
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

    # Generic providers don't GENERATE the workspace launcher script (that stays
    # a MindFlock-owned artifact written by the provisioning path), but they do
    # supply the flag vocabulary it launches them with — see launcher_spec.
    def owns_launcher(self, ctx: LaunchContext) -> bool:
        return False

    def launcher_spec(self) -> LauncherSpec:
        """This CLI's provisioned-launcher vocabulary, straight from its config.

        The same four knobs ``build_launch_command`` uses for a plain session, so
        a provisioned session (an ingested ticket, a PR workspace) launches the
        CLI exactly the way a hand-started one does — codex resumes with
        ``resume --last``, goose with ``-r``, aider with
        ``--restore-chat-history`` and no ``|| fresh`` fallback, and none of them
        is handed Claude's ``--dangerously-skip-permissions``.
        """
        cfg = self.cfg
        return LauncherSpec(
            skip_perms_flag=cfg.skip_perms_flag,
            prompt_arg=cfg.prompt_arg,
            resume_flag=cfg.resume_flag,
            resume_fallback=cfg.resume_fallback,
            natural_codes=tuple(cfg.natural_codes),
            # resolved_binary() already folds in the user's binary-path override,
            # so the launcher must not apply one a second time.
            command=cfg.resolved_binary(),
        )

    def effort_spec(self) -> EffortSpec:
        """This CLI's reasoning-effort vocabulary, straight from its config —
        so codex/antigravity and any user TOML provider that declares
        ``[launch] effort_args`` get per-launch effort with no code."""
        cfg = self.cfg
        return EffortSpec(
            args=tuple(cfg.effort_args),
            levels=tuple(cfg.effort_levels),
            ultra_level=cfg.effort_ultra_level,
            prompt_keyword=cfg.effort_keyword,
        )

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
            # tool_hook_events carries the red-zone guard + tool feed on this
            # CLI's Pre/Post events when its config opts in (Codex, or a user
            # TOML with [activity] tool_hook_events); None-valued for a CLI that
            # only reports activity, keeping its hook bytes unchanged.
            activity_markers.merge_activity_hooks(
                settings_path,
                cfg.activity_hook_events,
                session_name,
                record_thread=False,
                tool_hook_events=dict(cfg.tool_hook_events) or None,
                # Only a CLI whose hard guard reads this file gets the
                # disableAllHooks:false pin — Codex's hooks.json rejects the key.
                resist_disable_all=bool(cfg.red_zone_guard),
            )
            activity_markers.ensure_git_excluded(workdir, cfg.activity_hooks_file)
        except Exception:  # noqa: BLE001 — never break a launch over hook install
            return
        # Best-effort: write this worktree's guard file so an already-set repo
        # zone is enforced/detected from the first tool call (never raises).
        try:
            from backend.config import red_zones as _red_zones

            _red_zones.sync_for_workdir(workdir)
        except Exception:  # noqa: BLE001 — guard sync is enrichment only
            pass

    # --- red-zone guard --------------------------------------------------- #
    def red_zone_guard(self) -> bool:
        # Hard block for BOTH zone kinds (red keep-out, green only-here) when
        # the TOML opts in; otherwise zone violations are detect-only (the
        # agent is told they are "flagged and block pushes", not "blocked").
        return bool(self.cfg.red_zone_guard)

    def reports_activity(self) -> bool:
        # Configured hooks are the whole capability here: without them this CLI
        # never writes a marker and pane inspection is all there is.
        return bool(self.cfg.activity_hooks_file)

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

    # --- headless one-shot ------------------------------------------------ #
    def oneshot_argv(self, prompt: str) -> Optional[list[str]]:
        """From the config's ``oneshot_args`` template — None when it has none
        (aider) so the caller falls back to its no-model path."""
        return oneshot_command(
            self.cfg.resolved_binary(), self.cfg.oneshot_args, prompt
        )

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

    # --- peer shared sessions ------------------------------------------- #
    def sandbox_profile(self):
        """From the config's ``sandbox_*`` fields; ``None`` unless it names the
        hosts the CLI needs AND a way to attach the peer MCP (a CLI that can't
        be given the peer tools can't run a shared session)."""
        from .base import SandboxProfile

        cfg = self.cfg
        attaches = bool(
            cfg.peer_mcp_args or cfg.peer_mcp_env or cfg.peer_mcp_home_file[0]
        ) or (type(self).peer_mcp_args is not GenericProvider.peer_mcp_args)
        if not cfg.sandbox_egress or not attaches:
            return None
        return SandboxProfile(
            bin=(cfg.base_command().split() or [self.name])[0],
            egress=tuple(cfg.sandbox_egress),
            passthrough_env=tuple(cfg.auth_env),
            config_env=cfg.sandbox_config_env,
            config_dir=cfg.sandbox_config_dir,
            seed_files=tuple(cfg.sandbox_seed_files),
            env=tuple(cfg.sandbox_env),
        )

    def _peer_render(self, template: str, spec, config_file: str = "") -> str:
        """Fill a ``peer_mcp_*`` template (plain substitution — the values are
        JSON or shell-quoted, so braces in them can't be re-expanded)."""
        import json
        import shlex

        argv = [spec.command, *spec.args]
        env = spec.env
        values = {
            "{command}": spec.command,
            "{command_json}": json.dumps(spec.command),
            "{args_json}": json.dumps(list(spec.args)),
            "{argv_json}": json.dumps(argv),
            "{env_json}": json.dumps(env, sort_keys=True),
            "{argv_shell}": " ".join(shlex.quote(a) for a in argv),
            "{env_shell}": " ".join(
                "%s=%s" % (k, shlex.quote(v)) for k, v in sorted(env.items())
            ),
            # ``env K=V … command args`` — the env travels on the command line
            # itself, for CLIs that filter what env an extension may be given
            # (goose drops PYTHONPATH, without which the server can't import).
            "{argv_env_shell}": " ".join(
                ["env"]
                + ["%s=%s" % (k, shlex.quote(v)) for k, v in sorted(env.items())]
                + [shlex.quote(a) for a in argv]
            ),
            "{server}": spec.server_name,
            "{tools_csv}": ",".join(spec.tool_names),
            "{config_file}": config_file,
        }
        out = []
        i = 0
        while i < len(template):
            for key, val in values.items():
                if template.startswith(key, i):
                    out.append(val)
                    i += len(key)
                    break
            else:
                out.append(template[i])
                i += 1
        return "".join(out)

    def _peer_config_file(self, spec) -> str:
        name, content = self.cfg.peer_mcp_file
        if not name:
            return ""
        return spec.write_run_file(name, self._peer_render(content, spec))

    def peer_mcp_args(self, spec) -> tuple:
        cfg = self.cfg
        if not (cfg.peer_mcp_args or cfg.peer_mcp_env or cfg.peer_mcp_home_file[0]):
            return super().peer_mcp_args(spec)
        home_rel, home_content = cfg.peer_mcp_home_file
        if home_rel:
            spec.write_home_file(home_rel, self._peer_render(home_content, spec))
        config_file = self._peer_config_file(spec)
        return tuple(
            self._peer_render(a, spec, config_file) for a in self.cfg.peer_mcp_args
        )

    def peer_mcp_env(self, spec) -> dict:
        if not self.cfg.peer_mcp_env:
            return {}
        config_file = self._peer_config_file(spec)
        return {
            k: self._peer_render(v, spec, config_file) for k, v in self.cfg.peer_mcp_env
        }

    def login_command(self) -> Optional[str]:
        # A configured login command wins; otherwise fall back to running the
        # CLI itself (base default) so there is always something to open.
        return self.cfg.login_command or super().login_command()

    def auth_evidence(self) -> str:
        from .config import detect_auth

        return detect_auth(self.cfg.auth_files, self.cfg.auth_env)
