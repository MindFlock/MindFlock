"""Addon framework: the registry, the manifest, and the migrated addons.

Locks the productization contract — features self-register via an Addon + the
/api/addons manifest, with their routes mounted before the static catch-all.
"""

from __future__ import annotations

import pytest
from starlette.routing import Mount
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.addons import AppContext, ManagedProcess
from backend.web.addons.ticket_ingestion import TicketIngestionAddon
from backend.web.addons.assistant import AssistantAddon
from backend.web.core import events as events_mod
from backend.web.core.events import EventBus

client = TestClient(server.app)


def test_manifest_lists_migrated_addons():
    data = client.get("/api/addons").json()
    ids = [a["id"] for a in data["addons"]]
    assert ids == [
        "mindflock",
        "assistant",
        "settings",
        "doctor",
        "connections",
        "templates",
        "notify",
        "traffic",
    ]
    # Each UI addon contributes a frontend descriptor with a known slot. The
    # "connections" addon is API-only (its list renders inline in the Settings →
    # Connections screen), so it has no frontend descriptor.
    api_only = {"connections"}
    for a in data["addons"]:
        if a["id"] in api_only:
            assert a["frontend"] == [], a["id"]
            continue
        assert a["frontend"], a["id"]
        assert a["frontend"][0]["where"] in (
            "sidebar-bar",
            "grid-pane",
            "dialog",
            "pane-tab",
            "settings",
        )


def test_ticket_ingestion_is_managed_process():
    # The generic start/stop/logs UI can drive any ManagedProcess addon.
    assert isinstance(TicketIngestionAddon(), ManagedProcess)
    # Assistant is not a managed process (no start/stop child).
    assert not isinstance(AssistantAddon(), ManagedProcess)


def test_addon_routes_resolve_and_keep_stable_paths():
    # Paths are unchanged from the pre-refactor monolith.
    assert client.get("/api/mindflock/status").status_code == 200
    r = client.get("/api/assistant/todos")
    assert r.status_code == 200 and isinstance(r.json()["todos"], list)


def test_addon_routes_win_over_static_mount():
    # The catch-all StaticFiles mount must remain the last route, and addon
    # routes must win over it. That an addon endpoint returns its JSON (not the
    # static index.html) proves precedence.
    assert isinstance(server.app.routes[-1], Mount)
    r = client.get("/api/mindflock/status")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------------------- #
# Addon API v2: AppContext.subscribe / emit / sessions (roadmap B4)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fresh_bus(monkeypatch, tmp_path):
    """A private EventBus (and no user hooks) so these tests never see events
    other tests emitted. base.py references the module attribute at call time,
    so one setattr patches AppContext's view too."""
    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    bus = EventBus()
    monkeypatch.setattr(events_mod, "BUS", bus)
    return bus


def _ctx() -> AppContext:
    return AppContext(engine=None, register_task=lambda coro: None)


def test_ctx_subscribe_filters_by_event_name(fresh_bus):
    got = []
    unsubscribe = _ctx().subscribe("session.paused", got.append)
    fresh_bus.emit("session.paused", session="a")
    fresh_bus.emit("session.resumed", session="a")
    assert [e["event"] for e in got] == ["session.paused"]
    unsubscribe()
    fresh_bus.emit("session.paused", session="a")
    assert len(got) == 1


def test_ctx_subscribe_star_receives_everything(fresh_bus):
    got = []
    _ctx().subscribe("*", got.append)
    fresh_bus.emit("session.created", session="a")
    fresh_bus.emit("session.deleted", session="a")
    assert [e["event"] for e in got] == ["session.created", "session.deleted"]


def test_ctx_emit_auto_prefixes_addon_namespace(fresh_bus):
    ctx = _ctx()
    env = ctx.emit("notify.ping", session="s", data={"x": 1})
    assert env["event"] == "addon.notify.ping"
    assert env["session"] == "s" and env["data"] == {"x": 1}
    # An already-prefixed name is passed through unchanged.
    assert ctx.emit("addon.notify.pong")["event"] == "addon.notify.pong"
    # Addon emits reach bus subscribers like any other event.
    assert [e["seq"] for e in fresh_bus.backlog()] == [env["seq"], env["seq"] + 1]


def test_ctx_emit_rejects_reserved_and_empty_names(fresh_bus):
    ctx = _ctx()
    with pytest.raises(ValueError):
        ctx.emit("session.created", session="spoof")
    with pytest.raises(ValueError):
        ctx.emit("")
    assert fresh_bus.backlog() == []  # nothing leaked onto the bus


def test_ctx_sessions_reads_last_snapshot_and_is_read_only(monkeypatch):
    monkeypatch.setattr(events_mod, "_SESSIONS_SNAPSHOT", [])
    assert _ctx().sessions() == []  # empty before the first poll
    events_mod.set_sessions_snapshot([{"title": "t1", "status": "running"}])
    snap = _ctx().sessions()
    assert snap == [{"title": "t1", "status": "running"}]
    snap[0]["status"] = "mutated"  # copies: mutation must not leak back
    assert _ctx().sessions()[0]["status"] == "running"


# --------------------------------------------------------------------------- #
# Notify addon (roadmap B5): manifest + config route
# --------------------------------------------------------------------------- #
def test_notify_addon_manifest_exercises_generic_slot_path():
    data = client.get("/api/addons").json()
    notify = next(a for a in data["addons"] if a["id"] == "notify")
    fe = notify["frontend"][0]
    # No sidebar bar: the on/off toggle lives in the bell dropdown + Settings.
    # slots.js still imports the module (it keys on `module`, not `where`).
    assert fe["where"] == "settings"
    assert fe["module"] == "/addons/notify.js"  # loaded by core/slots.js
    assert fe["builtin_ui"] is False  # i.e. the generic path loads it


def test_notify_config_route_serves_rules():
    r = client.get("/api/notify/config")
    assert r.status_code == 200
    rules = r.json()["rules"]
    events = {rule["event"] for rule in rules}
    assert "session.activity_changed" in events  # clarify → needs input
    assert "session.stage_changed" in events  # pr → merged/closed
    for rule in rules:
        # Each rule now carries a stable id + human label + current enabled
        # state, on top of the matching/message fields. The internal
        # ``default_enabled`` flag is NOT exposed.
        assert set(rule) == {
            "id",
            "label",
            "event",
            "old",
            "new",
            "title",
            "body",
            "enabled",
        }
        assert isinstance(rule["enabled"], bool)


def test_notify_default_on_and_opt_in_rules():
    """Default-on rules (needs_input) start enabled; noisy opt-in rules
    (session_idle, precommit_running) start disabled."""
    rules = {r["id"]: r for r in client.get("/api/notify/config").json()["rules"]}
    assert rules["needs_input"]["enabled"] is True
    assert rules["session_idle"]["enabled"] is False
    assert rules["precommit_running"]["enabled"] is False
    # The idle rule fires on activity → idle; pre-commit on stage → precommit.
    assert rules["session_idle"]["event"] == "session.activity_changed"
    assert rules["session_idle"]["new"] == "idle"
    assert rules["precommit_running"]["new"] == "precommit"
    # Pre-commit failure is default-on and fires on stage → interrupt.
    assert rules["precommit_failed"]["enabled"] is True
    assert rules["precommit_failed"]["new"] == "interrupt"


def test_notify_opt_in_rule_toggles_on_and_off(tmp_path, monkeypatch):
    """Turning an opt-in rule on then off round-trips through the enabled_rules
    opt-in list (default-off rules aren't tracked via muted_rules)."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    assert (
        client.post(
            "/api/notify/rules/session_idle", json={"enabled": True}
        ).status_code
        == 200
    )
    rules = {r["id"]: r for r in client.get("/api/notify/config").json()["rules"]}
    assert rules["session_idle"]["enabled"] is True
    # And back off.
    client.post("/api/notify/rules/session_idle", json={"enabled": False})
    rules = {r["id"]: r for r in client.get("/api/notify/config").json()["rules"]}
    assert rules["session_idle"]["enabled"] is False


def test_notify_unknown_rule_is_404():
    r = client.post("/api/notify/rules/no_such_rule", json={"enabled": True})
    assert r.status_code == 404
    assert "unknown rule" in r.json()["error"]


def test_notify_default_on_rule_mutes_and_unmutes(tmp_path, monkeypatch):
    """A default-on rule persists its OFF state as an opt-out (muted_rules) and
    clears back to on — the mirror of the opt-in path above."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    from backend.config import settings as S

    S.invalidate()
    client.post("/api/notify/rules/needs_input", json={"enabled": False})
    rules = {r["id"]: r for r in client.get("/api/notify/config").json()["rules"]}
    assert rules["needs_input"]["enabled"] is False  # default-on rule now muted
    client.post("/api/notify/rules/needs_input", json={"enabled": True})
    rules = {r["id"]: r for r in client.get("/api/notify/config").json()["rules"]}
    assert rules["needs_input"]["enabled"] is True  # un-muted


def test_resolve_repo_root_prefers_config_toml_over_parent_count(tmp_path, monkeypatch):
    """Regression: an installed (uv tool) copy lives under
    ``…/site-packages/backend/…`` where the old ``parents[4]`` landed on the
    interpreter lib dir, not the repo — so the pipeline spawned with a stray
    empty ``state.json`` and re-ingested already-done tickets into promptless
    windows. The resolver must find the dir that actually holds ``config.toml``.
    """
    from backend.web.addons import ticket_ingestion as si

    monkeypatch.delenv("MINDFLOCK_REPO_ROOT", raising=False)

    # 1. Explicit env override wins outright.
    monkeypatch.setenv("MINDFLOCK_REPO_ROOT", str(tmp_path))
    assert si._resolve_repo_root() == tmp_path.resolve()
    monkeypatch.delenv("MINDFLOCK_REPO_ROOT", raising=False)

    # 2. Installed layout: no config.toml above __file__ → fall back to the cwd
    #    that holds config.toml (as ``mindflock serve`` is launched from the repo).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(repo)
    fake_installed = (
        tmp_path
        / "lib"
        / "python3.13"
        / "site-packages"
        / "mindflock"
        / "web"
        / "addons"
        / "ticket_ingestion.py"
    )
    fake_installed.parent.mkdir(parents=True)
    fake_installed.write_text("", encoding="utf-8")
    monkeypatch.setattr(si, "__file__", str(fake_installed))
    assert si._resolve_repo_root() == repo.resolve()
