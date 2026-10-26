"""``/api/instances/{title}/voice*``, the ``say`` output view, the ``/send``
voice suffix and the row fields ``last_say`` / ``voice``.

The TestClient pattern of test_agent_io: fake instances via
``setattr(server.ENGINE, "instances", {...})`` (never ``setitem`` — the real
state.json is behind ENGINE), tmux and the providers stubbed at the seams,
``MINDFLOCK_VOICE_FLAG_DIR`` / ``MINDFLOCK_ACTIVITY_MARKER_DIR`` under
``tmp_path``, and the one-shot model runner replaced.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import session
from backend.providers import activity_markers as am
from backend.web import server
from backend.web.core import agent_io, commit_message, proc, spoken, voice
from backend.web.routes import voice as voice_routes
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
    def __init__(self):
        self.alive = True
        self.screen = "visible screen text\n\n"

    def __call__(self, args, *, timeout, **kw):
        sub = args[1]
        if sub == "has-session":
            return subprocess.CompletedProcess(args, 0 if self.alive else 1)
        if sub == "capture-pane":
            return subprocess.CompletedProcess(args, 0, self.screen, "")
        raise AssertionError("unexpected tmux call %r" % (args,))


@pytest.fixture
def env(monkeypatch, tmp_path):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    spoken._REPLY_CACHE.clear()
    tm = _Tmux()
    monkeypatch.setattr(server, "_run_capped", tm)
    monkeypatch.setattr(proc, "run_capped", tm)
    monkeypatch.setattr(server, "_live_session_name", lambda n: n if tm.alive else None)
    monkeypatch.setattr(agent_io.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    state = SimpleNamespace(activity="idle", reply=None, hook=True, installs=[])
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: state.activity)
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: state.activity)

    class _Prov:
        name = "claude"

        def last_assistant_text(self, session_name, workdir, **kw):
            return state.reply

        def voice_prompt_hook(self):
            return state.hook

        def install_activity_hooks(self, workdir, session_name):
            state.installs.append((workdir, session_name))

    monkeypatch.setattr(server.providers, "resolve", lambda prog: _Prov())
    instances["w"] = _Inst("w")
    yield SimpleNamespace(tm=tm, state=state, instances=instances, tmp=tmp_path)
    spoken._REPLY_CACHE.clear()


def _write_reply(text, name="mindflock_w", ts=1700000000):
    p = am.reply_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": ts, "text": text}))
    return p


# --------------------------------------------------------------------------- #
# GET / POST /voice
# --------------------------------------------------------------------------- #
def test_voice_404_unknown_session(env):
    assert client.get("/api/instances/nope/voice").status_code == 404
    assert (
        client.post("/api/instances/nope/voice", json={"on": True}).status_code == 404
    )
    assert client.post("/api/instances/nope/voice/condense").status_code == 404


def test_voice_get_default_shape(env):
    r = client.get("/api/instances/w/voice")
    assert r.status_code == 200
    assert r.json() == {
        "on": False,
        "say": None,
        "has_question": False,
        "source": "none",
        "ts": None,
    }


def test_voice_get_reads_the_hooks_say_line(env):
    _write_reply("Done.\n\nSay: The tests pass. Should I open the PR?", ts=42)
    r = client.get("/api/instances/w/voice")
    assert r.json() == {
        "on": False,
        "say": "The tests pass. Should I open the PR?",
        "has_question": True,
        "source": "hook",
        "ts": 42.0,
    }


def test_voice_on_sets_the_flag_and_repins_a_hook_cli(env):
    r = client.post("/api/instances/w/voice", json={"on": True})
    assert r.status_code == 200
    body = r.json()
    assert body["on"] is True and body["source"] == "none"
    assert voice.is_on("mindflock_w")
    assert voice.flag_path("mindflock_w") == env.tmp / "voice" / "mindflock_w.on"
    # The running Claude CLI hot-reloads the voice-aware hook text.
    assert env.state.installs == [("/tmp/wt", "mindflock_w")]
    # Off: the flag goes, nothing is re-pinned.
    r = client.post("/api/instances/w/voice", json={"on": False})
    assert r.status_code == 200 and r.json()["on"] is False
    assert not voice.is_on("mindflock_w")
    assert env.state.installs == [("/tmp/wt", "mindflock_w")]
    # Off again is a no-op, not an error.
    assert (
        client.post("/api/instances/w/voice", json={"on": False}).json()["on"] is False
    )


def test_voice_on_for_a_cli_without_a_prompt_hook_does_not_repin(env):
    env.state.hook = False
    r = client.post("/api/instances/w/voice", json={"on": True})
    assert r.status_code == 200 and r.json()["on"] is True
    assert env.state.installs == []


def test_voice_on_survives_a_failing_hook_install(env):
    def boom(prog):
        raise RuntimeError("provider broke")

    # The flag is the contract; the re-pin is enrichment.
    env.instances["w"] = _Inst("w")
    import backend.providers as providers_mod

    orig = providers_mod.resolve
    providers_mod.resolve = boom
    try:
        r = client.post("/api/instances/w/voice", json={"on": True})
    finally:
        providers_mod.resolve = orig
    assert r.status_code == 200 and r.json()["on"] is True


@pytest.mark.parametrize("body", [{}, {"on": "yes"}, {"on": 1}, {"on": None}, [], "on"])
def test_voice_post_bad_body(env, body):
    r = client.post("/api/instances/w/voice", json=body)
    assert r.status_code == 400
    assert "on must be true or false" in r.json()["error"]
    assert not voice.is_on("mindflock_w")


def test_voice_post_non_json_body_is_400(env):
    r = client.post("/api/instances/w/voice", content=b"not json")
    assert r.status_code == 400


def test_voice_is_keyed_by_the_tmux_session_name(env):
    env.instances["My Session.v2"] = _Inst("My Session.v2")
    client.post("/api/instances/My Session.v2/voice", json={"on": True})
    assert voice.is_on("mindflock_MySession_v2")


# --------------------------------------------------------------------------- #
# GET /output?view=say
# --------------------------------------------------------------------------- #
def test_output_view_say_is_a_known_view(env):
    assert "say" in agent_io.OUTPUT_VIEWS
    r = client.get("/api/instances/w/output?view=say")
    assert r.status_code == 200
    assert r.json() == {
        "view": "say",
        "text": "",
        "source": "none",
        "has_question": False,
        "truncated": False,
        "activity": "idle",
    }


def test_output_view_say_serves_the_spoken_line(env):
    _write_reply("I did the thing.\n\n```\ncode\n```\n")  # no Say: -> digest
    r = client.get("/api/instances/w/output?view=say&max_chars=3")
    body = r.json()
    assert body["view"] == "say" and body["text"] == "I did the thing."
    assert body["source"] == "digest" and body["has_question"] is False
    assert body["truncated"] is False  # never tail-cut: ≤320 by construction
    _write_reply("Say: Shall I push?", ts=1700000001)
    body = client.get("/api/instances/w/output?view=say").json()
    assert body["text"] == "Shall I push?" and body["source"] == "hook"
    assert body["has_question"] is True


def test_output_view_say_still_404s_and_rejects_bad_max_chars(env):
    assert client.get("/api/instances/nope/output?view=say").status_code == 404
    assert client.get("/api/instances/w/output?view=say&max_chars=x").status_code == 400


# --------------------------------------------------------------------------- #
# POST /voice/condense — one headless model call, never cached
# --------------------------------------------------------------------------- #
@pytest.fixture
def oneshot(monkeypatch):
    calls = []
    out = {"text": "**Done.** The tests pass — should I push?\n", "error": None}

    def run(prompt, program):
        calls.append((prompt, program))
        if out["error"]:
            raise commit_message.CommitMessageError(out["error"])
        return out["text"]

    monkeypatch.setattr(voice_routes, "_run_oneshot", run)
    return SimpleNamespace(calls=calls, out=out)


def test_condense_409_without_a_reply_or_a_screen(env, oneshot):
    env.tm.alive = False
    r = client.post("/api/instances/w/voice/condense")
    assert r.status_code == 409 and "nothing to condense" in r.json()["error"]
    assert oneshot.calls == []


def test_condense_asks_the_model_once_per_call_and_strips_markdown(env, oneshot):
    env.state.reply = "Here is the long reply.\n\n```\ncode\n```\nShould I push?"
    r = client.post("/api/instances/w/voice/condense")
    assert r.status_code == 200
    assert r.json() == {
        "text": "Done. The tests pass — should I push?",
        "source": "model",
    }
    assert len(oneshot.calls) == 1
    prompt, program = oneshot.calls[0]
    assert program == "claude"
    assert prompt.startswith("Here is the long reply.")
    assert prompt.endswith(voice_routes.CONDENSE_SUFFIX)
    # Never cached: the same reply asks again.
    client.post("/api/instances/w/voice/condense")
    assert len(oneshot.calls) == 2


def test_condense_caps_the_input_to_the_tail(env, oneshot):
    env.state.reply = "x" * 10000 + " THE END."
    client.post("/api/instances/w/voice/condense")
    prompt = oneshot.calls[0][0]
    head = prompt[: -len("\n\n" + voice_routes.CONDENSE_SUFFIX)]
    assert len(head) <= voice_routes.CONDENSE_INPUT_MAX
    assert head.endswith("THE END.")


def test_condense_falls_back_to_the_reply_file_then_the_screen(env, oneshot):
    env.state.reply = None
    _write_reply("From the hook's file.")
    client.post("/api/instances/w/voice/condense")
    assert oneshot.calls[-1][0].startswith("From the hook's file.")
    spoken.forget("mindflock_w")
    client.post("/api/instances/w/voice/condense")
    assert oneshot.calls[-1][0].startswith("visible screen text")


@pytest.mark.parametrize(
    "error,status",
    [
        ("claude did not answer within 45s", 504),
        ("claude is not installed", 409),
        (
            "no installed CLI (claude) has a headless mode MindFlock can ask for a spoken summary",
            409,
        ),
        ("claude exited 1: boom", 502),
    ],
)
def test_condense_maps_the_cli_failures(env, oneshot, error, status):
    env.state.reply = "Something was said."
    oneshot.out["error"] = error
    r = client.post("/api/instances/w/voice/condense")
    assert r.status_code == status
    assert r.json() == {"error": error}


def test_condense_empty_answer_is_502(env, oneshot):
    env.state.reply = "Something was said."
    oneshot.out["text"] = "   \n"
    assert client.post("/api/instances/w/voice/condense").status_code == 502


def test_run_oneshot_uses_the_session_plan_pattern(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        commit_message,
        "pick_argv",
        lambda prompt, program, fallback="", purpose="": seen.setdefault(
            "pick", (prompt, program, fallback, purpose)
        )
        and ["cli", "-p", prompt],
    )
    monkeypatch.setattr(
        commit_message,
        "_run",
        lambda argv, cwd, timeout: seen.setdefault("run", (argv, cwd, timeout))
        and "ok",
    )
    assert voice_routes._run_oneshot("P", "codex") == "ok"
    assert seen["pick"] == ("P", "codex", "", "a spoken summary")
    argv, cwd, timeout = seen["run"]
    assert argv == ["cli", "-p", "P"] and timeout == voice_routes.CONDENSE_TIMEOUT_S
    # commit_message's neutral dir, never $HOME (claude -p would file its
    # transcript under ~/.claude/projects and read ~/CLAUDE.md there).
    assert cwd == commit_message.neutral_cwd()


# --------------------------------------------------------------------------- #
# /send: the instruction suffix for CLIs without a prompt hook
# --------------------------------------------------------------------------- #
@pytest.fixture
def send(monkeypatch, env):
    sent = []

    def deliver(inst, title, text, **kw):
        sent.append((title, text, kw))
        return "sent", None

    monkeypatch.setattr(server, "_deliver_to_agent", deliver)
    monkeypatch.setattr(server, "_driven_elsewhere", lambda req, title, payload: None)
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: False)
    return sent


def test_send_appends_the_instruction_for_a_cli_without_a_prompt_hook(send, env):
    env.state.hook = False
    voice.set_on("mindflock_w", True)
    r = client.post("/api/instances/w/send", json={"text": "fix the bug\n"})
    assert r.status_code == 200 and r.json() == {"sent": True, "submitted": True}
    assert send[-1][1] == "fix the bug\n\n" + spoken.INSTRUCTION
    assert send[-1][2]["submit"] is True


def test_send_never_appends_for_claude(send, env):
    env.state.hook = True  # Claude: the UserPromptSubmit hook injects it
    voice.set_on("mindflock_w", True)
    client.post("/api/instances/w/send", json={"text": "fix the bug"})
    assert send[-1][1] == "fix the bug"


def test_send_leaves_an_unsubmitted_paste_and_a_voice_off_session_alone(send, env):
    env.state.hook = False
    voice.set_on("mindflock_w", True)
    client.post("/api/instances/w/send", json={"text": "draft", "submit": False})
    assert send[-1][1] == "draft" and send[-1][2]["submit"] is False
    voice.set_on("mindflock_w", False)
    client.post("/api/instances/w/send", json={"text": "plain"})
    assert send[-1][1] == "plain"


def test_send_suffix_rides_a_queued_message_too(monkeypatch, env):
    env.state.hook = False
    voice.set_on("mindflock_w", True)
    seen = []
    monkeypatch.setattr(
        server,
        "_deliver_to_agent",
        lambda inst, title, text, **kw: (seen.append(text), ("queued", None))[1],
    )
    monkeypatch.setattr(server, "_driven_elsewhere", lambda req, title, payload: None)
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: False)
    r = client.post(
        "/api/instances/w/send", json={"text": "later", "dialog_safe": True}
    )
    assert r.json()["queued"] is True
    assert seen == ["later\n\n" + spoken.INSTRUCTION]


def test_voice_suffix_never_loses_the_prompt(env):
    def boom(prog):
        raise RuntimeError("no provider")

    import backend.providers as providers_mod

    voice.set_on("mindflock_w", True)
    orig = providers_mod.resolve
    providers_mod.resolve = boom
    try:
        assert server._voice_suffix(_Inst("w"), "w", "keep me") == "keep me"
    finally:
        providers_mod.resolve = orig


# --------------------------------------------------------------------------- #
# Rows: last_say + voice
# --------------------------------------------------------------------------- #
def _real_inst(title):
    # Never registered in ENGINE: these tests must not touch the live state.
    return session.NewInstance(
        session.InstanceOptions(title=title, path=".", program="claude", in_place=True)
    )


@pytest.fixture
def probes(monkeypatch, env):
    monkeypatch.setattr(server, "_session_last_prompt", lambda i: None)
    monkeypatch.setattr(server, "_session_last_prompt_full", lambda i: None)
    monkeypatch.setattr(server, "_session_last_turn", lambda i: None)
    monkeypatch.setattr(server, "_session_conversation", lambda i: "c1")
    monkeypatch.setattr(server, "_session_stage", lambda i: {"stage": "agent"})
    monkeypatch.setattr(server, "_session_tokens", lambda i: {})
    return env


def test_the_full_row_carries_last_say_and_voice(probes):
    inst = _real_inst("vrow-1")
    try:
        server._forget_probes("vrow-1")
        row = server._session_snapshot(inst, {})
        assert row["last_say"] is None and row["voice"] is False
        _write_reply("Done.\n\nSay: Row words. Next?", name="mindflock_vrow-1")
        voice.set_on("mindflock_vrow-1", True)
        server._forget_probes("vrow-1")
        row = server._session_snapshot(inst, {})
        assert row["last_say"] == "Row words. Next?"
        assert row["voice"] is True
        assert len(row["last_say"]) <= spoken.SAY_MAX_CHARS
    finally:
        server._forget_probes("vrow-1")


def test_the_cheap_row_carries_last_say_over_and_reads_voice_fresh(probes):
    inst = _real_inst("vrow-2")
    assert "last_say" in server._SNAPSHOT_PROBE_KEYS
    assert "voice" not in server._SNAPSHOT_PROBE_KEYS  # one stat: always fresh
    row = server._session_snapshot_cheap(inst, {})
    assert row["last_say"] is None and row["voice"] is False
    voice.set_on("mindflock_vrow-2", True)
    row = server._session_snapshot_cheap(inst, {}, prev={"last_say": "carried over"})
    assert row["last_say"] == "carried over" and row["voice"] is True
