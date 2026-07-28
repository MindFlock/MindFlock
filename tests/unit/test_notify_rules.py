"""Per-rule notification opt-out: the user picks which events notify them.

Covers the settings group, the /api/notify/config enabled state, the
POST /api/notify/rules/<id> toggle + persistence, and the frontend wiring.
"""

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.web import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #
def test_notification_settings_roundtrip():
    ns = S.NotificationSettings(muted_rules=["needs_input", "budget_exceeded"])
    assert S.NotificationSettings.from_dict(ns.to_dict()).muted_rules == [
        "needs_input",
        "budget_exceeded",
    ]
    assert S.NotificationSettings().to_dict() == {}  # empty omitted


# --------------------------------------------------------------------------- #
# Config reflects per-rule state; every rule defaults to on
# --------------------------------------------------------------------------- #
def test_config_rules_default_state():
    rules = {r["id"]: r for r in client.get("/api/notify/config").json()["rules"]}
    assert rules and all(r["id"] and r["label"] for r in rules.values())
    # Default-on (opt-out) rules start enabled.
    assert rules["needs_input"]["enabled"] is True
    assert rules["pr_closed"]["enabled"] is True
    assert rules["budget_exceeded"]["enabled"] is True
    # Noisy opt-in rules start disabled (must be turned on explicitly).
    assert rules["session_idle"]["enabled"] is False
    assert rules["precommit_running"]["enabled"] is False
    # Pre-commit *failure* is actionable, not noisy → default-on, fires on the
    # session parking in the "interrupt" stage.
    assert rules["precommit_failed"]["enabled"] is True
    assert rules["precommit_failed"]["event"] == "session.stage_changed"
    assert rules["precommit_failed"]["new"] == "interrupt"


def test_toggle_rule_off_persists_and_reflects():
    rid = client.get("/api/notify/config").json()["rules"][0]["id"]
    r = client.post("/api/notify/rules/" + rid, json={"enabled": False})
    assert r.status_code == 200
    # The returned + re-fetched config both show it muted.
    assert _enabled(r.json()["rules"], rid) is False
    assert _enabled(client.get("/api/notify/config").json()["rules"], rid) is False
    # Persisted as an opt-out in the settings store.
    S.invalidate()
    assert rid in S.load_settings().notifications.muted_rules


def test_toggle_rule_back_on_removes_mute():
    rid = client.get("/api/notify/config").json()["rules"][0]["id"]
    client.post("/api/notify/rules/" + rid, json={"enabled": False})
    client.post("/api/notify/rules/" + rid, json={"enabled": True})
    S.invalidate()
    assert rid not in S.load_settings().notifications.muted_rules
    assert _enabled(client.get("/api/notify/config").json()["rules"], rid) is True


def test_unknown_rule_id_is_404():
    assert (
        client.post("/api/notify/rules/nope", json={"enabled": False}).status_code
        == 404
    )


def _enabled(rules, rid):
    return next(r["enabled"] for r in rules if r["id"] == rid)


# --------------------------------------------------------------------------- #
# Frontend wiring
# --------------------------------------------------------------------------- #
def test_frontend_renders_per_rule_toggles():
    js = client.get("/app.js").text
    assert "/api/notify/rules/" in js
    assert "/api/notify/rules/" in js  # POST per-rule toggle
    assert "refreshRules" in js  # applies without reload
    notify = client.get("/addons/notify.js").text
    assert "rule.enabled === false" in notify  # muted rules are skipped
    assert "refreshRules" in notify
