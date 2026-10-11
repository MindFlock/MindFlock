"""Ticket-provider registry.

Maps a provider name to its adapter class and its UI credential schema. The
pipeline calls :func:`get_provider`; the web layer reads :data:`PROVIDER_META`
to render the right fields and drive the per-provider "Test connection" flow.
"""

from __future__ import annotations

import re
from typing import Type

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.providers.asana import AsanaProvider
from backend.ticket_ingestion.providers.base import (
    LABEL_FILTER_PROVIDERS,
    STATE_BOUNDED_PROVIDERS,
    ProviderError,
    TicketNotFound,
    TicketProvider,
    parse_acceptance_criteria,
)
from backend.ticket_ingestion.providers.github_issues import GithubIssuesProvider
from backend.ticket_ingestion.providers.jira import JiraProvider
from backend.ticket_ingestion.providers.linear import LinearProvider
from backend.ticket_ingestion.providers.shortcut import ShortcutProvider

__all__ = [
    "ProviderError",
    "TicketNotFound",
    "TicketProvider",
    "parse_acceptance_criteria",
    "PROVIDER_REGISTRY",
    "PROVIDER_META",
    "get_provider",
    "provider_slug_prefix",
    "provider_meta",
    "validate_source",
    "missing_auto_fields",
    "test_sensitive_keys",
]

PROVIDER_REGISTRY: dict[str, Type[TicketProvider]] = {
    GithubIssuesProvider.name: GithubIssuesProvider,
    ShortcutProvider.name: ShortcutProvider,
    JiraProvider.name: JiraProvider,
    LinearProvider.name: LinearProvider,
    AsanaProvider.name: AsanaProvider,
}


# UI-facing description of each provider and the fields it needs. ``field.key``
# maps 1:1 onto a ``TicketProviderConfig`` attribute so the settings layer can
# store/resolve it generically. ``secret`` fields are masked on read.
#
# ORDER MATTERS: the Settings UI defaults a newly added source to the first
# entry, so GitHub Issues leads. It is the widest surface (every OSS maintainer
# already has issues assigned to them) and the only source that needs no input at
# all — the token comes from `gh auth login` and the repo from `origin` — so it
# is the on-ramp a first-time user should land on.
def _scope_field(anyone_label: str, hint: str) -> dict:
    """The "whose tickets" picker, for a provider that can search without an
    assignee. QA queues are picked up by state — the tickets in them belong to
    whoever wrote the code and are deliberately never reassigned — so scope is a
    per-source choice, not a property of the account. Absent from Asana, whose
    task endpoint has no query without an assignee or a project."""
    return {
        "key": "assignee_scope",
        "label": "Whose tickets",
        "type": "choice",
        "secret": False,
        "required": False,
        "options": [
            {"value": "", "label": "Assigned to me"},
            {"value": "anyone", "label": anyone_label},
        ],
        "hint": hint,
    }


def _start_state_field(noun: str, hint: str) -> dict:
    """The optional "move it when a session starts" picker.

    One state, not several: this is a destination, where ``workflow_state`` is a
    filter. Empty = leave the ticket where it is, which is what every source did
    before the setting existed — the whole feature is opt-in per source, because
    a board only has a column to move work into if someone drew one.

    Only offered by the providers that can actually write a state back
    (``STATE_SETTING_PROVIDERS``); GitHub Issues and Asana have nowhere to move
    a ticket to.
    """
    return {
        "key": "start_state",
        "label": f"Move to {noun} on start",
        "type": "state_one",
        "secret": False,
        "required": False,
        "hint": hint,
    }


def _start_comment_field(noun: str) -> dict:
    """The optional "say so on the ticket" toggle that rides on the move above.

    A select rather than a checkbox because every catalog field is a string the
    card round-trips as-is; ``"on"`` / ``""`` is that string. Listed right after
    the start-state picker on every provider that has one, since it does
    nothing without it (:func:`~backend.ticket_ingestion.providers.base.comments_on_start`).
    """
    return {
        "key": "start_comment",
        "label": "Comment when moved",
        "type": "choice",
        "secret": False,
        "required": False,
        "options": [
            {"value": "", "label": "Off — move it silently"},
            {"value": "on", "label": "On — “MindFlock (you) is taking this on.”"},
        ],
        "hint": (
            f"Optional: when MindFlock moves the {noun} into the state above, "
            "also post a comment on it saying MindFlock is taking it on, with "
            "your first name from this connection. Only posted when the move "
            f"actually changes the {noun}'s state, so relaunching one that is "
            "already there doesn't comment twice."
        ),
    }


def _claim_label_field(noun: str) -> dict:
    """The claim label's stem, on every provider (all of them can label).

    When a session starts on a {noun} MindFlock assigns it to you and labels
    it ``<stem>:working`` so a teammate whose MindFlock is not paired with
    yours sees it is taken (backend.ticket_ingestion.tracker_claims)."""
    return {
        "key": "claim_label",
        "label": "Claim label",
        "secret": False,
        "required": False,
        "placeholder": "mindflock",
        "hint": (
            f"When a session starts on a {noun}, MindFlock assigns it to you "
            f"and labels it <this>:working (mindflock:working when blank), so "
            "teammates' MindFlock skips it — \u201c<you> is on this\u201d. The "
            "label comes off when the session closes or ships; the assignment "
            "stays."
        ),
    }


PROVIDER_META: list[dict] = [
    {
        "id": "github_issues",
        "label": "GitHub Issues",
        "blurb": (
            "Zero config: uses your GitHub connection (Settings → Connections → "
            "GitHub → Connect) and this repo. Auto-creates a session for each "
            "issue assigned to you."
        ),
        "fields": [
            {
                "key": "project",
                "label": "Repository",
                "secret": False,
                "required": False,
                "placeholder": "auto-detected from this repo — or owner/repo",
                "auto": True,
                # A person's override: blank means "back to auto-detect", so a
                # save that sends it blank clears it (unlike fields Test fills).
                "override": True,
            },
            # No per-source Token field: the credential is the one GitHub
            # connection (Settings → Connections → GitHub → Connect). A token
            # an older release stored on a source is still read as the last
            # fallback for one release (github_issues.PER_SOURCE_TOKEN_NOTICE),
            # then ignored.
            _scope_field(
                "Anyone's — every open issue",
                "GitHub issues have no workflow states, so the repository above "
                "is the only thing scoping this. Anyone's includes unassigned "
                "issues.",
            ),
            _claim_label_field("issue"),
        ],
    },
    {
        "id": "shortcut",
        "label": "Shortcut",
        "blurb": "Auto-create a session for each story assigned to you.",
        "fields": [
            {
                "key": "api_token",
                "label": "API token",
                "secret": True,
                "required": True,
                "placeholder": "Shortcut API token",
            },
            {
                "key": "member_id",
                "label": "Member ID",
                "secret": False,
                "required": True,
                "placeholder": "filled by Test",
                "auto": True,
            },
            _scope_field(
                "Anyone's — by state or label",
                "Anyone's picks up every story sitting in the ingest states "
                "below (or carrying an ingest label), whoever owns it — the QA "
                "queue. It needs at least one state or label; without one it "
                "stays assigned-to-me.",
            ),
            {
                "key": "workflow_state",
                "label": "Ingest states",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any state (add one or more)",
            },
            {
                "key": "ingest_labels",
                "label": "Ingest labels",
                "secret": False,
                "required": False,
                "placeholder": "any label — or e.g. mindflock, needs-agent",
                "hint": (
                    "Optional: only ingest stories carrying one of these labels "
                    "(comma-separated, case-insensitive), on top of the states "
                    "above. Use it when every ticket sits in the same state and "
                    "adding the label is how you hand one to MindFlock."
                ),
            },
            _start_state_field(
                "state",
                "Optional: when a session starts for one of these stories — "
                "whether the pipeline picked it up or you ran it by hand — move "
                "the story here. Leave it unset and the story stays where it is.",
            ),
            _start_comment_field("story"),
            _claim_label_field("story"),
        ],
    },
    {
        "id": "jira",
        "label": "Jira",
        "blurb": "Ingest Jira issues assigned to you (assignee = currentUser()).",
        "fields": [
            {
                "key": "base_url",
                "label": "Site URL",
                "secret": False,
                "required": True,
                "validate": "url",
                "placeholder": "https://your-domain.atlassian.net",
            },
            {
                "key": "email",
                "label": "Account email",
                "secret": False,
                "required": True,
                "validate": "email",
                "placeholder": "you@company.com",
            },
            {
                "key": "api_token",
                "label": "API token",
                "secret": True,
                "required": True,
                "placeholder": "Atlassian API token",
            },
            {
                "key": "member_id",
                "label": "Account ID",
                "secret": False,
                "required": False,
                "placeholder": "filled by Test (optional)",
                "auto": True,
            },
            {
                "key": "project",
                "label": "Project key",
                "secret": False,
                "required": False,
                "validate": "key",
                "placeholder": "e.g. ENG — optional",
                "hint": (
                    "Optional: the project a ticket filed from New → Ticket goes "
                    "into. Ingestion is by assignee or status across the whole "
                    "site, so leave it blank unless you file tickets from here."
                ),
            },
            _scope_field(
                "Anyone's — by status",
                "Anyone's picks up every issue sitting in the ingest statuses "
                "below, whoever it is assigned to. It needs at least one status "
                "selected; without one it stays assigned-to-me.",
            ),
            {
                "key": "workflow_state",
                "label": "Ingest statuses",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any status (add one or more)",
            },
            _start_state_field(
                "status",
                "Optional: when a session starts for one of these issues — "
                "whether the pipeline picked it up or you ran it by hand — "
                "transition the issue here. Jira only moves along transitions "
                "its workflow offers from the issue's current status; when there "
                "is none, the session still starts and the move is logged as a "
                "warning.",
            ),
            _start_comment_field("issue"),
            _claim_label_field("issue"),
        ],
    },
    {
        "id": "linear",
        "label": "Linear",
        "blurb": "Ingest Linear issues assigned to you.",
        "fields": [
            {
                "key": "api_token",
                "label": "API key",
                "secret": True,
                "required": True,
                "placeholder": "lin_api_…",
            },
            {
                "key": "member_id",
                "label": "User ID",
                "secret": False,
                "required": False,
                "placeholder": "filled by Test (optional)",
                "auto": True,
            },
            {
                "key": "project",
                "label": "Team key",
                "secret": False,
                "required": False,
                "validate": "key",
                "placeholder": "e.g. ENG — optional",
                "hint": (
                    "Optional: the team key. Bounds an Anyone's search to that "
                    "team's issues, and is where a ticket filed from New → "
                    "Ticket goes (needed only when the workspace has more than "
                    "one team)."
                ),
            },
            _scope_field(
                "Anyone's — by state",
                "Anyone's picks up every issue sitting in the ingest states "
                "below, whoever it is assigned to — scoped to the team in "
                "Team key when one is set. It needs at least one state selected; "
                "without one it stays assigned-to-me.",
            ),
            {
                "key": "workflow_state",
                "label": "Ingest states",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any state (add one or more)",
            },
            _start_state_field(
                "state",
                "Optional: when a session starts for one of these issues — "
                "whether the pipeline picked it up or you ran it by hand — move "
                "the issue here. Leave it unset and the issue stays where it is.",
            ),
            _start_comment_field("issue"),
            _claim_label_field("issue"),
        ],
    },
    {
        "id": "asana",
        "label": "Asana",
        "blurb": "Ingest Asana tasks assigned to you in a workspace.",
        "fields": [
            {
                "key": "api_token",
                "label": "Access token",
                "secret": True,
                "required": True,
                "placeholder": "Asana personal access token",
            },
            {
                "key": "project",
                "label": "Workspace",
                "secret": False,
                "required": True,
                "validate": "gid",
                "placeholder": "filled by Test — or the workspace gid",
                "auto": True,
                "hint": (
                    "Test looks the workspace up from the token: one workspace "
                    "fills this in, several offer a pick here."
                ),
            },
            {
                "key": "member_id",
                "label": "User GID",
                "secret": False,
                "required": False,
                "placeholder": "filled by Test (optional)",
                "auto": True,
            },
            _scope_field(
                "Anyone's — by section",
                "Anyone's picks up every task sitting in the ingest sections "
                "below, whoever it is assigned to. It needs at least one "
                "section selected; without one it stays assigned-to-me.",
            ),
            {
                "key": "workflow_state",
                "label": "Ingest sections",
                "type": "state",
                "secret": False,
                "required": False,
                "placeholder": "any section (add one or more)",
            },
            _start_state_field(
                "section",
                "Optional: when a session starts for one of these tasks — "
                "whether the pipeline picked it up or you ran it by hand — move "
                "the task into this section. Leave it unset and the task stays "
                "where it is.",
            ),
            _start_comment_field("task"),
            _claim_label_field("task"),
        ],
    },
]


def provider_meta(provider: str) -> dict | None:
    """The catalog entry for ``provider`` (``None`` for an unknown one)."""
    key = (provider or "").strip().lower()
    return next((m for m in PROVIDER_META if m["id"] == key), None)


# Per-field value checks, named by the catalog's ``validate`` key so the form
# can say what shape a field wants before the server is asked. Each answers
# the error to show, or ``""`` when the value passes. Blank values never reach
# here — "required" is its own check.
_VALIDATORS: dict[str, tuple[re.Pattern, str]] = {
    "url": (re.compile(r"^https?://\S+$"), "Must be a full URL starting with https://"),
    "email": (re.compile(r"^[^@\s]+@[^@\s]+$"), "Must be an email address"),
    "key": (
        re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$"),
        "A key like ENG — letters and digits, no spaces",
    ),
    "gid": (re.compile(r"^\d+$"), "A numeric gid, e.g. 1201234567890"),
}


def _field_error(field: dict, value: str) -> str:
    kind = field.get("validate")
    if not kind or kind not in _VALIDATORS:
        return ""
    pattern, message = _VALIDATORS[kind]
    return "" if pattern.match(value) else message


def validate_source(values: dict, *, for_save: bool = True) -> dict[str, str]:
    """Per-field errors for one source as the catalog defines it: ``{}`` means
    it can be saved.

    ``values`` is the source as it would be stored (secrets already resolved
    against the stored copy, so a kept token counts as present). Checks, in
    this order per field: required-and-blank, then the field's ``validate``
    shape; then the one cross-field rule — ``assignee_scope = "anyone"`` on a
    state-bounded provider needs at least one ingest state (or, on Shortcut,
    an ingest label), because :func:`ingests_any_assignee` would otherwise
    quietly fall back to assigned-to-me and the card would promise a queue it
    never polls.

    ``for_save`` skips the fields Test fills in (``auto``): a Shortcut source
    has to be saved before Test can look its member id up, so those are
    required *before the source counts as connected*
    (:func:`missing_auto_fields`), not before it can be written.
    """
    meta = provider_meta(str(values.get("provider") or ""))
    if meta is None:
        # Not in the catalog: nothing to check it against. The form only offers
        # catalog providers, so this is a hand-edited file — left alone, the
        # way the pipeline's own config loader reports it.
        return {}
    errors: dict[str, str] = {}
    for f in meta["fields"]:
        key = f["key"]
        value = str(values.get(key) or "").strip()
        if not value:
            if f.get("required") and not (for_save and f.get("auto")):
                errors[key] = (
                    "Filled in by Test — run it" if f.get("auto") else "Required"
                )
            continue
        err = _field_error(f, value)
        if err:
            errors[key] = err
    provider = meta["id"]
    scope = str(values.get("assignee_scope") or "").strip().lower()
    if scope == "anyone" and provider in STATE_BOUNDED_PROVIDERS:
        has_state = bool(str(values.get("workflow_state") or "").strip())
        has_label = provider in LABEL_FILTER_PROVIDERS and bool(
            str(values.get("ingest_labels") or "").strip()
        )
        if not has_state and not has_label:
            noun = next(
                (
                    f["label"].lower()
                    for f in meta["fields"]
                    if f["key"] == "workflow_state"
                ),
                "ingest states",
            )
            errors["assignee_scope"] = (
                f"Anyone's needs at least one of the {noun}"
                + (
                    " (or an ingest label)"
                    if provider in LABEL_FILTER_PROVIDERS
                    else ""
                )
                + " below — without one it would only take tickets assigned to you"
            )
    return errors


def missing_auto_fields(values: dict) -> list[str]:
    """The required fields Test fills in that are still blank — what keeps a
    saved source from counting as connected (``member_id`` on Shortcut, the
    workspace on Asana). Keys, in catalog order."""
    meta = provider_meta(str(values.get("provider") or ""))
    if meta is None:
        return []
    return [
        f["key"]
        for f in meta["fields"]
        if f.get("required")
        and f.get("auto")
        and not str(values.get(f["key"]) or "").strip()
    ]


def test_sensitive_keys(provider: str) -> list[str]:
    """The source keys whose change makes a stored test result stale: the
    provider itself and every catalog field (credentials, site, scope, states).
    Label, repo, agent, effort and depth change nothing a test looked at."""
    meta = provider_meta(provider)
    return ["provider"] + ([f["key"] for f in meta["fields"]] if meta else [])


def get_provider(cfg: TicketProviderConfig) -> TicketProvider:
    """Instantiate the adapter for ``cfg.provider``."""
    cls = PROVIDER_REGISTRY.get((cfg.provider or "shortcut").strip().lower())
    if cls is None:
        raise ProviderError(
            f"Unknown ticketing provider {cfg.provider!r}; "
            f"expected one of {', '.join(PROVIDER_REGISTRY)}"
        )
    return cls(cfg)


def provider_slug_prefix(provider: str) -> str:
    """The default slug/branch prefix for a provider name (``sc``/``jira``/…),
    without instantiating the adapter. Falls back to a sanitized provider name
    for unknown providers."""
    cls = PROVIDER_REGISTRY.get((provider or "shortcut").strip().lower())
    if cls is not None:
        return cls.slug_prefix
    return (provider or "src").strip().lower() or "src"
