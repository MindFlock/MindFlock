"""Host-side red-team regressions for shared-folder (peer) sessions.

The attacker is the sandboxed agent of a shared folder (prompt-injected by the
peer): it owns ``work/`` and can plant anything there, including symlinks. No
host-side MindFlock feature that touches that folder may write or delete
outside it.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


class _Inst:
    """Just enough of an Instance for the paste route / paste dirs."""

    PeerShare = "ab" * 16

    def __init__(self, ws):
        self.Path = str(ws)

    def Started(self):
        return True

    def GetWorktreePath(self):
        return self.Path


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A workspace whose ``.mindflock_pastes`` the agent pointed at a host dir
    holding a ``paste-*`` file of the user's."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "paste-keep-me.txt"
    victim.write_text("user data\n")
    ws = tmp_path / "work"
    ws.mkdir()
    os.symlink(str(outside), str(ws / ".mindflock_pastes"))
    monkeypatch.setitem(server.ENGINE.instances, "peer-redteam", _Inst(ws))
    return ws, outside, victim


def test_paste_into_peer_session_never_follows_planted_symlink(planted):
    ws, outside, victim = planted
    for _ in range(12):  # past the retention cap: pruning runs too
        r = client.post(
            "/api/paste-image?session=peer-redteam",
            content=PNG,
            headers={"content-type": "image/png"},
        )
        assert r.status_code == 409
    assert sorted(os.listdir(outside)) == ["paste-keep-me.txt"]
    assert victim.read_text() == "user data\n"


def test_restart_paste_wipe_never_follows_planted_symlink(planted):
    _ws, outside, victim = planted
    server._clear_all_pastes()
    assert victim.is_file()


def test_paste_into_real_workspace_dir_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ws = tmp_path / "work"
    ws.mkdir()
    monkeypatch.setitem(server.ENGINE.instances, "peer-redteam", _Inst(ws))
    r = client.post(
        "/api/paste-image?session=peer-redteam&name=shot.png",
        content=PNG,
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 200
    path = r.json()["path"]
    assert os.path.dirname(path) == str(ws / ".mindflock_pastes")
    with open(path, "rb") as f:
        assert f.read() == PNG


def test_pause_refused_for_peer_session(tmp_path, monkeypatch):
    calls = []

    class _PauseInst(_Inst):
        def Pause(self):
            calls.append("pause")

    monkeypatch.setitem(server.ENGINE.instances, "peer-redteam", _PauseInst(tmp_path))
    r = client.post("/api/instances/peer-redteam/pause")
    assert r.status_code == 409
    assert r.json().get("peer_share") is True
    assert calls == []
