"""Traffic addon: MindFlock's own public reach, over HTTP.

Exposes ``GET /api/traffic`` — GitHub stars/forks, per-release download counts,
and per-day click counts for the tracked ``mindflock.ai/go/<slug>`` redirects
(see the ``webpage/worker`` Cloudflare Worker in the marketing-site repo) — so
the desktop app's dev-only Settings → Site traffic screen has one endpoint to
poll instead of stitching three sources together in the browser.

Every upstream call is best-effort: a GitHub rate limit or the click-tracking
worker being unreachable degrades that ONE section (``errors.github`` /
``errors.clicks``) rather than 500ing the whole payload — the two sections
share nothing else, so there is no reason a click-tracking hiccup should hide
stars the request already has.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .base import Addon, AppContext, FrontendDescriptor

#: The product's own repo — never the user's workspace. Every other GitHub
#: integration in this codebase (github_pr.py, github_issues.py) resolves
#: owner/repo from a workspace's git remote; this addon has no workspace, it
#: always means MindFlock/MindFlock.
_REPO = "MindFlock/MindFlock"
_GITHUB_API = "https://api.github.com"
_CLICKS_STATS_URL = "https://mindflock.ai/go/_stats"
_HTTP_TIMEOUT_S = 15

#: Cached payload lifetime. Longer than Doctor's 30s on purpose: this hits
#: GitHub's REST API (60 req/hr unauthenticated, higher with a token) and an
#: external Worker, neither of which needs to be re-asked every settings-panel
#: open. ``?refresh=1`` bypasses it for a manual Refresh click.
_CACHE_TTL_S = 300.0


async def _token() -> str:
    """A GitHub token if one is reachable, else ``""`` — never raises.

    The same chain the PR buttons use (config.toml → Settings → ``$GH_TOKEN``
    → ``gh auth token``), because the ``gh auth token`` rung is where the token
    usually IS: `gh auth login` is a normal thing to have done and pasting a
    PAT into Settings is not. Resolving only the static layers left this screen
    calling GitHub anonymously on a machine that was perfectly well
    authenticated.

    Anonymous is still a supported state — read-only public data needs no
    scope, and stars/forks/downloads all answer without a token. Only the star
    HISTORY does not: ``GET /repos/{repo}/stargazers`` is 401 "Requires
    authentication" even for a public repo, so without this rung that one
    section was permanently broken.
    """
    try:
        from backend.web.core.github_pr import api_token

        return (await api_token()).strip()
    except Exception:  # noqa: BLE001 — an unresolvable token is a normal state
        return ""


async def _get_json(url: str, *, token: str = "", accept: str = "") -> tuple[int, Any]:
    """One GET -> ``(status, decoded_body)``; ``(0, error_string)`` when the
    request never completed. Mirrors the seam in ``core/github_pr.py``, kept
    separate because this addon calls two different hosts, not one API.

    ``accept`` overrides the default GitHub media type — needed for the
    stargazers endpoint, which only includes each star's timestamp under the
    ``application/vnd.github.star+json`` media type.
    """
    import aiohttp

    if accept:
        headers = {"Accept": accept}
    elif "github.com" in url:
        headers = {"Accept": "application/vnd.github+json"}
    else:
        headers = {}
    if token:
        headers["Authorization"] = "Bearer {}".format(token)
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 — non-JSON error page
                    data = await resp.text()
                return resp.status, data
    except Exception as err:  # noqa: BLE001 — network faults are routine here
        return 0, str(err)


async def _fetch_repo(token: str) -> tuple[Optional[dict], str]:
    status, data = await _get_json(
        "{}/repos/{}".format(_GITHUB_API, _REPO), token=token
    )
    if status == 200 and isinstance(data, dict):
        return {
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "url": data.get("html_url") or "https://github.com/" + _REPO,
        }, ""
    msg = data.get("message") if isinstance(data, dict) else str(data)
    return None, "GitHub repo lookup failed ({}): {}".format(status, msg)


async def _fetch_releases(token: str) -> tuple[list, int, str]:
    """Per-release asset download counts, newest first, plus the all-time sum.

    One page of up to 100 covers every release this project has cut so far
    with room to spare; if that ever stops being true, download counts merely
    stop at the 100th-newest release rather than the endpoint breaking.
    """
    status, data = await _get_json(
        "{}/repos/{}/releases?per_page=100".format(_GITHUB_API, _REPO), token=token
    )
    if status != 200 or not isinstance(data, list):
        msg = data.get("message") if isinstance(data, dict) else str(data)
        return [], 0, "GitHub releases lookup failed ({}): {}".format(status, msg)

    releases = []
    total = 0
    for rel in data:
        if not isinstance(rel, dict):
            continue
        assets = []
        rel_total = 0
        for a in rel.get("assets") or []:
            if not isinstance(a, dict):
                continue
            n = int(a.get("download_count") or 0)
            assets.append({"name": a.get("name"), "downloads": n})
            rel_total += n
        releases.append(
            {
                "tag": rel.get("tag_name"),
                "published_at": rel.get("published_at"),
                "prerelease": bool(rel.get("prerelease")),
                "assets": assets,
                "total_downloads": rel_total,
            }
        )
        total += rel_total
    return releases, total, ""


#: Bounds the stargazers pagination below. 30 pages * 100/page = 3,000 stars
#: of history — comfortably past what this project has today, and a request
#: nobody is waiting on synchronously (the whole payload is cached 5 minutes).
_STAR_HISTORY_MAX_PAGES = 30


async def _fetch_star_history(token: str) -> tuple[list, str]:
    """Cumulative GitHub stars by day, oldest first.

    The plain repo endpoint only has the CURRENT star count — no history. The
    stargazers endpoint has per-star timestamps, but only under the
    ``application/vnd.github.star+json`` media type (the default media type
    drops ``starred_at`` entirely). Each stargazer is one +1 event; grouping
    those by day and running a cumulative sum reconstructs the growth curve
    the same way star-history-style tools do.
    """
    accept = "application/vnd.github.star+json"
    by_day: dict[str, int] = {}
    for page in range(1, _STAR_HISTORY_MAX_PAGES + 1):
        status, data = await _get_json(
            "{}/repos/{}/stargazers?per_page=100&page={}".format(
                _GITHUB_API, _REPO, page
            ),
            token=token,
            accept=accept,
        )
        if status != 200 or not isinstance(data, list):
            if page == 1:
                msg = data.get("message") if isinstance(data, dict) else str(data)
                return [], "GitHub star history lookup failed ({}): {}".format(
                    status, msg
                )
            break  # a later page failing still leaves an honest partial history
        if not data:
            break
        for entry in data:
            if not isinstance(entry, dict):
                continue
            starred_at = str(entry.get("starred_at") or "")
            if len(starred_at) < 10:
                continue
            day = starred_at[:10]  # "2026-08-10T12:34:56Z" -> "2026-08-10"
            by_day[day] = by_day.get(day, 0) + 1
        if len(data) < 100:
            break  # last page

    if not by_day:
        return [], ""

    running = 0
    history = []
    for day in sorted(by_day):
        running += by_day[day]
        history.append({"day": day, "stars": running})
    return history, ""


async def _fetch_clicks(days: int) -> dict:
    status, data = await _get_json("{}?days={}".format(_CLICKS_STATS_URL, days))
    if status != 200 or not isinstance(data, dict):
        detail = data.get("error") if isinstance(data, dict) else data
        return {
            "days": days,
            "series": [],
            "totals_by_slug": {},
            "error": str(detail or status),
        }

    series = data.get("series") or []
    totals: dict[str, int] = {}
    for row in series:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "")
        totals[slug] = totals.get(slug, 0) + int(row.get("clicks") or 0)
    return {"days": days, "series": series, "totals_by_slug": totals, "error": ""}


class TrafficAddon(Addon):
    id = "traffic"
    label = "Site traffic"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._cache: Optional[dict] = None
        self._cached_at: float = 0.0
        self._router = self._build_router()

    async def _payload(self, *, days: int, refresh: bool) -> dict:
        now = time.monotonic()
        if (
            not refresh
            and self._cache is not None
            and self._cache.get("_days") == days
            and now - self._cached_at < _CACHE_TTL_S
        ):
            return self._cache

        token = await _token()
        repo, repo_err = await _fetch_repo(token)
        releases, downloads_total, releases_err = await _fetch_releases(token)
        star_history, star_history_err = await _fetch_star_history(token)
        clicks = await _fetch_clicks(days)

        github_err = repo_err or releases_err or star_history_err
        payload = {
            "generated": time.time(),
            "repo": repo,
            "star_history": star_history,
            "releases": releases,
            "downloads_total": downloads_total,
            "clicks": clicks,
            "errors": {
                "github": github_err or None,
                "clicks": clicks.get("error") or None,
            },
            "_days": days,
        }
        self._cache = payload
        self._cached_at = now
        return payload

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api")

        @router.get("/traffic")
        async def get_traffic(days: int = 90, refresh: bool = False) -> JSONResponse:
            days = max(1, min(90, days))
            payload = await self._payload(days=days, refresh=refresh)
            return JSONResponse({k: v for k, v in payload.items() if k != "_days"})

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    def frontend(self):
        return [
            FrontendDescriptor(
                id="traffic",
                label="Site traffic",
                where="settings",
                module=None,
                api_base="/api/traffic",
                order=95,
                # Hand-wired into the settings dialog (dev-shell gated), same
                # as Doctor — the generic slot renderer skips "settings".
                builtin_ui=True,
            )
        ]
