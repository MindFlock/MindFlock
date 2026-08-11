"""Unit tests for :mod:`backend.web.core.session_stats`.

Session telemetry read from provider transcripts. These cover the pure helpers
(``_created_epoch``, ``_agent_transcript_text`` against a fixture transcript)
and the ``_TOKENS_CACHE`` keying/invalidation so stale token/cost figures
aren't served after a session updates. The provider's own transcript reader is
stubbed so no real ``~/.claude`` scan or pricing feed is hit.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend import providers
from backend.providers import pricing
from backend.web import server
from backend.web.core import session_stats as st


# --------------------------------------------------------------------------- #
# _created_epoch
# --------------------------------------------------------------------------- #
def test_created_epoch_from_datetime():
    dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    inst = SimpleNamespace(CreatedAt=dt)
    assert st._created_epoch(inst) == dt.timestamp()


def test_created_epoch_none_when_missing():
    assert st._created_epoch(SimpleNamespace(CreatedAt=None)) is None
    assert st._created_epoch(SimpleNamespace()) is None


def test_created_epoch_none_on_bad_value():
    assert st._created_epoch(SimpleNamespace(CreatedAt="not-a-datetime")) is None


# --------------------------------------------------------------------------- #
# _agent_transcript_text
# --------------------------------------------------------------------------- #
def _write_transcript(home, workdir, lines):
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", workdir)
    proj = os.path.join(home, ".claude", "projects", encoded)
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, "conv.jsonl")
    with open(path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return path


def test_transcript_text_none_for_blank_workdir():
    assert st._agent_transcript_text("") is None


def test_transcript_text_none_when_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert st._agent_transcript_text(str(tmp_path / "wt")) is None


def test_transcript_text_renders_user_and_assistant(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workdir = str(tmp_path / "wt")
    _write_transcript(
        str(tmp_path),
        workdir,
        [
            {"type": "user", "message": {"content": "hello there"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi back"}]},
            },
            # tool-only turn: no text blocks → skipped as noise.
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "x"}]},
            },
            {"isMeta": True, "type": "user", "message": {"content": "meta noise"}},
        ],
    )
    text = st._agent_transcript_text(workdir)
    assert text is not None
    assert "## User\nhello there" in text
    assert "## Claude\nhi back" in text
    assert "meta noise" not in text  # isMeta lines are dropped


def test_transcript_text_keeps_a_prompt_typed_mid_turn(tmp_path, monkeypatch):
    """Queued prompts are filed as queue-operation and never re-filed as
    "user", so dropping them loses the message from the history page for
    good — and those are exactly the follow-ups sent while the agent works."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workdir = str(tmp_path / "wt")
    _write_transcript(
        str(tmp_path),
        workdir,
        [
            {"type": "user", "message": {"content": "start the work"}},
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "and also fix the menu",
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "on it"}]},
            },
            # The same text coming back off the queue must not print twice.
            {
                "type": "queue-operation",
                "operation": "remove",
                "content": "and also fix the menu",
            },
        ],
    )
    text = st._agent_transcript_text(workdir)
    assert text is not None
    assert "## User\nstart the work" in text
    assert "## User\nand also fix the menu" in text
    assert text.count("and also fix the menu") == 1
    assert "## Claude\non it" in text


# --------------------------------------------------------------------------- #
# _session_tokens — cache keying + invalidation
# --------------------------------------------------------------------------- #
class _FakeProvider:
    def __init__(self, ret):
        self.name = "claude"
        self._ret = ret
        self.calls = 0

    def session_tokens(self, wt, start, until, shared_cwd):
        self.calls += 1
        return dict(self._ret)


@pytest.fixture()
def clean_tokens_cache(monkeypatch):
    st._TOKENS_CACHE.clear()
    # Keep pricing offline-safe and deterministic.
    monkeypatch.setattr(pricing, "estimate_cost", lambda result, model: 1.25)
    monkeypatch.setattr(pricing, "context_window", lambda model: 200000)
    yield
    st._TOKENS_CACHE.clear()


def _tok_inst(title, wt):
    inst = SimpleNamespace(
        Program="claude",
        Title=title,
        CreatedAt=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    inst.Started = lambda: True
    inst.GetWorktreePath = lambda: wt
    return inst


def test_session_tokens_caches_by_title_and_worktree(
    clean_tokens_cache, monkeypatch, tmp_path
):
    wt = str(tmp_path / "wt")
    inst = _tok_inst("win", wt)
    monkeypatch.setattr(server.ENGINE, "instances", {"win": inst}, raising=False)

    prov = _FakeProvider(
        {"in": 10, "out": 5, "cache_read": 0, "cache_write": 0, "model": "m"}
    )
    monkeypatch.setattr(providers, "resolve", lambda program: prov)

    first = st._session_tokens(inst)
    assert first["out"] == 5
    assert first["cost"] == 1.25 and first["ctx_window"] == 200000
    assert prov.calls == 1
    assert (inst.Title, wt) in st._TOKENS_CACHE

    # Second call within the ~20s TTL is served from cache: the provider is not
    # re-read even though its underlying numbers changed.
    prov._ret = {"in": 999, "out": 999, "cache_read": 0, "cache_write": 0, "model": "m"}
    second = st._session_tokens(inst)
    assert second["out"] == 5  # stale-but-cached, provider not re-hit
    assert prov.calls == 1

    # Invalidating the cache serves fresh numbers.
    st._TOKENS_CACHE.clear()
    third = st._session_tokens(inst)
    assert third["out"] == 999
    assert prov.calls == 2


def test_session_tokens_zero_when_not_started(clean_tokens_cache, monkeypatch):
    inst = _tok_inst("win", "/tmp/wt")
    inst.Started = lambda: False
    monkeypatch.setattr(server.ENGINE, "instances", {}, raising=False)
    monkeypatch.setattr(providers, "resolve", lambda program: _FakeProvider({}))
    tok = st._session_tokens(inst)
    assert tok["in"] == 0 and tok["out"] == 0 and tok["cost"] == 0.0
