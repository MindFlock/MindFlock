"""Tests for the generic [ticketing] config schema."""

from pathlib import Path

import pytest

from backend.config.settings import Settings
from backend.ticket_ingestion.config import (
    ConfigError,
    PipelineConfig,
    TicketProviderConfig,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


COMMON = """
[repository]
url = "git@github.com:org/repo.git"
workspace_dir = "./workspaces"

[logging]
log_file = "./logs/pipeline.log"
log_level = "INFO"
"""


def test_generic_jira_config_parses(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
[ticketing]
provider = "jira"
base_url = "https://acme.atlassian.net"
email = "me@acme.com"
api_token = "tok"
""" + COMMON,
        )
    )
    assert cfg.ticketing.provider == "jira"
    assert cfg.ticketing.base_url == "https://acme.atlassian.net"
    assert cfg.ticketing.email == "me@acme.com"
    assert cfg.repo_url == "git@github.com:org/repo.git"


def test_generic_linear_config_parses(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
[ticketing]
provider = "linear"
api_token = "lin_key"
""" + COMMON,
        )
    )
    assert cfg.ticketing.provider == "linear"


def test_jira_missing_required_fields_errors(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "jira"
api_token = "tok"
""" + COMMON,
            )
        )
    msg = str(e.value)
    assert "ticketing.email" in msg and "ticketing.base_url" in msg


def test_github_issues_requires_project(tmp_path):
    with pytest.raises(ConfigError, match="ticketing.project"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "github_issues"
""" + COMMON,
            )
        )


def test_unknown_provider_errors(tmp_path):
    with pytest.raises(ConfigError, match="provider must be one of"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "trello"
""" + COMMON,
            )
        )


def test_ticketing_base_url_trailing_slash_stripped(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
[ticketing]
provider = "jira"
base_url = "https://acme.atlassian.net/"
email = "me@acme.com"
api_token = "tok"
""" + COMMON,
        )
    )
    assert cfg.ticketing.base_url == "https://acme.atlassian.net"


def test_pipeline_config_primary_source_alias():
    cfg = PipelineConfig(
        repo_url="r",
        workspace_dir=Path("."),
        min_description_length=20,
        log_file=Path("l"),
        log_level="INFO",
        ticketing=TicketProviderConfig(provider="linear", api_token="k", member_id="u"),
    )
    # ``ticketing`` is the primary (first) source.
    assert cfg.ticketing is cfg.ticketing_sources[0]
    assert cfg.ticketing.api_token == "k"


# --------------------------------------------------------------------------- #
# Settings-store ticketing sources.
# --------------------------------------------------------------------------- #
def test_settings_roundtrip_ticketing():
    s = Settings.from_dict(
        {
            "ticketing": {
                "sources": [
                    {"provider": "asana", "api_token": "t", "project": "ws"},
                ]
            }
        }
    )
    d = s.to_dict()
    assert d["ticketing"] == {
        "sources": [{"provider": "asana", "api_token": "t", "project": "ws"}]
    }
    s2 = Settings.from_dict(d)
    assert s2.ticketing.sources[0].provider == "asana"
    assert s2.to_dict()["ticketing"] == d["ticketing"]


def test_settings_multiple_same_provider_sources():
    s = Settings.from_dict(
        {
            "ticketing": {
                "sources": [
                    {
                        "id": "jira",
                        "provider": "jira",
                        "api_token": "a",
                        "base_url": "https://a",
                        "email": "a@a",
                    },
                    {
                        "id": "jira-2",
                        "provider": "jira",
                        "api_token": "b",
                        "base_url": "https://b",
                        "email": "b@b",
                    },
                ]
            }
        }
    )
    assert [x.id for x in s.ticketing.sources] == ["jira", "jira-2"]


def test_orchestrator_builds_one_scanner_per_source():
    from backend.ticket_ingestion.orchestrator import PipelineOrchestrator

    cfg = PipelineConfig(
        repo_url="r",
        workspace_dir=Path("."),
        min_description_length=20,
        log_file=Path("l"),
        log_level="INFO",
        ticketing_sources=[
            TicketProviderConfig(provider="shortcut", api_token="a", member_id="m1"),
            TicketProviderConfig(
                provider="jira",
                api_token="b",
                base_url="https://x",
                email="e",
                member_id="m2",
            ),
        ],
    )
    orch = PipelineOrchestrator(cfg)
    assert len(orch._scanners) == 2
    # Multiple sources => each scanner gets its own keyed poll checkpoint.
    assert [s._source_key for s in orch._scanners] == ["sc", "jira"]
    # Union assignee filter accepts a ticket assigned to any configured identity.
    assert orch._assignee_filter._member_ids == {"m1", "m2"}


def test_orchestrator_single_source_uses_keyed_checkpoint():
    from backend.ticket_ingestion.orchestrator import PipelineOrchestrator

    cfg = PipelineConfig(
        repo_url="r",
        workspace_dir=Path("."),
        min_description_length=20,
        log_file=Path("l"),
        log_level="INFO",
        ticketing=TicketProviderConfig(
            provider="shortcut", api_token="a", member_id="m1"
        ),
    )
    orch = PipelineOrchestrator(cfg)
    assert len(orch._scanners) == 1
    assert orch._scanners[0]._source_key == "sc"


def test_multi_source_config_distinct_slugs(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
[[ticketing.source]]
provider = "jira"
base_url = "https://a.atlassian.net"
email = "me@a.com"
api_token = "t1"

[[ticketing.source]]
provider = "jira"
base_url = "https://b.atlassian.net"
email = "me@b.com"
api_token = "t2"
""" + COMMON,
        )
    )
    from backend.ticket_ingestion.providers import get_provider

    assert len(cfg.ticketing_sources) == 2
    slugs = {get_provider(s).make_slug("PROJ-1") for s in cfg.ticketing_sources}
    assert slugs == {"jira-PROJ-1", "jira-2-PROJ-1"}
