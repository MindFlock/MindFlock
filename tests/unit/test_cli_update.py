"""``mindflock update`` / ``restart`` / ``devices update`` (backend.cli_update).

``update`` runs the install itself, the way install.json says this engine
was installed (uv tool, pipx — a dev checkout only gets the git commands),
with a stubbed subprocess here so nothing on the suite's machine moves; then
it restarts a running server and is only "done" once the server's own hello
answers with the new build. ``restart`` and the fleet rollout are thin
clients: a failed device exits non-zero with the reason.
"""

from __future__ import annotations

import builtins
import json
import subprocess

import pytest

from backend import cli, cli_update, client

_REAL_FIND = cli_update.find_own_server

BASE = "http://127.0.0.1:8765"


class Fake:
    """``gets``/``posts`` map a path to an answer, or to a list consumed one
    per call (the last one sticks); an exception instance is raised."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.gets: dict = {}
        self.posts: dict = {}
        self.calls: list = []
        monkeypatch.setattr(client, "discover", lambda host=None, port=None: BASE)
        # The server of THIS install answers at BASE (no process scan).
        monkeypatch.setattr(cli_update, "find_own_server", lambda: (BASE, []))
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


@pytest.fixture(autouse=True)
def _no_process_scan(monkeypatch):
    """ "The server of this install" is whatever ``client.discover`` answers
    here: no test scans the machine's real processes or probes real ports."""

    def _find():
        try:
            return client.discover(), []
        except client.ServerNotFound:
            return "", []

    monkeypatch.setattr(cli_update, "find_own_server", _find)


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


# --------------------------------------------------------------------------- #
# update (this machine): one path per install method, nothing real run
# --------------------------------------------------------------------------- #
class Installer:
    """A stubbed subprocess: records the argv `mindflock update` runs and
    answers with ``rc``. Nothing is installed on the machine running the
    suite."""

    def __init__(self, monkeypatch, rc=0):
        self.argv: list = []
        self.rc = rc

        def _run(argv, check=False, **kw):
            self.argv.append(list(argv))
            return subprocess.CompletedProcess(argv, self.rc)

        monkeypatch.setattr(cli_update.subprocess, "run", _run)


@pytest.fixture()
def inst(monkeypatch, tmp_path):
    """A release install whose install.json lives in a temp MINDFLOCK_HOME:
    the running package is NOT a dev checkout (the suite's own is), the
    newest release is v9.9.9, and every tag resolves to commit aaaa…"""
    from backend.web.core import self_update

    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path))
    monkeypatch.setattr(self_update, "_detected_kind", lambda: "uv-tool")
    monkeypatch.setattr(self_update, "installed_version", lambda: "0.7.4")
    monkeypatch.setattr(self_update, "installed_commit", lambda: "b" * 40)
    monkeypatch.setattr(self_update, "_resolve_commit", lambda ref: "a" * 40)
    monkeypatch.setattr(self_update.shutil, "which", lambda name: "/usr/bin/" + name)

    async def _tag(force=False):
        return "v9.9.9"

    monkeypatch.setattr(self_update, "latest_release_tag", _tag)
    return tmp_path


def _install_json(home, **fields):
    data = {"method": "uv-tool", "ref": "v0.7.4", "sha": "b" * 40, "python": "3.12"}
    data.update(fields)
    (home / "install.json").write_text(json.dumps(data), encoding="utf-8")
    return home / "install.json"


def test_check_prints_both_versions_and_exits_1_when_behind(srv, inst, capsys):
    _install_json(inst)
    run = Installer(srv_monkeypatch(srv))
    assert cli.main(["update", "--check"]) == 1
    out = capsys.readouterr().out
    assert "v0.7.4 (bbbbbbb)" in out and "newest release: v9.9.9" in out
    assert "installed with uv tool (install.json)" in out
    assert run.argv == [] and not [c for c in srv.calls if c[0] == "POST"]


def test_check_exits_0_when_current_and_2_when_github_is_unreachable(
    srv, inst, monkeypatch, capsys
):
    from backend.web.core import self_update

    _install_json(inst)
    monkeypatch.setattr(self_update, "installed_version", lambda: "9.9.9")
    assert cli.main(["update", "--check"]) == 0
    assert "Already on the newest release" in capsys.readouterr().out

    async def _none(force=False):
        return ""

    monkeypatch.setattr(self_update, "latest_release_tag", _none)
    assert cli.main(["update", "--check"]) == 2
    assert "Couldn't reach GitHub" in capsys.readouterr().out


def srv_monkeypatch(srv):
    # The Fake keeps the monkeypatch it was built with (see Fake.__init__).
    return srv.monkeypatch


def test_uv_tool_runs_the_pinned_install_then_restarts_the_server(
    srv, inst, monkeypatch, capsys
):
    from backend.web.core import self_update

    _install_json(inst)
    run = Installer(monkeypatch)
    srv.gets["/api/remote/hello"] = [
        {"version": "0.7.4", "commit": "b" * 40},  # not restarted yet
        None,
        {"version": "9.9.9", "commit": "a" * 40},
    ]
    assert cli.main(["update", "-y"]) == 0
    # THE install line: the same pinned command the server's updater runs,
    # on the resolved commit rather than the moving tag.
    assert run.argv == [
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--python",
            self_update.INSTALL_PYTHON,
            "mindflock[web] @ git+%s@%s" % (self_update.INSTALL_REPO, "a" * 40),
        ]
    ]
    assert run.argv[0] == self_update.install_command("uv-tool", "a" * 40)
    # Then the running server is restarted onto it, in its own mode …
    assert ("POST", "/api/server/restart", {"keep_mode": True}) in srv.calls
    assert not [c for c in srv.calls if c[1] == "/api/update/start"]
    # … and the version it now reports is printed.
    assert "MindFlock v9.9.9 (aaaaaaa) is running" in capsys.readouterr().out
    # install.json carries what is installed now.
    data = json.loads((inst / "install.json").read_text(encoding="utf-8"))
    assert data["method"] == "uv-tool" and data["ref"] == "v9.9.9"
    assert data["sha"] == "a" * 40 and data["python"] == "3.12" and data["at"]


def test_pipx_installs_the_tag_with_pipx(srv, inst, monkeypatch, capsys):
    from backend.web.core import self_update

    _install_json(inst, method="pipx")
    run = Installer(monkeypatch)
    srv.gets["/api/remote/hello"] = {"version": "9.9.9", "commit": ""}
    assert cli.main(["update", "--yes"]) == 0
    assert run.argv == [
        [
            "pipx",
            "install",
            "--force",
            "mindflock[web] @ git+%s@v9.9.9" % self_update.INSTALL_REPO,
        ]
    ]
    out = capsys.readouterr().out
    assert "installed with pipx (install.json)" in out
    assert ("POST", "/api/server/restart", {"keep_mode": True}) in srv.calls
    assert "MindFlock v9.9.9 is running" in out


def _checkout(monkeypatch, **over):
    """A development checkout whose status (dev_update.status) is ``over``."""
    from backend.web.core import dev_update
    from backend.web.core import self_update

    st = {
        "checkout": "/home/me/MindFlock",
        "branch": "main",
        "default_branch": "main",
        "dirty": False,
        "behind": 3,
        "ahead": 0,
        "can_update": True,
        "why_not": "",
        "remote": "origin",
        "upstream": "origin/main",
        "head": "b" * 40,
        "target": "a" * 40,
        "target_version": "9.9.9",
    }
    st.update(over)
    monkeypatch.setattr(self_update, "_detected_kind", lambda: "editable")
    monkeypatch.setattr(dev_update, "status", lambda *a, **k: dict(st))
    return st


def test_a_checkout_moves_forward_and_restarts_its_own_server(
    srv, inst, monkeypatch, capsys
):
    from backend.web.core import dev_update

    _checkout(monkeypatch)
    applied = []
    monkeypatch.setattr(
        dev_update, "apply_here", lambda st: applied.append(st["target"]) or (True, "")
    )
    run = Installer(monkeypatch)
    srv.gets["/api/remote/hello"] = {"version": "9.9.9", "commit": "a" * 40}
    assert cli.main(["update", "-y"]) == 0
    out = capsys.readouterr().out
    assert applied == ["a" * 40] and run.argv == []  # git, never the installer
    assert ("POST", "/api/server/restart", {"keep_mode": True}) in srv.calls
    assert "MindFlock v9.9.9" in out
    assert "git checkout" not in out and "uv sync" not in out


def test_a_checkout_that_cannot_update_says_why_and_changes_nothing(
    srv, inst, monkeypatch, capsys
):
    from backend.web.core import dev_update

    why = (
        "This is a development checkout on branch foo. Updates apply when it "
        "is on main with nothing uncommitted."
    )
    _checkout(monkeypatch, branch="foo", can_update=False, why_not=why)
    monkeypatch.setattr(dev_update, "apply_here", lambda st: pytest.fail("moved"))
    assert cli.main(["update", "-y"]) == 1
    assert why in capsys.readouterr().out
    assert not [c for c in srv.calls if c[0] == "POST"]
    # --check: what would happen, nothing changed, nothing to do = 0.
    assert cli.main(["update", "--check"]) == 0


def test_a_checkout_check_says_what_would_happen(srv, inst, monkeypatch, capsys):
    from backend.web.core import dev_update

    _checkout(monkeypatch)
    monkeypatch.setattr(dev_update, "apply_here", lambda st: pytest.fail("moved"))
    assert cli.main(["update", "--check"]) == 1
    out = capsys.readouterr().out
    assert "3 new commits on origin/main (v9.9.9)" in out
    assert "Updates: ask" in out
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_a_checkout_that_does_not_come_back_is_moved_back(
    srv, inst, monkeypatch, capsys
):
    from backend.web.core import dev_update

    st = _checkout(monkeypatch)
    monkeypatch.setattr(dev_update, "apply_here", lambda st: (True, ""))
    moved = []
    monkeypatch.setattr(
        dev_update, "move_back", lambda checkout, prev, **k: moved.append(prev) or True
    )
    monkeypatch.setattr(cli_update, "wait_for_build", lambda *a, **k: None)
    assert cli.main(["update", "-y"]) == 1
    assert moved == [st["head"]]
    assert "moved the checkout back to bbbbbbb" in capsys.readouterr().err


def test_a_dev_checkout_wins_over_a_stale_install_json(srv, inst, monkeypatch, capsys):
    # install.json says uv-tool (an earlier release install), but the package
    # running right now is a checkout: that is what must not be clobbered.
    from backend.web.core import dev_update

    _install_json(inst, method="uv-tool")
    _checkout(monkeypatch)
    monkeypatch.setattr(dev_update, "apply_here", lambda st: (True, ""))
    run = Installer(monkeypatch)
    srv.gets["/api/remote/hello"] = {"version": "9.9.9", "commit": "a" * 40}
    assert cli.main(["update", "-y"]) == 0
    assert run.argv == []


def test_auto_sets_the_mode_through_the_server_of_this_install(
    srv, inst, monkeypatch, capsys
):
    assert cli.main(["update", "--auto", "on"]) == 0
    assert ("POST", "/api/update/mode", {"mode": "auto"}) in srv.calls
    assert "automatic" in capsys.readouterr().out


def test_auto_with_no_server_writes_the_setting(inst, monkeypatch, capsys):
    from backend.web.core import self_update

    def _none(host=None, port=None):
        raise client.ServerNotFound()

    monkeypatch.setattr(client, "discover", _none)
    assert cli.main(["update", "--auto", "off"]) == 0
    assert self_update.update_mode() == ("off", True)


def test_the_own_server_is_found_by_install_id_not_the_default_port(monkeypatch):
    from backend.web.core import self_update

    # Undo the autouse stand-in: this is the real finder.
    monkeypatch.setattr(cli_update, "find_own_server", _REAL_FIND)
    monkeypatch.setattr(self_update, "install_id", lambda: "mine")
    monkeypatch.setattr(cli_update, "_local_servers", lambda: [(9000, False)])
    hellos = {
        "http://127.0.0.1:8765": {"app": "mindflock", "install_id": "other"},
        "http://127.0.0.1:9000": {"app": "mindflock", "install_id": "mine"},
    }
    monkeypatch.setattr(cli_update, "_hello", lambda base: hellos.get(base))
    own, others = cli_update.find_own_server()
    assert own == "http://127.0.0.1:9000"
    assert others == ["http://127.0.0.1:8765"]
    # A server too old to say: it counts when it runs this very interpreter.
    hellos["http://127.0.0.1:9000"] = {"app": "mindflock"}
    monkeypatch.setattr(cli_update, "_local_servers", lambda: [(9000, True)])
    assert cli_update.find_own_server()[0] == "http://127.0.0.1:9000"
    # Nothing of ours: nothing to restart.
    monkeypatch.setattr(cli_update, "_local_servers", lambda: [(9000, False)])
    assert cli_update.find_own_server()[0] == ""


def test_an_install_leaves_another_installs_server_alone(inst, monkeypatch, capsys):
    monkeypatch.setattr(
        cli_update, "find_own_server", lambda: ("", ["http://127.0.0.1:9000"])
    )
    monkeypatch.setattr(cli_update.time, "sleep", lambda s: None)
    posts = []
    monkeypatch.setattr(client, "post", lambda *a, **k: posts.append(a))
    _install_json(inst)
    Installer(monkeypatch)
    assert cli.main(["update", "--ref", "v9.9.9", "-y"]) == 0
    out = capsys.readouterr().out
    assert "start it with: mindflock serve" in out
    assert "127.0.0.1:9000 runs from another install" in out
    assert posts == []


def test_without_install_json_the_method_is_detected(srv, inst, monkeypatch, capsys):
    from backend.web.core import self_update

    assert not (inst / "install.json").exists()
    monkeypatch.setattr(self_update, "_detected_kind", lambda: "pipx")
    run = Installer(monkeypatch)
    srv.gets["/api/remote/hello"] = {"version": "9.9.9", "commit": ""}
    assert cli.main(["update", "-y"]) == 0
    assert "installed with pipx (detected)" in capsys.readouterr().out
    assert run.argv[0][:3] == ["pipx", "install", "--force"]


def test_with_no_server_it_just_installs(inst, monkeypatch, capsys):
    def _none(host=None, port=None):
        raise client.ServerNotFound()

    monkeypatch.setattr(client, "discover", _none)
    monkeypatch.setattr(cli_update.time, "sleep", lambda s: None)
    _install_json(inst)
    run = Installer(monkeypatch)
    assert cli.main(["update", "--ref", "v9.9.9", "-y"]) == 0
    assert len(run.argv) == 1
    assert "start it with: mindflock serve" in capsys.readouterr().out


def test_a_failed_install_exits_1_and_restarts_nothing(srv, inst, monkeypatch, capsys):
    _install_json(inst)
    Installer(monkeypatch, rc=7)
    assert cli.main(["update", "-y"]) == 1
    assert "exit 7" in capsys.readouterr().err
    assert not [c for c in srv.calls if c[0] == "POST"]


def test_an_unknown_install_is_refused_before_anything_runs(
    srv, inst, monkeypatch, capsys
):
    from backend.web.core import self_update

    monkeypatch.setattr(self_update, "_detected_kind", lambda: "other")
    run = Installer(monkeypatch)
    assert cli.main(["update", "-y"]) == 1
    assert "install.sh" in capsys.readouterr().err
    assert run.argv == []


def test_already_current_does_nothing(srv, inst, monkeypatch, capsys):
    from backend.web.core import self_update

    _install_json(inst)
    monkeypatch.setattr(self_update, "installed_version", lambda: "9.9.9")
    run = Installer(monkeypatch)
    assert cli.main(["update", "-y"]) == 0
    assert "Already on the newest release" in capsys.readouterr().out
    assert run.argv == []


def test_a_commit_ref_that_ls_remote_cannot_name_is_installed_as_itself(
    srv, inst, monkeypatch
):
    from backend.web.core import self_update

    _install_json(inst)
    monkeypatch.setattr(self_update, "_resolve_commit", lambda ref: "")
    run = Installer(monkeypatch)
    srv.gets["/api/remote/hello"] = {"version": "0.7.4", "commit": "c" * 40}
    assert cli.main(["update", "--ref", "c" * 40, "-y"]) == 0
    assert run.argv[0][-1].endswith("@" + "c" * 40)
    # A tag that resolves to nothing is an error, not an install of garbage.
    assert cli.main(["update", "--ref", "v0.0.0-nope", "-y"]) == 1
    assert len(run.argv) == 1


def test_the_question_names_the_method_and_no_means_no(srv, inst, monkeypatch, capsys):
    _install_json(inst, method="pipx")
    asked = []

    def _ask(prompt=""):
        asked.append(prompt)
        return "n"

    monkeypatch.setattr(builtins, "input", _ask)
    run = Installer(monkeypatch)
    assert cli.main(["update"]) == 1
    assert asked and "pipx" in asked[0] and "v9.9.9" in asked[0]
    assert run.argv == [] and "not updated" in capsys.readouterr().out


def test_restart_waits_for_the_server_to_come_back(srv, capsys):
    srv.gets["/api/remote/hello"] = [None, {"version": "9.9.9", "commit": ""}]
    assert cli.main(["restart"]) == 0
    # keep_mode: a server started in tailscale mode stays in it.
    assert ("POST", "/api/server/restart", {"keep_mode": True}) in srv.calls
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


def test_devices_update_gives_up_on_a_server_that_never_comes_back(
    srv, monkeypatch, capsys
):
    _yes(monkeypatch)
    monkeypatch.setattr(cli_update, "RESTART_WAIT_S", 0.05)
    srv.gets["/api/fleet"] = {"in_fleet": True, "members": []}
    srv.posts["/api/fleet/update"] = {"state": "running", "tag": "v9.9.9"}
    srv.gets["/api/fleet/update"] = client.ClientError("connection refused")
    assert cli.main(["devices", "update", "--yes"]) == 1
    assert "stopped answering" in capsys.readouterr().err


def test_update_all_devices_is_the_fleet_rollout(srv, monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli_update, "cmd_devices_update", lambda args: seen.append(args.tag) or 0
    )
    assert cli.main(["update", "--all-devices", "--ref", "v9.9.9"]) == 0
    assert seen == ["v9.9.9"]


def test_on_windows_the_install_goes_through_the_engine_swap(
    srv, inst, monkeypatch, capsys
):
    # `mindflock update` runs from the very venv it replaces; on Windows uv's
    # in-place rebuild of it leaves half an engine (2026-11-13). The swap
    # builds the new one beside it and puts the old one back on failure.
    from backend import compat
    from backend.web.core import engine_swap, self_update

    _install_json(inst)
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setattr(engine_swap, "find_exe", lambda env=None: r"C:\b\mindflock.exe")
    seen = {}

    def fake_swap(argv, **kw):
        seen.update(kw, argv=argv)
        return 2

    monkeypatch.setattr(engine_swap, "swap_install", fake_swap)
    assert cli.main(["update", "-y"]) == 1
    assert seen["argv"] == self_update.install_command("uv-tool", "a" * 40)
    assert seen["method"] == "uv-tool" and seen["exe"] == r"C:\b\mindflock.exe"
    assert seen["venv"] == __import__("sys").prefix
    assert not [c for c in srv.calls if c[0] == "POST"]  # nothing restarted


def test_off_windows_the_install_runs_directly(inst, monkeypatch):
    from backend import compat

    monkeypatch.setattr(compat, "IS_WINDOWS", False)
    run = Installer(monkeypatch)
    assert cli_update._install(["uv", "tool", "install"], "uv-tool") == 0
    assert run.argv == [["uv", "tool", "install"]]
