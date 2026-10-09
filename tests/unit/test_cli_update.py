"""``mindflock update`` / ``restart`` / ``devices update`` (backend.cli_update).

Thin clients, so what is worth pinning is the waiting: an update is only
"done" once the server's own hello answers with the new build, a failed or
rolled-back install exits non-zero with the reason, and a fleet rollout
prints each device's progress and fails when the rollout halts.
"""

from __future__ import annotations

import builtins

import pytest

from backend import cli, cli_update, client

BASE = "http://127.0.0.1:8765"


class Fake:
    """``gets``/``posts`` map a path to an answer, or to a list consumed one
    per call (the last one sticks); an exception instance is raised."""

    def __init__(self, monkeypatch):
        self.gets: dict = {}
        self.posts: dict = {}
        self.calls: list = []
        monkeypatch.setattr(client, "discover", lambda host=None, port=None: BASE)
        monkeypatch.setattr(client, "get", self._get)
        monkeypatch.setattr(client, "post", self._post)
        monkeypatch.setattr(cli_update.time, "sleep", lambda s: None)

    @staticmethod
    def _answer(table, path, default):
        ans = table.get(path, default)
        if isinstance(ans, list):
            ans = ans.pop(0) if len(ans) > 1 else ans[0]
        if isinstance(ans, BaseException):
            raise ans
        return ans

    def _get(self, base, path, timeout=None):
        self.calls.append(("GET", path, None))
        return self._answer(self.gets, path, None)

    def _post(self, base, path, payload=None, timeout=None):
        self.calls.append(("POST", path, payload))
        return self._answer(self.posts, path, {"ok": True})


@pytest.fixture()
def srv(monkeypatch):
    return Fake(monkeypatch)


def _yes(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")


_CHECK = {
    "current": "0.7.4",
    "commit": "b" * 40,
    "latest": "9.9.9",
    "checked": True,
    "available": True,
    "blocked": "",
    "restart_pending": False,
}


def test_commands_are_wired():
    assert "update" in cli._SESSION_COMMANDS and "restart" in cli._SESSION_COMMANDS
    args = cli._build_parser().parse_args(
        ["devices", "update", "--tag", "v9.9.9", "-y"]
    )
    assert args.devices_command == "update" and args.tag == "v9.9.9"
    args = cli._build_parser().parse_args(["update", "--check"])
    assert args.check is True and args.ref is None


def test_check_only_prints(srv, capsys):
    srv.gets["/api/update/check?refresh=1"] = _CHECK
    assert cli.main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "v0.7.4 (bbbbbbb)" in out and "newest release: v9.9.9" in out
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_update_waits_for_the_server_to_answer_on_the_new_build(
    srv, monkeypatch, capsys
):
    _yes(monkeypatch)
    srv.gets["/api/update/check?refresh=1"] = _CHECK
    srv.posts["/api/update/start"] = {"ok": True, "ref": "v9.9.9", "commit": "a" * 40}
    srv.gets["/api/update/state"] = [
        {"state": "started"},
        client.ServerNotFound(),  # it restarted between polls
        {"state": "done"},
    ]
    srv.gets["/api/remote/hello"] = [
        {"version": "0.7.4", "commit": "b" * 40},  # not restarted yet
        None,
        {"version": "9.9.9", "commit": "a" * 40},
    ]
    assert cli.main(["update"]) == 0
    assert ("POST", "/api/update/start", {}) in srv.calls
    assert "MindFlock v9.9.9 (aaaaaaa) is running" in capsys.readouterr().out


def test_a_rolled_back_update_fails_with_the_reason(srv, monkeypatch, capsys):
    _yes(monkeypatch)
    srv.gets["/api/update/check?refresh=1"] = _CHECK
    srv.posts["/api/update/start"] = {"ok": True, "ref": "v9.9.9", "commit": "a" * 40}
    srv.gets["/api/update/state"] = {
        "state": "rolled_back",
        "error": "the new version did not start, so the previous one was put back",
        "log": ["boom"],
    }
    assert cli.main(["update", "-y"]) == 1
    assert "previous one was put back" in capsys.readouterr().err


def test_a_dev_checkout_is_refused_before_anything_starts(srv, capsys):
    srv.gets["/api/update/check?refresh=1"] = {
        **_CHECK,
        "blocked": "development checkout",
    }
    assert cli.main(["update", "-y"]) == 1
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_already_current_does_nothing(srv, capsys):
    srv.gets["/api/update/check?refresh=1"] = {**_CHECK, "available": False}
    assert cli.main(["update", "-y"]) == 0
    assert "Already on the newest release" in capsys.readouterr().out


def test_restart_waits_for_the_server_to_come_back(srv, capsys):
    srv.gets["/api/remote/hello"] = [None, {"version": "9.9.9", "commit": ""}]
    assert cli.main(["restart"]) == 0
    assert ("POST", "/api/server/restart", None) in srv.calls
    assert "restarted — MindFlock v9.9.9" in capsys.readouterr().out


def test_devices_update_prints_progress_and_reports_a_halt(srv, monkeypatch, capsys):
    _yes(monkeypatch)
    srv.gets["/api/fleet"] = {
        "in_fleet": True,
        "members": [
            {"key": "laptop", "host": "Laptop", "self": True, "version": "0.7.4"},
            {"key": "rig", "host": "Rig", "reachable": True, "version": "0.7.4"},
        ],
    }
    srv.posts["/api/fleet/update"] = {"state": "running", "tag": "v9.9.9"}
    srv.gets["/api/fleet/update"] = [
        {
            "state": "running",
            "members": [{"key": "rig", "host": "Rig", "step": "updating"}],
        },
        {
            "state": "halted",
            "error": "Rig: v9.9.9 didn't start there — it was rolled back",
            "members": [
                {
                    "key": "rig",
                    "host": "Rig",
                    "step": "failed",
                    "detail": "v9.9.9 didn't start there — it was rolled back",
                },
                {
                    "key": "laptop",
                    "host": "Laptop",
                    "self": True,
                    "step": "not_started",
                },
            ],
        },
    ]
    assert cli.main(["devices", "update", "--tag", "v9.9.9"]) == 1
    out, err = capsys.readouterr()
    assert "Rig: updating" in out and "Rig: FAILED" in out
    assert "stopped: Rig:" in err
    assert ("POST", "/api/fleet/update", {"tag": "v9.9.9"}) in srv.calls


def test_update_all_devices_is_the_fleet_rollout(srv, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli_update, "cmd_devices_update", lambda args: seen.append(args.tag) or 0
    )
    assert cli.main(["update", "--all-devices", "--ref", "v9.9.9"]) == 0
    assert seen == ["v9.9.9"]


def test_with_no_server_it_installs_in_place(monkeypatch, capsys):
    from backend.web.core import self_update

    def _none(host=None, port=None):
        raise client.ServerNotFound()

    monkeypatch.setattr(client, "discover", _none)
    monkeypatch.setattr(cli_update.time, "sleep", lambda s: None)
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    started = []
    monkeypatch.setattr(
        self_update,
        "start_update",
        lambda ref: started.append(ref) or {"ok": True, "commit": "a" * 40},
    )
    monkeypatch.setattr(self_update, "read_state", lambda: {"state": "done"})
    assert cli.main(["update", "--ref", "v9.9.9", "-y"]) == 0
    assert started == ["v9.9.9"]
    assert "start it with: mindflock serve" in capsys.readouterr().out
