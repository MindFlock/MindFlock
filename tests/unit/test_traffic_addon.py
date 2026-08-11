"""Traffic addon (Settings → Site traffic, dev-shell only).

Covers the two things that decide whether the screen shows numbers at all:

* which token it calls GitHub with — the shared web-layer chain, whose last
  rung (``gh auth token``) is where the token normally lives;
* that one dead upstream costs you ONE section, not the whole payload.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.web.addons import traffic
from backend.web.core import github_pr


# --------------------------------------------------------------------------- #
# Token resolution
# --------------------------------------------------------------------------- #
def test_token_comes_from_the_shared_web_github_chain(monkeypatch):
    """Not the static-only resolver: this must reach the ``gh auth token`` rung.

    Resolving only config.toml/Settings/env left the screen calling GitHub
    anonymously on a machine authenticated through the gh CLI, which is the
    normal setup — and anonymous cannot read stargazers at all.
    """
    seen = []

    async def _api_token():
        seen.append(True)
        return "  ghp_from_the_cli  "

    monkeypatch.setattr(github_pr, "api_token", _api_token)
    assert asyncio.run(traffic._token()) == "ghp_from_the_cli"
    assert seen == [True]


def test_token_is_empty_when_nothing_is_configured(monkeypatch):
    """No token is a supported state — public stars/forks/downloads need none."""

    async def _api_token():
        return ""

    monkeypatch.setattr(github_pr, "api_token", _api_token)
    assert asyncio.run(traffic._token()) == ""


def test_token_swallows_a_broken_resolver(monkeypatch):
    """A dashboard must never 500 because the token chain raised."""

    async def _api_token():
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(github_pr, "api_token", _api_token)
    assert asyncio.run(traffic._token()) == ""


# --------------------------------------------------------------------------- #
# Payload assembly
# --------------------------------------------------------------------------- #
@pytest.fixture
def stub_http(monkeypatch):
    """Route ``_get_json`` by URL fragment; unmatched URLs are a hard failure."""

    def install(routes: dict):
        async def _get_json(url, *, token="", accept=""):
            for frag, resp in routes.items():
                if frag in url:
                    return resp
            raise AssertionError("unstubbed URL: " + url)

        monkeypatch.setattr(traffic, "_get_json", _get_json)

    return install


_REPO_OK = (200, {"stargazers_count": 12, "forks_count": 3, "open_issues_count": 1})
_RELEASES_OK = (
    200,
    [
        {
            "tag_name": "v0.1.15",
            "assets": [{"name": "MindFlock.dmg", "download_count": 7}],
        }
    ],
)
_CLICKS_OK = (200, {"series": [{"day": "2026-08-10", "slug": "mac", "clicks": 3}]})


def test_star_history_401_degrades_only_that_section(monkeypatch, stub_http):
    """The exact anonymous-GitHub failure: stargazers 401s, the rest answers.

    ``GET /repos/{repo}/stargazers`` requires authentication even for a public
    repo, so an unauthenticated dashboard loses the growth curve — and must
    keep the stars, forks, downloads and clicks it already has.
    """
    stub_http(
        {
            "/stargazers": (401, {"message": "Requires authentication"}),
            "/releases": _RELEASES_OK,
            "/repos/MindFlock/MindFlock": _REPO_OK,
            "/go/_stats": _CLICKS_OK,
        }
    )

    async def _no_token():
        return ""

    monkeypatch.setattr(traffic, "_token", _no_token)
    payload = asyncio.run(traffic.TrafficAddon()._payload(days=7, refresh=True))

    assert payload["repo"]["stars"] == 12
    assert payload["downloads_total"] == 7
    assert payload["clicks"]["totals_by_slug"] == {"mac": 3}
    assert payload["star_history"] == []
    assert "401" in payload["errors"]["github"]
    assert payload["errors"]["clicks"] is None


def test_star_history_is_cumulative_by_day(stub_http, monkeypatch):
    """Each stargazer is one +1 event; the curve is their running total."""
    stub_http(
        {
            "/stargazers": (
                200,
                [
                    {"starred_at": "2026-08-08T10:00:00Z"},
                    {"starred_at": "2026-08-10T11:00:00Z"},
                    {"starred_at": "2026-08-10T12:00:00Z"},
                ],
            ),
            "/releases": _RELEASES_OK,
            "/repos/MindFlock/MindFlock": _REPO_OK,
            "/go/_stats": _CLICKS_OK,
        }
    )

    async def _token():
        return "ghp_x"

    monkeypatch.setattr(traffic, "_token", _token)
    payload = asyncio.run(traffic.TrafficAddon()._payload(days=7, refresh=True))

    assert payload["star_history"] == [
        {"day": "2026-08-08", "stars": 1},
        {"day": "2026-08-10", "stars": 3},
    ]
    assert payload["errors"]["github"] is None


def test_unreachable_click_worker_leaves_github_intact(stub_http, monkeypatch):
    """The two sections share nothing; a Worker hiccup must not hide stars."""
    stub_http(
        {
            "/stargazers": (200, []),
            "/releases": _RELEASES_OK,
            "/repos/MindFlock/MindFlock": _REPO_OK,
            "/go/_stats": (0, "Cannot connect to host mindflock.ai"),
        }
    )

    async def _token():
        return "ghp_x"

    monkeypatch.setattr(traffic, "_token", _token)
    payload = asyncio.run(traffic.TrafficAddon()._payload(days=7, refresh=True))

    assert payload["repo"]["stars"] == 12
    assert payload["errors"]["github"] is None
    assert "mindflock.ai" in payload["errors"]["clicks"]
    assert payload["clicks"]["series"] == []
