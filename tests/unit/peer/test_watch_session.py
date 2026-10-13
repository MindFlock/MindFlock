"""Watching a peer's shared session (``screen`` op, their-* routes) and the
share fixes from the first two-person trial: a plain folder is shared as a
snapshot, a share failure says what broke, and new share ids keep macOS
socket paths short."""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest

from backend.peer import screen, share as share_mod, transport, wire
from backend.peer import service as svc_mod

from .test_integration_routes import FakeTransport, client, link, svc  # noqa: F401
from .test_service import LID, engine, make_service, op_error, run  # noqa: F401


# --------------------------------------------------------------------------- #
# sanitize: colours in, everything a terminal would ACT on out
# --------------------------------------------------------------------------- #
def test_sanitize_keeps_colours_only():
    raw = (
        "\x1b[1;32mok\x1b[0m plain"
        "\x1b]52;c;ZXZpbA==\x07"  # OSC 52: write the viewer's clipboard
        "\x1b]8;;https://evil.example\x1b\\link\x1b]8;;\x1b\\"  # OSC 8 hyperlink
        "\x1b[2J\x1b[H"  # clear + home
        "\x1bPq#0;2;0;0;0\x1b\\"  # DCS (sixel)
        "\x9b31m"  # C1 CSI
        "\x07\x08tail\r\n"
    )
    assert screen.sanitize(raw) == "\x1b[1;32mok\x1b[0m plainlinktail\n"


def test_sanitize_drops_a_lone_escape_and_bounds_the_size():
    assert screen.sanitize("a\x1bb") == "ab"
    assert len(screen.sanitize("x" * (screen.MAX_SCREEN_CHARS + 50))) == (
        screen.MAX_SCREEN_CHARS
    )
    assert screen.sanitize(None) == ""


# --------------------------------------------------------------------------- #
# wire + perms
# --------------------------------------------------------------------------- #
def test_screen_is_a_wire_op_gated_like_diff():
    assert "screen" in wire.OPS
    assert wire.validate_request("screen", {}) == {}
    ok = {"text": "\x1b[31mhi\x1b[0m", "cols": 80, "rows": 24, "agent": "running"}
    assert wire.validate_response("screen", ok) == ok
    for bad in (
        dict(ok, cols=5000),
        dict(ok, agent="dancing"),
        dict(ok, text="x" * (wire.MAX_SCREEN_CHARS + 1)),
    ):
        with pytest.raises(wire.ProtocolError):
            wire.validate_response("screen", bad)
    assert transport._OP_PERM["screen"] == "diff"


# --------------------------------------------------------------------------- #
# the sharer's side: handle_request("screen")
# --------------------------------------------------------------------------- #
def test_screen_needs_the_diff_permission(engine):  # noqa: F811
    lnk = link(perms={"messages": True, "diff": False, "read_file": True})
    svc = make_service(lnk)
    assert "not permitted" in op_error(svc, lnk, "screen", {})


def test_screen_without_a_share_says_so(engine):  # noqa: F811
    lnk = link()
    svc = make_service(lnk)
    assert "no shared folder" in op_error(svc, lnk, "screen", {})


def test_screen_of_a_stopped_session_is_empty(engine):  # noqa: F811
    lnk = link(share_id="ab" * 10, session_title="peer-bob-ababab")
    svc = make_service(lnk)
    out = run(svc.handle_request(lnk, "screen", {}))
    assert out == {"text": "", "cols": 0, "rows": 0, "agent": "none"}


def test_screen_captures_only_the_links_own_session(engine, monkeypatch):  # noqa: F811
    from backend.web import server

    title = "peer-bob-ababab"
    engine[title] = SimpleNamespace(PeerShare="ab" * 10)
    monkeypatch.setattr(server, "_live_session_name", lambda n: n)
    seen = []

    def fake_capture(name):
        seen.append(name)
        return {"text": "\x1b[32m$\x1b[0m ", "cols": 120, "rows": 40}

    monkeypatch.setattr(screen, "capture", fake_capture)
    lnk = link(share_id="ab" * 10, session_title=title)
    svc = make_service(lnk)
    out = run(svc.handle_request(lnk, "screen", {}))
    assert out["agent"] == "running" and out["cols"] == 120
    assert seen == ["mindflock_" + title]
    assert wire.validate_response("screen", out) == out


# --------------------------------------------------------------------------- #
# the viewer's side: their_screen / their_files / their_file, their_share
# --------------------------------------------------------------------------- #
def _with_transport(svc, answer):
    class T(FakeTransport):
        async def request(self, lid, op, p, timeout=60.0):
            return answer(op, p)

    svc._transport = T()
    svc._transport_started = True


def test_their_screen_arrives_sanitized(engine):  # noqa: F811
    svc = make_service()
    _with_transport(
        svc,
        lambda op, p: {
            "text": "\x1b[33mwarn\x1b[0m\x1b]52;c;eA==\x07",
            "cols": 80,
            "rows": 24,
            "agent": "running",
        },
    )
    out = run(svc.their_screen(LID))
    assert out["text"] == "\x1b[33mwarn\x1b[0m"


def test_an_older_peer_is_told_to_update(engine):  # noqa: F811
    svc = make_service()

    def answer(op, p):
        from backend.peer.transport import PeerOpError

        raise PeerOpError("unsupported")

    _with_transport(svc, answer)
    with pytest.raises(svc_mod.PeerServiceError, match="need to update") as exc:
        run(svc.their_screen(LID))
    assert exc.value.status == 409


def test_their_files_and_one_file(engine):  # noqa: F811
    svc = make_service()
    calls = []

    def answer(op, p):
        calls.append((op, p))
        if op == "list_files":
            return {"files": ["a.py"], "truncated": False}
        return {
            "path": p["path"],
            "size": 2,
            "encoding": "utf-8",
            "content": "hi",
            "truncated": False,
        }

    _with_transport(svc, answer)
    assert run(svc.their_files(LID))["files"] == ["a.py"]
    assert run(svc.their_file(LID, "a.py"))["content"] == "hi"
    assert calls == [("list_files", {}), ("read_file", {"path": "a.py"})]
    with pytest.raises(svc_mod.PeerServiceError):
        run(svc.their_file(LID, "  "))


def test_their_share_rides_on_the_link_view(engine):  # noqa: F811
    svc = make_service()
    _with_transport(
        svc, lambda op, p: {"shared": True, "agent": "running", "name": "bob"}
    )

    async def go():
        lnk = svc.store.get(LID)
        first = svc.link_view(lnk)["their_share"]  # not asked yet: unknown
        await asyncio.sleep(0)  # the refresh it scheduled
        await asyncio.sleep(0)
        return first, svc.link_view(lnk)["their_share"]

    first, later = run(go())
    assert first is None
    assert later == {"shared": True, "agent": "running"}


def test_their_share_is_forgotten_when_disconnected(engine):  # noqa: F811
    svc = make_service()
    svc._their_status[LID] = (0.0, {"shared": True, "agent": "running"})
    assert svc._their_share_view(LID, connected=False) is None
    assert LID not in svc._their_status


# --------------------------------------------------------------------------- #
# routes: a failure says what broke (Mauricio's bare 500)
# --------------------------------------------------------------------------- #
def test_an_unexpected_share_failure_is_a_readable_500(
    svc, client, monkeypatch
):  # noqa: F811
    async def boom(*a, **k):
        raise OSError("unix socket path too long: /Users/x/.mindflock/peer/…")

    monkeypatch.setattr(svc, "share", boom)
    r = client.post("/api/peer/links/" + LID + "/share", json={"repo_path": "/tmp"})
    assert r.status_code == 500
    assert r.json()["error"].startswith("OSError: unix socket path too long")


def test_their_screen_route(svc, client, monkeypatch):  # noqa: F811
    async def fake(lid):
        return {"text": "hi", "cols": 1, "rows": 1, "agent": "running"}

    monkeypatch.setattr(svc, "their_screen", fake)
    r = client.get("/api/peer/links/" + LID + "/their-screen")
    assert r.status_code == 200 and r.json()["text"] == "hi"


# --------------------------------------------------------------------------- #
# sharing a plain folder: a snapshot, the folder untouched
# --------------------------------------------------------------------------- #
@pytest.fixture
def peer_home(tmp_path, monkeypatch):
    home = tmp_path / "peer"
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(home))
    return home


def _plain_folder(tmp_path):
    d = tmp_path / "calc"
    (d / "src").mkdir(parents=True)
    (d / "node_modules" / "left-pad").mkdir(parents=True)
    (d / "main.py").write_text("print(1)\n")
    (d / "src" / "a.txt").write_text("hi\n")
    (d / "node_modules" / "left-pad" / "index.js").write_text("x\n")
    (d / ".gitignore").write_text("*.log\n")
    (d / "run.log").write_text("noise\n")
    return d


def test_a_plain_folder_is_shared_as_a_snapshot(peer_home, tmp_path):
    folder = _plain_folder(tmp_path)
    before = sorted(os.listdir(folder))
    sh = share_mod.create_share(LID, str(folder))
    assert share_mod.list_files(sh)["files"] == [".gitignore", "main.py", "src/a.txt"]
    # The folder was only read: no .git appeared, nothing changed.
    assert sorted(os.listdir(folder)) == before
    # The throwaway git dir is gone; only the share is left.
    assert os.listdir(peer_home / "shares") == [sh.share_id]
    # And the share behaves like any other: an edit shows in the diff.
    with open(os.path.join(sh.work, "main.py"), "a") as fh:
        fh.write("print(2)\n")
    assert "print(2)" in share_mod.diff(sh, 20000)["diff"]


def test_a_folder_inside_a_repo_is_a_snapshot_of_just_that_folder(peer_home, tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "top.txt").write_text("t\n")
    (repo / "sub" / "inner.txt").write_text("i\n")
    for args in (
        ["init", "-q"],
        ["-c", "user.email=t@e", "-c", "user.name=T", "add", "-A"],
        [
            "-c",
            "user.email=t@e",
            "-c",
            "user.name=T",
            "commit",
            "-qm",
            "c",
            "--no-gpg-sign",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    sh = share_mod.create_share(LID, str(repo / "sub"))
    assert share_mod.list_files(sh)["files"] == ["inner.txt"]


@pytest.mark.parametrize(
    "setup,branch,msg",
    [
        ("empty", None, "that folder is empty"),
        ("files", "main", "no branch to pick"),
    ],
)
def test_snapshot_refusals_say_why(peer_home, tmp_path, setup, branch, msg):
    folder = tmp_path / "f"
    folder.mkdir()
    if setup == "files":
        (folder / "a").write_text("a")
    with pytest.raises(share_mod.ShareError, match=msg):
        share_mod.create_share(LID, str(folder), branch)
    assert not (peer_home / "shares").exists() or os.listdir(peer_home / "shares") == []


def test_an_oversized_snapshot_is_refused(peer_home, tmp_path, monkeypatch):
    folder = _plain_folder(tmp_path)
    monkeypatch.setattr(share_mod, "SNAPSHOT_MAX_FILES", 2)
    with pytest.raises(share_mod.ShareError, match="too many to share"):
        share_mod.create_share(LID, str(folder))
    assert os.listdir(peer_home / "shares") == []


def test_new_share_ids_are_short(peer_home, tmp_path):
    """macOS caps a socket path at 104 bytes and the id sits in it."""
    sh = share_mod.create_share(LID, str(_plain_folder(tmp_path)))
    assert len(sh.share_id) == 20


def test_the_agent_socket_temp_name_is_no_longer_than_its_final_name():
    import inspect

    from backend.peer import agent_api

    src = inspect.getsource(agent_api.AgentApi._bind)
    assert '".a%s.s" % secrets.token_hex(3)' in src
    assert len(".a" + "ff" * 3 + ".s") <= len("agent.sock")
