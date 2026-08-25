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


def test_github_issues_needs_no_fields_at_all(tmp_path):
    """The zero-config on-ramp: GitHub Issues must load with nothing configured.

    Its token comes from the shared GitHub auth chain and its repo from the
    source's repo_url / [repository].url / this checkout's origin, so requiring
    ``project`` would reject exactly the config the feature exists to allow."""
    cfg = load_config(
        _write(
            tmp_path,
            """
[ticketing]
provider = "github_issues"
""" + COMMON,
        )
    )
    assert cfg.ticketing is not None
    assert cfg.ticketing.provider == "github_issues"
    assert cfg.ticketing.project == ""


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


def test_settings_roundtrip_assignee_scope():
    s = Settings.from_dict(
        {
            "ticketing": {
                "sources": [
                    {
                        "provider": "shortcut",
                        "api_token": "t",
                        "workflow_state": "100",
                        "assignee_scope": "anyone",
                    }
                ]
            }
        }
    )
    assert s.ticketing.sources[0].assignee_scope == "anyone"
    assert s.to_dict()["ticketing"]["sources"][0]["assignee_scope"] == "anyone"
    # The default is blank, and blank fields are dropped — an existing config
    # round-trips byte-identical.
    plain = Settings.from_dict(
        {"ticketing": {"sources": [{"provider": "asana", "api_token": "t"}]}}
    )
    assert "assignee_scope" not in plain.to_dict()["ticketing"]["sources"][0]


# --------------------------------------------------------------------------- #
# Assignee scope validation: "anyone" is only meaningful with something else
# bounding the search.
# --------------------------------------------------------------------------- #
def test_anyone_without_a_state_filter_is_a_config_problem(tmp_path):
    with pytest.raises(ConfigError, match="workflow_state"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "shortcut"
api_token = "tok"
member_id = "m"
assignee_scope = "anyone"
""" + COMMON,
            )
        )


def test_anyone_with_a_state_filter_parses(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
[ticketing]
provider = "shortcut"
api_token = "tok"
member_id = "m"
workflow_state = "100"
assignee_scope = "anyone"
""" + COMMON,
        )
    )
    assert cfg.ticketing.assignee_scope == "anyone"


def test_anyone_on_a_provider_that_cannot_do_it_is_a_problem(tmp_path):
    with pytest.raises(ConfigError, match="not supported"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "asana"
api_token = "pat"
project = "ws1"
assignee_scope = "anyone"
""" + COMMON,
            )
        )


def test_unknown_assignee_scope_is_a_problem(tmp_path):
    with pytest.raises(ConfigError, match="assignee_scope"):
        load_config(
            _write(
                tmp_path,
                """
[ticketing]
provider = "shortcut"
api_token = "tok"
member_id = "m"
assignee_scope = "everyone"
""" + COMMON,
            )
        )


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


# --------------------------------------------------------------------------- #
# Per-source thinking effort
#
# The third member of the per-source launch family (agent, depth, effort): how
# hard the agent thinks about tickets from THIS queue. A backlog of one-line copy
# fixes and a queue of schema migrations deserve different answers, and neither
# deserves to be set per ticket forever.
# --------------------------------------------------------------------------- #
def test_a_sources_effort_is_normalized_and_junk_is_refused():
    from backend.ticket_ingestion.config import _validate_effort

    problems: list = []
    assert _validate_effort("XHigh", "s.effort", problems) == "xhigh"
    assert _validate_effort("", "s.effort", problems) == ""
    assert problems == []

    # An unknown rung is a config the user should be TOLD about — running it at
    # the CLI's default in silence is how somebody concludes it does nothing.
    assert _validate_effort("turbo", "s.effort", problems) == ""
    assert problems and "must be one of" in problems[0]


def test_effort_for_reads_the_source_and_never_an_app_wide_default():
    from backend.ticket_ingestion.config import PipelineConfig, TicketProviderConfig

    cfg = PipelineConfig(
        ticketing_sources=[
            TicketProviderConfig(provider="shortcut", id="sc", effort="xhigh"),
            TicketProviderConfig(provider="jira", id="jira"),
        ]
    )
    assert cfg.effort_for("sc") == "xhigh"
    # No `[mindflock].effort` rung exists on purpose: "how hard to think" is a
    # property of the work, and a flock-wide default would re-price every queue.
    assert cfg.effort_for("jira") == ""
    assert cfg.effort_for("nope") == ""


def test_the_settings_source_round_trips_its_effort():
    from backend.config.settings import TicketingSource

    src = TicketingSource.from_dict(
        {"provider": "shortcut", "agent": "claude", "effort": "XHigh"}
    )
    assert src.effort == "xhigh"
    assert src.to_dict()["effort"] == "xhigh"
    # Coerced rather than stored verbatim: this is read on every settings load,
    # and a hand-edited rung no provider knows would reach a CLI that rejects it.
    assert TicketingSource.from_dict({"effort": "turbo"}).effort == ""
    # Blank never appears in the stored document — an unset field means inherit.
    assert "effort" not in TicketingSource.from_dict({"provider": "jira"}).to_dict()
