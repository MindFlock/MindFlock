"""Database explorer drivers survive engine updates.

The explorer installs a driver into the tool venv; every ``uv tool install
--force`` / ``pipx install --force`` rebuilt that venv from ``mindflock[web]``
alone and dropped it. The installer records what it installed
(``~/.mindflock/dbclient/drivers.json``); these tests pin the readers of that
record: the shared install line (``--with`` per driver), the pipx inject that
follows a pipx install (server script and ``mindflock update``), and the doctor
row. Nothing here runs a real installer.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from backend import cli_update as cu
from backend import doctor
from backend.web.core import self_update


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A temp $MINDFLOCK_HOME, so the record is never the real one."""
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path))
    return tmp_path


def _record(home, drivers):
    path = home / "dbclient" / "drivers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "drivers": drivers}), encoding="utf-8")
    return path


PG = {"package": "psycopg[binary]", "installed_at": "x", "python": "3.12"}
MY = {"package": "pymysql", "installed_at": "x", "python": "3.12"}


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #
def test_the_record_lives_under_mindflock_home(home):
    assert self_update.driver_record_path() == home / "dbclient" / "drivers.json"


def test_record_driver_writes_the_documented_shape(home):
    assert self_update.record_driver("postgres", "psycopg[binary]")
    data = json.loads((home / "dbclient" / "drivers.json").read_text("utf-8"))
    assert data["version"] == 1
    entry = data["drivers"]["postgres"]
    assert entry["package"] == "psycopg[binary]"
    assert entry["installed_at"].endswith("Z")
    assert entry["python"].count(".") == 1
    assert self_update.forget_driver("postgres")
    assert self_update.read_driver_record() == {}


def test_no_record_and_a_corrupt_record_both_read_as_none(home):
    assert self_update.read_driver_record() == {}
    (home / "dbclient").mkdir()
    (home / "dbclient" / "drivers.json").write_text("{nope", encoding="utf-8")
    assert self_update.recorded_driver_packages() == []


def test_a_record_entry_that_is_not_a_plain_package_never_reaches_an_argv(home):
    """The record is a file on disk; an install argv must not take a flag, a
    URL or a path from it."""
    _record(
        home,
        {
            "postgres": PG,
            "a": {"package": "--index-url=http://attacker.invalid"},
            "b": {"package": "git+https://example.invalid/x"},
            "c": {"package": "../../evil"},
            "d": {"package": "pymysql; os_name=='posix'"},
        },
    )
    assert self_update.recorded_driver_packages() == ["psycopg[binary]"]
    assert not self_update.record_driver("x", "-e /tmp/x")


# --------------------------------------------------------------------------- #
# Carried through the update
# --------------------------------------------------------------------------- #
def test_uv_install_line_carries_one_with_per_recorded_driver(home):
    _record(home, {"postgres": PG, "mysql": MY})
    argv = self_update.install_command("uv-tool", "a" * 40)
    spec = self_update.install_spec("a" * 40)
    assert argv == [
        "uv",
        "tool",
        "install",
        "--force",
        "--python",
        self_update.INSTALL_PYTHON,
        "--with",
        "pymysql",
        "--with",
        "psycopg[binary]",
        spec,
    ]
    # pipx has no --with: its line is unchanged and an inject follows.
    assert self_update.install_command("pipx", "v9.9.9") == [
        "pipx",
        "install",
        "--force",
        self_update.install_spec("v9.9.9"),
    ]
    assert self_update.driver_carry_commands("pipx") == [
        ["pipx", "inject", "mindflock", "pymysql", "psycopg[binary]"]
    ]
    assert self_update.driver_carry_commands("uv-tool") == []


def test_no_record_means_the_plain_install_line(home):
    assert "--with" not in self_update.install_command("uv-tool", "a" * 40)
    assert self_update.driver_carry_commands("pipx") == []


def test_the_server_script_carries_the_drivers(home, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "_state_dir", lambda: tmp_path)
    _record(home, {"postgres": PG})
    uv = self_update._script("v9.9.9", "a" * 40, method="uv-tool", prev_commit="b" * 40)
    # Both the update and its rollback keep the driver.
    assert uv.count("--with 'psycopg[binary]'") == 2
    assert "pipx inject" not in uv
    px = self_update._script("v9.9.9", "a" * 40, method="pipx", prev_commit="b" * 40)
    assert px.count("pipx inject mindflock 'psycopg[binary]'") == 2
    assert subprocess.run(["/bin/sh", "-n", "-c", px]).returncode == 0


def test_the_pipx_script_injects_after_a_good_install(home, tmp_path, monkeypatch):
    """Run the real generated script with a STUB pipx and a throwaway HOME."""
    monkeypatch.setattr(self_update, "_state_dir", lambda: tmp_path)
    _record(home, {"postgres": PG})
    fake_home = tmp_path / "fakehome"
    (fake_home / ".local" / "bin").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    pipx = bindir / "pipx"
    pipx.write_text(
        '#!/bin/sh\necho "$*" >> %s\n[ "$1" = inject ] && exit 1\nexit 0\n' % calls,
        encoding="utf-8",
    )
    pipx.chmod(0o755)
    script = tmp_path / "gen.sh"
    script.write_text(
        self_update._script("v9.9.9", "a" * 40, method="pipx"), encoding="utf-8"
    )
    env = dict(os.environ, HOME=str(fake_home), PATH="%s:/usr/bin:/bin" % bindir)
    cp = subprocess.run(
        ["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    # A failed inject never fails the update — the explorer heals on start.
    assert cp.returncode == 0, cp.stderr
    lines = calls.read_text("utf-8").splitlines()
    assert lines[0].startswith("install --force")
    assert lines[1] == "inject mindflock psycopg[binary]"
    assert "could not carry the database drivers" in self_update.log_path().read_text(
        "utf-8"
    )
    assert self_update.read_state()["state"] == "done"


def test_mindflock_update_injects_after_a_pipx_install(home, monkeypatch, capsys):
    _record(home, {"mysql": MY})
    ran = []
    monkeypatch.setattr(cu, "_run_installer", lambda argv: ran.append(argv) or 1)
    cu._carry_drivers("pipx")
    assert ran == [["pipx", "inject", "mindflock", "pymysql"]]
    assert "reinstalls them when the server starts" in capsys.readouterr().err
    ran.clear()
    cu._carry_drivers("uv-tool")
    assert ran == []


def test_mindflock_update_rollback_keeps_the_drivers(home, monkeypatch):
    _record(home, {"mysql": MY})
    ran = []
    monkeypatch.setattr(cu, "_run_installer", lambda argv: ran.append(argv) or 0)
    out = cu._rollback({"method": "pipx", "ref": "v0.7.5"})
    assert "rolled the engine back to v0.7.5" in out
    assert ran == [
        ["pipx", "install", "--force", self_update.install_spec("v0.7.5")],
        ["pipx", "inject", "mindflock", "pymysql"],
    ]


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #
def test_doctor_has_no_driver_row_until_one_is_recorded(home):
    assert doctor.check_db_drivers() is None
    assert doctor.CHECKS_BY_ID["db-drivers"] is doctor.check_db_drivers


def test_doctor_lists_the_recorded_drivers_as_optional_info(home):
    _record(home, {"postgres": PG, "mysql": MY})
    row = doctor.check_db_drivers()
    assert isinstance(row, doctor.Check)
    assert (row.id, row.label, row.status, row.group) == (
        "db-drivers",
        "Database drivers",
        "info",
        "optional",
    )
    assert row.detail.startswith("pymysql (mysql), psycopg (postgres)")
    assert not row.install and not row.cmd
