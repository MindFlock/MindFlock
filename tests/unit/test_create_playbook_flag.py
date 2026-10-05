"""``POST /api/instances`` with ``"playbook": "split"`` (the New dialog's
"Split across workers").

The launch prompt is decorated through the playbook registry — task first,
then the split instructions, idempotently — the session is forced into a
worktree of its own (workers fork from its commits), and a CLI that won't
get the MindFlock tools is refused up front. Every side effect of the create
is stubbed; the playbook handling and the route's own checks run for real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import session
from backend.mcp import playbooks
from backend.session.storage import Status
from backend.web import server
from backend.web.core import thread
from backend.web.server import app

client = TestClient(app)


class _NewInst:
    def __init__(self, opts):
        self.opts = opts
        self.Title = opts.title
        self.Branch = ""
        self.Program = opts.program
        self.Path = opts.path
        self.Prompt = opts.prompt
        self.Parent = opts.parent
        self.Spawned = opts.spawned
        self.Status = Status.Loading
        self.ExtraEnv = {}
        self.CreatedAt = None

    def SetStatus(self, s):  # noqa: N802
        self.Status = s

    def Started(self):  # noqa: N802
        return False

    def GetWorktreePath(self):  # noqa: N802
        return ""


@pytest.fixture
def create(monkeypatch, tmp_path):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(server._events.BUS, "emit", lambda name, **kw: None)
    monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
    monkeypatch.setattr(thread, "_SEEDS", {})
    captured: list = []

    def _new(opts):
        captured.append(opts)
        return _NewInst(opts)

    monkeypatch.setattr(session, "NewInstance", _new)
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
    monkeypatch.setattr(server, "_mark_onboarded", lambda: None)
    monkeypatch.setattr(server._ports, "env_for", lambda t: {})
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    state = SimpleNamespace(
        git=True,
        caps={"enabled": True, "providers": ["claude", "codex"]},
        zoned=[],
    )
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: (str(tmp_path), state.git)
    )
    monkeypatch.setattr(server, "_agent_mcp_caps", lambda: dict(state.caps))

    def _zones(prompt, program, dirs, plan_first):
        state.zoned.append(prompt)
        return prompt + "\n\n[zones]"

    monkeypatch.setattr(server, "_red_zone_prompt", _zones)

    def post(**body):
        body.setdefault("program", "claude")
        body.setdefault("repo_path", str(tmp_path))
        return client.post("/api/instances", json=body)

    return SimpleNamespace(post=post, captured=captured, state=state)


def test_split_decorates_the_prompt_task_first(create):
    r = create.post(title="api", prompt="add rate limiting", playbook="split")
    assert r.status_code == 202, r.text
    (opts,) = create.captured
    want = playbooks.decorate_prompt(
        "split", "add rate limiting", {"provider": "claude"}
    )
    # Split first, then the zone / plan-first notes.
    assert create.state.zoned == [want]
    assert opts.prompt == want + "\n\n[zones]"
    assert opts.prompt.split("\n", 1)[0] == playbooks.SPLIT_HEADER + "add rate limiting"
    assert "mcp__mindflock__spawn_session" in opts.prompt


def test_split_decoration_is_idempotent(create):
    once = playbooks.decorate_prompt(
        "split", "add rate limiting", {"provider": "claude"}
    )
    r = create.post(title="api", prompt=once, playbook="split")
    assert r.status_code == 202, r.text
    assert create.state.zoned == [once]


def test_split_uses_the_cli_spelling(create):
    r = create.post(title="cx", program="codex", prompt="task", playbook="split")
    assert r.status_code == 202, r.text
    assert "mcp__" not in create.captured[0].prompt
    assert "spawn_session" in create.captured[0].prompt


def test_split_forces_a_worktree(create):
    r = create.post(title="api", prompt="task", playbook="split", in_place=True)
    assert r.status_code == 202, r.text
    assert create.captured[0].in_place is False


def test_without_split_in_place_is_honored(create):
    r = create.post(title="api", prompt="task", in_place=True)
    assert r.status_code == 202, r.text
    assert create.captured[0].in_place is True
    assert create.state.zoned == ["task"]  # no split decoration


@pytest.mark.parametrize(
    "caps,program",
    [
        ({"enabled": False, "providers": ["claude"]}, "claude"),
        ({"enabled": True, "providers": ["codex"]}, "claude"),
        ({"enabled": True, "providers": ["claude", "codex"]}, "aider"),
    ],
)
def test_split_400_when_the_cli_gets_no_tools(create, caps, program):
    create.state.caps = caps
    r = create.post(title="api", program=program, prompt="task", playbook="split")
    assert r.status_code == 400
    assert r.json()["error"] == (
        "Split across workers needs the MindFlock tools: %s" % server._PB_NO_TOOLS
    )
    assert create.captured == []


@pytest.mark.parametrize(
    "body,msg",
    [
        ({"playbook": "wrapup", "prompt": "x"}, 'playbook must be "split"'),
        ({"playbook": 1, "prompt": "x"}, 'playbook must be "split"'),
        ({"playbook": "split", "prompt": "  "}, "needs a task"),
        ({"playbook": "split"}, "needs a task"),
    ],
)
def test_split_400s(create, body, msg):
    r = create.post(title="api", **body)
    assert r.status_code == 400 and msg in r.json()["error"]
    assert create.captured == []


def test_split_in_a_non_git_folder_is_400(create):
    create.state.git = False
    r = create.post(title="api", prompt="task", playbook="split")
    assert r.status_code == 400 and "needs a git repo" in r.json()["error"]
    assert create.captured == []


def test_empty_playbook_is_no_playbook(create):
    r = create.post(title="api", prompt="task", playbook="")
    assert r.status_code == 202
    assert create.state.zoned == ["task"]


def test_create_remembers_the_seed_prompt(create):
    create.post(title="w1", prompt="do the billing piece")
    assert thread._SEEDS["w1"][1] == "do the billing piece\n\n[zones]"


# --------------------------------------------------------------------------- #
# The session records its playbook (E2E defect B, 2026-10-05)                  #
# --------------------------------------------------------------------------- #
def test_split_records_the_playbook_on_the_session(create):
    """A split orchestrator is one from launch: its first spawn_session
    prompt (and a whoami before it) came before any child existed, so the
    rail — gated on "has children" — showed no answer strip for them."""
    r = create.post(title="api", prompt="add rate limiting", playbook="split")
    assert r.status_code == 202, r.text
    (opts,) = create.captured
    assert opts.playbook == "split"


@pytest.mark.parametrize("body", [{}, {"playbook": ""}, {"playbook": None}])
def test_no_playbook_records_none(create, body):
    r = create.post(title="plain", prompt="do a thing", **body)
    assert r.status_code == 202, r.text
    (opts,) = create.captured
    assert opts.playbook == ""


def test_the_playbook_survives_a_save_and_reaches_the_row():
    """Instance → InstanceData → state.json → Instance, and the row."""
    from backend.session.instance import FromInstanceData, new_instance
    from backend.session.storage import InstanceData
    from backend.web.core import snapshot

    opts = session.InstanceOptions(
        title="orch", path="/tmp/x", program="claude", playbook="split"
    )
    inst = new_instance(opts)
    assert inst.Playbook == "split"
    data = inst.ToInstanceData()
    assert data.playbook == "split"
    back = FromInstanceData(InstanceData.from_dict(data.to_dict()), attach=False)
    assert back.Playbook == "split"
    row = snapshot._instance_json(back, cheap=True)
    assert row["playbook"] == "split"
    plain = FromInstanceData(InstanceData(title="p", program="claude"), attach=False)
    assert snapshot._instance_json(plain, cheap=True)["playbook"] == ""
