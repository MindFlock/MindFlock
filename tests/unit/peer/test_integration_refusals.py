"""Server-side refusals for shared-folder (PeerShare) sessions, and the
session-create path guard for the peer root."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from backend.peer import paths

from ._integration_helpers import SHARE_ID, make_share, mk_inst

PEER = "peer-bob-abab"


@pytest.fixture
def env(monkeypatch, tmp_path):
    from backend.web import server

    p = make_share()
    other_wt = tmp_path / "other"
    other_wt.mkdir()
    insts = {
        PEER: mk_inst(PEER, p["work"], peer_share=SHARE_ID),
        "other": mk_inst("other", str(other_wt)),
    }
    monkeypatch.setattr(server.ENGINE, "instances", insts)
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)

    def no_shell(*a, **k):
        raise AssertionError("a refused route must not reach the shell pane")

    monkeypatch.setattr(server, "_ensure_shell_session", no_shell)
    return TestClient(server.app), p, insts


REFUSED = [
    ("post", "setup/rerun", None),
    ("post", "check", None),
    ("post", "push-branch", {}),
    ("post", "make-pr", {}),
    ("post", "merge-pr", {}),
    ("post", "fast-track", {"depth": "push"}),
    ("post", "lane", {"lane": "push"}),
    ("post", "ship-now", {}),
    ("post", "commit", {"message": "x"}),
    ("post", "commit-message/suggest", {}),
    ("post", "ide", {}),
    ("post", "cleanup", None),
    ("post", "copy", None),
    ("post", "parent", {"parent": "other"}),
    ("post", "test-plan", None),
    ("post", "fence", {"only": ["src/**"]}),
    ("post", "order", {"mode": "serial"}),
    ("post", "code-map/go", {}),
]


@pytest.mark.parametrize("method,suffix,body", REFUSED)
def test_refused_for_shared_session(env, method, suffix, body):
    client, _p, insts = env
    kw = {"json": body} if body is not None else {}
    r = client.post("/api/instances/%s/%s" % (PEER, suffix), **kw)
    assert r.status_code == 409, (suffix, r.status_code, r.text)
    assert "shared-folder" in r.json()["error"]
    assert set(insts) == {PEER, "other"}  # nothing created (copy) or removed


def test_rename_refused(env):
    client, _p, _ = env
    r = client.post("/api/aliases", json={"title": PEER, "alias": "my friend"})
    assert r.status_code == 409
    r = client.post("/api/aliases", json={"aliases": {PEER: "x", "other": "y"}})
    assert r.status_code == 200
    assert PEER not in r.json()["aliases"]


def test_shared_session_cannot_become_a_parent(env):
    client, _p, _ = env
    r = client.post("/api/instances/other/parent", json={"parent": PEER})
    assert r.status_code == 409


def test_create_with_shared_parent_refused(env, tmp_path):
    client, _p, insts = env
    r = client.post(
        "/api/instances",
        json={
            "title": "w",
            "repo_path": str(tmp_path),
            "in_place": True,
            "parent": PEER,
        },
    )
    assert r.status_code == 409
    assert "w" not in insts


@pytest.mark.parametrize(
    "how", ["direct", "root", "symlink", "dotdot", "tilde-free-rel"]
)
def test_create_refuses_paths_in_peer_root(env, tmp_path, monkeypatch, how):
    client, p, insts = env
    if how == "direct":
        path = p["work"]
    elif how == "root":
        path = paths.peer_root()
    elif how == "symlink":
        link = tmp_path / "harmless"
        os.symlink(p["work"], link)
        path = str(link)
    elif how == "dotdot":
        (tmp_path / "a").mkdir()
        path = str(tmp_path / "a" / os.path.relpath(p["home"], tmp_path / "a"))
    else:
        monkeypatch.chdir(p["root"])
        path = "work"
    r = client.post(
        "/api/instances", json={"title": "sneaky", "repo_path": path, "in_place": True}
    )
    assert r.status_code == 409, r.text
    assert "peer link" in r.json()["error"]
    assert "sneaky" not in insts
    # Nothing was initialised inside the share either.
    assert not os.path.exists(os.path.join(p["home"], ".git"))


def test_create_cannot_smuggle_peer_share_in_payload(env, tmp_path, monkeypatch):
    from backend.web import server

    client, _p, insts = env
    # Never actually Start the session (no tmux, no agent).
    monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
    r = client.post(
        "/api/instances",
        json={
            "title": "x",
            "repo_path": str(tmp_path),
            "in_place": True,
            "peer_share": SHARE_ID,
        },
    )
    # peer_share is not a payload key: an ordinary session, not a shared one.
    assert r.status_code == 202, r.text
    assert getattr(insts["x"], "PeerShare", "") == ""


def test_peer_create_requires_exact_share_folder(env, tmp_path):
    import asyncio

    from backend.web.core import session_create

    status, body = asyncio.run(
        session_create.create_result(
            {
                "title": "p2",
                "repo_path": str(tmp_path),
                "in_place": True,
                "program": "claude",
            },
            peer_share=SHARE_ID,
        )
    )
    assert status == 400 and "share's folder" in body["error"]
    status, body = asyncio.run(
        session_create.create_result(
            {
                "title": "p2",
                "repo_path": paths.share_paths(SHARE_ID)["work"],
                "in_place": True,
                "program": "claude",
                "parent": "other",
            },
            peer_share=SHARE_ID,
        )
    )
    assert status == 400 and "parent" in body["error"]


def test_ordinary_session_routes_still_work(env):
    client, _p, _ = env
    r = client.post("/api/aliases", json={"title": "other", "alias": "fine"})
    assert r.status_code == 200


def test_row_json_marks_shared_sessions(env):
    from backend.web import server

    _c, _p, insts = env
    assert server._instance_json(insts[PEER])["peer_share"] is True
    assert server._instance_json(insts["other"])["peer_share"] is False


# --------------------------------------------------------------------------- #
# Host features that would execute folder content
# --------------------------------------------------------------------------- #
def test_worktree_setup_and_check_never_run_in_a_shared_folder(env):
    from backend.web.core import worktree_setup as ws

    _c, p, _ = env
    with open(os.path.join(p["work"], ".mindflock.toml"), "w") as fh:
        fh.write(
            '[workspace]\nsetup = ["touch PWNED"]\ncheck_command = "touch PWNED"\n'
        )
    cfg = ws.load_config(p["work"])
    assert not cfg.has_setup and not cfg.check_command
    assert ws.start_check(PEER, p["work"], "touch PWNED") is False
    assert ws.start_setup(PEER, p["work"], p["work"]) is False
    assert not os.path.exists(os.path.join(p["work"], "PWNED"))


def test_ide_launch_refuses_shared_folder(env):
    from backend.web.core import ide_launch

    _c, p, _ = env
    with pytest.raises(ide_launch.IdeLaunchError, match="shared-folder"):
        ide_launch.launch_ide(p["work"], argv=["true"])


def test_team_run_adopt_refused(env):
    from backend.web.core import team_run_driver

    with pytest.raises(team_run_driver.RunError) as exc:
        team_run_driver.adopt("run1", PEER)
    assert exc.value.status == 409


def test_red_zone_monitor_skips_shared_sessions(env):
    from backend.web.core import red_zone_monitor

    _c, _p, insts = env
    for i in insts.values():
        i.Started = lambda: True
    live = red_zone_monitor._live(insts)
    assert [it["title"] for it in live] == ["other"]
