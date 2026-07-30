"""Unit tests for configuration loading."""

from pathlib import Path

import pytest

from backend.ticket_ingestion.config import ConfigError, PipelineConfig, load_config


@pytest.fixture
def valid_config_toml(tmp_path: Path) -> Path:
    """Create a valid config.toml file for testing."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"
api_token = "sc_test_token_123"
member_id = "56d8a839-abcd-1234-efgh-567890abcdef"

[repository]
url = "git@github.com:org/example-bot.git"
workspace_dir = "./workspaces"

[validation]
min_description_length = 20

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
""")
    return config_file


class TestLoadValidConfig:
    """Tests for loading a valid configuration file."""

    def test_loads_all_fields(self, valid_config_toml: Path) -> None:
        config = load_config(valid_config_toml)

        assert config.ticketing.provider == "shortcut"
        assert config.ticketing.api_token == "sc_test_token_123"
        assert config.ticketing.member_id == "56d8a839-abcd-1234-efgh-567890abcdef"
        assert config.repo_url == "git@github.com:org/example-bot.git"
        assert config.workspace_dir == Path("./workspaces")
        assert config.min_description_length == 20
        assert config.log_file == Path("./logs/pipeline.log")
        assert config.log_level == "INFO"

    def test_returns_pipeline_config_instance(self, valid_config_toml: Path) -> None:
        config = load_config(valid_config_toml)
        assert isinstance(config, PipelineConfig)


class TestMissingConfigFile:
    """Tests for missing configuration file."""

    def test_raises_config_error_for_missing_file(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.toml"
        with pytest.raises(ConfigError, match="Configuration file not found"):
            load_config(missing_path)

    def test_error_message_includes_path(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.toml"
        with pytest.raises(ConfigError, match="nonexistent.toml"):
            load_config(missing_path)


class TestInvalidToml:
    """Tests for invalid TOML syntax."""

    def test_raises_config_error_for_invalid_toml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("this is not [valid toml = ")
        with pytest.raises(ConfigError, match="Invalid TOML"):
            load_config(config_file)


class TestMissingFields:
    """Tests for missing required fields."""

    def test_missing_ticketing_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[repository]
url = "git@github.com:org/repo.git"
workspace_dir = "./workspaces"

[validation]
min_description_length = 20

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
""")
        with pytest.raises(ConfigError, match="\\[ticketing\\] section"):
            load_config(config_file)

    def test_missing_api_token(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"
member_id = "abc-123"

[repository]
url = "git@github.com:org/repo.git"
workspace_dir = "./workspaces"

[validation]
min_description_length = 20

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
""")
        with pytest.raises(ConfigError, match="api_token"):
            load_config(config_file)

    def test_missing_multiple_fields_reports_all(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"

[validation]
min_description_length = 20

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
""")
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_file)
        error_msg = str(exc_info.value)
        assert "api_token" in error_msg
        assert "member_id" in error_msg
        assert "repository.url" in error_msg


class TestInvalidTypes:
    """Tests for fields with invalid types."""

    def test_poll_interval_not_positive_integer(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"
api_token = "sc_test"
member_id = "abc-123"
poll_interval_seconds = -1

[repository]
url = "git@github.com:org/repo.git"
workspace_dir = "./workspaces"

[validation]
min_description_length = 20

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
""")
        with pytest.raises(ConfigError, match="poll_interval_seconds"):
            load_config(config_file)

    def test_min_description_length_not_integer(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"
api_token = "sc_test"
member_id = "abc-123"

[repository]
url = "git@github.com:org/repo.git"
workspace_dir = "./workspaces"

[validation]
min_description_length = "twenty"

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
""")
        with pytest.raises(ConfigError, match="must be an integer"):
            load_config(config_file)


class TestEngineDefault:
    """The flagship launch path (engine mode) must be on for a fresh install.

    A ``config.toml`` with no ``[mindflock]`` section — the shape produced by
    configuring everything from the Settings UI — used to parse to
    ``engine=None``, which the orchestrator reads as "standalone launcher": OS
    terminal tabs instead of MindFlock sessions.
    """

    def test_engine_enabled_without_a_mindflock_section(
        self, valid_config_toml: Path
    ) -> None:
        config = load_config(valid_config_toml)

        assert config.engine is not None
        assert config.engine.enabled is True
        assert config.engine.mode == "worktree"

    def test_mode_only_section_still_leaves_engine_enabled(
        self, tmp_path: Path
    ) -> None:
        """A section written by the Settings UI's mode dropdown alone (no
        ``enabled`` key) must not read as disabled."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"
api_token = "sc_test"
member_id = "abc-123"

[repository]
url = "git@github.com:org/repo.git"

[mindflock]
mode = "clone"
""")
        config = load_config(config_file)

        assert config.engine is not None
        assert config.engine.enabled is True
        assert config.engine.mode == "clone"

    def test_explicit_false_is_still_honoured(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("""\
[[ticketing.source]]
provider = "shortcut"
api_token = "sc_test"
member_id = "abc-123"

[repository]
url = "git@github.com:org/repo.git"

[mindflock]
enabled = false
""")
        config = load_config(config_file)

        assert config.engine is not None
        assert config.engine.enabled is False
