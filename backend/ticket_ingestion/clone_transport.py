"""Pick the clone URL (and the git environment) the ingestion pipeline clones with.

The monitors discover work by ``owner/repo`` slug, not by remote URL, so the
pipeline used to *synthesize* ``https://github.com/<slug>.git`` and clone that.
For a contributor whose git is set up for SSH only — no HTTPS credential helper,
no PAT in a keychain — that clone prompts for a username on a machine with
nobody watching, and the poll loop hangs until the wall-clock timeout fires.

So the transport becomes a choice, made in one place and shared by all four
ingestion entry points (issue monitor, PR monitor, ticket provisioner, PR
provisioner):

``[repository].git_transport`` (see :class:`~backend.config.settings.RepositorySettings`)

``"auto"`` (the default)
    Use the user's OWN ``[repository].url`` spelling verbatim whenever it names
    the same repo — MindFlock never rewrites a remote the user configured. Only
    when nothing configured names this repo do we fall back to the API's HTTPS
    clone URL, and finally to a synthesized one.
``"ssh"`` / ``"https"``
    An explicit instruction; it always wins, respelling whatever we have.

Note that git's own ``url.<base>.insteadOf`` rewrites still apply on top of
whatever this module returns: a user who has

    [url "git@github.com:"]
        insteadOf = https://github.com/

in their ``~/.gitconfig`` keeps working on the "auto" default without setting
anything here, because git rewrites our HTTPS URL to SSH before it dials out.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from backend.session.git.remote_url import (
    is_local_path,
    parse_remote,
    same_repo,
    to_https,
    to_ssh,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "TRANSPORTS",
    "normalize_transport",
    "resolve_transport",
    "configured_repo_url",
    "resolve_clone_url",
    "apply_transport",
    "headless_git_env",
    "clone_failure_hint",
    "run_network_git",
]

#: Accepted ``[repository].git_transport`` values. Mirrors
#: :data:`backend.config.settings.GIT_TRANSPORTS`; kept as a literal here so the
#: clone path never imports the settings store just to validate a string.
TRANSPORTS = ("auto", "ssh", "https")

#: Environment override for the transport, matching the ``MINDFLOCK_*``
#: convention of the neighbouring repository fields (``MINDFLOCK_REPO_URL``,
#: ``MINDFLOCK_WORKSPACE_DIR``).
TRANSPORT_ENV = "MINDFLOCK_GIT_TRANSPORT"


def normalize_transport(value: Any) -> str:
    """Coerce ``value`` to one of :data:`TRANSPORTS`.

    A typo in the config must not take the pipeline down, so an unknown value
    (and an empty one) degrades to ``"auto"`` rather than raising.
    """
    v = str(value or "").strip().lower()
    if v in TRANSPORTS:
        return v
    if v:
        _logger.warning(
            "Unknown [repository].git_transport %r; using 'auto' (valid: %s)",
            value,
            ", ".join(TRANSPORTS),
        )
    return "auto"


def resolve_transport(config: Any = None) -> str:
    """The effective transport: env → settings.json → ``config`` → ``"auto"``.

    ``config`` is any object that may carry a ``git_transport`` attribute (the
    pipeline config); ``None``/absent simply falls through. Read via
    :func:`getattr` so a config object that predates the field is fine.
    """
    from backend.config import settings as _s

    return normalize_transport(
        _s.resolve_str(
            env=TRANSPORT_ENV,
            settings_getter=lambda s: s.repository.git_transport,
            toml_value=getattr(config, "git_transport", None),
            default="auto",
        )
    )


def configured_repo_url(config: Any = None) -> str:
    """The user's own ``[repository].url`` spelling, or ``""``.

    This is the string the "auto" transport prefers verbatim. ``config``'s own
    ``repo_url`` wins when it is set (the pipeline already resolved the layers);
    otherwise fall back to the env var + settings store, which is all the
    monitors have — they are handed a ``GithubConfig``, which carries no repo
    URL at all.
    """
    explicit = str(getattr(config, "repo_url", "") or "").strip()
    if explicit:
        return explicit
    from backend.config import settings as _s

    return str(
        _s.resolve_str(
            env="MINDFLOCK_REPO_URL",
            settings_getter=lambda s: s.repository.url,
            default="",
        )
        or ""
    ).strip()


def resolve_clone_url(
    slug: str,
    *,
    api_https: Optional[str] = None,
    api_ssh: Optional[str] = None,
    configured_url: str = "",
    transport: str = "auto",
) -> str:
    """The URL to clone the repo named by ``slug`` (``owner/repo``) from.

    * ``api_https`` / ``api_ssh`` — the ``clone_url`` / ``ssh_url`` GitHub's REST
      payloads carry for that repo, when the caller has them.
    * ``configured_url`` — the user's own ``[repository].url`` (or a ticketing
      source's ``repo_url``). Honoured VERBATIM under ``"auto"``, but only when
      it names the same repo as ``slug``: a global default pointing at another
      repo must never be cloned in place of the one we were asked for.
    * ``transport`` — ``"auto"`` | ``"ssh"`` | ``"https"`` (see the module
      docstring); anything else degrades to ``"auto"``.

    Returns ``""`` only when there is nothing to go on at all (no slug, no API
    URL, no configured URL) — callers treat that as "no repo to clone".

    Whatever comes back is still subject to git's own ``url.<base>.insteadOf``
    rewrites, so a user who maps HTTPS onto SSH in their gitconfig keeps working
    on the default settings.
    """
    t = normalize_transport(transport)
    slug = (slug or "").strip().strip("/")
    https = (api_https or "").strip()
    ssh = (api_ssh or "").strip()
    configured = (configured_url or "").strip()
    synthetic = f"https://github.com/{slug}.git" if slug else ""

    # A local path (a canonical clone, a fixture repo) has no forge and no
    # transport to pick — clone it exactly as given.
    if configured and is_local_path(configured):
        return configured

    # The configured URL only speaks for THIS repo when it names it. With no
    # slug to compare against, it is all we have, so take it.
    if configured and (not synthetic or same_repo(configured, synthetic)):
        own = configured
    else:
        own = ""

    if t == "ssh":
        # The API hands us a ready-made ssh_url; otherwise respell whichever
        # spelling we do have.
        return ssh or to_ssh(own or https or synthetic) or own or https or synthetic
    if t == "https":
        return https or to_https(own or ssh or synthetic) or own or ssh or synthetic
    # auto: the user's own spelling wins, then the API's, then a synthesized one.
    return own or https or synthetic


def apply_transport(url: str, transport: str = "auto") -> str:
    """Apply the transport preference to an already-resolved clone ``url``.

    The provisioners are handed a URL (from the ticket, the PR record or the
    config) rather than a slug, and under ``"auto"`` they must clone it exactly
    as given. An explicit ``"ssh"``/``"https"`` respells it; a local path or an
    unparseable URL is always returned untouched.
    """
    u = (url or "").strip()
    if not u:
        return ""
    ref = parse_remote(u)
    return resolve_clone_url(
        ref.slug if ref is not None else "",
        configured_url=u,
        transport=transport,
    )


def headless_git_env(base: Optional[dict] = None) -> dict:
    """Environment for a git call that must fail fast instead of prompting.

    The pipeline clones with nobody at the keyboard, so a credential prompt is
    an infinite hang, not a question. ``GIT_TERMINAL_PROMPT=0`` turns the HTTPS
    username/password prompt into an immediate error, and ``BatchMode=yes``
    does the same for SSH's host-key / passphrase prompts — but only when the
    user has not set ``GIT_SSH_COMMAND`` themselves, since that variable is how
    people point git at a specific key or a jump host.
    """
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not env.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def clone_failure_hint(url: str) -> str:
    """A one-line, actionable hint for a failed clone of ``url``.

    Names the transport that was actually attempted, because "authentication
    failed" reads very differently depending on whether git was asking for a
    PAT or for an SSH key.
    """
    u = (url or "").strip()
    if not u:
        return "no clone URL was configured (set [repository].url)"
    if is_local_path(u):
        return f"cloned from the local path {u} — check it exists and is a git repo"
    ref = parse_remote(u)
    host = ref.host if ref is not None else "the remote"
    # scp-style (git@host:org/repo) has no scheme at all, which is why the
    # "no ://" case counts as SSH here.
    if u.startswith("ssh://") or (ref is not None and "://" not in u):
        return (
            f"cloned over SSH ({u}) — check `ssh -T git@{host}` and that your "
            "key is loaded (`ssh-add -l`), or set "
            '[repository].git_transport = "https"'
        )
    if u.startswith(("https://", "http://")):
        return (
            f"cloned over HTTPS ({u}) — check your git credential helper or "
            f'token for {host}, or set [repository].git_transport = "ssh" to '
            "use your SSH key instead"
        )
    return f"cloned from {u} — check the URL and your git credentials"


async def run_network_git(
    *args: str,
    cwd: Optional[str] = None,
    timeout: float,
    env: Optional[dict] = None,
) -> tuple[int, bytes, bytes]:
    """Run a git command that hits the network, headless.

    Same ``(rc, stdout, stderr)`` contract and rc-124 timeout convention as
    :func:`backend.ticket_ingestion._subprocess.run_capture`, plus the two
    things a clone needs when nobody is watching: the prompt-suppressing
    environment from :func:`headless_git_env`, and a CLOSED stdin so a
    credential helper that reads from it gets EOF instead of blocking forever.
    (``run_capture`` lets the child inherit our stdin, which is exactly the
    hang this avoids.)
    """
    kwargs: dict = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": headless_git_env(env),
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()  # reap the killed process
        except ProcessLookupError:
            pass
        return 124, b"", f"{' '.join(args)} timed out after {timeout:.0f}s".encode()
    return proc.returncode or 0, stdout, stderr
