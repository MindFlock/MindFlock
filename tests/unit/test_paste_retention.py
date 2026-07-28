"""Pasted-screenshot retention: keep the 10 newest, wipe everything on restart.

Pasted images (phone screenshots via /m, Ctrl+V in a pane) are transient input
for a live conversation — they must never accumulate disk across sessions or
survive a server restart.
"""

from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


def _global_paste_dir(home) -> str:
    return os.path.join(str(home), ".mindflock", "pastes")


def _pastes(base) -> list:
    try:
        return sorted(n for n in os.listdir(base) if n.startswith("paste-"))
    except OSError:
        return []


def test_endpoint_caps_at_ten_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    last_path = ""
    for _ in range(12):
        r = client.post(
            "/api/paste-image", content=PNG, headers={"content-type": "image/png"}
        )
        assert r.status_code == 200
        last_path = r.json()["path"]
    base = _global_paste_dir(tmp_path)
    assert len(_pastes(base)) == 10
    # The paste just taken is never the one pruned away.
    assert os.path.isfile(last_path)


def test_prune_keeps_newest_by_mtime(tmp_path):
    base = tmp_path / "pastes"
    base.mkdir()
    now = time.time()
    for i in range(15):
        p = base / f"paste-2026-{i:02d}.png"
        p.write_bytes(PNG)
        os.utime(p, (now + i, now + i))  # i = age rank: higher = newer
    server._prune_pastes(str(base))
    kept = _pastes(base)
    assert len(kept) == 10
    assert kept == [f"paste-2026-{i:02d}.png" for i in range(5, 15)]


def test_prune_never_touches_foreign_files(tmp_path):
    base = tmp_path / "pastes"
    base.mkdir()
    keeper = base / "my-important-notes.png"
    keeper.write_bytes(PNG)
    for i in range(12):
        (base / f"paste-{i:02d}.png").write_bytes(PNG)
    server._prune_pastes(str(base), keep=0)
    assert _pastes(base) == []
    assert keeper.is_file()  # non paste-* files are sacred


def test_restart_clears_global_and_workspace_pastes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Global dir with pastes.
    gbase = _global_paste_dir(tmp_path)
    os.makedirs(gbase)
    for i in range(3):
        with open(os.path.join(gbase, f"paste-{i}.png"), "wb") as f:
            f.write(PNG)
    # A session workspace with its own .mindflock_pastes.
    ws = tmp_path / "ws"
    wbase = ws / ".mindflock_pastes"
    wbase.mkdir(parents=True)
    (wbase / "paste-phone.png").write_bytes(PNG)

    class _Inst:
        Path = str(ws)

        def Started(self):
            return True

        def GetWorktreePath(self):
            return str(ws)

    monkeypatch.setitem(server.ENGINE.instances, "paste-test-inst", _Inst())
    server._clear_all_pastes()
    assert _pastes(gbase) == []
    assert _pastes(wbase) == []


def test_clear_all_pastes_never_raises_on_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no pastes dir exists at all
    server._clear_all_pastes()  # must be a silent no-op


# --- generalized file upload (drag-drop / file paste) ------------------------


def test_safe_upload_name_strips_paths_and_specials():
    assert server._safe_upload_name("report.pdf") == "report.pdf"
    assert server._safe_upload_name("../../etc/passwd") == "passwd"
    assert server._safe_upload_name("..\\..\\evil.exe") == "evil.exe"
    assert server._safe_upload_name(".bashrc") == "bashrc"
    assert server._safe_upload_name("my file (1).txt") == "my_file_1_.txt"
    assert server._safe_upload_name("") == ""
    assert len(server._safe_upload_name("x" * 300 + ".tar.gz")) <= 80


def test_upload_keeps_original_name_and_stays_in_paste_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = client.post(
        "/api/paste-image?name=../../notes%20v2.txt",
        content=b"hello agent",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 200
    path = r.json()["path"]
    base = _global_paste_dir(tmp_path)
    assert os.path.dirname(path) == base  # traversal in ?name= can't escape
    name = os.path.basename(path)
    assert name.startswith("paste-")  # retention pruning still applies
    assert name.endswith("-notes_v2.txt")
    with open(path, "rb") as f:
        assert f.read() == b"hello agent"


def test_upload_without_name_maps_type_to_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = client.post(
        "/api/paste-image",
        content=b"%PDF-1.4",
        headers={"content-type": "application/pdf"},
    )
    assert r.status_code == 200
    assert r.json()["path"].endswith(".bin")  # unknown non-image type
    r = client.post(
        "/api/paste-image", content=PNG, headers={"content-type": "image/png"}
    )
    assert r.json()["path"].endswith(".png")  # image behavior unchanged
