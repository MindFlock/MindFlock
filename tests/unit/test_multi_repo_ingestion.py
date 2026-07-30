"""Per-source repo for ticket ingestion: each ticketing source can target its
own repo, so ingestion spans many repos instead of one global one. Covers the
settings round-trip, config layering, the engine repo-URL override, the clone
transport (SSH is as first-class as HTTPS), and the source-card UI wiring.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion.config import PipelineConfig, load_config
from backend.session import provisioned
from backend.web import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    """Delegate to the shared settings-store isolation (tests/conftest.py).

    Also drops the transport env overrides so a developer shell can't steer the
    clone-URL assertions below."""
    monkeypatch.delenv("MINDFLOCK_REPO_URL", raising=False)
    monkeypatch.delenv("MINDFLOCK_GIT_TRANSPORT", raising=False)


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #
def test_source_repo_url_roundtrips():
    src = S.TicketingSource(provider="shortcut", repo_url="git@github.com:org/api.git")
    back = S.TicketingSource.from_dict(src.to_dict())
    assert back.repo_url == "git@github.com:org/api.git"


def test_source_repo_url_omitted_when_empty():
    assert "repo_url" not in S.TicketingSource(provider="shortcut").to_dict()


# --------------------------------------------------------------------------- #
# Config layering: a source's repo_url must reach the pipeline config
# --------------------------------------------------------------------------- #
def test_source_repo_url_flows_into_config():
    S.set_ticketing_sources(
        [
            {
                "provider": "shortcut",
                "api_token": "t1",
                "member_id": "m1",
                "repo_url": "git@github.com:org/api.git",
            },
            {
                "provider": "shortcut",
                "api_token": "t2",
                "member_id": "m2",
                "repo_url": "git@github.com:org/web.git",
            },
        ]
    )
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    cfg = load_config()
    repos = {s.repo_url for s in cfg.ticketing_sources}
    assert "git@github.com:org/api.git" in repos
    assert "git@github.com:org/web.git" in repos


def test_source_without_repo_url_falls_back_to_default_at_provision():
    # A source with no repo_url leaves it "" — downstream (provisioner / engine)
    # substitutes the global repository.url.
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    S.set_ticketing_sources(
        [
            {"provider": "shortcut", "api_token": "t", "member_id": "m"},
        ]
    )
    cfg = load_config()
    assert cfg.ticketing_sources[0].repo_url == ""


# --------------------------------------------------------------------------- #
# Engine path: an explicit repo-URL override wins over the configured repo
# --------------------------------------------------------------------------- #
def test_provision_settings_repo_url_override_wins():
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    s = provisioned.load_provision_settings(
        repo_url_override="git@github.com:org/api.git"
    )
    assert s is not None
    assert s.repo_url == "git@github.com:org/api.git"


def test_provision_settings_no_override_uses_configured_repo():
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    s = provisioned.load_provision_settings()
    assert s is not None
    assert s.repo_url == "git@github.com:org/default.git"


# --------------------------------------------------------------------------- #
# Clone transport: SSH is as first-class as HTTPS, and a configured URL is
# cloned VERBATIM
# --------------------------------------------------------------------------- #
def test_git_transport_defaults_to_auto():
    from backend.ticket_ingestion.clone_transport import resolve_transport

    assert resolve_transport() == "auto"


def test_git_transport_setting_reaches_the_clone_path():
    from backend.ticket_ingestion.clone_transport import resolve_transport

    S.update_settings(repository={"url": "u", "git_transport": "ssh"})
    assert resolve_transport() == "ssh"


def test_invalid_git_transport_setting_degrades_to_auto():
    from backend.ticket_ingestion.clone_transport import resolve_transport

    S.update_settings(repository={"url": "u", "git_transport": "carrier-pigeon"})
    assert resolve_transport() == "auto"


def test_env_git_transport_beats_the_settings_store(monkeypatch):
    from backend.ticket_ingestion.clone_transport import resolve_transport

    S.update_settings(repository={"url": "u", "git_transport": "https"})
    monkeypatch.setenv("MINDFLOCK_GIT_TRANSPORT", "ssh")
    assert resolve_transport() == "ssh"


class _FakeClone:
    """Stand-in for clone_transport.run_network_git that materializes the tmp
    clone dir (as git would) so the provisioner's rename-into-place succeeds."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, cwd=None, timeout=None, env=None):
        self.calls.append(args)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return 0, b"", b""


async def _clone_argv_for(monkeypatch, tmp_path, story_repo_url, config_repo_url):
    from backend.ticket_ingestion import provisioner as _prov
    from backend.ticket_ingestion.models import Ticket

    fake = _FakeClone()
    monkeypatch.setattr(_prov, "run_network_git", fake)
    cfg = PipelineConfig(repo_url=config_repo_url, workspace_dir=tmp_path)
    story = Ticket(
        id=1,
        name="t",
        description="d",
        acceptance_criteria=[],
        owner_ids=[],
        app_url="",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        repo_url=story_repo_url,
    )
    await _prov.EnvironmentProvisioner(cfg)._git_clone(story, tmp_path / "ws")
    return fake.calls[0]


async def test_source_ssh_repo_url_is_cloned_verbatim(tmp_path, monkeypatch):
    """The contributor's setup: an SSH remote must survive to the git argv."""
    argv = await _clone_argv_for(
        monkeypatch, tmp_path, "git@github.com:org/api.git", "https://github.com/org/x"
    )
    assert argv[3] == "git@github.com:org/api.git"


async def test_global_repo_url_used_when_the_source_has_none(tmp_path, monkeypatch):
    argv = await _clone_argv_for(
        monkeypatch, tmp_path, "", "git@github.com:org/default.git"
    )
    assert argv[3] == "git@github.com:org/default.git"


async def test_explicit_https_transport_respells_an_ssh_url(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_GIT_TRANSPORT", "https")
    argv = await _clone_argv_for(
        monkeypatch, tmp_path, "git@github.com:org/api.git", ""
    )
    assert argv[3] == "https://github.com/org/api.git"


async def test_local_path_repo_url_is_never_respelled(tmp_path, monkeypatch):
    # A local clone source has no forge and no transport to pick.
    monkeypatch.setenv("MINDFLOCK_GIT_TRANSPORT", "ssh")
    argv = await _clone_argv_for(monkeypatch, tmp_path, str(tmp_path / "src"), "")
    assert argv[3] == str(tmp_path / "src")


def test_headless_clone_env_disables_prompts():
    from backend.ticket_ingestion.clone_transport import headless_git_env

    env = headless_git_env({})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def test_headless_clone_env_keeps_a_user_ssh_command():
    # The user's own GIT_SSH_COMMAND (a specific key, a jump host) must win.
    from backend.ticket_ingestion.clone_transport import headless_git_env

    env = headless_git_env({"GIT_SSH_COMMAND": "ssh -i ~/.ssh/work"})
    assert env["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/work"


def test_clone_url_is_empty_when_there_is_nothing_to_go_on():
    from backend.ticket_ingestion.clone_transport import resolve_clone_url

    assert resolve_clone_url("") == ""


# --------------------------------------------------------------------------- #
# Frontend: each ticketing source card exposes a Repo URL field
# --------------------------------------------------------------------------- #
def test_source_card_js_has_repo_field():
    js = client.get("/app.js").text
    assert '"repo_url"' in js
    # saveAll() persists any [data-tk-field], and cards render for the sources.
    # Repo URL is now required per source (there is no global default repo).
    assert "Repo URL" in js


def test_source_card_is_collapsible():
    js = client.get("/app.js").text
    # Collapsible header + body wired, and saved sources fold by default.
    assert "setCollapsed" in js
    assert "tk-head" in js and "tk-body" in js
    css = client.get("/style.css").text
    body = css[css.index(".tk-source.tk-collapsed .tk-body") :]
    assert "display: none" in body[: body.index("}")]
