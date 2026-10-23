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


# --------------------------------------------------------------------------- #
# Pocket mode: the agent's words in the push, and one-tap dialog buttons
# --------------------------------------------------------------------------- #
import re  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from backend.web.addons import notify as notify_addon  # noqa: E402
from backend.web.core import agent_io as _agent_io  # noqa: E402
from backend.web.core import mobile_access, mobile_announce, voice_actions  # noqa: E402
from backend.session import tmux as _tmux  # noqa: E402

_PHONE = "http://box.tail.net:8765/m"
_HEX32 = re.compile(r"^[a-f0-9]{32}$")


@pytest.fixture
def pushes(monkeypatch):
    """Capture what dispatch would push, without touching the transport."""
    sent: list = []
    monkeypatch.setattr(
        notify_addon.ntfy, "publish_soon", lambda cfg, **kw: sent.append(kw)
    )
    voice_actions._reset_for_tests()
    yield sent
    voice_actions._reset_for_tests()


@pytest.fixture
def phone():
    """A known phone URL (tests/conftest.py already stubs the re-probe)."""
    mobile_announce.remember_url(_PHONE)
    yield
    mobile_announce.remember_url(None)


def _ntfy_on(server="https://ntfy.example.test"):
    """Pushes on, to a self-hosted server by default: the public ntfy.sh gets
    no one-tap buttons (``one_tap_allowed``), which its own test covers."""
    S.update_settings(
        notifications={"ntfy_enabled": True, "ntfy_topic": "t1", "ntfy_server": server}
    )


def _turn_ended(session="alpha", **data) -> dict:
    return {
        "seq": 2,
        "event": "session.turn_ended",
        "session": session,
        "old": None,
        "new": None,
        "ts": 0.0,
        "data": {"idle_for": 46.0, **data},
    }


def _clarify(session="alpha") -> dict:
    return {
        "seq": 1,
        "event": "session.activity_changed",
        "session": session,
        "old": "working",
        "new": "clarify",
        "ts": 0.0,
        "data": {},
    }


def _dialog(**over) -> dict:
    """A parsed Claude Code permission dialog, as GET /dialog serves it."""
    body = {
        "id": "abc123def456",  # pragma: allowlist secret
        "parsed": True,
        "question": "Bash command — git push origin main. Do you want to proceed?",
        "command": "git push origin main",
        "options": [
            {"key": "1", "label": "Yes", "kind": "yes"},
            {"key": "2", "label": "Yes, and don't ask again", "kind": "always"},
            {"key": "3", "label": "No", "kind": "no"},
        ],
    }
    body.update(over)
    return body


# --- session_idle: the spoken line ---------------------------------------- #
def test_finished_push_carries_the_spoken_line(pushes, phone):
    _ntfy_on()
    notify_addon.NotifyAddon()._on_event(
        _turn_ended(say="Done. The tests pass, should I push?", has_question=True)
    )
    (push,) = pushes
    assert push["message"] == "Done. The tests pass, should I push?"
    assert push["title"] == "alpha has finished"  # titles never change
    # The voice deep link: the page opens on the session, shows the line, and
    # one tap speaks it and listens.
    assert push["click"] == _PHONE + "?s=alpha&voice=1"
    # The URL is NOT in the spoken text — a read-aloud app would spell it.
    assert "http" not in push["message"]
    assert push.get("actions") is None


def test_finished_push_without_a_say_line_keeps_the_generic_body(pushes, phone):
    _ntfy_on()
    notify_addon.NotifyAddon()._on_event(_turn_ended(say=None, source="none"))
    (push,) = pushes
    assert push["message"].startswith("The agent finished its turn")
    assert push["message"].endswith(_PHONE + "?s=alpha&voice=1")
    pushes.clear()
    notify_addon.NotifyAddon()._on_event(_turn_ended(session="beta", say="   "))
    assert pushes[0]["message"].startswith("The agent finished its turn")


def test_agent_words_off_keeps_every_body_generic(pushes, phone, monkeypatch):
    _ntfy_on()
    S.update_settings(notifications={"ntfy_agent_words": False})
    addon = notify_addon.NotifyAddon()
    addon._on_event(_turn_ended(say="Done."))
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    addon._on_event(_clarify())
    idle, ask = pushes
    assert idle["message"].startswith("The agent finished its turn")
    assert "Done." not in idle["message"]
    assert ask["message"].startswith("The agent is waiting on a clarification.")
    assert "git push" not in ask["message"]
    # The buttons carry no words, so they stay: Yes / No / Open.
    assert [a["label"] for a in ask["actions"]] == ["Yes", "No", "Open"]


def test_agent_words_is_on_by_default():
    assert S.load_settings().notifications.ntfy_agent_words is True
    assert notify_addon._agent_words_on() is True


# --- needs_input: the question, and the buttons --------------------------- #
def test_dialog_push_carries_the_question_and_three_buttons(pushes, phone, monkeypatch):
    _ntfy_on()
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["title"] == "alpha needs you: Question"
    assert push["message"] == (
        "Bash command — git push origin main. Do you want to proceed?"
        " — git push origin main"
    )
    assert "http" not in push["message"]
    assert push["click"] == _PHONE + "?s=alpha&voice=1"
    yes, no, open_ = push["actions"]
    assert len(push["actions"]) <= 3
    for btn in (yes, no):
        assert btn["action"] == "http" and btn["method"] == "POST"
        assert btn["clear"] is True
        assert btn["url"].startswith("http://box.tail.net:8765/api/notify/act/")
        assert _HEX32.match(btn["url"].rsplit("/", 1)[1])
    assert (yes["label"], no["label"]) == ("Yes", "No")
    assert open_ == {
        "action": "view",
        "label": "Open",
        "url": _PHONE + "?s=alpha&voice=1",
    }
    # Each button is bound to THIS dialog and the option's own key.
    assert voice_actions.consume(yes["url"].rsplit("/", 1)[1]) == {
        "title": "alpha",
        "dialog_id": "abc123def456",
        "key": "1",
    }
    # (Spending Yes spent No too.)
    assert voice_actions.consume(no["url"].rsplit("/", 1)[1]) is None


def test_dialog_push_never_offers_always(pushes, phone, monkeypatch):
    """A standing permission is never one tap away from a lock screen."""
    _ntfy_on()
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    blob = str(pushes[0]["actions"]).lower()
    assert "always" not in blob and "don't ask" not in blob


def test_dialog_push_without_a_no_option_has_yes_and_open(pushes, phone, monkeypatch):
    _ntfy_on()
    monkeypatch.setattr(
        notify_addon,
        "_read_dialog",
        lambda title: _dialog(options=[{"key": "1", "label": "Yes", "kind": "yes"}]),
    )
    notify_addon.NotifyAddon()._on_event(_clarify())
    assert [a["label"] for a in pushes[0]["actions"]] == ["Yes", "Open"]


def test_dialog_without_a_yes_option_has_no_buttons(pushes, phone, monkeypatch):
    """A free-text question, or a numbered menu — no one-tap answer, but the
    question still travels."""
    _ntfy_on()
    monkeypatch.setattr(
        notify_addon,
        "_read_dialog",
        lambda title: _dialog(
            question="Which file?",
            command=None,
            options=[
                {"key": "1", "label": "a.py", "kind": "other"},
                {"key": "2", "label": "b.py", "kind": "other"},
            ],
        ),
    )
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["message"] == "Which file?"
    assert push.get("actions") is None


def test_dialog_push_without_a_phone_url_has_no_http_buttons(pushes, monkeypatch):
    """No tailnet: no base to post back to, so no button — the question still
    goes, and the push falls back to the user's own click URL."""
    _ntfy_on()
    mobile_announce.remember_url(None)
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["message"].startswith("Bash command")
    assert push["click"] is None and push.get("actions") is None
    assert voice_actions.live_count() == 0  # nothing minted for nobody


def test_shared_link_dialog_push_has_open_only(pushes, phone, monkeypatch):
    """The shared link is answered by whichever device is up; a token minted
    here would 404 on another one. So: Open, and no http buttons."""
    _ntfy_on()
    monkeypatch.setattr(mobile_access._shared_link, "advertised_url", lambda: _PHONE)
    from backend.web.core import remote as _remote

    monkeypatch.setattr(_remote, "self_identity", lambda: {"key": "box"})
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["actions"] == [
        {"action": "view", "label": "Open", "url": _PHONE + "?s=box%3A%3Aalpha&voice=1"}
    ]
    assert voice_actions.live_count() == 0


def test_no_token_ever_travels_in_a_button(pushes, monkeypatch):
    """The cached phone URL is token-free by construction; even one that isn't
    must not leak into the buttons (they are stored on the ntfy server)."""
    _ntfy_on()
    mobile_announce.remember_url(_PHONE + "?token=SECRET")
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    blob = " ".join(a["url"] for a in pushes[0]["actions"])
    assert "SECRET" not in blob and "token=" not in blob
    mobile_announce.remember_url(None)


def test_unparsed_or_missing_dialog_keeps_the_generic_body(pushes, phone, monkeypatch):
    _ntfy_on()
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: None)
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["message"] == (
        "The agent is waiting on a clarification.\n" + _PHONE + "?s=alpha&voice=1"
    )
    assert push.get("actions") is None


def test_dialog_body_is_capped_and_clean():
    assert notify_addon.dialog_body(None) == ""
    assert notify_addon.dialog_body(_dialog(question="", command="x")) == ""
    assert (
        notify_addon.dialog_body(_dialog(question="Proceed?", command=None))
        == "Proceed?"
    )
    assert (
        notify_addon.dialog_body(
            _dialog(question=" Proceed?\n ", command="  git   push ")
        )
        == "Proceed? — git push"
    )
    long = notify_addon.dialog_body(_dialog(command="x" * 500))
    assert len(long) == notify_addon.MAX_DIALOG_BODY and long.endswith("…")


def test_a_dialog_read_failure_never_breaks_the_push(pushes, phone, monkeypatch):
    _ntfy_on()

    def _boom(title):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(notify_addon._agent_io, "current_dialog", _boom)
    monkeypatch.setattr(
        server.ENGINE, "instances", {"alpha": SimpleNamespace(Program="claude")}
    )
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    notify_addon.NotifyAddon()._on_event(_clarify())
    assert pushes[0]["message"].startswith("The agent is waiting on a clarification.")


# --- the dialog read: the same parse GET /dialog serves ------------------- #
def test_read_dialog_uses_the_dialog_routes_parser(monkeypatch):
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"alpha": SimpleNamespace(Title="alpha", Program="codex")},
    )
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    asked: list = []

    def _current(name, program):
        asked.append((name, program))
        return _dialog(), None

    monkeypatch.setattr(_agent_io, "current_dialog", _current)
    assert notify_addon._read_dialog("alpha") == _dialog()
    assert asked == [(_tmux.to_mindflock_tmux_name("alpha"), "codex")]


def test_read_dialog_is_none_without_a_session_pane_or_parse(monkeypatch):
    assert notify_addon._read_dialog("nobody") is None  # not a session
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"alpha": SimpleNamespace(Title="alpha", Program="claude")},
    )
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    assert notify_addon._read_dialog("alpha") is None  # no live pane
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    monkeypatch.setattr(
        _agent_io, "current_dialog", lambda n, p: (None, "capture failed")
    )
    assert notify_addon._read_dialog("alpha") is None  # tmux refused
    monkeypatch.setattr(
        _agent_io,
        "current_dialog",
        lambda n, p: (_dialog(parsed=False, options=[]), None),
    )
    assert notify_addon._read_dialog("alpha") is None  # nothing the provider parses


def test_dialog_push_reads_the_live_screen_at_fire_time(pushes, phone, monkeypatch):
    """End to end through the real reader: the event names the session, the
    reader finds its pane and parses what is on it."""
    _ntfy_on()
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"alpha": SimpleNamespace(Title="alpha", Program="claude")},
    )
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    monkeypatch.setattr(_agent_io, "current_dialog", lambda n, p: (_dialog(), None))
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["message"].startswith("Bash command — git push origin main.")
    assert [a["label"] for a in push["actions"]] == ["Yes", "No", "Open"]


def test_only_the_two_spoken_rules_get_the_voice_link(pushes, phone):
    _ntfy_on()
    notify_addon.NotifyAddon()._on_event({**_clarify(), "new": "offline"})
    (push,) = pushes
    assert push["click"] == _PHONE + "?s=alpha"
    assert push["message"].endswith(_PHONE + "?s=alpha")


# --- the ntfy view: the setting travels with the channel ------------------ #
def test_ntfy_view_carries_agent_words_and_saves_it():
    assert client.get("/api/notify/ntfy").json()["agent_words"] is True
    r = client.post("/api/notify/ntfy", json={"agent_words": False})
    assert r.status_code == 200 and r.json()["agent_words"] is False
    S.invalidate()
    assert S.load_settings().notifications.ntfy_agent_words is False
    assert client.get("/api/notify/ntfy").json()["agent_words"] is False
    r = client.post("/api/notify/ntfy", json={"agent_words": True})
    assert r.json()["agent_words"] is True
    S.invalidate()
    assert S.load_settings().notifications.ntfy_agent_words is True


def test_agent_words_save_leaves_the_rest_of_the_channel_alone():
    S.update_settings(
        notifications={
            "ntfy_enabled": True,
            "ntfy_topic": "keep-me",
            "ntfy_token": "tk",
        }
    )
    v = client.post("/api/notify/ntfy", json={"agent_words": False}).json()
    assert v["enabled"] is True and v["topic"] == "keep-me" and v["has_token"] is True


# --- the buttons are a permission channel: the owner's switch, no public server
def test_no_one_tap_buttons_when_voice_approval_is_off(pushes, phone, monkeypatch):
    _ntfy_on()
    S.update_settings(voice={"approve_dialogs": False})
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    # The words still travel; only Open remains.
    assert "git push" in push["message"]
    assert [a["label"] for a in push["actions"]] == ["Open"]


def test_no_one_tap_buttons_on_the_public_ntfy_server(pushes, phone, monkeypatch):
    _ntfy_on(server="")  # "" = the public ntfy.sh
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: _dialog())
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert [a["label"] for a in push["actions"]] == ["Open"]


def test_one_tap_allowed_fails_closed(monkeypatch):
    class Boom:
        is_public_server = False

    monkeypatch.setattr(
        notify_addon.settings_store,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("x")),
    )
    assert notify_addon.one_tap_allowed(Boom()) is False
