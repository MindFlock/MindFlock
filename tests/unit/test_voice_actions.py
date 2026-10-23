"""Pocket mode's one-tap dialog buttons: the single-use capability tokens
behind them (:mod:`backend.web.core.voice_actions`) and the public route that
spends one (``POST /api/notify/act/<token>``).

The token is the whole credential a lock-screen button carries, so what these
pin is the token's *narrowness*: one dialog, one key, once, ten minutes, and
spent together with its siblings — and that the route does exactly what the
UI's own answer call does (the same helper, so every refusal it has applies).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend.web import server
from backend.web.core import voice_actions

client = TestClient(server.app)

_HEX32 = re.compile(r"^[a-f0-9]{32}$")


@pytest.fixture(autouse=True)
def _fresh():
    voice_actions._reset_for_tests()
    yield
    voice_actions._reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    """A controllable clock for expiry: ``clock[0] += seconds``."""
    now = [1000.0]
    monkeypatch.setattr(voice_actions, "_now", lambda: now[0])
    return now


# --------------------------------------------------------------------------- #
# The token store
# --------------------------------------------------------------------------- #
def test_issue_mints_a_32_hex_token_that_consumes_once():
    tok = voice_actions.issue("alpha", "d1", "1")
    assert _HEX32.match(tok)
    assert voice_actions.consume(tok) == {
        "title": "alpha",
        "dialog_id": "d1",
        "key": "1",
    }
    # Single use: the second press of the same button answers nothing.
    assert voice_actions.consume(tok) is None


def test_two_tokens_never_collide():
    assert voice_actions.issue("a", "d", "1") != voice_actions.issue("a", "d", "1")


def test_unknown_and_malformed_tokens_consume_to_none():
    assert voice_actions.consume("0" * 32) is None
    assert voice_actions.consume("") is None
    assert voice_actions.consume(None) is None  # type: ignore[arg-type]


def test_a_token_expires_after_ten_minutes(clock):
    tok = voice_actions.issue("alpha", "d1", "1")
    clock[0] += voice_actions.TTL_S - 1
    fresh = voice_actions.issue("alpha", "d2", "1")
    clock[0] += 2  # the first is now past its TTL, the second is not
    assert voice_actions.consume(tok) is None
    assert voice_actions.consume(fresh) == {
        "title": "alpha",
        "dialog_id": "d2",
        "key": "1",
    }


def test_the_oldest_tokens_are_evicted_past_the_ceiling():
    first = voice_actions.issue("alpha", "d0", "1")
    for n in range(voice_actions.MAX_LIVE):
        last = voice_actions.issue("alpha", "d%d" % (n + 1), "1")
    assert voice_actions.live_count() == voice_actions.MAX_LIVE
    assert voice_actions.consume(first) is None  # pushed out
    assert voice_actions.consume(last) is not None


def test_spending_one_button_spends_the_dialogs_other_buttons():
    """Yes and No are minted together; after a Yes the No must not survive to
    land on an identical prompt that comes up later with the same id."""
    yes = voice_actions.issue("alpha", "d1", "1")
    no = voice_actions.issue("alpha", "d1", "3")
    other_dialog = voice_actions.issue("alpha", "d2", "1")
    other_session = voice_actions.issue("beta", "d1", "1")
    assert voice_actions.consume(yes)["key"] == "1"
    assert voice_actions.consume(no) is None
    # Only the SAME dialog of the SAME session is spent with it.
    assert voice_actions.consume(other_dialog)["dialog_id"] == "d2"
    assert voice_actions.consume(other_session)["title"] == "beta"


def test_expired_tokens_are_swept_on_issue(clock):
    for n in range(5):
        voice_actions.issue("alpha", "d%d" % n, "1")
    clock[0] += voice_actions.TTL_S + 1
    voice_actions.issue("alpha", "fresh", "1")
    assert voice_actions.live_count() == 1


# --------------------------------------------------------------------------- #
# POST /api/notify/act/<token>
# --------------------------------------------------------------------------- #
@pytest.fixture
def rig(monkeypatch):
    """One session ``w``, no budget lock, and a recording stand-in for the
    server's answer helper (``server._answer_dialog``) that says yes."""
    instances = {"w": SimpleNamespace(Title="w", Program="claude")}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server, "_budget_locked", lambda title: False)
    calls: list = []
    state = SimpleNamespace(reply=({"ok": True, "activity_before": "clarify"}, 200))

    def _answer(inst, title, text, keys, dialog_id, by):
        calls.append(
            {
                "inst": inst,
                "title": title,
                "text": text,
                "keys": keys,
                "dialog_id": dialog_id,
                "by": by,
            }
        )
        return state.reply

    monkeypatch.setattr(server, "_answer_dialog", _answer)
    return SimpleNamespace(calls=calls, state=state, instances=instances)


def _act(token: str):
    return client.post("/api/notify/act/" + token)


def test_act_answers_the_dialog_exactly_like_the_answer_route(rig):
    tok = voice_actions.issue("w", "d1", "1")
    r = _act(tok)
    assert r.status_code == 200 and r.json()["ok"] is True
    (call,) = rig.calls
    assert call["inst"] is rig.instances["w"]
    # No text, the one key, pinned to the dialog, as a person — the body the
    # phone page's own Yes button would have posted.
    assert (call["title"], call["text"], call["keys"]) == ("w", "", ["1"])
    assert (call["dialog_id"], call["by"]) == ("d1", "user")


def test_act_is_404_on_reuse_unknown_and_expired(rig, clock):
    tok = voice_actions.issue("w", "d1", "1")
    assert _act(tok).status_code == 200
    r = _act(tok)  # a second press
    assert r.status_code == 404 and "error" in r.json()
    assert _act("f" * 32).status_code == 404  # never issued
    old = voice_actions.issue("w", "d1", "1")
    clock[0] += voice_actions.TTL_S + 1
    assert _act(old).status_code == 404
    # Only the first press reached the agent.
    assert len(rig.calls) == 1


def test_act_passes_the_answer_routes_refusals_through(rig):
    rig.state.reply = ({"error": "the prompt changed", "dialog_changed": True}, 409)
    r = _act(voice_actions.issue("w", "d1", "1"))
    assert r.status_code == 409 and r.json()["dialog_changed"] is True
    rig.state.reply = (
        {"error": "that prompt was just answered", "dialog_answered": True},
        409,
    )
    r = _act(voice_actions.issue("w", "d1", "1"))
    assert r.status_code == 409 and r.json()["dialog_answered"] is True
    rig.state.reply = ({"error": "failed to send to agent session"}, 502)
    assert _act(voice_actions.issue("w", "d1", "1")).status_code == 502


def test_act_is_404_for_a_session_that_is_gone(rig):
    tok = voice_actions.issue("ghost", "d1", "1")
    r = _act(tok)
    assert r.status_code == 404 and "instance not found" in r.json()["error"]
    assert rig.calls == []


def test_act_honours_the_budget_lock(rig, monkeypatch):
    monkeypatch.setattr(server, "_budget_locked", lambda title: True)
    r = _act(voice_actions.issue("w", "d1", "1"))
    assert r.status_code == 409 and r.json()["budget_locked"] is True
    assert rig.calls == []


def test_act_only_presses_a_key_the_answer_route_allows(rig):
    """The store is not the validator: a key outside the answer allow-list is
    refused the way /answer refuses it, and never typed."""
    r = _act(voice_actions.issue("w", "d1", "C-c"))
    assert r.status_code == 400 and "not allowed" in r.json()["error"]
    assert rig.calls == []


def test_act_route_is_post_only():
    paths = server.app.openapi()["paths"]
    assert set(paths["/api/notify/act/{token}"]) == {"post"}
