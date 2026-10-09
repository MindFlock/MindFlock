"""Connect GitHub: one sign-in for opening PRs and for pushing.

Setup's "Connect GitHub" step (and ``/api/github/*``) is built on this module.
Before it, the GitHub token was a field folded under Intake → Pull requests →
Advanced with no link to make one, and a fresh machine had no git identity and
no push credential either — the first push failed in a shell pane nobody was
looking at.

**Three ways in, best first** (no new accounts — the owner registers nothing):

1. *Device flow*, when a GitHub OAuth App client id is configured
   (``github.oauth_client_id`` or ``$MINDFLOCK_GITHUB_CLIENT_ID``): GitHub
   hands out a short code, the person types it at github.com/login/device, and
   :func:`poll_device_flow` collects the token. Nothing to copy.
2. *gh*, when the GitHub CLI is installed: ``gh auth login --web`` runs in a
   login terminal (the same throwaway PTY the agent sign-in uses), then
   :func:`import_gh_token` copies ``gh auth token`` into ``github.token``.
3. *A pre-filled token page* (:data:`TOKEN_URL`: the scopes and a description
   already filled in) and a paste box (:func:`store_token`).

Whichever way, the token lands in ``github.token`` — the field PR review, issue
handling and Make PR already read, and the one settings sync shares with the
user's other devices.

**Pushing** is plain git over the user's own remote. :func:`push_check` asks
the remote for real (a ``push --dry-run`` with ``GIT_TERMINAL_PROMPT=0``, so a
missing credential fails at once), :func:`setup_git_credential` lets git push
over HTTPS with the connected sign-in (``gh auth setup-git``, or MindFlock's
own credential helper for github.com when no helper is set), and
:func:`set_git_identity` writes ``user.name``/``user.email`` to the GLOBAL git
config — each only when the person clicks for it.

Every HTTP call goes through :func:`_http` (stdlib ``urllib``; tests replace
it). Nothing here ever returns a token.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from backend import git_auth_hints

#: Where to make a token by hand: classic PAT, scopes pre-ticked.
TOKEN_URL = (
    "https://github.com/settings/tokens/new?scopes=repo,read:org"
    "&description=MindFlock"
)
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
API_URL = "https://api.github.com"
#: What the device flow asks for: push/PRs on private repos, and org repos.
SCOPES = "repo read:org"
CLIENT_ID_ENV = "MINDFLOCK_GITHUB_CLIENT_ID"

#: The ``gh`` sign-in Setup runs in a login terminal. ``setup-git`` makes gh
#: git's credential helper for github.com, so pushing works right after.
GH_LOGIN_COMMAND = (
    "gh auth login --web --git-protocol https --hostname github.com"
    " && gh auth setup-git"
)
GH_LOGIN_SESSION = "mindflock_login_gh"

_TIMEOUT = 10.0
_USER_TTL = 300.0
_PUSH_TTL = 300.0
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #
def _http(
    method: str,
    url: str,
    *,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = _TIMEOUT,
) -> Tuple[int, Dict[str, str], object]:
    """``(status, headers, json_body)``; ``(0, {}, None)`` when unreachable.
    Form-encodes ``data`` (GitHub's OAuth endpoints take forms)."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "MindFlock")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            status, hdrs = resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as err:
        raw = err.read() if hasattr(err, "read") else b""
        status, hdrs = err.code, dict((err.headers or {}).items())
    except Exception:  # noqa: BLE001 — offline, DNS, TLS, timeout
        return 0, {}, None
    try:
        return status, hdrs, json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        return status, hdrs, None


def _settings():
    from backend.config import settings as settings_store

    return settings_store


def client_id() -> str:
    """The OAuth App client id the device flow uses, or ``""`` (no flow)."""
    env = (os.environ.get(CLIENT_ID_ENV) or "").strip()
    if env:
        return env
    try:
        return (_settings().load_settings().github.oauth_client_id or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def gh_path() -> str:
    return shutil.which("gh") or ""


def _gh_token() -> str:
    """``gh auth token``, or ``""``."""
    if not gh_path():
        return ""
    try:
        cp = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def resolve_token() -> Tuple[str, str]:
    """``(source, token)`` in the order every GitHub consumer resolves it
    (:mod:`backend.ticket_ingestion.github_auth`): Settings, then
    ``$GH_TOKEN``/``$GITHUB_TOKEN``, then ``gh auth token``. ``("none", "")``
    when there is none."""
    try:
        tok = (_settings().load_settings().github.token or "").strip()
    except Exception:  # noqa: BLE001
        tok = ""
    if tok:
        return "settings", tok
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if (os.environ.get(var) or "").strip():
            return "env:" + var, os.environ[var].strip()
    tok = _gh_token()
    if tok:
        return "gh-cli", tok
    return "none", ""


# --------------------------------------------------------------------------- #
# who the token is
# --------------------------------------------------------------------------- #
_USERS: Dict[str, Tuple[float, dict]] = {}


def _fp(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def fetch_user(token: str, *, fresh: bool = False) -> dict:
    """``{"ok", "login", "name", "id", "scopes", "error"}`` for ``token``
    (GET /user; the classic-token scopes come from ``X-OAuth-Scopes``).
    Cached a few minutes per token."""
    if not token:
        return {
            "ok": False,
            "login": "",
            "name": "",
            "id": 0,
            "scopes": [],
            "error": "",
        }
    key = _fp(token)
    hit = _USERS.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < _USER_TTL:
        return dict(hit[1])
    status, hdrs, body = _http(
        "GET", API_URL + "/user", headers={"Authorization": "Bearer " + token}
    )
    lower = {k.lower(): v for k, v in hdrs.items()}
    scopes = [
        s.strip() for s in (lower.get("x-oauth-scopes") or "").split(",") if s.strip()
    ]
    if status == 200 and isinstance(body, dict):
        out = {
            "ok": True,
            "login": str(body.get("login") or ""),
            "name": str(body.get("name") or ""),
            "id": int(body.get("id") or 0),
            "scopes": scopes,
            "error": "",
        }
    elif status == 401:
        out = {
            "ok": False,
            "login": "",
            "name": "",
            "id": 0,
            "scopes": [],
            "error": "GitHub doesn't accept this token (expired or revoked)",
        }
    elif status == 0:
        # Unknown, not wrong: don't cache an offline answer.
        return {
            "ok": False,
            "login": "",
            "name": "",
            "id": 0,
            "scopes": [],
            "error": "couldn't reach GitHub",
        }
    else:
        out = {
            "ok": False,
            "login": "",
            "name": "",
            "id": 0,
            "scopes": [],
            "error": "GitHub answered HTTP %s" % status,
        }
    _USERS[key] = (time.monotonic(), out)
    return dict(out)


def store_token(token: str) -> None:
    """Save ``token`` as ``github.token`` — what the settings save route does
    with a pasted one: every cached copy is dropped, and the edit is stamped
    for settings sync now (it travels to the user's other devices)."""
    token = (token or "").strip()
    _settings().update_settings(github={"token": token})
    try:
        from backend.ticket_ingestion import github_auth as _ingest_auth

        _ingest_auth.invalidate()
    except Exception:  # noqa: BLE001
        pass
    try:
        from backend.web.core import settings_sync

        settings_sync.local_change(["github.token"])
    except Exception:  # noqa: BLE001 — a save must never fail on this
        pass


def paste_token(token: str) -> dict:
    """Check a pasted token with GitHub, then save it. ``{"ok", "login",
    "error"}`` — a token GitHub rejects is not saved; one we couldn't check
    (offline) is, and says so."""
    token = (token or "").strip()
    if not token or len(token) > 400 or any(c.isspace() for c in token):
        return {
            "ok": False,
            "login": "",
            "error": "that doesn't look like a GitHub token",
        }
    who = fetch_user(token, fresh=True)
    if not who["ok"] and who["error"] != "couldn't reach GitHub":
        return {"ok": False, "login": "", "error": who["error"]}
    store_token(token)
    return {
        "ok": True,
        "login": who["login"],
        "error": "" if who["ok"] else who["error"],
    }


def import_gh_token() -> dict:
    """After ``gh auth login``: copy gh's token into ``github.token``."""
    tok = _gh_token()
    if not tok:
        return {"ok": False, "login": "", "error": "gh isn't signed in yet"}
    store_token(tok)
    who = fetch_user(tok)
    return {"ok": True, "login": who.get("login", ""), "error": ""}


# --------------------------------------------------------------------------- #
# git identity + push credential
# --------------------------------------------------------------------------- #
def _git(
    *args: str, cwd: Optional[str] = None, timeout: float = 10.0
) -> Tuple[int, str, str]:
    env = dict(os.environ, **git_auth_hints.NO_PROMPT_ENV)
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as err:
        return 127, "", str(err)
    return cp.returncode, cp.stdout.strip(), cp.stderr.strip()


def git_identity() -> dict:
    """``{"name", "email"}`` from the GLOBAL git config (``""`` when unset)."""
    if not shutil.which("git"):
        return {"name": "", "email": ""}
    _rc, name, _ = _git("config", "--global", "--get", "user.name")
    _rc, email, _ = _git("config", "--global", "--get", "user.email")
    return {"name": name, "email": email}


def suggested_identity(user: dict) -> dict:
    """What Setup pre-fills from the GitHub account: its display name (else
    the login) and its private ``id+login@users.noreply.github.com``."""
    login = str(user.get("login") or "")
    if not login:
        return {"name": "", "email": ""}
    uid = int(user.get("id") or 0)
    email = (
        "%d+%s@users.noreply.github.com" % (uid, login)
        if uid
        else "%s@users.noreply.github.com" % login
    )
    return {"name": str(user.get("name") or "") or login, "email": email}


def set_git_identity(name: str, email: str) -> dict:
    """Write ``user.name``/``user.email`` to the GLOBAL git config. Only ever
    called from the person's own click (Setup), never on its own."""
    name, email = (name or "").strip(), (email or "").strip()
    if not name or not email or "@" not in email or len(name) > 200 or len(email) > 200:
        return {"ok": False, "error": "give a name and an email address"}
    if any(c in name + email for c in "\n\r\0"):
        return {"ok": False, "error": "no line breaks, please"}
    if not shutil.which("git"):
        return {"ok": False, "error": "git isn't installed"}
    for key, val in (("user.name", name), ("user.email", email)):
        rc, _out, err = _git("config", "--global", key, val)
        if rc != 0:
            return {"ok": False, "error": err or "git config failed"}
    return {"ok": True, "error": "", **git_identity()}


def credential_helper() -> str:
    """git's effective credential helper for https://github.com, or ``""``."""
    rc, out, _ = _git(
        "config", "--get-urlmatch", "credential.helper", "https://github.com"
    )
    return out.splitlines()[-1].strip() if rc == 0 and out else ""


def _mindflock_bin() -> str:
    return shutil.which("mindflock") or ""


def setup_git_credential() -> dict:
    """Let git push to github.com over HTTPS with the connected sign-in.

    ``gh auth setup-git`` when gh is signed in (gh is then git's helper);
    otherwise MindFlock's own helper (``mindflock git-credential``, which
    answers only for https://github.com, with ``github.token``) — and only
    when git has no helper for GitHub already: an existing one (the OS
    keychain, Git Credential Manager) is the user's, never replaced."""
    if gh_path() and _gh_token():
        try:
            cp = subprocess.run(
                ["gh", "auth", "setup-git", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as err:
            return {"ok": False, "helper": "", "error": str(err)}
        if cp.returncode == 0:
            return {"ok": True, "helper": "gh", "error": ""}
        return {
            "ok": False,
            "helper": "",
            "error": cp.stderr.strip() or "gh auth setup-git failed",
        }
    existing = credential_helper()
    if existing:
        return {
            "ok": False,
            "helper": existing,
            "error": "git already has a credential helper for GitHub (%s) — "
            "sign in through it, or remove it first" % existing,
        }
    if resolve_token()[0] == "none":
        return {"ok": False, "helper": "", "error": "connect GitHub first"}
    exe = _mindflock_bin()
    if not exe:
        return {
            "ok": False,
            "helper": "",
            "error": "the mindflock command isn't on PATH",
        }
    helper = "!%s git-credential" % exe.replace(" ", "\\ ")
    rc, _out, err = _git(
        "config", "--global", "credential.https://github.com.helper", helper
    )
    if rc != 0:
        return {"ok": False, "helper": "", "error": err or "git config failed"}
    return {"ok": True, "helper": "mindflock", "error": ""}


def credential_answer(stdin_text: str) -> str:
    """The ``get`` reply of ``mindflock git-credential`` for git's request
    ``stdin_text`` (``key=value`` lines): the token for https://github.com,
    nothing for any other host (git then asks its next helper)."""
    req: Dict[str, str] = {}
    for line in (stdin_text or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            req[k.strip()] = v.strip()
    if (
        req.get("host", "").lower() != "github.com"
        or req.get("protocol", "https") != "https"
    ):
        return ""
    _source, tok = resolve_token()
    if not tok:
        return ""
    return "username=x-access-token\npassword=%s\n" % tok


_PUSH: Dict[str, Tuple[float, dict]] = {}
_PUSH_RUNNING: set = set()


def push_check(repo: str) -> dict:
    """Can this computer push to ``repo``'s origin? Asks the remote for real:
    a ``push --dry-run`` (it authenticates like a push, changes nothing),
    with prompts off so a missing credential fails at once.

    ``{"ok": True|False|None, "repo", "remote", "id", "message", "fix"}`` —
    ``None`` when there is nothing to ask (no repo, no origin) or the network
    gave no answer."""
    out = {
        "ok": None,
        "repo": repo or "",
        "remote": "",
        "id": "",
        "message": "",
        "fix": "",
    }
    if not repo or not os.path.isdir(repo) or not shutil.which("git"):
        out["message"] = "no repository to check yet"
        return out
    rc, url, _ = _git("-C", repo, "remote", "get-url", "origin")
    if rc != 0 or not url:
        out["message"] = "no origin remote in %s" % repo
        return out
    out["remote"] = url
    rc, _o, _e = _git("-C", repo, "rev-parse", "--verify", "-q", "HEAD")
    target = ["HEAD:refs/heads/mindflock-push-check"] if rc == 0 else []
    if target:
        rc, so, se = _git(
            "-C",
            repo,
            "push",
            "--dry-run",
            "--no-verify",
            "--porcelain",
            "origin",
            *target,
            timeout=20.0,
        )
    else:  # no commit to offer: reading is the best we can ask
        rc, so, se = _git("-C", repo, "ls-remote", "--heads", "origin", timeout=20.0)
    if rc == 0:
        out.update(ok=True, message="can push to %s" % url)
        return out
    hint = git_auth_hints.classify(se + "\n" + so)
    if hint:
        out.update(ok=False, id=hint["id"], message=hint["message"], fix=hint["fix"])
    elif rc == 124:
        out.update(message="the remote didn't answer in time")
    else:
        out.update(
            ok=False, id="other", message=(se or so or "push check failed")[:300]
        )
    return out


def push_check_cached(repo: str, *, start: bool = True) -> Optional[dict]:
    """The last :func:`push_check` of ``repo`` when fresh, else ``None`` —
    and (``start``) a background check so the next ask has one. For the
    readiness summary, which must never wait on the network."""
    hit = _PUSH.get(repo)
    if hit and time.monotonic() - hit[0] < _PUSH_TTL:
        return dict(hit[1])
    if start and repo and repo not in _PUSH_RUNNING:
        _PUSH_RUNNING.add(repo)

        def run():
            try:
                _PUSH[repo] = (time.monotonic(), push_check(repo))
            finally:
                _PUSH_RUNNING.discard(repo)

        threading.Thread(target=run, daemon=True, name="push-check").start()
    return dict(hit[1]) if hit else None


def remember_push_check(repo: str, result: dict) -> None:
    _PUSH[repo] = (time.monotonic(), dict(result))


def remembered_repo() -> str:
    try:
        return str(_settings().load_settings().general.last_repo_path or "")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def status(*, check_user: bool = True) -> dict:
    """Everything the "Connect GitHub" step shows. Never the token."""
    source, tok = resolve_token()
    gh = gh_path()
    gh_authed = bool(gh) and (source == "gh-cli" or bool(_gh_token()))
    user = fetch_user(tok) if (tok and check_user) else {}
    ident = git_identity()
    methods: List[str] = []
    if client_id():
        methods.append("device")
    if gh:
        methods.append("gh")
    methods.append("token")
    return {
        "connected": bool(tok),
        "source": source,
        "login": user.get("login", ""),
        "scopes": user.get("scopes", []),
        "user_error": user.get("error", ""),
        "gh": {"installed": bool(gh), "authenticated": bool(gh_authed)},
        "methods": methods,
        "token_url": TOKEN_URL,
        "identity": ident,
        "identity_suggested": (
            suggested_identity(user) if user.get("ok") else {"name": "", "email": ""}
        ),
        "credential_helper": credential_helper() if shutil.which("git") else "",
        "flow": flow_view(),
    }


# --------------------------------------------------------------------------- #
# device flow — one at a time, per server
# --------------------------------------------------------------------------- #
#: ``state``: idle → pending → done | expired | denied | error.
_FLOW: dict = {"state": "idle"}


def flow_view() -> dict:
    """The flow as the client sees it (never the device code)."""
    f = _FLOW
    return {
        "state": f.get("state", "idle"),
        "user_code": f.get("user_code", ""),
        "verification_uri": f.get("verification_uri", ""),
        "expires_at": f.get("expires_at", 0.0),
        "interval": f.get("interval", 0),
        "login": f.get("login", ""),
        "error": f.get("error", ""),
    }


def start_device_flow() -> dict:
    """Ask GitHub for a user code. ``{"ok", ...flow_view(), "error"}``."""
    cid = client_id()
    if not cid:
        return {
            "ok": False,
            **flow_view(),
            "error": "no GitHub OAuth App is configured",
        }
    status_code, _h, body = _http(
        "POST", DEVICE_CODE_URL, data={"client_id": cid, "scope": SCOPES}
    )
    if status_code != 200 or not isinstance(body, dict) or not body.get("device_code"):
        err = (body or {}).get("error_description") if isinstance(body, dict) else ""
        return {
            "ok": False,
            **flow_view(),
            "error": err
            or (
                "couldn't reach GitHub"
                if not status_code
                else "GitHub answered HTTP %s" % status_code
            ),
        }
    now = time.time()
    with _LOCK:
        _FLOW.clear()
        _FLOW.update(
            state="pending",
            client_id=cid,
            device_code=str(body["device_code"]),
            user_code=str(body.get("user_code") or ""),
            verification_uri=str(
                body.get("verification_uri") or "https://github.com/login/device"
            ),
            expires_at=now + int(body.get("expires_in") or 900),
            interval=max(1, int(body.get("interval") or 5)),
            next_poll=now + max(1, int(body.get("interval") or 5)),
            login="",
            error="",
        )
    return {"ok": True, **flow_view()}


def poll_device_flow(now: Optional[float] = None) -> dict:
    """Advance the flow by at most one request to GitHub, never faster than
    the interval GitHub set (``slow_down`` adds to it). On success the token
    is saved (:func:`store_token`) and the flow reads ``done``."""
    now = time.time() if now is None else now
    with _LOCK:
        if _FLOW.get("state") != "pending":
            return flow_view()
        if now >= _FLOW["expires_at"]:
            _FLOW.update(
                state="expired", device_code="", error="the code expired — start again"
            )
            return flow_view()
        if now < _FLOW["next_poll"]:
            return flow_view()
        _FLOW["next_poll"] = now + _FLOW["interval"]
        code, cid = _FLOW["device_code"], _FLOW["client_id"]
    status_code, _h, body = _http(
        "POST",
        ACCESS_TOKEN_URL,
        data={
            "client_id": cid,
            "device_code": code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    body = body if isinstance(body, dict) else {}
    token = str(body.get("access_token") or "")
    err = str(body.get("error") or "")
    if token:
        store_token(token)
        login = fetch_user(token).get("login", "")
    with _LOCK:
        if _FLOW.get("device_code") != code:  # cancelled or restarted meanwhile
            return flow_view()
        if token:
            _FLOW.update(state="done", device_code="", login=login, error="")
        elif err == "authorization_pending" or (not status_code and not err):
            pass  # keep waiting (a network blip is retried next interval)
        elif err == "slow_down":
            _FLOW["interval"] = max(
                _FLOW["interval"] + 5, int(body.get("interval") or 0)
            )
            _FLOW["next_poll"] = now + _FLOW["interval"]
        elif err == "expired_token":
            _FLOW.update(
                state="expired", device_code="", error="the code expired — start again"
            )
        elif err == "access_denied":
            _FLOW.update(
                state="denied", device_code="", error="sign-in was cancelled on GitHub"
            )
        else:
            _FLOW.update(
                state="error",
                device_code="",
                error=str(
                    body.get("error_description") or err or "HTTP %s" % status_code
                ),
            )
        return flow_view()


def cancel_device_flow() -> dict:
    with _LOCK:
        _FLOW.clear()
        _FLOW["state"] = "idle"
    return flow_view()
