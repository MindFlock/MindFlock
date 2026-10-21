"""The connected GitHub sign-in reaches git — no keychain, gh or global config.

The gap this closes (2026-10-21, a Mac user who could not provision a session):
Connect GitHub's token (device flow or paste) served the REST API only. Every
clone, fetch and push went through git's own helper chain — on a Mac Apple's
``osxkeychain``, which threw "git-credential-osxkeychain wants to use your
confidential information" at each call once the keychain item's access list
no longer matched the git MindFlock ran — and Setup's "Let git push with this
sign-in" refused ("git already has a credential helper"). Now git's
per-process config (``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n``) makes
``mindflock git-credential`` the one helper for https://github.com on every
git call MindFlock makes and in every session's shell, while a sign-in is
connected — and nothing changes without one.
"""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

import pytest

from backend import git_auth_hints as hints
from backend.config import settings as S
from backend.web.core import github_auth as web_auth

KEY = hints.HELPER_KEY


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Scratch settings, no env tokens, no gh."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(web_auth, "_gh_token", lambda: "")
    S.invalidate()
    yield
    S.invalidate()


def _connect(token: str = "ghp_signin") -> str:
    S.update_settings(github={"token": token})
    return token


def _pairs(env: dict) -> list:
    """``[(key, value), …]`` of the per-process config ``env`` carries."""
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    return [
        (env.get("GIT_CONFIG_KEY_%d" % i), env.get("GIT_CONFIG_VALUE_%d" % i))
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# The per-process config itself
# --------------------------------------------------------------------------- #
def test_reset_then_ours_for_github_only():
    env = hints.with_credential_helper({})
    assert _pairs(env) == [(KEY, ""), (KEY, hints.helper_command())]
    assert set(env) == {
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_KEY_1",
        "GIT_CONFIG_VALUE_1",
    }


def test_the_helper_is_this_install_in_isolated_mode():
    cmd = hints.helper_command()
    assert cmd.startswith("!" + shlex.quote(sys.executable) + " ")
    assert cmd.endswith(" -I -m backend.cli git-credential")


def test_appended_after_config_the_env_already_carries():
    base = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "x.y",
        "GIT_CONFIG_VALUE_0": "z",
    }
    env = hints.with_credential_helper(base)
    assert _pairs(env) == [("x.y", "z"), (KEY, ""), (KEY, hints.helper_command())]
    assert base == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "x.y",
        "GIT_CONFIG_VALUE_0": "z",
    }


def test_laid_on_twice_is_laid_on_once():
    once = hints.with_credential_helper({"A": "1"})
    assert hints.with_credential_helper(once) == once


def test_without_an_interpreter_nothing_is_added(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    assert hints.with_credential_helper({"A": "1"}) == {"A": "1"}


# --------------------------------------------------------------------------- #
# Only while a sign-in is connected
# --------------------------------------------------------------------------- #
def test_nothing_without_a_sign_in():
    assert web_auth.git_env({"A": "1"}) == {"A": "1"}
    assert hints.github_signin({}) == {}
    assert "GIT_CONFIG_COUNT" not in hints.github_env({"A": "1"})


def test_the_settings_token_connects_git():
    _connect()
    env = web_auth.git_env({"A": "1"})
    assert env["A"] == "1" and _pairs(env)[-1] == (KEY, hints.helper_command())
    full = hints.github_env({"A": "1"})
    assert full["A"] == "1" and full["PATH"] == os.environ["PATH"]
    assert _pairs(full) == _pairs(env)


def test_an_env_token_connects_git_but_a_gh_only_sign_in_does_not(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    assert web_auth.connected_for_git()
    monkeypatch.delenv("GITHUB_TOKEN")
    # gh's own sign-in already made gh git's helper (gh auth setup-git): no
    # subprocess per git call to find out.
    monkeypatch.setattr(web_auth, "_gh_token", lambda: "gho_x")
    assert not web_auth.connected_for_git()


def test_a_token_connected_after_boot_reaches_another_process(tmp_path):
    # The ticket pipeline (its clones are what Intake provisions with) is a
    # separate process with a boot-time settings cache; the web server's
    # Connect GitHub writes the file, not that cache.
    assert not web_auth.connected_for_git()  # cache warm, empty
    path = pathlib.Path(os.environ["MINDFLOCK_SETTINGS_FILE"])
    path.write_text(json.dumps({"github": {"token": "ghp_later"}}))
    assert web_auth.connected_for_git()


def test_an_existing_os_helper_does_not_get_in_the_way(monkeypatch):
    # The Mac story: osxkeychain is git's helper, and used to be the only one
    # asked. Per-process config needs no permission from it.
    _connect()
    monkeypatch.setattr(web_auth, "credential_helper", lambda: "osxkeychain")
    assert _pairs(web_auth.git_env({}))[0] == (KEY, "")


# --------------------------------------------------------------------------- #
# Every git call MindFlock makes against GitHub
# --------------------------------------------------------------------------- #
def test_provisioning_clone_and_fetch_carry_the_sign_in():
    from backend.session import provisioned

    assert "GIT_CONFIG_COUNT" not in provisioned._noninteractive_env({})
    _connect()
    env = provisioned._noninteractive_env({})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert _pairs(env)[-1] == (KEY, hints.helper_command())


def test_the_pipeline_clone_carries_the_sign_in():
    from backend.ticket_ingestion.clone_transport import headless_git_env

    assert "GIT_CONFIG_COUNT" not in headless_git_env({})
    _connect()
    env = headless_git_env({})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert _pairs(env)[-1] == (KEY, hints.helper_command())


def test_the_push_check_asks_with_the_sign_in(monkeypatch):
    _connect()
    seen = {}

    def fake_git(args, cwd, *, timeout, env=None, **kw):
        seen["env"] = dict(env or {})

        class R:
            returncode, stdout, stderr, timed_out, not_run = 0, "", "", False, False

        return R()

    monkeypatch.setattr(web_auth.gitutil, "git", fake_git)
    web_auth._git("ls-remote", "origin")
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert _pairs(seen["env"])[0] == (KEY, "")


# --------------------------------------------------------------------------- #
# …and git inside a session
# --------------------------------------------------------------------------- #
def _in_place_instance(monkeypatch, tmp_path):
    from tests.unit.test_instance_lifecycle import FakeTmux
    from backend.session import instance as inst_mod

    inst = inst_mod.Instance()
    inst.Title = "demo"
    inst.Path = str(tmp_path)
    inst.InPlace = True
    inst.ExtraEnv = {"PORT": "9000"}
    made = FakeTmux()
    monkeypatch.setattr(inst_mod.tmux, "NewTmuxSession", lambda title, program: made)
    return inst, made


def test_a_session_launched_while_connected_carries_the_sign_in(monkeypatch, tmp_path):
    _connect()
    inst, made = _in_place_instance(monkeypatch, tmp_path)
    inst.Start(True)
    assert made.extra_env["PORT"] == "9000"
    assert _pairs(made.extra_env) == [(KEY, ""), (KEY, hints.helper_command())]


def test_a_session_launched_without_one_is_unchanged(monkeypatch, tmp_path):
    inst, made = _in_place_instance(monkeypatch, tmp_path)
    inst.Start(True)
    assert made.extra_env == {"PORT": "9000"}


def test_a_shared_folder_session_never_carries_it(monkeypatch, tmp_path):
    _connect()
    inst, made = _in_place_instance(monkeypatch, tmp_path)
    inst.PeerShare = "share-1"
    monkeypatch.setattr(
        inst, "_configure_peer_launch_command", lambda resume=False: None
    )
    monkeypatch.setattr(inst, "_peer_guard_launch", lambda resume: None)
    inst.Start(True)
    assert made.extra_env == {"PORT": "9000"}


def test_the_config_keys_ride_the_session_env_not_the_secret_file():
    from backend.session import secret_env

    # GIT_CONFIG_KEY_n names a config key, never a credential: routed to the
    # 0600 file while its count and value stayed on the env, every later
    # pane's git would die with "missing config key".
    assert not secret_env.is_secret("GIT_CONFIG_KEY_0")
    assert not secret_env.is_secret("git_config_key_12")
    plain, secret = secret_env.split(hints.with_credential_helper({}))
    assert secret == {} and set(plain) == set(hints.with_credential_helper({}))


def test_the_shell_session_exports_the_sign_in(monkeypatch):
    from backend.web.core import agent_sessions

    calls = []

    class _Srv:
        def _shell_tmux_name(self, title):
            return "mindflock_shell_" + title

        def _live_session_name(self, name):
            return None

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(agent_sessions, "_server", lambda: _Srv())
    monkeypatch.setattr(agent_sessions, "_peer_shell_refused", lambda t, w: False)
    monkeypatch.setattr(agent_sessions._proc, "run_capped", fake_run)
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: None)
    monkeypatch.setattr(
        agent_sessions.providers.launch_script, "oom_score_prefix", lambda: ""
    )
    monkeypatch.setenv("SHELL", "/bin/zsh")

    name, err = agent_sessions._ensure_shell_session("t", "/tmp")
    assert err is None
    line = [c for c in calls if c[:2] == ["tmux", "new-session"]][0][-1]
    assert line == "/bin/zsh"  # nothing connected: the shell, untouched

    _connect()
    calls.clear()
    agent_sessions._ensure_shell_session("t", "/tmp")
    line = [c for c in calls if c[:2] == ["tmux", "new-session"]][0][-1]
    assert line.startswith("export GIT_CONFIG_COUNT=2; export GIT_CONFIG_KEY_0=")
    assert "export GIT_CONFIG_VALUE_0=''; " in line
    assert line.endswith("; exec /bin/zsh")


# --------------------------------------------------------------------------- #
# git itself, and the helper from a fresh process
# --------------------------------------------------------------------------- #
def _git_version() -> tuple:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    try:
        return tuple(int(p) for p in out.split()[2].split(".")[:2])
    except (IndexError, ValueError):
        return (0, 0)


needs_git = pytest.mark.skipif(
    not shutil.which("git") or _git_version() < (2, 31),
    reason="needs git ≥ 2.31 (GIT_CONFIG_COUNT)",
)


@needs_git
def test_git_asks_the_sign_in_first_and_leaves_other_hosts_alone(tmp_path, monkeypatch):
    # A global helper stands in for osxkeychain: with the pairs, github.com
    # never reaches it; any other host still does.
    theirs = tmp_path / "theirs.sh"
    theirs.write_text("#!/bin/sh\necho username=them\necho password=THEIRS\n")
    ours = tmp_path / "ours.sh"
    ours.write_text("#!/bin/sh\necho username=x-access-token\necho password=OURS\n")
    for p in (theirs, ours):
        p.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[credential]\n\thelper = !%s\n" % theirs)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    def fill(host: str, env: dict) -> str:
        cp = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=%s\n" % host,
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            timeout=20,
        )
        return cp.stdout

    assert "password=THEIRS" in fill("github.com", {})
    env = hints.with_credential_helper({}, helper="!" + str(ours))
    assert "password=OURS" in fill("github.com", env)
    assert "password=THEIRS" in fill("gitlab.com", env)


@needs_git
def test_the_helper_answers_from_any_folder_with_the_settings_token(tmp_path):
    # Run as git would: this interpreter, isolated, from a worktree that has
    # a backend/ of its own (a MindFlock checkout!) which must not shadow the
    # installed package.
    token = _connect("ghp_fresh_process")
    decoy = tmp_path / "work" / "backend"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("")
    (decoy / "cli.py").write_text("print('DECOY')\n")
    cmd = hints.helper_command()[1:] + " get"
    cp = subprocess.run(
        ["sh", "-c", cmd],
        input="protocol=https\nhost=github.com\n",
        capture_output=True,
        text=True,
        cwd=str(tmp_path / "work"),
        env={**os.environ, "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    assert "DECOY" not in cp.stdout
    assert cp.stdout == "username=x-access-token\npassword=%s\n" % token
