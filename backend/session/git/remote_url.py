"""Parse git remote URLs into ``(host, owner, repo)`` and build forge URLs.

MindFlock never rewrites a user's remote: whatever spelling is in their git
config is the spelling we push to. But several features need to know *which*
repo a remote points at — deriving a branch/compare URL for the browser,
comparing a workspace's origin against the configured repo, or picking the
matching transport when a clone URL has to be synthesized from an
``owner/repo`` slug.

All four spellings git accepts are handled::

    https://github.com/Org/repo.git
    ssh://git@github.com:22/Org/repo.git
    git@github.com:Org/repo.git          (scp-style — no scheme, no slash)
    git://github.com/Org/repo.git

Local filesystem paths (``/home/me/app``, ``../app``, ``file://``) parse to
``None``: they are valid clone sources but have no forge behind them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit

__all__ = [
    "RemoteRef",
    "parse_remote",
    "is_local_path",
    "same_repo",
    "branch_url",
    "compare_url",
    "pr_list_url",
    "to_ssh",
    "to_https",
]

# scp-style: [user@]host:path — no scheme, and the colon is NOT followed by a
# port number (``host:22/path`` is an ssh:// URL missing its scheme, not scp).
_SCP = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/@]+):(?P<path>.+)$")


@dataclass(frozen=True)
class RemoteRef:
    """A remote resolved to the repo it names."""

    host: str  # lower-cased, no port ("github.com")
    owner: str  # first path segment, original case ("MindFlock")
    repo: str  # last path segment, no ".git" ("MindFlock")

    @property
    def slug(self) -> str:
        """``owner/repo`` — the form the GitHub API and ``gh -R`` take."""
        return "{}/{}".format(self.owner, self.repo)

    @property
    def web_url(self) -> str:
        """The repo's browser URL."""
        return "https://{}/{}/{}".format(self.host, _esc(self.owner), _esc(self.repo))

    def key(self) -> tuple:
        """Transport-independent identity, for comparing two spellings."""
        return (self.host, self.owner.lower(), self.repo.lower())


def is_local_path(url: str) -> bool:
    """Whether ``url`` names a path on this machine rather than a forge.

    ``git clone`` accepts local paths, and MindFlock's provisioning uses them
    (a canonical base clone is cloned from the user's own checkout), so this is
    a routine case rather than an error.
    """
    u = (url or "").strip()
    if not u:
        return False
    if u.startswith("file://"):
        return True
    if u.startswith(("/", "./", "../", "~")):
        return True
    # Windows drive letter: C:\repo or C:/repo. Distinguished from scp-style
    # (git@host:path) by the single-character "host".
    if len(u) > 2 and u[1] == ":" and u[0].isalpha() and u[2] in "\\/":
        return True
    return False


def parse_remote(url: str) -> Optional[RemoteRef]:
    """Resolve ``url`` to the repo it names, or ``None``.

    Returns ``None`` for local paths, unrecognised spellings, and any URL whose
    path is not at least ``owner/repo`` — callers treat that as "no forge
    behind this remote" and degrade rather than guess.
    """
    u = (url or "").strip()
    if not u or is_local_path(u):
        return None

    host = ""
    path = ""
    if "://" in u:
        parts = urlsplit(u)
        if not parts.hostname:
            return None
        host = parts.hostname
        path = parts.path
    else:
        m = _SCP.match(u)
        if m is None:
            return None
        host = m.group("host")
        path = m.group("path")
        # ``host:22/Org/repo`` is a schemeless ssh URL, not an scp path whose
        # first segment happens to be numeric.
        head, _, rest = path.partition("/")
        if head.isdigit() and rest:
            path = rest

    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return None

    repo = segments[-1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not repo:
        return None

    return RemoteRef(host=host.lower(), owner=segments[-2], repo=repo)


def same_repo(a: str, b: str) -> bool:
    """Whether two remote URLs name the same repo, ignoring transport.

    ``git@github.com:Org/app.git`` and ``https://github.com/Org/app`` are the
    same repo; a literal string compare says otherwise, which is how a user
    with an SSH checkout and an HTTPS ``[repository].url`` (or the reverse)
    silently loses their configured base branch.
    """
    ra, rb = parse_remote(a), parse_remote(b)
    if ra is not None and rb is not None:
        return ra.key() == rb.key()
    return False


def branch_url(url: str, branch: str) -> Optional[str]:
    """Browser URL for ``branch`` on the repo ``url`` names, or ``None``."""
    ref = parse_remote(url)
    if ref is None or not branch:
        return None
    return "{}/tree/{}".format(ref.web_url, _esc(branch))


def compare_url(url: str, base: str, head: str) -> Optional[str]:
    """GitHub's prefilled "open a pull request" URL, or ``None``.

    This is what MindFlock hands the user when it cannot open the PR itself —
    the page opens with the diff and body already filled in, so the PR is one
    click away without ``gh`` and without any token.
    """
    ref = parse_remote(url)
    if ref is None or not base or not head:
        return None
    return "{}/compare/{}...{}?expand=1".format(ref.web_url, _esc(base), _esc(head))


def pr_list_url(url: str, branch: str) -> Optional[str]:
    """The repo's PR list filtered to ``branch`` as head, or ``None``."""
    ref = parse_remote(url)
    if ref is None or not branch:
        return None
    return "{}/pulls?q={}".format(
        ref.web_url, quote("is:pr head:{}".format(branch), safe="")
    )


def to_ssh(url: str) -> Optional[str]:
    """``url`` respelled as an scp-style SSH remote, or ``None``."""
    ref = parse_remote(url)
    if ref is None:
        return None
    return "git@{}:{}/{}.git".format(ref.host, ref.owner, ref.repo)


def to_https(url: str) -> Optional[str]:
    """``url`` respelled as an HTTPS remote, or ``None``."""
    ref = parse_remote(url)
    if ref is None:
        return None
    return "https://{}/{}/{}.git".format(ref.host, ref.owner, ref.repo)


def _esc(segment: str) -> str:
    """Percent-encode one URL path segment (branch names may contain ``#``)."""
    return quote(segment, safe="/")
