"""CodeRabbit-specific reading of a PR (docs/pr-review-rounds.md "CodeRabbit's
review bodies").

CodeRabbit posts most findings as inline review threads, but three kinds
live ONLY in its review's body, each in a ``<details>`` block, per file:

- "Nitpick comments": small suggestions on changed lines;
- "Outside diff range comments": findings on lines the diff does not touch
  (GitHub can't take them inline);
- "Duplicate comments": a finding it already made that still applies.

Each item there names a file and a line range (`` `12-14`: ``), a bold title,
a description and usually a "Prompt to fix issue" / "Prompt for AI Agents"
block. Nothing on GitHub tracks them — no thread to resolve — so what marks
one handled is its FINGERPRINT (:func:`fingerprint`: file + normalised
title), the same for the item whatever review repeats it: a request key
(``n:<fingerprint>``) a review loop or PR intake already handed over is never
handed again, and an item whose fingerprint matches one of CodeRabbit's
inline threads (open or resolved) is that thread, never a second request.

Only the NEWEST CodeRabbit review on the PR's current head that has a body
is read (:func:`newest_review`): an older review's items were about an older
commit, and CodeRabbit's thread-reply "reviews" have empty bodies.

The rest of the module is pure (no I/O); ``pr_comments`` does the fetching.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional


def is_coderabbit(login: str) -> bool:
    """``login`` is CodeRabbit (``coderabbitai``, ``coderabbitai[bot]``)."""
    return "coderabbit" in str(login or "").lower()


#: The three body blocks that carry items, by the words in their summary
#: ("🧹 Nitpick comments (3)" / "Nitpick comments: 3").
_SECTION_RE = re.compile(
    r"\b(nitpick|outside diff range|duplicate) comments\b", re.IGNORECASE
)
#: A file block's summary: "path/to/file.py: 2" or "path/to/file.py (2)".
_FILE_RE = re.compile(r"^(?P<path>\S.*?)(?::\s*\d+|\s*\(\d+\))\s*$")
#: Where an item starts: its line range, `` `12-14`: `` or `` `12`: ``.
_ITEM_RE = re.compile(r"(?m)^[ \t]*`(?P<a>\d+)(?:-(?P<b>\d+))?`\s*:[ \t]*")
_DETAILS_TAG_RE = re.compile(r"<(/?)details\b[^>]*>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"\s*<summary\b[^>]*>(?P<s>.*?)</summary>", re.I | re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PICTURE_RE = re.compile(r"<picture\b.*?</picture>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BOLD_RE = re.compile(r"\*\*(?P<t>.+?)\*\*")
#: A bold run that is a severity / category badge, not the item's title.
_BADGE_RE = re.compile(
    r"^(trivial|minor|major|critical|nitpick|potential issue|refactor "
    r"suggestion|quick win|info|warning|caution|note)$",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*$")
#: How much of an item's description (when it has no prompt block) is kept.
_TEXT_MAX = 2000


@dataclass(frozen=True)
class BodyItem:
    """One actionable item from a CodeRabbit review body."""

    section: str  # "nitpick" | "outside diff range" | "duplicate"
    path: str
    line: Optional[int]
    end_line: Optional[int]
    title: str
    #: What the agent is handed: the title, then the item's own prompt for an
    #: AI agent when it has one, else its description.
    text: str
    #: :func:`fingerprint` of (path, title): its identity across reviews.
    key: str


def _plain(text: str) -> str:
    out = _HTML_COMMENT_RE.sub(" ", text or "")
    out = _PICTURE_RE.sub(" ", out)
    out = _TAG_RE.sub(" ", out)
    return " ".join(out.split())


def _norm(text: str) -> str:
    """Lowercase words only: the part of a title that survives re-wording of
    markup, punctuation and emoji between two reviews."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", _plain(text).lower()).split())


def title_of(body: str) -> str:
    """The item's / comment's title: its first bold run that is not a
    severity badge ("**Minor** · **Quick win**" rows and icon-only runs are
    skipped). "" when it has none."""
    text = _HTML_COMMENT_RE.sub(" ", body or "")
    for line in text.splitlines():
        if "·" in line:  # "**Trivial** · **Maintainability** · **Quick win**"
            continue
        for m in _BOLD_RE.finditer(line):
            t = _plain(m.group("t")).strip(" _*`")
            if t and not _BADGE_RE.match(t.strip(" .:!")):
                return t
    return ""


def fingerprint(path: str, body: str) -> str:
    """A finding's identity: its file and its normalised title (the first
    160 characters of its text when it has no title). The same for an inline
    comment and a body item that say the same thing, and for one item that
    two reviews repeat."""
    title = title_of(body)
    words = _norm(title) if title else _norm(body)[:160]
    raw = "%s\n%s" % (str(path or "").strip().lower(), words)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _details(text: str) -> list:
    """The ``<details>`` blocks of ``text`` as a tree of
    ``{"inner", "end", "kids"}`` (offsets of the block's inside); a block
    left unclosed is dropped."""
    stack: list = []
    roots: list = []
    for m in _DETAILS_TAG_RE.finditer(text):
        if not m.group(1):
            stack.append({"inner": m.end(), "end": -1, "kids": []})
            continue
        if not stack:
            continue
        node = stack.pop()
        node["end"] = m.start()
        (stack[-1]["kids"] if stack else roots).append(node)
    return roots


def _summary(text: str, node: dict) -> tuple:
    """``(summary text, offset just past </summary>)`` of a block."""
    m = _SUMMARY_RE.match(text, node["inner"], node["end"])
    if m is None:
        return "", node["inner"]
    return _plain(m.group("s")), m.end()


def _walk(nodes: list) -> Iterable[dict]:
    for n in nodes:
        yield n
        yield from _walk(n["kids"])


def _prompt_of(chunk: str) -> str:
    """The item's own prompt for an AI agent ("Prompt to fix issue" /
    "Prompt for AI Agents" block), fences and markup dropped; "" if none."""
    for node in _walk(_details(chunk)):
        summary, start = _summary(chunk, node)
        if "prompt" not in summary.lower() or "all" in summary.lower().split():
            continue
        inner = _HTML_COMMENT_RE.sub("", chunk[start : node["end"]])
        inner = _TAG_RE.sub("", inner)
        lines = [ln.rstrip() for ln in inner.splitlines() if not _FENCE_RE.match(ln)]
        return "\n".join(lines).strip()
    return ""


def _description(chunk: str) -> str:
    """The item's text without its nested blocks, markup or badge rows."""
    nodes = _details(chunk)
    keep, at = [], 0
    for n in nodes:  # cut every nested block (prompts, committable diffs)
        open_at = chunk.rfind("<details", 0, n["inner"])
        keep.append(chunk[at : max(open_at, at)])
        at = n["end"] + len("</details>")
    keep.append(chunk[at:])
    text = _HTML_COMMENT_RE.sub("", "".join(keep))
    text = _PICTURE_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if "·" not in ln or "**" not in ln]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out[:_TEXT_MAX]


def _items_of_file(section: str, path: str, chunk: str) -> List[BodyItem]:
    starts = list(_ITEM_RE.finditer(chunk))
    out: List[BodyItem] = []
    for k, m in enumerate(starts):
        end = starts[k + 1].start() if k + 1 < len(starts) else len(chunk)
        part = chunk[m.end() : end]
        # The item ends where its file block's own markup resumes.
        part = re.split(r"(?m)^\s*-{3,}\s*$", part)[0]
        title = title_of(part)
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else None
        prompt = _prompt_of(part)
        body = prompt or _description(part)
        if title and not body.lstrip("* ").startswith(title):
            body = "**%s**\n\n%s" % (title, body) if body else "**%s**" % title
        if not body.strip():
            continue
        out.append(
            BodyItem(
                section=section,
                path=path,
                line=a,
                end_line=b,
                title=title,
                text=body.strip(),
                key=fingerprint(path, part),
            )
        )
    return out


def body_items(body: str) -> List[BodyItem]:
    """Every item in a CodeRabbit review body's "Nitpick comments",
    "Outside diff range comments" and "Duplicate comments" blocks, in order,
    one per fingerprint (a "Duplicate" that repeats a nitpick in the same
    body is the nitpick). The older format's quoted (``> ``) caution block is
    read like the rest."""
    text = re.sub(r"(?m)^> ?", "", str(body or ""))
    out: List[BodyItem] = []
    seen: set = set()
    for node in _walk(_details(text)):
        summary, _ = _summary(text, node)
        m = _SECTION_RE.search(summary)
        if m is None:
            continue
        section = m.group(1).lower()
        for kid in node["kids"]:
            fsum, start = _summary(text, kid)
            fm = _FILE_RE.match(fsum)
            if fm is None:
                continue
            for item in _items_of_file(
                section, fm.group("path").strip(), text[start : kid["end"]]
            ):
                if item.key not in seen:
                    seen.add(item.key)
                    out.append(item)
    return out


def _ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def newest_review(reviews: list, head: str = "") -> Optional[dict]:
    """CodeRabbit's newest submitted review with a body on ``head`` (any
    commit when ``head`` is ""), from GitHub's ``/pulls/{n}/reviews`` rows;
    None when it has none there. Its thread-reply reviews (empty bodies) and
    pending ones never count."""
    best, best_at = None, (-1.0, -1)
    for r in reviews or []:
        if not isinstance(r, dict) or not isinstance(r.get("id"), int):
            continue
        if not is_coderabbit((r.get("user") or {}).get("login") or ""):
            continue
        if str(r.get("state") or "").upper() == "PENDING":
            continue
        if not str(r.get("body") or "").strip():
            continue
        if head and str(r.get("commit_id") or "") != head:
            continue
        at = (_ts(r.get("submitted_at")), r["id"])
        if at > best_at:
            best, best_at = r, at
    return best
