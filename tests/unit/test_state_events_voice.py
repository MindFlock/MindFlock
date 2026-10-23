"""The spoken line on the two "it finished" events (voice mode).

``session.activity_changed new="idle"`` and ``session.turn_ended`` carry
``data = {say, has_question, source}`` from ``core.spoken.spoken_for``; each
channel remembers the reply it last announced per session, so ``say`` is
null when the session goes idle again without a newer reply. The settle,
dwell and claim gates are the ones test_turn_boundary pins; here they are
zeroed or granted so each test exercises the payload alone.
"""

from __future__ import annotations

import json

import pytest

from backend.providers import activity_markers as am
from backend.web import server
from backend.web.core import events as events_mod
from backend.web.core import session_removal, spoken, state_events, voice
from backend.web.core.events import EventBus

NONE = {"say": None, "has_question": False, "source": "none"}


class _Inst:
    Program = "claude"

    def __init__(self, title):
        self.Title = title

    def GetWorktreePath(self):  # noqa: N802
        return "/tmp/wt"


@pytest.fixture()
def bus(monkeypatch, tmp_path):
    b = EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(server._agent_state, "claim_work", lambda t: 5.0)
    monkeypatch.setattr(server.ENGINE, "instances", {"t": _Inst("t")})
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    spoken._REPLY_CACHE.clear()
    yield b
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    spoken._REPLY_CACHE.clear()


def _say(monkeypatch, rec):
    """Replace spoken_for with a settable record (``rec["v"]``)."""
    monkeypatch.setattr(
        spoken,
        "spoken_for",
        lambda title, inst: (
            dict(rec["v"])
            if rec.get("v")
            else {"say": None, "source": "none", "has_question": False, "ts": None}
        ),
    )


def _tick(title, activity):
    server._emit_state_changes(title, "running", activity, "agent")


def _idle_events(got):
    return [
        e
        for e in got
        if e["event"] == "session.activity_changed" and e["new"] == "idle"
    ]


def _ended(got):
    return [e for e in got if e["event"] == "session.turn_ended"]


def test_idle_carries_the_spoken_line_and_working_carries_nothing(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    rec = {
        "v": {
            "say": "All green. Push it?",
            "source": "hook",
            "has_question": True,
            "ts": 10.0,
        }
    }
    _say(monkeypatch, rec)
    _tick("t", "working")
    _tick("t", "idle")  # parks the settle candidate
    _tick("t", "idle")  # settles -> the announcement
    idle = _idle_events(got)
    assert len(idle) == 1
    assert idle[0]["data"] == {
        "say": "All green. Push it?",
        "has_question": True,
        "source": "hook",
    }
    # Back to work: a transition OUT of idle carries no words (and the first
    # sighting above only seeded the snapshot, so this is the first one).
    _tick("t", "working")
    working = [
        e
        for e in got
        if e["event"] == "session.activity_changed" and e["new"] == "working"
    ]
    assert len(working) == 1 and working[0]["data"] == {}


def test_a_second_idle_without_a_newer_reply_says_nothing(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    rec = {"v": {"say": "Done.", "source": "digest", "has_question": False, "ts": 10.0}}
    _say(monkeypatch, rec)
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert _idle_events(got)[-1]["data"]["say"] == "Done."
    # idle -> working -> idle with the SAME reply (a /clear, a bare prompt, a
    # CLI that files no reply): the old words must not be spoken again.
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert len(_idle_events(got)) == 2
    assert _idle_events(got)[-1]["data"] == NONE
    # A newer reply (new stamp) speaks again.
    rec["v"] = {"say": "Done.", "source": "digest", "has_question": False, "ts": 11.0}
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert _idle_events(got)[-1]["data"]["say"] == "Done."
    # …and so does the same stamp with different words (a digest refreshed
    # from the transcript).
    rec["v"] = {
        "say": "Done, and pushed.",
        "source": "digest",
        "has_question": False,
        "ts": 11.0,
    }
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert _idle_events(got)[-1]["data"]["say"] == "Done, and pushed."


def test_turn_ended_carries_the_same_keys_on_its_own_channel(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)
    rec = {
        "v": {
            "say": "Fixed it. Want a PR?",
            "source": "hook",
            "has_question": True,
            "ts": 1.0,
        }
    }
    _say(monkeypatch, rec)
    monkeypatch.setattr(server, "_TURN_END_DWELL_S", 0.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 5.0)
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")  # settles: activity_changed(idle) AND, dwell 0, turn_ended
    assert _idle_events(got)[0]["data"]["say"] == "Fixed it. Want a PR?"
    ended = _ended(got)
    assert len(ended) == 1
    data = ended[0]["data"]
    assert "idle_for" in data and isinstance(data["idle_for"], float)
    # The idle channel announced it first; the push channel still carries it.
    assert {k: data[k] for k in ("say", "has_question", "source")} == {
        "say": "Fixed it. Want a PR?",
        "has_question": True,
        "source": "hook",
    }
    # A second work cycle with no newer reply: idle_for stays, the words go.
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert len(_ended(got)) == 2
    data2 = _ended(got)[1]["data"]
    assert "idle_for" in data2
    assert {k: data2[k] for k in ("say", "has_question", "source")} == NONE


def test_unknown_session_or_a_failing_reader_still_emits(bus, monkeypatch):
    got = []
    bus.subscribe(got.append)

    def boom(title, inst):
        raise RuntimeError("reader broke")

    monkeypatch.setattr(spoken, "spoken_for", boom)
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert _idle_events(got)[-1]["data"] == NONE
    # Not in the registry at all: no reader call, the event still goes out.
    _tick("ghost", "working")
    _tick("ghost", "idle")
    _tick("ghost", "idle")
    assert [e["session"] for e in _idle_events(got)] == ["t", "ghost"]
    assert _idle_events(got)[-1]["data"] == NONE


def test_the_payload_reads_the_real_reply_file(bus):
    """End to end through core.spoken: the Stop hook's file is what speaks."""
    got = []
    bus.subscribe(got.append)
    p = am.reply_path("mindflock_t")
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps({"ts": 1700000000, "text": "Ran it.\n\nSay: Tests pass. Merge?"})
    )
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert _idle_events(got)[-1]["data"] == {
        "say": "Tests pass. Merge?",
        "has_question": True,
        "source": "hook",
    }


def test_session_removal_forgets_flag_reply_and_memory(bus):
    # Remembered on the idle channel…
    p = am.reply_path("mindflock_t")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"ts": 1, "text": "Say: bye"}))
    voice.set_on("mindflock_t", True)
    _tick("t", "working")
    _tick("t", "idle")
    _tick("t", "idle")
    assert "t" in state_events._SPOKEN_ANNOUNCED
    assert voice.is_on("mindflock_t") and p.exists()
    # …and gone with the session: no flag (the hook would keep injecting the
    # instruction into a namesake), no reply (a namesake must not speak the
    # dead one's last words), no memory.
    session_removal._forget_voice("t")
    assert not voice.is_on("mindflock_t")
    assert not p.exists()
    assert "t" not in state_events._SPOKEN_ANNOUNCED
    assert session_removal._forget_voice in server._SESSION_REMOVED_HOOKS
