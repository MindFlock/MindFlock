"""A rename IS the title (backend.web.core.rename + rename_stores).

Never touches tmux (the tmux steps are monkeypatched), the live engine
(a stand-in engine), or the real config dir (HOME and every store file point
into tmp_path).
"""

from __future__ import annotations

import json
import os
import re
import threading
import types
from pathlib import Path

import pytest

from backend.session import tmux
from backend.web.core import aliases, events
from backend.web.core import rename
from backend.web.core import rename_stores  # noqa: F401 — registers the stores

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """HOME (so config.GetConfigDir) in tmp_path, every load-once store
    reloaded from there, and no tmux."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / ".mindflock").mkdir(parents=True)
    from backend.web.core import (
        asking,
        guests,
        hidden_sessions,
        ownership,
        person_sessions,
        shared_branch,
    )
    from backend.web.core import budget
    from backend.web.core import terminal

    for mod, attr in (
        (aliases, "_ALIASES"),
        (hidden_sessions, "_HIDDEN"),
        (person_sessions, "_PERSON"),
        (shared_branch, "_SHARED"),
        (ownership, "_OWNERS"),
        (guests, "_LINKS"),
        (budget, "_BUDGET_OV"),
        (rename, "_MAP"),
    ):
        monkeypatch.setattr(mod, attr, None)
    monkeypatch.setattr(asking, "_STORE_LOADED", True)
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", tmp_path / "exit")
    monkeypatch.setattr(rename, "_tmux_rename", lambda o, n: True)
    monkeypatch.setattr(rename, "_pane_pid", lambda name: "4242")
    yield


class _Inst:
    def __init__(self, title, parent="", branch=""):
        self.Title = title
        self.Parent = parent
        self.Branch = branch
        self.PeerShare = ""
        self._tmux_session = types.SimpleNamespace(
            sanitized_name=tmux.to_mindflock_tmux_name(title)
        )
        self._git_worktree = types.SimpleNamespace(sessionName=title)


class _Engine:
    def __init__(self, *insts):
        self.instances = {i.Title: i for i in insts}
        self.lock = threading.RLock()
        self.saves = []

    def save(self, exclude_titles=()):
        self.saves.append(tuple(exclude_titles))


def _srv(*insts):
    """A stand-in for backend.web.server: what core.rename reaches for."""
    from backend.web.core import peer_guard

    eng = _Engine(*insts)
    srv = types.SimpleNamespace(
        ENGINE=eng,
        _aliases=aliases,
        _events=events,
        _peer_guard=peer_guard,
        _session_branches=lambda: {
            t: getattr(i, "Branch", "") for t, i in eng.instances.items()
        },
    )
    return srv


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    ["", "  ", " lead", "trail ", "a/b", "a\\b", "dev::x", "a\x07b", "..", "x" * 121],
)
def test_invalid_names(name):
    assert rename.title_error(name)


@pytest.mark.parametrize(
    "name", ["fix-login", "Fix Login 2", "sc-123.v2", "a:b", "修复"]
)
def test_valid_names(name):
    assert rename.title_error(name) == ""


# --------------------------------------------------------------------------- #
# The rename
# --------------------------------------------------------------------------- #
def test_rename_moves_engine_key_instance_and_children():
    lead, kid = _Inst("old"), _Inst("kid", parent="old")
    srv = _srv(lead, kid)
    res = rename.rename_session(srv, "old", "new")
    assert res.status == 200 and res.body == {"title": "new", "from": "old"}
    assert set(srv.ENGINE.instances) == {"new", "kid"}
    assert srv.ENGINE.instances["new"] is lead and lead.Title == "new"
    assert lead._tmux_session.sanitized_name == "mindflock_new"
    assert lead._git_worktree.sessionName == "new"
    assert kid.Parent == "new"
    # Saved with the old title excluded = tombstoned for co-running servers.
    assert srv.ENGINE.saves == [("old",)]


def test_rename_refusals():
    srv = _srv(_Inst("a"), _Inst("b"), _Inst("c d"))
    assert rename.rename_session(srv, "nope", "x").status == 404
    assert rename.rename_session(srv, "a", "bad/name").status == 400
    r = rename.rename_session(srv, "a", "b")
    assert r.status == 409 and r.body["session"] == "b"
    # Two titles that share one tmux name ("cd" and "c d") cannot coexist.
    r = rename.rename_session(srv, "a", "cd")
    assert r.status == 409 and r.body["session"] == "c d"
    # A name another session SHOWS (its alias) is taken too.
    aliases.set_alias("b", "Login fix")
    assert rename.rename_session(srv, "a", "Login fix").status == 409
    assert rename.rename_session(srv, "dev::a", "x").status == 409
    assert srv.ENGINE.saves == []


def test_rename_refuses_peer_session():
    inst = _Inst("shared")
    inst.PeerShare = "share-1"
    srv = _srv(inst)
    r = rename.rename_session(srv, "shared", "mine")
    assert r.status == 409 and r.body.get("peer_share")


def test_same_name_is_a_noop():
    srv = _srv(_Inst("a"))
    r = rename.rename_session(srv, "a", " a ")
    assert r.status == 200 and r.body["unchanged"] is True
    assert srv.ENGINE.saves == []


def test_tmux_refusal_changes_nothing(monkeypatch):
    monkeypatch.setattr(rename, "_tmux_rename", lambda o, n: False)
    srv = _srv(_Inst("a"))
    assert rename.rename_session(srv, "a", "b").status == 409
    assert set(srv.ENGINE.instances) == {"a"}


def test_rename_emits_session_renamed_and_rekeys_snapshot(monkeypatch):
    events.set_sessions_snapshot(
        [{"title": "old", "branch": "b"}, {"title": "kid", "parent": "old"}]
    )
    seen = []
    unsub = events.BUS.subscribe(
        lambda e: seen.append(e) if e["event"] == "session.renamed" else None
    )
    try:
        rename.rename_session(
            srv := _srv(_Inst("old"), _Inst("kid", "old")), "old", "new"
        )
    finally:
        unsub()
        snap = events.sessions_snapshot()
        events.set_sessions_snapshot([])
    assert srv.ENGINE.instances["kid"].Parent == "new"
    assert [(e["session"], e["data"]["from"], e["data"]["to"]) for e in seen] == [
        ("old", "old", "new")
    ]
    assert {r["title"] for r in snap} == {"new", "kid"}
    assert [r for r in snap if r["title"] == "kid"][0]["parent"] == "new"


def test_alias_is_dropped_by_the_rename():
    aliases.set_alias("old", "Pretty")
    rename.rename_session(_srv(_Inst("old")), "old", "Pretty")
    assert aliases.all_aliases() == {}


# --------------------------------------------------------------------------- #
# Files named after the tmux name
# --------------------------------------------------------------------------- #
def test_marker_files_follow_the_tmux_name(tmp_path):
    from backend.providers import activity_markers, thread_markers
    from backend.web.core import voice

    act, thr, vf = (
        activity_markers.marker_dir(),
        thread_markers.marker_dir(),
        voice.flag_dir(),
    )
    for d in (act, thr, vf):
        d.mkdir(parents=True, exist_ok=True)
    (act / "mindflock_old.json").write_text("{}")
    (act / "mindflock_old.reply.json").write_text("{}")
    (act / "mindflock_old_sh.json").write_text("{}")
    (act / "mindflock_old-2.json").write_text("{}")  # another session's
    (thr / "mindflock_old.thread").write_text("sid")
    (thr / "mindflock_old@work.thread").write_text("sid2")
    (vf / "mindflock_old.flock").write_text("x")
    rename.rename_session(_srv(_Inst("old")), "old", "new")
    assert sorted(os.listdir(act)) == [
        "mindflock_new.json",
        "mindflock_new.reply.json",
        "mindflock_new_sh.json",
        "mindflock_old-2.json",
    ]
    assert sorted(os.listdir(thr)) == [
        "mindflock_new.thread",
        "mindflock_new@work.thread",
    ]
    assert os.listdir(vf) == ["mindflock_new.flock"]


def test_running_agents_exit_code_lands_under_the_new_name(tmp_path):
    from backend.web.core import terminal

    rename.rename_session(_srv(_Inst("old")), "old", "new")
    old_p = terminal._exit_marker_path("mindflock_old")
    assert old_p.is_symlink()
    # What the running wrapper does when the agent exits: `echo $? > old`.
    with open(old_p, "w") as fh:
        fh.write("137\n")
    assert terminal._read_exit_marker("mindflock_new") == 137
    # The session going takes the link with it.
    rename.forget("new")
    assert not old_p.is_symlink()


def test_finished_agents_exit_marker_is_moved():
    from backend.web.core import terminal

    p = terminal._exit_marker_path("mindflock_old")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("0\n")
    rename.rename_session(_srv(_Inst("old")), "old", "new")
    assert not p.exists() and terminal._read_exit_marker("mindflock_new") == 0


def test_mcp_run_file_carries_the_new_identity():
    from backend.providers import mcp_attach

    old_p = mcp_attach.run_file_path("mindflock_old")
    os.makedirs(os.path.dirname(old_p), exist_ok=True)
    doc = {
        "mcpServers": {
            mcp_attach.SERVER_NAME: {"env": {"MINDFLOCK_SESSION_TITLE": "old"}},
            "other": {"env": {}},
        }
    }
    Path(old_p).write_text(json.dumps(doc))
    mcp_attach._LAUNCHED["mindflock_old"] = True
    rename.rename_session(_srv(_Inst("old")), "old", "new")
    for p in (old_p, mcp_attach.run_file_path("mindflock_new")):
        got = json.loads(Path(p).read_text())
        assert got["mcpServers"][mcp_attach.SERVER_NAME]["env"] == {
            "MINDFLOCK_SESSION_TITLE": "new"
        }
    assert "mindflock_new" in mcp_attach._LAUNCHED
    rename.forget("new")
    assert not os.path.exists(old_p)


# --------------------------------------------------------------------------- #
# renamed_from: old titles keep reaching the session
# --------------------------------------------------------------------------- #
def test_renamed_from_chain_overlay_and_resolve():
    srv = _srv(_Inst("a"))
    rename.rename_session(srv, "a", "b")
    rename.rename_session(srv, "b", "c")
    assert rename.resolve("a") == "c" and rename.resolve("b") == "c"
    rows = [{"title": "c"}, {"title": "x", "device": "mac"}]
    rename.overlay(rows)
    assert rows[0]["renamed_from"] == ["a", "b"] and "renamed_from" not in rows[1]
    # Persisted.
    rename._reset_for_tests()
    assert rename.resolve("a") == "c"


def test_rewrite_path_only_for_dead_old_titles():
    rename._note_renamed("old", "new", "")
    live = {"new"}
    assert (
        rename.rewrite_path("/api/instances/old/send", live)
        == "/api/instances/new/send"
    )
    assert rename.rewrite_path("/api/instances/old", live) == "/api/instances/new"
    assert (
        rename.rewrite_path("/api/instances/other/x", live) == "/api/instances/other/x"
    )
    assert rename.rewrite_path("/api/sessions/old", live) == "/api/sessions/old"
    # The old name is a live session again: it is THAT session's now.
    assert rename.rewrite_path("/api/instances/old/x", {"old", "new"}) == (
        "/api/instances/old/x"
    )


def test_prune_drops_gone_and_relaunched():
    rename._note_renamed("a", "b", "100")
    rename._note_renamed("c", "d", "200")
    rename._note_renamed("e", "f", "300")
    pids = {"mindflock_b": "100", "mindflock_d": "999"}
    dropped = rename.prune({"b", "d"}, pane_pid=lambda n: pids.get(n, ""))
    # d relaunched (pane pid moved on); f is gone; b still runs as launched.
    assert sorted(dropped) == ["c", "e"]
    assert rename.resolve("a") == "b"


# --------------------------------------------------------------------------- #
# The stores
# --------------------------------------------------------------------------- #
PID = "01234567-89ab-cdef-0123-456789abcdef"


def test_stores_move_with_the_rename():
    from backend.web.core import (
        autopilot,
        finished_children,
        hidden_sessions,
        mailbox,
        person_sessions,
        ports,
        prompt_queue,
        sleep,
        worker_order,
    )

    hidden_sessions.set_hidden_by("old", "verify")
    person_sessions.set_person("old", PID)
    with sleep._LOCK:
        sleep._load()["old"] = {"since": 1.0, "reason": "idle", "freed_mb": 1}
        sleep._persist()
    prompt_queue._save({"old": {"items": [{"id": "1", "text": "hi"}]}})
    autopilot._save({"old": {"depth": "commit"}})
    port = ports.allocate("old")
    with mailbox._mutate() as data:
        mailbox._box(data, "old", create=True)["messages"].append(
            {"from": "lead", "to": "old", "text": "x"}
        )
        mailbox._box(data, "lead", create=True)["messages"].append(
            {"from": "old", "to": "lead", "text": "y"}
        )
    finished_children._save({"old": [{"title": "w1"}], "lead": [{"title": "old"}]})
    with worker_order.edit() as data:
        data.setdefault("parents", {})["lead"] = {
            "workers": {
                "old": {"after": ["w0"]},
                "w2": {"after": ["old"], "why": {"old": "r"}},
            },
            "planned": {"old": ["w3"]},
        }

    rename.rename_session(_srv(_Inst("old"), _Inst("lead")), "old", "new")

    assert hidden_sessions._load() == {"new": "verify"}
    assert person_sessions._load() == {"new": PID}
    assert list(sleep._load()) == ["new"]
    assert list(prompt_queue._load()) == ["new"]
    assert list(autopilot._load()) == ["new"]
    assert ports.get("new") == port and ports.get("old") is None
    boxes = mailbox._load()[0]["boxes"]
    assert set(boxes) == {"new", "lead"}
    assert boxes["new"]["messages"][0]["to"] == "new"
    assert boxes["lead"]["messages"][0]["from"] == "new"
    assert finished_children._load() == {
        "new": [{"title": "w1"}],
        "lead": [{"title": "new"}],
    }
    lead = worker_order.load()["parents"]["lead"]
    assert set(lead["workers"]) == {"new", "w2"}
    assert lead["workers"]["w2"] == {"after": ["new"], "why": {"new": "r"}}
    assert lead["planned"] == {"new": ["w3"]}


def test_flights_guests_and_fences_follow():
    from backend.config import red_zones
    from backend.web.core import flights, guests

    with flights._LOCK:
        flights._save_flights_locked(
            {
                "f1": {
                    "id": "f1",
                    "session": "old",
                    "steps": [{"title": "old"}],  # a step LABEL — left alone
                    "step_state": [{"detail": {"children": ["old", "w"]}}],
                }
            }
        )
    with guests._LOCK:
        guests._load()["L1"] = {"token": "t", "sessions": ["old", "x"]}
        guests._persist()
    wt = str(Path(os.environ["HOME"]) / "wt")
    os.makedirs(wt, exist_ok=True)
    red_zones.set_session_fence(
        wt, "mindflock_w", ["src/**"], owner="orch:old", by="old"
    )
    red_zones.set_session_fence(wt, "mindflock_old", ["docs/**"], owner="orch:lead")

    rename.rename_session(_srv(_Inst("old"), _Inst("w")), "old", "new")

    f = flights._load_flights_locked()["f1"]
    assert f["session"] == "new" and f["steps"] == [{"title": "old"}]
    assert f["step_state"][0]["detail"]["children"] == ["new", "w"]
    assert guests._load()["L1"]["sessions"] == ["new", "x"]
    sessions = red_zones._load()["worktrees"][os.path.realpath(wt)]["sessions"]
    assert set(sessions) == {"mindflock_w", "mindflock_new"}
    assert sessions["mindflock_w"]["owner"] == "orch:new"
    assert sessions["mindflock_w"]["by"] == "new"


def test_a_failing_store_never_stops_the_rename(monkeypatch):
    def boom(old, new):
        raise RuntimeError("x")

    rename.register("boom", boom)
    try:
        srv = _srv(_Inst("old"))
        r = rename.rename_session(srv, "old", "new")
    finally:
        rename.REGISTRY[:] = [s for s in rename.REGISTRY if s.name != "boom"]
    assert r.status == 200 and r.body["failed_stores"] == ["boom"]
    assert "new" in srv.ENGINE.instances


# --------------------------------------------------------------------------- #
# A new title-keyed store must be registered
# --------------------------------------------------------------------------- #
#: Config-dir JSON files that are NOT keyed by session title, and why.
NOT_SESSION_KEYED = {
    "state.json": "the engine itself — renamed by core.rename directly",
    "instances.json": "engine (legacy name)",
    "config.json": "engine config",
    "settings.json": "settings (no per-session keys)",
    "install.json": "install record",
    "pricing.json": "model prices",
    "recently_closed.json": "a closed session keeps the name it closed under",
    "remote_devices.json": "devices",
    "fleet.json": "devices",
    "fleet_update.json": "devices",
    "settings_sync.json": "settings sync",
    "update_announced.json": "release notes",
    "update.json": "self-update",
    "session_templates.json": "templates (named by template)",
    "plans.json": "flight plans hold no session",
    "trigger_state.json": "flight triggers by plan",
    "hook_tokens.json": "flight hook tokens by flight",
    "sky_notes.json": "sky notes by plan",
    "window_refresh.json": "by provider",
    "dbclient.json": "db client",
    "dbclient_tests.json": "db client",
    "todos.json": "assistant todos",
    "guest_links.json": "registered (sessions lists)",
    "automation_here.json": "pipeline hand-over",
    "auth.json": "provider credentials",
    "hooks.json": "provider hook config",
    "endpoint.json": "shared MCP bridge",
    "bridge.json": "shared MCP bridge",
    "servers.json": "shared MCP bridge",
    "mcp.json": "peer MCP config",
    "links.json": "peer links",
    "drivers.json": "db client drivers",
    "packages.json": "voice packages",
    "direct_url.json": "package metadata",
    "rename_migration.json": "core.rename's own once-marker",
    "test_plans.json": "registered",
    # Frontend / editor project files the server reads, not stores.
    "package.json": "npm manifest",
    "tsconfig.json": "TypeScript config",
    "jsconfig.json": "editor config",
}


def _store_filenames():
    """``"<name>.json"`` literals in the server-side source: every JSON store."""
    names = set()
    for sub in (
        "backend/web",
        "backend/config",
        "backend/providers",
        "backend/ticket_ingestion",
    ):
        for p in (REPO / sub).rglob("*.py"):
            for m in re.finditer(
                r"[\"']([a-z][a-z0-9_]*\.json)[\"']", p.read_text("utf-8")
            ):
                names.add(m.group(1))
    return names


def test_every_json_store_is_registered_or_explained():
    registered = rename.registered_files()
    unknown = sorted(
        n
        for n in _store_filenames()
        if n not in registered and n not in NOT_SESSION_KEYED
    )
    assert not unknown, (
        "new JSON store(s) %s: if they key anything by session title, register a "
        "rename for them in backend/web/core/rename_stores.py; else add them to "
        "NOT_SESSION_KEYED here with the reason" % unknown
    )


def test_memory_dicts_still_exist():
    """The in-memory entries name module attributes; a renamed attribute would
    silently stop moving."""
    import importlib

    for modname, attr in rename_stores._MEMORY_DICTS + rename_stores._MEMORY_TMUX_DICTS:
        assert isinstance(getattr(importlib.import_module(modname), attr), dict), (
            modname,
            attr,
        )


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
def test_migration_renames_free_valid_aliases_once(tmp_path):
    srv = _srv(_Inst("t1"), _Inst("t2"), _Inst("t3"), _Inst("taken"))
    aliases.set_alias("t1", "Login fix")
    aliases.set_alias("t2", "bad/name")
    aliases.set_alias("t3", "taken")
    aliases.set_alias("gone", "Whatever")
    assert rename.migrate_once(srv) == {"t1": "Login fix"}
    assert "Login fix" in srv.ENGINE.instances
    # The unmigratable stay aliases (displayName keeps showing them).
    assert aliases.all_aliases() == {
        "t2": "bad/name",
        "t3": "taken",
        "gone": "Whatever",
    }
    # Recorded: a second start does nothing.
    aliases.set_alias("t2", "fine-now")
    assert rename.migrate_once(srv) == {}


# --------------------------------------------------------------------------- #
# MCP identity
# --------------------------------------------------------------------------- #
def _identity(title, pane_session=""):
    from backend.mcp.identity import Identity

    env = {"MINDFLOCK_SESSION_TITLE": title, "MINDFLOCK_MCP_MANAGED": "1"}
    if pane_session:
        env.update(TMUX="/tmp/x,1,0", TMUX_PANE="%1")

    def run(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=pane_session + "\n")

    return Identity(env=env, run=run)


def test_identity_follows_a_rename():
    rows = [{"title": "new", "tmux_name": "mindflock_new", "renamed_from": ["old"]}]
    assert _identity("old", "mindflock_new").resolve(rows) == "new"
    assert _identity("old").resolve(rows) == "new"


def test_identity_with_a_namesake_in_the_old_name():
    rows = [
        {"title": "new", "tmux_name": "mindflock_new", "renamed_from": ["old"]},
        {"title": "old", "tmux_name": "mindflock_old"},
    ]
    # We run in the renamed session's pane: we are it, not the namesake.
    assert _identity("old", "mindflock_new").resolve(rows) == "new"
    # The namesake's own MCP is the namesake.
    assert _identity("old", "mindflock_old").resolve(rows) == "old"


def test_identity_renamed_but_in_another_pane_fails_closed():
    rows = [
        {"title": "new", "tmux_name": "mindflock_new", "renamed_from": ["old"]},
        {"title": "x", "tmux_name": "mindflock_x"},
    ]
    ident = _identity("old", "mindflock_x")
    assert ident.resolve(rows) is None and "renamed" in ident.reason


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #
def test_route_and_old_title_routing(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    eng = _Engine(_Inst("old"))
    monkeypatch.setattr(server, "ENGINE", eng)
    client = TestClient(server.app)
    r = client.post("/api/instances/old/rename", json={"to": "new"})
    assert r.status_code == 200 and r.json() == {"title": "new", "from": "old"}
    assert list(eng.instances) == ["new"]
    r = client.post("/api/instances/new/rename", json={"to": ""})
    assert r.status_code == 400
    # A request still naming the old title reaches the renamed session.
    seen = []
    monkeypatch.setattr(
        server,
        "_inst_or_404",
        lambda t: (seen.append(t), (None, server.JSONResponse({}, 404)))[1],
    )
    client.get("/api/instances/old/budget")
    assert seen == ["new"]


def test_alias_seed_migrates_and_reports(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    eng = _Engine(_Inst("t1"), _Inst("t2"))
    monkeypatch.setattr(server, "ENGINE", eng)
    client = TestClient(server.app)
    r = client.post("/api/aliases", json={"aliases": {"t1": "Pretty", "t2": "a/b"}})
    assert r.json()["renamed"] == {"t1": "Pretty"}
    assert r.json()["aliases"] == {"t2": "a/b"}
    assert set(eng.instances) == {"Pretty", "t2"}
