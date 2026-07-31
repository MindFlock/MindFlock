"""Sessions that ran out of usage with nothing queued (server._watch_*).

The prompt queue has always ridden out a usage limit for sessions with a prompt
waiting. A session that simply ran out mid-task has an empty queue, so nothing
was watching it: it sat on its CLI's limit screen long after the window
reopened, until a human noticed. This is the watcher that covers that gap, plus
the one event it emits so "your usage is back" can reach a phone.

The three things worth pinning: it acts only when the window has actually
reopened, it never fights the queue drain for the same session, and it announces
the reopening once — not once per limited session, and never at all for a window
that merely rolled over while everything was fine.
"""

from __future__ import annotations

import types

import pytest

from backend.config import settings as S
from backend.web import server
from backend.web.core import events, prompt_queue as _pq


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


@pytest.fixture
def watch(monkeypatch):
    """A single live session parked on the usage-limit screen, with every
    shell-out replaced. Returns a handle whose ``limited_until`` decides what
    the limit gate reports and whose ``sent`` records what reached the agent."""
    h = types.SimpleNamespace(
        limited_until=0.0, sent=[], escapes=[], events=[], now=10_000.0
    )
    inst = types.SimpleNamespace(
        Program="claude",
        Started=lambda: True,
        Status="running",
    )
    monkeypatch.setattr(server.time, "time", lambda: h.now)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server.ENGINE, "instances", {"alpha": inst})
    monkeypatch.setattr(server, "_ensure_agent_session", lambda i, t: ("sess", None))
    monkeypatch.setattr(server, "_refresh_limit_state", lambda i, t, n: h.limited_until)
    monkeypatch.setattr(
        server, "_send_escape_to_agent", lambda n: h.escapes.append(n) or True
    )
    monkeypatch.setattr(
        server,
        "_send_to_agent",
        lambda n, text, submit=True: h.sent.append(text) is None,
    )
    monkeypatch.setattr(
        events.BUS,
        "emit",
        lambda name, **kw: h.events.append((name, kw)),
    )
    # The watcher's candidate list is the event snapshot the state tick keeps.
    monkeypatch.setattr(
        server, "_EVENT_SNAPSHOT", {"alpha": {"activity": "limit"}}, raising=False
    )
    server._QUEUE_STATE.pop("alpha", None)
    monkeypatch.setattr(server, "_LAST_RESTORE_EMIT", 0.0, raising=False)
    _pq.clear("alpha")
    yield h
    server._QUEUE_STATE.pop("alpha", None)
    _pq.clear("alpha")


def _restored(h):
    return [kw for name, kw in h.events if name == "session.usage_restored"]


# --------------------------------------------------------------------------- #
# Acting only when the window has actually reopened
# --------------------------------------------------------------------------- #
def test_still_limited_is_left_alone(watch):
    watch.limited_until = watch.now + 3600
    server._watch_limited_sessions()
    assert watch.sent == [] and watch.escapes == []
    assert _restored(watch) == []


def test_reopened_window_resumes_the_session(watch):
    server._watch_limited_sessions()
    # Esc first (the CLI leaves its limit menu up until a key is pressed), then
    # the nudge — the same order the queue drain uses.
    assert watch.escapes == ["sess"]
    assert watch.sent == [server._LIMIT_RESUME_PROMPT]
    assert _restored(watch) == [{"session": "alpha", "data": {"resumed": True}}]


def test_a_session_with_no_limit_screen_is_not_watched(watch, monkeypatch):
    """The whole pass costs one dict read when nothing has run out."""
    monkeypatch.setattr(server, "_EVENT_SNAPSHOT", {"alpha": {"activity": "idle"}})
    server._watch_limited_sessions()
    assert watch.sent == [] and _restored(watch) == []


def test_a_paused_session_is_not_resumed(watch, monkeypatch):
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {
            "alpha": types.SimpleNamespace(
                Program="claude", Started=lambda: True, Status=server.session.Paused
            )
        },
    )
    server._watch_limited_sessions()
    assert watch.sent == [] and _restored(watch) == []


def test_a_dead_tmux_session_announces_nothing(watch, monkeypatch):
    """The send is how we know the resume landed; without it there is nothing
    to tell the user about."""
    monkeypatch.setattr(server, "_send_to_agent", lambda n, text, submit=True: False)
    server._watch_limited_sessions()
    assert _restored(watch) == []


# --------------------------------------------------------------------------- #
# Not fighting the queue drain
# --------------------------------------------------------------------------- #
def test_a_session_with_a_queued_prompt_is_left_to_the_drain(watch):
    """Both would send on the same tick; the drain's prompt is the better one."""
    _pq.enqueue("alpha", "do the thing")
    server._watch_limited_sessions()
    assert watch.sent == [] and watch.escapes == []


def test_a_disabled_queue_is_still_watched(watch):
    """Auto-run off means the drain will never send — so this session IS stuck,
    and the watcher is the only thing that will get it moving again."""
    _pq.enqueue("alpha", "do the thing")
    _pq.set_flags("alpha", enabled=False)
    server._watch_limited_sessions()
    assert watch.sent == [server._LIMIT_RESUME_PROMPT]


def test_the_nudge_is_sent_once_not_every_tick(watch):
    """Armed-gating, exactly like the drain: a nudge that doesn't take retries
    after the re-arm window rather than every five seconds."""
    server._watch_limited_sessions()
    watch.now += server._QUEUE_SEND_COOLDOWN + 1
    server._watch_limited_sessions()
    assert watch.sent == [server._LIMIT_RESUME_PROMPT]
    # ...but a stuck session does get a bounded retry.
    watch.now += server._QUEUE_REARM_IDLE + 1
    server._watch_limited_sessions()
    assert len(watch.sent) == 2


# --------------------------------------------------------------------------- #
# The announcement
# --------------------------------------------------------------------------- #
def test_usage_restored_is_announced_once_per_window(watch, monkeypatch):
    """Several sessions unblock in the same pass; "usage is back" is one fact
    about the account, not one per session."""
    inst = server.ENGINE.instances["alpha"]
    monkeypatch.setattr(
        server.ENGINE, "instances", {"alpha": inst, "beta": inst, "gamma": inst}
    )
    monkeypatch.setattr(
        server,
        "_EVENT_SNAPSHOT",
        {t: {"activity": "limit"} for t in ("alpha", "beta", "gamma")},
    )
    server._watch_limited_sessions()
    assert len(watch.sent) == 3  # every stuck session is resumed
    assert len(_restored(watch)) == 1  # but the news is announced once
    for t in ("beta", "gamma"):
        server._QUEUE_STATE.pop(t, None)


def test_a_later_window_announces_again(watch):
    server._watch_limited_sessions()
    watch.now += server._LIMIT_RESTORE_QUIET + 1
    server._QUEUE_STATE["alpha"]["armed"] = True
    server._watch_limited_sessions()
    assert len(_restored(watch)) == 2


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def test_auto_resume_off_still_announces(watch):
    """Knowing your usage is back is useful even when you want to restart the
    work yourself."""
    S.update_settings(general={"resume_on_usage_reset": False})
    server._watch_limited_sessions()
    assert watch.sent == []
    assert _restored(watch) == [{"session": "alpha", "data": {"resumed": False}}]


def test_auto_resume_defaults_to_on():
    """Unset reads as on — running out is temporary, and the point of the limit
    gate is that MindFlock picks the work back up."""
    assert S.load_settings().general.resume_on_usage_reset is None
    assert server._resume_on_usage_reset() is True


def test_auto_resume_survives_a_settings_round_trip():
    S.update_settings(general={"resume_on_usage_reset": False})
    S.invalidate()
    assert S.load_settings().general.resume_on_usage_reset is False
    assert server._resume_on_usage_reset() is False
    S.update_settings(general={"resume_on_usage_reset": True})
    assert server._resume_on_usage_reset() is True


# --------------------------------------------------------------------------- #
# The rules that carry it to a phone
# --------------------------------------------------------------------------- #
def test_both_usage_rules_exist_and_are_on_by_default():
    from backend.web.addons import notify

    by_id = {r["id"]: r for r in notify.NOTIFY_RULES}
    out, back = by_id["usage_limit"], by_id["usage_restored"]
    # Running out is just the activity transition — no bespoke event needed.
    assert out["event"] == "session.activity_changed" and out["new"] == "limit"
    # Coming back is the watcher's event, which only fires for a session that
    # had run out — so it can't fire on an ordinary window rollover.
    assert back["event"] == "session.usage_restored"
    assert out["default_enabled"] and back["default_enabled"]


def test_the_usage_rules_can_be_muted():
    from starlette.testclient import TestClient

    client = TestClient(server.app)
    for rule_id in ("usage_limit", "usage_restored"):
        r = client.post("/api/notify/rules/%s" % rule_id, json={"enabled": False})
        assert r.status_code == 200
        assert {x["id"]: x["enabled"] for x in r.json()["rules"]}[rule_id] is False
