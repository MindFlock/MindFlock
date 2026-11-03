"""A person whose only agent is aider, opencode, cline or goose: the gaps a
keyless end-to-end run found (docs/vision/agents-oai.md).

* cline and goose launch through a subcommand (``cline -i``, ``goose
  session``), and the install checks looked THAT up on PATH — so both read as
  not installed and could not be made the default agent.
* aider prints its gates and questions into the scrollback: an answered one
  stays on screen, and was answered again (``D`` + Enter typed as the task) or
  read as Needs you for good.
* aider's first-run "Add .aider* to .gitignore?" held every new session's task.
* cline's first launch opens a ClinePass promo that swallowed the typed task.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from backend import providers
from backend.providers import config as provider_config

DATA = Path(__file__).parent / "data" / "dialogs"


def _screen(name: str) -> str:
    text = (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    assert first.startswith("#! ")
    return rest


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch, tmp_path):
    for n in ("AIDER", "OPENCODE", "CLINE", "GOOSE"):
        monkeypatch.delenv("MINDFLOCK_PROVIDER_BIN_" + n, raising=False)
    monkeypatch.setattr(provider_config, "settings_binary_path", lambda name: "")


# --------------------------------------------------------------------------- #
# The executable vs the command line                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name, exe, command",
    [
        ("cline", "cline", "cline -i"),
        ("goose", "goose", "goose session"),
        ("aider", "aider", "aider"),
        ("opencode", "opencode", "opencode"),
    ],
)
def test_the_executable_is_the_program_not_its_subcommand(name, exe, command):
    cfg = providers.resolve(name).cfg
    assert cfg.resolved_executable() == exe
    assert provider_config.resolve_provider_binary(name, cfg) == exe
    # The launch still runs the interactive subcommand.
    assert cfg.resolved_binary() == command


@pytest.mark.parametrize("name", ["aider", "opencode", "cline", "goose"])
def test_an_installed_cli_is_installed(name, monkeypatch, tmp_path):
    """The default-agent guard (POST /api/settings) and Settings → Providers:
    a CLI on PATH is installed, whatever subcommand it launches with."""
    from backend.web.core import settings_hooks

    exe = tmp_path / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert settings_hooks.provider_installed(name) is True


def test_a_binary_override_keeps_the_interactive_subcommand(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_GOOSE", "/opt/goose/bin/goose")
    p = providers.resolve("goose")
    assert p.cfg.resolved_executable() == "/opt/goose/bin/goose"
    assert p.cfg.resolved_binary() == "/opt/goose/bin/goose session"
    cmd = p.build_launch_command(providers.LaunchContext(program="goose"))
    assert cmd.startswith("/opt/goose/bin/goose session")
    # The one-shot still hangs off the executable alone.
    assert p.oneshot_argv("hi") == ["/opt/goose/bin/goose", "run", "-q", "-t", "hi"]


# --------------------------------------------------------------------------- #
# aider: answered gates and questions stay in the scrollback                    #
# --------------------------------------------------------------------------- #
def test_an_answered_aider_gate_is_not_answered_again():
    spec = providers.resolve("aider").trust_prompt()
    screen = _screen("aider_docgate_answered_w80")
    assert "Open documentation url for more info" in screen  # still visible
    assert spec.on_screen(screen) is False
    assert providers.resolve("aider").dialog_on_screen(screen) is False


def test_a_live_aider_gate_is_answered():
    spec = providers.resolve("aider").trust_prompt()
    screen = _screen("aider_docgate_answered_w80")
    live = screen[: screen.index("[Yes]: D")] + "[Yes]: \n"
    assert spec.on_screen(live) is True
    assert spec.keystroke == b"D\r"
    # Wrapped at a narrow width, the phrase straddles two lines.
    assert spec.on_screen(
        "x\nOpen documentation url\nfor more info? (Y)es/(N)o [Yes]:\n"
    )


@pytest.mark.parametrize(
    "name", ["aider_create_answered_w80", "aider_docgate_answered_w80"]
)
def test_an_answered_aider_question_is_not_needs_you(name):
    screen = _screen(name)
    assert re.search(r"\(Y\)es/\(N\)o", screen)  # the old, unanchored reading
    pats = providers.resolve("aider").waiting_prompt_patterns()
    assert not any(re.search(p, screen) for p in pats)


@pytest.mark.parametrize(
    "tail",
    [
        "Create new file? (Y)es/(N)o [Yes]: ",
        "Run shell command? (Y)es/(N)o/(D)on't ask again [Y\nes]:   \n\n",
        "Allow edits? (Y)es/(N)o [No]: y",  # typed, not yet sent
    ],
)
def test_a_live_aider_question_is_needs_you(tail):
    pats = providers.resolve("aider").waiting_prompt_patterns()
    assert any(re.search(p, "> hello\n" + tail) for p in pats)


def _git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def test_aider_files_are_excluded_before_launch(tmp_path):
    """No first-run "Add .aider* to .gitignore?" (it checks ``git
    check-ignore``), nothing for a Commit stop to pick up, .gitignore untouched
    — in a worktree too (info/exclude lives in the common git dir)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=repo,
    )
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    prov = providers.resolve("aider")
    assert prov.cfg.git_excludes == (".aider*",)
    prov.install_activity_hooks(str(wt), "mindflock_t")
    prov.install_activity_hooks(str(wt), "mindflock_t")  # idempotent
    for name in (".aider", ".aider.chat.history.md", ".aider.tags.cache.v4"):
        cp = subprocess.run(["git", "check-ignore", "-q", name], cwd=wt)
        assert cp.returncode == 0, name
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".aider*") == 1
    assert not (wt / ".gitignore").exists()


def test_toml_declares_tail_lines_and_excludes():
    cfg = provider_config._config_from_toml(
        {
            "provider": {"name": "mycli"},
            "launch": {"git_excludes": [".mycli*"]},
            "classify": {
                "trust_patterns": ["Trust?"],
                "trust_keystroke": "esc",
                "trust_tail_lines": 2,
            },
        }
    )
    assert cfg.git_excludes == (".mycli*",)
    assert cfg.trust_tail_lines == 2
    assert cfg.keystroke_bytes() == b"\x1b"


# --------------------------------------------------------------------------- #
# cline: the first-launch ClinePass promo                                      #
# --------------------------------------------------------------------------- #
def test_the_clinepass_promo_is_a_gate_closed_with_escape():
    spec = providers.resolve("cline").trust_prompt()
    screen = _screen("cline_clinepass_w120")
    assert spec.on_screen(screen) is True
    # Enter would open a browser; Escape closes it and types nothing.
    assert spec.keystroke == b"\x1b"
    # Any automated typer holds its message while it is up.
    assert providers.resolve("cline").dialog_on_screen(screen) is True


@pytest.mark.parametrize(
    "name", ["cline_command_w80", "cline_question_w80", "cline_denied_w80"]
)
def test_cline_dialog_screens_are_not_the_promo(name):
    assert providers.resolve("cline").trust_prompt().on_screen(_screen(name)) is False


# --------------------------------------------------------------------------- #
# aider: a live turn reads as working                                          #
# --------------------------------------------------------------------------- #
def test_aider_mid_turn_is_working():
    """The spinner names the model ("Waiting for openai/mock"), so the old
    "Waiting for LLM" literal never matched and a quick turn never read as
    working — an Agent stop then waited for the agent to start."""
    pats = providers.resolve("aider").working_pane_patterns()
    screen = _screen("aider_working_w200")
    assert "Waiting for LLM" not in screen
    assert any(re.search(p, screen) for p in pats)
    # ASCII frames, a narrow pane.
    assert any(re.search(p, "> hi\n  =#      Waiting for gpt-4o") for p in pats)


@pytest.mark.parametrize(
    "name",
    ["aider_create_answered_w80", "aider_docgate_answered_w80", "aider_answered_w60"],
)
def test_aider_idle_is_not_working(name):
    pats = providers.resolve("aider").working_pane_patterns()
    assert not any(re.search(p, _screen(name)) for p in pats)
    assert not any(re.search(p, "> Waiting for the build to finish") for p in pats)


# --------------------------------------------------------------------------- #
# goose: the MindFlock MCP rides on its command line                          #
# --------------------------------------------------------------------------- #
def test_goose_attaches_the_mindflock_mcp_per_launch():
    from backend.providers import mcp_attach

    assert "goose" in mcp_attach.supported_providers()
    for name in ("aider", "opencode", "cline"):
        assert name not in mcp_attach.supported_providers()
    spec = mcp_attach.McpSpec(
        title="t1", tmux_name="mindflock_t1", python="/py", pythonpath="/src", port=9123
    )
    args = providers.resolve("goose").mcp_launch_args(spec)
    assert args[0] == "--with-extension" and len(args) == 2
    # env(1) carries PYTHONPATH (goose filters it from an extension's env).
    assert args[1].startswith("env ") and "PYTHONPATH=/src" in args[1]
    assert "MINDFLOCK_SESSION_TITLE=t1" in args[1] and "MINDFLOCK_PORT=9123" in args[1]
    assert args[1].endswith("/py -P -m backend.mcp")
    cmd = providers.resolve("goose").build_launch_command(
        providers.LaunchContext(program="goose", launch_args=args)
    )
    assert cmd.startswith("goose session --with-extension 'env ")
    assert providers.resolve("aider").mcp_launch_args(spec) == ()
