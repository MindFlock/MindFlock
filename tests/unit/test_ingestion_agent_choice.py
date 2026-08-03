"""Ingestion is multi-CLI: which agent an ingested ticket runs, and how.

Before this, ingestion could only really run Claude Code — the provisioned
launcher hardcoded Claude's flags — which contradicted the provider-agnostic
pitch and forced a paid Claude dependency. These tests pin the two halves of the
fix:

  * **selection** — a source's ``agent`` wins, then ``[mindflock].agent``, then
    the engine default, and the same chain is used by every launch path (engine
    bridge, standalone tmux, PR review, forced start) so they cannot disagree;
  * **defaults are untouched** — with nothing configured the resolution is ``""``
    (= "use the engine default"), so existing installs are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.ticket_ingestion.config import (
    ConfigError,
    EngineConfig,
    PipelineConfig,
    TicketProviderConfig,
    known_agents,
)


def _cfg(sources, engine_agent: str = "") -> PipelineConfig:
    return PipelineConfig(
        repo_url="git@github.com:org/repo.git",
        ticketing_sources=sources,
        engine=EngineConfig(enabled=True, mode="worktree", agent=engine_agent),
    )


# --------------------------------------------------------------------------- #
# The resolution chain.
# --------------------------------------------------------------------------- #
def test_nothing_configured_defers_to_the_engine_default():
    cfg = _cfg([TicketProviderConfig(provider="shortcut")])
    # "" is the signal every launch path reads as "use the engine's own default
    # program" — so an install that never sets an agent is byte-unchanged.
    assert cfg.agent_for("shortcut") == ""
    assert cfg.agent_for() == ""


def test_engine_agent_is_the_pipeline_wide_default():
    cfg = _cfg([TicketProviderConfig(provider="shortcut")], engine_agent="codex")
    assert cfg.agent_for("shortcut") == "codex"
    # PR review / issue handling have no source, so they take the same default.
    assert cfg.agent_for() == "codex"


def test_source_agent_overrides_the_pipeline_default():
    cfg = _cfg(
        [
            TicketProviderConfig(provider="jira", id="jira", agent="aider"),
            TicketProviderConfig(provider="shortcut", id="sc"),
        ],
        engine_agent="codex",
    )
    assert cfg.agent_for("jira") == "aider"  # its own choice
    assert cfg.agent_for("sc") == "codex"  # falls back to the pipeline default
    assert cfg.agent_for("nope") == "codex"  # unknown source -> default


def test_two_sources_can_run_different_clis():
    """The point of per-source selection: route one queue to a hosted CLI and
    another to a local-model CLI in the same flock."""
    cfg = _cfg(
        [
            TicketProviderConfig(provider="github_issues", id="gh", agent="goose"),
            TicketProviderConfig(provider="jira", id="jira", agent="claude"),
        ]
    )
    assert cfg.agent_for("gh") == "goose"
    assert cfg.agent_for("jira") == "claude"


# --------------------------------------------------------------------------- #
# Validation: a typo must be caught at load, not at launch.
# --------------------------------------------------------------------------- #
COMMON = """
[repository]
url = "git@github.com:org/repo.git"
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_known_agents_lists_real_providers_without_the_catch_all():
    names = known_agents()
    assert "claude" in names and "codex" in names
    # ``generic`` claims every program, so it is never a meaningful choice.
    assert "generic" not in names


def test_unknown_source_agent_is_a_config_error(tmp_path):
    """Without this the launch path falls through to the catch-all provider and
    runs the misspelled name as a bare program — the session dies with a shell
    "command not found" that reads like a MindFlock bug."""
    from backend.ticket_ingestion.config import load_config

    with pytest.raises(ConfigError, match="agent"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "github_issues"
agent = "clyde"
""" + COMMON,
            )
        )


def test_unknown_engine_agent_is_a_config_error(tmp_path):
    from backend.ticket_ingestion.config import load_config

    with pytest.raises(ConfigError, match="agent"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "github_issues"

[mindflock]
agent = "clyde"
""" + COMMON,
            )
        )


def test_valid_agents_load(tmp_path):
    from backend.ticket_ingestion.config import load_config

    cfg = load_config(
        _write(
            tmp_path,
            """
[ticketing]
provider = "github_issues"
agent = "aider"

[mindflock]
agent = "codex"
""" + COMMON,
        )
    )
    assert cfg.engine is not None and cfg.engine.agent == "codex"
    assert cfg.ticketing is not None and cfg.ticketing.agent == "aider"
    assert cfg.agent_for(cfg.ticketing.id) == "aider"


# --------------------------------------------------------------------------- #
# The stamp reaches the launch paths.
# --------------------------------------------------------------------------- #
def test_ticket_carries_its_source_agent():
    from tests._factories import make_ticket

    t = make_ticket(id=1, name="n")
    assert t.agent == ""  # default: engine default downstream
    t.agent = "goose"
    assert t.agent == "goose"


def test_standalone_runner_resolves_the_tickets_agent():
    from backend.ticket_ingestion.claude_runner import AgentCliRunner
    from tests._factories import make_ticket

    cfg = _cfg(
        [TicketProviderConfig(provider="jira", id="jira", agent="aider")],
        engine_agent="codex",
    )
    runner = AgentCliRunner(cfg)
    stamped = make_ticket(id=1, name="n")
    stamped.agent = "goose"
    assert runner.agent_for(stamped) == "goose"  # the ticket's own stamp wins

    unstamped = make_ticket(id=2, name="n")
    unstamped.provider = "jira"
    assert runner.agent_for(unstamped) == "aider"  # then its source's

    other = make_ticket(id=3, name="n")
    other.provider = "shortcut"
    assert runner.agent_for(other) == "codex"  # then the pipeline default


def test_session_runner_prefers_the_stamp_then_the_source():
    from backend.ticket_ingestion.session_runner import SessionRunner, _resolve_program
    from tests._factories import make_ticket

    cfg = _cfg(
        [TicketProviderConfig(provider="jira", id="jira", agent="aider")],
        engine_agent="codex",
    )
    runner = SessionRunner(cfg)
    t = make_ticket(id=1, name="n")
    t.provider = "jira"
    assert runner._agent_for(t) == "aider"
    t.agent = "goose"
    assert runner._agent_for(t) == "goose"
    # An explicit agent is used verbatim as the instance program.
    assert _resolve_program("goose") == "goose"


def test_standalone_runner_launches_the_chosen_cli(tmp_path):
    """The standalone tmux path must build the CHOSEN CLI's command — this is the
    path that used to hardcode `claude "$(cat prompt)"`."""
    from backend.providers import launch_script

    prompt = tmp_path / "p.md"
    prompt.write_text("do it", encoding="utf-8")

    _, codex_cmd = launch_script.launch_command("codex", str(prompt))
    assert codex_cmd.startswith("codex ")
    assert "claude" not in codex_cmd

    # goose's chat entry point is a subcommand and it takes no prompt argument,
    # so it gets `goose session` plus the keystroke seeder.
    preamble, goose_cmd = launch_script.launch_command("goose", str(prompt))
    assert "goose session" in goose_cmd
    assert launch_script.SEED_FN in goose_cmd and "paste-buffer" in preamble
