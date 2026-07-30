"""Tests for :mod:`backend.session.git.remote_url`.

This parser is load-bearing for the whole gh-optional / SSH-first path: it is
what lets MindFlock recognise the repo behind a remote without ``gh``, decide
that an SSH checkout and an HTTPS ``[repository].url`` are the same repo, and
build the compare URL handed to the user when it cannot open a PR itself. Pure
string work — nothing here spawns a process or touches the network.
"""

from __future__ import annotations

import pytest

from backend.session.git.remote_url import (
    branch_url,
    compare_url,
    is_local_path,
    parse_remote,
    pr_list_url,
    same_repo,
    to_https,
    to_ssh,
)


# ---------------------------------------------------------------------------
# parse_remote — every spelling git itself accepts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Org/repo.git",
        "https://github.com/Org/repo",
        "http://github.com/Org/repo.git",
        "git@github.com:Org/repo.git",
        "git@github.com:Org/repo",
        "ssh://git@github.com/Org/repo.git",
        "ssh://git@github.com:22/Org/repo.git",
        "git://github.com/Org/repo.git",
        "https://user:token@github.com/Org/repo.git",
    ],
)
def test_every_spelling_resolves_to_the_same_repo(url: str) -> None:
    ref = parse_remote(url)
    assert ref is not None, url
    assert (ref.host, ref.owner, ref.repo) == ("github.com", "Org", "repo")
    assert ref.slug == "Org/repo"
    assert ref.web_url == "https://github.com/Org/repo"


def test_host_is_lowercased_but_owner_and_repo_keep_their_case() -> None:
    # GitHub URLs are case-insensitive for the host only; the owner/repo case
    # is what the API echoes back, so preserve it.
    ref = parse_remote("https://GITHUB.com/Org/RepoName.git")
    assert ref is not None
    assert ref.host == "github.com"
    assert (ref.owner, ref.repo) == ("Org", "RepoName")


def test_non_github_forge_parses_too() -> None:
    ref = parse_remote("git@gitlab.example.com:team/service.git")
    assert ref is not None
    assert (ref.host, ref.owner, ref.repo) == ("gitlab.example.com", "team", "service")


@pytest.mark.parametrize(
    "url",
    [
        "/home/me/app",
        "./app",
        "../app",
        "~/src/app",
        "file:///home/me/app",
        "C:\\repo\\app",
        "C:/repo/app",
    ],
)
def test_local_paths_are_recognised_and_never_parsed_as_a_forge(url: str) -> None:
    # MindFlock legitimately clones from local paths, so these are a routine
    # case, not an error — they simply have no forge behind them.
    assert is_local_path(url) is True
    assert parse_remote(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "git@github.com:repo.git",  # no owner segment
        "https://github.com/Org",  # no repo segment
        "https:///Org/repo.git",  # no host
        "not a url",
    ],
)
def test_unusable_remotes_return_none_rather_than_guessing(url: str) -> None:
    assert parse_remote(url) is None


def test_a_numeric_first_segment_is_not_mistaken_for_an_ssh_port() -> None:
    # `host:22/Org/repo` is a schemeless ssh URL; `host:22/repo` would leave
    # only one segment. Guard the disambiguation explicitly.
    ref = parse_remote("git@github.com:22/Org/repo.git")
    assert ref is not None
    assert (ref.owner, ref.repo) == ("Org", "repo")


# ---------------------------------------------------------------------------
# same_repo — the transport-independent comparison
# ---------------------------------------------------------------------------
def test_ssh_and_https_spellings_of_one_repo_compare_equal() -> None:
    # This is the bug that silently dropped a user's configured base branch:
    # an SSH checkout against an HTTPS [repository].url.
    assert same_repo("git@github.com:Org/app.git", "https://github.com/Org/app")


def test_comparison_ignores_owner_and_repo_case() -> None:
    assert same_repo("git@github.com:org/APP.git", "https://github.com/Org/app")


def test_different_repos_and_hosts_do_not_compare_equal() -> None:
    assert not same_repo("git@github.com:Org/app.git", "git@github.com:Org/other.git")
    assert not same_repo("git@gitlab.com:Org/app.git", "https://github.com/Org/app")


def test_same_repo_is_false_for_local_paths() -> None:
    # Documented behaviour callers depend on: a local path has no forge
    # identity, so a caller that ALSO needs local-path equality must keep its
    # own literal compare on top. `provisioned._same_repo_url` is that caller,
    # and tests/unit/test_provision_generalization.py pins the fallback — if
    # this assertion ever flips, go re-read those tests before "fixing" it.
    assert not same_repo("/home/me/app", "/home/me/app")


# ---------------------------------------------------------------------------
# URL builders — what the user is handed when gh is absent
# ---------------------------------------------------------------------------
def test_branch_url_from_an_ssh_remote_is_a_browsable_https_url() -> None:
    assert (
        branch_url("git@github.com:Org/app.git", "feature-x")
        == "https://github.com/Org/app/tree/feature-x"
    )


def test_compare_url_is_the_prefilled_open_a_pr_page() -> None:
    assert (
        compare_url("git@github.com:Org/app.git", "main", "feature-x")
        == "https://github.com/Org/app/compare/main...feature-x?expand=1"
    )


def test_slashes_in_branch_names_survive_but_other_specials_are_escaped() -> None:
    # `feat/thing` must stay a path, while a `#` would otherwise truncate the
    # URL at the fragment.
    assert branch_url("https://github.com/Org/app", "feat/thing").endswith(
        "/tree/feat/thing"
    )
    assert "%23" in branch_url("https://github.com/Org/app", "fix#42")


def test_pr_list_url_filters_by_head_branch() -> None:
    url = pr_list_url("https://github.com/Org/app", "feature-x")
    assert url is not None
    assert url.startswith("https://github.com/Org/app/pulls?q=")
    assert "head%3Afeature-x" in url


@pytest.mark.parametrize("builder", [branch_url, pr_list_url])
def test_builders_return_none_without_a_forge_or_a_branch(builder) -> None:
    assert builder("/home/me/app", "feature-x") is None
    assert builder("https://github.com/Org/app", "") is None


def test_compare_url_returns_none_when_either_end_is_missing() -> None:
    assert compare_url("/home/me/app", "main", "feature-x") is None
    assert compare_url("https://github.com/Org/app", "", "feature-x") is None
    assert compare_url("https://github.com/Org/app", "main", "") is None


# ---------------------------------------------------------------------------
# to_ssh / to_https — only used to SYNTHESIZE a URL, never to rewrite one
# ---------------------------------------------------------------------------
def test_transport_respelling_round_trips() -> None:
    ssh = "git@github.com:Org/app.git"
    https = "https://github.com/Org/app.git"
    assert to_ssh(https) == ssh
    assert to_https(ssh) == https
    assert to_ssh(ssh) == ssh
    assert to_https(https) == https


def test_respelling_a_local_path_is_refused() -> None:
    assert to_ssh("/home/me/app") is None
    assert to_https("/home/me/app") is None
