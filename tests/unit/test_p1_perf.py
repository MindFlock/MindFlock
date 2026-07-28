"""P1 performance fixes.

* GET /api/instances is READ-ONLY: the side effects that used to piggyback on
  it (budget-crossing notifications, auto check-run kicks, *_changed events,
  the addon sessions snapshot) now live in ``server._instances_tick`` driven
  by an always-on background loop.
* The expensive per-session probes (stage / activity / last-turn) are memoized
  ~2.5s per (probe, session) so N concurrent pollers cost one probe run.
* ``GitWorktree.Diff()`` / ``DiffNumstat()`` are memoized ~2.5s per worktree
  path so idle sessions stop re-running ``git add -N`` + a full diff per tick.
"""

from __future__ import annotations


import pytest
from starlette.testclient import TestClient

from backend import session
from backend.session.git import diff as diff_mod
from backend.web import server
from backend.web.core import events as events_mod


# --------------------------------------------------------------------------- #
# Probe memo (server._probe_cached)
# --------------------------------------------------------------------------- #
class _Inst:
    """Minimal weakref-able stand-in for a session instance."""

    def __init__(self, title):
        self.Title = title


@pytest.fixture()
def probe_clock(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["t"])
    server._PROBE_CACHE.clear()
    yield clock
    server._PROBE_CACHE.clear()


def test_probe_memo_serves_cached_value_within_ttl(probe_clock):
    inst = _Inst("p1-memo")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"stage": "agent", "n": calls["n"]}

    first = server._probe_cached("stage", inst, compute)
    second = server._probe_cached("stage", inst, compute)
    assert first == second == {"stage": "agent", "n": 1}
    assert calls["n"] == 1  # concurrent pollers share one probe run


def test_probe_memo_recomputes_after_ttl(probe_clock):
    inst = _Inst("p1-memo")
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    assert server._probe_cached("act", inst, compute) == 1
    probe_clock["t"] += server._PROBE_TTL + 0.01
    assert server._probe_cached("act", inst, compute) == 2


def test_probe_memo_keys_are_per_probe_and_per_title(probe_clock):
    a, b = _Inst("p1-a"), _Inst("p1-b")
    assert server._probe_cached("stage", a, lambda: "sa") == "sa"
    # A different probe for the same session computes independently.
    assert server._probe_cached("activity", a, lambda: "aa") == "aa"
    # A different session computes independently.
    assert server._probe_cached("stage", b, lambda: "sb") == "sb"
    # And the original entry is still served.
    assert server._probe_cached("stage", a, lambda: "WRONG") == "sa"


def test_probe_memo_misses_for_recreated_same_title_instance(probe_clock):
    # A session deleted and recreated under the same title is a NEW object:
    # the identity guard must not serve it the old instance's answer.
    old = _Inst("p1-recreate")
    assert server._probe_cached("stage", old, lambda: "old") == "old"
    new = _Inst("p1-recreate")
    assert server._probe_cached("stage", new, lambda: "new") == "new"


def test_forget_probes_clears_every_probe_for_the_title(probe_clock):
    inst = _Inst("p1-forget")
    server._probe_cached("stage", inst, lambda: "s")
    server._probe_cached("activity", inst, lambda: "a")
    server._forget_probes("p1-forget")
    assert server._probe_cached("stage", inst, lambda: "s2") == "s2"
    assert server._probe_cached("activity", inst, lambda: "a2") == "a2"


# --------------------------------------------------------------------------- #
# GET /api/instances is side-effect free; the tick fires the side effects.
# --------------------------------------------------------------------------- #
_ZERO_TOKENS = {
    "in": 0,
    "out": 0,
    "cache_read": 0,
    "cache_write": 0,
    "ctx": 0,
    "ctx_window": 0,
    "model": "",
    "cost": 7.5,
}


@pytest.fixture()
def polled_session(monkeypatch):
    """One fake started session + recorders on every removed poll side effect,
    with the expensive probes stubbed so nothing shells out."""
    from backend.session.tmux import tmux as tmux_mod

    title = "p1-sef"
    inst = session.NewInstance(
        session.InstanceOptions(title=title, path=".", program="claude", in_place=True)
    )
    inst._started = True
    inst._tmux_session = tmux_mod.NewTmuxSession(title, "claude")
    server.ENGINE.instances[title] = inst
    server._PROBE_CACHE.clear()

    calls = {"budget": [], "emit": [], "start_check": []}
    monkeypatch.setattr(
        server, "_check_session_budget", lambda t, c: calls["budget"].append((t, c))
    )
    monkeypatch.setattr(
        server, "_emit_state_changes", lambda *a: calls["emit"].append(a)
    )
    monkeypatch.setattr(
        server._wt_setup, "start_check", lambda *a, **k: calls["start_check"].append(a)
    )
    # Stub the probes: this test is about WHO runs the side effects, not the
    # probes themselves (each has its own tests).
    monkeypatch.setattr(
        server, "_session_stage", lambda i: {"stage": "agent", "pr_url": None}
    )
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(server, "_session_last_turn", lambda i: None)
    monkeypatch.setattr(server, "_session_tokens", lambda i: dict(_ZERO_TOKENS))
    monkeypatch.setattr(server, "_session_diff_stat", lambda i: None)
    monkeypatch.setattr(server, "_has_origin", lambda folder: False)
    try:
        yield title, calls
    finally:
        server.ENGINE.instances.pop(title, None)
        server._forget_probes(title)
        server._EVENT_SNAPSHOT.pop(title, None)


def test_get_instances_is_side_effect_free(polled_session):
    title, calls = polled_session
    client = TestClient(server.app)
    r = client.get("/api/instances")
    assert r.status_code == 200
    row = next(d for d in r.json() if d["title"] == title)
    # Response shape unchanged: the fields the frontend reads are all present.
    for k in (
        "title",
        "branch",
        "status",
        "stage",
        "queue",
        "tokens",
        "tokens_in",
        "tokens_cache_read",
        "tokens_cache_write",
        "tokens_ctx",
        "tokens_ctx_window",
        "tokens_cost",
        "tokens_model",
        "budget",
        "activity",
        "last_turn",
        "setup",
        "check",
        "ports",
        "diff_stat",
    ):
        assert k in row, k
    assert row["activity"] == "idle" and row["stage"] == "agent"
    # ...but none of the removed side effects fired on GET.
    assert calls["budget"] == []
    assert calls["emit"] == []
    assert calls["start_check"] == []


def test_instances_tick_fires_the_side_effects(polled_session):
    title, calls = polled_session
    server._instances_tick()
    assert (title, 7.5) in calls["budget"]
    assert any(a[0] == title for a in calls["emit"])
    emitted = next(a for a in calls["emit"] if a[0] == title)
    assert emitted[2:] == ("idle", "agent")  # (title, status, activity, stage)
    # The addon snapshot is refreshed by the tick (not by GET).
    assert any(d["title"] == title for d in events_mod.sessions_snapshot())


def test_get_instances_never_probes_on_stale_snapshot(polled_session, monkeypatch):
    # Cold boot / stale-tick path: the GET must answer from cheap fields only
    # (a full probe build blocks seconds per big worktree), leaving the real
    # values to the always-on tick.
    title, _ = polled_session
    probed = []
    monkeypatch.setattr(
        server,
        "_session_stage",
        lambda i: probed.append("stage") or {"stage": "agent", "pr_url": None},
    )
    monkeypatch.setattr(
        server,
        "_session_tokens",
        lambda i: probed.append("tokens") or dict(_ZERO_TOKENS),
    )
    monkeypatch.setattr(server, "_SNAPSHOT_AT", 0.0)  # never published
    monkeypatch.setattr(events_mod, "_SESSIONS_SNAPSHOT", [], raising=False)
    r = TestClient(server.app).get("/api/instances")
    row = next(d for d in r.json() if d["title"] == title)
    assert probed == []  # no expensive probe ran on the request path
    # Placeholders the UI already renders (a paused session looks the same).
    assert row["diff_stat"] is None and row["activity"] == "idle"
    assert row["stage"] == "agent" and row["tokens_cost"] == 0.0


def test_get_instances_carries_probe_fields_from_last_snapshot(
    polled_session, monkeypatch
):
    # Stale-but-existing snapshot: probe-derived fields (diff stat, stage,
    # tokens…) are served from the last published entry instead of flashing
    # back to placeholders; cheap fields (queue) are still computed fresh.
    title, _ = polled_session
    server._instances_tick()  # publish a full snapshot for this title
    prev = next(d for d in events_mod.sessions_snapshot() if d["title"] == title)
    assert prev["tokens_cost"] == 7.5  # the stubbed probe value made it in
    monkeypatch.setattr(server, "_SNAPSHOT_AT", 0.0)  # force the fallback path
    r = TestClient(server.app).get("/api/instances")
    row = next(d for d in r.json() if d["title"] == title)
    assert row["tokens_cost"] == 7.5  # carried over, not recomputed/zeroed
    assert row["activity"] == "idle" and row["stage"] == "agent"
    assert row["queue"]["pending"] == 0  # fresh cheap field, present


def test_build_snapshot_is_parallel_and_order_preserving(polled_session):
    # The parallel build must return one entry per session, in ENGINE order.
    from backend.session.tmux import tmux as tmux_mod

    extra = "p1-sef-2"
    inst = session.NewInstance(
        session.InstanceOptions(title=extra, path=".", program="claude", in_place=True)
    )
    inst._started = True
    inst._tmux_session = tmux_mod.NewTmuxSession(extra, "claude")
    server.ENGINE.instances[extra] = inst
    try:
        out = server._build_instances_snapshot()
        want = [i.Title for i in server.ENGINE.instances.values()]
        assert [d["title"] for d in out] == want
    finally:
        server.ENGINE.instances.pop(extra, None)
        server._forget_probes(extra)
        server._EVENT_SNAPSHOT.pop(extra, None)


def test_instances_tick_loop_is_registered_in_lifespan():
    # The tick must run with ZERO clients connected -> it hangs off the
    # always-on lifespan, not the /api/events client-gated ticker.
    import inspect

    src = inspect.getsource(server.lifespan)
    assert "_instances_tick_loop" in src


# --------------------------------------------------------------------------- #
# Diff / DiffNumstat TTL cache (session/git/diff.py)
# --------------------------------------------------------------------------- #
class _FakeWT(diff_mod.GitWorktreeDiffMixin):
    def __init__(self, path):
        self.worktreePath = path
        self.calls = []

    def run_git_command(self, path, *args):
        self.calls.append(args)
        if "--numstat" in args:
            return "1\t2\ta.py\n"
        if "diff" in args:
            return "+x\n-y\n-z\n"
        return ""  # the `add -N .` call


@pytest.fixture()
def diff_clock(monkeypatch):
    clock = {"t": 500.0}
    monkeypatch.setattr(diff_mod.time, "monotonic", lambda: clock["t"])
    diff_mod._DIFF_CACHE.clear()
    yield clock
    diff_mod._DIFF_CACHE.clear()


def test_diff_is_cached_within_ttl(diff_clock, tmp_path):
    wt = _FakeWT(str(tmp_path))
    s1 = wt.Diff()
    assert (s1.Added, s1.Removed) == (1, 2)
    assert len(wt.calls) == 2  # add -N + diff
    s2 = wt.Diff()
    assert len(wt.calls) == 2  # served from cache: no new git commands
    assert (s2.Added, s2.Removed, s2.Content) == (s1.Added, s1.Removed, s1.Content)
    # Mutating a returned object must not poison the cache.
    s2.Added = 99
    assert wt.Diff().Added == 1


def test_diff_recomputes_after_ttl(diff_clock, tmp_path):
    wt = _FakeWT(str(tmp_path))
    wt.Diff()
    diff_clock["t"] += diff_mod._DIFF_TTL + 0.01
    wt.Diff()
    assert len(wt.calls) == 4  # a just-edited worktree is re-diffed within one TTL


def test_numstat_and_full_diff_cache_independently(diff_clock, tmp_path):
    wt = _FakeWT(str(tmp_path))
    wt.Diff()
    assert len(wt.calls) == 2
    s = wt.DiffNumstat()  # different probe -> computes despite the Diff entry
    assert (s.Added, s.Removed) == (1, 2)
    assert len(wt.calls) == 4
    wt.DiffNumstat()
    wt.Diff()
    assert len(wt.calls) == 4  # both now served from their own entries


def test_diff_cache_is_per_worktree_path(diff_clock, tmp_path):
    a = _FakeWT(str(tmp_path / "a"))
    b = _FakeWT(str(tmp_path / "b"))
    a.Diff()
    b.Diff()
    assert len(a.calls) == 2 and len(b.calls) == 2  # no cross-worktree sharing


def test_diff_without_worktree_path_bypasses_cache(diff_clock):
    wt = _FakeWT("")
    wt.Diff()
    wt.Diff()
    assert len(wt.calls) == 4  # never cached under the shared "" key
