"""Per-session budget lock + raise (temporary N hours / forever), the auth-mode
gate override, and the first-run onboarded flag."""

import time

import pytest
from starlette.testclient import TestClient

from backend import session
from backend.web import server
from backend.web.core import budget as budget_core


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate the override store + settings from real state. The in-memory
    # override map is private state of core.budget (rebound there on lazy
    # load), so its reset targets that module, not the server facade.
    monkeypatch.setattr(
        server, "_budget_overrides_path", lambda: str(tmp_path / "bo.json")
    )
    monkeypatch.setattr(budget_core, "_BUDGET_OV", None)
    monkeypatch.setattr(server, "_session_budget_usd", lambda: 5.0)
    return TestClient(server.app)


def _fake_session(title, cost):
    from backend.session.tmux import tmux as tmux_mod

    i = session.NewInstance(
        session.InstanceOptions(title=title, path=".", program="claude", in_place=True)
    )
    i._started = True
    i._tmux_session = tmux_mod.NewTmuxSession(title, "claude")
    server.ENGINE.instances[title] = i
    return i


def test_budget_lock_blocks_send_until_raised(client, monkeypatch):
    title = "bl-test"
    _fake_session(title, cost=7.0)
    monkeypatch.setattr(server, "_session_tokens", lambda inst: {"cost": 7.0, "out": 0})
    try:
        # Over the $5 budget -> locked.
        assert server._budget_locked(title) is True
        b = client.get(f"/api/instances/{title}/budget").json()
        assert b["locked"] is True and b["limit"] == 5.0

        # Sending is refused while locked.
        r = client.post(f"/api/instances/{title}/send", json={"text": "hi"})
        assert r.status_code == 409 and r.json().get("budget_locked") is True

        # Raise for 2 hours -> unlocked, with an expiry.
        r = client.post(
            f"/api/instances/{title}/budget/raise", json={"limit": 20, "hours": 2}
        )
        assert r.status_code == 200 and r.json()["locked"] is False
        assert r.json()["expires"] is not None
        assert server._budget_locked(title) is False
    finally:
        server.ENGINE.instances.pop(title, None)
        server._forget_budget(title)


def test_temporary_raise_relocks_after_expiry(client, monkeypatch):
    title = "bl-exp"
    _fake_session(title, cost=7.0)
    monkeypatch.setattr(server, "_session_tokens", lambda inst: {"cost": 7.0, "out": 0})
    try:
        client.post(
            f"/api/instances/{title}/budget/raise", json={"limit": 20, "hours": 2}
        )
        assert server._budget_locked(title) is False
        # Force the raise to have expired.
        budget_core._BUDGET_OV[title]["expires"] = time.time() - 1
        assert server._budget_locked(title) is True  # falls back to the $5 base
    finally:
        server.ENGINE.instances.pop(title, None)
        server._forget_budget(title)


def test_forever_raise_has_no_expiry(client, monkeypatch):
    title = "bl-forever"
    _fake_session(title, cost=7.0)
    monkeypatch.setattr(server, "_session_tokens", lambda inst: {"cost": 7.0, "out": 0})
    try:
        r = client.post(
            f"/api/instances/{title}/budget/raise", json={"limit": 50, "hours": 0}
        )
        assert r.json()["expires"] is None and r.json()["locked"] is False
    finally:
        server.ENGINE.instances.pop(title, None)
        server._forget_budget(title)


def test_raise_requires_positive_limit(client):
    title = "bl-bad"
    _fake_session(title, cost=1.0)
    try:
        r = client.post(f"/api/instances/{title}/budget/raise", json={"limit": 0})
        assert r.status_code == 400
    finally:
        server.ENGINE.instances.pop(title, None)
        server._forget_budget(title)


def test_config_exposes_auth_and_onboarded(client):
    cfg = client.get("/api/config").json()
    assert "auth_mode" in cfg and "auth_enabled" in cfg and "onboarded" in cfg


# --------------------------------------------------------------------------- #
# Direct unit tests on core.budget (no HTTP client).
#
# These bypass the Starlette layer to lock the budget arithmetic itself and to
# confirm the lazy-load rebind: the private override map lives on core.budget
# (``_BUDGET_OV``), and reset targets that module — not the server facade.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def budget_isolated(tmp_path, monkeypatch):
    """Isolate the persisted override store + reset the in-memory maps.

    Points the override file at tmp, drops any cached override map (forcing a
    fresh lazy load into ``core.budget._BUDGET_OV``), and clears the
    per-session ``exceeded``-notice arming so ordering between tests can't leak.
    """
    monkeypatch.setattr(
        server, "_budget_overrides_path", lambda: str(tmp_path / "bo.json")
    )
    monkeypatch.setattr(budget_core, "_BUDGET_OV", None)
    budget_core._BUDGET_FIRED.clear()
    monkeypatch.setattr(server, "_session_budget_usd", lambda: 5.0)
    yield
    budget_core._BUDGET_FIRED.clear()


def test_effective_budget_falls_back_to_base(budget_isolated):
    # No override → the global per-session default.
    assert budget_core._effective_budget("none") == 5.0


def test_set_override_takes_effect_and_expires(budget_isolated):
    title = "core-ov"
    # A raise with no expiry ("forever") overrides the base.
    budget_core._set_budget_override(title, limit=20.0, expires=None)
    assert budget_core._effective_budget(title) == 20.0
    # The override was persisted and rebound into core.budget, not server.
    assert budget_core._BUDGET_OV[title]["limit"] == 20.0

    # A temporary raise that has already expired falls back to the base.
    budget_core._set_budget_override(title, limit=50.0, expires=time.time() - 1)
    assert budget_core._effective_budget(title) == 5.0

    # An in-flight temporary raise is honoured until its expiry.
    budget_core._set_budget_override(title, limit=30.0, expires=time.time() + 3600)
    assert budget_core._effective_budget(title) == 30.0

    # Forgetting the override reverts to the base.
    budget_core._forget_budget(title)
    assert budget_core._effective_budget(title) == 5.0


def test_check_session_budget_fires_once_per_level(budget_isolated, monkeypatch):
    title = "core-fire"
    fired = []
    monkeypatch.setattr(
        budget_core._events.BUS,
        "emit",
        lambda ev, **kw: fired.append((ev, kw)),
    )
    # Under budget → nothing.
    budget_core._check_session_budget(title, cost=1.0)
    assert fired == []
    # First crossing at the $5 level → exactly one event.
    budget_core._check_session_budget(title, cost=6.0)
    assert len(fired) == 1
    assert fired[0][0] == "session.budget_exceeded"
    # Still over at the SAME level → deduped, no second event.
    budget_core._check_session_budget(title, cost=9.0)
    assert len(fired) == 1
    # Raising the override re-arms the notice for the new level via _BUDGET_FIRED.
    budget_core._set_budget_override(title, limit=8.0, expires=None)
    assert title not in budget_core._BUDGET_FIRED
    monkeypatch.setattr(server, "_session_budget_usd", lambda: 8.0)
    budget_core._check_session_budget(title, cost=9.0)
    assert len(fired) == 2
