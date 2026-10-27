"""A session is asking you something (``backend.web.core.asking``).

The question finder (:func:`asking.question_in`, the screen fallback), the
per-title state (seed, detect, human input, dismiss, a newer reply), and every
surface that reads it: the instance rows' ``asking``, ``GET /dialog``'s
``kind``, ``/answer`` on a ``q:`` id, ``POST /asking/dismiss``, the outbox's
``question`` rows, ``session.question`` (normal and missed-turn paths), the
``needs_input`` push (matching, body, dedupe, one push instead of two), the
voice router, Codex's ``last_assistant_text`` and the ``ask_popup`` setting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.providers import activity_markers as am
from backend.providers import codex as codex_mod
from backend.providers import codex_usage_api, thread_markers
from backend.web import server
from backend.web.addons import notify as notify_addon
from backend.web.core import agent_state, asking
from backend.web.core import events as events_mod
from backend.web.core import mobile_announce, outbox, proc, spoken, state_events
from backend.web.core import voice_actions, voice_router
from backend.web.core.events import EventBus

client = TestClient(server.app)
DATA = Path(__file__).parent / "data" / "codex_rollout"
TID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"


class _Inst:
    def __init__(self, title, program="claude"):
        self.Title = title
        self.Program = program

    def GetWorktreePath(self):  # noqa: N802
        return "/tmp/wt"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    for d in (asking._STATE, asking._INPUT_AT, asking._DIALOG_MEMO):
        d.clear()
    spoken._REPLY_CACHE.clear()
    agent_state._LAST_SCREEN.clear()
    with server._PROBE_CACHE_LOCK:
        server._PROBE_CACHE.clear()
    yield
    for d in (asking._STATE, asking._INPUT_AT, asking._DIALOG_MEMO):
        d.clear()
    spoken._REPLY_CACHE.clear()
    agent_state._LAST_SCREEN.clear()


def _reply(title, text, ts):
    """File ``title``'s reply the way Claude's Stop hook does."""
    p = am.reply_path("mindflock_" + title)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": ts, "text": text}))


# --------------------------------------------------------------------------- #
# question_in                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, want",
    [
        (
            "I fixed the bug.\n\nWhich approach do you prefer?",
            "Which approach do you prefer?",
        ),
        # Only the question sentences of the final paragraph.
        (
            "Done.\n\nThe tests pass. Should I push it now? Or open a PR first?",
            "Should I push it now? Or open a PR first?",
        ),
        # A question word opening the final paragraph, no "?" (models drop it).
        ("All set.\n\nShould I open a PR", "Should I open a PR"),
        # "I fixed it. Done?" — a "?" is a "?": the short question counts.
        ("I fixed it. Done?", "Done?"),
        # The options under the question: the question above them.
        (
            "Two ways to do it.\n\nWhich do you want?\n\n1. Rewrite it\n2. Patch it",
            "Which do you want?",
        ),
        # Markdown taken off.
        ("Done.\n\n**Want me to merge it?**", "Want me to merge it?"),
        # A Say: line that asks counts — and decides.
        ("Long reply…\n\nSay: Tests pass. Should I push?", "Should I push?"),
    ],
)
def test_question_in_finds_the_question_the_reply_ends_with(text, want):
    assert asking.question_in(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "Done. The tests pass.",
        # A "?" buried in an earlier paragraph: the agent moved on after it.
        "Should I use a dict? I went with a list.\n\nAll tests pass.",
        # Inside a code block.
        "Here it is:\n\n```python\nx = a if ok else b  # why?\n```",
        "Ran it:\n\n```\nAre you sure? [y/N]\n```",
        # Inside inline code in the final paragraph.
        "I added a `maybe?` operator to the parser.",
        # A "?" mentioned as a character (a real reply that popped up as a
        # question).
        'Wiring the tour into the top-bar mic now (first-use trigger, the "?" in its options).',
        "Press the '?' key for help.",
        # No "?" and no question word: conservative, not a question.
        "Let me know which you prefer.",
        # A Say: line that does not ask decides too.
        "Do you want X?\n\nSay: Fixed the bug, all tests pass.",
    ],
)
def test_question_in_is_conservative(text):
    assert asking.question_in(text) is None


def test_question_in_caps_the_text():
    got = asking.question_in("Ok.\n\n" + "Should I " + "really " * 100 + "do it?")
    assert got is not None and len(got) <= asking.QUESTION_MAX_CHARS
    assert got.endswith("…")


def test_screen_reply_is_the_paragraph_above_the_input_prompt():
    screen = (
        "some earlier output\n"
        "\n"
        "⏺ I renamed the helper.\n"
        "  Do you want the docs updated too?\n"
        "\n"
        "╭──────────────────────────────╮\n"
        "│ >                            │\n"
        "╰──────────────────────────────╯\n"
        "  ? for shortcuts\n"
    )
    text = asking.screen_reply(screen)
    assert text == "I renamed the helper.\nDo you want the docs updated too?"
    assert asking.question_in(text) == "Do you want the docs updated too?"
    # No input prompt near the bottom: no guess.
    assert asking.screen_reply("Working… (esc to interrupt)\nthinking\n") is None


# --------------------------------------------------------------------------- #
# state                                                                        #
# --------------------------------------------------------------------------- #
def test_state_detect_input_dismiss_and_a_newer_reply():
    inst = _Inst("t")
    t0 = time.time() - 30
    _reply("t", "Done.\n\nShould I push?", t0)
    got = asking.current("t", inst, "idle")
    assert got == {
        "kind": "question",
        "text": "Should I push?",
        "ts": t0,
        "dialog_id": None,
        "parsed": False,
        "source": "hook",
    }
    # Not idle: nothing to show (a working session is not asking).
    assert asking.current("t", inst, "working") is None
    # Somebody typed into it: answered.
    asking.note_input("t")
    assert asking.current("t", inst, "idle") is None
    # A newer reply that asks re-arms…
    _reply("t", "Pushed.\n\nOpen a PR as well?", time.time() + 1)
    assert asking.current("t", inst, "idle")["text"] == "Open a PR as well?"
    # …dismissing suppresses that reply only…
    assert asking.dismiss("t") is True
    assert asking.current("t", inst, "idle") is None
    assert asking.dismiss("t") is False
    # …and the next one asks again.
    _reply("t", "Opened.\n\nMerge it?", time.time() + 2)
    assert asking.current("t", inst, "idle")["text"] == "Merge it?"
    # A reply that asks nothing clears it.
    _reply("t", "Merged.", time.time() + 3)
    assert asking.current("t", inst, "idle") is None


def test_a_queued_prompt_going_in_answers_it(monkeypatch):
    inst = _Inst("t")
    _reply("t", "Should I push?", time.time() - 5)
    assert asking.current("t", inst, "idle") is not None
    asking._on_event({"event": "session.prompt_sent", "session": "t"})
    assert asking.current("t", inst, "idle") is None


def test_human_input_through_state_events_answers_it(monkeypatch):
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {})
    inst = _Inst("t")
    _reply("t", "Should I push?", time.time() - 5)
    assert asking.current("t", inst, "idle") is not None
    state_events._note_human_input("t")
    assert asking.current("t", inst, "idle") is None


def test_dialog_kind_reads_the_probes_cached_screen():
    inst = _Inst("t", program="aider")
    # No fresh capture: the null-text shape (the client fetches /dialog).
    got = asking.current("t", inst, "clarify")
    assert got["kind"] == "dialog" and got["text"] is None and got["dialog_id"] is None
    agent_state.note_screen("t", "Some CLI\nOverwrite config.yaml? [y/N]\n")
    got = asking.current("t", inst, "clarify")
    assert got["kind"] == "dialog"
    assert got["text"] == "Overwrite config.yaml? [y/N]"
    assert got["parsed"] is False and got["dialog_id"]
    # A stale capture is not used.
    agent_state._LAST_SCREEN["t"] = (time.time() - 60, "x")
    assert asking.current("t", inst, "clarify")["text"] is None


def test_the_screen_fallback_for_a_cli_with_no_transcript():
    inst = _Inst("t", program="aider")
    agent_state.note_screen("t", "⏺ Renamed it.\n  Shall I commit?\n\n> \n")
    got = asking.current("t", inst, "idle")
    assert got["text"] == "Shall I commit?" and got["source"] == "screen"


def test_seed_then_announce_once_per_reply():
    inst = _Inst("t")
    _reply("t", "Should I push?", time.time() - 50)
    asking.seed("t", inst)
    # What it asked before we first saw it is old news (a restart).
    assert asking.announce("t", inst) is None
    ts = time.time() + 1
    _reply("t", "Done.\n\nMerge it?", ts)
    got = asking.announce("t", inst)
    assert got == {"title": "t", "text": "Merge it?", "ts": ts, "kind": "question"}
    assert asking.announce("t", inst) is None  # once per reply
    # Never sighted before its turn end: provenance unknown, quiet.
    _reply("u", "Should I?", time.time())
    assert asking.announce("u", _Inst("u")) is None


# --------------------------------------------------------------------------- #
# the row, /dialog, /answer, /asking/dismiss                                   #
# --------------------------------------------------------------------------- #
def test_row_field_is_a_probe_and_carried_on_the_cheap_path():
    assert "asking" in server._SNAPSHOT_PROBE_KEYS
    inst = _Inst("t")
    _reply("t", "Should I push?", time.time() - 5)
    got = server._session_asking_cached(inst, "idle")
    assert got["kind"] == "question" and got["text"] == "Should I push?"


class _Tmux:
    def __init__(self, screen):
        self.screen = screen

    def __call__(self, args, *, timeout, **kw):
        sub = args[1]
        if sub == "has-session":
            return subprocess.CompletedProcess(args, 0)
        if sub == "capture-pane":
            return subprocess.CompletedProcess(args, 0, self.screen, "")
        if sub in ("send-keys", "list-clients"):
            return subprocess.CompletedProcess(args, 0, b"", b"")
        raise AssertionError("unexpected tmux call %r" % (args,))


@pytest.fixture
def env(monkeypatch):
    instances = {"t": _Inst("t")}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    tm = _Tmux("Done.\n\n> \n")
    monkeypatch.setattr(server, "_run_capped", tm)
    monkeypatch.setattr(proc, "run_capped", tm)
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    state = SimpleNamespace(activity="idle")
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: state.activity)
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: state.activity)
    return SimpleNamespace(tm=tm, state=state)


def test_dialog_route_serves_a_prose_question(env):
    ts = time.time() - 5
    _reply("t", "Renamed it.\n\nShould I update the docs too?", ts)
    r = client.get("/api/instances/t/dialog")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": asking.question_id(ts),
        "kind": "question",
        "parsed": False,
        "question": "Should I update the docs too?",
        "options": [],
        "ts": ts,
    }
    assert r.json()["id"].startswith("q:")
    # Answered (dismissed): back to "not waiting".
    r = client.post("/api/instances/t/asking/dismiss", json={})
    assert r.status_code == 200 and r.json() == {"ok": True, "dismissed": True}
    assert client.get("/api/instances/t/dialog?quiet=1").status_code == 204
    assert client.get("/api/instances/t/dialog").status_code == 409
    # Dismissing again is still ok; an unknown session is 404.
    assert client.post("/api/instances/t/asking/dismiss", json={}).json() == {
        "ok": True,
        "dismissed": False,
    }
    assert client.post("/api/instances/nope/asking/dismiss", json={}).status_code == 404


def test_dialog_route_marks_a_dialog_kind(env):
    env.state.activity = "clarify"
    env.tm.screen = "Some CLI\nOverwrite config.yaml? [y/N]\n"
    body = client.get("/api/instances/t/dialog").json()
    assert body["kind"] == "dialog" and body["parsed"] is False


def test_answer_on_a_question_id_is_not_a_dialog(env):
    r = client.post(
        "/api/instances/t/answer", json={"keys": ["Enter"], "dialog_id": "q:12.000"}
    )
    assert r.status_code == 409
    assert r.json() == {"error": "not a dialog", "no_dialog": True}


# --------------------------------------------------------------------------- #
# outbox                                                                       #
# --------------------------------------------------------------------------- #
def test_outbox_lists_a_prose_question_with_the_answers():
    now = 200_000.0

    def row(title, **kw):
        base = {"title": title, "repo": "r", "branch": "b/" + title}
        base.update(kw)
        return base

    rows = [
        row(
            "asks",
            activity="idle",
            asking={"kind": "question", "text": "Merge it?", "ts": now - 5},
        ),
        row("dialog", activity="clarify", activity_since=now - 1),
        # Dismissed / answered: no asking, no row.
        row("quiet", activity="idle", asking=None),
    ]
    out = outbox.build(rows, [], {}, now=now, today_start=now - 3600)
    waiting = out["groups"]["waiting"]
    assert [(w["title"], w["kind"]) for w in waiting] == [
        ("asks", "question"),
        ("dialog", "prompt"),
    ]
    q = waiting[0]
    assert q["reason"] == "its agent asked a question"
    assert q["text"] == "Merge it?"
    assert q["since"] == now - 5
    assert q["actions"] == outbox.WAITING_ACTIONS["question"]


# --------------------------------------------------------------------------- #
# session.question                                                             #
# --------------------------------------------------------------------------- #
@pytest.fixture
def bus(monkeypatch):
    b = EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    state_events._REPLY_SEEN.clear()
    state_events._REPLY_WORK.clear()
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(server, "_tmux_client_input_recent", lambda t, w: False)
    monkeypatch.setattr(server.ENGINE, "instances", {"t": _Inst("t")})
    got: list = []
    b.subscribe(got.append)
    yield got
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    state_events._REPLY_SEEN.clear()
    state_events._REPLY_WORK.clear()


def _tick(activity):
    server._emit_state_changes("t", "running", activity, "agent")


def _questions(got):
    return [e for e in got if e["event"] == "session.question"]


def test_session_question_fires_once_at_the_turn_end(bus):
    _tick("working")  # first sighting: seeds (no reply yet)
    ts = time.time() + 1
    _reply("t", "Ran it.\n\nSay: Tests pass. Should I merge?", ts)
    _tick("idle")
    _tick("idle")
    qs = _questions(bus)
    assert len(qs) == 1
    assert qs[0]["session"] == "t"
    assert qs[0]["data"] == {
        "title": "t",
        "text": "Should I merge?",
        "ts": ts,
        "kind": "question",
        "say": "Tests pass. Should I merge?",
    }
    idle = [e for e in bus if e["event"] == "session.activity_changed"]
    assert idle[-1]["new"] == "idle"
    assert idle[-1]["data"]["question"] == "Should I merge?"
    # More idle passes, the same reply: never again.
    _tick("idle")
    _tick("idle")
    assert len(_questions(bus)) == 1


def test_session_question_on_the_missed_turn_path(bus):
    _reply("t", "Say: First answer.", time.time() - 60)
    _tick("working")
    _tick("idle")
    _tick("idle")
    assert _questions(bus) == []
    # A 2-3 s turn the poll never saw working, ending on a question.
    ts = time.time() + 5
    _reply("t", "Hello!\n\nSay: Hello! How is your day going?", ts)
    _tick("idle")
    qs = _questions(bus)
    assert len(qs) == 1 and qs[0]["data"]["text"] == "How is your day going?"
    _tick("idle")
    assert len(_questions(bus)) == 1


def test_a_restart_does_not_reannounce(bus):
    _reply("t", "Should I push?", time.time() - 100)
    _tick("idle")  # first sighting: seeded
    _tick("idle")
    _tick("idle")
    assert _questions(bus) == []


def test_turn_ended_carries_the_question(bus, monkeypatch):
    monkeypatch.setattr(server._agent_state, "claim_work", lambda t: 5.0)
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: 1.0)
    monkeypatch.setattr(server._agent_state, "work_evidence", lambda t: "marker")
    monkeypatch.setattr(
        server._agent_state, "reading_is_authoritative", lambda t, a: True
    )
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {})
    monkeypatch.setattr(state_events, "_TURN_END_DWELLS", {"marker": 0.0})
    _tick("working")
    _reply("t", "Done.\n\nWant a PR?", time.time() + 1)
    _tick("idle")
    _tick("idle")
    ended = [e for e in bus if e["event"] == "session.turn_ended"]
    assert ended and ended[-1]["data"]["question"] == "Want a PR?"


# --------------------------------------------------------------------------- #
# the needs_input push                                                         #
# --------------------------------------------------------------------------- #
_PHONE = "http://box.tail.net:8765/m"


@pytest.fixture
def pushes(monkeypatch, isolate_settings_store):
    sent: list = []
    monkeypatch.setattr(
        notify_addon.ntfy, "publish_soon", lambda cfg, **kw: sent.append(kw)
    )
    voice_actions._reset_for_tests()
    mobile_announce.remember_url(_PHONE)
    S.update_settings(
        notifications={
            "ntfy_enabled": True,
            "ntfy_topic": "t1",
            "ntfy_server": "https://ntfy.example.test",
        }
    )
    yield sent
    mobile_announce.remember_url(None)
    voice_actions._reset_for_tests()


def _question_event(session="alpha", text="Should I push?", ts=100.0):
    return {
        "seq": 3,
        "event": "session.question",
        "session": session,
        "old": None,
        "new": None,
        "ts": 0.0,
        "data": {"title": session, "text": text, "ts": ts, "kind": "question"},
    }


def _clarify(session="alpha"):
    return {
        "seq": 1,
        "event": "session.activity_changed",
        "session": session,
        "old": "working",
        "new": "clarify",
        "ts": 0.0,
        "data": {},
    }


def _turn_ended(session="alpha", **data):
    return {
        "seq": 2,
        "event": "session.turn_ended",
        "session": session,
        "old": None,
        "new": None,
        "ts": 0.0,
        "data": {"idle_for": 46.0, **data},
    }


def test_needs_input_rule_matches_a_prose_question():
    rule = next(r for r in notify_addon.NOTIFY_RULES if r["id"] == "needs_input")
    assert notify_addon._matches(rule, _question_event())
    assert notify_addon._matches(rule, _clarify())
    assert not notify_addon._matches(rule, _turn_ended())
    # Every alternative names a real event (the "also" twin of the vocabulary
    # check — the bus must carry it for the rule to ever fire).
    for shape in rule["also"]:
        assert shape["event"] == "session.question"


def test_question_push_says_the_question(pushes):
    notify_addon.NotifyAddon()._on_event(_question_event())
    (push,) = pushes
    assert push["title"] == "alpha needs you: Question"
    assert push["message"] == "Should I push?"
    assert push["click"] == _PHONE + "?s=alpha&voice=1"
    assert push["priority"] == 4
    assert push.get("actions") is None  # a prose question has no one-tap answer


def test_question_push_without_agent_words_is_generic(pushes):
    S.update_settings(notifications={"ntfy_agent_words": False})
    notify_addon.NotifyAddon()._on_event(_question_event())
    (push,) = pushes
    assert push["message"].startswith("The agent is waiting on a clarification.")
    assert "Should I push?" not in push["message"]


def test_a_finished_turn_that_asks_is_one_push(pushes):
    addon = notify_addon.NotifyAddon()
    addon._on_event(_question_event())
    addon._on_event(_turn_ended(say="Done. Should I push?", question="Should I push?"))
    assert [p["title"] for p in pushes] == ["alpha needs you: Question"]
    # A finished turn that asks nothing still says "has finished".
    addon._on_event(_turn_ended(session="beta", say="Done.", question=None))
    assert pushes[-1]["title"] == "beta has finished"


def test_needs_input_dedupes_on_what_is_asked(pushes, monkeypatch):
    addon = notify_addon.NotifyAddon()
    seen = {"id": "d1"}
    monkeypatch.setattr(
        notify_addon,
        "_read_dialog",
        lambda title: {
            "id": seen["id"],
            "parsed": True,
            "question": "Run it?",
            "command": None,
            "options": [{"key": "1", "label": "Yes", "kind": "yes"}],
        },
    )
    addon._on_event(_clarify())
    addon._on_event(_clarify())  # the same dialog re-read: no second push
    assert len(pushes) == 1
    seen["id"] = "d2"
    addon._on_event(_clarify())  # a DIFFERENT dialog seconds later: pushes
    assert len(pushes) == 2
    # Questions dedupe on their reply ts.
    addon._on_event(_question_event(ts=5.0))
    addon._on_event(_question_event(ts=5.0))
    addon._on_event(_question_event(ts=6.0, text="And merge?"))
    assert [p["message"] for p in pushes[2:]] == ["Should I push?", "And merge?"]


def test_an_unparsed_dialog_push_says_its_question_line(pushes, monkeypatch):
    monkeypatch.setattr(notify_addon, "_read_dialog", lambda title: None)
    monkeypatch.setattr(
        notify_addon,
        "_read_unparsed_dialog",
        lambda title: {
            "id": "u1",
            "parsed": False,
            "question": "Overwrite config.yaml? [y/N]",
            "command": None,
            "options": [],
        },
    )
    notify_addon.NotifyAddon()._on_event(_clarify())
    (push,) = pushes
    assert push["message"] == "Overwrite config.yaml? [y/N]"
    assert push.get("actions") is None


# --------------------------------------------------------------------------- #
# voice router                                                                 #
# --------------------------------------------------------------------------- #
def test_voice_router_reads_a_prose_question():
    row = {
        "title": "api",
        "activity": "idle",
        "asking": {"kind": "question", "text": "Should I push?", "ts": 1.0},
    }
    ss = voice_router.session_state(row)
    assert ss["label"] == "Question" and ss["in_bell"] is True
    needs = voice_router.answer("what needs me", "", [row])
    assert "api is asking: Should I push?" in needs["say"]
    dialog = {
        "id": "q:1.000",
        "kind": "question",
        "parsed": False,
        "question": "Should I push?",
        "options": [],
        "ts": 1.0,
    }
    q = voice_router.finish({"kind": "question", "title": "api", "name": "api"}, dialog)
    assert q["say"] == "api: Should I push?"
    # A targeted answer: it was asked in words — say so, don't fail.
    a = voice_router.finish(
        {
            "kind": "answer",
            "title": "api",
            "name": "api",
            "choice": "yes",
            "words": "yes",
        },
        dialog,
    )
    assert a["no_dialog"] is True and "asked in words" in a["say"]
    # A bare one is a prompt.
    bare = voice_router.finish(
        {"kind": "answer", "title": "api", "name": "api", "bare": True, "words": "yes"},
        dialog,
    )
    assert bare == {"kind": "prompt"}


# --------------------------------------------------------------------------- #
# Codex: the newest assistant message of THIS window's rollout                 #
# --------------------------------------------------------------------------- #
def test_codex_last_assistant_text_reads_the_windows_rollout(monkeypatch, tmp_path):
    day = tmp_path / "sessions" / "2026" / "10" / "26"
    day.mkdir(parents=True)
    for f in DATA.iterdir():
        shutil.copy(f, day / f.name)
    monkeypatch.setattr(codex_usage_api, "_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(codex_usage_api, "_codex_homes", lambda: [])
    codex_mod._ROLLOUT_PATHS.clear()
    codex_mod._LAST_ASSISTANT_CACHE.clear()
    marks = {"mindflock_cx": TID}
    monkeypatch.setattr(thread_markers, "read", lambda name: marks.get(name, ""))
    from backend import providers

    prov = providers.resolve("codex")
    text = prov.last_assistant_text("mindflock_cx", "/tmp/wt")
    assert text == (
        "Renamed `old_helper` to `parse_row` in three files.\n\n"
        "Should I also update the docs?"
    )
    assert asking.question_in(text) == "Should I also update the docs?"
    assert prov.last_assistant_text("mindflock_cx", "/tmp/wt", contains="look at") == (
        "I'll look at the helper first."
    )
    # No thread bound to the window: never a sibling's newest rollout.
    assert prov.last_assistant_text("mindflock_other", "/tmp/wt") is None


# --------------------------------------------------------------------------- #
# the ask_popup setting                                                        #
# --------------------------------------------------------------------------- #
def test_ask_popup_setting_roundtrip_and_views(isolate_settings_store):
    assert S.NotificationSettings().ask_popup is True
    assert "ask_popup" not in S.NotificationSettings().to_dict()
    off = S.NotificationSettings(ask_popup=False)
    assert off.to_dict() == {"ask_popup": False}
    assert S.NotificationSettings.from_dict(off.to_dict()).ask_popup is False
    # The Notifications screen reads and writes it through /api/notify/ntfy.
    assert client.get("/api/notify/ntfy").json()["ask_popup"] is True
    r = client.post("/api/notify/ntfy", json={"ask_popup": False})
    assert r.status_code == 200 and r.json()["ask_popup"] is False
    assert S.load_settings().notifications.ask_popup is False
    # The phone page reads it off GET /api/voice.
    assert client.get("/api/voice").json()["ask_popup"] is False
