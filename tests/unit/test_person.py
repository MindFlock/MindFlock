"""The person layer (:mod:`backend.web.core.person`, docs/people.md).

One person id shared by all of a person's devices, stamped on what MindFlock
does: the settings record and its migration, the sync tie-break (both sides
give the same answer), the ``X-MindFlock-Person`` header round trip, fleet
claims, git identity on MindFlock's own commits, ``whoami`` and the session
stamp. The pairing handshake runs end to end in ``test_fleet.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend import session
from backend.config import settings as store
from backend.session.instance import InstanceOptions, new_instance
from backend.web import server
from backend.web.core import auth as web_auth
from backend.web.core import fleet_claims, git_merge, person, person_sessions
from backend.web.core import remote, settings_sync, thread
from backend.web.core import snapshot as snapshot_mod
from tests.unit._mcp_fakes import FakeCtx, make_box, row
from tests.unit.test_hidden_sessions import _NewInst

A = "00000000-0000-4000-8000-00000000000a"
B = "ffffffff-ffff-4fff-8fff-fffffffffffb"


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(person, "_LOGGED", set())
    monkeypatch.setattr(person_sessions, "_path", lambda: str(tmp_path / "sp.json"))
    monkeypatch.setattr(person_sessions, "_PERSON", None)
    store.invalidate()
    yield
    monkeypatch.setattr(person_sessions, "_PERSON", None)
    store.invalidate()


def _doc() -> dict:
    p = store.settings_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _write(doc: dict) -> None:
    p = store.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc))
    store.invalidate()


# --------------------------------------------------------------------------- #
# the record + migration
# --------------------------------------------------------------------------- #
def test_an_existing_store_gets_one_id_on_first_load_and_keeps_it():
    _write({"ui": {"accent": "teal"}})
    pid = person.ensure_id()
    assert store.person_id(pid) == pid
    assert _doc() == {"ui": {"accent": "teal"}, "person": {"id": pid}}
    assert person.ensure_id() == pid  # made once
    assert person.known_id() == pid


def test_known_id_never_writes():
    assert person.known_id() == ""
    assert not store.settings_path().exists()


def test_an_unreadable_store_is_never_saved_over():
    p = store.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    store.invalidate()
    assert person.ensure_id() == ""
    assert p.read_text() == "{not json"


def test_the_record_is_cleaned_on_load():
    s = store.Settings.from_dict(
        {
            "person": {
                "id": A.upper(),
                "name": " Ada\x07 ",
                "git_name": "Ada <x>",
                "git_email": "not an email",
            }
        }
    )
    assert s.person.id == A  # lower-cased
    assert s.person.name == "Ada"
    assert s.person.git_name == "Ada x"
    assert s.person.git_email == ""
    assert store.Settings.from_dict({"person": {"id": "nope"}}).person.id == ""
    assert store.Settings().to_dict() == {}  # nothing emitted while unset


def test_update_validates_and_never_sets_the_id():
    pid = person.ensure_id()
    out = person.update(name="Ada", git_name="Ada L", git_email="ada@example.com")
    assert out == {
        "id": pid,
        "name": "Ada",
        "git_name": "Ada L",
        "git_email": "ada@example.com",
    }
    with pytest.raises(ValueError):
        person.update(git_email="nope")
    assert person.update(name="")["name"] == ""


def test_the_settings_post_cannot_set_the_id():
    from backend.web.addons import settings as settings_addon

    pid = person.ensure_id()
    settings_addon._apply_post({"person": {"id": B, "name": "Ada"}})
    store.invalidate()
    got = store.load_settings().person
    assert got.id == pid and got.name == "Ada"


def test_person_routes(monkeypatch):
    client = TestClient(server.app)
    pid = person.ensure_id()
    assert client.get("/api/person").json()["id"] == pid
    r = client.post("/api/person", json={"name": "Ada", "id": B})
    assert r.status_code == 200 and r.json()["name"] == "Ada"
    assert r.json()["id"] == pid  # the id is not the route's to set
    r = client.post("/api/person", json={"git_email": "x y"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# agreeing on one id over settings sync
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("auto", ["", "laptop", "rig", "elsewhere"])
def test_the_tie_break_is_the_same_from_both_sides(auto):
    here = person.winner(
        A, B, self_device="laptop", their_device="rig", automation_device=auto
    )
    there = person.winner(
        B, A, self_device="rig", their_device="laptop", automation_device=auto
    )
    assert here == there
    if auto == "laptop":
        assert here == A
    elif auto == "rig":
        assert here == B
    else:
        assert here == min(A, B)


def test_the_automation_device_wins_even_with_the_larger_id():
    assert (
        person.winner(A, B, self_device="l", their_device="r", automation_device="r")
        == B
    )


def test_reconcile_switches_once_and_logs_once(monkeypatch):
    logged = []
    monkeypatch.setattr(person, "_info", lambda fmt, *a: logged.append(fmt % a))
    _write({"person": {"id": B}})
    assert person.reconcile({"id": A}, "rig", "laptop") is True
    assert person.known_id() == A
    assert person.reconcile({"id": A}, "rig", "laptop") is False  # agreed now
    assert len(logged) == 1 and B in logged[0] and A in logged[0]


def test_reconcile_keeps_the_winner_here():
    _write({"person": {"id": A}})
    assert person.reconcile({"id": B}, "rig", "laptop") is False
    assert person.known_id() == A
    assert person.reconcile({"id": "junk"}, "rig", "laptop") is False


def test_the_sync_export_carries_the_id_and_a_pass_settles_it(monkeypatch):
    _write({"person": {"id": B}, "github": {"automation_device": "rig"}})
    monkeypatch.setattr(settings_sync, "_self_key", lambda: "laptop")
    monkeypatch.setattr(settings_sync, "_fleet_id", lambda: "f" * 16)
    assert settings_sync.export()["person"] == {"id": B}
    # rig runs the automation: its id wins here even though B > A is not the rule.
    assert settings_sync._settle_person({"device": "rig", "person": {"id": A}})
    assert person.known_id() == A


def test_name_and_git_identity_sync_but_the_id_is_agreed():
    assert settings_sync.SYNCED["person"] == ("name", "git_name", "git_email")
    assert settings_sync.LOCAL["person"] == ("id",)


# --------------------------------------------------------------------------- #
# the header: sent beside the device key, recorded on the request
# --------------------------------------------------------------------------- #
def test_proxied_requests_carry_the_person(monkeypatch):
    monkeypatch.setattr(remote, "self_identity", lambda: {"key": "laptop"})
    monkeypatch.setattr(remote, "_fleet_key_for", lambda d: "")
    monkeypatch.setattr(remote, "token_for", lambda d: "")
    assert remote.PERSON_HEADER not in remote._headers_for("rig")  # no id yet
    pid = person.ensure_id()
    h = remote._headers_for("rig")
    assert h[remote.REMOTE_HEADER] == "laptop" and h[remote.PERSON_HEADER] == pid


def _echo_app():
    app = FastAPI()

    @app.get("/api/who")
    def who(request: Request):
        return {"person": web_auth.request_person(request)}

    app.add_middleware(web_auth.AuthMiddleware)
    return TestClient(app)


def test_the_receiving_side_records_the_person(monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    mine = person.ensure_id()
    c = _echo_app()
    sent = {remote.REMOTE_HEADER: "rig", remote.PERSON_HEADER: B.upper()}
    assert c.get("/api/who", headers=sent).json() == {"person": B}
    # Only beside the remote marker: a browser can't name someone else.
    assert c.get("/api/who", headers={remote.PERSON_HEADER: B}).json() == {
        "person": mine
    }
    # Junk is dropped, not refused.
    junk = {remote.REMOTE_HEADER: "rig", remote.PERSON_HEADER: "<script>"}
    r = c.get("/api/who", headers=junk)
    assert r.status_code == 200 and r.json() == {"person": mine}


def test_the_person_header_is_not_a_credential(monkeypatch):
    """With the gate on, a request naming this very person is still 401."""
    monkeypatch.setenv("MINDFLOCK_AUTH", "1")
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    pid = person.ensure_id()
    c = _echo_app()
    r = c.get(
        "/api/who", headers={remote.REMOTE_HEADER: "rig", remote.PERSON_HEADER: pid}
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# fleet claims
# --------------------------------------------------------------------------- #
def test_claims_carry_the_person_and_read_old_devices(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(remote, "self_identity", lambda: {"key": "laptop"})
    pid = person.ensure_id()
    assert fleet_claims.claims_json()["person"] == pid
    devs = [{"key": "rig", "host": "Rig"}, {"key": "old", "host": "Old"}]
    monkeypatch.setattr(remote, "fleet_devices", lambda: [dict(d) for d in devs])
    monkeypatch.setattr(remote, "connected_devices", lambda: [])
    answers = {
        "rig": {
            "device": "rig",
            "person": A,
            "claims": {
                "sc-1": {"kind": "session", "since": 1.0},
                "sc-2": {"kind": "session", "since": 1.0, "person": B},
            },
        },
        "old": {"device": "old", "claims": {"sc-3": {"kind": "session", "since": 1}}},
    }

    async def get_json(dev, path, timeout=3.0):
        return 200, answers[dev["key"]]

    monkeypatch.setattr(remote, "get_json", get_json)
    fleet_claims.clear_cache()
    got = asyncio.run(fleet_claims.fleet_claims(fresh=True))
    assert got["sc-1"][0]["person"] == A  # the answering device's person
    assert got["sc-2"][0]["person"] == B  # the claim's own
    assert got["sc-3"][0]["person"] == ""  # a device that predates people
    fleet_claims.clear_cache()


# --------------------------------------------------------------------------- #
# git identity on MindFlock's own commits
# --------------------------------------------------------------------------- #
def test_git_identity_args_need_both_name_and_email():
    assert person.git_identity_args() == []
    person.update(git_name="Ada L")
    assert person.git_identity_args() == []
    person.update(git_email="ada@example.com")
    assert person.git_identity_args() == [
        "-c",
        "user.name=Ada L",
        "-c",
        "user.email=ada@example.com",
    ]


def test_the_fast_track_commit_is_authored_as_the_person():
    plain = server._commit_shell_command()
    assert "git commit -F .mindflock_commit_msg" in plain  # unchanged when unset
    person.update(git_name="Ada L", git_email="ada@example.com")
    cmd = server._commit_shell_command("black")
    assert (
        "SKIP=black git -c 'user.name=Ada L' -c user.email=ada@example.com "
        "commit -F .mindflock_commit_msg" in cmd
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.name", "Repo Config")
    _git(r, "config", "user.email", "repo@example.com")
    (r / "a.txt").write_text("a\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _author(repo):
    return _git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>")


def test_team_run_commits_use_the_person_else_the_repo(repo):
    (repo / "b.txt").write_text("b\n")
    head = _git(repo, "rev-parse", "HEAD")
    res = git_merge.commit_paths(str(repo), ["b.txt"], "piece one", head)
    assert res["result"] == "committed", res
    assert (
        _author(repo) == "Repo Config <repo@example.com>|Repo Config <repo@example.com>"
    )
    person.update(git_name="Ada L", git_email="ada@example.com")
    (repo / "c.txt").write_text("c\n")
    head = _git(repo, "rev-parse", "HEAD")
    res = git_merge.commit_paths(str(repo), ["c.txt"], "piece two", head)
    assert res["result"] == "committed", res
    assert _author(repo) == "Ada L <ada@example.com>|Ada L <ada@example.com>"
    # The integration merge too.
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "d.txt").write_text("d\n")
    _git(repo, "add", "d.txt")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "main")
    out = git_merge.merge_into(str(repo), "side", message="Merge side")
    assert out["result"] == "clean", out
    assert _author(repo) == "Ada L <ada@example.com>|Ada L <ada@example.com>"


def test_peer_export_is_authored_as_the_person_when_known():
    from backend.peer import share

    assert share._export_author() is None
    assert share._author_env(None)["GIT_AUTHOR_NAME"] == share.AUTHOR_NAME
    person.update(git_name="Ada L", git_email="ada@example.com")
    env = share._author_env(share._export_author())
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Ada L"
    assert env["GIT_AUTHOR_EMAIL"] == "ada@example.com"


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
def test_whoami_names_the_person():
    box, api, clock = make_box([row("orch"), row("w1", parent="orch", person=A)])
    assert box.whoami({}, FakeCtx(clock))["person"] is None  # an older server
    api.top_routes[("GET", "/api/person")] = {"id": A, "name": "Ada", "git_name": "x"}
    assert box.whoami({}, FakeCtx(clock))["person"] == {"id": A, "name": "Ada"}
    rows = box.list_sessions({"filter": "all"}, FakeCtx(clock))["sessions"]
    by = {r["title"]: r for r in rows}
    assert by["w1"]["person"] == A and "person" not in by["orch"]


# --------------------------------------------------------------------------- #
# the session stamp
# --------------------------------------------------------------------------- #
def test_the_store_records_resets_and_drops():
    person_sessions.set_person("a", A)
    assert person_sessions.person("a") == A
    person_sessions.set_person("a", "junk")  # not an id: forgotten
    assert person_sessions.person("a") == ""
    person_sessions.set_person("b", B)
    server._forget_session_state("b")
    assert person_sessions.person("b") == ""


def test_rows_carry_the_person(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    inst = new_instance(InstanceOptions(title="w", path=str(tmp_path), program="bash"))
    assert snapshot_mod._instance_json(inst, cheap=True)["person"] == ""
    person_sessions.set_person("w", A)
    assert snapshot_mod._instance_json(inst, cheap=True)["person"] == A


@pytest.fixture
def create(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(server._events.BUS, "emit", lambda name, **kw: None)
    monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
    monkeypatch.setattr(thread, "_SEEDS", {})
    monkeypatch.setattr(session, "NewInstance", _NewInst)
    monkeypatch.setattr(server, "_instance_json", lambda i, **k: {"title": i.Title})
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
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    client = TestClient(server.app)

    def post(headers=None, **body):
        body.setdefault("program", "claude")
        body.setdefault("repo_path", str(tmp_path))
        return client.post("/api/instances", json=body, headers=headers or {})

    return SimpleNamespace(post=post)


def test_a_new_session_records_who_created_it(create):
    mine = person.ensure_id()
    assert create.post(title="mine", person=B).status_code == 202
    assert person_sessions.person("mine") == mine  # never from the payload
    relayed = {remote.REMOTE_HEADER: "rig", remote.PERSON_HEADER: A}
    assert create.post(headers=relayed, title="theirs").status_code == 202
    assert person_sessions.person("theirs") == A
