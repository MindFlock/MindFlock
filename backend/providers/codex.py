"""The OpenAI Codex CLI provider.

Config-driven like the other bundled CLIs (launch/resume/trust all come from its
:class:`ProviderConfig` in ``config.py``), but with live plan usage, paid-mode
detection, and per-session token telemetry wired to ``codex_usage_api`` — the
same three things the Claude provider surfaces, read from Codex's own on-disk
session rollout files. Everything degrades to the generic behaviour on failure.
"""

from __future__ import annotations

from typing import Optional

from .generic import GenericProvider


class CodexProvider(GenericProvider):
    # --- usage-window knowledge ------------------------------------------- #
    def usage_mode(self) -> str:
        """Prefer the real auth mode from ``~/.codex/auth.json`` (ChatGPT plan
        -> windowed, API key -> metered); fall back to the config's window-kind
        default when auth is unknown."""
        try:
            from . import codex_usage_api

            mode = codex_usage_api.usage_mode()
            if mode:
                return mode
        except Exception:  # noqa: BLE001 — auth probe is best-effort
            pass
        return super().usage_mode()

    def usage_live(self) -> Optional[dict]:
        """Live window %/reset from Codex's newest session rollout snapshot."""
        try:
            from . import codex_usage_api

            return codex_usage_api.live_usage()
        except Exception:  # noqa: BLE001 — live is enrichment only
            return None

    def usage_periods(self) -> Optional[dict]:
        """Rolling day/week/month/year token+cost totals from Codex's rollout
        history (parity with Claude's transcript-derived breakdown)."""
        try:
            from . import codex_usage_api

            return codex_usage_api.windows()
        except Exception:  # noqa: BLE001 — history is optional
            return None

    # --- per-session resume thread ----------------------------------------- #
    def record_thread(self, session_name: str, workdir: str, since_ts=None) -> None:
        """Bind this window to its own codex session id (from the rollout file
        created for the current run), so a crash-resume targets ``codex resume
        <id>`` instead of ``resume --last`` (which grabs whichever sibling on
        the same directory spoke last)."""
        try:
            from . import codex_usage_api, thread_markers

            sid = codex_usage_api.find_thread_id(
                workdir,
                since_ts,
                exclude=thread_markers.claimed(exclude_session=session_name),
            )
            if sid and sid != thread_markers.read(session_name):
                thread_markers.record(session_name, sid)
        except Exception:  # noqa: BLE001 — thread binding is enrichment only
            pass

    # --- telemetry -------------------------------------------------------- #
    def session_tokens(
        self,
        workdir: str,
        since_ts: Optional[float],
        until_ts: Optional[float] = None,
        shared_cwd: bool = False,
    ) -> dict:
        """Tokens for Codex sessions in ``workdir`` since ``since_ts`` (parsed
        from the CLI's rollout ``token_count`` events). Falls back to the
        generic empty telemetry when there's nothing on disk yet."""
        try:
            from . import codex_usage_api

            got = codex_usage_api.session_usage(workdir, since_ts, until_ts)
            if got:
                return got
        except Exception:  # noqa: BLE001 — telemetry is best-effort
            pass
        return super().session_tokens(workdir, since_ts, until_ts, shared_cwd)
