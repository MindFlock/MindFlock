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
import time
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
# /send `say`: the route parses it, the delivery asks for the Say line
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


@pytest.mark.parametrize(
    "body, say",
    [
        ({"text": "fix it", "say": True}, True),
        ({"text": "fix it"}, False),
        ({"text": "fix it", "say": False}, False),
        ({"text": "fix it", "say": "yes"}, False),  # a boolean, not truthy
        ({"text": "draft", "say": True, "submit": False}, False),  # a paste
    ],
)
def test_send_hands_say_to_the_delivery_untouched(send, env, body, say):
    r = client.post("/api/instances/w/send", json=body)
    assert r.status_code == 200
    title, text, kw = send[-1]
    assert text == body["text"]  # nothing appended at request time
    assert kw["say"] is say


@pytest.fixture
def typed(monkeypatch, env, tmp_path):
    """The REAL _deliver_to_agent with the tmux seams stubbed; records what
    is typed and whether ``.next`` was armed at that instant."""
    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "pq.json"))
    monkeypatch.setattr(server, "_driven_elsewhere", lambda req, title, payload: None)
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: False)
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("mindflock_" + t, None)
    )
    monkeypatch.setattr(server, "_note_human_input", lambda title: None)
    monkeypatch.setattr(server, "_dialog_on_screen", lambda inst, name: False)
    out = SimpleNamespace(sent=[], ok=True)

    def send_to_agent(name, text, submit=True):
        out.sent.append((name, text, submit, voice.next_path(name).exists()))
        return out.ok

    monkeypatch.setattr(server, "_send_to_agent", send_to_agent)
    return out


def test_say_on_claude_arms_next_right_before_the_keystrokes(typed, env):
    env.state.hook = True
    r = client.post("/api/instances/w/send", json={"text": "fix it", "say": True})
    assert r.json() == {"sent": True, "submitted": True}
    assert typed.sent == [("mindflock_w", "fix it", True, True)]
    # The hook re-pinned first (an older build's hook knows only `.on`).
    assert env.state.installs == [("/tmp/wt", "mindflock_w")]
    nxt = voice.next_path("mindflock_w")
    assert float(nxt.read_text()) == pytest.approx(time.time(), abs=5)
    assert not voice.is_on("mindflock_w")  # the sticky flag is not touched


def test_say_on_a_hookless_cli_types_the_instruction(typed, env):
    env.state.hook = False
    client.post("/api/instances/w/send", json={"text": "fix it\n", "say": True})
    assert typed.sent == [
        ("mindflock_w", "fix it\n\n" + spoken.INSTRUCTION, True, False)
    ]
    assert env.state.installs == []


@pytest.mark.parametrize("hook", [True, False])
def test_no_say_asks_for_nothing_even_with_the_sticky_flag(typed, env, hook):
    env.state.hook = hook
    voice.set_on("mindflock_w", True)
    client.post("/api/instances/w/send", json={"text": "plain"})
    client.post(
        "/api/instances/w/send", json={"text": "draft", "say": True, "submit": False}
    )
    assert typed.sent == [
        ("mindflock_w", "plain", True, False),
        ("mindflock_w", "draft", False, False),
    ]
    assert not voice.next_path("mindflock_w").exists()


def test_a_failed_send_disarms_again(typed, env):
    env.state.hook = True
    typed.ok = False
    r = client.post("/api/instances/w/send", json={"text": "fix it", "say": True})
    assert r.status_code == 502
    assert typed.sent[-1][3] is True  # it was armed for the keystrokes ...
    assert not voice.next_path("mindflock_w").exists()  # ... and taken back


def test_an_unarmable_request_falls_back_to_typing_it(typed, env, monkeypatch):
    env.state.hook = True

    def boom(name):
        raise OSError("read-only")

    monkeypatch.setattr(voice, "arm_next", boom)
    client.post("/api/instances/w/send", json={"text": "fix it", "say": True})
    assert typed.sent[-1][1] == "fix it\n\n" + spoken.INSTRUCTION


def test_voice_say_never_loses_the_prompt(env, monkeypatch):
    def boom(prog):
        raise RuntimeError("no provider")

    monkeypatch.setattr(server.providers, "resolve", boom)
    assert server._voice_say(_Inst("w"), "w", "keep me") == "keep me"
    assert server._voice_suffix("x\n\n" + spoken.INSTRUCTION + "\n") == (
        "x\n\n" + spoken.INSTRUCTION + "\n"
    )  # idempotent


@pytest.mark.parametrize("hook", [True, False])
def test_a_queued_say_prompt_keeps_say_and_arms_at_the_drain(
    typed, env, monkeypatch, hook
):
    from backend.web.core import prompt_queue as pq
    from backend.web.core import queue_driver

    env.state.hook = hook
    env.state.activity = "clarify"  # dialog_safe: queued, not typed
    r = client.post(
        "/api/instances/w/send",
        json={"text": "later", "say": True, "dialog_safe": True},
    )
    assert r.json()["queued"] is True
    assert typed.sent == [] and not voice.next_path("mindflock_w").exists()
    (item,) = pq.list_queue("w")
    assert item["text"] == "later" and item["say"] is True
    # The drain types it: armed (Claude) / suffixed (others) only now.
    monkeypatch.setattr(queue_driver, "_dialog_on_screen", lambda inst, name: False)
    rec = {"armed": True, "sent_at": 0.0}
    assert queue_driver._send_queued_item("w", "mindflock_w", item, rec, time.time())
    if hook:
        assert typed.sent == [("mindflock_w", "later", True, True)]
    else:
        assert typed.sent == [
            ("mindflock_w", "later\n\n" + spoken.INSTRUCTION, True, False)
        ]
    assert rec["sent_text"] == typed.sent[-1][1]
    assert pq.list_queue("w") == []


def test_a_queued_prompt_without_say_drains_plain(typed, env, monkeypatch):
    from backend.web.core import prompt_queue as pq
    from backend.web.core import queue_driver

    env.state.hook = False
    voice.set_on("mindflock_w", True)
    pq.enqueue("w", "typed by hand")
    (item,) = pq.list_queue("w")
    assert "say" not in item
    monkeypatch.setattr(queue_driver, "_dialog_on_screen", lambda inst, name: False)
    assert queue_driver._send_queued_item("w", "mindflock_w", item, {}, time.time())
    assert typed.sent == [("mindflock_w", "typed by hand", True, False)]


def test_a_failed_drain_send_disarms_and_keeps_the_item(typed, env, monkeypatch):
    from backend.web.core import prompt_queue as pq
    from backend.web.core import queue_driver

    env.state.hook = True
    typed.ok = False
    pq.enqueue("w", "later", say=True)
    (item,) = pq.list_queue("w")
    monkeypatch.setattr(queue_driver, "_dialog_on_screen", lambda inst, name: False)
    assert not queue_driver._send_queued_item("w", "mindflock_w", item, {}, time.time())
    assert not voice.next_path("mindflock_w").exists()
    assert pq.list_queue("w") == [item]


def test_send_now_arms_a_say_item(typed, env):
    from backend.web.core import prompt_queue as pq

    env.state.hook = True
    pq.enqueue("w", "now please", say=True)
    (item,) = pq.list_queue("w")
    r = client.post("/api/instances/w/queue/send_now", json={"id": item["id"]})
    assert r.status_code == 200, r.text
    assert typed.sent == [("mindflock_w", "now please", True, True)]


# --------------------------------------------------------------------------- #
# /send {"enter": true}: press Enter on the draft live dictation typed
# --------------------------------------------------------------------------- #
def test_enter_presses_enter_and_arms_next_on_claude(typed, env):
    env.state.hook = True
    r = client.post("/api/instances/w/send", json={"enter": True, "say": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": True, "submitted": True, "entered": True}
    # Nothing typed but the Enter itself (the helper's separate keystroke),
    # with the one-shot request armed at that instant.
    assert typed.sent == [("mindflock_w", "", True, True)]
    assert env.state.installs == [("/tmp/wt", "mindflock_w")]


def test_enter_on_a_hookless_cli_types_the_instruction_after_the_draft(typed, env):
    env.state.hook = False
    r = client.post(
        "/api/instances/w/send", json={"enter": True, "text": "", "say": True}
    )
    assert r.json()["entered"] is True
    assert typed.sent == [("mindflock_w", "\n\n" + spoken.INSTRUCTION, True, False)]


def test_enter_without_say_presses_only_enter(typed, env):
    env.state.hook = True
    voice.set_on("mindflock_w", True)  # the sticky flag asks nothing per prompt
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 200
    assert typed.sent == [("mindflock_w", "", True, False)]
    assert not voice.next_path("mindflock_w").exists()


@pytest.mark.parametrize(
    "body",
    [
        {"enter": True, "text": "words"},
        {"enter": True, "submit": False},
        {"enter": "yes"},  # a boolean, not truthy: the plain empty-message 400
        {},
    ],
)
def test_enter_bad_forms_are_400(typed, env, body):
    r = client.post("/api/instances/w/send", json=body)
    assert r.status_code == 400
    assert typed.sent == []


@pytest.mark.parametrize("activity", ["clarify", "limit", "unknown"])
def test_enter_never_answers_a_dialog(typed, env, activity):
    env.state.activity = activity
    r = client.post("/api/instances/w/send", json={"enter": True, "say": True})
    assert r.status_code == 409
    assert r.json()["in_dialog"] is True
    assert typed.sent == []
    assert not voice.next_path("mindflock_w").exists()


def test_enter_checks_the_screen_for_a_dialog(typed, env, monkeypatch):
    monkeypatch.setattr(server, "_dialog_on_screen", lambda inst, name: True)
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 409 and r.json()["in_dialog"] is True
    assert typed.sent == []


def test_enter_into_a_working_agent_goes_through(typed, env):
    env.state.activity = "working"  # like a dialog_safe send: the CLI queues it
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 200
    assert typed.sent == [("mindflock_w", "", True, False)]


def test_enter_refuses_a_paused_session(typed, env):
    env.instances["w"].Status = server.Paused
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 409
    assert r.json()["paused"] is True
    assert typed.sent == []


def test_enter_refuses_an_asleep_session_without_waking_or_queueing(
    typed, env, monkeypatch
):
    from backend.web.core import prompt_queue as pq

    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: True)
    woke = []
    monkeypatch.setattr(server._sleep, "ensure_awake", lambda *a: woke.append(a))
    r = client.post("/api/instances/w/send", json={"enter": True, "say": True})
    assert r.status_code == 409
    assert "asleep" in r.json()["error"]
    assert typed.sent == [] and woke == [] and pq.list_queue("w") == []


def test_enter_refuses_a_session_that_is_not_running(typed, env, monkeypatch):
    env.tm.alive = False
    booted = []
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: booted.append(t) or ("x", None)
    )
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 409
    assert "isn't running" in r.json()["error"]
    assert typed.sent == [] and booted == []


def test_enter_over_budget_is_409(typed, env, monkeypatch):
    monkeypatch.setattr(server, "_budget_locked", lambda t: True)
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 409 and r.json()["budget_locked"] is True
    assert typed.sent == []


def test_enter_respects_the_driver(typed, env, monkeypatch):
    from fastapi.responses import JSONResponse

    monkeypatch.setattr(
        server,
        "_driven_elsewhere",
        lambda req, title, payload: JSONResponse({"error": "driven by B"}, 409),
    )
    r = client.post("/api/instances/w/send", json={"enter": True})
    assert r.status_code == 409 and typed.sent == []


def test_a_failed_enter_is_502_and_disarms(typed, env):
    env.state.hook = True
    typed.ok = False
    r = client.post("/api/instances/w/send", json={"enter": True, "say": True})
    assert r.status_code == 502
    assert typed.sent[-1][3] is True
    assert not voice.next_path("mindflock_w").exists()


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
