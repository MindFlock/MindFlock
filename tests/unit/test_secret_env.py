"""Credentials reach a session through a 0600 file, never through argv.

The gap this closes: `/proc/<pid>/cmdline` is world-readable on Linux, so
`env ANTHROPIC_API_KEY=… claude` hands the key to every local account for as
long as the session runs. Auth profiles were the first feature to put a real
credential on that path.
"""

from __future__ import annotations

import os
import stat

import pytest

from backend.session import secret_env as se


@pytest.fixture(autouse=True)
def _run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "run"))
    return tmp_path / "run"


# --------------------------------------------------------------------------- #
# Classification — broad on purpose: a false positive costs a file write, a
# false negative costs a key in `ps`.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "SOME_SECRET",
        "DB_PASSWORD",
    ],
)
def test_credentials_are_recognised(name):
    assert se.is_secret(name)


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "PORT",
        "MINDFLOCK_PORT_BASE",
        "TESTMON_ENV",
        "MINDFLOCK_PROFILE_ID",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "GOOSE_PROVIDER",
        # A documented literal placeholder LM Studio ignores — routing it
        # through a file would add a file to clean up and protect nothing.
        "LM_STUDIO_API_KEY",
    ],
)
def test_settings_are_not_mistaken_for_credentials(name):
    assert not se.is_secret(name)


def test_split_separates_the_two_halves():
    plain, secret = se.split(
        {
            "PORT": "1",
            "ANTHROPIC_API_KEY": "sk-x",  # pragma: allowlist secret
            "CLAUDE_CONFIG_DIR": "/d",
        }  # pragma: allowlist secret
    )
    assert plain == {"PORT": "1", "CLAUDE_CONFIG_DIR": "/d"}
    assert secret == {"ANTHROPIC_API_KEY": "sk-x"}  # pragma: allowlist secret


def test_split_of_nothing_is_two_empties():
    assert se.split(None) == ({}, {})
    assert se.split({}) == ({}, {})


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def test_write_creates_a_0600_file_in_a_0700_dir(_run_dir):
    path = se.write("win", {"ANTHROPIC_API_KEY": "sk-x"})  # pragma: allowlist secret
    assert path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(_run_dir).st_mode) == 0o700


def test_the_file_is_sourceable_shell_that_exports_the_value(_run_dir):
    import subprocess

    path = se.write(
        "win", {"ANTHROPIC_API_KEY": "sk with 'quotes'"}
    )  # pragma: allowlist secret
    out = subprocess.run(
        ["bash", "-c", ". %s; printf '%%s' \"$ANTHROPIC_API_KEY\"" % path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == "sk with 'quotes'"


def test_writing_nothing_removes_a_previous_file(_run_dir):
    """A session that stops using a profile stops carrying its key."""
    path = se.write("win", {"ANTHROPIC_API_KEY": "sk-x"})  # pragma: allowlist secret
    assert os.path.exists(path)
    assert se.write("win", {}) == ""
    assert not os.path.exists(path)


def test_write_replaces_rather_than_appends(_run_dir):
    se.write("win", {"ANTHROPIC_API_KEY": "old-key"})  # pragma: allowlist secret
    path = se.write("win", {"ANTHROPIC_API_KEY": "new-key"})  # pragma: allowlist secret
    body = open(path).read()
    assert "new-key" in body and "old-key" not in body


def test_two_sessions_get_two_files(_run_dir):
    a = se.write("one", {"ANTHROPIC_API_KEY": "sk-a"})  # pragma: allowlist secret
    b = se.write("two", {"ANTHROPIC_API_KEY": "sk-b"})  # pragma: allowlist secret
    assert a != b
    assert "sk-b" not in open(a).read()


def test_source_prefix_carries_the_path_not_the_value(_run_dir):
    path = se.write(
        "win", {"ANTHROPIC_API_KEY": "sk-secret"}  # pragma: allowlist secret
    )  # pragma: allowlist secret
    prefix = se.source_prefix(path)
    assert path in prefix
    assert "sk-secret" not in prefix


def test_source_prefix_tolerates_a_swept_file():
    """A stale launcher must start without the credential rather than die on a
    missing include — the same outcome as never having had one."""
    import subprocess

    prefix = se.source_prefix("/nonexistent/nope.env")
    r = subprocess.run(
        ["bash", "-c", prefix + "echo ok"], capture_output=True, text=True
    )
    assert r.returncode == 0 and r.stdout.strip() == "ok"


def test_no_path_means_no_prefix():
    assert se.source_prefix("") == ""


def test_clear_is_idempotent(_run_dir):
    se.write("win", {"ANTHROPIC_API_KEY": "sk-x"})  # pragma: allowlist secret
    se.clear("win")
    se.clear("win")  # must not raise on an already-gone file


def test_a_hostile_session_name_cannot_escape_the_run_dir(_run_dir):
    path = se.write(
        "../../etc/passwd", {"ANTHROPIC_API_KEY": "sk-x"}  # pragma: allowlist secret
    )  # pragma: allowlist secret
    assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(str(_run_dir))


# --------------------------------------------------------------------------- #
# The relaunch path uses the same file
# --------------------------------------------------------------------------- #
def test_relaunch_prefix_keeps_the_key_out_of_the_command_string():
    from backend.web.core import agent_sessions

    out = agent_sessions._env_prefix(
        "win",
        {
            "CLAUDE_CONFIG_DIR": "/d",
            "ANTHROPIC_API_KEY": "sk-secret",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    assert "sk-secret" not in out, "the key reached the sh -c argv"
    assert "export CLAUDE_CONFIG_DIR=/d" in out
    assert ".env" in out


def test_two_overlays_on_one_relaunch_do_not_clobber_each_others_keys():
    """The profile and local-model prefixes are applied one after the other and
    both can carry a credential; the second write must not drop the first."""
    from backend.web.core import agent_sessions

    agent_sessions._PENDING_SECRETS.pop("win2", None)
    agent_sessions._env_prefix(
        "win2", {"OPENAI_API_KEY": "sk-local"}  # pragma: allowlist secret
    )  # pragma: allowlist secret
    out = agent_sessions._env_prefix(
        "win2", {"ANTHROPIC_API_KEY": "sk-prof"}  # pragma: allowlist secret
    )  # pragma: allowlist secret
    path = [t for t in out.split() if t.endswith(".env")][0].strip("'\"")
    body = open(path).read()
    assert "sk-local" in body and "sk-prof" in body
    agent_sessions._PENDING_SECRETS.pop("win2", None)
