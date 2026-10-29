"""Flock mode (``core.flock_mode``): the sticky ``.flock`` flag file, the
request text from settings, ``POST /api/instances/{title}/flock``, the row
field, ``/api/config``'s ``flock`` and ``/send``'s workers request.

The TestClient pattern of test_voice_routes: fake instances via
``setattr(server.ENGINE, "instances", {...})`` (never ``setitem``), tmux and
the providers stubbed at the seams, ``MINDFLOCK_VOICE_FLAG_DIR`` and
``MINDFLOCK_SETTINGS_FILE`` under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import session
from backend.config import settings as settings_mod
from backend.web import server
from backend.web.core import flock_mode, guests, spoken
from backend.web.server import app

client = TestClient(app)

_DEFAULT = settings_mod.DEFAULT_WORKERS_REQUEST.replace(
    "{model}", settings_mod.DEFAULT_WORKERS_MODEL
)


class _Inst:
    def __init__(self, title, wt="/tmp/wt", program="claude"):
        self.Title = title
        self.Program = program
        self._wt = wt

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """Flag dir + settings file under tmp_path (never the real ones)."""
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod.invalidate()
    yield tmp_path
    settings_mod.invalidate()


@pytest.fixture
def env(monkeypatch, cfg):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    state = SimpleNamespace(hook=True, installs=[])

    class _Prov:
        name = "claude"

        def voice_prompt_hook(self):
            return state.hook

        def install_activity_hooks(self, workdir, session_name):
            state.installs.append((workdir, session_name))

    monkeypatch.setattr(server.providers, "resolve", lambda prog: _Prov())
    instances["w"] = _Inst("w")
    return SimpleNamespace(state=state, instances=instances, tmp=cfg)


# --------------------------------------------------------------------------- #
# core.flock_mode
# --------------------------------------------------------------------------- #
def test_the_flag_file_lifecycle(cfg):
    p = flock_mode.flag_path("mindflock_w")
    assert p == cfg / "voice" / "mindflock_w.flock"
    assert flock_mode.is_on("mindflock_w") is False
    assert flock_mode.is_on("") is False
    assert flock_mode.set_on("mindflock_w", True) is True
    # The file holds the request text the hook prints.
    assert p.read_text() == _DEFAULT + "\n"
    assert flock_mode.set_on("mindflock_w", True, "  Custom words. ") is True
    assert p.read_text() == "Custom words.\n"
    assert flock_mode.set_on("mindflock_w", False) is False
    assert flock_mode.set_on("mindflock_w", False) is False  # idempotent
    assert not p.exists()


def test_request_text_fills_in_the_model(cfg):
    assert flock_mode.request_text() == _DEFAULT
    assert "(model claude-opus-5-5)" in _DEFAULT
    settings_mod.update_settings(
        general={
            "workers_request": "Workers on {model}, {braces} kept",
            "workers_model": "m-1",
        }
    )
    assert flock_mode.request_text() == "Workers on m-1, {braces} kept"
    # A blank value reads as its default.
    settings_mod.update_settings(general={"workers_request": "", "workers_model": " "})
    assert flock_mode.request_text() == _DEFAULT


def test_the_defaults_are_never_written_to_disk(cfg):
    gen = settings_mod.GeneralSettings.from_dict({})
    assert gen.workers_request == settings_mod.DEFAULT_WORKERS_REQUEST
    assert gen.workers_model == settings_mod.DEFAULT_WORKERS_MODEL
    assert "workers_request" not in gen.to_dict()
    assert "workers_model" not in gen.to_dict()
    gen = settings_mod.GeneralSettings.from_dict({"workers_model": "m-2"})
    assert gen.to_dict() == {"workers_model": "m-2"}


def test_rewrite_all_puts_the_new_words_in_every_flag(cfg):
    assert flock_mode.rewrite_all() == 0  # no flag dir yet
    flock_mode.set_on("mindflock_a", True)
    flock_mode.set_on("mindflock_b", True, "stale words")
    (cfg / "voice" / "mindflock_c.on").write_text("1\n")  # a voice flag: untouched
    settings_mod.update_settings(general={"workers_model": "m-3"})
    assert flock_mode.rewrite_all() == 2
    want = _DEFAULT.replace("claude-opus-5-5", "m-3") + "\n"
    assert flock_mode.flag_path("mindflock_a").read_text() == want
    assert flock_mode.flag_path("mindflock_b").read_text() == want
    assert (cfg / "voice" / "mindflock_c.on").read_text() == "1\n"


def test_a_settings_save_rewrites_the_flags(env):
    flock_mode.set_on("mindflock_w", True)
    r = client.post("/api/settings", json={"general": {"workers_model": "m-4"}})
    assert r.status_code == 200, r.text
    assert "(model m-4)" in flock_mode.flag_path("mindflock_w").read_text()
    # The settings view shows the effective words (defaults included).
    gen = r.json()["settings"]["general"]
    assert gen["workers_model"] == "m-4"


# --------------------------------------------------------------------------- #
# POST /api/instances/{title}/flock
# --------------------------------------------------------------------------- #
def test_the_route_turns_it_on_and_off(env):
    r = client.post("/api/instances/w/flock", json={"on": True})
    assert r.status_code == 200 and r.json() == {"flock": True}
    assert flock_mode.is_on("mindflock_w")
    # A hook CLI is re-pinned so an older build's hook learns the .flock file.
    assert env.state.installs == [("/tmp/wt", "mindflock_w")]
    r = client.post("/api/instances/w/flock", json={"on": False})
    assert r.json() == {"flock": False}
    assert not flock_mode.is_on("mindflock_w")
    assert len(env.state.installs) == 1  # off never re-pins


def test_a_hookless_cli_is_not_repinned(env):
    env.state.hook = False
    assert client.post("/api/instances/w/flock", json={"on": True}).json() == {
        "flock": True
    }
    assert env.state.installs == []


def test_the_route_refuses_bad_bodies_unknown_and_remote_sessions(env):
    assert (
        client.post("/api/instances/nope/flock", json={"on": True}).status_code == 404
    )
    for body in ({}, {"on": "yes"}, {"on": 1}):
        assert client.post("/api/instances/w/flock", json=body).status_code == 400
    assert client.post("/api/instances/w/flock", content=b"not json").status_code == 400
    # A <device>::<title> row is forwarded to its device by the remote proxy,
    # which relays it with X-MindFlock-Remote: the far route refuses that.
    r = client.post("/api/instances/dev::far/flock", json={"on": True})
    assert r.status_code != 200
    # ...and the handler itself refuses a namespaced title and a relayed
    # request (the gate 403s an unauthenticated relay before it gets here).
    from backend.web.routes import flock as flock_routes

    class _Req:
        def __init__(self, headers):
            self.headers = headers

        async def json(self):
            return {"on": True}

    r = asyncio.run(flock_routes.flock_set("dev::far", _Req({})))
    assert r.status_code == 409
    r = asyncio.run(flock_routes.flock_set("w", _Req({"X-MindFlock-Remote": "d"})))
    assert r.status_code == 409
    assert not flock_mode.is_on("mindflock_w")


def test_a_guest_link_may_not_set_it():
    assert "flock" not in guests.SESSION_WRITE_OK


# --------------------------------------------------------------------------- #
# Rows and /api/config
# --------------------------------------------------------------------------- #
def _real_inst(title):
    # Never registered in ENGINE: these tests must not touch the live state.
    return session.NewInstance(
        session.InstanceOptions(title=title, path=".", program="claude", in_place=True)
    )


def test_rows_carry_flock(monkeypatch, cfg):
    monkeypatch.setattr(server, "_session_last_prompt", lambda i: None)
    monkeypatch.setattr(server, "_session_last_prompt_full", lambda i: None)
    monkeypatch.setattr(server, "_session_last_turn", lambda i: None)
    monkeypatch.setattr(server, "_session_conversation", lambda i: "c1")
    monkeypatch.setattr(server, "_session_stage", lambda i: {"stage": "agent"})
    monkeypatch.setattr(server, "_session_tokens", lambda i: {})
    inst = _real_inst("frow-1")
    assert "flock" not in server._SNAPSHOT_PROBE_KEYS  # one stat: always fresh
    try:
        server._forget_probes("frow-1")
        assert server._session_snapshot(inst, {})["flock"] is False
        assert server._session_snapshot_cheap(inst, {})["flock"] is False
        flock_mode.set_on("mindflock_frow-1", True)
        server._forget_probes("frow-1")
        assert server._session_snapshot(inst, {})["flock"] is True
        assert server._session_snapshot_cheap(inst, {})["flock"] is True
    finally:
        server._forget_probes("frow-1")


def test_config_carries_the_request(cfg):
    body = client.get("/api/config").json()
    assert body["flock"] == {"request": _DEFAULT, "model": "claude-opus-5-5"}
    settings_mod.update_settings(general={"workers_model": "m-5"})
    body = client.get("/api/config").json()
    assert body["flock"]["model"] == "m-5"
    assert "(model m-5)" in body["flock"]["request"]


# --------------------------------------------------------------------------- #
# /send: the workers request
# --------------------------------------------------------------------------- #
@pytest.fixture
def typed(monkeypatch, env, tmp_path):
    """The REAL _deliver_to_agent with the tmux seams stubbed; records what
    is typed."""
    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "pq.json"))
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setattr(server, "_driven_elsewhere", lambda req, title, payload: None)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda title: False)
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: ("mindflock_" + t, None)
    )
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    monkeypatch.setattr(server, "_note_human_input", lambda title: None)
    monkeypatch.setattr(server, "_dialog_on_screen", lambda inst, name: False)
    activity = SimpleNamespace(now="idle")
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: activity.now)
    out = SimpleNamespace(sent=[], activity=activity)

    def send_to_agent(name, text, submit=True):
        out.sent.append((name, text, submit))
        return True

    monkeypatch.setattr(server, "_send_to_agent", send_to_agent)
    return out


def _send(**body):
    r = client.post("/api/instances/w/send", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_sticky_flock_on_a_hookless_cli_types_the_request(typed, env):
    env.state.hook = False
    flock_mode.set_on("mindflock_w", True)
    _send(text="fix it\n")
    assert typed.sent == [("mindflock_w", "fix it\n\n" + _DEFAULT, True)]


def test_sticky_flock_on_a_hook_cli_leaves_it_to_the_hook(typed, env):
    env.state.hook = True
    flock_mode.set_on("mindflock_w", True)
    _send(text="fix it", flock=True)  # the one-shot too: no duplicate
    assert typed.sent == [("mindflock_w", "fix it", True)]


@pytest.mark.parametrize("hook", [True, False])
def test_a_one_shot_flock_types_the_request(typed, env, hook):
    env.state.hook = hook
    _send(text="fix it", flock=True)
    _send(text="next one")  # one-shot: the next prompt is plain
    assert typed.sent == [
        ("mindflock_w", "fix it\n\n" + _DEFAULT, True),
        ("mindflock_w", "next one", True),
    ]
    assert not flock_mode.is_on("mindflock_w")


@pytest.mark.parametrize("flock", ["yes", 1, False])
def test_only_a_boolean_true_asks(typed, env, flock):
    _send(text="fix it", flock=flock)
    assert typed.sent == [("mindflock_w", "fix it", True)]


def test_an_unsubmitted_paste_never_asks(typed, env):
    env.state.hook = False
    flock_mode.set_on("mindflock_w", True)
    _send(text="draft", flock=True, submit=False)
    assert typed.sent == [("mindflock_w", "draft", False)]


def test_flock_comes_before_the_voice_instruction(typed, env):
    env.state.hook = False
    _send(text="fix it", flock=True, say=True)
    assert typed.sent == [
        ("mindflock_w", "fix it\n\n" + _DEFAULT + "\n\n" + spoken.INSTRUCTION, True)
    ]


def test_enter_types_the_request_after_the_draft(typed, env):
    env.state.hook = False
    flock_mode.set_on("mindflock_w", True)
    assert _send(enter=True)["entered"] is True
    assert typed.sent == [("mindflock_w", "\n\n" + _DEFAULT, True)]


def test_a_queued_prompt_carries_the_request_in_its_text(typed, env):
    from backend.web.core import prompt_queue as pq

    typed.activity.now = "clarify"  # dialog_safe: queued, not typed
    assert _send(text="later", flock=True, dialog_safe=True)["queued"] is True
    assert typed.sent == []
    (item,) = pq.list_queue("w")
    assert item["text"] == "later\n\n" + _DEFAULT


def test_the_suffix_is_idempotent(cfg):
    once = server._flock_suffix("x")
    assert server._flock_suffix(once + "\n") == once + "\n"
    assert json.dumps(once).count("Use MindFlock workers") == 1
