"""Small send fixes from the first real cross-device run.

* ``send-files --from TITLE`` reads relative paths from that session's
  folder, not from wherever the CLI ran.
* A session that was just closed (an auto-closed worker) answers "closed N
  min ago — reopen it (mindflock reopen X)", from the API, the CLI and the
  MCP — and a no-commit worker is not auto-closed while its parent is still
  mid-turn.
* Git-ignored files stay out — a NAMED ignored file and an ignored folder
  included — unless the owner names ONE such file with
  ``--include-ignored`` (a built installer in a build-output folder).
"""

from __future__ import annotations

import datetime
import os
import subprocess

import pytest

from backend.mcp.protocol import ToolError
from backend.web.core import auto_close as ac
from backend.web.core import recently_closed
from backend.web.core import send_files
from backend.web.core.send_session import Refusal

from tests.unit import test_auto_close as tac
from tests.unit import test_send_files as tsf
from tests.unit._mcp_fakes import FakeCtx, make_box, row

world = tsf.world  # the send_files fixture (repo, fake device, inbox)
_quiet = tac._quiet  # auto_close: no tmux, no team runs, no routes


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=a", "-c", "user.email=a@b.invalid", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A worktree with an ignored build-output folder holding an installer."""
    r = tmp_path / "wt"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text("electron/dist/\n*.log\n.env\n")
    (r / "README.md").write_text("hi\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    dist = r / "electron" / "dist"
    (dist / "sub").mkdir(parents=True)
    (dist / "Tripwire-Setup.exe").write_bytes(b"MZ" + b"\0" * 64)
    (dist / "sub" / "latest.yml").write_text("version: 1\n")
    (r / "debug.log").write_text("noise\n")
    (r / ".env").write_text("TOKEN=x\n")
    return r


# --------------------------------------------------------------------------- #
# ignored files
# --------------------------------------------------------------------------- #
def test_a_named_ignored_file_is_refused_by_default_with_the_flag_named(repo):
    with pytest.raises(Refusal) as err:
        send_files.collect(str(repo), ["electron/dist/Tripwire-Setup.exe"])
    assert err.value.code == "ignored"
    assert "--include-ignored" in err.value.message
    assert "mindflock send-files DEVICE electron/dist/Tripwire-Setup.exe" in (
        err.value.message
    )


def test_an_ignored_folder_sends_nothing(repo):
    plan = send_files.collect(str(repo), ["electron/dist", "README.md"])
    assert [f["path"] for f in plan["files"]] == ["README.md"]
    assert {"path": "electron/dist/", "why": "ignored by git"} in plan["skipped"]
    # A folder inside the ignored one is just as ignored.
    plan = send_files.collect(str(repo), ["electron/dist/sub", "README.md"])
    assert [f["path"] for f in plan["files"]] == ["README.md"]


def test_include_ignored_sends_exactly_one_named_file(repo):
    plan = send_files.collect(
        str(repo), ["electron/dist/Tripwire-Setup.exe"], ignored_ok=True
    )
    assert [f["path"] for f in plan["files"]] == ["electron/dist/Tripwire-Setup.exe"]
    assert plan["ignored"] == ["electron/dist/Tripwire-Setup.exe"]
    # Absolute, inside the worktree: the same.
    plan = send_files.collect(
        str(repo), [str(repo / "electron/dist/Tripwire-Setup.exe")], ignored_ok=True
    )
    assert plan["ignored"] == ["electron/dist/Tripwire-Setup.exe"]


def test_include_ignored_never_widens_to_folders_or_several_paths(repo):
    with pytest.raises(Refusal) as err:
        send_files.collect(str(repo), ["electron/dist"], ignored_ok=True)
    assert err.value.code == "include_ignored_one_file"
    with pytest.raises(Refusal) as err:
        send_files.collect(
            str(repo),
            ["electron/dist/Tripwire-Setup.exe", "debug.log"],
            ignored_ok=True,
        )
    assert err.value.code == "include_ignored_one_file"


def test_include_ignored_still_refuses_secrets_and_outside_paths(repo, tmp_path):
    with pytest.raises(Refusal) as err:
        send_files.collect(str(repo), [".env"], ignored_ok=True)
    assert err.value.code == "secret"
    outside = tmp_path / "Setup.exe"
    outside.write_bytes(b"MZ")
    with pytest.raises(Refusal) as err:
        send_files.collect(str(repo), [str(outside)], ignored_ok=True)
    assert err.value.code == "outside_worktree"


def test_include_ignored_still_has_the_size_limit(repo):
    with pytest.raises(Refusal) as err:
        send_files.collect(
            str(repo),
            ["electron/dist/Tripwire-Setup.exe"],
            ignored_ok=True,
            max_bytes=10,
        )
    assert err.value.code == "too_large"


def test_a_tracked_file_matching_an_ignore_rule_is_not_ignored(repo):
    (repo / "kept.log").write_text("tracked\n")
    _git(repo, "add", "-f", "kept.log")
    _git(repo, "commit", "-qm", "track a log")
    plan = send_files.collect(str(repo), ["kept.log"])
    assert [f["path"] for f in plan["files"]] == ["kept.log"]


def test_the_route_takes_include_ignored_end_to_end(world):
    here = world["here"]
    (here / ".gitignore").write_text("dist/\n")
    (here / "dist").mkdir()
    (here / "dist" / "Setup.exe").write_bytes(b"MZ installer")
    r = tsf._send(["dist/Setup.exe"])
    assert r.status_code == 403 and r.json()["code"] == "ignored"
    r = tsf._send(["dist/Setup.exe"], include_ignored="yes")
    assert r.status_code == 400 and r.json()["code"] == "bad_request"
    r = tsf._send(["dist/Setup.exe"], include_ignored=True)
    assert r.status_code == 200, r.text
    assert r.json()["ignored"] == ["dist/Setup.exe"]
    assert [f["path"] for f in r.json()["files"]] == ["dist/Setup.exe"]


def test_the_mcp_does_not_offer_include_ignored():
    from backend.mcp import devices

    assert "include_ignored" not in devices.S_SEND_FILES["properties"]


def test_limits_take_a_190_mb_installer():
    lim = send_files.limits()
    assert lim["max_bytes"] >= 512 * 1024 * 1024 > 190 * 1024 * 1024


# --------------------------------------------------------------------------- #
# --from TITLE: relative paths read from the session's folder
# --------------------------------------------------------------------------- #
def test_from_reads_relative_paths_from_the_session_folder(tmp_path, monkeypatch):
    from backend import cli

    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    paths, how = cli._paths_for_session(["dist/x.exe", "/abs/y"], str(wt))
    assert paths == [str(wt.resolve() / "dist/x.exe"), "/abs/y"]
    assert "session's folder" in how and str(wt.resolve()) in how
    # Inside the session's folder: the shell's meaning (the current dir).
    monkeypatch.chdir(wt / "sub")
    paths, how = cli._paths_for_session(["x.exe"], str(wt))
    assert paths == [str((wt / "sub" / "x.exe").resolve())]
    assert "current directory" in how
    # All absolute: nothing to say.
    assert cli._paths_for_session(["/a"], str(wt)) == (["/a"], "")


# --------------------------------------------------------------------------- #
# a session that was just closed
# --------------------------------------------------------------------------- #
def _closed(monkeypatch, title, minutes_ago):
    at = (
        datetime.datetime.now().astimezone()
        - datetime.timedelta(minutes=minutes_ago, seconds=5)
    ).isoformat()
    entry = {"id": title + "-1", "title": title, "closed_at": at}
    monkeypatch.setattr(recently_closed, "_load_recently_closed", lambda: [entry])
    return entry


def test_closed_hint_says_how_long_ago_and_how_to_reopen(monkeypatch):
    _closed(monkeypatch, "tripwire-w1", 3)
    hit = recently_closed.closed_hint("tripwire-w1")
    assert hit["minutes"] == 3 and hit["id"] == "tripwire-w1-1"
    assert hit["sentence"].startswith("tripwire-w1 was closed 3 min ago")
    assert "mindflock reopen tripwire-w1" in hit["sentence"]
    assert recently_closed.closed_hint("other") is None


def test_send_routes_say_closed_n_min_ago(world, monkeypatch):
    _closed(monkeypatch, "gone-worker", 4)
    from fastapi.testclient import TestClient

    from backend.web import server

    c = TestClient(server.app)
    for path, body in (
        ("send-files", {"device": "MLRIG2023", "paths": ["README.md"]}),
        ("send-to-device", {"device": "MLRIG2023"}),
    ):
        r = c.post("/api/instances/gone-worker/%s" % path, json=body)
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "closed"
        assert "closed 4 min ago" in r.json()["error"]
        assert "mindflock reopen gone-worker" in r.json()["error"]


def test_cli_send_names_the_reopen_command(monkeypatch):
    from backend import cli
    from backend import client

    at = datetime.datetime.now().astimezone().isoformat()
    monkeypatch.setattr(
        client,
        "get",
        lambda base, path, **k: [{"id": "w-1", "title": "w", "closed_at": at}],
    )
    with pytest.raises(client.ClientError) as err:
        cli._closed_or_raise("http://x", [], "w")
    assert "w was closed just now" in str(err.value)
    assert "mindflock reopen w" in str(err.value)


def test_mcp_send_files_says_the_worker_was_closed():
    from tests.unit.test_send_session import _tree

    box, api, clock = make_box(_tree(), me="orch")
    api.top_routes[("GET", "/api/recently-closed")] = [
        {"id": "x-1", "title": "orch-w1", "closed_at": "2026-11-22T10:00:00+00:00"}
    ]
    with pytest.raises(ToolError) as err:
        box.send_files(
            {"device": "mac", "paths": ["a"], "title": "orch-w1"}, FakeCtx(clock)
        )
    assert "orch-w1 was closed" in str(err.value)
    assert "mindflock reopen orch-w1" in str(err.value)


# --------------------------------------------------------------------------- #
# a no-commit worker waits while its parent is mid-turn
# --------------------------------------------------------------------------- #
def test_a_no_commit_worker_waits_while_its_parent_works(monkeypatch):
    monkeypatch.setattr(
        ac,
        "work_accounted",
        lambda srv, inst, title, row, parent, orphan: (True, ac.W_NO_COMMITS),
    )
    srv, w = tac._world()
    srv.activity["lead"] = "working"
    verdict, why, _ = tac._judge(srv, w)
    assert verdict == ac.WAIT and why == ac.R_PARENT_BUSY
    srv.activity["lead"] = "idle"
    assert tac._judge(srv, w)[0] == ac.CLOSE


def test_a_merged_worker_does_not_wait_for_a_busy_parent(monkeypatch):
    srv, w = tac._world()
    srv.activity["lead"] = "working"
    assert tac._judge(srv, w)[0] == ac.CLOSE
