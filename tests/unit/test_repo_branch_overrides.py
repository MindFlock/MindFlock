"""Per-repo base / PR base branches (``repository.repo_branches``).

The flat ``repository.base_branch`` / ``pr_base_branch`` pair is the default
for every repo; a repo block overrides both where sessions fork from and where
their PRs point. Pinned here: the coercer, the resolver (a block's blank PR
base is ITS base, never the flat PR base), the fork base for ticket and
local-repo provisioning, the Make-PR target, and the multi-repo ticket prompt.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.config import settings as S
from backend.session import provisioned


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    monkeypatch.delenv("MINDFLOCK_BASE_BRANCH", raising=False)
    monkeypatch.delenv("MINDFLOCK_REPO_URL", raising=False)


def _flock(**repo):
    S.update_settings(
        repository={
            "url": "git@github.com:acme/sitecheck.git",
            "workspace_dir": "./ws",
            "base_branch": "staging",
            "pr_base_branch": "staging",
            **repo,
        }
    )


def test_coercer_drops_blanks_unknown_keys_and_empty_blocks():
    got = S._repo_branches(
        {
            "acme/beacon": {"base_branch": " main ", "pr_base_branch": "", "x": "y"},
            "acme/empty": {"base_branch": ""},
            "": {"base_branch": "main"},
            "acme/bad": "main",
        }
    )
    assert got == {"acme/beacon": {"base_branch": "main"}}
    assert S._repo_branches(["acme/x"]) == {}


def test_round_trip_and_blank_map_is_not_written():
    _flock(repo_branches={"acme/beacon": {"base_branch": "main"}})
    st = S.load_settings()
    assert st.repository.repo_branches == {"acme/beacon": {"base_branch": "main"}}
    assert "repo_branches" not in S.RepositorySettings().to_dict()


def test_resolver_block_base_answers_for_its_pr_base_too():
    _flock(
        repo_branches={
            "Acme/Beacon": {"base_branch": "main"},
            "acme/drift": {"base_branch": "main", "pr_base_branch": "release"},
        }
    )
    # Case-insensitive, and the flat pr_base_branch ("staging") is NOT inherited.
    assert S.repo_branches_for("acme/beacon") == ("main", "main")
    assert S.repo_branches_for("acme/drift") == ("main", "release")
    assert S.repo_branches_for("acme/sitecheck") == ("", "")
    assert S.repo_branches_for("") == ("", "")


def test_repo_slug_for_urls_and_local_checkouts(tmp_path):
    assert provisioned.repo_slug_for("git@github.com:acme/beacon.git") == "acme/beacon"
    assert provisioned.repo_slug_for("https://github.com/acme/beacon") == "acme/beacon"
    repo = tmp_path / "beacon"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:acme/beacon.git",
        ],
        check=True,
    )
    assert provisioned.repo_slug_for(str(repo)) == "acme/beacon"
    assert provisioned.repo_slug_for(str(tmp_path / "missing")) == ""
    assert provisioned.repo_slug_for("") == ""


def test_ticket_provisioning_forks_from_the_repos_own_base():
    _flock(repo_branches={"acme/beacon": {"base_branch": "main"}})
    beacon = provisioned.load_provision_settings(
        repo_url_override="git@github.com:acme/beacon.git"
    )
    other = provisioned.load_provision_settings(
        repo_url_override="git@github.com:acme/sitecheck.git"
    )
    assert beacon is not None and beacon.base_branch == "main"
    assert other is not None and other.base_branch == "staging"


def test_ticket_provisioning_default_is_main_not_the_repos_default_branch():
    """With nothing configured the fork base is the literal "main" — no
    lookup of the repo's GitHub default branch happens on this path."""
    S.update_settings(
        repository={"url": "git@github.com:acme/x.git", "workspace_dir": "./ws"}
    )
    s = provisioned.load_provision_settings()
    assert s is not None and s.base_branch == "main"


def test_local_repo_provisioning_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioned, "default_workspace_dir", lambda: tmp_path / "ws")
    repo = tmp_path / "beacon"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "dev", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/acme/beacon",
        ],
        check=True,
    )
    assert provisioned.local_settings_for(repo).base_branch == "dev"
    _flock(repo_branches={"acme/beacon": {"base_branch": "main"}})
    assert provisioned.local_settings_for(repo).base_branch == "main"


def test_configured_pr_base_prefers_the_repo_block(monkeypatch):
    from backend.web import server

    _flock(repo_branches={"acme/beacon": {"base_branch": "main"}})
    slugs = {"/wt/beacon": "acme/beacon", "/wt/site": "acme/sitecheck"}
    monkeypatch.setattr(provisioned, "repo_slug_for", lambda wt: slugs.get(wt, ""))
    assert server._configured_pr_base("/wt/beacon") == "main"
    assert server._configured_pr_base("/wt/site") == "staging"
    assert server._configured_pr_base() == "staging"


def test_multi_repo_prompt_uses_each_extra_repos_branches(tmp_path):
    from backend.ticket_ingestion import repo_routing as R
    from tests._factories import make_ticket

    _flock(repo_branches={"acme/beacon": {"base_branch": "main"}})
    story = make_ticket(
        slug="sc-7", name="Fix", repo_url="git@github.com:acme/sitecheck.git"
    )
    story.route = {
        "reason": "",
        "method": "llm",
        "extra": [
            {
                "name": "beacon",
                "url": "git@github.com:acme/beacon.git",
                "path": "/code/beacon",
            },
            {"name": "api", "url": "git@github.com:acme/api.git", "path": ""},
        ],
        "others": [],
    }
    text = R.other_repos_section(story, "feature/sc-7/fix")
    beacon, api = text.split("### beacon")[1].split("### api")
    assert "-b feature/sc-7/fix origin/main`" in beacon
    assert "into `main` (`--base main`" in beacon
    # No block: the flock-wide pair.
    assert "from `staging`." in api
    assert "into `staging` (`--base staging`" in api


def test_multi_repo_prompt_unconfigured_keeps_origin_head():
    from backend.ticket_ingestion import repo_routing as R
    from tests._factories import make_ticket

    story = make_ticket(
        slug="sc-8", name="Fix", repo_url="git@github.com:acme/sitecheck.git"
    )
    story.route = {
        "reason": "",
        "method": "llm",
        "extra": [
            {"name": "beacon", "url": "git@github.com:acme/beacon.git", "path": "/c/b"}
        ],
        "others": [],
    }
    text = R.other_repos_section(story, "feature/sc-8/fix")
    assert "-b feature/sc-8/fix origin/HEAD`" in text
    assert "3. Open a pull request for it in that repository.\n" in text
