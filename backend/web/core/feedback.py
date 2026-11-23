"""Feedback: report a problem or an idea about MindFlock itself
(docs/web-api.md "Feedback", docs/mcp.md ``report_issue``).

Two ways in, one way out:

* the person opens the feedback box in the app and types; or
* an agent they are talking to calls the MCP's ``report_issue``, which only
  writes a DRAFT here (:func:`add_draft`). The app shows it in the same box.

Nothing leaves this machine until the person presses Send. Send
(:func:`send`) files a GitHub issue on :data:`REPO` with the GitHub sign-in
MindFlock already has; without one (or when GitHub refuses that token) it
answers with a pre-filled ``issues/new`` link for the browser instead. Either
way the issue is PUBLIC, so:

* an agent's draft is scrubbed when it is stored (:func:`scrub`: the home
  folder becomes ``~``, anything shaped like a key or token is replaced), and
  the person reads and may edit it before sending;
* what is sent is scrubbed for keys again, whoever wrote it;
* the only thing added by MindFlock is one line the box shows beforehand
  (:func:`about_line`: the version and the operating system), and it can be
  unticked.

Drafts live in ``<mindflock home>/feedback_drafts.json`` (at most
:data:`MAX_DRAFTS`; the oldest goes first) so one survives a restart.
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from backend.config import config
from backend.fsutil import atomic_write_json

__all__ = [
    "REPO",
    "KINDS",
    "FeedbackError",
    "about_line",
    "add_draft",
    "compose",
    "drafts",
    "issue_url",
    "remove_draft",
    "scrub",
    "send",
]

#: Where reports go.
REPO = "MindFlock/MindFlock"
KINDS = ("bug", "idea", "feedback")
#: The GitHub label asked for per kind (GitHub drops a label the reporter may
#: not set, so this never fails a report).
_LABELS = {"bug": ["bug"], "idea": ["enhancement"], "feedback": []}

MAX_TITLE = 200
MAX_BODY = 20000
MAX_DRAFTS = 20
#: A pre-filled issues/new link longer than this is cut (browsers and GitHub
#: both refuse very long URLs).
MAX_URL = 6000
FILE = "feedback_drafts.json"

_LOCK = threading.Lock()


class FeedbackError(ValueError):
    """Bad input: the message is a sentence for the person."""


# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #
#: Shapes of credentials. Each match becomes ``[removed]``.
_SECRET_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),
    re.compile(r"(?i)\b(bearer|token|authorization:)\s+[A-Za-z0-9_\-\.=]{20,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.S,
    ),
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:secret|password|passwd|api[_-]?key|token)[A-Z0-9_]*)"
        r"(\s*[=:]\s*)(['\"]?)[^\s'\"]{8,}\3"
    ),
)
#: A user's folder in a path: the name after it is a person's login name.
_USER_DIR_RE = re.compile(r"(?i)(/home/|/Users/|[A-Z]:\\Users\\)([^/\\\s:'\"]+)")
REMOVED = "[removed]"


def _home() -> str:
    try:
        return os.path.expanduser("~")
    except Exception:  # noqa: BLE001
        return ""


def scrub(text: str, *, paths: bool = True) -> Tuple[str, int]:
    """``text`` with credentials replaced by ``[removed]`` and, with
    ``paths``, the home folder written as ``~`` and any other user's folder
    name as ``<user>``. Returns ``(text, how many things were replaced)``."""
    n = 0
    out = str(text or "")

    def _key(m: "re.Match[str]") -> str:
        nonlocal n
        n += 1
        # NAME=value keeps the name: it says what was there without the value.
        if m.re is _SECRET_RES[-1]:
            return "%s%s%s" % (m.group(1), m.group(2), REMOVED)
        if m.re is _SECRET_RES[7]:
            return "%s %s" % (m.group(1), REMOVED)
        return REMOVED

    for rx in _SECRET_RES:
        out = rx.sub(_key, out)
    if paths:
        home = _home()
        if home and len(home) > 3 and home in out:
            n += out.count(home)
            out = out.replace(home, "~")

        def _user(m: "re.Match[str]") -> str:
            nonlocal n
            n += 1
            return "%s<user>" % m.group(1)

        out = _USER_DIR_RE.sub(_user, out)
    return out, n


# --------------------------------------------------------------------------- #
# What MindFlock adds
# --------------------------------------------------------------------------- #
def _os_name() -> str:
    system = platform.system() or "unknown OS"
    if system == "Darwin":
        ver = platform.mac_ver()[0]
        return "macOS %s" % ver if ver else "macOS"
    if system == "Linux":
        try:
            if "microsoft" in platform.release().lower():
                return "Linux (WSL)"
        except Exception:  # noqa: BLE001
            pass
        return "Linux"
    if system == "Windows":
        rel = platform.release()
        return "Windows %s" % rel if rel else "Windows"
    return system


def about_line() -> str:
    """The one line MindFlock adds to a report (shown in the box first)."""
    try:
        from backend import __version__ as version
    except Exception:  # noqa: BLE001
        version = ""
    parts = ["MindFlock %s" % (("v" + version) if version else "(unknown version)")]
    parts.append(_os_name())
    return " · ".join(parts)


def _clean_title(title: Any) -> str:
    return " ".join(str(title or "").split())[:MAX_TITLE]


def _clean_kind(kind: Any) -> str:
    k = str(kind or "").strip().lower()
    return k if k in KINDS else "feedback"


def compose(
    title: Any, body: Any, kind: Any = "feedback", *, include_about: bool = True
) -> Dict[str, Any]:
    """The exact issue that would be filed: ``{title, body, kind, labels,
    removed}``. Raises :class:`FeedbackError` when there is nothing to send."""
    t, n1 = scrub(_clean_title(title), paths=False)
    b, n2 = scrub(str(body or "").strip()[:MAX_BODY], paths=False)
    k = _clean_kind(kind)
    if not t and b:
        # A feedback box with only a message: its first line is the title.
        first = b.splitlines()[0].strip()
        t = first[:80] + ("…" if len(first) > 80 else "")
    if not t:
        raise FeedbackError("Write a line or two about it first.")
    if include_about:
        b = (b + "\n\n---\n" if b else "") + about_line()
    return {"title": t, "body": b, "kind": k, "labels": _LABELS[k], "removed": n1 + n2}


def issue_url(title: str, body: str, labels: Optional[List[str]] = None) -> str:
    """A pre-filled ``issues/new`` link (the browser way)."""
    base = "https://github.com/%s/issues/new" % REPO

    def build(b: str) -> str:
        q = {"title": title, "body": b}
        if labels:
            q["labels"] = ",".join(labels)
        return base + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)

    url = build(body)
    cut = len(body)
    note = "\n\n(cut to fit a link: paste the rest here)"
    while len(url) > MAX_URL and cut > 0:
        cut = max(0, cut - max(200, (len(url) - MAX_URL) // 3))
        url = build(body[:cut].rstrip() + note)
    return url


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #
def _path() -> str:
    return os.path.join(config.GetConfigDir(), FILE)


def _load() -> List[dict]:
    import json

    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = data.get("drafts") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and r.get("id")]


def _save(rows: List[dict]) -> None:
    atomic_write_json(_path(), {"drafts": rows})


def drafts() -> List[dict]:
    """The drafts waiting for the person, newest first."""
    with _LOCK:
        return sorted(_load(), key=lambda r: -float(r.get("created") or 0))


def add_draft(
    title: Any, body: Any, kind: Any = "bug", *, session: str = "", agent: str = ""
) -> dict:
    """Store an agent's draft (scrubbed) and return it. A draft with the same
    title replaces the older one, so an agent that retries doesn't pile up."""
    t, n1 = scrub(_clean_title(title))
    b, n2 = scrub(str(body or "").strip()[:MAX_BODY])
    if not t:
        raise FeedbackError("a report needs a title")
    if not b:
        raise FeedbackError(
            "a report needs a body: what happened and what you expected"
        )
    rec = {
        "id": secrets.token_hex(4),
        "title": t,
        "body": b,
        "kind": _clean_kind(kind),
        "session": str(session or "")[:200],
        "agent": str(agent or "")[:40],
        "created": time.time(),
        "removed": n1 + n2,
    }
    with _LOCK:
        rows = [r for r in _load() if r.get("title") != t]
        rows.append(rec)
        rows.sort(key=lambda r: float(r.get("created") or 0))
        _save(rows[-MAX_DRAFTS:])
    return rec


def remove_draft(draft_id: str) -> bool:
    with _LOCK:
        rows = _load()
        keep = [r for r in rows if r.get("id") != draft_id]
        if len(keep) == len(rows):
            return False
        _save(keep)
        return True


# --------------------------------------------------------------------------- #
# Send
# --------------------------------------------------------------------------- #
def _why(status: int, data: Any) -> str:
    """Why GitHub didn't take the issue, in words."""
    if status == 0:
        return "GitHub couldn't be reached from here"
    if status == 401:
        return "the saved GitHub sign-in is no longer valid"
    if status in (403, 404):
        return "the saved GitHub sign-in can't file issues on %s" % REPO
    if status == 410:
        return "issues are turned off on %s" % REPO
    msg = data.get("message") if isinstance(data, dict) else ""
    return "GitHub answered %d%s" % (status, (": %s" % msg) if msg else "")


async def send(
    title: Any,
    body: Any,
    kind: Any = "feedback",
    *,
    include_about: bool = True,
    draft_id: str = "",
) -> Dict[str, Any]:
    """File the report. ``{"ok": True, "via": "github", "url", "number"}`` when
    GitHub took it; ``{"ok": True, "via": "browser", "url", "why"}`` when the
    person has to press Submit on a pre-filled page instead. The draft it came
    from is removed only when GitHub took it."""
    from backend.web.core import github_pr as _pr

    issue = compose(title, body, kind, include_about=include_about)
    token = await _pr.api_token()
    why = "MindFlock isn't signed in to GitHub"
    if token:
        payload: Dict[str, Any] = {"title": issue["title"], "body": issue["body"]}
        if issue["labels"]:
            payload["labels"] = issue["labels"]
        status, data = await _pr._request(
            "POST", "/repos/%s/issues" % REPO, token=token, body=payload
        )
        if status == 201 and isinstance(data, dict) and data.get("html_url"):
            if draft_id:
                remove_draft(draft_id)
            return {
                "ok": True,
                "via": "github",
                "url": str(data["html_url"]),
                "number": data.get("number"),
                "removed": issue["removed"],
            }
        why = _why(status, data)
    return {
        "ok": True,
        "via": "browser",
        "url": issue_url(issue["title"], issue["body"], issue["labels"]),
        "why": why,
        "removed": issue["removed"],
    }
