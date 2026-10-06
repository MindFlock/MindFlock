"""MindFlock MCP auto-attach (:mod:`backend.providers.mcp_attach`).

Pins the launch-side contract: what the per-session spec carries (and never
carries — a token, ``PORT``), the Claude run file and its two single-token
flags, the Codex ``-c`` inline table (a TOML round-trip with hostile strings,
and the shell quoting of every launch path that carries it), the off switches,
the three launch sites + the Assistant, the ticket pipeline's env, the
``/api/config`` capability and uninstall.

The CLI-side facts these encode (``--opt=value`` not swallowing the prompt,
no approval dialog for ``--mcp-config`` servers, additive ``--allowedTools``,
the per-server ``timeout`` key, Codex parsing the override) were verified
against the installed claude 2.1.289 / codex 0.146.0; see the module docs.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import textwrap
import tomllib
from types import SimpleNamespace

import pytest

from backend import providers
from backend.config import settings as S
from backend.providers import mcp_attach
from backend.providers.base import BaseProvider, LaunchContext
from backend.providers.claude import ClaudeProvider, claude_launch_command
from backend.providers.codex import CodexProvider
from backend.providers.config import BUILTIN_CONFIGS
from backend.providers.generic import GenericProvider

_TOOLS = (
    "whoami",
    "list_sessions",
    "get_session",
    "read_output",
    "get_diff",
    "check_inbox",
    "wait_for_message",
    "wait_for_session",
    "report_result",
    "list_tickets",
    "get_run",
    "list_runs",
    "wait_for_run",
    # A split lead's two reports (review finding 34).
    "propose_run_plan",
    "report_integrated",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every env input the resolver reads starts unset — a developer's shell
    inside a MindFlock session exports UVICORN_PORT and PORT, which would make
    the port assertions machine-dependent."""
    for var in (
        "MINDFLOCK_AGENT_MCP",
        "MINDFLOCK_SERVER_PORT",
        "UVICORN_PORT",
        "PORT",
        "MINDFLOCK_MCP_PYTHON",
        "MINDFLOCK_MCP_PYTHONPATH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "argv", ["pytest"])
    S.invalidate()
    yield
    S.invalidate()


def _codex() -> CodexProvider:
    return CodexProvider([c for c in BUILTIN_CONFIGS if c.name == "codex"][0])


def _aider() -> GenericProvider:
    return GenericProvider([c for c in BUILTIN_CONFIGS if c.name == "aider"][0])


def _spec(**kw) -> mcp_attach.McpSpec:
    base = dict(
        title="feat-x",
        tmux_name="mindflock_feat-x",
        workdir="/wt",
        python="/opt/py/bin/python3",
        pythonpath="/opt/app",
        port=8765,
        scope="children",
    )
    base.update(kw)
    return mcp_attach.McpSpec(**base)


def _write_settings(general: dict) -> None:
    path = S.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"general": general}), encoding="utf-8")


# --------------------------------------------------------------------------- #
# McpSpec
# --------------------------------------------------------------------------- #
def test_spec_env_for_a_managed_session():
    env = _spec(settings_file="/s/settings.json").env()
    assert env == {
        "MINDFLOCK_HOST": "127.0.0.1",
        "MINDFLOCK_PORT": "8765",
        "MINDFLOCK_MCP_SCOPE": "children",
        "MINDFLOCK_MCP_MANAGED": "1",
        "MINDFLOCK_SESSION_TITLE": "feat-x",
        "PYTHONPATH": "/opt/app",
        "MINDFLOCK_SETTINGS_FILE": "/s/settings.json",
    }
    assert all(isinstance(v, str) for v in env.values())


def test_spec_env_unmanaged_has_no_identity():
    # The Assistant: an external client — no title, no managed marker (which
    # would make the MCP fail closed to read-only).
    env = _spec(title="", managed=False).env()
    assert "MINDFLOCK_SESSION_TITLE" not in env
    assert "MINDFLOCK_MCP_MANAGED" not in env


def test_spec_env_never_carries_a_token(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "sekrit")
    spec = mcp_attach.build_spec("t", "mindflock_t")
    blob = json.dumps(mcp_attach.claude_config(spec)) + mcp_attach.codex_server_table(
        spec
    )
    assert "sekrit" not in blob
    assert "MINDFLOCK_AUTH_TOKEN" not in spec.env()


def test_spec_command_is_python_dash_P_dash_m():
    spec = _spec()
    assert spec.command() == "/opt/py/bin/python3"
    assert spec.args() == ("-P", "-m", "backend.mcp")
    assert mcp_attach.McpSpec(title="", tmux_name="x").command() == sys.executable


# --------------------------------------------------------------------------- #
# Toggle + scope
# --------------------------------------------------------------------------- #
def test_enabled_by_default():
    assert mcp_attach.enabled() is True


def test_settings_false_turns_it_off():
    S.update_settings(general={"agent_mcp": False})
    assert mcp_attach.enabled() is False
    S.update_settings(general={"agent_mcp": True})
    assert mcp_attach.enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "OFF", " no "])
def test_env_kill_switch_wins_over_settings(monkeypatch, val):
    S.update_settings(general={"agent_mcp": True})
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", val)
    assert mcp_attach.enabled() is False


def test_env_truthy_does_not_override_a_settings_off(monkeypatch):
    # The env var is a kill switch, not a force-on.
    S.update_settings(general={"agent_mcp": False})
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "1")
    assert mcp_attach.enabled() is False


def test_corrupt_settings_reads_as_on():
    path = S.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert mcp_attach.enabled() is True
    assert mcp_attach.configured_scope() == "children"


def test_toggle_is_read_fresh_not_from_the_process_cache():
    """The ticket pipeline child keeps its own settings cache; a save from the
    server must still take effect for its next launch."""
    assert S.load_settings().general.agent_mcp is None  # cache warmed: unset
    _write_settings({"agent_mcp": False})  # written behind the cache's back
    assert S.load_settings().general.agent_mcp is None  # cache is stale...
    assert mcp_attach.enabled() is False  # ...the attach read is not


@pytest.mark.parametrize(
    "stored, expected",
    [
        (None, "children"),
        ("readonly", "readonly"),
        ("ALL", "all"),
        ("children", "children"),
        ("everything", "children"),
    ],
)
def test_configured_scope(stored, expected):
    if stored is not None:
        _write_settings({"agent_mcp_scope": stored})
    assert mcp_attach.configured_scope() == expected


# --------------------------------------------------------------------------- #
# Settings round trip (the resume_on_usage_reset recipe)
# --------------------------------------------------------------------------- #
def test_general_settings_round_trip():
    g = S.GeneralSettings()
    assert g.agent_mcp is None and g.agent_mcp_scope == ""
    assert "agent_mcp" not in g.to_dict() and "agent_mcp_scope" not in g.to_dict()
    for val in (True, False):
        g = S.GeneralSettings.from_dict({"agent_mcp": val, "agent_mcp_scope": "all"})
        assert g.to_dict()["agent_mcp"] is val
        assert S.GeneralSettings.from_dict(g.to_dict()) == g
    assert (
        S.GeneralSettings.from_dict({"agent_mcp_scope": " ReadOnly "}).agent_mcp_scope
        == "readonly"
    )
    assert (
        S.GeneralSettings.from_dict({"agent_mcp_scope": "root"}).agent_mcp_scope == ""
    )


def test_settings_store_round_trip():
    S.update_settings(general={"agent_mcp": False, "agent_mcp_scope": "readonly"})
    S.invalidate()
    general = S.load_settings().general
    assert general.agent_mcp is False
    assert general.agent_mcp_scope == "readonly"
    # Other general keys are untouched by the new ones.
    S.update_settings(general={"onboarded": True})
    assert S.load_settings().general.agent_mcp is False


# --------------------------------------------------------------------------- #
# Port / interpreter resolution
# --------------------------------------------------------------------------- #
def test_port_defaults_to_8765():
    assert mcp_attach.server_port() == 8765


def test_port_never_comes_from_PORT(monkeypatch):
    # PORT is the session's dev-port block inside an agent shell.
    monkeypatch.setenv("PORT", "4100")
    assert mcp_attach.server_port() == 8765


def test_port_precedence(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mindflock", "serve", "--port", "9100"])
    assert mcp_attach.server_port() == 9100
    monkeypatch.setattr(sys, "argv", ["mindflock", "serve", "--port=9200"])
    assert mcp_attach.server_port() == 9200
    monkeypatch.setenv("UVICORN_PORT", "9300")
    assert mcp_attach.server_port() == 9300
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "9400")
    assert mcp_attach.server_port() == 9400


@pytest.mark.parametrize("bad", ["", "abc", "0", "70000", "-1", "80.5"])
def test_invalid_port_values_fall_through(monkeypatch, bad):
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", bad)
    monkeypatch.setenv("UVICORN_PORT", "9300")
    assert mcp_attach.server_port() == 9300
    monkeypatch.setattr(sys, "argv", ["x", "--port", bad])
    monkeypatch.delenv("UVICORN_PORT")
    assert mcp_attach.server_port() == 8765


def test_python_and_pythonpath(monkeypatch):
    assert mcp_attach.mcp_python() == sys.executable
    root = mcp_attach.mcp_pythonpath()
    # The directory that holds THIS process's backend package.
    assert os.path.isfile(os.path.join(root, "backend", "__init__.py"))
    import backend

    assert os.path.dirname(os.path.abspath(backend.__file__)) == os.path.join(
        root, "backend"
    )
    monkeypatch.setenv("MINDFLOCK_MCP_PYTHON", "/srv/venv/bin/python")
    monkeypatch.setenv("MINDFLOCK_MCP_PYTHONPATH", "/srv/app")
    assert mcp_attach.mcp_python() == "/srv/venv/bin/python"
    assert mcp_attach.mcp_pythonpath() == "/srv/app"


def test_build_spec_reads_env_and_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("UVICORN_PORT", "9555")
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "s.json"))
    S.update_settings(general={"agent_mcp_scope": "all"})
    spec = mcp_attach.build_spec("feat", "mindflock_feat", "/wt")
    assert (spec.title, spec.tmux_name, spec.workdir) == (
        "feat",
        "mindflock_feat",
        "/wt",
    )
    assert spec.port == 9555 and spec.scope == "all" and spec.managed is True
    assert spec.settings_file == str(tmp_path / "s.json")
    assert spec.python == sys.executable


def test_python_dash_P_ignores_a_backend_package_in_the_cwd(tmp_path):
    """The reason for ``-P``: an agent working on MindFlock itself runs the MCP
    with a ``backend/`` in its cwd that must NOT shadow the server's."""
    decoy = tmp_path / "agent_cwd"
    (decoy / "backend" / "mcp").mkdir(parents=True)
    (decoy / "backend" / "__init__.py").write_text("raise SystemExit('DECOY')\n")
    real = tmp_path / "server_pkg"
    (real / "backend" / "mcp").mkdir(parents=True)
    (real / "backend" / "__init__.py").write_text("")
    (real / "backend" / "mcp" / "__init__.py").write_text("")
    (real / "backend" / "mcp" / "__main__.py").write_text("print('REAL MCP')\n")
    spec = _spec(python=sys.executable, pythonpath=str(real))
    env = {"PATH": os.environ.get("PATH", ""), **spec.env()}
    cp = subprocess.run(
        [spec.command(), *spec.args()],
        cwd=decoy,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "REAL MCP"


# --------------------------------------------------------------------------- #
# Run files (Claude)
# --------------------------------------------------------------------------- #
def test_run_dir_override_and_default(monkeypatch, tmp_path):
    assert mcp_attach.run_dir() == os.environ["MINDFLOCK_RUN_DIR"]
    monkeypatch.delenv("MINDFLOCK_RUN_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mcp_attach.run_dir() == str(tmp_path / ".mindflock" / "run")


@pytest.mark.parametrize(
    "name, base",
    [
        ("mindflock_feat-x", "mcp-mindflock_feat-x.json"),
        ("../../etc/passwd", "mcp-_.._etc_passwd-"),
        ("a/b c", "mcp-a_b_c-"),
        ("", "mcp-session.json"),
        ("...", "mcp-session-"),
    ],
)
def test_run_file_path_stays_in_the_run_dir(name, base):
    path = mcp_attach.run_file_path(name)
    assert os.path.dirname(path) == mcp_attach.run_dir()
    assert os.path.basename(path).startswith(base)
    assert path.endswith(".json")


@pytest.mark.parametrize(
    "a, b",
    [
        ("mindflock_修复登录", "mindflock_添加测试"),
        ("mindflock_fixlogin!", "mindflock_fixlogin?"),
        ("mindflock_über", "mindflock_öber"),
        ("mindflock_a+b", "mindflock_a_b"),
    ],
)
def test_distinct_tmux_names_never_share_a_run_file(a, b):
    """Lossy sanitizing used to map these pairs onto ONE file: the last launch
    owned both identities, and one removal deleted the other's config."""
    assert mcp_attach.run_file_path(a) != mcp_attach.run_file_path(b)
    # Stable per name (a relaunch finds the same file).
    assert mcp_attach.run_file_path(a) == mcp_attach.run_file_path(a)


def test_write_claude_config_shape_and_perms():
    spec = _spec()
    path = mcp_attach.write_claude_config(spec)
    assert path == mcp_attach.run_file_path("mindflock_feat-x")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc == {
        "mcpServers": {
            "mindflock": {
                "type": "stdio",
                "command": "/opt/py/bin/python3",
                "args": ["-P", "-m", "backend.mcp"],
                "env": spec.env(),
                "timeout": 1620000,
            }
        }
    }
    # No temp file left behind by the atomic write.
    assert os.listdir(os.path.dirname(path)) == [os.path.basename(path)]


def test_write_claude_config_rewrites_every_launch():
    mcp_attach.write_claude_config(_spec(port=8765))
    path = mcp_attach.write_claude_config(_spec(port=9001))
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["mcpServers"]["mindflock"]["env"]["MINDFLOCK_PORT"] == "9001"


def test_forget_removes_the_run_file():
    path = mcp_attach.write_claude_config(_spec())
    assert mcp_attach.forget("mindflock_feat-x") is True
    assert not os.path.exists(path)
    assert mcp_attach.forget("mindflock_feat-x") is False  # already gone
    assert mcp_attach.forget("") is False


def test_run_files_lists_only_ours(tmp_path):
    a = mcp_attach.write_claude_config(_spec(tmux_name="mindflock_a"))
    b = mcp_attach.write_claude_config(_spec(tmux_name="mindflock_b"))
    with open(os.path.join(mcp_attach.run_dir(), "other.json"), "w") as fh:
        fh.write("{}")
    assert mcp_attach.run_files() == sorted([a, b])


def test_run_files_without_a_run_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "nope"))
    assert mcp_attach.run_files() == []


# --------------------------------------------------------------------------- #
# Claude provider flags
# --------------------------------------------------------------------------- #
def test_claude_mcp_launch_args():
    args = ClaudeProvider().mcp_launch_args(_spec())
    path = mcp_attach.run_file_path("mindflock_feat-x")
    assert args == (
        "--mcp-config=" + path,
        "--allowedTools=" + ",".join("mcp__mindflock__" + t for t in _TOOLS),
    )
    assert os.path.isfile(path)
    # Additive: the user's own servers keep loading.
    assert not any("strict-mcp-config" in a for a in args)


@pytest.mark.parametrize("skip", [False, True])
def test_claude_args_never_sit_spaced_before_the_seed(skip):
    """Both options are variadic; the seed follows the launch args directly.
    The single-token ``--opt=value`` form is what keeps the prompt a
    positional (the spaced form made claude 2.1.289 read the prompt as a
    second config path)."""
    args = ClaudeProvider().mcp_launch_args(_spec())
    cmd = claude_launch_command(
        "claude",
        resume=False,
        skip_permissions=skip,
        seed=" 'do the thing'",
        launch_args=args + ("--model", "opus"),
    )
    argv = shlex.split(cmd)
    assert argv[0] == "claude"
    assert argv[1] == args[0] and argv[2] == args[1]
    assert "--mcp-config" not in argv and "--allowedTools" not in argv
    assert argv[-1] == "do the thing"


def test_minimal_and_oneshot_launches_are_unchanged():
    prov = ClaudeProvider()
    assert prov.minimal_launch_command() == (
        "claude --strict-mcp-config --mcp-config '{\"mcpServers\":{}}' "
        "--dangerously-skip-permissions"
    )
    argv = prov.oneshot_argv("q")
    assert argv[1:] == [
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "-p",
        "q",
    ]


def test_base_and_generic_providers_have_no_attach():
    assert BaseProvider().mcp_launch_args(_spec()) == ()
    assert _aider().mcp_launch_args(_spec()) == ()


# --------------------------------------------------------------------------- #
# Codex inline table
# --------------------------------------------------------------------------- #
_HOSTILE = [
    "plain",
    'quote " in it',
    "back\\slash",
    "new\nline\tand\rcr",
    "del\x7fchar",
    "emoji 😀 and ünïcode",
    "sh $(rm -rf ~) `x` 'single' ;|&",
    "bell\x07 nul-ish\x01 esc\x1b",
]


def _parse(value: str) -> dict:
    return tomllib.loads("x = " + value)["x"]


@pytest.mark.parametrize("title", _HOSTILE)
def test_codex_table_round_trips_through_tomllib(title):
    spec = _spec(title=title, python="/py dir/bin/python", pythonpath='/a"b')
    got = _parse(mcp_attach.codex_server_table(spec))
    assert got["command"] == "/py dir/bin/python"
    assert got["args"] == ["-P", "-m", "backend.mcp"]
    assert got["env"] == spec.env()
    assert got["env"]["MINDFLOCK_SESSION_TITLE"] == title
    assert got["env_vars"] == [
        "TMUX",
        "TMUX_PANE",
        "TMUX_TMPDIR",
        "MINDFLOCK_AUTH_TOKEN",
    ]
    assert got["startup_timeout_sec"] == 30
    assert got["tool_timeout_sec"] == 1620
    assert got["tools"] == {t: {"approval_mode": "approve"} for t in _TOOLS}


def test_codex_table_replaces_lone_surrogates():
    # Not a Unicode scalar: TOML (and Codex) would reject the whole value.
    got = _parse(mcp_attach.codex_server_table(_spec(title="bad \ud800 sur")))
    assert got["env"]["MINDFLOCK_SESSION_TITLE"] == "bad � sur"


def test_codex_env_values_are_all_strings():
    got = _parse(mcp_attach.codex_server_table(_spec(port=9001)))
    assert got["env"]["MINDFLOCK_PORT"] == "9001"  # a bare int fails Codex's map


def test_codex_table_is_one_line():
    assert "\n" not in mcp_attach.codex_server_table(_spec(title="a\nb"))


def test_codex_mcp_launch_args():
    args = _codex().mcp_launch_args(_spec())
    assert len(args) == 2 and args[0] == "-c"
    key, _, value = args[1].partition("=")
    assert key == "mcp_servers.mindflock"
    assert _parse(value)["command"] == "/opt/py/bin/python3"


def test_codex_override_survives_the_plain_launch_shell_quoting():
    """The generic provider shell-quotes launch args; `sh -c` (what tmux runs)
    must hand codex the override as ONE argv element, byte-identical."""
    args = _codex().mcp_launch_args(_spec(title=_HOSTILE[6] + _HOSTILE[1]))
    for resume in (False, True):
        cmd = _codex().build_launch_command(
            LaunchContext(program="codex", resume=resume, launch_args=args)
        )
        first = cmd.split(" || ")[0]
        argv = shlex.split(first)
        assert argv[:3] == ["codex", "-c", args[1]]
        if resume:
            # -c is global, so it may precede the resume subcommand.
            assert argv[3:5] == ["resume", "--last"]


def _fake_cli(tmp_path, name="codex"):
    """An executable that records its argv as JSON, then exits 0."""
    out = tmp_path / ("%s-argv.json" % name)
    exe = tmp_path / ("fake-%s" % name)
    exe.write_text(
        "#!%s\nimport json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n"
        % (sys.executable, str(out))
    )
    exe.chmod(0o755)
    return exe, out


def test_codex_override_survives_the_provisioned_launcher(tmp_path, monkeypatch):
    """The provisioned launcher nests the args inside `bash -ilc '<inner>'` —
    run the generated script for real and check codex's argv."""
    from backend.session import provisioned

    exe, out = _fake_cli(tmp_path)
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CODEX", str(exe))
    home = tmp_path / "home"
    home.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    args = _codex().mcp_launch_args(_spec(title=_HOSTILE[6] + _HOSTILE[1]))
    script = provisioned.write_launcher(
        str(wt), "", program="codex", skip_permissions=False, launch_args=args
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb"}
    subprocess.run(
        ["bash", script],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    argv = json.loads(out.read_text())
    assert argv[:2] == list(args)


def test_claude_args_survive_the_provisioned_launcher(tmp_path, monkeypatch):
    from backend.session import provisioned

    exe, out = _fake_cli(tmp_path, "claude")
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CLAUDE", str(exe))
    home = tmp_path / "home"
    home.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    args = ClaudeProvider().mcp_launch_args(_spec())
    script = provisioned.write_launcher(
        str(wt), "seed me", program="claude", skip_permissions=True, launch_args=args
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "dumb"}
    subprocess.run(
        ["bash", script],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    argv = json.loads(out.read_text())
    assert argv[:2] == list(args)
    assert argv[-1] == "seed me"


# --------------------------------------------------------------------------- #
# attach_args / refresh_launcher_config / supported_providers
# --------------------------------------------------------------------------- #
def test_attach_args_for_claude_and_codex():
    claude = mcp_attach.attach_args(
        ClaudeProvider(), title="t", tmux_name="mindflock_t"
    )
    assert claude[0] == "--mcp-config=" + mcp_attach.run_file_path("mindflock_t")
    codex = mcp_attach.attach_args(_codex(), title="t", tmux_name="mindflock_t")
    assert codex[0] == "-c"
    env = _parse(codex[1].partition("=")[2])["env"]
    assert env["MINDFLOCK_SESSION_TITLE"] == "t"


def test_attach_args_off_unsupported_or_broken_is_empty(monkeypatch):
    assert mcp_attach.attach_args(_aider(), title="t", tmux_name="mindflock_t") == ()
    assert mcp_attach.attach_args(object(), title="t", tmux_name="mindflock_t") == ()
    assert mcp_attach.attach_args(ClaudeProvider(), title="t", tmux_name="") == ()

    class Boom(BaseProvider):
        def mcp_launch_args(self, spec):
            raise OSError("disk full")

    assert mcp_attach.attach_args(Boom(), title="t", tmux_name="mindflock_t") == ()
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    assert (
        mcp_attach.attach_args(ClaudeProvider(), title="t", tmux_name="mindflock_t")
        == ()
    )
    assert not os.path.exists(mcp_attach.run_file_path("mindflock_t"))


def test_attach_args_unmanaged_for_the_assistant():
    mcp_attach.attach_args(
        ClaudeProvider(), title="", tmux_name="mindflock_asst", managed=False
    )
    doc = json.loads(open(mcp_attach.run_file_path("mindflock_asst")).read())
    env = doc["mcpServers"]["mindflock"]["env"]
    assert "MINDFLOCK_MCP_MANAGED" not in env and "MINDFLOCK_SESSION_TITLE" not in env


def _launcher(tmp_path, body: str) -> str:
    p = tmp_path / ".mindflock_launch.sh"
    p.write_text(body)
    return str(p)


def test_refresh_launcher_rewrites_the_run_file(tmp_path, monkeypatch):
    monkeypatch.setenv("UVICORN_PORT", "9100")
    path = mcp_attach.run_file_path("mindflock_t")
    launcher = _launcher(tmp_path, "claude --mcp-config=%s\n" % path)
    mcp_attach.refresh_launcher_config(
        ClaudeProvider(),
        title="t",
        tmux_name="mindflock_t",
        workdir="/wt",
        launcher=launcher,
    )
    doc = json.loads(open(path).read())
    assert doc["mcpServers"]["mindflock"]["env"]["MINDFLOCK_PORT"] == "9100"


def test_refresh_launcher_when_off_keeps_a_referenced_file_present(
    tmp_path, monkeypatch
):
    """Claude refuses to start on a missing --mcp-config file; a launcher that
    baked the path in must still start with attach turned off."""
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    path = mcp_attach.run_file_path("mindflock_t")
    launcher = _launcher(tmp_path, "claude --mcp-config=%s\n" % path)
    mcp_attach.refresh_launcher_config(
        ClaudeProvider(),
        title="t",
        tmux_name="mindflock_t",
        workdir="/wt",
        launcher=launcher,
    )
    assert json.loads(open(path).read()) == {"mcpServers": {}}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_refresh_launcher_when_off_leaves_unrelated_launchers_alone(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    launcher = _launcher(tmp_path, "claude --continue\n")
    mcp_attach.refresh_launcher_config(
        ClaudeProvider(),
        title="t",
        tmux_name="mindflock_t",
        workdir="/wt",
        launcher=launcher,
    )
    assert not os.path.exists(mcp_attach.run_file_path("mindflock_t"))
    # A missing launcher is not an error either.
    mcp_attach.refresh_launcher_config(
        ClaudeProvider(),
        title="t",
        tmux_name="mindflock_t",
        workdir="/wt",
        launcher=str(tmp_path / "gone.sh"),
    )


def test_refresh_launcher_when_off_empties_an_existing_file(tmp_path, monkeypatch):
    """The kill switch must hold on a provisioned relaunch: the enabled-era
    run file (full server, old port) used to survive and re-attach the MCP."""
    path = mcp_attach.write_claude_config(_spec(tmux_name="mindflock_t", port=9300))
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    launcher = _launcher(tmp_path, "claude --mcp-config=%s\n" % path)
    mcp_attach.refresh_launcher_config(
        ClaudeProvider(),
        title="t",
        tmux_name="mindflock_t",
        workdir="/wt",
        launcher=launcher,
    )
    assert json.loads(open(path).read()) == {"mcpServers": {}}


def test_refresh_launcher_never_writes_outside_the_run_dir(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.json"
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    launcher = _launcher(tmp_path, "claude --mcp-config=%s\n" % elsewhere)
    mcp_attach.refresh_launcher_config(
        ClaudeProvider(),
        title="t",
        tmux_name="mindflock_t",
        workdir="/wt",
        launcher=launcher,
    )
    assert not elsewhere.exists()


def test_launcher_attach_stale(tmp_path, monkeypatch):
    import shlex

    prov = ClaudeProvider()
    args = mcp_attach.attach_args(prov, title="foo", tmux_name="mindflock_foo")
    script = "claude " + " ".join(shlex.quote(a) for a in args) + "\n"
    kw = dict(workdir="", script=script)
    assert not mcp_attach.launcher_attach_stale(
        prov, title="foo", tmux_name="mindflock_foo", **kw
    )
    # Reopened as foo-2: the baked run file names the OLD session.
    assert mcp_attach.launcher_attach_stale(
        prov, title="foo-2", tmux_name="mindflock_foo-2", **kw
    )
    # Toggled off: any baked attach is stale; none is fine.
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    assert mcp_attach.launcher_attach_stale(
        prov, title="foo", tmux_name="mindflock_foo", **kw
    )
    assert not mcp_attach.launcher_attach_stale(
        prov, title="foo", tmux_name="mindflock_foo", workdir="", script="claude\n"
    )
    codex_script = "codex -c 'mcp_servers.mindflock={x=1}'\n"
    assert mcp_attach.launcher_attach_stale(
        _codex(),
        title="foo",
        tmux_name="mindflock_foo",
        workdir="",
        script=codex_script,
    )


def test_a_split_leads_reports_never_park_on_a_permission_dialog():
    """A lead in a permission-gated session would sit in planning on a
    dialog: proposing is report-only (the user approves the plan)."""
    for tool in ("propose_run_plan", "report_integrated"):
        assert tool in mcp_attach.AUTO_APPROVED_TOOLS
        assert ("mcp__mindflock__" + tool) in mcp_attach.claude_tool_names()


def test_send_message_is_not_auto_approved():
    """Pre-approved send_message let an approval-gated session type into a
    skip-permissions session with no human in the loop."""
    assert "send_message" not in mcp_attach.AUTO_APPROVED_TOOLS
    assert "report_result" in mcp_attach.AUTO_APPROVED_TOOLS


@pytest.mark.parametrize(
    "tool",
    [
        "ship_session",
        "set_autopilot",
        "spawn_ticket_session",
        "start_team_run",
        "control_run",
        "fence_session",
        "set_order",
    ],
)
def test_ship_and_ticket_writes_are_never_auto_approved(tool):
    """They push code, open and merge PRs, provision sessions and move
    tickets: a gated session must ask, as for spawn/kill."""
    assert tool not in mcp_attach.AUTO_APPROVED_TOOLS
    assert ("mcp__mindflock__" + tool) not in mcp_attach.claude_tool_names()
    table = _parse(mcp_attach.codex_server_table(_spec(title="t")))
    assert tool not in table["tools"]
    assert "list_tickets" in table["tools"]


def test_every_auto_approved_tool_is_a_read_or_the_report():
    """Both lists come from AUTO_APPROVED_TOOLS; each entry must be a real
    tool, and every one but the reports (report_result, and a split lead's
    propose_run_plan / report_integrated — the user approves the plan, the
    server re-verifies the merge) must be read-only."""
    from backend.mcp.tools import build_tools
    from tests.unit._mcp_fakes import make_box

    box, _, _ = make_box([])
    by = {t.name: t for t in build_tools(box)}
    for name in mcp_attach.AUTO_APPROVED_TOOLS:
        assert name in by, name
        if name not in ("report_result", "propose_run_plan", "report_integrated"):
            assert by[name].annotations["readOnlyHint"] is True, name
    assert "list_tickets" in mcp_attach.AUTO_APPROVED_TOOLS


def test_supported_providers_are_claude_and_codex():
    assert mcp_attach.supported_providers() == ["claude", "codex"]


# --------------------------------------------------------------------------- #
# Launch site 1: Instance._configure_launch_command (engine)
# --------------------------------------------------------------------------- #
class _RecordingProvider(BaseProvider):
    name = "rec"

    def __init__(self):
        self.ctx = None
        self.hooks = []

    def mcp_launch_args(self, spec):
        self.spec = spec
        return ("--mcp-flag=" + spec.title,)

    def build_launch_command(self, ctx):
        self.ctx = ctx
        return "rec-cmd"

    def install_activity_hooks(self, workdir, session_name):
        self.hooks.append((workdir, session_name))


def _engine_instance(tmp_path, *, provisioned=False, launch_args=("--user",)):
    from backend.session.instance import Instance

    inst = Instance()
    inst.Title = "feat-x"
    inst.Program = "rec"
    inst.Prompt = ""
    inst.LaunchArgs = tuple(launch_args)
    inst.Provisioned = provisioned
    inst._tmux_session = SimpleNamespace(
        sanitized_name="mindflock_feat-x", launch_command=None
    )
    inst._git_worktree = SimpleNamespace(GetWorktreePath=lambda: str(tmp_path))
    return inst


def test_engine_plain_launch_prepends_attach_args(tmp_path, monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    inst = _engine_instance(tmp_path)
    inst._configure_launch_command()
    assert prov.ctx.launch_args == ("--mcp-flag=feat-x", "--user")
    assert prov.spec.tmux_name == "mindflock_feat-x"
    assert prov.spec.workdir == str(tmp_path) and prov.spec.managed is True
    assert inst._tmux_session.launch_command == "rec-cmd"
    # Never persisted: the UI keeps showing only the user's args.
    assert inst.LaunchArgs == ("--user",)


def test_engine_plain_launch_without_attach_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    prov = _RecordingProvider()
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    inst = _engine_instance(tmp_path)
    inst._configure_launch_command()
    assert prov.ctx.launch_args == ("--user",)


def test_engine_provisioned_launcher_gets_attach_args(tmp_path, monkeypatch):
    from backend.session import provisioned

    prov = _RecordingProvider()
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    seen = {}

    def fake_write_launcher(wt, prompt, **kw):
        seen.update(kw, wt=wt)
        return "/wt/.mindflock_launch.sh"

    monkeypatch.setattr(provisioned, "write_launcher", fake_write_launcher)
    inst = _engine_instance(tmp_path, provisioned=True)
    inst._configure_launch_command()
    assert seen["launch_args"] == ("--mcp-flag=feat-x", "--user")
    assert inst._tmux_session.launch_command == "/wt/.mindflock_launch.sh"
    assert inst.LaunchArgs == ("--user",)
    assert prov.ctx is None  # the launcher, not build_launch_command


def test_engine_real_claude_plain_launch(tmp_path, monkeypatch):
    """The real Claude provider end to end through the engine: the run file is
    written and the flags lead the command, ahead of the user's args."""
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path / "seeds"))
    wt = tmp_path / "wt"
    wt.mkdir()
    inst = _engine_instance(wt, launch_args=("--model", "opus"))
    inst.Program = "claude"
    inst.Prompt = "go"
    inst._configure_launch_command()
    argv = shlex.split(inst._tmux_session.launch_command)
    run_file = mcp_attach.run_file_path("mindflock_feat-x")
    assert argv[:4] == [
        "claude",
        "--mcp-config=" + run_file,
        "--allowedTools=" + ",".join("mcp__mindflock__" + t for t in _TOOLS),
        "--model",
    ]
    doc = json.loads(open(run_file).read())
    assert doc["mcpServers"]["mindflock"]["env"]["MINDFLOCK_SESSION_TITLE"] == "feat-x"


# --------------------------------------------------------------------------- #
# Launch site 2: agent_sessions._ensure_agent_session (web relaunch)
# --------------------------------------------------------------------------- #
class _RelaunchProvider(_RecordingProvider):
    def is_natural_exit(self, code):
        return False


def _wire_relaunch(monkeypatch, prov, *, launcher_exists: bool):
    from backend.web.core import agent_sessions

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        rc = 1 if "has-session" in argv else 0
        return subprocess.CompletedProcess(argv, rc, stdout=b"", stderr=b"")

    monkeypatch.setattr(agent_sessions.providers, "resolve", lambda prog: prov)
    monkeypatch.setattr(agent_sessions, "_read_exit_marker", lambda name: 137)
    monkeypatch.setattr(agent_sessions, "_clear_exit_marker", lambda name: None)
    monkeypatch.setattr(agent_sessions, "_wrap_launch_cmd", lambda cmd, name: cmd)
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: None)
    real_isfile = os.path.isfile
    monkeypatch.setattr(
        agent_sessions.os.path,
        "isfile",
        lambda p: (
            launcher_exists if p.endswith(".mindflock_launch.sh") else real_isfile(p)
        ),
    )
    from backend.web import server

    monkeypatch.setattr(server, "_run_capped", fake_run)
    return agent_sessions, calls


def test_relaunch_plain_prepends_attach_args(tmp_path, monkeypatch):
    prov = _RelaunchProvider()
    agent_sessions, calls = _wire_relaunch(monkeypatch, prov, launcher_exists=False)
    inst = SimpleNamespace(
        Program="rec",
        InPlace=False,
        LaunchArgs=("--user",),
        GetWorktreePath=lambda: str(tmp_path),
    )
    name, err = agent_sessions._ensure_agent_session(inst, "feat-x")
    assert (name, err) == ("mindflock_feat-x", None)
    assert prov.ctx.launch_args == ("--mcp-flag=feat-x", "--user")
    assert prov.spec.title == "feat-x" and prov.spec.tmux_name == "mindflock_feat-x"
    assert inst.LaunchArgs == ("--user",)


def test_relaunch_via_launcher_refreshes_the_run_file(tmp_path, monkeypatch):
    prov = ClaudeProvider()
    agent_sessions, calls = _wire_relaunch(monkeypatch, prov, launcher_exists=True)
    monkeypatch.setattr(prov, "install_activity_hooks", lambda *a: None)
    monkeypatch.setenv("UVICORN_PORT", "9222")
    inst = SimpleNamespace(
        Program="claude",
        InPlace=False,
        LaunchArgs=(),
        GetWorktreePath=lambda: str(tmp_path),
    )
    name, err = agent_sessions._ensure_agent_session(inst, "feat-x")
    assert err is None
    doc = json.loads(open(mcp_attach.run_file_path("mindflock_feat-x")).read())
    assert doc["mcpServers"]["mindflock"]["env"]["MINDFLOCK_PORT"] == "9222"
    new = [c for c in calls if "new-session" in c][0]
    assert new[-1].endswith(".mindflock_launch.sh")  # the launcher, re-run as is


def test_relaunch_rewrites_a_launcher_baked_for_another_title(tmp_path, monkeypatch):
    """Reopened as feat-x-2 (its title was taken): the launcher still named
    feat-x's run file / title — i.e. the LIVE namesake's identity."""
    from backend.web import server

    prov = ClaudeProvider()
    agent_sessions, calls = _wire_relaunch(monkeypatch, prov, launcher_exists=True)
    monkeypatch.setattr(prov, "install_activity_hooks", lambda *a: None)
    old_args = mcp_attach.attach_args(
        prov, title="feat-x", tmux_name="mindflock_feat-x"
    )
    (tmp_path / ".mindflock_launch.sh").write_text(
        "claude " + " ".join(shlex.quote(a) for a in old_args) + "\n"
    )
    rewrites = []
    monkeypatch.setattr(
        server,
        "_rewrite_provisioned_launcher",
        lambda inst, wt, title="": rewrites.append((wt, title)) or True,
    )
    inst = SimpleNamespace(
        Program="claude",
        InPlace=False,
        LaunchArgs=(),
        GetWorktreePath=lambda: str(tmp_path),
    )
    agent_sessions._ensure_agent_session(inst, "feat-x-2")
    assert rewrites == [(str(tmp_path), "feat-x-2")]
    # An up-to-date launcher is left alone.
    rewrites.clear()
    agent_sessions._ensure_agent_session(inst, "feat-x")
    assert rewrites == []


def test_profile_swap_rewrite_keeps_the_mcp_attach(tmp_path, monkeypatch):
    """The profile-swap rewrite used to write prof + user args only, so the
    session lost the MindFlock tools for the rest of its life."""
    from backend.session import provisioned
    from backend.web import server

    (tmp_path / provisioned.LAUNCHER_BASENAME).write_text("old\n")
    seen = {}
    monkeypatch.setattr(
        provisioned, "write_launcher", lambda wt, prompt, **kw: seen.update(kw)
    )
    inst = SimpleNamespace(
        Title="feat-x",
        Program="claude",
        Provisioned=True,
        ProfileId="",
        ProfileModel="",
        LaunchArgs=("--user",),
        _git_worktree=SimpleNamespace(
            _provision_settings=SimpleNamespace(skip_permissions=True, caches=[])
        ),
    )
    assert server._rewrite_launcher_for_profile(inst, str(tmp_path)) is True
    args = seen["launch_args"]
    assert args[0] == "--mcp-config=" + mcp_attach.run_file_path("mindflock_feat-x")
    assert args[1].startswith("--allowedTools=")
    assert args[-1] == "--user"


# --------------------------------------------------------------------------- #
# Launch site 3: the Assistant addon
# --------------------------------------------------------------------------- #
def test_assistant_attaches_unmanaged(monkeypatch, tmp_path):
    from backend.web.addons import assistant as A

    prov = _RelaunchProvider()

    def fake_run(argv, **kw):
        rc = 1 if "has-session" in argv else 0
        return subprocess.CompletedProcess(argv, rc, stderr=b"")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    monkeypatch.setattr(A.providers, "resolve", lambda _p: prov)
    monkeypatch.setattr(A, "_read_exit_marker", lambda _n: None)
    monkeypatch.setattr(A, "_clear_exit_marker", lambda _n: None)
    monkeypatch.setattr(A, "_wrap_launch_cmd", lambda cmd, _n: cmd)
    monkeypatch.setattr(A, "apply_scroll_speed", lambda: None)
    monkeypatch.setattr(A, "_seed_assistant_dir", lambda: None)
    name, err = A._ensure_assistant_session()
    assert err is None
    assert prov.ctx.launch_args[0] == "--mcp-flag="  # no title
    assert prov.spec.managed is False
    assert prov.spec.tmux_name == A.ASSIST_TMUX


# --------------------------------------------------------------------------- #
# The launch record behind the row's ``mcp_attached``
# --------------------------------------------------------------------------- #
def test_launch_record_is_unknown_until_a_launch():
    assert mcp_attach.launch_attached("mindflock_feat-x") is None
    mcp_attach.note_launch("mindflock_feat-x", True)
    assert mcp_attach.launch_attached("mindflock_feat-x") is True
    mcp_attach.note_launch("mindflock_feat-x", False)  # a later launch wins
    assert mcp_attach.launch_attached("mindflock_feat-x") is False
    mcp_attach.note_launch("", True)  # no name, nothing recorded
    assert mcp_attach.launch_attached("") is None


def test_forget_drops_the_launch_record():
    mcp_attach.note_launch("mindflock_feat-x", True)
    mcp_attach.forget("mindflock_feat-x")
    assert mcp_attach.launch_attached("mindflock_feat-x") is None


def test_script_attached():
    want = ("--mcp-config=/run/mcp-x.json", "--allowedTools=a b")
    script = "claude " + " ".join(shlex.quote(a) for a in want) + " --user\n"
    assert mcp_attach.script_attached(script, want) is True
    assert mcp_attach.script_attached(script, want + ("--more",)) is False
    assert mcp_attach.script_attached(script, ()) is False
    assert mcp_attach.script_attached("", want) is False


def test_refresh_launcher_reports_whether_the_relaunch_attaches(tmp_path, monkeypatch):
    prov = ClaudeProvider()
    want = mcp_attach.attach_args(prov, title="t", tmux_name="mindflock_t")
    baked = _launcher(tmp_path, "claude " + " ".join(shlex.quote(a) for a in want))
    kw = dict(title="t", tmux_name="mindflock_t", workdir="/wt", launcher=baked)
    assert mcp_attach.refresh_launcher_config(prov, **kw) is True
    stale = _launcher(tmp_path, "claude --model opus\n")
    assert mcp_attach.refresh_launcher_config(prov, **dict(kw, launcher=stale)) is False
    missing = str(tmp_path / "nope.sh")
    assert (
        mcp_attach.refresh_launcher_config(prov, **dict(kw, launcher=missing)) is False
    )
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    assert mcp_attach.refresh_launcher_config(prov, **dict(kw, launcher=baked)) is False


def test_engine_plain_launch_records_attached(tmp_path, monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    inst = _engine_instance(tmp_path)
    inst._configure_launch_command()
    assert mcp_attach.launch_attached("mindflock_feat-x") is True
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    inst._configure_launch_command()
    assert mcp_attach.launch_attached("mindflock_feat-x") is False


def test_engine_bare_program_launch_is_not_attached(tmp_path, monkeypatch):
    """A provider that builds no command runs the bare program — no launch
    args at all, so no tools, whatever attach_args said."""
    prov = _RecordingProvider()
    prov.build_launch_command = lambda ctx: None
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    _engine_instance(tmp_path)._configure_launch_command()
    assert mcp_attach.launch_attached("mindflock_feat-x") is False


def test_engine_unsupported_provider_is_not_attached(tmp_path, monkeypatch):
    prov = _RecordingProvider()
    prov.mcp_launch_args = lambda spec: ()
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    _engine_instance(tmp_path)._configure_launch_command()
    assert mcp_attach.launch_attached("mindflock_feat-x") is False


def test_engine_provisioned_launch_records_attached(tmp_path, monkeypatch):
    from backend.session import provisioned

    prov = _RecordingProvider()
    monkeypatch.setattr(providers, "resolve", lambda prog: prov)
    monkeypatch.setattr(provisioned, "write_launcher", lambda wt, p, **kw: "/wt/l.sh")
    _engine_instance(tmp_path, provisioned=True)._configure_launch_command()
    assert mcp_attach.launch_attached("mindflock_feat-x") is True

    def broken(wt, p, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(provisioned, "write_launcher", broken)
    _engine_instance(tmp_path, provisioned=True)._configure_launch_command()
    assert mcp_attach.launch_attached("mindflock_feat-x") is False


def test_engine_resume_records_the_configured_command(tmp_path, monkeypatch):
    """Resume reruns whatever command this Instance was configured with: one
    loaded from state.json was never configured (it relaunches the bare
    program), so its resumed agent has no tools."""
    from backend.session.instance import Instance

    started = []
    inst = Instance()
    inst.Title = "feat-x"
    inst._tmux_session = SimpleNamespace(
        sanitized_name="mindflock_feat-x",
        start=lambda wt: started.append(wt),
        extra_env={},
    )
    inst._git_worktree = SimpleNamespace(GetWorktreePath=lambda: str(tmp_path))
    inst._start_tmux_or_cleanup()
    assert started and mcp_attach.launch_attached("mindflock_feat-x") is False
    inst._mcp_launch_attached = True  # configured with the flags this process
    inst._start_tmux_or_cleanup()
    assert mcp_attach.launch_attached("mindflock_feat-x") is True


def test_relaunch_plain_records_attached(tmp_path, monkeypatch):
    prov = _RelaunchProvider()
    agent_sessions, _ = _wire_relaunch(monkeypatch, prov, launcher_exists=False)
    inst = SimpleNamespace(
        Program="rec",
        InPlace=False,
        LaunchArgs=(),
        GetWorktreePath=lambda: str(tmp_path),
    )
    agent_sessions._ensure_agent_session(inst, "feat-x")
    assert mcp_attach.launch_attached("mindflock_feat-x") is True
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    agent_sessions._ensure_agent_session(inst, "feat-x")
    assert mcp_attach.launch_attached("mindflock_feat-x") is False


def test_relaunch_via_launcher_records_what_the_script_carries(tmp_path, monkeypatch):
    from backend.web import server

    prov = ClaudeProvider()
    agent_sessions, _ = _wire_relaunch(monkeypatch, prov, launcher_exists=True)
    monkeypatch.setattr(prov, "install_activity_hooks", lambda *a: None)
    want = mcp_attach.attach_args(prov, title="feat-x", tmux_name="mindflock_feat-x")
    launcher = tmp_path / ".mindflock_launch.sh"
    launcher.write_text("claude " + " ".join(shlex.quote(a) for a in want) + "\n")
    inst = SimpleNamespace(
        Program="claude",
        InPlace=False,
        LaunchArgs=(),
        GetWorktreePath=lambda: str(tmp_path),
    )
    agent_sessions._ensure_agent_session(inst, "feat-x")
    assert mcp_attach.launch_attached("mindflock_feat-x") is True
    # A launcher that could not be rewritten to carry them: no tools.
    launcher.write_text("claude --model opus\n")
    monkeypatch.setattr(server, "_rewrite_provisioned_launcher", lambda *a, **k: False)
    agent_sessions._ensure_agent_session(inst, "feat-x")
    assert mcp_attach.launch_attached("mindflock_feat-x") is False


def test_relaunch_that_fails_records_nothing(tmp_path, monkeypatch):
    from backend.web import server

    prov = _RelaunchProvider()
    agent_sessions, _ = _wire_relaunch(monkeypatch, prov, launcher_exists=False)

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"nope")

    monkeypatch.setattr(server, "_run_capped", failing)
    inst = SimpleNamespace(
        Program="rec",
        InPlace=False,
        LaunchArgs=(),
        GetWorktreePath=lambda: str(tmp_path),
    )
    name, err = agent_sessions._ensure_agent_session(inst, "feat-x")
    assert err == "nope"
    assert mcp_attach.launch_attached("mindflock_feat-x") is None


def test_assistant_launch_records_attached(monkeypatch, tmp_path):
    from backend.web.addons import assistant as A

    prov = _RelaunchProvider()

    def fake_run(argv, **kw):
        rc = 1 if "has-session" in argv else 0
        return subprocess.CompletedProcess(argv, rc, stderr=b"")

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    monkeypatch.setattr(A.providers, "resolve", lambda _p: prov)
    monkeypatch.setattr(A, "_read_exit_marker", lambda _n: None)
    monkeypatch.setattr(A, "_clear_exit_marker", lambda _n: None)
    monkeypatch.setattr(A, "_wrap_launch_cmd", lambda cmd, _n: cmd)
    monkeypatch.setattr(A, "apply_scroll_speed", lambda: None)
    monkeypatch.setattr(A, "_seed_assistant_dir", lambda: None)
    A._ensure_assistant_session()
    assert mcp_attach.launch_attached(A.ASSIST_TMUX) is True


# --------------------------------------------------------------------------- #
# Ticket pipeline child env
# --------------------------------------------------------------------------- #
def test_pipeline_child_gets_the_servers_python_and_port(monkeypatch, tmp_path):
    from backend.web.addons.ticket_ingestion import TicketIngestionController

    monkeypatch.setenv("UVICORN_PORT", "9444")
    monkeypatch.setenv("PORT", "4100")  # a session dev port: must not leak in
    ti = TicketIngestionController.__new__(TicketIngestionController)
    ti._repo_root = tmp_path
    env = ti._env()
    assert env["MINDFLOCK_SERVER_PORT"] == "9444"
    assert env["MINDFLOCK_MCP_PYTHON"] == sys.executable
    assert env["MINDFLOCK_MCP_PYTHONPATH"] == mcp_attach.mcp_pythonpath()
    # ...and in the child (its own sys.executable / argv / PORT differ) those
    # are what the spec resolves to.
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(sys, "argv", ["-m", "backend.ticket_ingestion"])
    spec = mcp_attach.build_spec("t", "mindflock_t")
    assert spec.port == 9444 and spec.python == sys.executable


# --------------------------------------------------------------------------- #
# GET /api/config caps.agent_mcp + POST /api/settings
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.web import server

    return TestClient(server.app)


def test_config_caps_agent_mcp(client, monkeypatch):
    caps = client.get("/api/config").json()["caps"]["agent_mcp"]
    assert caps == {"enabled": True, "providers": ["claude", "codex"]}
    monkeypatch.setenv("MINDFLOCK_AGENT_MCP", "0")
    assert client.get("/api/config").json()["caps"]["agent_mcp"]["enabled"] is False


def test_settings_route_toggles_attach(client):
    r = client.post("/api/settings", json={"general": {"agent_mcp": False}})
    assert r.status_code == 200, r.text
    assert mcp_attach.enabled() is False
    assert client.get("/api/config").json()["caps"]["agent_mcp"]["enabled"] is False
    client.post(
        "/api/settings", json={"general": {"agent_mcp": True, "agent_mcp_scope": "all"}}
    )
    assert mcp_attach.enabled() is True
    assert mcp_attach.configured_scope() == "all"


def test_caps_never_500(client, monkeypatch):
    def boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(mcp_attach, "supported_providers", boom)
    caps = client.get("/api/config").json()["caps"]["agent_mcp"]
    assert caps == {"enabled": False, "providers": []}


# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
def test_uninstall_removes_run_files(monkeypatch, tmp_path):
    from backend import uninstall

    monkeypatch.setattr(uninstall, "_load_instances", lambda: ([], None))
    monkeypatch.setattr(uninstall, "_orphan_worktrees", lambda known: ([], None))
    a = mcp_attach.write_claude_config(_spec(tmux_name="mindflock_a"))
    keep = os.path.join(mcp_attach.run_dir(), "not-ours.json")
    with open(keep, "w") as fh:
        fh.write("{}")
    plan = uninstall.build_plan()
    assert plan.run_files == [a]

    dry = uninstall.execute(plan, dry_run=True)
    assert os.path.exists(a)
    assert "would delete MCP run file %s" % a in dry.actions

    report = uninstall.execute(plan)
    assert not os.path.exists(a) and os.path.exists(keep)
    assert "deleted MCP run file %s" % a in report.actions
    assert report.errors == []
    # A file that vanished between plan and execute is not an error.
    assert uninstall.execute(plan).errors == []


def test_uninstall_cli_reports_run_files(monkeypatch, capsys):
    from backend import cli
    from backend import uninstall

    monkeypatch.setattr(uninstall, "_load_instances", lambda: ([], None))
    monkeypatch.setattr(uninstall, "_orphan_worktrees", lambda known: ([], None))
    monkeypatch.setattr(uninstall, "server_is_running", lambda h=None, p=None: False)
    mcp_attach.write_claude_config(_spec(tmux_name="mindflock_a"))
    rc = cli.main(["uninstall", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MCP run files:        1" in out
    assert "would delete MCP run file" in out


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #
def test_module_has_no_import_cycle():
    code = textwrap.dedent("""
        import backend.providers.mcp_attach as m
        import backend.providers as p
        assert p.mcp_attach is m
        """)
    cp = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert cp.returncode == 0, cp.stderr
