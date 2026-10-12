"""Per-window thread HISTORY (``<session>.threads``) and how per-session cost
uses it: the current marker is overwritten on every new conversation, so only
the history can say which conversation files in a shared cwd are a window's."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend import providers
from backend.providers import pricing
from backend.providers import thread_markers as tm
from backend.session import tmux
from backend.web import server
from backend.web.core import session_stats as st


@pytest.fixture
def markers(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))
    return tmp_path / "threads"


def test_record_appends_each_distinct_thread_once(markers):
    tm.record("win", "conv-1")
    tm.record("win", "conv-2")
    tm.record("win", "conv-1")
    assert tm.read("win") == "conv-1"
    assert (markers / "win.threads").read_text() == "conv-1\nconv-2\n"
    assert tm.history("win") == {"conv-1", "conv-2"}


def test_history_is_capped(markers, monkeypatch):
    monkeypatch.setattr(tm, "HISTORY_MAX", 3)
    for i in range(5):
        tm.record("win", "conv-%d" % i)
    assert (markers / "win.threads").read_text().split() == [
        "conv-2",
        "conv-3",
        "conv-4",
    ]


def test_history_includes_a_current_marker_written_before_history_existed(markers):
    markers.mkdir()
    (markers / "old.thread").write_text("conv-legacy")
    assert tm.history("old") == {"conv-legacy"}
    assert tm.history("nobody") == set()


def test_fresh_launch_clear_keeps_history_forget_drops_it(markers):
    tm.record("win", "conv-1")
    tm.clear("win")
    assert tm.read("win") == ""
    assert tm.history("win") == {"conv-1"}  # earlier conversations stay its own
    tm.clear("win", forget_accounts=True)
    assert tm.history("win") == set()


def test_claimed_ignores_history_files(markers):
    tm.record("a", "conv-a1")
    tm.record("a", "conv-a2")
    assert tm.claimed(exclude_session="b") == {"conv-a2"}


def test_switch_profile_records_the_restored_thread(markers):
    tm.record("win", "conv-work", "work")
    tm.record("win", "conv-home")
    (markers / "win.threads").unlink()
    assert tm.switch_profile("win", "", "work") == "conv-work"
    assert tm.history("win") == {"conv-work"}


# --------------------------------------------------------------------------- #
# session_stats hands the histories to the provider and keeps its cost
# --------------------------------------------------------------------------- #
class _Prov:
    name = "claude"

    def __init__(self, ret):
        self.ret = ret
        self.kw = None

    def session_tokens(self, wt, start, until, shared_cwd, **kw):
        self.kw = dict(kw, shared_cwd=shared_cwd)
        return dict(self.ret)


def _inst(title, wt, hour):
    inst = SimpleNamespace(
        Program="claude",
        Title=title,
        CreatedAt=datetime(2025, 6, 1, hour, tzinfo=timezone.utc),
    )
    inst.Started = lambda: True
    inst.GetWorktreePath = lambda: wt
    return inst


@pytest.fixture
def tokens(monkeypatch, markers):
    st._TOKENS_CACHE.clear()
    monkeypatch.setattr(pricing, "estimate_cost", lambda result, model: 1.25)
    monkeypatch.setattr(pricing, "context_window", lambda model: 1000)
    yield
    st._TOKENS_CACHE.clear()


def test_session_tokens_passes_own_and_sibling_threads(tokens, monkeypatch):
    a, b = _inst("win", "/wt", 1), _inst("win-copy", "/wt", 2)
    monkeypatch.setattr(server.ENGINE, "instances", {"a": a, "b": b}, raising=False)
    tm.record(tmux.to_mindflock_tmux_name("win"), "conv-a1")
    tm.record(tmux.to_mindflock_tmux_name("win"), "conv-a2")
    tm.record(tmux.to_mindflock_tmux_name("win-copy"), "conv-b1")
    prov = _Prov({"in": 1, "out": 1, "cache_read": 0, "cache_write": 0, "model": "m"})
    monkeypatch.setattr(providers, "resolve", lambda program: prov)
    st._session_tokens(a)
    assert prov.kw == {
        "threads": {"conv-a1", "conv-a2"},
        "other_threads": {"conv-b1"},
        "shared_cwd": True,
    }


def test_provider_cost_wins_over_the_single_model_estimate(tokens, monkeypatch):
    a = _inst("win", "/wt", 1)
    monkeypatch.setattr(server.ENGINE, "instances", {"a": a}, raising=False)
    prov = _Prov({"in": 1, "out": 1, "cache_read": 0, "cache_write": 0, "cost": 7.5})
    monkeypatch.setattr(providers, "resolve", lambda program: prov)
    assert st._session_tokens(a)["cost"] == 7.5
    st._TOKENS_CACHE.clear()
    prov.ret = {"in": 1, "out": 1, "cache_read": 0, "cache_write": 0}
    assert st._session_tokens(a)["cost"] == 1.25  # other providers: estimated
