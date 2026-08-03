"""Core dataclasses used across the pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


@dataclass
class Attachment:
    name: str
    url: str
    content_type: str | None = None
    # Provider-supplied HTTP headers required to download this attachment
    # (e.g. ``{"Authorization": "Bearer …"}`` for Jira/Asana/GitHub, or
    # ``{"Shortcut-Token": …}`` for Shortcut-hosted files). Empty = public URL.
    auth_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Ticket:
    """A normalized work item from any ticketing provider.

    The field names retain the original Shortcut vocabulary (``owner_ids``,
    ``app_url``) so the whole pipeline — validator, prompt builder, provisioner —
    keeps working unchanged across providers. Provider adapters populate:

    * ``provider`` — machine id of the source (``"shortcut"``, ``"jira"``, …).
    * ``slug`` — the provider-scoped, filesystem/branch-safe handle used for the
      git branch (``feature/<slug>/<name-slug>``), the tmux session and the
      MindFlock session title. Globally unique across providers so dedup and
      workspace paths never collide. Defaults to ``sc-<id>`` (the historic
      Shortcut scheme) when a provider doesn't set it.
    * ``source_label`` — human name of the source for prompt text (e.g. the
      ``"Jira URL:"`` line).

    ``id`` is the provider-native id and may be an int (Shortcut/GitHub) or a
    string key (Jira ``PROJ-1``, Linear ``ENG-5``, Asana gid); it is always
    coerced to ``str`` for the slug.
    """

    id: int | str
    name: str
    description: str
    acceptance_criteria: list[str]
    owner_ids: list[str]
    app_url: str
    created_at: datetime
    comments: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    provider: str = "shortcut"
    slug: str = ""
    source_label: str = "Shortcut"
    # Git clone URL / local path this ticket's session provisions into, stamped
    # from its ingestion source. Empty = fall back to the global repository.url.
    repo_url: str = ""
    # Which agent CLI this ticket's session runs — a provider name ("claude",
    # "codex", "aider", "goose", …) stamped from its ingestion source, so one
    # flock can route (say) Jira tickets to codex and GitHub issues to a local
    # model. Empty = fall back to [mindflock].agent, then the engine default.
    agent: str = ""
    # Human name of the ticket's workflow state / bucket ("In Progress",
    # "Ready for Dev", …) when the provider knows it, spelled exactly the way
    # that provider's ``list_states()`` spells it — the assigned-tickets panel
    # matches the two. Populated by the adapters that expose a state model
    # (Shortcut/Jira/Linear); the pipeline itself never reads it.
    # Empty = unknown.
    state: str = ""

    def __post_init__(self) -> None:
        if not self.slug:
            # Historic default: Shortcut stories branch as feature/sc-<id>/…
            self.slug = f"sc-{self.id}"


@dataclass
class WebhookEvent:
    event_id: str
    # Same union as ``Ticket.id``: the provider-native id, which is a string for
    # Jira/Linear/Asana. ``PipelineOrchestrator._fetch_story`` already accepts
    # ``int | str`` and stringifies it, so annotating this ``int`` was a
    # Shortcut-era leftover that mistyped every non-Shortcut event.
    story_id: int | str
    action_type: str
    member_id: str
    owner_ids: list[str]
    changed_at: datetime
    raw_payload: dict[str, Any]


@dataclass
class ValidationResult:
    is_valid: bool
    failures: list[str] = field(default_factory=list)


@dataclass
class ProvisionedEnvironment:
    directory: Path
    branch_name: str
    cursor_window_id: int


@dataclass
class ProcessingRecord:
    # Historically the numeric Shortcut story id; now the provider-scoped ticket
    # slug (e.g. "sc-123", "jira-PROJ-1") so dedup works across providers.
    story_id: int | str
    branch: str
    status: str
    processed_at: datetime
    failure_reason: str | None = None


@dataclass
class ClarificationResult:
    action: Literal["provide_context", "skip"]
    supplemental_context: str | None = None


@dataclass
class PullRequest:
    number: int
    head_ref: str
    head_sha: str
    base_ref: str
    title: str
    url: str
    author: str
    created_at: datetime
    # Multi-repo PR review: which repo this PR belongs to and where to clone it
    # from. Both default to "" so single-repo callers (and the existing
    # positional constructions in tests) keep working; the monitor tags every
    # PR it discovers, and downstream (comments, provisioning) prefer these over
    # the single ``github.repo`` / ``repository.url`` fallbacks.
    repo: str = ""  # owner/name
    clone_url: str = ""  # git clone URL; "" -> fall back to repository.url


def pr_slug(pr: "PullRequest") -> str:
    """A collision-free identifier for a PR across repos.

    ``<repo-name>-<number>`` (repo's last path segment, sanitized) so PR #5 in
    two different repos don't share a workspace dir / session title.
    """
    import re

    name = pr.repo.rstrip("/").split("/")[-1]
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return f"{safe}-{pr.number}"


@dataclass
class Issue:
    """A GitHub issue picked up by the automated issue-handling loop.

    Mirrors :class:`PullRequest`: the monitor tags every issue it discovers
    with ``repo``/``clone_url`` so downstream provisioning knows where to
    clone from. ``comments`` are pre-rendered ``"[<ts> by <author>] <body>"``
    strings, ready to drop into a session prompt.
    """

    number: int
    title: str
    body: str
    url: str
    author: str
    created_at: datetime
    repo: str = ""  # owner/name
    clone_url: str = ""  # git clone URL; "" -> fall back to repository.url
    comments: list[str] = field(default_factory=list)


def issue_slug(issue: "Issue") -> str:
    """A collision-free identifier for an issue across repos.

    ``<repo-name>-<number>`` (repo's last path segment, sanitized), matching
    :func:`pr_slug` so issue #5 in two different repos don't share a workspace
    dir / session title.
    """
    import re

    name = issue.repo.rstrip("/").split("/")[-1]
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return f"{safe}-{issue.number}"


@dataclass
class ProcessedIssue:
    number: int
    processed_at: datetime
    repo: str = ""  # owner/name
    # "" (default) = processed normally; "failed" = gave up after the retry
    # cap in _process_issue (manual unblock: delete the entry from state.json).
    status: str = ""


@dataclass
class PRComment:
    id: int
    kind: Literal["review", "issue"]
    author: str
    body: str
    url: str
    path: str | None = None
    line: int | None = None
    diff_hunk: str | None = None


@dataclass
class ProvisionedPRWorkspace:
    directory: Path
    head_ref: str
    head_sha: str


@dataclass
class ProcessedPR:
    number: int
    head_sha: str
    processed_at: datetime
    repo: str = ""  # owner/name
    # "" (default) = processed normally; "failed" = gave up after the retry
    # cap in _process_pr (manual unblock: delete the entry from state.json).
    status: str = ""
