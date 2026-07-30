"""Open, merge and look up GitHub pull requests without the GitHub CLI.

``gh`` is the nicest way to do this — it carries the user's own credentials and
needs nothing pasted into MindFlock — so the routes still reach for it first.
But it is OPTIONAL: a contributor whose git config uses SSH remotes and who
never installed ``gh`` must still be able to press "Make PR". This module is
the middle rung of that ladder: plain GitHub REST, authenticated with whatever
token :mod:`backend.ticket_ingestion.github_auth` can resolve (config.toml →
Settings → ``$GH_TOKEN``/``$GITHUB_TOKEN`` → ``gh auth token``). Below it sits
a browser fallback in the routes, which needs no credentials at all.

Transport-independence is the point. The repo is resolved from the workspace's
own ``origin`` through :func:`~backend.session.git.parse_remote`, so an SSH
remote, an HTTPS remote and an scp-style remote all resolve to the same
``owner/repo``. MindFlock never rewrites the user's remote to make this work —
pushing stays plain ``git push`` over whatever they configured.

Nothing here raises at the caller. These sit behind HTTP routes that must
degrade to the browser fallback rather than 500, so every entry point returns a
typed :class:`PRResult` (or ``None``): ``unavailable`` means "REST could not
even be attempted" (no token, or an origin with no forge behind it), which is
the signal to fall through to the browser.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

from backend.session.git import RemoteRef, parse_remote

_logger = logging.getLogger(__name__)

_API = "https://api.github.com"
# Total wall-clock budget for one request/response, matching the ingestion
# monitors. A route that waits longer than this has already lost the user.
_HTTP_TIMEOUT_S = 30
# Git plumbing here is metadata-only (a remote URL, a commit list), so a short
# leash is safe: a wedged git degrades to "no REST path" and the browser
# fallback takes over.
_GIT_TIMEOUT_S = 15


@dataclass(frozen=True)
class PRResult:
    """The outcome of a REST PR operation — never an exception.

    ``unavailable`` is deliberately distinct from a plain failure: it means the
    REST rung could not be tried at all (no token, or ``origin`` names no
    forge), so the caller should offer the browser instead of showing an error.
    """

    ok: bool = False
    url: Optional[str] = None
    number: Optional[int] = None
    state: Optional[str] = None
    error: str = ""
    unavailable: bool = False


# --------------------------------------------------------------------------- #
# Repo resolution — from the workspace's own remote, whatever its spelling
# --------------------------------------------------------------------------- #
def origin_url(wt: str) -> str:
    """The workspace's ``origin`` URL verbatim, or ``""``.

    Verbatim matters: this is the string the user configured, and it is what
    :mod:`backend.session.git.remote_url` turns into browser URLs. We read it,
    we never write it.
    """
    if not wt:
        return ""
    try:
        cp = subprocess.run(
            ["git", "-C", wt, "remote", "get-url", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.decode("utf-8", "replace").strip()


def repo_ref(wt: str) -> Optional[RemoteRef]:
    """The ``owner/repo`` behind the workspace's origin, or ``None``.

    ``None`` covers both "no origin" and "origin is a local path / some other
    forge" — in every one of those cases there is no GitHub API to call.
    """
    return parse_remote(origin_url(wt))


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _github_config():
    """The ingestion ``[github]`` section, or a bare config with no token.

    The web UI's PR buttons must work on a machine that never configured
    ticket ingestion, so a missing/blank section is normal rather than an
    error: the bare config resolves ``token=""`` and the shared resolver then
    walks Settings → env → ``gh auth token`` exactly as it does elsewhere.
    """
    from backend.ticket_ingestion.config import GithubConfig

    try:
        from backend.ticket_ingestion.config import load_config

        cfg = load_config()
        if cfg.github is not None:
            return cfg.github
    except Exception:  # noqa: BLE001 — ingestion is optional; never break a PR
        pass
    return GithubConfig(
        base_branch="main",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
    )


async def _token() -> str:
    """A usable GitHub API token, or ``""`` — never raises.

    ``resolve_token`` raises a long "no token available" message when every
    layer comes up empty; here that is a routine outcome (it just means the
    browser fallback is next), so it is folded into an empty string.
    """
    from backend.ticket_ingestion.github_auth import resolve_token

    try:
        return (await resolve_token(_github_config())).strip()
    except Exception:  # noqa: BLE001 — "no token" is a normal state here
        return ""


def has_token_sync() -> bool:
    """Whether a token is reachable WITHOUT shelling out — for capability probes.

    Deliberately skips the ``gh auth token`` rung: capability probes run on
    every page load, and a subprocess per probe is not worth the extra
    coverage. The gh rung is already represented by ``gh_available()`` on the
    caller's side, so nothing real is lost.
    """
    try:
        from backend.config.secrets import resolve_secret_sync

        explicit = ""
        try:
            explicit = (_github_config().token or "").strip()
        except Exception:  # noqa: BLE001
            explicit = ""
        return bool(
            resolve_secret_sync(
                explicit=explicit,
                settings_getter=lambda s: s.github.token,
                env_vars=("GH_TOKEN", "GITHUB_TOKEN"),
            )
        )
    except Exception:  # noqa: BLE001 — capability probes must never 500
        return False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def _request(
    method: str,
    path: str,
    *,
    token: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
) -> tuple[int, Any]:
    """One GitHub API call -> ``(status, decoded_body)``; ``(0, error_string)``
    when the request never completed (DNS, TLS, timeout, offline).

    The single HTTP seam in this module: every operation below funnels through
    it, which is also what tests stub. ``aiohttp`` is imported lazily so that
    importing the web server does not pay for it on machines that never touch
    GitHub.
    """
    import aiohttp

    headers = {
        "Authorization": "Bearer {}".format(token),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = "{}{}".format(_API, path)
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, url, headers=headers, params=params, json=body
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 — non-JSON error page
                    data = await resp.text()
                return resp.status, data
    except Exception as err:  # noqa: BLE001 — network faults are a normal outcome
        _logger.debug("GitHub %s %s failed: %s", method, path, err)
        return 0, str(err)


def _api_message(status: int, data: Any) -> str:
    """GitHub's own words for a failure, so the UI can repeat them.

    The API puts the useful part in ``message`` plus a list of ``errors`` (that
    is where "No commits between main and feat" lives), and the routes match on
    that text — so both are joined rather than summarised.
    """
    if status == 0:
        return "could not reach api.github.com: {}".format(data)
    if isinstance(data, dict):
        parts = [str(data.get("message") or "").strip()]
        for e in data.get("errors") or []:
            if isinstance(e, dict):
                m = str(e.get("message") or "").strip()
                if m:
                    parts.append(m)
        text = " — ".join(p for p in parts if p)
        if text:
            return text
    if isinstance(data, str) and data.strip():
        return data.strip()
    return "GitHub API returned HTTP {}".format(status)


# --------------------------------------------------------------------------- #
# Filling a PR from the branch's commits (what `gh pr create --fill` does)
# --------------------------------------------------------------------------- #
def _commit_messages(wt: str, base: str, head: str) -> list[str]:
    """Full messages of the commits ``head`` has beyond ``base``, oldest first.

    ``origin/<base>`` is tried before the local ``<base>`` for the same reason
    the stage probe does it: the local base branch is often stale (or absent in
    a worktree), and the PR is measured against the remote anyway.
    """
    for ref in ("origin/" + base, base):
        try:
            cp = subprocess.run(
                [
                    "git",
                    "-C",
                    wt,
                    "log",
                    "--reverse",
                    "--no-merges",
                    # NUL between commits: a commit body may contain anything,
                    # including blank lines, so no textual separator is safe.
                    "--format=%B%x00",
                    "{}..{}".format(ref, head),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if cp.returncode != 0:
            continue  # unknown ref — try the next spelling
        raw = cp.stdout.decode("utf-8", "replace")
        msgs = [m.strip() for m in raw.split("\0")]
        msgs = [m for m in msgs if m]
        if msgs:
            return msgs
    return []


def _fill(wt: str, base: str, head: str) -> Optional[tuple[str, str]]:
    """``(title, body)`` for the PR, mirroring ``gh pr create --fill``.

    Title is the first commit's SUBJECT; body is everything else the branch's
    commits say — that first commit's own body included, since the common
    one-commit branch keeps its explanation there and dropping it would make
    the REST rung write worse PRs than the gh rung.

    ``None`` when the branch has no commits beyond the base — the caller turns
    that into the same "nothing to PR" message gh's failure produces.
    """
    msgs = _commit_messages(wt, base, head)
    if not msgs:
        return None
    subject, _, first_body = msgs[0].partition("\n")
    parts = [first_body.strip()] + [m.strip() for m in msgs[1:]]
    body = "\n\n".join(p for p in parts if p)
    return subject.strip(), body


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
async def create_pr(wt: str, base: str, head: str) -> PRResult:
    """Open a PR from ``head`` into ``base`` over REST.

    ``head`` is sent unqualified (``feat/x``, not ``owner:feat/x``): MindFlock
    pushes branches to the session's own origin, so the head always lives in
    the same repo the PR is opened against.
    """
    ref = repo_ref(wt)
    if ref is None:
        return PRResult(unavailable=True)
    token = await _token()
    if not token:
        return PRResult(unavailable=True)

    filled = _fill(wt, base, head)
    if filled is None:
        # Same wording GitHub itself uses, so the route's "nothing to PR"
        # mapping fires whether the failure came from git or from the API.
        return PRResult(error="No commits between {} and {}".format(base, head))
    title, body = filled

    status, data = await _request(
        "POST",
        "/repos/{}/pulls".format(ref.slug),
        token=token,
        body={"title": title, "body": body, "base": base, "head": head},
    )
    if status == 201 and isinstance(data, dict):
        return PRResult(
            ok=True,
            url=data.get("html_url"),
            number=data.get("number"),
            state="OPEN",
        )
    return PRResult(error=_api_message(status, data))


async def merge_pr(wt: str, number: int) -> PRResult:
    """Merge PR ``number`` with a true merge commit (never squash/rebase).

    Matches ``gh pr merge --merge``; a repo that forbids merge commits refuses,
    and its refusal is surfaced verbatim, exactly as gh's was.
    """
    ref = repo_ref(wt)
    if ref is None:
        return PRResult(unavailable=True)
    token = await _token()
    if not token:
        return PRResult(unavailable=True)

    status, data = await _request(
        "PUT",
        "/repos/{}/pulls/{}/merge".format(ref.slug, number),
        token=token,
        body={"merge_method": "merge"},
    )
    if status == 200 and isinstance(data, dict) and data.get("merged"):
        return PRResult(ok=True, number=number, state="MERGED")
    return PRResult(number=number, error=_api_message(status, data))


async def find_pr(wt: str, branch: str) -> Optional[dict]:
    """The most recent PR whose head is ``branch``, or ``None``.

    Shaped like the ``gh pr list --json url,state`` answer it stands in for —
    ``{"url", "state", "number"}`` with an uppercase OPEN/MERGED/CLOSED state —
    so the stage machine cannot tell which rung produced it. REST reports a
    merged PR as ``state: closed`` plus a ``merged_at``, which is folded back
    into gh's three-way vocabulary here.
    """
    ref = repo_ref(wt)
    if ref is None or not branch:
        return None
    token = await _token()
    if not token:
        return None

    status, data = await _request(
        "GET",
        "/repos/{}/pulls".format(ref.slug),
        token=token,
        params={
            "head": "{}:{}".format(ref.owner, branch),
            "state": "all",
            "per_page": "1",
        },
    )
    if status != 200 or not isinstance(data, list) or not data:
        return None
    item = data[0]
    if not isinstance(item, dict):
        return None
    state = "MERGED" if item.get("merged_at") else str(item.get("state", "")).upper()
    return {"url": item.get("html_url"), "state": state, "number": item.get("number")}


# --------------------------------------------------------------------------- #
# Sync bridge
# --------------------------------------------------------------------------- #
def _run_sync(coro):
    """Await ``coro`` from synchronous code, wherever that code is running.

    The stage probe (``_pr_info``) is synchronous and already runs in a worker
    thread, where ``asyncio.run`` is free. Being called from the event-loop
    thread would be a bug, but a wedged UI is a worse answer than a slow one,
    so that case is handed to a scratch thread instead of raising.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def find_pr_sync(wt: str, branch: str) -> Optional[dict]:
    """:func:`find_pr` for synchronous callers (the stage probe)."""
    try:
        return _run_sync(find_pr(wt, branch))
    except Exception:  # noqa: BLE001 — a stage probe never fails a poll
        return None
