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
    # "A session finishes its work" is on for someone who never chose.
    assert rules["session_idle"]["enabled"] is True
    # Noisy opt-in rules start disabled (must be turned on explicitly).
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
    """The Settings switches (pinned in frontend/src/__tests__/notifyWiring.test.ts)
    POST a route the server serves, and the served addon honours the result."""
    assert "/api/notify/rules/" in client.get("/app.js").text  # POST per-rule toggle
    assert "post" in server.app.openapi()["paths"]["/api/notify/rules/{rule_id}"]
    notify = client.get("/addons/notify.js").text
    assert "rule.enabled === false" in notify  # muted rules are skipped
    assert "refreshRules" in notify  # applies without reload


# --------------------------------------------------------------------------- #
# Plain words for Settings → Notifications
# --------------------------------------------------------------------------- #
def test_every_rule_has_a_plain_one_line_description():
    import re

    rules = client.get("/api/notify/config").json()["rules"]
    for r in rules:
        for key in ("label", "description"):
            text = r.get(key) or ""
            assert text, (r["id"], key)
            assert not re.search(r"\{[a-z_]+\}", text), (r["id"], key)
            assert "\n" not in text and len(text) <= 90, (r["id"], key)
            # No internal ids or event names in the words.
            assert not re.search(r"session\.[a-z_]+|_[a-z]+_", text), (r["id"], key)


# --------------------------------------------------------------------------- #
# session_idle: on by default, but never flipped for someone who chose
# --------------------------------------------------------------------------- #
def _write_notifs(**notifs):
    S.update_settings(notifications=notifs)


def test_session_idle_stays_off_for_someone_who_saved_other_choices():
    # Before the new default, absence from enabled_rules meant "off".
    _write_notifs(muted_rules=["pr_closed"], enabled_rules=[])
    assert (
        _enabled(client.get("/api/notify/config").json()["rules"], "session_idle")
        is False
    )
    _write_notifs(muted_rules=[], enabled_rules=["precommit_running"])
    assert (
        _enabled(client.get("/api/notify/config").json()["rules"], "session_idle")
        is False
    )


def test_session_idle_an_old_opt_in_stays_on():
    _write_notifs(muted_rules=["pr_closed"], enabled_rules=["session_idle"])
    assert (
        _enabled(client.get("/api/notify/config").json()["rules"], "session_idle")
        is True
    )


def test_session_idle_toggles_round_trip_with_other_choices_saved():
    _write_notifs(muted_rules=["pr_closed"], enabled_rules=[])
    r = client.post("/api/notify/rules/session_idle", json={"enabled": True})
    assert _enabled(r.json()["rules"], "session_idle") is True
    notifs = S.load_settings().notifications
    assert "session_idle" in notifs.enabled_rules
    assert "session_idle" not in notifs.muted_rules
    r = client.post("/api/notify/rules/session_idle", json={"enabled": False})
    assert _enabled(r.json()["rules"], "session_idle") is False
    notifs = S.load_settings().notifications
    assert "session_idle" in notifs.muted_rules
    assert "session_idle" not in notifs.enabled_rules


def test_session_idle_dispatches_by_default():
    from backend.web.addons import notify

    assert "session_idle" in {r["id"] for r in notify._enabled_rules()}
