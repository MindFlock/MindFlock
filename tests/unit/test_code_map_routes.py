"""Code Map + red-zone routes, the message delivery seam, the push/PR/merge
red-zone gate, prompt decoration and the server wiring of the reconcile loop.

Hermetic: ``server.ENGINE.instances`` is REPLACED (never merely extended — it
is the developer's live state.json) with fake instances over real throwaway
git repos; ``ENGINE.save`` and ``_register_task`` are neutralized; tmux is
never touched (the delivery collaborators are recorders).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.config import red_zones
from backend.providers import _tool_hook_src as th
from backend.session.storage import Status
from backend.web import server
from backend.web.core import code_map
from backend.web.core import red_zone_monitor as mon

client = TestClient(server.app)
# The real one, before the autouse fixture swaps in a coroutine-closer.
_REAL_REGISTER_TASK = server._register_task


def _git(cwd, *args) -> str:
    cp = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class _FakeInst:
    InPlace = False

    def __init__(self, title: str, wt: str, program: str = "claude"):
        self.Title = title
        self.Branch = "feature"
        self.Path = wt
        self.Status = Status.Running
        self.Program = program
        self.ExtraEnv: dict = {}
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setattr(server.ENGINE, "instances", {}, raising=False)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)

    def _close(coro):
        try:
            coro.close()
        except AttributeError:
            pass

    monkeypatch.setattr(server, "_register_task", _close)
    mon.reset_for_tests()
    yield
    mon.reset_for_tests()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _write(r, "app.py", "import lib\n")
    _write(r, "lib.py", "X = 1\n")
    _write(r, "config/settings.toml", "a = 1\n")
    _write(r, ".gitignore", "local.env\n")
    _write(r, "local.env", "SECRET=1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    base = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-qb", "feature")
    monkeypatch.setattr(server, "_session_fork_point", lambda inst, wt: base)
    monkeypatch.setattr(server, "_session_base_branch", lambda inst: "main")
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "")
    return r


@pytest.fixture
def sess(repo, monkeypatch):
    inst = _FakeInst("rzt", str(repo))
    server.ENGINE.instances["rzt"] = inst
    return inst


def _rid(repo) -> str:
    return red_zones.repo_identity(str(repo))[0]


# --------------------------------------------------------------------------- #
# Gates common to every per-session route
# --------------------------------------------------------------------------- #
_SESSION_ROUTES = [
    ("GET", "/api/instances/{t}/code-map"),
    ("GET", "/api/instances/{t}/code-map/live"),
    ("GET", "/api/instances/{t}/code-map/atlas"),
    ("GET", "/api/instances/{t}/code-map/file?path=app.py"),
    ("GET", "/api/instances/{t}/code-map/search?q=lib"),
    ("GET", "/api/instances/{t}/code-map/entry-points"),
    ("POST", "/api/instances/{t}/code-map/ask-plan"),
    ("POST", "/api/instances/{t}/code-map/go"),
    ("GET", "/api/instances/{t}/red-zones"),
    ("POST", "/api/instances/{t}/red-zones"),
    ("POST", "/api/instances/{t}/red-zones/preview"),
    ("DELETE", "/api/instances/{t}/red-zones/rz_x"),
    ("POST", "/api/instances/{t}/red-zones/rz_x/waive"),
    ("POST", "/api/instances/{t}/red-zones/allow"),
    ("POST", "/api/instances/{t}/red-zones/exempt"),
]


@pytest.mark.parametrize("method,path", _SESSION_ROUTES)
def test_unknown_title_404_and_no_worktree_409(method, path, tmp_path):
    body = {} if method == "POST" else None
    r = client.request(method, path.format(t="nope"), json=body)
    assert r.status_code == 404
    server.ENGINE.instances["nowt"] = _FakeInst("nowt", str(tmp_path / "missing"))
    r = client.request(method, path.format(t="nowt"), json=body)
    assert r.status_code == 409
    assert r.json()["error"] == "workspace not ready"


@pytest.mark.parametrize("method,path", _SESSION_ROUTES)
def test_no_git_409(method, path, monkeypatch):
    monkeypatch.setattr(server, "git_available", lambda: False)
    r = client.request(
        method, path.format(t="nope"), json={} if method == "POST" else None
    )
    assert r.status_code == 409
    assert "git is not installed" in r.json()["error"]


# --------------------------------------------------------------------------- #
# Snapshot + live
# --------------------------------------------------------------------------- #
def test_snapshot_shape_and_unchanged_fingerprint(sess, repo):
    r = client.get("/api/instances/rzt/code-map")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {
        "root",
        "repo",
        "fingerprint",
        "files",
        "truncated",
        "edges",
        "graph_partial",
        "langs",
    }
    rels = [f[0] for f in d["files"]]
    assert "app.py" in rels and "lib.py" in rels
    assert "local.env" not in rels  # ignored and not zoned
    assert [rels.index("app.py"), rels.index("lib.py")] in d["edges"]
    assert d["repo"]["id"] == _rid(repo)
    fp = d["fingerprint"]
    assert fp
    again = client.get("/api/instances/rzt/code-map", params={"fp": fp}).json()
    assert again == {"unchanged": True, "fingerprint": fp}
    # A zone changes the snapshot (its ignored files join) without touching git.
    red_zones.add_zone("repo", _rid(repo), "local.env")
    d2 = client.get("/api/instances/rzt/code-map", params={"fp": fp}).json()
    assert "unchanged" not in d2 and d2["fingerprint"] != fp
    row = next(f for f in d2["files"] if f[0] == "local.env")
    assert row[2] & code_map.FLAG_IGNORED


def test_live_shape_and_feed_relativized(sess, repo, monkeypatch):
    from backend.session import tmux

    red_zones.add_zone("repo", _rid(repo), "config/")
    _write(repo, "config/settings.toml", "a = 2\n")
    feed = red_zones.feed_path(tmux.to_mindflock_tmux_name("rzt"))
    os.makedirs(os.path.dirname(feed), exist_ok=True)
    now = time.time()
    with open(feed, "w") as f:
        f.write(
            json.dumps(
                {
                    "v": 1,
                    "ts": now,
                    "ev": "pre",
                    "tool": "Write",
                    "kind": "edit",
                    "writes": [str(repo / "app.py"), "/etc/elsewhere"],
                    "tp": "/secret/transcript.jsonl",
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        server._events,
        "sessions_snapshot",
        lambda: [{"title": "rzt", "activity": "working"}],
    )
    d = client.get("/api/instances/rzt/code-map/live", params={"since": 0}).json()
    assert set(d) >= {
        "now",
        "fingerprint",
        "repo",
        "changed",
        "feed",
        "plan",
        "off_plan",
        "zones",
        "breaches",
        "guard",
        "activity",
        "plan_supported",
        "others",
    }
    (rec,) = d["feed"]
    assert rec["writes"] == ["app.py"] and "tp" not in rec
    assert d["activity"] == "working"
    assert d["plan_supported"] is True
    assert [z["pattern"] for z in d["zones"]] == ["config/"]
    assert d["breaches"] == [
        {
            "path": "config/settings.toml",
            "pattern": "config/",
            "zone_id": d["zones"][0]["id"],
            "committed": False,
            "kind": "red",
        }
    ]
    assert d["guard"]["hard"] is True and d["guard"]["state"]
    assert any(c["path"] == "config/settings.toml" for c in d["changed"])
    # since= filters the feed.
    later = client.get(
        "/api/instances/rzt/code-map/live", params={"since": now + 1}
    ).json()
    assert later["feed"] == []
    # Committed breaches are flagged committed.
    _git(repo, "commit", "-qam", "cfg")
    d = client.get("/api/instances/rzt/code-map/live").json()
    assert d["breaches"][0]["committed"] is True


def test_live_feed_passes_the_subagent_fields_through(sess, repo, monkeypatch):
    """The Map gives each subagent its own bird: the parent's Agent call
    carries ``desc``/``atype``, a call made inside a subagent ``agent`` and
    ``agent_type`` — the live route must not strip them."""
    from backend.session import tmux

    feed = red_zones.feed_path(tmux.to_mindflock_tmux_name("rzt"))
    os.makedirs(os.path.dirname(feed), exist_ok=True)
    now = time.time()
    recs = [
        {
            "v": 1,
            "ts": now - 3,
            "ev": "pre",
            "tool": "Agent",
            "kind": "agent",
            "id": "t1",
            "desc": "Map the loader",
            "atype": "Explore",
        },
        {
            "v": 1,
            "ts": now - 2,
            "ev": "pre",
            "tool": "Read",
            "kind": "read",
            "id": "t2",
            "agent": "a7",
            "agent_type": "Explore",
            "reads": [str(repo / "app.py")],
        },
        {
            "v": 1,
            "ts": now - 1,
            "ev": "post",
            "tool": "Agent",
            "kind": "agent",
            "id": "t1",
            "desc": "Map the loader",
            "atype": "Explore",
        },
    ]
    with open(feed, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    monkeypatch.setattr(
        server._events,
        "sessions_snapshot",
        lambda: [{"title": "rzt", "activity": "working"}],
    )
    d = client.get("/api/instances/rzt/code-map/live", params={"since": 0}).json()
    call_pre, inner, call_post = d["feed"]
    assert call_pre["desc"] == "Map the loader" and call_pre["atype"] == "Explore"
    assert call_post["desc"] == "Map the loader" and call_post["ev"] == "post"
    assert inner["agent"] == "a7" and inner["agent_type"] == "Explore"
    assert inner["reads"] == ["app.py"]


# --------------------------------------------------------------------------- #
# Zone CRUD per session
# --------------------------------------------------------------------------- #
def test_add_zone_syncs_guard_now_and_seeds_already_changed(sess, repo):
    _write(repo, "config/settings.toml", "a = 5\n")
    r = client.post(
        "/api/instances/rzt/red-zones",
        json={"pattern": "config/", "name": "Config"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["told"] is False
    assert d["zone"]["pattern"] == "config/" and d["zone"]["name"] == "Config"
    assert d["already_changed"] == ["config/settings.toml"]
    assert [z["scope"] for z in d["zones"]] == ["repo"]
    # The guard reflects the zone BEFORE the route returned (next tool call).
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert [x["pattern"] for x in guard["rules"]] == ["config/"]
    # Pre-existing change recorded as known: the loop won't announce it.
    root = os.path.realpath(str(repo))
    assert "config/settings.toml" in mon._ROOTS[root]["announced"]
    # Same pattern again dedupes.
    again = client.post("/api/instances/rzt/red-zones", json={"pattern": "config/"})
    assert again.json()["zone"]["id"] == d["zone"]["id"]


def test_add_zone_worktree_scope_and_validation(sess, repo):
    r = client.post(
        "/api/instances/rzt/red-zones", json={"pattern": "lib.py", "scope": "worktree"}
    )
    assert r.status_code == 200
    assert [z["scope"] for z in r.json()["zones"]] == ["worktree"]
    assert red_zones.worktree_zones(str(repo))[0]["pattern"] == "lib.py"
    for bad in ("", "../x", "/" * 3, "a/../b"):
        assert (
            client.post(
                "/api/instances/rzt/red-zones", json={"pattern": bad}
            ).status_code
            == 400
        ), bad
    assert (
        client.post(
            "/api/instances/rzt/red-zones", json={"pattern": "x", "scope": "team"}
        ).status_code
        == 400
    )


def test_add_zone_tell_agent_delivers_the_notice(sess, repo, monkeypatch):
    sent = _delivery(monkeypatch, activity="idle")
    r = client.post(
        "/api/instances/rzt/red-zones",
        json={"pattern": "config/", "tell_agent": True},
    )
    assert r.json()["told"] == "sent"
    assert sent["sent"] and "`config/` is now a red zone" in sent["sent"][0][1]


def test_preview_is_a_dry_run(sess, repo):
    _write(repo, "config/settings.toml", "a = 3\n")
    r = client.post("/api/instances/rzt/red-zones/preview", json={"pattern": "config/"})
    assert r.status_code == 200
    d = r.json()
    assert d["count"] == 1 and d["sample"] == ["config/settings.toml"]
    assert d["changed"] == ["config/settings.toml"]
    assert d["ignored_count"] == 0
    assert red_zones.compile_pattern("config/") == d["re"]
    ign = client.post(
        "/api/instances/rzt/red-zones/preview", json={"pattern": "local.env"}
    ).json()
    assert ign["count"] == 1 and ign["ignored_count"] == 1
    assert red_zones.all_repos() == {}  # nothing saved
    assert (
        client.post(
            "/api/instances/rzt/red-zones/preview", json={"pattern": "../etc"}
        ).status_code
        == 400
    )


def test_delete_and_waive(sess, repo):
    z = client.post("/api/instances/rzt/red-zones", json={"pattern": "config/"}).json()[
        "zone"
    ]
    wz = client.post(
        "/api/instances/rzt/red-zones", json={"pattern": "lib.py", "scope": "worktree"}
    ).json()["zone"]
    gp = Path(red_zones.guard_path(str(repo)))

    r = client.post(
        f"/api/instances/rzt/red-zones/{z['id']}/waive", json={"waived": True}
    )
    assert r.status_code == 200
    by = {x["id"]: x for x in r.json()["zones"]}
    assert by[z["id"]]["waived"] is True
    assert [x["pattern"] for x in json.loads(gp.read_text())["rules"]] == ["lib.py"]
    # A worktree zone can't be waived; an unknown one is 404.
    assert (
        client.post(
            f"/api/instances/rzt/red-zones/{wz['id']}/waive", json={}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/instances/rzt/red-zones/rz_nope/waive", json={}).status_code
        == 404
    )
    client.post(f"/api/instances/rzt/red-zones/{z['id']}/waive", json={"waived": False})
    assert len(json.loads(gp.read_text())["rules"]) == 2

    r = client.delete(f"/api/instances/rzt/red-zones/{z['id']}")
    assert r.status_code == 200 and [x["id"] for x in r.json()["zones"]] == [wz["id"]]
    r = client.delete(f"/api/instances/rzt/red-zones/{wz['id']}")
    assert r.json()["zones"] == []
    assert json.loads(gp.read_text())["rules"] == []
    assert client.delete(f"/api/instances/rzt/red-zones/{z['id']}").status_code == 404


def test_get_session_red_zones(sess, repo):
    red_zones.set_plan_first(_rid(repo), True)
    red_zones.add_zone("repo", _rid(repo), "config/")
    d = client.get("/api/instances/rzt/red-zones").json()
    assert d["repo"]["id"] == _rid(repo)
    assert d["plan_first"] is True
    assert [z["pattern"] for z in d["zones"]] == ["config/"]


def test_route_writes_are_not_store_tampering(sess, repo):
    mon.note_route_write()
    before = mon._STORE["digest"]
    client.post("/api/instances/rzt/red-zones", json={"pattern": "config/"})
    assert mon._STORE["digest"] == red_zones.store_digest() != before


# --------------------------------------------------------------------------- #
# Global /api/red-zones
# --------------------------------------------------------------------------- #
def test_global_red_zones_crud(sess, repo):
    rid = _rid(repo)
    assert client.get("/api/red-zones").json() == {"repos": {}}
    assert client.post("/api/red-zones", json={"pattern": "x"}).status_code == 400
    assert (
        client.post(
            "/api/red-zones", json={"repo_id": rid, "pattern": ".."}
        ).status_code
        == 400
    )
    r = client.post(
        "/api/red-zones",
        json={"repo_id": rid, "pattern": "config/", "name": "Cfg", "label": "o/r"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["repos"][rid]["label"] == "o/r"
    assert [z["pattern"] for z in d["repos"][rid]["zones"]] == ["config/"]
    # Every live worktree of that repo was re-synced now.
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert [x["pattern"] for x in guard["rules"]] == ["config/"]

    r = client.post("/api/red-zones/plan-first", json={"repo_id": rid, "on": True})
    assert r.json()["repos"][rid]["plan_first"] is True
    assert (
        client.post("/api/red-zones/plan-first", json={"on": True}).status_code == 400
    )

    zid = d["zone"]["id"]
    r = client.delete(f"/api/red-zones/{zid}")
    assert r.status_code == 200 and r.json()["repos"][rid]["zones"] == []
    assert json.loads(Path(red_zones.guard_path(str(repo))).read_text())["rules"] == []
    assert client.delete(f"/api/red-zones/{zid}").status_code == 404


# --------------------------------------------------------------------------- #
# Delivery: ask-plan / go (queue vs send, never reboot)
# --------------------------------------------------------------------------- #
def _delivery(monkeypatch, *, activity="idle", live=True, locked=False):
    rec = {"sent": [], "queued": [], "activity": activity}
    monkeypatch.setattr(
        server, "_live_session_name", lambda name: name if live else None
    )
    monkeypatch.setattr(server, "_agent_activity", lambda inst, t: rec["activity"])
    monkeypatch.setattr(
        server,
        "_send_to_agent",
        lambda name, text, submit=True: rec["sent"].append((name, text)) or True,
    )
    monkeypatch.setattr(
        server._prompt_queue,
        "enqueue",
        lambda title, text, index=None: rec["queued"].append((title, text)) or {},
    )
    monkeypatch.setattr(server, "_emit_queue_changed", lambda title: None)
    monkeypatch.setattr(server, "_budget_locked", lambda title: locked)
    monkeypatch.setattr(
        server,
        "_agent_session_ready",
        lambda inst, title: pytest.fail("a Map message must never reboot the agent"),
    )
    return rec


def test_ask_plan_sends_when_idle(sess, monkeypatch):
    rec = _delivery(monkeypatch, activity="idle")
    r = client.post("/api/instances/rzt/code-map/ask-plan", json={"mode": "plan"})
    assert r.json() == {"ok": True, "told": "sent"}
    assert rec["sent"] == [("mindflock_rzt", red_zones.PLAN_PROMPT)]
    assert rec["queued"] == []
    server._HUMAN_INPUT_AT.pop("rzt", None)


@pytest.mark.parametrize("activity", ["working", "clarify", "limit"])
def test_ask_plan_queues_while_busy(sess, monkeypatch, activity):
    rec = _delivery(monkeypatch, activity=activity)
    r = client.post("/api/instances/rzt/code-map/ask-plan", json={"mode": "remaining"})
    assert r.json() == {"ok": True, "told": "queued"}
    assert rec["queued"] == [("rzt", red_zones.REMAINING_PROMPT)]
    assert rec["sent"] == []


def test_ask_plan_refuses_dead_agent_and_over_budget(sess, monkeypatch):
    _delivery(monkeypatch, live=False)
    d = client.post("/api/instances/rzt/code-map/ask-plan", json={}).json()
    assert d["ok"] is False and d["told"] is False and "isn't running" in d["reason"]
    _delivery(monkeypatch, locked=True)
    d = client.post("/api/instances/rzt/code-map/ask-plan", json={}).json()
    assert d == {"ok": False, "told": False, "reason": "over budget"}
    assert (
        client.post(
            "/api/instances/rzt/code-map/ask-plan", json={"mode": "bogus"}
        ).status_code
        == 400
    )


def test_go_names_the_staged_zones(sess, repo, monkeypatch):
    rec = _delivery(monkeypatch, activity="idle")
    z = red_zones.add_zone("repo", _rid(repo), "config/", name="Config")
    r = client.post(
        "/api/instances/rzt/code-map/go", json={"zone_ids": [z["id"], "rz_nope"]}
    )
    assert r.json()["told"] == "sent"
    text = rec["sent"][0][1]
    assert text == red_zones.go_message([z])
    assert "`config/` (Config)" in text
    rec["sent"].clear()
    client.post("/api/instances/rzt/code-map/go", json={})
    assert rec["sent"][0][1] == red_zones.go_message([])
    server._HUMAN_INPUT_AT.pop("rzt", None)


def test_send_route_keeps_its_contract(sess, monkeypatch):
    """/send still boots the agent and types unconditionally (no queueing)."""
    rec = _delivery(monkeypatch, activity="working")
    monkeypatch.setattr(
        server, "_agent_session_ready", lambda inst, title: ("agent_rzt", None)
    )
    r = client.post("/api/instances/rzt/send", json={"text": "hi"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert rec["sent"] == [("agent_rzt", "hi")] and rec["queued"] == []
    monkeypatch.setattr(server, "_send_to_agent", lambda n, t, submit=True: False)
    assert client.post("/api/instances/rzt/send", json={"text": "x"}).status_code == 502
    monkeypatch.setattr(
        server, "_agent_session_ready", lambda inst, title: ("n", "workspace gone")
    )
    r = client.post("/api/instances/rzt/send", json={"text": "x"})
    assert r.status_code == 409 and r.json()["error"] == "workspace gone"
    server._HUMAN_INPUT_AT.pop("rzt", None)


# --------------------------------------------------------------------------- #
# Push / make-pr / merge gate
# --------------------------------------------------------------------------- #
def _commit_zone_breach(repo):
    red_zones.add_zone("repo", _rid(repo), "config/")
    _write(repo, "config/settings.toml", "a = 7\n")
    _git(repo, "commit", "-qam", "touch config")


def test_push_gate_409_and_override(sess, repo):
    # Uncommitted zone changes don't gate a push (the push carries commits).
    red_zones.add_zone("repo", _rid(repo), "config/")
    _write(repo, "config/settings.toml", "a = 7\n")
    r = client.post("/api/instances/rzt/push-branch", json={})
    assert r.status_code == 400  # past the gate: no origin
    _git(repo, "commit", "-qam", "touch config")
    r = client.post("/api/instances/rzt/push-branch", json={})
    assert r.status_code == 409
    d = r.json()
    assert d["error"] == "red zone breached: config/settings.toml (config/)"
    assert d["red_zone_breaches"] == [
        {
            "path": "config/settings.toml",
            "pattern": "config/",
            "zone_id": red_zones.repo_zones(_rid(repo))[0]["id"],
            "kind": "red",
        }
    ]
    r = client.post("/api/instances/rzt/push-branch", json={"override_red_zones": True})
    assert r.status_code == 400 and "no origin" in r.json()["error"]


def test_waived_zone_does_not_gate(sess, repo):
    _commit_zone_breach(repo)
    zid = red_zones.repo_zones(_rid(repo))[0]["id"]
    red_zones.set_waiver(str(repo), zid, True)
    r = client.post("/api/instances/rzt/push-branch", json={})
    assert r.status_code == 400  # no origin — the gate let it through


def _no_github(monkeypatch):
    monkeypatch.setattr(server, "gh_available", lambda: False)

    async def _create(wt, base, branch):
        return SimpleNamespace(unavailable=True, ok=False, error="", url=None)

    async def _find(wt, branch):
        return None

    monkeypatch.setattr(server._github_pr, "create_pr", _create)
    monkeypatch.setattr(server._github_pr, "find_pr", _find)


def test_make_pr_and_merge_gate_on_the_pushed_branch(sess, repo, tmp_path, monkeypatch):
    _no_github(monkeypatch)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _commit_zone_breach(repo)
    _git(repo, "push", "-q", "origin", "feature")
    # Revert locally: HEAD is clean, but the pushed branch (what the PR
    # carries) still has the breach — the PR/merge gates must read origin.
    _git(repo, "revert", "--no-edit", "HEAD")
    assert server._red_zone_breaches_for(sess, str(repo), "HEAD") == []
    r = client.post("/api/instances/rzt/make-pr", json={})
    assert r.status_code == 409 and r.json()["red_zone_breaches"]
    r = client.post("/api/instances/rzt/merge-pr", json={})
    assert r.status_code == 409 and r.json()["red_zone_breaches"]
    r = client.post("/api/instances/rzt/make-pr", json={"override_red_zones": True})
    assert r.status_code == 200 and r.json()["ok"] is False  # browser handoff
    r = client.post("/api/instances/rzt/merge-pr", json={"override_red_zones": True})
    assert r.status_code == 200 and r.json()["ok"] is False
    # Merge also still works body-less (the autopilot's call shape).
    r = client.post("/api/instances/rzt/merge-pr")
    assert r.status_code == 409


def test_autopilot_merge_halts_on_a_red_zone_even_if_a_path_says_pending(
    monkeypatch,
):
    async def _merge(title, payload=None):
        return JSONResponse(
            {
                "error": "red zone breached: backend/pending.py (backend/)",
                "red_zone_breaches": [
                    {
                        "path": "backend/pending.py",
                        "pattern": "backend/",
                        "zone_id": "z",
                    }
                ],
            },
            status_code=409,
        )

    halts, notes = [], []
    monkeypatch.setattr(server, "instance_merge_pr", _merge)
    monkeypatch.setattr(server, "_autopilot_halt", lambda t, r: halts.append(r))
    monkeypatch.setattr(server, "_autopilot_note", lambda *a: notes.append(a))
    asyncio.run(
        server._autopilot_act(
            "ap", "/wt", {"issues": {}}, {"now": time.time()}, "merge", {}
        )
    )
    assert halts == ["red zone breached: backend/pending.py (backend/)"]
    assert notes == []


# --------------------------------------------------------------------------- #
# Prompt decoration (create + intake)
# --------------------------------------------------------------------------- #
def test_create_instance_plan_first_and_zone_note(repo):
    red_zones.add_zone("repo", _rid(repo), "config/")
    r = client.post(
        "/api/instances",
        json={
            "title": "rz-new",
            "repo_path": str(repo),
            "program": "claude",
            "prompt": "fix the bug",
            "plan_first": True,
        },
    )
    assert r.status_code == 202, r.text
    p = server.ENGINE.instances["rz-new"].Prompt
    assert p.startswith("fix the bug")
    assert red_zones.PLAN_PROMPT in p
    assert "Red zones (MindFlock blocks edits here): `config/`" in p
    server._EVENT_SNAPSHOT.pop("rz-new", None)

    r = client.post(
        "/api/instances",
        json={"title": "rz-new2", "repo_path": str(repo), "prompt": "go"},
    )
    assert r.status_code == 202
    p = server.ENGINE.instances["rz-new2"].Prompt
    assert red_zones.PLAN_PROMPT not in p  # the repo flag is for intake only
    server._EVENT_SNAPSHOT.pop("rz-new2", None)


def test_red_zone_prompt_helper(repo, tmp_path):
    rid = _rid(repo)
    red_zones.add_zone("repo", rid, "config/")
    # Detect-only provider: told the zones are flagged, not blocked.
    out = server._red_zone_prompt("do it", "codex", [str(repo)], None)
    assert "changes there are flagged and block pushes" in out
    assert red_zones.PLAN_PROMPT not in out
    red_zones.set_plan_first(rid, True)
    out = server._red_zone_prompt("do it", "claude", [str(repo)], None)
    assert red_zones.PLAN_PROMPT in out
    # Idempotent; nothing to key off -> plan-first only when asked explicitly.
    assert server._red_zone_prompt(out, "claude", [str(repo)], None) == out
    assert server._red_zone_prompt("x", "claude", [str(tmp_path / "nope")], None) == "x"
    assert red_zones.PLAN_PROMPT in server._red_zone_prompt("x", "claude", [], True)
    assert server._red_zone_prompt("", "claude", [str(repo)], True) == ""
    assert str(repo) in server._repo_url_workdirs(str(repo))


class _Stop(Exception):
    """Raised by the NewInstance stub once it has the options."""


def _run_intake_start(monkeypatch, path: str, body: dict) -> str:
    """POST an intake start, run its background launch to NewInstance, and
    return the prompt it would have launched with. Nothing is provisioned:
    NewInstance records the options and raises; every tracker/forge call is
    a stub."""
    tasks: list = []
    seen: list = []

    def _new(opts):
        seen.append(opts)
        raise _Stop()

    monkeypatch.setattr(server, "_register_task", tasks.append)
    monkeypatch.setattr(server.session, "NewInstance", _new)
    monkeypatch.setattr(server, "_arm_intake_autopilot", lambda *a, **k: None)
    monkeypatch.setattr(server, "_cached_session_title", lambda *a, **k: "")
    monkeypatch.setattr(
        server._worktree_reclaim, "reclaim_for_launch", lambda *a, **k: None
    )
    r = client.post(path, json=body)
    assert r.status_code == 202, r.text
    (coro,) = tasks
    asyncio.run(coro)
    (opts,) = seen
    return opts.prompt


def _stub_ticket(monkeypatch, repo_url: str, agent: str = "claude"):
    story = SimpleNamespace(
        id="1", name="fix it", repo_url=repo_url, agent=agent, effort=""
    )
    ts = server._ticket_start

    async def _find(source, tid):
        return story

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ts, "find_ticket", _find)
    monkeypatch.setattr(ts, "session_title", lambda s: "tix-rz-1")
    monkeypatch.setattr(ts, "branch_for", lambda s: "feature/tix-rz-1")
    monkeypatch.setattr(ts, "workspace_mode", lambda: "worktree")
    monkeypatch.setattr(ts, "build_prompt", lambda s: "do the ticket")
    monkeypatch.setattr(ts, "record_started", lambda s: None)
    monkeypatch.setattr(ts, "record_result", lambda *a, **k: None)
    monkeypatch.setattr(ts, "agent_for", lambda s: agent)
    monkeypatch.setattr(ts, "effort_for", lambda s: "")
    monkeypatch.setattr(ts, "download_attachments", _none)
    monkeypatch.setattr(ts, "move_to_start_state", _none)
    monkeypatch.setattr(server, "_source_intake_depth", lambda src: "")


def _stub_issue(monkeypatch, repo_url: str, agent: str = "claude"):
    story = SimpleNamespace(id="7", repo_url=repo_url, agent=agent)
    iss = server._issue_start

    async def _find(repo, number):
        return SimpleNamespace(title="bug", number=number)

    async def _prepare(issue):
        return story, "do the issue", "feature/iss-7"

    monkeypatch.setattr(iss, "find_issue", _find)
    monkeypatch.setattr(iss, "session_title", lambda i: "iss-rz-7")
    monkeypatch.setattr(iss, "branch_for", lambda i: "feature/iss-7")
    monkeypatch.setattr(iss, "workspace_mode", lambda: "worktree")
    monkeypatch.setattr(iss, "prepare_start", _prepare)
    monkeypatch.setattr(server, "_repo_intake_depth", lambda *a: "")


@pytest.mark.parametrize("route", ["ticket", "issue"])
def test_intake_starts_launch_with_the_repos_plan_first_flag(repo, monkeypatch, route):
    """F24: behavioural, not a source-text count — the prompt the intake
    start actually hands NewInstance carries the repo's Plan-first
    instruction and zone note (and a CLI that can't show a plan doesn't get
    told to wait for a Go it can't receive)."""
    rid = _rid(repo)
    red_zones.add_zone("repo", rid, "config/")
    red_zones.set_plan_first(rid, True)
    for agent, want_plan in (("claude", True), ("codex", False)):
        if route == "ticket":
            _stub_ticket(monkeypatch, str(repo), agent)
            p = _run_intake_start(
                monkeypatch, "/api/tickets/start", {"source": "jira", "id": "1"}
            )
        else:
            _stub_issue(monkeypatch, str(repo), agent)
            p = _run_intake_start(
                monkeypatch, "/api/github/issues/start", {"repo": "o/r", "number": 7}
            )
        assert (red_zones.PLAN_PROMPT in p) is want_plan, (route, agent)
        assert "`config/`" in p, (route, agent)
    red_zones.set_plan_first(rid, False)
    _stub_ticket(monkeypatch, str(repo))
    p = _run_intake_start(
        monkeypatch, "/api/tickets/start", {"source": "jira", "id": "1"}
    )
    assert red_zones.PLAN_PROMPT not in p and "`config/`" in p


# --------------------------------------------------------------------------- #
# Row field, DELETE path, lifespan loop
# --------------------------------------------------------------------------- #
def test_row_carries_the_monitor_summary(sess, repo):
    assert server._instance_json(sess, cheap=True)["redzone"] is None
    red_zones.add_zone("repo", _rid(repo), "config/")
    mon.tick({"rzt": sess}, {})
    row = server._instance_json(sess, cheap=True)
    assert row["redzone"] == mon.summary("rzt")
    assert row["redzone"]["zones"] == 1


def test_delete_forgets_red_zone_state_before_kill():
    src = inspect.getsource(server.delete_instance)
    assert "_red_zone_monitor.forget" in src
    assert src.index("_red_zone_monitor.forget") < src.index("to_thread(inst.Kill)")


def test_loop_is_off_under_pytest_unless_asked(monkeypatch):
    assert server._RED_ZONE_LOOP_ENABLED is None
    assert server._red_zone_loop_enabled() is False
    monkeypatch.setattr(server, "_RED_ZONE_LOOP_ENABLED", True)
    assert server._red_zone_loop_enabled() is True


def test_tick_reads_activity_from_the_published_snapshot(sess, monkeypatch):
    calls = []
    monkeypatch.setattr(mon, "tick", lambda inst, act: calls.append((inst, act)))
    monkeypatch.setattr(
        server._events,
        "sessions_snapshot",
        lambda: [{"title": "rzt", "activity": "idle"}, {"nope": 1}],
    )
    server._red_zone_tick()
    assert calls == [({"rzt": sess}, {"rzt": "idle"})]


def test_lifespan_runs_the_loop_when_enabled(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(server, "_RED_ZONE_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_register_task", _REAL_REGISTER_TASK)
    monkeypatch.setattr(mon, "tick", lambda inst, act: ran.set())
    with TestClient(server.app):
        assert ran.wait(5.0)


# --------------------------------------------------------------------------- #
# Partial import graph: never "unchanged" until complete (F26/F32)
# --------------------------------------------------------------------------- #
def test_a_partial_graph_is_refetched_until_complete(sess, repo, monkeypatch):
    """The first build of a big/cold repo runs out of budget. Its snapshot
    fingerprint must differ from the live poll's (so the client comes back)
    and a refetch must rebuild — never answer "unchanged" — until the graph
    is whole."""
    monkeypatch.setattr(code_map, "GRAPH_BUDGET_S", -1.0)  # nothing new is read
    first = client.get("/api/instances/rzt/code-map").json()
    assert first["graph_partial"] is True and first["edges"] == []
    live_fp = client.get("/api/instances/rzt/code-map/live").json()["fingerprint"]
    assert first["fingerprint"] != live_fp  # the client's refetch trigger
    assert first["fingerprint"] == live_fp + ".partial"
    # Even a client that sends the plain fingerprint gets a rebuild.
    again = client.get("/api/instances/rzt/code-map", params={"fp": live_fp}).json()
    assert "unchanged" not in again and again["graph_partial"] is True
    monkeypatch.setattr(code_map, "GRAPH_BUDGET_S", 6.0)
    done = client.get(
        "/api/instances/rzt/code-map", params={"fp": first["fingerprint"]}
    ).json()
    assert done["graph_partial"] is False and done["edges"]
    assert done["fingerprint"] == live_fp
    # Complete now: the shortcut is back.
    assert client.get("/api/instances/rzt/code-map", params={"fp": live_fp}).json() == {
        "unchanged": True,
        "fingerprint": live_fp,
    }


# --------------------------------------------------------------------------- #
# Case-insensitive filesystems (F33)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ci", [True, False])
def test_case_insensitive_fs_reaches_live_breaches_and_the_push_gate(
    sess, repo, monkeypatch, ci
):
    monkeypatch.setattr(red_zones, "_probe_case_insensitive", lambda root: ci)
    red_zones.add_zone("repo", _rid(repo), "Config/")
    _write(repo, "config/settings.toml", "a = 8\n")
    d = client.get("/api/instances/rzt/code-map/live").json()
    assert d["ci"] is ci
    assert [b["path"] for b in d["breaches"]] == (
        ["config/settings.toml"] if ci else []
    )
    _git(repo, "commit", "-qam", "cfg")
    r = client.post("/api/instances/rzt/push-branch", json={})
    assert r.status_code == (409 if ci else 400)  # 400 = past the gate, no origin


# --------------------------------------------------------------------------- #
# Plan-first only for CLIs that can show a plan (F40)
# --------------------------------------------------------------------------- #
def test_plan_first_is_dropped_for_a_cli_without_plan_support(repo):
    rid = _rid(repo)
    red_zones.set_plan_first(rid, True)
    assert red_zones.PLAN_PROMPT in server._red_zone_prompt(
        "x", "claude", [str(repo)], None
    )
    for explicit in (None, True):
        out = server._red_zone_prompt("x", "codex", [str(repo)], explicit)
        assert red_zones.PLAN_PROMPT not in out, explicit
    r = client.post(
        "/api/instances",
        json={
            "title": "rz-codex",
            "repo_path": str(repo),
            "program": "codex",
            "prompt": "fix it",
            "plan_first": True,
        },
    )
    assert r.status_code == 202, r.text
    assert red_zones.PLAN_PROMPT not in server.ENGINE.instances["rz-codex"].Prompt
    server._EVENT_SNAPSHOT.pop("rz-codex", None)


def test_providers_manage_says_which_clis_support_plans():
    rows = {
        p["name"]: p for p in client.get("/api/providers/manage").json()["providers"]
    }
    assert rows["claude"]["plan_supported"] is True
    assert rows["codex"]["plan_supported"] is False
    assert all(isinstance(p["plan_supported"], bool) for p in rows.values())


# --------------------------------------------------------------------------- #
# DELETE: in-place zones survive, no resurrection mid-Kill, feed gone (F44/45/48)
# --------------------------------------------------------------------------- #
class _KillInst(_FakeInst):
    def __init__(self, title, wt, on_kill=None, in_place=False):
        super().__init__(title, wt)
        self.InPlace = in_place
        self._on_kill = on_kill

    def Kill(self):  # noqa: N802
        if self._on_kill:
            self._on_kill()


def _stub_delete(monkeypatch):
    monkeypatch.setattr(server, "_kill_shell_session", lambda t: None)
    monkeypatch.setattr(server, "_close_cursor_window", lambda p: None)
    monkeypatch.setattr(server, "_remove_trust_entry", lambda p: None)
    monkeypatch.setattr(server._aliases, "drop", lambda t: None)


def test_deleting_an_in_place_session_keeps_the_folders_zones(repo, monkeypatch):
    """F44: Kill never removes an in-place session's folder (it is the
    user's own checkout); its "This worktree" zones belong to the folder."""
    _stub_delete(monkeypatch)
    rid = _rid(repo)
    red_zones.add_zone("worktree", str(repo), "config/", repo_id=rid)
    server.ENGINE.instances["rz-ip"] = _KillInst("rz-ip", str(repo), in_place=True)
    assert client.delete("/api/instances/rz-ip").status_code == 200
    assert [z["pattern"] for z in red_zones.worktree_zones(str(repo))] == ["config/"]


def test_deleting_a_session_whose_worktree_is_removed_drops_its_zones(
    tmp_path, repo, monkeypatch
):
    import shutil

    _stub_delete(monkeypatch)
    wt = tmp_path / "wt-gone"
    _git(repo, "worktree", "add", "-q", "-b", "gone", str(wt))
    red_zones.add_zone("worktree", str(wt), "config/", repo_id=_rid(repo))
    server.ENGINE.instances["rz-gone"] = _KillInst(
        "rz-gone", str(wt), on_kill=lambda: shutil.rmtree(wt)
    )
    assert client.delete("/api/instances/rz-gone").status_code == 200
    assert red_zones.worktree_zones(str(wt)) == []


def test_a_tick_during_kill_cannot_resurrect_the_guard_or_cry_tamper(repo, monkeypatch):
    """F45: the instance is still registered while Kill runs; a tick then
    must neither rewrite the guard forget() removed nor alert on it. F48:
    a feed record the dying agent writes during Kill is removed too."""
    from backend.session import tmux as _tmux
    from backend.web.core import events as _ev

    _stub_delete(monkeypatch)
    red_zones.add_zone("repo", _rid(repo), "config/")
    got: list = []
    unsub = _ev.BUS.subscribe(
        lambda env: (
            got.append(env)
            if str(env.get("event", "")).startswith("session.red_zone")
            else None
        )
    )
    feed = red_zones.feed_path(_tmux.to_mindflock_tmux_name("rz-del"))

    def _kill():
        mon.tick(dict(server.ENGINE.instances), {})  # the loop, mid-Kill
        os.makedirs(os.path.dirname(feed), exist_ok=True)
        with open(feed, "a") as f:  # the dying agent's last Post hook
            f.write('{"v":1,"ts":1,"ev":"post","tool":"Read"}\n')

    inst = _KillInst("rz-del", str(repo), on_kill=_kill)
    server.ENGINE.instances["rz-del"] = inst
    mon.tick({"rz-del": inst}, {})
    gp = Path(red_zones.guard_path(str(repo)))
    assert gp.exists()
    try:
        assert client.delete("/api/instances/rz-del").status_code == 200
    finally:
        unsub()
    assert not gp.exists()
    assert [e for e in got if e["event"] == "session.red_zone_tampered"] == []
    assert not os.path.exists(feed)
    assert mon.summary("rz-del") is None
    # A NEW session reusing the title is watched normally.
    again = _FakeInst("rz-del", str(repo), program="bash")
    server.ENGINE.instances["rz-del"] = again
    mon.tick({"rz-del": again}, {})
    assert gp.exists() and mon.summary("rz-del")["zones"] == 1


# --------------------------------------------------------------------------- #
# PR/merge gate reads what the forge will diff (F46) + a fork point that a
# tag named like the base can't blind
# --------------------------------------------------------------------------- #
def _with_origin(repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main", "feature")
    return bare


def test_merge_gate_ignores_upstream_changes_merged_locally(
    sess, repo, tmp_path, monkeypatch
):
    """A teammate changes zoned config on main; the user merges main locally
    (not pushed). The pushed branch the PR carries has no zoned change —
    only the fork point moved."""
    from backend.web.core import snapshot

    monkeypatch.setattr(server, "_session_fork_point", snapshot._session_fork_point)
    _with_origin(repo, tmp_path)  # first: an origin changes the repo identity
    red_zones.add_zone("repo", _rid(repo), "config/")
    assert red_zones.repo_zones(_rid(repo))
    _write(repo, "app.py", "import lib\nprint(1)\n")
    _git(repo, "commit", "-qam", "feature work")
    _git(repo, "push", "-q", "origin", "feature")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "config/settings.toml", "a = 99\n")  # the teammate's change
    _git(repo, "commit", "-qam", "teammate")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")  # local, unpushed
    # The HEAD-derived fork point sees the teammate's change reversed…
    fork = snapshot._session_fork_point(sess, str(repo))
    assert "config/settings.toml" in _git(
        repo, "diff", "--name-only", fork, "refs/remotes/origin/feature"
    )
    # …the gate reads what the PR carries.
    assert server._red_zone_breaches_for(sess, str(repo), "origin/feature") == []
    assert server._red_zone_breaches_for(sess, str(repo), "HEAD") == []
    # A zoned change the branch really carries still gates.
    _write(repo, "config/settings.toml", "a = 100\n")
    _git(repo, "commit", "-qam", "ours")
    _git(repo, "push", "-q", "origin", "feature")
    (hit,) = server._red_zone_breaches_for(sess, str(repo), "origin/feature")
    assert hit["path"] == "config/settings.toml"


def test_a_tag_named_like_the_base_cannot_blind_the_push_gate(
    sess, repo, tmp_path, monkeypatch
):
    from backend.web.core import snapshot

    monkeypatch.setattr(server, "_session_fork_point", snapshot._session_fork_point)
    _with_origin(repo, tmp_path)
    _commit_zone_breach(repo)
    _git(repo, "tag", "origin/main", "HEAD")  # would make the fork point HEAD
    assert snapshot._session_fork_point(sess, str(repo)) != _git(
        repo, "rev-parse", "HEAD"
    )
    r = client.post("/api/instances/rzt/push-branch", json={})
    assert r.status_code == 409 and r.json()["red_zone_breaches"]


# --------------------------------------------------------------------------- #
# v3 green zones through the routes (critic findings named per test)
# --------------------------------------------------------------------------- #
def _green_add(body: dict):
    return client.post("/api/instances/rzt/red-zones", json=dict(body, kind="green"))


def test_green_add_is_worktree_scope_and_stored_apart(sess, repo):
    """C1/C6: green defaults to — and only accepts — worktree scope; it is
    stored under its own key and synced into `green_rules` at once."""
    r = _green_add({"pattern": "/app.py", "scope": "repo"})
    assert r.status_code == 400 and "worktree-scope only" in r.json()["error"]
    r = client.post(
        "/api/red-zones", json={"repo_id": _rid(repo), "pattern": "x", "kind": "green"}
    )
    assert r.status_code == 400
    r = _green_add({"pattern": "/app.py"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["zone"]["kind"] == "green" and d["zone"]["pattern"] == "/app.py"
    assert [(z["kind"], z["scope"]) for z in d["zones"]] == [("green", "worktree")]
    assert d["exempt"] == [] and d["committed_outside"] == 0
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert guard["rules"] == [] and guard["green_rules"][0]["pattern"] == "/app.py"
    assert red_zones.worktree_zones(str(repo)) == []  # the red list is untouched
    g = client.get("/api/instances/rzt/red-zones").json()
    assert g["mode"] == "green" and g["sessions_here"] == 1


def test_same_pattern_red_and_green_is_409(sess, repo):
    """M6."""
    assert (
        client.post(
            "/api/instances/rzt/red-zones", json={"pattern": "config/"}
        ).status_code
        == 200
    )
    r = _green_add({"pattern": "config"})
    assert r.status_code == 409 and "already a red zone" in r.json()["error"]


def test_green_add_exempts_earlier_work_and_the_gate_honours_it(sess, repo):
    """C5: scoping a session mid-flight must not block its push on work that
    was done before; editing that work AFTER the scope is a breach again."""
    _write(repo, "lib.py", "X = 2\n")
    _git(repo, "commit", "-qam", "earlier work")
    _write(repo, "config/settings.toml", "a = 9\n")  # uncommitted earlier work
    r = _green_add({"pattern": "/app.py"})
    d = r.json()
    assert d["exempt"] == ["config/settings.toml", "lib.py"]
    assert d["committed_outside"] == 1
    assert set(red_zones.green_exempt(str(repo))) == {"config/settings.toml", "lib.py"}
    push = client.post("/api/instances/rzt/push-branch", json={})
    assert push.status_code == 400  # past the gate (no origin)
    live = client.get("/api/instances/rzt/code-map/live").json()
    assert live["breaches"] == [] and live["mode"] == "green"
    _write(repo, "lib.py", "X = 3\n")
    _git(repo, "commit", "-qam", "after the scope")
    push = client.post("/api/instances/rzt/push-branch", json={})
    assert push.status_code == 409
    body = push.json()
    assert body["error"] == "outside green zone: lib.py"
    assert body["red_zone_breaches"] == [
        {"path": "lib.py", "pattern": "outside green", "zone_id": None, "kind": "green"}
    ]


def test_green_exempt_covers_work_committed_then_edited_further(sess, repo):
    """Committed pre-scope work (blob C) edited again without committing
    (blob W): the exemption used to record only W, so the push gate — which
    compares the blob at HEAD — blocked the push while the Map showed no
    breach. Every identity the path had when the scope was set is exempt;
    an edit AFTER the scope is still a breach."""
    _write(repo, "lib.py", "X = 2\n")
    _git(repo, "commit", "-qam", "earlier work")
    _write(repo, "lib.py", "X = 3\n")  # uncommitted, still pre-scope
    d = _green_add({"pattern": "/app.py"}).json()
    assert d["exempt"] == ["lib.py"] and d["committed_outside"] == 1
    assert server._red_zone_breaches_for(sess, str(repo), "HEAD") == []
    assert client.get("/api/instances/rzt/code-map/live").json()["breaches"] == []
    assert client.post("/api/instances/rzt/push-branch", json={}).status_code == 400
    # Restoring the committed pre-scope content keeps it exempt everywhere.
    _git(repo, "checkout", "--", "lib.py")
    assert server._red_zone_breaches_for(sess, str(repo), "HEAD") == []
    assert client.get("/api/instances/rzt/code-map/live").json()["breaches"] == []
    # A change after the scope is a breach at the gate again.
    _write(repo, "lib.py", "X = 4\n")
    _git(repo, "commit", "-qam", "after the scope")
    assert client.post("/api/instances/rzt/push-branch", json={}).status_code == 409


def test_allow_and_plan_scope_take_bracketed_paths_literally(sess, repo, monkeypatch):
    """`app/[slug]/page.tsx` (Next.js / SvelteKit): the zone built from a
    concrete path used to read `[slug]` as a character class — the allowed
    file stayed outside the scope while `app/s/page.tsx` got in."""
    _delivery(monkeypatch, activity="idle")
    _write(repo, "app/[slug]/page.tsx", "x\n")
    _write(repo, "pages/[id].tsx", "x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "routes")
    _green_add({"pattern": "/app.py"})
    r = client.post(
        "/api/instances/rzt/red-zones/allow", json={"path": "app/[slug]/page.tsx"}
    )
    assert r.status_code == 200, r.text
    doc = red_zones.zones_doc(str(repo), _rid(repo))
    assert red_zones.classify("app/[slug]/page.tsx", None, doc) == "ok"
    assert red_zones.classify("app/s/page.tsx", None, doc) == "outside"
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert (
        th._mf_classify("app/[slug]/page.tsx", None, guard) == "ok"
    )  # the hook agrees
    assert th._mf_classify("app/s/page.tsx", None, guard) == "outside"
    server._HUMAN_INPUT_AT.pop("rzt", None)

    plan = {
        "source": "declared",
        "ts": 1.0,
        "items": [
            {"path": "pages/[id].tsx", "intent": "edit", "new": False},
            {"path": "src/routes/[...rest]/+page.svelte", "intent": "add", "new": True},
        ],
    }
    monkeypatch.setattr(server._code_map, "current_plan", lambda *a, **k: plan)
    pats = server._plan_scope_patterns(sess, "rzt", str(repo))
    doc = {"green": pats, "red": [], "companions": []}
    assert red_zones.classify("pages/[id].tsx", None, doc) == "ok"
    assert red_zones.classify("pages/i.tsx", None, doc) == "outside"
    assert red_zones.classify("src/routes/[...rest]/+page.svelte", None, doc) == "ok"


def test_green_add_can_treat_earlier_work_as_breaches(sess, repo):
    _write(repo, "lib.py", "X = 2\n")
    _git(repo, "commit", "-qam", "earlier work")
    d = _green_add({"pattern": "/app.py", "exempt": False}).json()
    assert d["exempt"] == []
    assert client.post("/api/instances/rzt/push-branch", json={}).status_code == 409
    # ...and the later choice via the exempt route, both ways.
    r = client.post(
        "/api/instances/rzt/red-zones/exempt",
        json={"exempt": True, "paths": ["lib.py"]},
    )
    assert set(r.json()["exempt"]) == {"lib.py"}
    assert client.post("/api/instances/rzt/push-branch", json={}).status_code == 400
    r = client.post("/api/instances/rzt/red-zones/exempt", json={"exempt": False})
    assert r.json()["exempt"] == {}
    assert client.post("/api/instances/rzt/push-branch", json={}).status_code == 409
    assert (
        client.post(
            "/api/instances/rzt/red-zones/exempt", json={"paths": "lib.py"}
        ).status_code
        == 400
    )


def test_green_tell_agent_never_says_revert(sess, repo, monkeypatch):
    """M5."""
    sent = _delivery(monkeypatch, activity="idle")
    r = _green_add({"pattern": "/app.py", "tell_agent": True})
    assert r.json()["told"] == "sent"
    text = sent["sent"][0][1]
    assert "only modify files inside it" in text and "`/app.py`" in text
    assert "revert" not in text.lower()
    server._HUMAN_INPUT_AT.pop("rzt", None)


def test_green_preview_warns_on_unanchored_and_empty_scopes(sess, repo):
    """M1 (unanchored patterns widen the scope) and M2 (a typo matches
    nothing → everything is read-only)."""
    _write(repo, "tests/a.py", "x\n")
    _write(repo, "backend/tests/b.py", "x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tests")
    _write(repo, "lib.py", "X = 5\n")
    r = client.post(
        "/api/instances/rzt/red-zones/preview",
        json={"pattern": "tests", "kind": "green"},
    )
    d = r.json()
    assert d["writable_files"] == 2
    assert sorted(d["roots"]) == ["backend/tests", "tests"]
    assert d["unanchored"] is True and d["anchored"] is None
    assert any("unanchored" in w for w in d["warnings"])
    assert d["changed_outside"] == ["lib.py"] and d["committed_outside"] == 0
    one = client.post(
        "/api/instances/rzt/red-zones/preview",
        json={"pattern": "lib.py", "kind": "green"},
    ).json()
    assert one["anchored"] == "/lib.py"
    assert one["changed_outside"] == ["backend/tests/b.py", "tests/a.py"]
    assert one["committed_outside"] == 2
    none = client.post(
        "/api/instances/rzt/red-zones/preview",
        json={"pattern": "/typo_dir", "kind": "green"},
    ).json()
    assert none["writable_files"] == 0 and none["unanchored"] is False
    assert any("nothing exists here yet" in w for w in none["warnings"])
    assert red_zones.effective_zones(str(repo), _rid(repo)) == []  # dry run


def test_allow_this_file_widens_the_scope_and_tells_the_agent(sess, repo, monkeypatch):
    """The deny → request loop (H4): [Allow this file] adds an anchored
    worktree green zone for exactly that path."""
    sent = _delivery(monkeypatch, activity="idle")
    r = client.post("/api/instances/rzt/red-zones/allow", json={"path": "lib.py"})
    assert r.status_code == 409 and "no green zones" in r.json()["error"]
    _green_add({"pattern": "/app.py"})
    red_zones.add_zone("repo", _rid(repo), "config/")
    r = client.post(
        "/api/instances/rzt/red-zones/allow", json={"path": "config/settings.toml"}
    )
    assert r.status_code == 409 and "red zone" in r.json()["error"]
    for bad in ("/etc/passwd", "../x", ""):
        assert (
            client.post(
                "/api/instances/rzt/red-zones/allow", json={"path": bad}
            ).status_code
            == 400
        ), bad
    r = client.post("/api/instances/rzt/red-zones/allow", json={"path": "./lib.py"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["zone"]["pattern"] == "/lib.py" and d["zone"]["kind"] == "green"
    assert d["told"] == "sent" and "`lib.py`" in sent["sent"][-1][1]
    guard = json.loads(Path(red_zones.guard_path(str(repo))).read_text())
    assert sorted(g["pattern"] for g in guard["green_rules"]) == ["/app.py", "/lib.py"]
    server._HUMAN_INPUT_AT.pop("rzt", None)


def test_go_only_the_planned_files_creates_the_scope(sess, repo, monkeypatch):
    sent = _delivery(monkeypatch, activity="idle")
    plan = {
        "source": "declared",
        "ts": 1.0,
        "items": [
            {"path": "app.py", "intent": "edit", "new": False},
            {"path": "pkg/new_mod.py", "intent": "add", "new": True},
            {"path": "top_new.py", "intent": "add", "new": True},
        ],
    }
    monkeypatch.setattr(server._code_map, "current_plan", lambda *a, **k: plan)
    _write(repo, "lib.py", "X = 4\n")
    r = client.post(
        "/api/instances/rzt/code-map/go", json={"zone_ids": [], "scope_to_plan": True}
    )
    d = r.json()
    assert d["told"] == "sent"
    assert [z["pattern"] for z in d["zones"]] == ["/app.py", "/pkg/", "/top_new.py"]
    assert d["exempt"] == ["lib.py"]
    text = sent["sent"][0][1]
    assert "only modify the planned files — `/app.py`, `/pkg/`, `/top_new.py`" in text
    kinds = {z["kind"] for z in red_zones.effective_zones(str(repo), _rid(repo))}
    assert kinds == {"green"}
    monkeypatch.setattr(server._code_map, "current_plan", lambda *a, **k: {"items": []})
    r = client.post("/api/instances/rzt/code-map/go", json={"scope_to_plan": True})
    assert r.status_code == 409
    server._HUMAN_INPUT_AT.pop("rzt", None)


def test_companions_config_routes(sess, repo):
    rid = _rid(repo)
    r = client.get("/api/red-zones/companions", params={"repo_id": rid})
    assert r.json()["patterns"] == [] and "uv.lock" in r.json()["defaults"]
    r = client.put(
        "/api/red-zones/companions",
        json={"repo_id": rid, "patterns": ["/lib.py"]},
    )
    assert r.status_code == 200 and r.json()["patterns"] == ["/lib.py"]
    assert (
        client.put(
            "/api/red-zones/companions", json={"repo_id": rid, "patterns": ["../x"]}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/red-zones/companions", json={"repo_id": rid, "patterns": "x"}
        ).status_code
        == 400
    )
    assert client.get("/api/red-zones/companions").status_code == 400
    # A configured derived output is writable (a companion) under green.
    _green_add({"pattern": "/app.py"})
    _write(repo, "lib.py", "X = 8\n")
    live = client.get("/api/instances/rzt/code-map/live").json()
    assert live["breaches"] == []
    assert {
        "pattern": "/lib.py",
        "re": red_zones.compile_pattern("/lib.py"),
        "source": "repo",
    } in live["companions"]


def test_live_green_fields_peek_and_plan_flags(sess, repo, monkeypatch):
    """H5: reads outside the scope are advisory "peek" marks, never denied;
    plan items outside the scope are flagged every poll."""
    from backend.session import tmux

    _green_add({"pattern": "/app.py"})
    feed = red_zones.feed_path(tmux.to_mindflock_tmux_name("rzt"))
    os.makedirs(os.path.dirname(feed), exist_ok=True)
    with open(feed, "w") as f:
        f.write(
            json.dumps(
                {
                    "v": 1,
                    "ts": time.time(),
                    "ev": "pre",
                    "tool": "Read",
                    "kind": "read",
                    "reads": [str(repo / "lib.py"), str(repo / "app.py")],
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        server._code_map,
        "current_plan",
        lambda *a, **k: {
            "source": "declared",
            "ts": 1.0,
            "items": [
                {"path": "app.py", "intent": "x", "new": False},
                {"path": "lib.py", "intent": "y", "new": False},
            ],
        },
    )
    _write(repo, "lib.py", "X = 6\n")
    d = client.get("/api/instances/rzt/code-map/live").json()
    assert d["mode"] == "green" and d["guard"]["mode"] == "green"
    (rec,) = d["feed"]
    assert rec["peek"] == ["lib.py"]
    items = {i["path"]: i for i in d["plan"]["items"]}
    assert items["lib.py"]["outside"] is True and "outside" not in items["app.py"]
    assert d["breaches"] == [
        {
            "path": "lib.py",
            "pattern": "outside green",
            "zone_id": None,
            "committed": False,
            "kind": "green",
        }
    ]
    assert any(c["pattern"] == "uv.lock" for c in d["companions"])
    assert d["companion_files"] == []


def test_removing_a_green_zone_exempts_its_work_and_tells_the_agent(
    sess, repo, monkeypatch
):
    """M5 (narrowing is announced) + C5 (work done inside the removed zone
    was legitimate when it was done)."""
    sent = _delivery(monkeypatch, activity="idle")
    _green_add({"pattern": "/app.py"})
    lib = _green_add({"pattern": "/lib.py"}).json()["zone"]
    _write(repo, "lib.py", "X = 7\n")
    r = client.delete("/api/instances/rzt/red-zones/%s" % lib["id"])
    d = r.json()
    assert d["ok"] is True and d["told"] == "sent"
    assert "`/lib.py` is no longer in your scope" in sent["sent"][-1][1]
    assert "lib.py" in red_zones.green_exempt(str(repo))
    live = client.get("/api/instances/rzt/code-map/live").json()
    assert live["breaches"] == []
    server._HUMAN_INPUT_AT.pop("rzt", None)


# --------------------------------------------------------------------------- #
# Atlas routes (the drill-down board; analysis in core.code_outline)
# --------------------------------------------------------------------------- #
def _atlas_repo(repo) -> None:
    _write(
        repo,
        "svc/api.py",
        "from fastapi import APIRouter\nfrom svc.core import Engine\n"
        "router = APIRouter()\n\n\n@router.get('/items')\ndef items():\n"
        "    return Engine().run()\n",
    )
    _write(
        repo,
        "svc/core.py",
        "class Engine:\n    def run(self):\n        return 1\n\n\n"
        "def helper(a: int) -> int:\n    return a\n",
    )
    _write(repo, "svc/__init__.py", "")
    _write(repo, "tests/test_core.py", "from svc.core import Engine\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "svc")


def test_atlas_root_and_drilled_level(sess, repo):
    _atlas_repo(repo)
    d = client.get("/api/instances/rzt/code-map/atlas").json()
    assert {"path", "crumbs", "nodes", "tiers", "back_edges", "extras"} <= set(d)
    assert d["path"] == ""
    by = {n["path"]: n for n in d["nodes"]}
    assert "svc" in by and by["svc"]["kind"] == "dir"
    assert by["svc"]["role"] == "code"
    lvl = client.get("/api/instances/rzt/code-map/atlas", params={"path": "svc"}).json()
    assert lvl["path"] == "svc"
    assert [c["path"] for c in lvl["crumbs"]][-1] == "svc"
    names = {n["path"]: n for n in lvl["nodes"]}
    # api.py imports core.py: a sibling relation both ways round.
    assert "svc/core.py" in names["svc/api.py"]["deps_out"]
    assert "svc/api.py" in names["svc/core.py"]["deps_in"]
    assert any(i["name"] == "Engine" for i in names["svc/core.py"]["interface"])


def test_atlas_and_file_reject_escaping_paths_with_400(sess, repo):
    for route in ("atlas", "file"):
        for bad in ("/etc", "../x", "a/../../b"):
            r = client.get("/api/instances/rzt/code-map/" + route, params={"path": bad})
            assert r.status_code == 400, (route, bad)
            assert r.json()["error"]
    for bad in ("", "missing.py", "config"):
        r = client.get("/api/instances/rzt/code-map/file", params={"path": bad})
        assert r.status_code == 400, bad


def test_file_view_route_shape_and_changes(sess, repo):
    _atlas_repo(repo)
    _write(
        repo,
        "svc/core.py",
        "class Engine:\n    def run(self):\n        return 2\n\n\n"
        "def helper(a: int) -> int:\n    return a\n",
    )
    d = client.get(
        "/api/instances/rzt/code-map/file", params={"path": "svc/core.py"}
    ).json()
    assert d["path"] == "svc/core.py" and d["lang"] == "py"
    eng = next(s for s in d["symbols"] if s["name"] == "Engine")
    assert eng["kind"] == "class" and eng["children"][0]["name"] == "run"
    assert any(u["path"] == "svc/api.py" for u in d["used_by"])
    assert "tests/test_core.py" in d["tested_by"]
    assert d["changed_lines"] and "Engine.run" in d["changed_symbols"]
    assert d["zones"] == {"red": False, "green": None}


def test_file_view_zones_follow_the_shared_classifier(sess, repo):
    _atlas_repo(repo)
    r = client.post(
        "/api/instances/rzt/red-zones",
        json={"pattern": "/svc/api.py", "scope": "worktree"},
    )
    assert r.status_code == 200, r.text
    assert _green_add({"pattern": "/svc/"}).status_code == 200

    def z(p):
        return client.get(
            "/api/instances/rzt/code-map/file", params={"path": p}
        ).json()["zones"]

    assert z("svc/api.py") == {"red": True, "green": False}  # red wins
    assert z("svc/core.py") == {"red": False, "green": True}
    assert z("lib.py") == {"red": False, "green": False}


def test_search_and_entry_points_routes(sess, repo):
    _atlas_repo(repo)
    items = client.get(
        "/api/instances/rzt/code-map/search", params={"q": "engine"}
    ).json()["items"]
    top = items[0]
    assert top["name"] == "Engine" and top["path"] == "svc/core.py"
    assert top["line"] == 1 and {"kind", "score"} <= set(top)
    ep = client.get("/api/instances/rzt/code-map/entry-points").json()
    http = [i for i in ep["items"] if i["kind"] == "http"]
    assert http and http[0]["route"] == "/items" and http[0]["method"] == "GET"
    assert http[0]["path"] == "svc/api.py" and ep["total"] >= 1
