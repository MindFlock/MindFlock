"""Where your devices leave their (encrypted) addresses: a private repo in
your own GitHub account.

"Reach my devices from anywhere" (``docs/devices-anywhere.md`` §1): one file
per device, ``<slot>.rec``, at the root of ``<login>/mindflock-devices``,
written through the Contents API with the GitHub sign-in MindFlock already
has (:func:`backend.web.core.github_pr.api_token`: Settings → env →
``gh auth token``). A private repo needs the ``repo`` scope — what MindFlock's
own GitHub sign-in, its pre-filled token page and ``gh auth login`` all
already grant; a gist would need ``gist``, which MindFlock's sign-in lacks.

Every failure is a :class:`StoreError` with a ``kind`` and a plain sentence —
never a silent one: ``no_token``, ``permission`` (with the remedy),
``public_repo``, ``unreachable``, ``rate_limited`` (``retry_at``),
``conflict``, ``error``. Only ciphertext and opaque file names ever go in;
commit messages are the constant ``"update"``.

:class:`DirStore` is the same interface over a local folder — the test
carrier for two scratch servers on one machine
(``MINDFLOCK_DEVICES_ANYWHERE_STORE``); it never touches GitHub.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

REPO_NAME = "mindflock-devices"
REPO_DESCRIPTION = (
    "MindFlock: encrypted addresses of your own devices. Private on purpose. "
    "To remove, turn off 'Reach my devices from anywhere' on each device first."
)
API = "https://api.github.com"
_TIMEOUT_S = 15.0
_MAX_FILE = 4096
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.rec\Z")

PERMISSION_TEXT = (
    "GitHub sign-in is missing the repo permission — reconnect GitHub "
    "(Settings → GitHub), or give your fine-grained token access to a private "
    "repo named %s (Contents: read and write)" % REPO_NAME
)
NO_TOKEN_TEXT = (
    "Not signed in to GitHub — connect GitHub (Settings → GitHub) so your "
    "devices can find each other"
)
PUBLIC_TEXT = (
    "A public repo named %s is in your GitHub account — make it private or "
    "rename it; your devices only use a private one" % REPO_NAME
)
UNREACHABLE_TEXT = "Couldn't reach GitHub"


class StoreError(Exception):
    def __init__(self, kind: str, text: str, retry_at: float = 0.0):
        super().__init__(text)
        self.kind = kind
        self.text = text
        self.retry_at = retry_at


def _rate_limited(status: int, headers: dict) -> Optional[float]:
    """When a 403/429 is GitHub's rate limit: the epoch time to retry at."""
    if status not in (403, 429):
        return None
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if "retry-after" in h:
        try:
            return time.time() + max(1.0, float(h["retry-after"]))
        except ValueError:
            return time.time() + 60.0
    if h.get("x-ratelimit-remaining") == "0":
        try:
            return max(time.time() + 1.0, float(h.get("x-ratelimit-reset") or 0))
        except ValueError:
            return time.time() + 600.0
    if status == 429:
        return time.time() + 60.0
    return None


def _wait_text(retry_at: float) -> str:
    mins = max(1, int((retry_at - time.time() + 59) // 60))
    return "GitHub is rate-limiting — retrying in %d min" % mins


async def _http(
    method: str, path: str, token: str, body: Optional[dict] = None
) -> Tuple[int, dict, object]:
    """One call → ``(status, headers, json_or_text)``; ``(0, {}, why)`` when it
    never completed. The one HTTP seam (tests replace the whole store)."""
    import aiohttp

    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MindFlock",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, API + path, headers=headers, json=body
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    data = None
                return resp.status, dict(resp.headers), data
    except Exception as err:  # noqa: BLE001 — offline is a normal outcome
        return 0, {}, type(err).__name__


class GitHubStore:
    """The records in ``<login>/mindflock-devices`` (see the module
    docstring). ``token()`` → the GitHub token (``""`` = none)."""

    def __init__(
        self,
        token: Callable[[], Awaitable[str]],
        *,
        http=None,
        repo: str = REPO_NAME,
    ):
        self._token = token
        self._http = http or _http
        self.repo = repo
        self.login = ""
        self._checked = False  # the repo exists, is private and ours

    async def _call(self, method: str, path: str, body: Optional[dict] = None):
        tok = (await self._token() or "").strip()
        if not tok:
            raise StoreError("no_token", NO_TOKEN_TEXT)
        status, headers, data = await self._http(method, path, tok, body)
        if status == 0:
            raise StoreError("unreachable", UNREACHABLE_TEXT)
        retry = _rate_limited(status, headers)
        if retry is not None:
            raise StoreError("rate_limited", _wait_text(retry), retry_at=retry)
        if status == 401:
            raise StoreError(
                "no_token",
                "GitHub rejected the sign-in (expired or revoked) — reconnect "
                "GitHub (Settings → GitHub)",
            )
        if status >= 500:
            raise StoreError("unreachable", "GitHub answered HTTP %d" % status)
        return status, headers, data

    async def _who(self) -> str:
        if self.login:
            return self.login
        status, headers, data = await self._call("GET", "/user")
        if status != 200 or not isinstance(data, dict) or not data.get("login"):
            raise StoreError(
                "error", "GitHub answered HTTP %d for the signed-in user" % status
            )
        scopes = {str(k).lower(): str(v) for k, v in (headers or {}).items()}.get(
            "x-oauth-scopes"
        )
        if scopes is not None:
            have = {s.strip() for s in scopes.split(",") if s.strip()}
            # A classic token says what it has: without ``repo`` a private
            # repo can't be created, read or written. Fine-grained tokens say
            # nothing; their 403/404s below give the same answer.
            if "repo" not in have:
                raise StoreError("permission", PERMISSION_TEXT)
        self.login = str(data["login"])
        return self.login

    def _base(self) -> str:
        return "/repos/%s/%s" % (self.login, self.repo)

    async def ensure_repo(self, create: bool) -> bool:
        """Whether the repo exists (private, ours). ``create``: make it when
        it doesn't. Raises :class:`StoreError`."""
        if self._checked:
            return True
        await self._who()
        status, _h, data = await self._call("GET", self._base())
        if status == 200 and isinstance(data, dict):
            if not data.get("private"):
                raise StoreError("public_repo", PUBLIC_TEXT)
            perms = data.get("permissions")
            if isinstance(perms, dict) and perms.get("push") is False:
                raise StoreError("permission", PERMISSION_TEXT)
            self._checked = True
            return True
        if status == 403:
            raise StoreError("permission", PERMISSION_TEXT)
        if status != 404:
            raise StoreError("error", "GitHub answered HTTP %d" % status)
        if not create:
            return False
        status, _h, data = await self._call(
            "POST",
            "/user/repos",
            {
                "name": self.repo,
                "private": True,
                "auto_init": True,
                "description": REPO_DESCRIPTION,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
            },
        )
        if status in (403, 404):
            raise StoreError("permission", PERMISSION_TEXT)
        if status == 422:
            # Created meanwhile by another of your devices: look again.
            return await self.ensure_repo(create=False)
        if status not in (200, 201):
            raise StoreError(
                "error", "GitHub answered HTTP %d creating the repo" % status
            )
        if isinstance(data, dict) and data.get("private") is False:
            raise StoreError("public_repo", PUBLIC_TEXT)
        self._checked = True
        return True

    async def list(self) -> Dict[str, str]:
        """``{file name: git sha}`` of every record file (``{}`` before the
        repo exists)."""
        if not await self.ensure_repo(create=False):
            return {}
        status, _h, data = await self._call("GET", self._base() + "/contents/")
        if status == 404:
            return {}
        if status == 403:
            raise StoreError("permission", PERMISSION_TEXT)
        if status != 200 or not isinstance(data, list):
            raise StoreError(
                "error", "GitHub answered HTTP %d listing records" % status
            )
        out = {}
        for item in data:
            if (
                isinstance(item, dict)
                and item.get("type") == "file"
                and isinstance(item.get("name"), str)
                and _NAME_RE.match(item["name"])
                and isinstance(item.get("sha"), str)
            ):
                out[item["name"]] = item["sha"]
        return out

    async def read(self, name: str) -> Tuple[str, str]:
        """``(text, sha)`` of one record file; ``("", "")`` when gone."""
        if not _NAME_RE.match(name):
            raise ValueError("bad record name")
        status, _h, data = await self._call("GET", self._base() + "/contents/" + name)
        if status == 404:
            return "", ""
        if status == 403:
            raise StoreError("permission", PERMISSION_TEXT)
        if status != 200 or not isinstance(data, dict):
            raise StoreError(
                "error", "GitHub answered HTTP %d reading a record" % status
            )
        if int(data.get("size") or 0) > _MAX_FILE:
            return "", str(data.get("sha") or "")
        try:
            text = base64.b64decode(str(data.get("content") or "")).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            text = ""
        return text, str(data.get("sha") or "")

    async def _sha(self, name: str) -> str:
        status, _h, data = await self._call("GET", self._base() + "/contents/" + name)
        if status == 200 and isinstance(data, dict):
            return str(data.get("sha") or "")
        return ""

    async def write(self, name: str, text: str, sha: str = "") -> str:
        """Create or replace one record file → its new sha."""
        if not _NAME_RE.match(name):
            raise ValueError("bad record name")
        await self.ensure_repo(create=True)
        body = {
            "message": "update",
            "content": base64.b64encode(text.encode("ascii")).decode("ascii"),
        }
        for attempt in range(2):
            if sha:
                body["sha"] = sha
            else:
                body.pop("sha", None)
            status, _h, data = await self._call(
                "PUT", self._base() + "/contents/" + name, body
            )
            if status in (200, 201) and isinstance(data, dict):
                return str((data.get("content") or {}).get("sha") or "")
            if status in (409, 422) and attempt == 0:
                sha = await self._sha(name)  # changed under us: take the latest
                continue
            if status in (403, 404):
                raise StoreError("permission", PERMISSION_TEXT)
            break
        raise StoreError("error", "GitHub answered HTTP %d saving the record" % status)

    async def delete(self, name: str, sha: str = "") -> bool:
        """Remove one record file (gone already is fine)."""
        if not _NAME_RE.match(name):
            raise ValueError("bad record name")
        if not await self.ensure_repo(create=False):
            return True
        sha = sha or await self._sha(name)
        if not sha:
            return True
        status, _h, _d = await self._call(
            "DELETE",
            self._base() + "/contents/" + name,
            {"message": "update", "sha": sha},
        )
        if status in (200, 404):
            return True
        if status == 409 or status == 422:
            sha = await self._sha(name)
            if not sha:
                return True
            status, _h, _d = await self._call(
                "DELETE",
                self._base() + "/contents/" + name,
                {"message": "update", "sha": sha},
            )
            if status in (200, 404):
                return True
        if status == 403:
            raise StoreError("permission", PERMISSION_TEXT)
        raise StoreError(
            "error", "GitHub answered HTTP %d removing the record" % status
        )


class DirStore:
    """The same interface over a local folder (the test carrier — never
    GitHub). The sha is the content's sha1, like git's."""

    def __init__(self, path: str):
        self.path = path
        self.login = "local-folder"

    def _p(self, name: str) -> str:
        if not _NAME_RE.match(name):
            raise ValueError("bad record name")
        return os.path.join(self.path, name)

    @staticmethod
    def _sha_of(text: str) -> str:
        return hashlib.sha1(text.encode("ascii")).hexdigest()

    async def ensure_repo(self, create: bool) -> bool:
        if os.path.isdir(self.path):
            return True
        if create:
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            return True
        return False

    async def list(self) -> Dict[str, str]:
        if not os.path.isdir(self.path):
            return {}
        out = {}
        for n in os.listdir(self.path):
            if _NAME_RE.match(n):
                try:
                    with open(os.path.join(self.path, n), encoding="ascii") as f:
                        out[n] = self._sha_of(f.read())
                except (OSError, UnicodeDecodeError):
                    continue
        return out

    async def read(self, name: str) -> Tuple[str, str]:
        try:
            with open(self._p(name), encoding="ascii") as f:
                text = f.read(_MAX_FILE + 1)
        except (OSError, UnicodeDecodeError):
            return "", ""
        return (text if len(text) <= _MAX_FILE else ""), self._sha_of(text)

    async def write(self, name: str, text: str, sha: str = "") -> str:
        await self.ensure_repo(create=True)
        tmp = self._p(name) + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write(text)
        os.replace(tmp, self._p(name))
        return self._sha_of(text)

    async def delete(self, name: str, sha: str = "") -> bool:
        try:
            os.unlink(self._p(name))
        except FileNotFoundError:
            pass
        return True
