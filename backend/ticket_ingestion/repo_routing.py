"""Which repo(s) a ticket needs, when its source spans several.

A ticketing source used to point at ONE repo, but one team's tickets rarely
live in one repo: a board covers the backend, the frontend and a scanner, and a
ticket may need two of them ("add the endpoint, then call it from the UI"). A
source can now hold a SET of repos (``TicketProviderConfig.repos``), and this is
the first step a ticket takes before it launches: pick the repo(s) it needs.

The order, cheapest and most predictable first:

1. **One repo** — nothing to decide; nothing changes (no routing, no record).
   **A repo picked on the Intake row** (``method = "manual"``) comes next and
   outranks everything below: a person chose it, so it sticks for that ticket.
2. **Labels** — a ticket label that names a repo: its full name
   (``vnext-frontend``), a word of its name no other repo of the source shares
   (``frontend``), or one of its hint words. Labels are what a person puts on a
   ticket on purpose, so every repo a label names is routed to — two labels for
   two repos is a two-repo ticket.
3. **Title** — the same words in the ticket's title. Exactly one repo named is
   a decision; several is ambiguous and goes on to the model.
4. **The agent CLI** — the one-shot ``claude -p`` / ``codex exec`` path every
   other MindFlock model question uses (:mod:`backend.web.core.commit_message`),
   given the ticket and each repo's name, hint and README opening. A decision it
   made before for the same ticket is reused (the session it started lives in
   that repo), unless a label or the title now says otherwise.
5. **Fallback** — the repo the title named first, else the source's first repo.

A decision is recorded per ticket (state.json ``ticket_routes``) with a one-line
reason, which the Intake ticket row shows (``→ vnext-frontend: label
'frontend'``). The session starts in the PRIMARY repo; any other repo the ticket
needs goes into the prompt, by absolute path, with instructions to make that
change on its own branch and open its own PR there (:func:`other_repos_section`)
— one session, not an orchestration of several.

Nothing here raises into a launch: every failure degrades to the fallback.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from backend.ticket_ingestion.config import RepoTarget

_logger = logging.getLogger(__name__)

#: How long the agent CLI gets to answer. A cold CLI start plus one short model
#: turn is ~10s; this only runs once per ticket, off the event loop.
LLM_TIMEOUT = 90.0
#: How much of the ticket description goes into the routing question.
DESCRIPTION_BUDGET = 3000
#: How much of a repo's README opening describes it to the model.
README_BUDGET = 240
#: Name words too generic to say anything about a repo on their own.
_GENERIC_WORDS = frozenset({"the", "and", "app", "repo", "main", "code", "src", "lib"})

#: ``method`` values a decision can carry.
METHOD_LABEL = "label"
METHOD_TITLE = "title"
METHOD_LLM = "llm"
METHOD_FALLBACK = "fallback"
#: Picked by a person on the Intake row (``POST /api/tickets/start`` ``repo``).
METHOD_MANUAL = "manual"

Ask = Callable[[str], str]


@dataclass
class RouteDecision:
    """Where a ticket goes: ``repos`` (URLs, primary first), why, and how."""

    repos: list[str]
    reason: str
    method: str
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def primary(self) -> str:
        return self.repos[0] if self.repos else ""

    @property
    def extra(self) -> list[str]:
        return self.repos[1:]

    def summary(self) -> str:
        """``vnext-frontend: label 'frontend'`` — the row's one line."""
        names = " + ".join(repo_name(u) for u in self.repos)
        return f"{names}: {self.reason}" if self.reason else names

    def to_dict(self) -> dict:
        return {
            "repos": list(self.repos),
            "reason": self.reason,
            "method": self.method,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Optional["RouteDecision"]:
        if not isinstance(d, dict):
            return None
        repos = [str(u) for u in (d.get("repos") or []) if str(u or "").strip()]
        if not repos:
            return None
        return cls(
            repos=repos,
            reason=str(d.get("reason") or ""),
            method=str(d.get("method") or ""),
            at=str(d.get("at") or ""),
        )


def targets_of(src) -> list[RepoTarget]:
    """``src.repo_targets()``, tolerating a source object without it (a
    hand-built stand-in that only carries ``repo_url``)."""
    fn = getattr(src, "repo_targets", None)
    if callable(fn):
        return list(fn())
    url = str(getattr(src, "repo_url", "") or "").strip()
    return [RepoTarget(url=url)] if url else []


# --------------------------------------------------------------------------- #
# Names and keywords
# --------------------------------------------------------------------------- #
def repo_name(url: str) -> str:
    """The bare repo name behind a clone URL or local path (``vnext-frontend``)."""
    try:
        from backend.session.provisioned import repo_display_name

        name = repo_display_name(url)
        if name:
            return name
    except Exception:  # noqa: BLE001 — the engine half may be missing
        pass
    tail = (url or "").strip().rstrip("/").split("/")[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _name_words(name: str) -> list[str]:
    return [w for w in _norm(name).split() if len(w) >= 3 and w not in _GENERIC_WORDS]


def hint_terms(hint: str) -> list[str]:
    """A hint's comma/semicolon/newline-separated terms, normalized."""
    return [t for t in (_norm(p) for p in re.split(r"[,;\n]", hint or "")) if t]


def _keywords(targets: Sequence[RepoTarget]) -> list[list[str]]:
    """Per target: the phrases that name it — its full name, the words of its
    name no other target shares, and its hint terms (all normalized)."""
    names = [repo_name(t.url) for t in targets]
    word_owners: dict[str, int] = {}
    for name in names:
        for w in set(_name_words(name)):
            word_owners[w] = word_owners.get(w, 0) + 1
    out: list[list[str]] = []
    for t, name in zip(targets, names):
        phrases: list[str] = []
        full = _norm(name)
        if full:
            phrases.append(full)
        phrases += [w for w in _name_words(name) if word_owners.get(w) == 1]
        phrases += hint_terms(t.hint)
        out.append(list(dict.fromkeys(phrases)))
    return out


def _mentions(text: str, phrase: str) -> bool:
    """Whether normalized ``phrase`` occurs in ``text`` as whole words."""
    hay = " " + _norm(text) + " "
    return bool(phrase) and (" " + phrase + " ") in hay


# --------------------------------------------------------------------------- #
# Deterministic routing
# --------------------------------------------------------------------------- #
def route_by_labels(story, targets: Sequence[RepoTarget]) -> Optional[RouteDecision]:
    """Every repo a ticket label names, in the source's order — or ``None``."""
    labels = [
        str(lb) for lb in (getattr(story, "labels", None) or []) if str(lb).strip()
    ]
    if not labels:
        return None
    hits: list[tuple[str, str]] = []  # (url, label)
    for t, phrases in zip(targets, _keywords(targets)):
        for lb in labels:
            if _norm(lb) in phrases:
                hits.append((t.url, lb))
                break
    if not hits:
        return None
    quoted = ", ".join(f"'{lb}'" for _, lb in hits)
    return RouteDecision(
        repos=[u for u, _ in hits],
        reason=("label " if len(hits) == 1 else "labels ") + quoted,
        method=METHOD_LABEL,
    )


def title_mentions(story, targets: Sequence[RepoTarget]) -> list[tuple[str, str]]:
    """``(url, phrase)`` for each repo the ticket's title names, in order."""
    title = str(getattr(story, "name", "") or "")
    hits: list[tuple[str, str]] = []
    for t, phrases in zip(targets, _keywords(targets)):
        for p in phrases:
            if _mentions(title, p):
                hits.append((t.url, p))
                break
    return hits


def route_deterministic(
    story, targets: Sequence[RepoTarget]
) -> Optional[RouteDecision]:
    """Labels first, then a title that names exactly one repo; else ``None``."""
    by_label = route_by_labels(story, targets)
    if by_label is not None:
        return by_label
    hits = title_mentions(story, targets)
    if len(hits) == 1:
        url, phrase = hits[0]
        return RouteDecision(
            repos=[url], reason=f"title mentions '{phrase}'", method=METHOD_TITLE
        )
    return None


# --------------------------------------------------------------------------- #
# The model question
# --------------------------------------------------------------------------- #
def readme_blurb(path: str, budget: int = README_BUDGET) -> str:
    """The first prose lines of a checkout's README, or ``""``."""
    if not path:
        return ""
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        p = Path(path) / name
        try:
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
        lines: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            # Badges, images, HTML and rules say nothing about what the repo is.
            if not line or line.startswith(("![", "[![", "<", "---", "===", "```")):
                continue
            line = line.lstrip("#").strip()
            if line:
                lines.append(line)
            if sum(len(x) for x in lines) >= budget:
                break
        blurb = " ".join(lines)
        return blurb[: budget - 1] + "…" if len(blurb) > budget else blurb
    return ""


def local_checkout(url: str, *, clone: bool = False) -> str:
    """An absolute local checkout of ``url``, or ``""``.

    ``url`` itself when it is a local directory; else the provisioning base
    clone MindFlock keeps for it, when one exists. ``clone=True`` makes that
    base clone when there is none yet (a network clone — only for a repo the
    ticket is actually going to be told to work in). Never raises."""
    u = os.path.expanduser((url or "").strip())
    if not u:
        return ""
    if os.path.isdir(u):
        return os.path.abspath(u)
    try:
        from backend.session import provisioned

        for d in provisioned.launch_workdirs(url):
            if d and os.path.isdir(d):
                return os.path.abspath(d)
        if clone:
            settings = provisioned.load_provision_settings(repo_url_override=url)
            if settings is not None:
                return os.path.abspath(provisioned.ensure_base_repo(settings))
    except Exception as err:  # noqa: BLE001
        _logger.info("No local checkout of %s for routing: %s", url, err)
    return ""


def build_llm_prompt(
    story, targets: Sequence[RepoTarget], blurbs: Sequence[str] = ()
) -> str:
    """The routing question for the agent CLI."""
    lines = [
        "You are routing a software ticket to the code repositories it needs "
        "changes in. Do not edit anything; only answer.",
        "",
        "Repositories:",
    ]
    for i, t in enumerate(targets):
        parts = [f"{i + 1}. {repo_name(t.url)}"]
        if t.hint:
            parts.append(f"hint: {t.hint}")
        blurb = blurbs[i] if i < len(blurbs) else ""
        if blurb:
            parts.append(f"README: {blurb}")
        lines.append(" — ".join(parts))
    labels = [
        str(lb) for lb in (getattr(story, "labels", None) or []) if str(lb).strip()
    ]
    desc = str(getattr(story, "description", "") or "").strip()
    if len(desc) > DESCRIPTION_BUDGET:
        desc = desc[:DESCRIPTION_BUDGET] + "…"
    lines += [
        "",
        f"Ticket title: {getattr(story, 'name', '')}",
    ]
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    lines += [
        "Description:",
        desc or "(none)",
        "",
        "Reply with the repository names from the list above that this ticket "
        "needs code changes in, the one where most of the work happens first, "
        "comma-separated inside <repos></repos>. Name more than one only when "
        "the ticket clearly needs changes in each. Then give a reason of at most "
        "twelve words inside <reason></reason>.",
    ]
    return "\n".join(lines)


_REPOS_RE = re.compile(r"<repos>(.*?)</repos>", re.S | re.I)
_REASON_RE = re.compile(r"<reason>(.*?)</reason>", re.S | re.I)


def parse_llm_answer(
    raw: str, targets: Sequence[RepoTarget]
) -> Optional[RouteDecision]:
    """The model's ``<repos>``/``<reason>`` answer as a decision, or ``None``
    when it names nothing on the list."""
    m = _REPOS_RE.search(raw or "")
    if not m:
        return None
    by_name = {_norm(repo_name(t.url)): t.url for t in targets}
    picked: list[str] = []
    for part in re.split(r"[,\n]", m.group(1)):
        token = part.strip().strip("`'\"*").strip()
        token = re.sub(r"^\d+[.)]\s*", "", token)  # "2. vnext-frontend"
        url = by_name.get(_norm(token))
        if url is None and token.isdigit() and 1 <= int(token) <= len(targets):
            url = targets[int(token) - 1].url
        if url and url not in picked:
            picked.append(url)
    if not picked:
        return None
    reason_m = _REASON_RE.search(raw or "")
    reason = " ".join((reason_m.group(1) if reason_m else "").split())
    if len(reason) > 120:
        reason = reason[:119] + "…"
    return RouteDecision(
        repos=picked,
        reason=("model: " + reason) if reason else "model's pick",
        method=METHOD_LLM,
    )


def ask_cli(prompt: str, program: str = "", timeout: float = LLM_TIMEOUT) -> str:
    """Ask the ticket's agent CLI (else the default one) ``prompt`` headlessly.

    The same one-shot path the commit-message, session-plan and ticket-draft
    questions use (:func:`backend.web.core.commit_message.pick_argv`), run in
    MindFlock's neutral directory so it never touches a repo. Raises on any
    failure; :func:`route` turns that into the fallback."""
    from backend.web.core import commit_message as _commit_message

    fallback = ""
    try:
        from backend.config.program import resolve_default_program

        fallback = resolve_default_program() or ""
    except Exception:  # noqa: BLE001
        pass
    argv = _commit_message.pick_argv(
        prompt, program, fallback, purpose="the repo a ticket needs"
    )
    return _commit_message._run(argv, _commit_message.neutral_cwd(), timeout)


# --------------------------------------------------------------------------- #
# The whole decision
# --------------------------------------------------------------------------- #
def route(
    story,
    targets: Sequence[RepoTarget],
    *,
    ask: Optional[Ask] = None,
    stored: Optional[dict] = None,
    blurbs: Optional[Sequence[str]] = None,
) -> RouteDecision:
    """Pick the repo(s) ``story`` needs from ``targets`` (see the module
    docstring for the order). ``ask`` answers the model question (``None`` =
    skip it); ``stored`` is a previous decision for this ticket."""
    targets = [t for t in targets if (t.url or "").strip()]
    if not targets:
        return RouteDecision(repos=[], reason="", method=METHOD_FALLBACK)
    if len(targets) == 1:
        return RouteDecision(repos=[targets[0].url], reason="", method="single")
    urls = {t.url for t in targets}
    prev = RouteDecision.from_dict(stored or {})
    if prev is not None and prev.method == METHOD_MANUAL and set(prev.repos) <= urls:
        return prev
    decided = route_deterministic(story, targets)
    if decided is not None:
        return decided
    if prev is not None and prev.method == METHOD_LLM and set(prev.repos) <= urls:
        return prev
    if ask is not None:
        try:
            if blurbs is None:
                blurbs = [readme_blurb(local_checkout(t.url)) for t in targets]
            answer = parse_llm_answer(
                ask(build_llm_prompt(story, targets, blurbs)), targets
            )
            if answer is not None:
                return answer
            _logger.info(
                "Repo routing for %s: the model named no listed repo", _slug(story)
            )
        except Exception as err:  # noqa: BLE001 — a model is a convenience
            _logger.info("Repo routing for %s: no model answer (%s)", _slug(story), err)
    hits = title_mentions(story, targets)
    if hits:
        return RouteDecision(
            repos=[hits[0][0]],
            reason=f"title mentions '{hits[0][1]}' (first of several)",
            method=METHOD_FALLBACK,
        )
    return RouteDecision(
        repos=[targets[0].url], reason="no signal — first repo", method=METHOD_FALLBACK
    )


def _slug(story) -> str:
    return str(getattr(story, "slug", "") or getattr(story, "id", "") or "?")


def apply_route(
    story,
    src,
    *,
    state_dir,
    program: str = "",
    ask: Optional[Ask] = None,
    use_model: bool = True,
) -> Optional[RouteDecision]:
    """Route ``story`` across ``src``'s repos and stamp the result on it.

    Sets ``story.repo_url`` to the primary repo and ``story.route`` to the
    reason, the other repos the ticket needs (resolved to absolute local
    checkouts — cloned when MindFlock has none yet) and the source's repos it
    was not routed to; records the decision in ``state_dir``. Returns ``None``
    (and leaves the story alone) for a source with fewer than two repos.
    ``ask`` defaults to the agent CLI (``program``, else the default one)."""
    targets = targets_of(src) if src is not None else []
    if len(targets) < 2:
        return None
    from backend.ticket_ingestion.state import load_ticket_routes

    slug = _slug(story)
    try:
        stored = load_ticket_routes(state_dir).get(slug)
    except Exception:  # noqa: BLE001
        stored = None
    if ask is None and use_model:
        ask = lambda prompt: ask_cli(prompt, program)  # noqa: E731
    decision = route(story, targets, ask=ask, stored=stored)
    _stamp(story, targets, decision, state_dir)
    return decision


def pick_target(src, repo: str) -> Optional[str]:
    """The URL of ``src``'s repo that ``repo`` names — its URL or its short
    name (``vnext-frontend``) — or ``None`` when it names none of them."""
    want = str(repo or "").strip()
    if not want:
        return None
    for t in targets_of(src) if src is not None else []:
        if want == t.url or _norm(want) == _norm(repo_name(t.url)):
            return t.url
    return None


def apply_manual_route(story, src, repo_url: str, *, state_dir) -> RouteDecision:
    """Stamp a repo picked on the Intake row onto ``story`` — no routing, no
    model — and record it, so the row and any later start keep it.
    ``repo_url`` must already be one of ``src``'s repos (:func:`pick_target`)."""
    decision = RouteDecision(
        repos=[repo_url], reason="picked on the row", method=METHOD_MANUAL
    )
    _stamp(story, targets_of(src), decision, state_dir)
    return decision


def _stamp(story, targets, decision: RouteDecision, state_dir) -> None:
    """Point ``story`` at ``decision`` (primary repo + the prompt's route
    block) and record the decision for the ticket."""
    from backend.ticket_ingestion.state import record_ticket_route

    slug = _slug(story)
    story.repo_url = decision.primary
    extra = []
    for url in decision.extra:
        extra.append(
            {
                "name": repo_name(url),
                "url": url,
                "path": local_checkout(url, clone=True),
            }
        )
    story.route = {
        "reason": decision.reason,
        "method": decision.method,
        "extra": extra,
        "others": [repo_name(t.url) for t in targets if t.url not in decision.repos],
    }
    try:
        record_ticket_route(state_dir, slug, decision.to_dict())
    except Exception as err:  # noqa: BLE001 — the launch matters more
        _logger.warning("Could not record the repo route for %s: %s", slug, err)
    _logger.info("Routed ticket %s → %s", slug, decision.summary())


def preview(story, src, stored_routes: dict) -> Optional[dict]:
    """The route a ticket row shows, WITHOUT asking a model: a repo picked on
    the row, else the recorded decision when there is one (and a label or the
    title doesn't now say otherwise), else the deterministic one, else
    ``pending``. ``None`` for a one-repo source. ``{"repos": [names],
    "repo_url", "reason", "method", "summary", "choices": [{name, url}]}`` —
    ``choices`` is every repo of the source, for the row's Repo picker."""
    targets = targets_of(src) if src is not None else []
    if len(targets) < 2:
        return None
    urls = {t.url for t in targets}
    # Every repo of the source, for the row's Repo picker.
    choices = [{"name": repo_name(t.url), "url": t.url} for t in targets]
    prev = RouteDecision.from_dict((stored_routes or {}).get(_slug(story)) or {})
    if prev is not None and not set(prev.repos) <= urls:
        prev = None
    decision = prev if prev is not None and prev.method == METHOD_MANUAL else None
    if decision is None:
        decision = route_deterministic(story, targets) or prev
    if decision is None:
        return {
            "repos": [],
            "repo_url": "",
            "reason": "picked when it starts",
            "method": "pending",
            "summary": "repo picked when it starts",
            "choices": choices,
        }
    return {
        "repos": [repo_name(u) for u in decision.repos],
        "repo_url": decision.primary,
        "reason": decision.reason,
        "method": decision.method,
        "summary": decision.summary(),
        "choices": choices,
    }


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #
def _worktree_dir(path: str, slug: str) -> str:
    base = path.rstrip("/\\")
    name = os.path.basename(base)
    if name.startswith("_base_"):
        name = name[len("_base_") :]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{name}-{slug}").strip("-")
    return os.path.join(os.path.dirname(base), safe)


def _extra_branches(url: str, path: str) -> tuple[str, str]:
    """``(fork base, PR base)`` for an extra repo of a multi-repo ticket.

    The same answer its own ticket session would get: the repo's
    ``repository.repo_branches`` block, else the flock-wide base / PR base.
    ``("", "")`` when nothing is configured — the prompt then keeps
    ``origin/HEAD`` and leaves the PR target to the repo's default. Never
    raises: it only shapes prompt text.
    """
    try:
        from backend.config import settings as _s
        from backend.session.provisioned import repo_slug_for

        st = _s.load_settings()
        base, pr = _s.repo_branches_for(repo_slug_for(url or path), st)
        if base:
            return base, pr
        base = (st.repository.base_branch or st.github.base_branch or "").strip()
        return base, (st.repository.pr_base_branch or "").strip() or base
    except Exception:  # noqa: BLE001
        return "", ""


def other_repos_section(story, branch: str) -> str:
    """The prompt section for a routed ticket, or ``""``.

    A multi-repo ticket gets each other repo's absolute path and the steps to
    change it on its own branch and open its own PR; a ticket routed to one
    repo of several gets a note naming the reason and the repos it was NOT
    sent to, so a misroute is said rather than forced."""
    route_info = getattr(story, "route", None) or {}
    if not route_info:
        return ""
    here = repo_name(getattr(story, "repo_url", "") or "") or "this repository"
    reason = str(route_info.get("reason") or "").strip()
    why = f" — {reason}" if reason else ""
    extra = [e for e in (route_info.get("extra") or []) if isinstance(e, dict)]
    lines: list[str] = []
    if not extra:
        others = [str(n) for n in (route_info.get("others") or []) if n]
        if not others:
            return ""
        lines += [
            "## Repository",
            "",
            f"This ticket's source covers several repositories; MindFlock "
            f"routed it to `{here}` (this workspace){why}. The others are "
            + ", ".join(f"`{n}`" for n in others)
            + ". If the work actually belongs in one of those, say so rather "
            "than forcing it in here.",
            "",
        ]
        return "\n".join(lines)
    slug = str(getattr(story, "slug", "") or "ticket")
    lines += [
        "## Other repositories this ticket needs",
        "",
        f"MindFlock routed this ticket to `{here}` (this workspace) and also to "
        f"the repositories below{why}. Make the changes each of them needs too "
        "— this overrides the instruction above about not working in other "
        "repos, for these repositories only. Keep each repository's change "
        "separate: its own branch, its own commits, its own pull request.",
        "",
    ]
    for e in extra:
        name = str(e.get("name") or repo_name(str(e.get("url") or "")))
        path = str(e.get("path") or "").strip()
        url = str(e.get("url") or "").strip()
        lines.append(f"### {name}")
        lines.append("")
        base, pr_base = _extra_branches(url, path)
        if pr_base:
            open_pr = (
                f"3. Open a pull request for it in that repository into "
                f"`{pr_base}` (`--base {shlex.quote(pr_base)}`; the repo's "
                "default branch only if it has no such branch)."
            )
        else:
            open_pr = "3. Open a pull request for it in that repository."
        if path:
            wt = _worktree_dir(path, slug)
            # Quoted for the shell: a macOS path may well hold a space.
            qp, qwt, qb = shlex.quote(path), shlex.quote(wt), shlex.quote(branch)
            if base:
                start = shlex.quote("origin/" + base)
                fallback = f"use `origin/HEAD` if `origin/{base}` does not exist"
            else:
                start = "origin/HEAD"
                fallback = "use the repo's default branch if `origin/HEAD` is not set"
            lines += [
                f"Local checkout: `{path}`"
                + (f" (clone of `{url}`)" if url != path else ""),
                "",
                "1. Work in a separate worktree so that checkout (which may be in "
                "use) is left alone:",
                f"   `git -C {qp} fetch origin && git -C {qp} worktree add "
                f"{qwt} -b {qb} {start}` ({fallback}; if the branch already "
                "exists, check it out instead of creating it).",
                f"2. Make the change in `{wt}`, commit it there, and push it: "
                f"`git -C {qwt} push -u origin {qb}`.",
                open_pr,
            ]
        else:
            lines += [
                f"Repository: `{url}` (no local checkout yet)",
                "",
                "1. Clone it next to this workspace and create the branch "
                f"`{branch}` there" + (f", from `{base}`." if base else "."),
                "2. Make the change, commit it, and push the branch.",
                open_pr,
            ]
        lines.append("")
    lines += [
        "Link the pull requests to each other in their descriptions so "
        "reviewers can find the pair. If one of these repositories turns out "
        "not to need a change, skip it and say why.",
        "",
    ]
    return "\n".join(lines)
