"""Session telemetry read from provider transcripts: tokens, cost, history.

Two consumers of the same on-disk source (the coding CLI's per-cwd transcript
files):

* :func:`_session_tokens` — the four token figures + estimated USD cost shown
  per session (cached ~20s, windowed by the session's creation time so copies
  sharing a worktree each get their own conversation's numbers);
* :func:`_agent_transcript_text` — one window's conversation (selected by its
  thread marker, not by mtime) rendered as plain text for the "Copy all" /
  history endpoint (the agent TUI runs on tmux's alternate screen, so tmux
  itself accumulates no scrollback).

Split out of ``backend.web.server`` (which re-imports these names — the
routes, tick loops, and tests reference them through the server namespace).
"""

from __future__ import annotations

import json
import time
from typing import Dict, Optional

from backend import providers


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


_TOKENS_CACHE: Dict[tuple, tuple] = {}  # (title, worktree) -> (expires_epoch, tokens)


def forget_tokens(title: str) -> None:
    """Drop the memoized token stats for one session (kill/delete paths) so a
    long-running server with session churn doesn't hold entries for dead
    titles."""
    for k in [k for k in _TOKENS_CACHE if k[0] == title]:
        _TOKENS_CACHE.pop(k, None)


def _created_epoch(inst) -> Optional[float]:
    """A session's creation time as epoch seconds, or None."""
    ca = getattr(inst, "CreatedAt", None)
    if ca is None:
        return None
    try:
        return ca.timestamp()
    except Exception:  # noqa: BLE001
        return None


def _session_tokens(inst) -> dict:
    """Best-effort tokens used by claude in this session, SINCE it started.

    Returns ``{"in", "out", "cache_read", "cache_write"}`` — the same four
    figures Claude Code's own ``/usage`` reports, read from the same source: the
    per-message ``usage`` blocks in the transcript. ``in`` is REAL input only
    (``input_tokens``); cache read/creation are reported separately rather than
    folded into ``in`` (folding them in inflated the figure ~400x, since a
    cache-read alone can dwarf real input).

    Claude Code stores per-session transcripts under
    ``<config-dir>/projects/<cwd-with-non-alnum->-dashes>/*.jsonl``; each
    assistant message carries a ``usage`` block. We sum across all transcripts
    for the worktree (covers every claude run in it). Cached ~20s; never raises.

    The config dir is normally ``~/.claude``, but wrappers or alternate
    installs may launch claude with ``CLAUDE_CONFIG_DIR`` pointing elsewhere.
    So we scan every ``~/.claude*`` config root (plus ``$CLAUDE_CONFIG_DIR`` if
    set) and sum — otherwise those sessions read as 0.
    """
    srv = _server()
    zero = {
        "in": 0,
        "out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "ctx": 0,
        "ctx_window": 0,
        "model": "",
        "cost": 0.0,
    }
    try:
        if not inst.Started():
            return dict(zero)
        wt = inst.GetWorktreePath()
        if not wt:
            return dict(zero)
        now = time.time()
        # Cache PER SESSION, not per worktree: copies share one worktree, so a
        # worktree-keyed cache would hand every copy the first session's figures.
        title = getattr(inst, "Title", "") or ""
        cache_key = (title, wt)
        cached = _TOKENS_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        # Only count output generated SINCE this session started — the transcript
        # dir (keyed by cwd) can hold runs from earlier sessions in the same
        # folder (esp. in-place repos). Compare each entry's `timestamp` to the
        # instance's creation time.
        start = _created_epoch(inst)
        # When other live sessions share this worktree (a window and its copies),
        # bound this session's telemetry above by the NEXT sibling's start time.
        # Each copy launches its own Claude conversation, so windowing by
        # creation time attributes each conversation to exactly one session
        # instead of summing every run in the shared cwd (the copy-cost bug).
        until = None
        shared_cwd = False
        for other in list(srv.ENGINE.instances.values()):
            if other is inst:
                continue
            try:
                if other.GetWorktreePath() != wt:
                    continue
            except Exception:  # noqa: BLE001
                continue
            shared_cwd = True  # a copy (or its original) shares this worktree
            o_start = _created_epoch(other)
            if start is not None and o_start is not None and o_start > start:
                until = o_start if until is None else min(until, o_start)
        # The session's coding provider knows how to read its own telemetry
        # (Claude scans its transcript jsonl; other providers may have none).
        result = providers.resolve(getattr(inst, "Program", "") or "").session_tokens(
            wt, start, until, shared_cwd
        )
        # Enrich raw counts with an estimated $ cost and the context-window limit,
        # priced from the live AI Pricing Guru feed (best-effort; offline-safe).
        try:
            from backend.providers import pricing

            model = result.get("model") or ""
            result["cost"] = pricing.estimate_cost(result, model)
            # Prefer a window the provider reported first-hand (Codex records its
            # own model_context_window per turn); only fall back to the priced
            # lookup when the provider gave us none (Claude's path).
            result["ctx_window"] = result.get("ctx_window") or pricing.context_window(
                model
            )
        except Exception:  # noqa: BLE001 — pricing is optional; never block tokens
            result.setdefault("cost", 0.0)
            result.setdefault("ctx_window", 0)
        _TOKENS_CACHE[cache_key] = (now + 20, result)
        return result
    except Exception:  # noqa: BLE001
        return dict(zero)


def _agent_transcript_text(workdir: str, session_name: str = ""):
    """One Claude Code conversation in ``workdir``, rendered as plain text.

    The agent TUI sits on tmux's ALTERNATE screen, so the tmux pane accumulates
    no history (history_size stays 0) and capture-pane only sees the visible
    frame. The full conversation lives in Claude Code's transcript files:
    ``<config-root>/projects/<cwd-slug>/*.jsonl`` — the same layout the usage
    scanner in providers/claude.py walks.

    WHICH file is the window's own: several sessions can run in one directory
    and they all write into that same project dir, so ``session_name`` (the
    window's tmux session) selects its conversation via the recorded thread
    marker; only without one does this fall back to the newest by mtime.
    Returns None when no transcript exists (e.g. non-Claude provider); never
    raises.
    """
    if not workdir:
        return None
    try:
        from backend.providers.claude import _session_transcript

        newest = _session_transcript(workdir, session_name)
    except Exception:  # noqa: BLE001 — history is best-effort
        return None
    if newest is None:
        return None
    parts = []
    try:
        with open(newest[2], errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("isMeta"):
                    continue
                if obj.get("type") == "queue-operation":
                    # A prompt typed while the turn was still running. It is
                    # never re-filed as a "user" entry, so skipping it drops
                    # the message from the conversation for good.
                    if obj.get("operation") != "enqueue":
                        continue
                    queued = obj.get("content")
                    if isinstance(queued, str) and queued.strip():
                        parts.append("## User\n" + queued)
                    continue
                if obj.get("type") not in ("user", "assistant"):
                    continue
                content = (obj.get("message") or {}).get("content")
                texts = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            t = blk.get("text") or ""
                            if t.strip():
                                texts.append(t)
                if not texts:
                    continue  # tool_use / tool_result-only turns: skip the noise
                if obj["type"] == "user":
                    # Slash-command plumbing the CLI files as "user" messages
                    # (<command-name>/model</command-name>, its
                    # <local-command-stdout> echo, caveat banners). They are
                    # not conversation; rendering them as prompts is noise.
                    head = "\n".join(texts).lstrip()
                    if head.startswith("<"):
                        continue
                who = "User" if obj["type"] == "user" else "Claude"
                parts.append("## " + who + "\n" + "\n".join(texts))
    except OSError:
        return None
    return ("\n\n".join(parts) + "\n") if parts else None
