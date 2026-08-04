"""Settings → Coding provider must decide what actually launches.

The regression these guard against: the chosen default lived in
``settings.json`` (``coding_cli.default_provider``) while every launch path read
``config.json``'s ``default_program``, which is seeded once on first run by a
helper that only hunts for ``claude``. Nothing bridged the two, so picking a
different default changed the Providers badge and ``mindflock doctor`` and
nothing else — every session, ingested or hand-started, still ran Claude.
"""

from __future__ import annotations

import pytest

from backend.config import program as P


class _Cfg:
    def __init__(self, program: str) -> None:
        self._program = program

    def GetProgram(self) -> str:
        return self._program


def _settings(default_provider: str):
    """A stand-in for the settings store exposing only what the resolver reads."""
    return type(
        "S",
        (),
        {"coding_cli": type("C", (), {"default_provider": default_provider})()},
    )()


class TestResolveDefaultProgram:
    def test_settings_choice_beats_the_engine_config(self, monkeypatch):
        """The whole point: config.json still says claude on every install that
        predates this, so the user's pick has to win or nothing changes."""
        monkeypatch.setattr(P, "load_settings", lambda: _settings("codex"))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("claude"))
        assert P.resolve_default_program() == "codex"

    def test_falls_back_to_engine_config_when_nothing_is_chosen(self, monkeypatch):
        monkeypatch.setattr(P, "load_settings", lambda: _settings(""))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("aider"))
        assert P.resolve_default_program() == "aider"

    def test_blank_setting_is_not_a_choice(self, monkeypatch):
        """A whitespace-only value is an empty form field, not a provider named
        ' ' — it must not shadow the engine config."""
        monkeypatch.setattr(P, "load_settings", lambda: _settings("   "))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("goose"))
        assert P.resolve_default_program() == "goose"

    @pytest.mark.parametrize("broken", ["settings", "config", "both"])
    def test_a_broken_store_degrades_instead_of_raising(self, monkeypatch, broken):
        """Every caller is mid-launch; an unreadable store must not become an
        exception that kills the session."""

        def _boom():
            raise OSError("unreadable")

        monkeypatch.setattr(
            P,
            "load_settings",
            _boom if broken in ("settings", "both") else lambda: _settings(""),
        )
        monkeypatch.setattr(
            P,
            "LoadConfig",
            _boom if broken in ("config", "both") else lambda: _Cfg("codex"),
        )
        expected = "claude" if broken in ("config", "both") else "codex"
        assert P.resolve_default_program() == expected

    def test_last_resort_is_the_literal_default(self, monkeypatch):
        monkeypatch.setattr(P, "load_settings", lambda: _settings(""))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg(""))
        assert P.resolve_default_program() == P.DEFAULT_PROGRAM == "claude"


class TestLaunchPathsUseTheResolver:
    """Each launch path resolved the default independently before this, which is
    how they drifted. These pin them to the shared chain."""

    def test_ingestion_engine_path(self, monkeypatch):
        from backend.ticket_ingestion import session_runner

        monkeypatch.setattr(P, "load_settings", lambda: _settings("codex"))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("claude"))
        assert session_runner._resolve_program("") == "codex"

    def test_ingestion_standalone_path(self, monkeypatch):
        from backend.ticket_ingestion import claude_runner

        monkeypatch.setattr(P, "load_settings", lambda: _settings("codex"))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("claude"))
        assert claude_runner.default_agent() == "codex"

    def test_an_explicit_per_source_agent_still_wins(self, monkeypatch):
        """A ticketing source that names its own CLI outranks the global
        default — otherwise per-source routing would be silently ignored."""
        from backend.ticket_ingestion import session_runner

        monkeypatch.setattr(P, "load_settings", lambda: _settings("codex"))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("claude"))
        assert session_runner._resolve_program("aider") == "aider"


def _pipeline(github_agent="", issue_agent="", engine_agent=""):
    """A PipelineConfig carrying only the fields the agent chain reads."""
    from backend.ticket_ingestion.config import (
        EngineConfig,
        GithubConfig,
        PipelineConfig,
    )

    gh = GithubConfig(
        base_branch="main",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        agent=github_agent,
        issue_agent=issue_agent,
    )
    cfg = PipelineConfig()
    cfg.github = gh
    cfg.engine = EngineConfig(agent=engine_agent)
    return cfg


class TestPerSurfaceAgents:
    """PR review and issue handling each pick their own CLI. They are separately
    configured features — separate repo lists, separate toggles — so neither may
    quietly inherit the other's choice."""

    def test_pr_review_uses_its_own_agent(self):
        assert _pipeline(github_agent="codex").pr_agent() == "codex"

    def test_issue_handling_uses_its_own_agent(self):
        assert _pipeline(issue_agent="aider").issue_agent() == "aider"

    def test_issue_handling_does_not_inherit_the_review_agent(self):
        """Setting only PR review's CLI must leave issue handling on the shared
        default, not silently adopt the review one."""
        cfg = _pipeline(github_agent="codex")
        assert cfg.pr_agent() == "codex"
        assert cfg.issue_agent() == ""

    def test_review_does_not_inherit_the_issue_agent(self):
        cfg = _pipeline(issue_agent="aider")
        assert cfg.issue_agent() == "aider"
        assert cfg.pr_agent() == ""

    def test_both_fall_back_to_the_pipeline_wide_agent(self):
        cfg = _pipeline(engine_agent="goose")
        assert cfg.pr_agent() == "goose"
        assert cfg.issue_agent() == "goose"

    def test_a_surface_agent_outranks_the_pipeline_wide_one(self):
        cfg = _pipeline(github_agent="codex", issue_agent="aider", engine_agent="goose")
        assert cfg.pr_agent() == "codex"
        assert cfg.issue_agent() == "aider"

    def test_unset_means_empty_so_the_resolver_decides(self):
        cfg = _pipeline()
        assert cfg.pr_agent() == ""
        assert cfg.issue_agent() == ""


class TestAssistantProgram:
    def test_uses_its_own_setting_first(self, monkeypatch):
        """The assistant was hardcoded to claude, which made it unusable for
        anyone who had never set Claude up."""
        from backend.web.addons import assistant
        from backend.config import settings as settings_mod

        monkeypatch.setattr(
            settings_mod,
            "load_settings",
            lambda: type(
                "S",
                (),
                {
                    "coding_cli": type(
                        "C", (), {"assistant_provider": "codex", "default_provider": ""}
                    )()
                },
            )(),
        )
        assert assistant._assistant_program() == "codex"

    def test_falls_back_to_the_shared_default(self, monkeypatch):
        from backend.web.addons import assistant
        from backend.config import settings as settings_mod

        monkeypatch.setattr(
            settings_mod,
            "load_settings",
            lambda: type(
                "S",
                (),
                {"coding_cli": type("C", (), {"assistant_provider": ""})()},
            )(),
        )
        monkeypatch.setattr(P, "load_settings", lambda: _settings("aider"))
        monkeypatch.setattr(P, "LoadConfig", lambda: _Cfg("claude"))
        assert assistant._assistant_program() == "aider"


class TestParsedGithubCarriesTheAgents:
    """The seam the per-surface pickers actually travel through.

    TestPerSurfaceAgents builds a GithubConfig by hand and TestSettingsRoundTrip
    stops at GithubSettings, so nothing used to exercise ``_parse_github`` — and
    it silently dropped both agent fields, leaving them at their "" dataclass
    default however the config was configured. The precedence logic was correct
    and unreachable: every PR review fell through to the app-wide default.
    """

    def _parsed(self, **github):
        from pathlib import Path

        from backend.ticket_ingestion.config import _parse_github

        return _parse_github(
            {"github": {"base_branch": "main", **github}}, Path("config.toml")
        )

    def test_pr_review_agent_survives_the_parse(self):
        assert self._parsed(agent="antigravity").agent == "antigravity"

    def test_issue_agent_survives_the_parse(self):
        assert self._parsed(issue_agent="codex").issue_agent == "codex"

    def test_both_default_to_empty_so_the_resolver_still_decides(self):
        gh = self._parsed()
        assert (gh.agent, gh.issue_agent) == ("", "")

    def test_parsed_config_reaches_pr_agent(self):
        """End to end through the chain the launch paths call."""
        from backend.ticket_ingestion.config import EngineConfig, PipelineConfig

        cfg = PipelineConfig()
        cfg.github = self._parsed(agent="antigravity")
        cfg.engine = EngineConfig(agent="")
        assert cfg.pr_agent() == "antigravity"


class TestForcedReviewHonoursTheReviewAgent:
    """ "Begin review" used to launch ENGINE.default_program() outright, so the
    Agent CLI dropdown directly above the button governed only the auto
    monitor."""

    def test_review_agent_reads_the_configured_pr_agent(self, monkeypatch):
        from backend.web.core import pr_review

        monkeypatch.setattr(
            pr_review,
            "_load_config",
            lambda: type("C", (), {"pr_agent": lambda self: "antigravity"})(),
        )
        assert pr_review.review_agent() == "antigravity"

    def test_unset_returns_blank_so_the_caller_falls_back(self, monkeypatch):
        from backend.web.core import pr_review

        monkeypatch.setattr(
            pr_review,
            "_load_config",
            lambda: type("C", (), {"pr_agent": lambda self: ""})(),
        )
        assert pr_review.review_agent() == ""

    def test_unconfigured_ingestion_degrades_instead_of_blocking_the_review(
        self, monkeypatch
    ):
        from backend.web.core import pr_review

        def boom():
            raise RuntimeError("ingestion was never configured")

        monkeypatch.setattr(pr_review, "_load_config", boom)
        assert pr_review.review_agent() == ""

    def test_the_route_prefers_the_review_agent_over_the_app_default(self):
        """Pins the launch expression itself: the forced-review instance takes
        review_agent() first and only then ENGINE.default_program()."""
        import inspect

        from backend.web import server

        src = inspect.getsource(server.github_force_review)
        assert "_pr_review.review_agent() or ENGINE.default_program()" in src


class TestProviderSwitchAppliesToTheNextLaunch:
    """Switching provider must apply to the NEXT ticket / issue / PR, not to the
    one after the next pipeline restart.

    ``__main__.main`` calls ``load_config()`` once and hands the snapshot to the
    orchestrator for the life of the process, so every per-surface agent read off
    ``self.config`` was pinned to whatever was configured at startup. Agent
    choice is re-read at launch instead — the rule ``resolve_default_program``
    already follows for the app-wide default.
    """

    def test_config_for_launch_rereads(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        monkeypatch.setattr(C, "load_config", lambda: _pipeline(github_agent="codex"))
        assert (
            C.config_for_launch(_pipeline(github_agent="stale")).pr_agent() == "codex"
        )

    def test_fresh_agent_prefers_disk_over_the_snapshot(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        monkeypatch.setattr(C, "load_config", lambda: _pipeline(github_agent="codex"))
        snap = _pipeline(github_agent="stale")
        assert C.fresh_agent(lambda c: c.pr_agent(), snap) == "codex"

    def test_fresh_agent_keeps_the_snapshot_when_disk_has_no_opinion(self, monkeypatch):
        """The config is a constructor argument; a caller that built one by hand
        keeps it unless the on-disk config makes an explicit choice."""
        from backend.ticket_ingestion import config as C

        monkeypatch.setattr(C, "load_config", lambda: _pipeline())
        snap = _pipeline(github_agent="aider")
        assert C.fresh_agent(lambda c: c.pr_agent(), snap) == "aider"

    def test_fresh_agent_survives_an_unreadable_config(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        def boom():
            raise RuntimeError("gone")

        monkeypatch.setattr(C, "load_config", boom)
        assert (
            C.fresh_agent(lambda c: c.pr_agent(), _pipeline(github_agent="aider"))
            == "aider"
        )

    def test_fresh_agent_with_nothing_anywhere_is_blank(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        monkeypatch.setattr(C, "load_config", lambda: _pipeline())
        assert C.fresh_agent(lambda c: c.pr_agent(), None) == ""

    def test_config_for_launch_falls_back_when_the_reread_fails(self, monkeypatch):
        """A config that broke since startup must not fail the launch."""
        from backend.ticket_ingestion import config as C

        def boom():
            raise RuntimeError("config.toml went missing")

        monkeypatch.setattr(C, "load_config", boom)
        snapshot = _pipeline(github_agent="snapshot-value")
        assert C.config_for_launch(snapshot) is snapshot

    def test_ticket_launch_sees_a_provider_switched_after_startup(self, monkeypatch):
        """SessionRunner holds a startup snapshot; the ticket's CLI must not."""
        from backend.ticket_ingestion import config as C, session_runner

        runner = session_runner.SessionRunner(_pipeline(engine_agent="stale"))
        monkeypatch.setattr(C, "load_config", lambda: _pipeline(engine_agent="codex"))
        story = type("S", (), {"agent": "", "provider": ""})()
        assert runner._agent_for(story) == "codex"

    def test_a_ticket_that_pins_its_own_agent_still_wins(self, monkeypatch):
        from backend.ticket_ingestion import config as C, session_runner

        runner = session_runner.SessionRunner(_pipeline())
        monkeypatch.setattr(C, "load_config", lambda: _pipeline(engine_agent="codex"))
        story = type("S", (), {"agent": "aider", "provider": ""})()
        assert runner._agent_for(story) == "aider"

    def test_pr_launch_rereads_the_review_agent(self):
        """Both PR runners resolve through pr_agent(), re-read at launch."""
        import inspect

        from backend.ticket_ingestion import orchestrator, session_runner

        pr_src = inspect.getsource(session_runner.SessionRunner._create_pr_instance)
        assert "fresh_agent" in pr_src
        assert "c.pr_agent()" in pr_src
        # The engine-off fallback runner used the ingestion-wide agent, so the
        # two runners disagreed about which CLI reviews a PR.
        init_src = inspect.getsource(orchestrator.PipelineOrchestrator.__init__)
        assert "config.pr_agent()" in init_src
        assert "config.agent_for()" not in init_src

    def test_issue_force_start_uses_the_issue_agent_not_the_pipeline_one(self):
        """Settings → Git issues → Agent CLI governed only the auto monitor:
        "Start work" read agent_for(), which skips past github.issue_agent."""
        import inspect

        from backend.web.core import issue_start

        src = inspect.getsource(issue_start.prepare_start)
        # Comments explain the old behaviour by name, so compare CODE only.
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        assert 'getattr(cfg, "issue_agent"' in code
        assert "agent_for(" not in code

    def test_every_surface_resolves_through_its_own_chain(self):
        """One config, three surfaces, three answers — no cross-contamination."""
        cfg = _pipeline(github_agent="codex", issue_agent="aider", engine_agent="goose")
        assert (cfg.pr_agent(), cfg.issue_agent(), cfg.agent_for("")) == (
            "codex",
            "aider",
            "goose",
        )


class TestSettingsRoundTrip:
    """The new fields have to survive a save/load cycle or the pickers silently
    forget what you chose."""

    def test_github_agents(self):
        from backend.config.settings import GithubSettings

        gh = GithubSettings(agent="codex", issue_agent="aider")
        again = GithubSettings.from_dict(gh.to_dict())
        assert (again.agent, again.issue_agent) == ("codex", "aider")

    def test_github_agents_are_omitted_when_unset(self):
        """Blank must not be written, so an unset picker keeps falling through
        rather than pinning an empty provider name into settings.json."""
        from backend.config.settings import GithubSettings

        assert "agent" not in GithubSettings().to_dict()
        assert "issue_agent" not in GithubSettings().to_dict()

    def test_assistant_provider(self):
        from backend.config.settings import CodingCliSettings

        cc = CodingCliSettings(assistant_provider="goose")
        assert CodingCliSettings.from_dict(cc.to_dict()).assistant_provider == "goose"

    def test_assistant_provider_omitted_when_unset(self):
        from backend.config.settings import CodingCliSettings

        assert "assistant_provider" not in CodingCliSettings().to_dict()
