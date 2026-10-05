"""``GET /api/instances/{title}/dialog`` and ``/answer``'s ``dialog_id`` / ``by``.

The UI's in-place answer buttons read the dialog an agent is blocked on as
data, then post the chosen key with the dialog's id: a click meant for one
prompt must never answer the next. tmux is stubbed at the server seam
(``_run_capped``) with a screen the test controls; the providers are the
real ones, so the golden screens go through their own parsers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.providers import dialogs
from backend.web import server
from backend.web.core import agent_io
from backend.web.server import app

client = TestClient(app)
DATA = Path(__file__).parent / "data" / "dialogs"


def _screen(name: str) -> str:
    return (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")


class _Inst:
    def __init__(self, title, program="claude"):
        self.Title = title
        self.Program = program

    def GetWorktreePath(self):  # noqa: N802
        return "/tmp/wt"


class _Tmux:
    def __init__(self):
        self.calls: list = []
        self.alive = True
        self.screen = _screen("claude_bash")
        self.capture_rc = 0

    def __call__(self, args, *, timeout, **kw):
        self.calls.append(list(args))
        sub = args[1]
        if sub == "has-session":
            return subprocess.CompletedProcess(args, 0 if self.alive else 1)
        if sub == "capture-pane":
            assert "-S" not in args  # the visible screen only
            return subprocess.CompletedProcess(
                args, self.capture_rc, self.screen, "boom" if self.capture_rc else ""
            )
        if sub == "send-keys":
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError("unexpected tmux call %r" % (args,))

    def sends(self):
        return [c[4:] for c in self.calls if c[1] == "send-keys"]


@pytest.fixture
def env(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    tm = _Tmux()
    monkeypatch.setattr(server, "_run_capped", tm)
    monkeypatch.setattr(server, "_live_session_name", lambda n: n if tm.alive else None)
    monkeypatch.setattr(agent_io.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    state = SimpleNamespace(activity="clarify", cached="idle")
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: state.activity)
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: state.cached)
    human: list = []
    monkeypatch.setattr(server, "_note_human_input", human.append)
    instances["w"] = _Inst("w")
    instances["cx"] = _Inst("cx", program="codex")
    instances["aid"] = _Inst("aid", program="aider")
    return SimpleNamespace(tm=tm, state=state, human=human)


def _dialog(title="w"):
    return client.get("/api/instances/%s/dialog" % title)


# --------------------------------------------------------------------------- #
# GET /dialog                                                                  #
# --------------------------------------------------------------------------- #
def test_dialog_parsed_claude_bash(env):
    r = _dialog()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parsed"] is True
    assert body["question"] == (
        "Bash command — Add redis as a dependency. Do you want to proceed?"
    )
    assert body["command"] == "uv add redis"
    assert [(o["key"], o["kind"]) for o in body["options"]] == [
        ("1", "yes"),
        ("2", "always"),
        ("3", "no"),
    ]
    screen = _screen("claude_bash")
    want = dialogs.dialog_id(dialogs.parse_claude(screen), screen)
    assert body["id"] == want
    assert set(body) == {"id", "parsed", "question", "command", "options"}


def test_dialog_codex_uses_the_codex_parser(env):
    env.tm.screen = _screen("codex_exec")
    body = _dialog("cx").json()
    assert body["parsed"] is True and body["command"] == "uv add redis"
    assert body["options"][0]["label"] == "Yes, just this once"


def test_dialog_unparsed_falls_back_to_the_question_line(env):
    env.tm.screen = "Some CLI\nOverwrite config.yaml? [y/N]\n"
    body = _dialog("aid").json()
    assert body == {
        "id": dialogs.dialog_id(None, env.tm.screen),
        "parsed": False,
        "question": "Overwrite config.yaml? [y/N]",
        "command": None,
        "options": [],
    }


def test_dialog_parser_crash_reads_as_unparsed(env, monkeypatch):
    from backend.providers.claude import ClaudeProvider

    def boom(self, screen):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(ClaudeProvider, "parse_dialog", boom)
    body = _dialog().json()
    assert body["parsed"] is False and body["question"] == "Do you want to proceed?"


#: A Claude screen mid-turn: no dialog anywhere on it.
_WORKING = (DATA / "claude2_working.screen.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("activity", ["limit", "offline"])
def test_dialog_409_on_limit_or_offline(env, activity):
    env.state.activity = activity
    r = _dialog()
    assert r.status_code == 409
    assert r.json() == {
        "error": "session is not waiting on a prompt (activity: %s)" % activity
    }
    assert not [c for c in env.tm.calls if c[1] == "capture-pane"]


@pytest.mark.parametrize("activity", ["idle", "working"])
def test_dialog_409_when_neither_clarify_nor_a_dialog_on_screen(env, activity):
    env.state.activity = activity
    env.tm.screen = _WORKING
    r = _dialog()
    assert r.status_code == 409
    assert r.json() == {
        "error": "session is not waiting on a prompt (activity: %s)" % activity
    }


@pytest.mark.parametrize("activity", ["idle", "working", "unknown"])
def test_dialog_on_screen_is_served_whatever_the_reading(env, activity, monkeypatch):
    """E2E defect A: a background sub-agent's permission prompt read as
    ``working`` (its siblings' tool events rewrote the hook marker) and
    /dialog answered 409 while the dialog sat on screen. A dialog the
    provider PARSES is screen evidence, and it beats the reading."""
    if activity == "unknown":

        def boom(i, t):
            raise RuntimeError("probe")

        monkeypatch.setattr(server, "_agent_activity", boom)
    else:
        env.state.activity = activity
    env.tm.screen = _screen("claude2_tool_use_tab")
    r = _dialog()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parsed"] is True
    assert body["command"] == "hello-worker · mindflock — Spawn worker session"
    assert body["source"] == "general-purpose agent"
    assert "from the" not in body["question"]


def test_dialog_uses_the_uncached_activity(env):
    env.state.cached = "clarify"
    env.state.activity = "idle"
    env.tm.screen = _WORKING
    assert _dialog().status_code == 409


def test_dialog_activity_probe_failure_is_409(env, monkeypatch):
    def boom(i, t):
        raise RuntimeError("probe")

    monkeypatch.setattr(server, "_agent_activity", boom)
    env.tm.screen = _WORKING
    r = _dialog()
    assert r.status_code == 409 and "unknown" in r.json()["error"]


def test_dialog_errors(env):
    assert _dialog("ghost").status_code == 404
    env.tm.capture_rc = 1
    r = _dialog()
    assert r.status_code == 500 and r.json()["error"] == "boom"
    env.tm.alive = False
    r = _dialog()
    assert r.status_code == 409 and r.json()["error"] == "no live session"


# --------------------------------------------------------------------------- #
# POST /answer dialog_id                                                       #
# --------------------------------------------------------------------------- #
def _answer(body, title="w"):
    return client.post("/api/instances/%s/answer" % title, json=body)


def test_answer_with_the_current_dialog_id_presses_the_key(env):
    did = _dialog().json()["id"]
    r = _answer({"keys": ["1"], "dialog_id": did})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "activity_before": "clarify"}
    assert env.tm.sends() == [["1"]]


def test_answer_after_the_prompt_changed_is_409_and_types_nothing(env):
    did = _dialog().json()["id"]
    # The prompt was answered meanwhile; the NEXT one is up.
    env.tm.screen = _screen("claude_edit_file")
    r = _answer({"keys": ["2"], "dialog_id": did})
    assert r.status_code == 409
    assert r.json() == {"error": "the prompt changed", "dialog_changed": True}
    assert env.tm.sends() == []


def test_answer_survives_a_moved_cursor(env):
    did = _dialog().json()["id"]
    env.tm.screen = env.tm.screen.replace(" ❯ 1. Yes", "   1. Yes").replace(
        "   2. Yes, and", " ❯ 2. Yes, and"
    )
    assert _answer({"keys": ["2"], "dialog_id": did}).status_code == 200


def test_answer_unreadable_screen_with_a_dialog_id_is_409(env):
    did = _dialog().json()["id"]
    env.tm.capture_rc = 1
    r = _answer({"keys": ["1"], "dialog_id": did})
    assert r.status_code == 409 and r.json()["dialog_changed"] is True
    assert env.tm.sends() == []


def test_answer_without_a_dialog_id_skips_the_check(env):
    env.tm.screen = _screen("claude_edit_file")
    assert _answer({"keys": ["1"]}).status_code == 200
    assert not [c for c in env.tm.calls if c[1] == "capture-pane"]


def test_answer_dialog_id_on_a_limit_menu(env):
    env.state.activity = "limit"
    env.tm.screen = "You've hit your limit\n ❯ 1. Stop and wait\n   2. Switch\n"
    did = dialogs.dialog_id(None, env.tm.screen)
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 200


@pytest.mark.parametrize(
    "body,msg",
    [
        ({"keys": ["1"], "dialog_id": 5}, "dialog_id must be a non-empty string"),
        ({"keys": ["1"], "dialog_id": " "}, "dialog_id must be a non-empty string"),
        ({"keys": ["1"], "dialog_id": "x" * 65}, "dialog_id is too long"),
        ({"keys": ["1"], "by": "robot"}, "by must be one of: agent, user"),
    ],
)
def test_answer_guard_validation(env, body, msg):
    r = _answer(body)
    assert r.status_code == 400 and r.json()["error"] == msg
    assert env.tm.sends() == []


# --------------------------------------------------------------------------- #
# POST /answer by                                                              #
# --------------------------------------------------------------------------- #
def test_answer_by_user_counts_as_human_presence(env):
    assert _answer({"keys": ["1"], "by": "user"}).status_code == 200
    assert env.human == ["w"]


@pytest.mark.parametrize("body", [{"keys": ["1"]}, {"keys": ["1"], "by": "agent"}])
def test_answer_by_agent_is_not_presence(env, body):
    assert _answer(body).status_code == 200
    assert env.human == []


def test_refused_user_answer_is_not_presence(env):
    did = _dialog().json()["id"]
    env.tm.screen = _screen("claude_trust_folder")
    assert _answer({"keys": ["1"], "dialog_id": did, "by": "user"}).status_code == 409
    env.state.activity = "idle"
    env.tm.screen = _WORKING
    assert _answer({"keys": ["1"], "by": "user"}).status_code == 409
    assert env.human == []


def test_parse_answer_meta_defaults():
    assert agent_io.parse_answer_meta({}) == (None, "agent", None)
    assert agent_io.parse_answer_meta(None) == (None, "agent", None)
    assert agent_io.parse_answer_meta({"dialog_id": " ab12 ", "by": "user"}) == (
        "ab12",
        "user",
        None,
    )


# --------------------------------------------------------------------------- #
# One answer per dialog (review 2026-10-05)                                    #
# --------------------------------------------------------------------------- #
def test_a_second_click_on_the_same_dialog_is_refused(env):
    """Two posts with the same dialog_id (a double click, the rail strip and
    the bell): the CLI has not redrawn yet, so the dialog check alone passes
    both — the second must be refused, not typed into the NEXT prompt."""
    did = _dialog().json()["id"]
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 200
    r = _answer({"keys": ["1"], "dialog_id": did})
    assert r.status_code == 409
    assert r.json() == {
        "error": "that prompt was just answered",
        "dialog_answered": True,
    }
    assert env.tm.sends() == [["1"]]


def test_navigation_keys_do_not_count_as_an_answer(env):
    did = _dialog().json()["id"]
    assert _answer({"keys": ["Down"], "dialog_id": did}).status_code == 200
    assert _answer({"keys": ["Enter"], "dialog_id": did}).status_code == 200
    assert _answer({"keys": ["2"], "dialog_id": did}).status_code == 409
    assert env.tm.sends() == [["Down"], ["Enter"]]


def test_the_same_prompt_asked_again_is_answerable(env, monkeypatch):
    """An identical prompt (the same command again) has the same id: it is
    answerable once another dialog was seen in between, or once the hold
    has passed — the UI's strip releases its latch on the same clock."""
    now = [1000.0]
    monkeypatch.setattr(agent_io.time, "monotonic", lambda: now[0])
    did = _dialog().json()["id"]
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 200
    env.tm.screen = _screen("claude_edit_file")
    _dialog()  # a different dialog was up in between
    env.tm.screen = _screen("claude_bash")
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 200
    # ...and by the clock alone.
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 409
    now[0] += agent_io.ANSWERED_HOLD_S - 0.1
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 409
    now[0] += 0.2
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 200
    assert env.tm.sends() == [["1"]] * 3


def test_concurrent_answers_to_one_dialog_type_once(env, monkeypatch):
    """Both requests read the screen before either sends: the per-session
    lock must make the second one see the first one's answer."""
    import threading

    did = _dialog().json()["id"]
    real = agent_io.current_dialog
    gate = threading.Barrier(2, timeout=0.5)

    def slow(name, program):
        try:
            gate.wait()  # only passes if both are inside the read at once
        except threading.BrokenBarrierError:
            pass
        return real(name, program)

    monkeypatch.setattr(agent_io, "current_dialog", slow)
    out: list = []

    def post():
        out.append(_answer({"keys": ["1"], "dialog_id": did}).status_code)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == [200, 409]
    assert env.tm.sends() == [["1"]]


# --------------------------------------------------------------------------- #
# Screen evidence beats the activity reading (E2E defect A, 2026-10-05)        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("activity", ["working", "idle"])
def test_a_click_on_a_visible_dialog_is_never_refused_for_its_reading(env, activity):
    """Twice in the live run a click on a strip already showing was refused
    with "not waiting on a prompt (activity: working)" and then "(idle)"
    while the dialog was on screen."""
    env.tm.screen = _screen("claude2_tool_use_tab")
    did = _dialog().json()["id"]
    env.state.activity = activity
    r = _answer({"keys": ["1"], "dialog_id": did, "by": "user"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "activity_before": "clarify"}
    assert env.tm.sends() == [["1"]]
    # Once per screen read: the evidence and the id check share a capture.
    assert len([c for c in env.tm.calls if c[1] == "capture-pane"]) == 2


@pytest.mark.parametrize("activity", ["working", "idle"])
def test_no_dialog_on_screen_still_refuses_a_working_or_idle_agent(env, activity):
    env.tm.screen = _WORKING
    env.state.activity = activity
    r = _answer({"keys": ["1"]})
    assert r.status_code == 409
    assert "not waiting on a prompt (activity: %s)" % activity in r.json()["error"]
    assert env.tm.sends() == []


def test_an_offline_session_is_never_answered_from_its_screen(env):
    env.tm.screen = _screen("claude2_tool_use_tab")
    env.state.activity = "offline"
    assert _answer({"keys": ["1"]}).status_code == 409
    assert env.tm.sends() == []


def test_a_resize_between_fetch_and_click_keeps_the_dialog_id(env):
    """E2E defect C: the strip fetched the dialog at 79 columns, the page
    opening reflowed the pane to 164, and the click was refused as "the
    prompt changed" for the very same prompt."""
    env.tm.screen = _screen("claude2_create_file_narrow")
    did = _dialog().json()["id"]
    env.tm.screen = _screen("claude2_create_file_wide")
    assert _dialog().json()["id"] == did
    r = _answer({"keys": ["1"], "dialog_id": did, "by": "user"})
    assert r.status_code == 200, r.text
    assert env.tm.sends() == [["1"]]


@pytest.mark.parametrize(
    "fetched,clicked",
    [
        ("claude2_stop_session_w100", "claude2_stop_session_w164"),
        ("claude2_stop_session_w164", "claude2_stop_session_w100"),
        ("claude2_spawn_w164", "claude2_spawn_w100"),
        ("claude2_spawn_w62", "claude2_spawn_w79"),
    ],
)
def test_a_cut_option_survives_the_resize_between_fetch_and_click(
    env, fetched, clicked
):
    """Second live run: option 2 ("don't ask again … in ~/.mindflock/work…")
    is cut to the width, which used to drop the question and the options
    from the id — 624c4ddd433a at 100 columns, 740f2d6ad10f at 164."""
    env.tm.screen = _screen(fetched)
    did = _dialog().json()["id"]
    env.tm.screen = _screen(clicked)
    r = _answer({"keys": ["1"], "dialog_id": did, "by": "user"})
    assert r.status_code == 200, r.text
    assert env.tm.sends() == [["1"]]


def test_the_just_answered_guard_holds_across_a_resize(env):
    env.tm.screen = _screen("claude2_stop_session_w100")
    did = _dialog().json()["id"]
    assert _answer({"keys": ["1"], "dialog_id": did}).status_code == 200
    # The CLI hasn't redrawn yet, but the pane was resized meanwhile.
    env.tm.screen = _screen("claude2_stop_session_w164")
    r = _answer({"keys": ["1"], "dialog_id": did})
    assert r.status_code == 409 and r.json()["dialog_answered"] is True
    assert env.tm.sends() == [["1"]]


def test_the_narrow_two_option_variant_refuses_a_wide_click(env):
    """ "2" is "Yes, and don't ask again" at 100 columns and "No" in the
    2-option variant Claude draws at 80: never press a key across them."""
    env.tm.screen = _screen("claude2_stop_session_w100")
    did = _dialog().json()["id"]
    env.tm.screen = _screen("claude2_stop_session_w80")
    r = _answer({"keys": ["2"], "dialog_id": did, "by": "user"})
    assert r.status_code == 409 and r.json()["dialog_changed"] is True
    assert env.tm.sends() == []


# --------------------------------------------------------------------------- #
# ?quiet=1: "not waiting" is no dialog, not an error (live run, 2026-10-05)    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("activity", ["idle", "working", "limit", "offline"])
def test_quiet_not_waiting_is_204_without_a_body(env, activity):
    """The UI's strips fetched /dialog right after an answer, while their row
    still read clarify: ~8 409s per run, each one a console error."""
    env.state.activity = activity
    env.tm.screen = _WORKING
    r = client.get("/api/instances/w/dialog?quiet=1")
    assert r.status_code == 204 and r.content == b""


def test_quiet_not_waiting_without_a_live_session_is_204(env):
    env.state.activity = "idle"
    env.tm.alive = False
    assert client.get("/api/instances/w/dialog?quiet=1").status_code == 204


def test_quiet_still_serves_a_dialog_and_still_reports_real_errors(env):
    r = client.get("/api/instances/w/dialog?quiet=1")
    assert r.status_code == 200 and r.json()["parsed"] is True
    assert client.get("/api/instances/ghost/dialog?quiet=1").status_code == 404
    env.tm.capture_rc = 1
    assert client.get("/api/instances/w/dialog?quiet=1").status_code == 500
    env.tm.alive = False  # clarify, but no pane: an oddity worth a 409
    r = client.get("/api/instances/w/dialog?quiet=1")
    assert r.status_code == 409 and r.json()["error"] == "no live session"
