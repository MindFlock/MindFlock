"""Verify's helper sessions are hidden by an explicit flag, not by title.

``hidden_by`` (:mod:`backend.web.core.hidden_sessions`) is web metadata the
create route records when the caller passes it (``run_test_plan`` does) and
every ``/api/instances`` row carries back. The title prefix ``verify-`` is no
longer a signal: a person may name a session that way and it stays an ordinary,
listed session. Covers the store, the create route (stores the flag, refuses a
bad one, never rejects a ``verify-…`` title), the row, the delete path and the
startup backfill for runs that predate the flag.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import session
from backend.session.instance import InstanceOptions, new_instance
from backend.session.storage import Status
from backend.web import server
from backend.web.core import hidden_sessions, thread
from backend.web.core import snapshot as snapshot_mod
from backend.web.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Fresh on-disk store per test; never the user's real ~/.mindflock."""
    monkeypatch.setattr(hidden_sessions, "_path", lambda: str(tmp_path / "h.json"))
    monkeypatch.setattr(hidden_sessions, "_HIDDEN", None)
    yield
    monkeypatch.setattr(hidden_sessions, "_HIDDEN", None)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def test_unflagged_session_is_ordinary():
    assert hidden_sessions.hidden_by("verify-foo") == ""
    assert hidden_sessions.hidden_by("") == ""


def test_flag_roundtrips_and_persists():
    hidden_sessions.set_hidden_by("verify-p1-abc1234", "verify")
    assert hidden_sessions.hidden_by("verify-p1-abc1234") == "verify"
    # Survives a "restart" (the in-memory cache dropped, reloaded from disk).
    hidden_sessions._HIDDEN = None
    assert hidden_sessions.hidden_by("verify-p1-abc1234") == "verify"
    assert hidden_sessions.all_hidden() == {"verify-p1-abc1234": "verify"}


def test_clearing_and_dropping_forget_the_session():
    hidden_sessions.set_hidden_by("a", "verify")
    hidden_sessions.set_hidden_by("a", "")
    assert hidden_sessions.hidden_by("a") == ""
    hidden_sessions.set_hidden_by("b", "verify")
    hidden_sessions.drop("b")
    hidden_sessions.drop("never-there")
    assert hidden_sessions.all_hidden() == {}


def test_unknown_owner_is_refused_and_never_stored():
    assert hidden_sessions.validate(None) is None
    assert hidden_sessions.validate("") is None
    assert hidden_sessions.validate("verify") is None
    assert "hidden_by must be one of" in hidden_sessions.validate("assistant")
    assert "hidden_by must be one of" in hidden_sessions.validate(True)
    hidden_sessions.set_hidden_by("x", "assistant")
    assert hidden_sessions.hidden_by("x") == ""


def test_corrupt_values_on_disk_are_dropped(tmp_path):
    (tmp_path / "h.json").write_text('{"a": "verify", "b": "junk", "c": 1}')
    assert hidden_sessions.all_hidden() == {"a": "verify"}


# --------------------------------------------------------------------------- #
# The create route
# --------------------------------------------------------------------------- #
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
    """``POST /api/instances`` with every side effect stubbed; the route's own
    checks and the flag handling run for real (test_create_playbook_flag's
    fixture)."""
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
    monkeypatch.setattr(
        server,
        "_instance_json",
        lambda i, **k: {
            "title": i.Title,
            "hidden_by": hidden_sessions.hidden_by(i.Title),
        },
    )
    monkeypatch.setattr(server, "_mark_onboarded", lambda: None)
    monkeypatch.setattr(server._ports, "env_for", lambda t: {})
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: (str(tmp_path), True)
    )
    monkeypatch.setattr(
        server, "_agent_mcp_caps", lambda: {"enabled": True, "providers": ["claude"]}
    )
    monkeypatch.setattr(server, "_red_zone_prompt", lambda p, *a, **k: p)

    def post(**body):
        body.setdefault("program", "claude")
        body.setdefault("repo_path", str(tmp_path))
        return client.post("/api/instances", json=body)

    return SimpleNamespace(post=post, captured=captured, instances=instances)


def test_create_stores_the_flag(create):
    r = create.post(title="verify-p1-abc1234", prompt="check it", hidden_by="verify")
    assert r.status_code == 202, r.text
    assert r.json()["hidden_by"] == "verify"
    assert hidden_sessions.hidden_by("verify-p1-abc1234") == "verify"


def test_create_never_rejects_a_verify_titled_session(create):
    """The New dialog's naming rules are unchanged: ``verify-…`` is a fine name
    for a person's own session, and without the flag it stays an ordinary one."""
    r = create.post(title="verify-foo", prompt="my own thing")
    assert r.status_code == 202, r.text
    assert r.json()["hidden_by"] == ""
    assert hidden_sessions.hidden_by("verify-foo") == ""
    assert "verify-foo" in create.instances


@pytest.mark.parametrize("bad", ["assistant", True, ["verify"], 1])
def test_create_refuses_an_unknown_owner(create, bad):
    r = create.post(title="x", hidden_by=bad)
    assert r.status_code == 400, r.text
    assert "hidden_by" in r.json()["error"]
    assert create.captured == []
    assert hidden_sessions.all_hidden() == {}


def test_create_absent_or_empty_flag_is_ordinary(create):
    assert create.post(title="a", hidden_by="").status_code == 202
    assert create.post(title="b", hidden_by=None).status_code == 202
    assert hidden_sessions.all_hidden() == {}


def test_a_reused_title_does_not_inherit_the_flag(create):
    """Delete drops the entry (``_forget_session_state``), and creating the
    same title again without the flag records nothing — so an ordinary
    namesake never comes back hidden."""
    hidden_sessions.set_hidden_by("verify-p1-abc1234", "verify")
    server._forget_session_state("verify-p1-abc1234")
    assert hidden_sessions.hidden_by("verify-p1-abc1234") == ""
    r = create.post(title="verify-p1-abc1234", prompt="mine now")
    assert r.status_code == 202, r.text
    assert r.json()["hidden_by"] == ""


# --------------------------------------------------------------------------- #
# The row (what GET /api/instances serves)
# --------------------------------------------------------------------------- #
def test_row_carries_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    run = new_instance(
        InstanceOptions(title="verify-p1-abc1234", path=str(tmp_path), program="bash")
    )
    mine = new_instance(
        InstanceOptions(title="verify-foo", path=str(tmp_path), program="bash")
    )
    hidden_sessions.set_hidden_by("verify-p1-abc1234", "verify")
    assert snapshot_mod._instance_json(run, cheap=True)["hidden_by"] == "verify"
    # The title is not the signal: an unflagged verify-foo is ordinary.
    assert snapshot_mod._instance_json(mine, cheap=True)["hidden_by"] == ""


def test_list_route_returns_the_flag(monkeypatch, tmp_path):
    """The real list route with a stale tick snapshot (the cheap path), the
    only stub being the probe-free row builder's expensive neighbours."""
    run = new_instance(
        InstanceOptions(title="verify-p1-abc1234", path=str(tmp_path), program="bash")
    )
    mine = new_instance(
        InstanceOptions(title="verify-foo", path=str(tmp_path), program="bash")
    )
    monkeypatch.setattr(
        server.ENGINE, "instances", {"verify-p1-abc1234": run, "verify-foo": mine}
    )
    monkeypatch.setattr(server, "_SNAPSHOT_AT", 0.0)
    monkeypatch.setattr(server._remote, "merged_instances", lambda: [])
    monkeypatch.setattr(server, "_pending_rows", lambda: [])
    hidden_sessions.set_hidden_by("verify-p1-abc1234", "verify")
    r = client.get("/api/instances")
    assert r.status_code == 200, r.text
    by_title = {row["title"]: row for row in r.json()}
    assert by_title["verify-p1-abc1234"]["hidden_by"] == "verify"
    assert by_title["verify-foo"]["hidden_by"] == ""


# --------------------------------------------------------------------------- #
# Verify sets it; the backfill covers the runs that predate it
# --------------------------------------------------------------------------- #
def test_run_test_plan_creates_with_the_flag():
    """``run_test_plan`` is the one creator of verify sessions; its create call
    passes the flag and its reuse path stamps it. Pinned on the source: the
    route needs a whole plan store, a repo and a tmux to run for real."""
    src = inspect.getsource(server.run_test_plan)
    assert '"hidden_by": "verify"' in src
    assert '_hidden_sessions.set_hidden_by, title, "verify"' in src


def test_backfill_flags_only_runs_a_checklist_names(monkeypatch):
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {
            "verify-p1-abc1234": object(),  # current run of a plan
            "verify-p2-0000000": object(),  # an older run in p2's history
            "verify-foo": object(),  # a person's own session
            "verify-p9-dead000": object(),  # no checklist names it
            "api": object(),
        },
    )
    plans = [
        {"id": "p1", "run_session": "verify-p1-abc1234", "runs": []},
        {
            "id": "p2",
            "run_session": "",
            "runs": [{"session": "verify-p2-0000000"}, "junk", {"session": ""}],
        },
        {"id": "p3", "run_session": "verify-foo-not-live", "runs": None},
    ]
    monkeypatch.setattr(server._test_plans, "list_plans", lambda: plans)
    assert server._backfill_hidden_verify_sessions() == 2
    assert hidden_sessions.all_hidden() == {
        "verify-p1-abc1234": "verify",
        "verify-p2-0000000": "verify",
    }
    # Idempotent: a second start flags nothing new.
    assert server._backfill_hidden_verify_sessions() == 0


def test_create_docs_name_the_field():
    """The accepted-keys docstring and the API doc both carry the field, so
    the next reader finds it where the other create fields are."""
    from backend.web.core import session_create

    assert "``hidden_by``" in (session_create.create.__doc__ or "")
