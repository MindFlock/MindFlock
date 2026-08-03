"""GitHub Issues as the zero-config on-ramp.

The claim is that picking "GitHub Issues" and saving is the whole setup: the
token comes from the shared GitHub auth chain and the repository resolves itself.
These tests pin the resolution order and, importantly, that it *degrades to a
readable instruction* rather than a crash when nothing names a repo.
"""

from __future__ import annotations

import pytest

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.providers import PROVIDER_META, PROVIDER_REGISTRY
from backend.ticket_ingestion.providers.base import ProviderError
from backend.ticket_ingestion.providers.github_issues import GithubIssuesProvider


def _prov(**cfg) -> GithubIssuesProvider:
    base = dict(provider="github_issues")
    base.update(cfg)
    return GithubIssuesProvider(TicketProviderConfig(**base))


def _no_ambient_repo(monkeypatch) -> None:
    """Neutralize the machine's own git remote + saved settings.

    Without this the tests would pass or fail depending on where the suite runs
    (this repo has an origin), which is exactly the ambient dependency the
    feature introduces and therefore the one worth isolating.
    """
    monkeypatch.setattr(GithubIssuesProvider, "_origin_url", lambda self: "")
    monkeypatch.setattr(GithubIssuesProvider, "_config_repo_url", lambda self: "")


# --------------------------------------------------------------------------- #
# Catalog position: the UI defaults a new source to the first entry.
# --------------------------------------------------------------------------- #
def test_github_issues_leads_the_catalog():
    assert PROVIDER_META[0]["id"] == "github_issues"
    assert next(iter(PROVIDER_REGISTRY)) == "github_issues"


def test_no_github_issues_field_is_required():
    entry = next(p for p in PROVIDER_META if p["id"] == "github_issues")
    assert entry["fields"], "the fields are still offered, just not mandatory"
    assert not any(f.get("required") for f in entry["fields"])
    # The repo field advertises that Test fills it in.
    project = next(f for f in entry["fields"] if f["key"] == "project")
    assert project.get("auto") is True


# --------------------------------------------------------------------------- #
# Repo resolution order.
# --------------------------------------------------------------------------- #
def test_explicit_project_wins(monkeypatch):
    monkeypatch.setattr(GithubIssuesProvider, "_origin_url", lambda self: "")
    monkeypatch.setattr(
        GithubIssuesProvider, "_config_repo_url", lambda self: "git@github.com:x/y.git"
    )
    assert _prov(project="acme/app").resolve_repo() == "acme/app"


def test_project_tolerates_stray_slashes(monkeypatch):
    _no_ambient_repo(monkeypatch)
    assert _prov(project="/acme/app/").resolve_repo() == "acme/app"


@pytest.mark.parametrize(
    "repo_url",
    [
        "git@github.com:acme/app.git",
        "https://github.com/acme/app.git",
        "https://github.com/acme/app",
        "ssh://git@github.com/acme/app.git",
    ],
)
def test_source_repo_url_resolves_for_every_transport(monkeypatch, repo_url):
    monkeypatch.setattr(GithubIssuesProvider, "_origin_url", lambda self: "")
    assert _prov(repo_url=repo_url).resolve_repo() == "acme/app"


def test_falls_back_to_the_global_repository_url(monkeypatch):
    monkeypatch.setattr(GithubIssuesProvider, "_origin_url", lambda self: "")
    monkeypatch.setattr(
        GithubIssuesProvider,
        "_config_repo_url",
        lambda self: "https://github.com/acme/global.git",
    )
    assert _prov().resolve_repo() == "acme/global"


def test_falls_back_to_the_local_origin_remote(monkeypatch):
    """The step that makes "install and save" work with nothing typed anywhere."""
    monkeypatch.setattr(GithubIssuesProvider, "_config_repo_url", lambda self: "")
    monkeypatch.setattr(
        GithubIssuesProvider,
        "_origin_url",
        lambda self: "git@github.com:acme/from-origin.git",
    )
    assert _prov().resolve_repo() == "acme/from-origin"


def test_a_local_path_remote_is_not_mistaken_for_a_repo(monkeypatch):
    """A clone of a local directory has no forge behind it, so it must fall
    through rather than yield a nonsense owner/repo."""
    monkeypatch.setattr(GithubIssuesProvider, "_config_repo_url", lambda self: "")
    monkeypatch.setattr(
        GithubIssuesProvider, "_origin_url", lambda self: "/home/me/checkouts/app"
    )
    assert _prov().resolve_repo() == ""


def test_unresolvable_repo_raises_an_actionable_error(monkeypatch):
    _no_ambient_repo(monkeypatch)
    with pytest.raises(ProviderError) as err:
        _prov()._repo()
    msg = str(err.value)
    # It must say what to do, not just that it failed.
    assert "Repo URL" in msg and "origin" in msg


def test_origin_probe_survives_a_non_git_directory(monkeypatch, tmp_path):
    """Not being in a git repo is an ordinary outcome here, never an exception."""
    monkeypatch.chdir(tmp_path)
    assert _prov()._origin_url() in ("", None) or isinstance(_prov()._origin_url(), str)
