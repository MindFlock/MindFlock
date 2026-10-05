"""``GET /api/instances/{title}/output`` and ``POST /api/instances/{title}/answer``.

The control surface an orchestrator agent reads and steers a worker through.
tmux and the providers are stubbed at the server seams (``_run_capped``,
``_live_session_name``, ``providers.resolve``, ``_agent_transcript_text``,
``_agent_activity``), so nothing here starts a real session.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import agent_io
from backend.web.server import app

client = TestClient(app)


class _Inst:
    def __init__(self, title, wt="/tmp/wt", program="claude"):
        self.Title = title
        self.Program = program
        self._wt = wt

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


class _Tmux:
    """A recording stand-in for ``server._run_capped`` (tmux only)."""

    def __init__(self):
        self.calls: list = []
        self.alive = True
        self.screen = "screen line\n\n"
        self.scrollback = "old\nscroll\n"
        self.fail_on: str = ""

    def __call__(self, args, *, timeout, **kw):
        self.calls.append(list(args))
        sub = args[1]
        if sub == "has-session":
            return subprocess.CompletedProcess(args, 0 if self.alive else 1)
        if sub == "capture-pane":
            if "-S" in args:
                return subprocess.CompletedProcess(args, 0, self.scrollback, "")
            return subprocess.CompletedProcess(args, 0, self.screen, "")
        if sub == "send-keys":
            rc = 1 if self.fail_on and self.fail_on in args else 0
            return subprocess.CompletedProcess(args, rc)
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
    state = SimpleNamespace(activity="clarify", reply=None, transcript="")
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: state.activity)
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: state.activity)
    monkeypatch.setattr(
        server, "_agent_transcript_text", lambda wt, name: state.transcript
    )

    class _Prov:
        def last_assistant_text(self, session_name, workdir, **kw):
            state.asked = (session_name, workdir)
            return state.reply

    monkeypatch.setattr(server.providers, "resolve", lambda prog: _Prov())
    human: list = []
    monkeypatch.setattr(server, "_note_human_input", human.append)
    instances["w"] = _Inst("w")
    return SimpleNamespace(tm=tm, state=state, instances=instances, human=human)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def test_tail_keeps_the_end():
    assert agent_io.tail("abcdef", 3) == ("def", True)
    assert agent_io.tail("abc", 3) == ("abc", False)
    assert agent_io.tail("", 3) == ("", False)
    assert agent_io.tail(None, 3) == ("", False)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, (6000, None)),
        ("", (6000, None)),
        ("10", (10, None)),
        ("999999", (50000, None)),
        ("0", (0, "max_chars must be positive")),
        ("-3", (0, "max_chars must be positive")),
        ("x", (0, "max_chars must be an integer")),
    ],
)
def test_parse_max_chars(raw, expected):
    assert agent_io.parse_max_chars(raw) == expected


def test_clean_answer_text_strips_every_control_char():
    assert agent_io.clean_answer_text("a\x1b[2Jb\nc\td\r\x7f\x9be") == "a[2Jbcde"
    assert agent_io.clean_answer_text("héllo – ok") == "héllo – ok"


def test_parse_answer():
    assert agent_io.parse_answer({"text": "yes"}) == ("yes", [], None)
    assert agent_io.parse_answer({"keys": ["Down", "Enter"]}) == (
        "",
        ["Down", "Enter"],
        None,
    )
    assert agent_io.parse_answer({"text": None, "keys": ["1"]})[2] is None
    assert "nothing to send" in agent_io.parse_answer({})[2]
    assert "nothing to send" in agent_io.parse_answer(None)[2]
    # Text that is only control characters is nothing.
    assert "nothing to send" in agent_io.parse_answer({"text": "\n\x1b"})[2]
    assert "string" in agent_io.parse_answer({"text": 5})[2]
    assert "too long" in agent_io.parse_answer({"text": "x" * 2001})[2]
    assert agent_io.parse_answer({"text": "x" * 2000})[2] is None
    assert "list" in agent_io.parse_answer({"keys": "Enter"})[2]
    assert "too many" in agent_io.parse_answer({"keys": ["y"] * 21})[2]
    for bad in ("C-c", "0", "Y", "BSpace", "enter", 1, None):
        err = agent_io.parse_answer({"keys": [bad]})[2]
        assert err and "key not allowed" in err, bad


def test_answer_key_allow_list_is_exact():
    assert agent_io.ANSWER_KEYS == frozenset(
        "Enter Escape Up Down Left Right Tab BTab Space 1 2 3 4 5 6 7 8 9 y n".split()
    )


# --------------------------------------------------------------------------- #
# /output                                                                      #
# --------------------------------------------------------------------------- #
def test_output_404_for_unknown(env):
    r = client.get("/api/instances/nope/output")
    assert r.status_code == 404


def test_output_bad_view_and_max_chars(env):
    r = client.get("/api/instances/w/output?view=pane")
    assert r.status_code == 400 and "view must be one of" in r.json()["error"]
    r = client.get("/api/instances/w/output?max_chars=abc")
    assert r.status_code == 400 and "integer" in r.json()["error"]


def test_output_last_reply(env):
    env.state.reply = "Done: added tests."
    env.state.activity = "idle"
    r = client.get("/api/instances/w/output")
    assert r.status_code == 200
    assert r.json() == {
        "view": "last_reply",
        "text": "Done: added tests.",
        "truncated": False,
        "activity": "idle",
    }
    # Asked for THIS window's transcript (tmux name + worktree).
    assert env.state.asked == ("mindflock_w", "/tmp/wt")


def test_output_last_reply_truncates_to_the_tail(env):
    env.state.reply = "x" * 100 + "THE END"
    r = client.get("/api/instances/w/output?view=last_reply&max_chars=7")
    body = r.json()
    assert body["text"] == "THE END" and body["truncated"] is True


def test_output_last_reply_falls_back_to_the_screen(env):
    env.state.reply = None
    r = client.get("/api/instances/w/output")
    body = r.json()
    assert body["view"] == "screen" and body["fallback"] is True
    assert body["text"] == "screen line\n"
    (cap,) = [c for c in env.tm.calls if c[1] == "capture-pane"]
    # Visible screen only: no -S scrollback range.
    assert cap == ["tmux", "capture-pane", "-p", "-J", "-t", "mindflock_w"]


def test_output_blank_reply_also_falls_back(env):
    env.state.reply = "   \n"
    assert client.get("/api/instances/w/output").json()["fallback"] is True


def test_output_fallback_without_tmux_is_409(env):
    env.state.reply = None
    env.tm.alive = False
    r = client.get("/api/instances/w/output")
    assert r.status_code == 409 and r.json()["error"] == "no live session"


def test_output_provider_error_reads_as_no_reply(env, monkeypatch):
    class _Boom:
        def last_assistant_text(self, *a, **k):
            raise RuntimeError("bad transcript")

    monkeypatch.setattr(server.providers, "resolve", lambda prog: _Boom())
    body = client.get("/api/instances/w/output").json()
    assert body["view"] == "screen" and body["fallback"] is True


def test_output_screen(env):
    r = client.get("/api/instances/w/output?view=screen")
    body = r.json()
    assert body == {
        "view": "screen",
        "text": "screen line\n",
        "truncated": False,
        "activity": "clarify",
    }


def test_output_screen_409_without_tmux(env):
    env.tm.alive = False
    r = client.get("/api/instances/w/output?view=screen")
    assert r.status_code == 409


def test_output_screen_capture_failure_is_500(env, monkeypatch):
    def _fail(args, *, timeout, **kw):
        return subprocess.CompletedProcess(args, 1, "", "no pane")

    monkeypatch.setattr(server, "_run_capped", _fail)
    r = client.get("/api/instances/w/output?view=screen")
    assert r.status_code == 500 and r.json()["error"] == "no pane"


def test_output_transcript_prefers_the_provider_transcript(env):
    env.state.transcript = "## User\nhi\n\n## Claude\nhello"
    body = client.get("/api/instances/w/output?view=transcript").json()
    assert body["view"] == "transcript" and body["text"].endswith("hello")
    assert not [c for c in env.tm.calls if c[1] == "capture-pane"]


def test_output_transcript_falls_back_to_scrollback(env):
    env.state.transcript = ""
    body = client.get("/api/instances/w/output?view=transcript").json()
    assert body["view"] == "transcript" and body["text"] == "old\nscroll\n"
    assert "fallback" not in body
    (cap,) = [c for c in env.tm.calls if c[1] == "capture-pane"]
    assert cap[-2:] == ["-S", "-"]


def test_output_transcript_without_anything_is_409(env):
    env.tm.alive = False
    r = client.get("/api/instances/w/output?view=transcript")
    assert r.status_code == 409


def test_output_activity_is_enrichment_only(env, monkeypatch):
    env.state.reply = "r"
    monkeypatch.setattr(
        server,
        "_agent_activity_cached",
        lambda i, t: (_ for _ in ()).throw(RuntimeError("x")),
    )
    body = client.get("/api/instances/w/output").json()
    assert body["activity"] == "" and body["text"] == "r"


# --------------------------------------------------------------------------- #
# /answer                                                                      #
# --------------------------------------------------------------------------- #
def test_answer_types_text_then_keys_without_enter(env):
    r = client.post(
        "/api/instances/w/answer",
        json={"text": "use\nthe\x1b fix", "keys": ["Down", "Enter"]},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "activity_before": "clarify"}
    assert env.tm.sends() == [["-l", "--", "usethe fix"], ["Down"], ["Enter"]]
    # Every send targets the agent's tmux session.
    for c in env.tm.calls:
        if c[1] == "send-keys":
            assert c[2:4] == ["-t", "mindflock_w"]
    # An agent answered, not a human.
    assert env.human == []


def test_answer_text_only_never_presses_enter(env):
    client.post("/api/instances/w/answer", json={"text": "2"})
    assert env.tm.sends() == [["-l", "--", "2"]]


def test_answer_keys_only_on_a_limit_menu(env):
    env.state.activity = "limit"
    r = client.post("/api/instances/w/answer", json={"keys": ["Escape"]})
    assert r.json()["activity_before"] == "limit"
    assert env.tm.sends() == [["Escape"]]


@pytest.mark.parametrize("activity", ["idle", "working", "offline"])
def test_answer_409_unless_waiting_on_a_prompt(env, activity):
    env.state.activity = activity
    r = client.post("/api/instances/w/answer", json={"keys": ["Enter"]})
    assert r.status_code == 409
    assert r.json()["error"] == (
        "session is not waiting on a prompt (activity: %s)" % activity
    )
    assert env.tm.sends() == []


def test_answer_activity_probe_failure_is_409(env, monkeypatch):
    monkeypatch.setattr(
        server, "_agent_activity", lambda i, t: (_ for _ in ()).throw(OSError())
    )
    r = client.post("/api/instances/w/answer", json={"keys": ["Enter"]})
    assert r.status_code == 409 and "activity: unknown" in r.json()["error"]


def test_answer_uses_the_uncached_activity(env, monkeypatch):
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: "clarify")
    env.state.activity = "idle"  # what the live probe says
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    r = client.post("/api/instances/w/answer", json={"keys": ["Enter"]})
    assert r.status_code == 409


def test_answer_validation_errors(env):
    assert client.post("/api/instances/nope/answer", json={}).status_code == 404
    r = client.post("/api/instances/w/answer", json={"keys": ["C-c"]})
    assert r.status_code == 400 and "key not allowed" in r.json()["error"]
    r = client.post("/api/instances/w/answer", json={})
    assert r.status_code == 400
    assert env.tm.sends() == []


def test_answer_budget_locked_is_409(env, monkeypatch):
    monkeypatch.setattr(server, "_budget_locked", lambda t: True)
    r = client.post("/api/instances/w/answer", json={"keys": ["Enter"]})
    assert r.status_code == 409 and r.json()["budget_locked"] is True
    assert env.tm.sends() == []


def test_answer_502_when_tmux_is_gone(env):
    env.tm.alive = False
    r = client.post("/api/instances/w/answer", json={"keys": ["Enter"]})
    assert r.status_code == 502


def test_answer_stops_at_the_first_failed_key(env):
    env.tm.fail_on = "Down"
    r = client.post("/api/instances/w/answer", json={"keys": ["Up", "Down", "Enter"]})
    assert r.status_code == 502
    assert env.tm.sends() == [["Up"], ["Down"]]


def test_send_answer_failed_text_sends_no_keys(env):
    env.tm.fail_on = "-l"
    assert agent_io.send_answer("mindflock_w", "x", ["Enter"]) is False
    assert env.tm.sends() == [["-l", "--", "x"]]
    assert agent_io.send_answer("", "x", []) is False


@pytest.mark.parametrize("text", ["-H", "-tb", "--", "-l", "-5", "-t staging"])
def test_send_answer_text_starting_with_a_dash_is_literal(env, text):
    """Regression: without ``--`` tmux parsed a leading '-' as flags — some
    answers errored, others (``-H``, ``-tb``) typed NOTHING with rc 0 while the
    keys (Enter) still went out and the route said ok."""
    assert agent_io.send_answer("mindflock_w", text, ["Enter"]) is True
    assert env.tm.sends() == [["-l", "--", text], ["Enter"]]
