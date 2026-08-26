"""Provider connection: install detection, login-evidence probing, and the
one-click login terminal (Settings → Providers).

Covers the seamless-setup surface added for multi-machine / multi-provider use:

* ``config.detect_auth`` — credential-file / env-var evidence, and that a miss
  is silent (never a false "logged out");
* each provider's ``login_command`` / ``install_hint`` / ``auth_evidence``;
* ``GET /api/providers/status`` — installed reflects PATH, the default is
  flagged, the catch-all ``generic`` is hidden, and login/install hints ride
  along;
* ``provider_login.ensure_login_session`` — unknown provider is an error, a
  known one spawns a tmux session, and an existing session is reused (tmux is
  faked; no real sessions);
* the ``/providers/{name}/login-close`` teardown endpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import backend.web.addons.settings as settings_addon
from backend import providers
from backend.config import settings as S
from backend.providers import claude_usage_api
from backend.providers.base import BaseProvider
from backend.providers.config import detect_auth
from backend.web.core import provider_login
from backend.web.core import terminal as web_terminal


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    # A dev machine may carry real API keys — clear the ones providers probe so
    # auth-evidence assertions are deterministic.
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    # A Mac running the suite has a real Claude login in its Keychain, which is
    # genuine evidence — pin that lookup off so the file/env assertions below
    # can't be answered by the developer's own login (the keychain tests
    # re-patch it with what they want it to find).
    monkeypatch.setattr(claude_usage_api, "_keychain_doc", lambda: None)
    S.invalidate()
    providers.rebuild_registry()
    yield
    S.invalidate()
    providers.rebuild_registry()


# --------------------------------------------------------------------------- #
# detect_auth
# --------------------------------------------------------------------------- #
def test_detect_auth_finds_credential_file(tmp_path):
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    got = detect_auth((str(cred),), ())
    assert str(cred) in got


def test_detect_auth_expands_home_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".creds").write_text("x")
    assert "creds" in detect_auth(("~/.creds",), ())


def test_detect_auth_env_var(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert detect_auth((), ("OPENAI_API_KEY",)) == "OPENAI_API_KEY is set"


def test_detect_auth_miss_is_empty_not_false():
    # A miss is "" (reported as "unknown"), never a definitive negative.
    assert detect_auth(("/no/such/file",), ("NOPE_NOT_SET_VAR",)) == ""


# --------------------------------------------------------------------------- #
# per-provider connection methods
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,login,has_install",
    [
        ("claude", "claude", True),
        ("codex", "codex login", True),
        ("opencode", "opencode auth login", True),
        ("goose", "goose configure", False),
        ("aider", "aider", True),
        ("antigravity", "agy", False),
    ],
)
def test_login_and_install_hints(name, login, has_install):
    p = providers.get(name)
    assert p is not None
    assert p.login_command() == login
    assert bool(p.install_hint()) is has_install


def test_claude_auth_evidence_reads_credentials(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text('{"oauthAccount": {"email": "e@x"}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert ".claude.json" in providers.get("claude").auth_evidence()


def test_codex_auth_evidence_from_env(monkeypatch, tmp_path):
    # Isolate HOME so a real ~/.codex/auth.json on the dev box doesn't win the
    # file check ahead of the env probe we're asserting here.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert providers.get("codex").auth_evidence() == "OPENAI_API_KEY is set"


def test_auth_evidence_unknown_when_nothing_found(monkeypatch, tmp_path):
    # opencode with no auth file and no key -> unknown (""), never asserts logged out.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert providers.get("opencode").auth_evidence() == ""


def test_user_toml_connect_table(tmp_path, monkeypatch):
    d = tmp_path / "providers"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mycli.toml").write_text(
        '[provider]\nname = "mycli"\nprogram = "mycli"\n'
        '[connect]\nlogin_command = "mycli auth"\n'
        'install_hint = "npm i -g mycli"\nauth_env = ["MYCLI_TOKEN"]\n'
    )
    providers.rebuild_registry()
    p = providers.get("mycli")
    assert p.login_command() == "mycli auth"
    assert p.install_hint() == "npm i -g mycli"
    monkeypatch.setenv("MYCLI_TOKEN", "t")
    assert "MYCLI_TOKEN" in p.auth_evidence()


# --------------------------------------------------------------------------- #
# GET /api/providers/status
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from backend.web.server import app

    with TestClient(app) as c:
        yield c


def test_status_endpoint_shape(client, monkeypatch):
    import backend.web.addons.settings as settings_addon

    # Pretend only claude + codex are installed on PATH.
    monkeypatch.setattr(
        settings_addon.shutil,
        "which",
        lambda b: "/usr/bin/" + b if b in ("claude", "codex") else None,
    )
    body = client.get("/api/providers/status").json()
    provs = {p["name"]: p for p in body["providers"]}
    assert "generic" not in provs  # catch-all fallback hidden
    assert body["default"] in provs
    assert provs["claude"]["installed"] is True
    assert provs["claude"]["path"] == "/usr/bin/claude"
    assert provs["aider"]["installed"] is False
    # Hints ride along so the UI can offer install + login in one place.
    assert provs["codex"]["login_command"] == "codex login"
    assert provs["aider"]["install_hint"]


def test_status_flags_the_default_provider(client, monkeypatch):
    S.save_settings(S.Settings.from_dict({"coding_cli": {"default_provider": "codex"}}))
    body = client.get("/api/providers/status").json()
    assert body["default"] == "codex"
    provs = {p["name"]: p for p in body["providers"]}
    assert provs["codex"]["is_default"] is True
    assert provs["claude"]["is_default"] is False


# --------------------------------------------------------------------------- #
# ensure_login_session (tmux faked)
# --------------------------------------------------------------------------- #
class _FakeRun:
    """Records tmux invocations; has-session miss then new-session success."""

    def __init__(self):
        self.calls = []
        self.existing = set()

    def __call__(self, argv, **kw):
        self.calls.append(argv)

        class _CP:
            returncode = 0
            stderr = b""

        cp = _CP()
        if len(argv) >= 2 and argv[1] == "has-session":
            target = argv[2].split("=", 1)[1]
            cp.returncode = 0 if target in self.existing else 1
        elif len(argv) >= 2 and argv[1] == "new-session":
            # -s <session> is the 5th token in our invocation
            self.existing.add(argv[argv.index("-s") + 1])
        return cp


def test_ensure_login_session_unknown_provider_errors():
    session, err = provider_login.ensure_login_session("definitely-not-real")
    assert err and "login flow" in err


def test_ensure_login_session_spawns_and_is_idempotent(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(provider_login.subprocess, "run", fake)
    session, err = provider_login.ensure_login_session("codex")
    assert err is None
    assert session == "mindflock_login_codex"
    assert any(a[1] == "new-session" for a in fake.calls)
    # Second call reuses the existing session — no second new-session.
    fake.calls.clear()
    session2, err2 = provider_login.ensure_login_session("codex")
    assert err2 is None and session2 == session
    assert not any(a[1] == "new-session" for a in fake.calls)


def test_ensure_login_session_runs_the_login_command(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(provider_login.subprocess, "run", fake)
    provider_login.ensure_login_session("codex")
    new_session = next(a for a in fake.calls if a[1] == "new-session")
    # The wrapped shell command must invoke the provider's login command.
    assert any("codex login" in tok for tok in new_session)


def test_login_close_endpoint_kills_session(client, monkeypatch):
    killed = {}
    monkeypatch.setattr(
        provider_login,
        "kill_login_session",
        lambda name, profile="": killed.setdefault("name", name),
    )
    r = client.post("/api/providers/codex/login-close")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert killed["name"] == "codex"


# --------------------------------------------------------------------------- #
# BaseProvider connection defaults — the floor every provider inherits.
# --------------------------------------------------------------------------- #
def test_base_provider_connection_defaults():
    b = BaseProvider()
    assert b.install_hint() == ""
    assert b.auth_evidence() == ""
    # No aliases -> login_command falls back to the registry name.
    assert b.login_command() == b.name


def test_base_provider_login_command_uses_first_alias():
    class _P(BaseProvider):
        name = "p"
        program_aliases = ("foo", "bar")

    assert _P().login_command() == "foo"


# --------------------------------------------------------------------------- #
# ClaudeProvider.install_hint — npm when present, else the native script.
# --------------------------------------------------------------------------- #
def test_claude_install_hint_prefers_npm(monkeypatch):
    monkeypatch.setattr(
        shutil, "which", lambda b: "/usr/bin/npm" if b == "npm" else None
    )
    assert (
        providers.get("claude").install_hint()
        == "npm install -g @anthropic-ai/claude-code"
    )


def test_claude_install_hint_falls_back_to_curl(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda b: None)
    hint = providers.get("claude").install_hint()
    assert hint.startswith("curl") and "claude.ai/install.sh" in hint


# --------------------------------------------------------------------------- #
# ClaudeProvider.auth_evidence — each credential marker + env fallback.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", ["oauthAccount", "primaryApiKey", "claudeAiOauth"])
def test_claude_auth_evidence_each_marker(marker, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no real ~/.claude.json interference
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text('{"%s": "x"}' % marker)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert ".claude.json" in providers.get("claude").auth_evidence()


def test_claude_auth_evidence_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no credential files anywhere
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert providers.get("claude").auth_evidence() == "ANTHROPIC_API_KEY is set"


def test_claude_auth_evidence_empty_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert providers.get("claude").auth_evidence() == ""


def test_claude_auth_evidence_prefers_config_dir_over_home(tmp_path, monkeypatch):
    # Both the CLAUDE_CONFIG_DIR copy and ~/.claude.json carry a marker; the
    # config-dir candidate is probed first, so its path is the one reported.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text('{"oauthAccount": "home"}')
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text('{"oauthAccount": "cfg"}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    evidence = providers.get("claude").auth_evidence()
    assert str(cfg) in evidence and str(tmp_path / ".claude.json") not in evidence


def test_claude_auth_evidence_finds_a_macos_keychain_login(tmp_path, monkeypatch):
    # On macOS Claude Code keeps its OAuth credentials in the login Keychain and
    # writes no .credentials.json, so a fully logged-in Mac was reported as
    # "no sign of a login" until the keychain counted as evidence too.
    monkeypatch.setenv("HOME", str(tmp_path))  # no credential files anywhere
    monkeypatch.setattr(
        claude_usage_api,
        "_keychain_doc",
        lambda: {"claudeAiOauth": {"accessToken": "x"}},
    )
    assert "Keychain" in providers.get("claude").auth_evidence()


def test_claude_auth_evidence_skips_the_keychain_when_a_file_answered(
    tmp_path, monkeypatch
):
    # The keychain lookup shells out to `security` and can raise a one-time
    # keychain prompt, so a credential file must short-circuit it entirely.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude.json").write_text('{"oauthAccount": "x"}')

    def _never():
        raise AssertionError("the keychain was consulted despite a credential file")

    monkeypatch.setattr(claude_usage_api, "_keychain_doc", _never)
    assert ".claude.json" in providers.get("claude").auth_evidence()


def test_claude_auth_evidence_survives_a_broken_keychain_lookup(tmp_path, monkeypatch):
    # A denied keychain prompt / missing `security` binary is no evidence, never
    # an exception into the doctor or the providers list.
    monkeypatch.setenv("HOME", str(tmp_path))

    def _boom():
        raise OSError("security: user denied access")

    monkeypatch.setattr(claude_usage_api, "_keychain_doc", _boom)
    assert providers.get("claude").auth_evidence() == ""


def test_claude_auth_evidence_never_raises_on_unreadable_path(tmp_path, monkeypatch):
    # A candidate path that is a directory (read_text -> IsADirectoryError) must
    # be swallowed, not propagated.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").mkdir()  # a dir where a file is expected
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert providers.get("claude").auth_evidence() == ""


# --------------------------------------------------------------------------- #
# detect_auth — $VAR expansion and file-beats-env precedence.
# --------------------------------------------------------------------------- #
def test_detect_auth_expands_env_var_in_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCREDDIR", str(tmp_path))
    (tmp_path / "cred").write_text("x")
    assert "cred" in detect_auth(("$MYCREDDIR/cred",), ())


def test_detect_auth_file_wins_over_env(tmp_path, monkeypatch):
    cred = tmp_path / "auth.json"
    cred.write_text("{}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    got = detect_auth((str(cred),), ("OPENAI_API_KEY",))
    assert str(cred) in got and "is set" not in got


# --------------------------------------------------------------------------- #
# [connect] TOML parsing — coercion and partial/malformed tables.
# --------------------------------------------------------------------------- #
def _write_provider(tmp_path, name, body):
    d = tmp_path / "providers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body)


def test_connect_toml_coerces_non_string_list_items(tmp_path):
    _write_provider(
        tmp_path,
        "cli2",
        '[provider]\nname = "cli2"\nprogram = "cli2"\n'
        '[connect]\ninstall_hint = "brew install cli2"\nauth_files = [1, 2]\n',
    )
    providers.rebuild_registry()
    cfg = providers.get("cli2").cfg
    assert cfg.auth_files == ("1", "2")  # ints coerced to str
    assert cfg.install_hint == "brew install cli2"


def test_connect_toml_coerces_non_string_login_command(tmp_path):
    _write_provider(
        tmp_path,
        "cli3",
        '[provider]\nname = "cli3"\nprogram = "cli3"\n[connect]\nlogin_command = 5\n',
    )
    providers.rebuild_registry()
    assert providers.get("cli3").login_command() == "5"


def test_connect_toml_partial_table_leaves_others_empty(tmp_path):
    # Only install_hint set; the missing keys stay empty and login_command
    # falls back to the program alias.
    _write_provider(
        tmp_path,
        "cli4",
        '[provider]\nname = "cli4"\nprogram = "cli4"\n'
        '[connect]\ninstall_hint = "pipx install cli4"\n',
    )
    providers.rebuild_registry()
    p = providers.get("cli4")
    assert p.install_hint() == "pipx install cli4"
    assert p.cfg.auth_files == () and p.cfg.auth_env == ()
    assert p.login_command() == "cli4"  # super() fallback


# --------------------------------------------------------------------------- #
# GenericProvider.login_command / auth_evidence delegation.
# --------------------------------------------------------------------------- #
def test_generic_login_command_falls_back_to_alias(tmp_path):
    _write_provider(
        tmp_path, "nocmd", '[provider]\nname = "nocmd"\nprogram = "nocmd"\n'
    )
    providers.rebuild_registry()
    assert providers.get("nocmd").login_command() == "nocmd"


def test_generic_login_command_uses_configured_value(tmp_path):
    _write_provider(
        tmp_path,
        "hascmd",
        '[provider]\nname = "hascmd"\nprogram = "hascmd"\n'
        '[connect]\nlogin_command = "hascmd sign-in"\n',
    )
    providers.rebuild_registry()
    assert providers.get("hascmd").login_command() == "hascmd sign-in"


def test_generic_auth_evidence_delegates_to_detect_auth(tmp_path):
    cred = tmp_path / "acct.json"
    cred.write_text("{}")
    _write_provider(
        tmp_path,
        "filed",
        '[provider]\nname = "filed"\nprogram = "filed"\n'
        '[connect]\nauth_files = ["%s"]\n' % cred,
    )
    providers.rebuild_registry()
    assert str(cred) in providers.get("filed").auth_evidence()


# --------------------------------------------------------------------------- #
# _provider_status — path-override branch, _safe swallowing, default fallback.
# --------------------------------------------------------------------------- #
def test_provider_status_path_override_installed(tmp_path, monkeypatch):
    exe = tmp_path / "codexbin"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CODEX", str(exe))
    st = settings_addon._provider_status(providers.get("codex"), "claude")
    assert st["installed"] is True
    assert st["path"] == str(exe)


def test_provider_status_path_override_not_executable(tmp_path, monkeypatch):
    noexec = tmp_path / "noexec"
    noexec.write_text("x")  # left non-executable (0o644)
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CODEX", str(noexec))
    st = settings_addon._provider_status(providers.get("codex"), "claude")
    assert st["installed"] is False
    assert st["path"] == ""


def test_provider_status_swallows_auth_evidence_error():
    class _Boom:
        name = "boomcli"
        program_aliases = ("boomcli",)

        def auth_evidence(self):
            raise RuntimeError("credential probe blew up")

        def login_command(self):
            return "boomcli login"

        def install_hint(self):
            return ""

    st = settings_addon._provider_status(_Boom(), "claude")
    assert st["authenticated"] is False
    assert st["auth_detail"] == ""
    assert st["login_command"] == "boomcli login"  # other probes still run


def test_default_provider_name_falls_back_when_settings_raise(monkeypatch):
    def _boom():
        raise RuntimeError("settings store unavailable")

    monkeypatch.setattr(settings_addon.settings_store, "load_settings", _boom)
    assert settings_addon._default_provider_name() == providers.DEFAULT_PROVIDER


# --------------------------------------------------------------------------- #
# _provider_installed — the guard that keeps a missing CLI off the launch path.
# --------------------------------------------------------------------------- #
def test_provider_installed_true_when_on_path(monkeypatch):
    monkeypatch.setattr(
        settings_addon.shutil,
        "which",
        lambda b: "/usr/bin/" + b if b == "codex" else None,
    )
    assert settings_addon._provider_installed("codex") is True


def test_provider_installed_false_when_missing_on_path(monkeypatch):
    monkeypatch.setattr(settings_addon.shutil, "which", lambda b: None)
    assert settings_addon._provider_installed("codex") is False


def test_provider_installed_true_for_executable_path_override(tmp_path, monkeypatch):
    exe = tmp_path / "codexbin"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CODEX", str(exe))
    assert settings_addon._provider_installed("codex") is True


def test_provider_installed_false_for_non_executable_path_override(
    tmp_path, monkeypatch
):
    noexec = tmp_path / "noexec"
    noexec.write_text("x")  # left 0o644
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CODEX", str(noexec))
    assert settings_addon._provider_installed("codex") is False


def test_provider_installed_false_when_binary_empty(monkeypatch):
    monkeypatch.setattr(
        settings_addon.provider_config, "resolve_provider_binary", lambda *a, **k: ""
    )
    assert settings_addon._provider_installed("codex") is False


def test_provider_installed_swallows_resolve_error(monkeypatch):
    def _boom(name):
        raise RuntimeError("registry blew up")

    monkeypatch.setattr(settings_addon.providers, "resolve", _boom)
    assert settings_addon._provider_installed("codex") is False  # never raises


# --------------------------------------------------------------------------- #
# _apply_post — the default-provider integrity guard.
# --------------------------------------------------------------------------- #
def _stored_default():
    return settings_addon.settings_store.load_settings().coding_cli.default_provider


def test_apply_post_rejects_uninstalled_default_provider(monkeypatch):
    monkeypatch.setattr(settings_addon, "_provider_installed", lambda name: False)
    with pytest.raises(ValueError) as ei:
        settings_addon._apply_post({"coding_cli": {"default_provider": "codex"}})
    assert "codex" in str(ei.value)  # message names the offending provider
    assert _stored_default() == ""  # store was NOT patched


def test_apply_post_allows_installed_default_provider(monkeypatch):
    monkeypatch.setattr(settings_addon, "_provider_installed", lambda name: True)
    settings_addon._apply_post({"coding_cli": {"default_provider": "codex"}})
    assert _stored_default() == "codex"


def test_apply_post_allows_clearing_default_provider(monkeypatch):
    # An empty / whitespace-only value clears the default and skips the guard —
    # you can always unset the default even with nothing installed.
    monkeypatch.setattr(
        settings_addon,
        "_provider_installed",
        lambda name: pytest.fail("guard ran while clearing the default"),
    )
    # An empty string clears the stored default outright...
    settings_addon._apply_post({"coding_cli": {"default_provider": ""}})
    assert _stored_default() == ""
    # ...and a whitespace-only value is written through without hitting the
    # install guard (the pytest.fail mock proves the guard never ran).
    settings_addon._apply_post({"coding_cli": {"default_provider": "   "}})


def test_apply_post_guard_is_scoped_to_default_provider_field(monkeypatch):
    # A coding_cli update that doesn't touch default_provider is never guarded,
    # even when nothing is installed.
    monkeypatch.setattr(
        settings_addon,
        "_provider_installed",
        lambda name: pytest.fail("guard ran for a non-default_provider field"),
    )
    settings_addon._apply_post(
        {"coding_cli": {"default_launch_args": {"codex": "--yolo"}}}
    )


# --------------------------------------------------------------------------- #
# /providers/{name}/login-terminal websocket route.
# --------------------------------------------------------------------------- #
def test_login_terminal_ws_unknown_provider_errors_cleanly(client):
    with client.websocket_connect(
        "/api/providers/definitely-not-real/login-terminal"
    ) as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "login flow" in msg["message"]


def test_login_terminal_ws_known_provider_bridges(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        provider_login,
        "ensure_login_session",
        lambda name, profile="": (
            calls.append(name) or ("mindflock_login_" + name, None)
        ),
    )
    monkeypatch.setattr(
        web_terminal,
        "spawn_tmux_attach",
        lambda session, dimensions=(24, 80): ("proc", session),
    )

    async def _fake_pump(ws, proc, allow_input=True):
        await ws.send_text(json.dumps({"type": "bridged", "session": proc[1]}))

    monkeypatch.setattr(web_terminal, "pump_pty", _fake_pump)

    with client.websocket_connect("/api/providers/codex/login-terminal") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "bridged"
    assert msg["session"] == "mindflock_login_codex"
    assert calls == ["codex"]


# --------------------------------------------------------------------------- #
# provider_login internals — session naming, tmux timeouts, and races.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("codex", "mindflock_login_codex"),
        ("My Cli!", "mindflock_login_my_cli_"),
        ("a/b c", "mindflock_login_a_b_c"),
        ("", "mindflock_login_cli"),
        ("   ", "mindflock_login_cli"),
    ],
)
def test_login_session_name_sanitization(raw, expected):
    assert provider_login.login_session_name(raw) == expected


def test_login_command_for_generic_is_none():
    # ``generic`` resolves as a provider but is the catch-all, not a real CLI.
    assert providers.get("generic") is not None
    assert provider_login._login_command_for("generic") is None


def test_login_command_for_swallows_provider_error(monkeypatch):
    p = providers.get("codex")

    def _boom():
        raise RuntimeError("provider quirk")

    monkeypatch.setattr(p, "login_command", _boom)
    assert provider_login._login_command_for("codex") is None


class _CP:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def test_ensure_login_session_has_session_timeout(monkeypatch):
    def _run(argv, **kw):
        if argv[1] == "has-session":
            raise subprocess.TimeoutExpired(argv, 10)
        return _CP()

    monkeypatch.setattr(provider_login.subprocess, "run", _run)
    session, err = provider_login.ensure_login_session("codex")
    assert err == "tmux timed out after 10s"


def test_ensure_login_session_new_session_timeout(monkeypatch):
    def _run(argv, **kw):
        if argv[1] == "has-session":
            return _CP(returncode=1)  # miss -> proceed to new-session
        if argv[1] == "new-session":
            raise subprocess.TimeoutExpired(argv, 10)
        return _CP()

    monkeypatch.setattr(provider_login.subprocess, "run", _run)
    session, err = provider_login.ensure_login_session("codex")
    assert err == "tmux new-session timed out after 10s"


def test_ensure_login_session_duplicate_race_is_success(monkeypatch):
    state = {"has": 0}

    def _run(argv, **kw):
        if argv[1] == "has-session":
            state["has"] += 1
            # First probe misses; the post-new-session race probe hits.
            return _CP(returncode=0 if state["has"] >= 2 else 1)
        if argv[1] == "new-session":
            return _CP(returncode=1)  # duplicate-session loser
        return _CP()

    monkeypatch.setattr(provider_login.subprocess, "run", _run)
    session, err = provider_login.ensure_login_session("codex")
    assert err is None and session == "mindflock_login_codex"


def test_ensure_login_session_new_session_failure_returns_stderr(monkeypatch):
    def _run(argv, **kw):
        if argv[1] == "has-session":
            return _CP(returncode=1)  # always a miss
        if argv[1] == "new-session":
            return _CP(returncode=1, stderr=b"boom: bad tmux")
        return _CP()

    monkeypatch.setattr(provider_login.subprocess, "run", _run)
    session, err = provider_login.ensure_login_session("codex")
    assert err == "boom: bad tmux"


def test_ensure_login_session_new_session_failure_fallback_message(monkeypatch):
    def _run(argv, **kw):
        if argv[1] == "has-session":
            return _CP(returncode=1)
        if argv[1] == "new-session":
            return _CP(returncode=1, stderr=b"")  # no stderr -> generic message
        return _CP()

    monkeypatch.setattr(provider_login.subprocess, "run", _run)
    session, err = provider_login.ensure_login_session("codex")
    assert err == "tmux failed"


def test_ensure_login_session_runs_in_home_and_keeps_pane_alive(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(provider_login.subprocess, "run", fake)
    provider_login.ensure_login_session("codex")
    new_session = next(a for a in fake.calls if a[1] == "new-session")
    # The session is anchored in $HOME via `-c <home>`...
    assert "-c" in new_session
    assert new_session[new_session.index("-c") + 1] == os.path.expanduser("~")
    # ...and the wrapped command re-execs a shell so the pane survives exit.
    wrapped = new_session[-1]
    assert "exec ${SHELL:-/bin/sh}" in wrapped


def test_kill_login_session_targets_the_session_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        provider_login.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or _CP(),
    )
    provider_login.kill_login_session("codex")
    assert calls == [["tmux", "kill-session", "-t=mindflock_login_codex"]]


def test_kill_login_session_swallows_timeout(monkeypatch):
    def _run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(provider_login.subprocess, "run", _run)
    # Must not raise.
    provider_login.kill_login_session("codex")
