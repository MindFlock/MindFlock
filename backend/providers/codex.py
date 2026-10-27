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
    # --- MindFlock MCP auto-attach ----------------------------------------- #
    def mcp_launch_args(self, spec) -> tuple:
        """ONE ``-c mcp_servers.mindflock={...}`` override (a TOML inline table,
        see :func:`backend.providers.mcp_attach.codex_server_table`).

        ``-c`` is a global option, so it stays valid in front of the generic
        launch's ``resume`` subcommand. A per-launch override rather than a
        ``config.toml`` edit: an auth profile points ``CODEX_HOME`` at its own
        per-account directory, which a global file would miss.
        """
        from . import mcp_attach

        return (
            "-c",
            "mcp_servers.%s=%s"
            % (mcp_attach.SERVER_NAME, mcp_attach.codex_server_table(spec)),
        )

    def peer_mcp_args(self, spec) -> tuple:
        """ONE ``-c mcp_servers.mindflock={...}`` table for peer mode: the peer
        env only (no host env forwarded), every peer tool pre-approved — the
        sandbox is the boundary."""
        from . import mcp_attach

        return (
            "-c",
            "mcp_servers.%s=%s"
            % (
                mcp_attach.SERVER_NAME,
                mcp_attach.peer_codex_server_table(spec.share_id, spec.token),
            ),
        )

    # --- terminal classification ------------------------------------------ #
    def parse_dialog(self, screen_text: str) -> Optional[dict]:
        """The approval overlay (run a command / make edits / grant
        permissions), a request for input or the folder-trust screen: the
        question, an optional ``Reason:``, the ``$ command`` and ``› 1.``
        options (see :func:`backend.providers.dialogs.parse_codex`)."""
        from . import dialogs

        return dialogs.parse_codex(screen_text)

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
    def record_thread(
        self,
        session_name: str,
        workdir: str,
        since_ts=None,
        profile_id: str = "",
    ) -> None:
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
                thread_markers.record(session_name, sid, profile_id)
        except Exception:  # noqa: BLE001 — thread binding is enrichment only
            pass

    # --- transcript ------------------------------------------------------- #
    def last_assistant_text(
        self,
        session_name: str,
        workdir: str,
        contains=None,
        transcript_path=None,
    ):
        """The newest assistant message of THIS window's codex conversation
        (the rollout named after the thread id ``record_thread`` bound —
        never the directory's newest rollout, which may be a sibling's),
        optionally containing ``contains``. Cached on the file's (path,
        mtime, size), so a poll costs a stat. None without a bound thread or
        a readable rollout. Never raises."""
        return _codex_last_assistant_text(session_name, contains, transcript_path)

    # --- telemetry -------------------------------------------------------- #
    def session_tokens(
        self,
        workdir: str,
        since_ts: Optional[float],
        until_ts: Optional[float] = None,
        shared_cwd: bool = False,
        threads=None,
        other_threads=None,
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


# --------------------------------------------------------------------------- #
# The newest assistant message, from this window's rollout
# --------------------------------------------------------------------------- #
#: Bytes read off a rollout's tail when looking for its newest assistant
#: message — a turn's own records, not the whole conversation.
_TAIL_BYTES = 2 * 1024 * 1024
_ROLLOUT_PATHS: dict = {}  # thread id -> rollout path
_LAST_ASSISTANT_CACHE: dict = {}  # (path, mtime_ns, size, contains) -> text|None
_CACHE_MAX = 256


def _rollout_for(thread_id: str):
    """The rollout file Codex named after ``thread_id``
    (``rollout-<stamp>-<id>.jsonl`` under any config root's ``sessions``),
    or None. The path is remembered per id; a vanished file is looked up
    again."""
    import os

    hit = _ROLLOUT_PATHS.get(thread_id)
    if hit is not None and os.path.isfile(hit):
        return hit
    from . import codex_usage_api

    for root in codex_usage_api._sessions_dirs():
        try:
            for p in root.glob("*/*/*/rollout-*-%s.jsonl" % thread_id):
                if p.is_file():
                    if len(_ROLLOUT_PATHS) >= _CACHE_MAX:
                        _ROLLOUT_PATHS.clear()
                    _ROLLOUT_PATHS[thread_id] = str(p)
                    return str(p)
        except Exception:  # noqa: BLE001 — an unreadable root holds nothing
            continue
    return None


def _message_text(doc) -> Optional[str]:
    """The text of one rollout record when it is an assistant message
    (``response_item`` / ``message`` / ``role: assistant``), else None."""
    if not isinstance(doc, dict) or doc.get("type") != "response_item":
        return None
    payload = doc.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    if payload.get("role") != "assistant":
        return None
    parts = [
        str(c.get("text") or "")
        for c in payload.get("content") or ()
        if isinstance(c, dict) and c.get("type") in ("output_text", "text")
    ]
    text = "\n".join(p for p in parts if p)
    return text if text.strip() else None


def _codex_last_assistant_text(
    session_name: str, contains=None, transcript_path=None
) -> Optional[str]:
    import json
    import os

    try:
        path = None
        if transcript_path and os.path.isfile(transcript_path):
            path = transcript_path
        else:
            from . import thread_markers

            tid = thread_markers.read(session_name) if session_name else ""
            if tid:
                path = _rollout_for(tid)
        if not path:
            return None
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size, contains or "")
        if key in _LAST_ASSISTANT_CACHE:
            return _LAST_ASSISTANT_CACHE[key]
        with open(path, "rb") as f:
            if st.st_size > _TAIL_BYTES:
                f.seek(st.st_size - _TAIL_BYTES)
                f.readline()  # a partial first line
            lines = f.read().decode("utf-8", "replace").splitlines()
        result = None
        for line in reversed(lines):
            if '"assistant"' not in line:
                continue
            if contains and contains not in line:
                continue
            try:
                text = _message_text(json.loads(line))
            except ValueError:
                continue
            if text is None or (contains and contains not in text):
                continue
            result = text
            break
        if len(_LAST_ASSISTANT_CACHE) >= _CACHE_MAX:
            _LAST_ASSISTANT_CACHE.clear()
        _LAST_ASSISTANT_CACHE[key] = result
        return result
    except Exception:  # noqa: BLE001 — no transcript reads as no reply
        return None
