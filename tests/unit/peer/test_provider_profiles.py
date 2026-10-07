"""Peer shared sessions run whatever agent CLI declares a sandbox profile.

What a CLI needs inside the sandbox — its binary, API hosts, config dir, login
files, how the peer MCP attaches — is the provider's to declare
(``BaseProvider.sandbox_profile`` / ``peer_mcp_args`` / ``peer_mcp_env``, or a
TOML ``[peer]`` section). These pin the two halves of that contract: the
sandbox REFUSES a profile it would not honour as written, and a declared one
is honoured with no per-CLI code in the sandbox.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex

import pytest

from backend import providers
from backend.peer import launch, paths, sandbox
from backend.peer.sandbox import SandboxError
from backend.providers import mcp_attach
from backend.providers.base import BaseProvider, SandboxProfile
from backend.providers.config import ProviderConfig, _config_from_toml
from backend.providers.generic import GenericProvider

SHARE = "ab" * 16


class _Fake(BaseProvider):
    name = "fakecli"
    program_aliases = ("fakecli",)

    def __init__(self, profile):
        self._profile = profile

    def sandbox_profile(self):
        return self._profile


@pytest.fixture()
def use(monkeypatch):
    """Make ``providers.get(name)`` answer with the given provider."""

    def _use(provider):
        real = providers.get
        monkeypatch.setattr(
            providers,
            "get",
            lambda name: provider if name == provider.name else real(name),
        )
        return provider

    return _use


@pytest.mark.parametrize(
    "change,why",
    [
        ({"bin": "/usr/bin/evil"}, "bad sandbox binary"),
        ({"bin": "../x"}, "bad sandbox binary"),
        ({"passthrough_env": ("LD_PRELOAD",)}, "may not enter"),
        ({"passthrough_env": ("AWS_SECRET_ACCESS_KEY",)}, "may not enter"),
        ({"env": (("PATH", "/tmp"),)}, "may not enter"),
        ({"env": (("HTTPS_PROXY", "http://evil"),)}, "may not enter"),
        ({"config_env": "HOME", "config_dir": ".x"}, "may not enter"),
        ({"config_dir": "../.ssh"}, "bad config dir"),
        ({"config_dir": "/etc"}, "bad config dir"),
        ({"seed_files": ("../../.ssh/id_rsa",)}, "bad seed file"),
        ({"seed_files": ("/etc/shadow",)}, "bad seed file"),
    ],
)
def test_a_profile_the_sandbox_would_not_honour_is_refused(use, change, why):
    base = SandboxProfile(bin="fakecli", egress=("api.example.com",))
    use(_Fake(dataclasses.replace(base, **change)))
    with pytest.raises(SandboxError, match=why):
        sandbox.profile_for("fakecli")


def test_no_profile_means_no_shared_session(use):
    use(_Fake(None))
    with pytest.raises(SandboxError, match="no sandbox profile"):
        sandbox.profile_for("fakecli")
    assert "fakecli" not in sandbox.sandboxable()


def test_claude_and_codex_declare_theirs():
    assert {"claude", "codex"} <= set(sandbox.sandboxable())
    assert sandbox.profile_for("codex").config_env == "CODEX_HOME"
    assert "api.anthropic.com" in sandbox.egress_allow("claude")


def test_egress_drops_what_a_profile_cannot_widen_to(use):
    use(
        _Fake(
            SandboxProfile(
                bin="fakecli",
                egress=("api.example.com", "*", ".com", "10.0.0.1", "ok.example.org"),
            )
        )
    )
    assert sandbox.egress_allow("fakecli") == ["api.example.com", "ok.example.org"]


# --------------------------------------------------------------------------
# A CLI declared entirely in TOML


_TOML = {
    "provider": {"name": "tomlcli", "program": "tomlcli"},
    "connect": {"auth_env": ["TOMLCLI_API_KEY"]},
    "peer": {
        "egress": ["api.tomlcli.dev"],
        "config_env": "TOMLCLI_HOME",
        "config_dir": ".tomlcli",
        "seed_files": ["auth.json"],
        "env": {"TOMLCLI_NO_UPDATE": "1"},
        "mcp_args": ["--mcp-config", "{config_file}"],
        "mcp_env": {"TOMLCLI_MCP": "{env_json}"},
        "mcp_file": {
            "name": "tomlcli-mcp.json",
            "content": '{"servers":{"{server}":{"cmd":{argv_json},"env":{env_json}}}}',
        },
    },
}


@pytest.fixture()
def tomlcli(use, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    return use(GenericProvider(_config_from_toml(_TOML)))


def test_toml_peer_section_becomes_a_profile(tomlcli):
    prof = sandbox.profile_for("tomlcli")
    assert prof == SandboxProfile(
        bin="tomlcli",
        egress=("api.tomlcli.dev",),
        passthrough_env=("TOMLCLI_API_KEY",),
        config_env="TOMLCLI_HOME",
        config_dir=".tomlcli",
        seed_files=("auth.json",),
        env=(("TOMLCLI_NO_UPDATE", "1"),),
    )


def test_toml_without_an_mcp_attach_is_not_sandboxable(use):
    raw = json.loads(json.dumps(_TOML))
    for key in ("mcp_args", "mcp_env", "mcp_file"):
        raw["peer"].pop(key)
    raw["provider"]["name"] = "noattach"
    use(GenericProvider(_config_from_toml(raw)))
    with pytest.raises(SandboxError, match="no sandbox profile"):
        sandbox.profile_for("noattach")


def test_toml_mcp_templates_render_the_peer_spec(tomlcli):
    args = mcp_attach.peer_attach_args(tomlcli, share_id=SHARE, token="tok123")
    env = mcp_attach.peer_attach_env(tomlcli, share_id=SHARE, token="tok123")
    assert args[0] == "--mcp-config"
    path = args[1]
    assert path == os.path.join(paths.share_paths(SHARE)["run"], "tomlcli-mcp.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    body = json.loads(open(path).read())
    server = body["servers"]["mindflock"]
    spec = mcp_attach.PeerMcpSpec(SHARE, "tok123")
    assert server["cmd"] == [spec.command, *spec.args]
    assert server["env"]["MINDFLOCK_MCP_MODE"] == "peer"
    assert server["env"]["MINDFLOCK_PEER_TOKEN"] == "tok123"
    assert json.loads(env["TOMLCLI_MCP"]) == server["env"]


def test_a_template_value_cannot_inject_placeholders(tomlcli):
    # Substitution is single-pass: a value that happens to contain a
    # placeholder is never expanded again.
    spec = mcp_attach.PeerMcpSpec(SHARE, "{config_file}")
    assert tomlcli._peer_render("{env_json}", spec, "/x") == json.dumps(
        spec.env, sort_keys=True
    )


def test_generic_login_seeding_follows_the_config_env(tomlcli, monkeypatch, tmp_path):
    host_dir = tmp_path / "elsewhere"
    host_dir.mkdir()
    (host_dir / "auth.json").write_text('{"t": "FAKE"}')
    (host_dir / "history.jsonl").write_text("CANARY")
    monkeypatch.setenv("TOMLCLI_HOME", str(host_dir))
    share = paths.share_paths(SHARE)
    for key in ("root", "work", "gitdir", "home", "run"):
        paths.ensure_dir(share[key])
    with open(os.path.join(share["work"], ".git"), "w") as fh:
        fh.write("gitdir: %s\n" % share["gitdir"])
    written = sandbox.prepare_home(share, "tomlcli")
    assert written == [os.path.join(share["home"], ".tomlcli", "auth.json")]
    assert not os.path.exists(os.path.join(share["home"], ".tomlcli", "history.jsonl"))


def test_launch_exports_the_mcp_env_inside_the_sandbox(tomlcli, monkeypatch):
    monkeypatch.setattr(launch, "allowed_providers", lambda: ["tomlcli"])
    monkeypatch.setattr(launch, "check_sandbox", lambda: None)
    monkeypatch.setattr(providers, "resolve", lambda program: tomlcli)
    work = paths.ensure_dir(paths.share_paths(SHARE)["work"])
    assert os.path.isdir(work)
    launch.register_token(SHARE, "tok123")
    try:
        cmd = launch.build_command(
            program="tomlcli", share_id=SHARE, session_name="mf-peer"
        )
    finally:
        launch.forget_token(SHARE)
    inner = shlex.split(cmd)[-1]
    # `export`, so every command of a compound launch line sees it.
    assert inner.startswith("export TOMLCLI_MCP=")
    assert "--mcp-config" in inner


def test_launch_refuses_an_unsupported_cli_by_name(monkeypatch):
    monkeypatch.setattr(launch, "allowed_providers", lambda: ["claude"])
    with pytest.raises(launch.PeerLaunchError, match="aider can't run"):
        launch.provider_name("aider")


def test_provider_config_defaults_declare_nothing():
    cfg = ProviderConfig(name="x", program_aliases=("x",))
    assert GenericProvider(cfg).sandbox_profile() is None


def test_built_in_profiles_cover_the_cli_that_can_attach():
    # Each verified live inside the real sandbox (the peer MCP server spawned):
    # claude, codex, opencode, cline, goose, antigravity. aider has no MCP
    # client at all, so it can't be given the peer tools.
    have = set(sandbox.sandboxable())
    assert {"claude", "codex", "opencode", "cline", "goose", "antigravity"} <= have
    assert "aider" not in have


def test_opencode_attach_is_strict():
    p = providers.get("opencode")
    env = dict(p.cfg.sandbox_env)
    # A planted opencode.json / .opencode/ plugin in the shared folder must not load.
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    spec = mcp_attach.PeerMcpSpec(SHARE, "tok")
    conf = json.loads(p.peer_mcp_env(spec)["OPENCODE_CONFIG_CONTENT"])
    server = conf["mcp"]["mindflock"]
    assert server["type"] == "local" and server["environment"] == spec.env
    assert list(conf["mcp"]) == ["mindflock"]


def test_goose_drops_its_profile_and_passes_env_through_env1():
    p = providers.get("goose")
    spec = mcp_attach.PeerMcpSpec(SHARE, "tok")
    args = p.peer_mcp_args(spec)
    assert args[:3] == ("--no-profile", "--with-builtin", "developer")
    assert args[3] == "--with-extension"
    # goose filters PYTHONPATH out of an extension's env, so it rides env(1).
    ext = shlex.split(args[4])
    assert ext[0] == "env"
    assert "PYTHONPATH=" + spec.env["PYTHONPATH"] in ext
    assert ext[-3:] == list(spec.args)


def test_agy_config_is_written_into_the_sandbox_home(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    p = providers.get("antigravity")
    spec = mcp_attach.PeerMcpSpec(SHARE, "tok")
    assert p.peer_mcp_args(spec) == ()
    path = os.path.join(
        paths.share_paths(SHARE)["home"], ".gemini", "config", "mcp_config.json"
    )
    server = json.loads(open(path).read())["mcpServers"]["mindflock"]
    assert [server["command"], *server["args"]] == [spec.command, *spec.args]
    assert server["env"] == spec.env
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_home_file_write_does_not_follow_a_planted_symlink(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    home = paths.ensure_dir(paths.share_paths(SHARE)["home"])
    outside = tmp_path / "outside"
    outside.mkdir()
    os.makedirs(os.path.join(home, ".gemini"))
    os.symlink(outside, os.path.join(home, ".gemini", "config"))
    with pytest.raises(SandboxError):
        mcp_attach.PeerMcpSpec(SHARE, "tok").write_home_file(
            ".gemini/config/mcp_config.json", "{}"
        )
    assert list(outside.iterdir()) == []
