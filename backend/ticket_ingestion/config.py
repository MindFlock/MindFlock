"""Pipeline configuration loading."""

from dataclasses import dataclass, field
from pathlib import Path

import tomli

from backend.workspace_setup import (
    CacheSeed,
    WorkspaceConfigError,
    parse_caches,
    parse_setup_commands,
)

# Imported for the one coercion, not for the resolution chain: clone_transport
# owns the vocabulary ("auto"/"ssh"/"https") and the degrade-with-a-warning
# rule, so the parser must not grow a second copy that could drift from it.
from backend.ticket_ingestion.clone_transport import (
    normalize_transport as _normalize_transport,
)


class ConfigError(Exception):
    """Raised for any configuration loading failure."""


@dataclass
class GithubConfig:
    base_branch: str
    min_age_minutes: int
    poll_interval_seconds: int
    enabled: bool
    skip_authors: list[str]
    token: str = (
        ""  # Optional; falls back to GH_TOKEN/GITHUB_TOKEN env, then `gh auth token`.
    )
    # Multi-repo PR review: every ``owner/name`` repo to watch.
    repos: list[str] = field(default_factory=list)
    # Automated issue handling (opt-in, default off): newly opened issues in
    # ``issue_repos`` (its own list, independent of PR review's ``repos``)
    # each get a coding session on a fresh branch. Its tuning knobs
    # (``issue_min_age_minutes`` / ``issue_poll_interval_seconds`` /
    # ``issue_skip_authors``) are independent of the PR-review ones above.
    issues_enabled: bool = False
    issue_repos: list[str] = field(default_factory=list)
    issue_min_age_minutes: int = 15
    issue_poll_interval_seconds: int = 60
    issue_skip_authors: list[str] = field(default_factory=list)
    #: Coding CLI PR-review sessions run. Empty = ``[mindflock].agent``, then
    #: the resolved default. Independent of ``issue_agent`` below.
    agent: str = ""
    #: Coding CLI issue-handling sessions run, same fallback chain.
    issue_agent: str = ""
    #: Per-repo overrides of the PR-review knobs, keyed by ``owner/name``:
    #: ``{"agent"?, "base_branch"?, "min_age_minutes"?, "skip_authors"?}``.
    #: Each watched repo is its own card in the Intake tab, so each can want
    #: its own grace period / base branch / CLI; an absent key inherits the
    #: flat field above. See ``backend.config.settings.REPO_OVERRIDE_KEYS``.
    repo_settings: dict[str, dict] = field(default_factory=dict)
    #: The issue-handling twin, keyed by a repo in ``issue_repos``.
    issue_repo_settings: dict[str, dict] = field(default_factory=dict)

    def repo_list(self) -> list[str]:
        """The effective ``owner/name`` repos to watch (blanks stripped)."""
        return [r.strip() for r in (self.repos or []) if r and r.strip()]

    def issue_repo_list(self) -> list[str]:
        """The effective ``owner/name`` repos issue handling watches."""
        return [r.strip() for r in (self.issue_repos or []) if r and r.strip()]

    # -- per-repo resolution ------------------------------------------------ #
    # Every filter the monitors apply goes through one of these rather than
    # reading the flat field directly, so "this repo overrides it" is a single
    # decision made in one place instead of a condition repeated at each use.
    def _override(self, repo: str, key: str, *, issues: bool = False):
        """This repo's override for ``key``, or ``None`` to inherit."""
        table = self.issue_repo_settings if issues else self.repo_settings
        block = (table or {}).get((repo or "").strip())
        if not isinstance(block, dict):
            return None
        val = block.get(key)
        return None if val in (None, "", []) else val

    def min_age_for(self, repo: str) -> int:
        """Minutes a PR in ``repo`` must exist before auto review takes it."""
        val = self._override(repo, "min_age_minutes")
        return int(val) if val is not None else int(self.min_age_minutes)

    def base_branch_for(self, repo: str) -> str:
        """The base branch auto review filters ``repo``'s PRs to (``""`` = any)."""
        val = self._override(repo, "base_branch")
        return str(val) if val is not None else (self.base_branch or "")

    def skip_authors_for(self, repo: str) -> list[str]:
        """Logins whose review comments are ignored on ``repo``."""
        val = self._override(repo, "skip_authors")
        return list(val) if val is not None else list(self.skip_authors or [])

    def agent_for_repo(self, repo: str) -> str:
        """Coding CLI ``repo``'s review sessions run (``""`` = inherit)."""
        return str(self._override(repo, "agent") or "")

    def issue_min_age_for(self, repo: str) -> int:
        """Minutes an issue in ``repo`` must exist before auto handling takes it."""
        val = self._override(repo, "min_age_minutes", issues=True)
        return int(val) if val is not None else int(self.issue_min_age_minutes)

    def issue_skip_authors_for(self, repo: str) -> list[str]:
        """Logins whose issues are ignored on ``repo``."""
        val = self._override(repo, "skip_authors", issues=True)
        return list(val) if val is not None else list(self.issue_skip_authors or [])

    def issue_agent_for_repo(self, repo: str) -> str:
        """Coding CLI ``repo``'s issue sessions run (``""`` = inherit)."""
        return str(self._override(repo, "agent", issues=True) or "")


@dataclass
class TicketProviderConfig:
    """Selected ticketing provider + its credentials/scope.

    A single flat, provider-agnostic shape that covers every supported source.
    Each adapter reads the subset it needs:

    * ``provider`` — ``shortcut`` | ``jira`` | ``linear`` | ``github_issues`` | ``asana``.
    * ``api_token`` — the primary secret (Shortcut token, Jira API token, Linear
      API key, GitHub PAT, Asana PAT). GitHub falls back to the shared GitHub
      auth chain (``[github].token`` / ``gh`` CLI) when empty.
    * ``base_url`` — site/base URL where it varies by tenant (Jira Cloud
      ``https://your-domain.atlassian.net``). Empty = the provider's public API.
    * ``email`` — Jira Cloud basic-auth account email (paired with ``api_token``).
    * ``member_id`` — the "assigned to me" identity: Shortcut member UUID, Jira
      ``accountId``, Linear user id, GitHub login, Asana user gid. Empty = the
      provider resolves "me" from the token when it can.
    * ``project`` — optional scope filter: Jira project key(s), ``owner/repo``
      for GitHub, Asana workspace gid, Linear team key.
    * ``workflow_state`` — provider-native status id a ticket must be in to be
      ingested (Shortcut workflow-state id, Jira status id, Linear state id).
      Empty = ingest any state. Providers without workflow states (GitHub Issues,
      Asana) ignore it.
    * ``workflow_state_id`` — Shortcut's integer status filter; honoured when
      ``workflow_state`` is empty.
    * ``assignee_scope`` — whose tickets to ingest: ``""``/``"mine"`` (assigned to
      ``member_id``, the historic behavior) or ``"anyone"`` (every ticket sitting
      in ``workflow_state``, whoever owns it — a QA queue picks work up by state,
      not by assignment). ``"anyone"`` is only honoured together with a
      workflow-state filter; see
      :func:`~backend.ticket_ingestion.providers.base.ingests_any_assignee`.
    * ``poll_interval_seconds`` — poll cadence.
    """

    provider: str = "shortcut"
    api_token: str = ""
    base_url: str = ""
    email: str = ""
    member_id: str = ""
    project: str = ""
    workflow_state: str = ""
    workflow_state_id: int | None = None
    assignee_scope: str = ""
    poll_interval_seconds: int = 20
    # Per-source discriminator so you can connect several sources — including
    # multiple of the SAME provider (e.g. two Jira sites) — without their
    # branches / dedup keys / poll state colliding. It becomes the ticket slug
    # prefix (``feature/<id>-<ticket>/…``) and the per-source state key. Empty =
    # fall back to the provider's default prefix (sc/jira/lin/gh/asana), which
    # keeps a lone Shortcut source byte-identical to the historic scheme.
    id: str = ""
    #: Human label for the UI / connection list. Empty = derived from provider.
    label: str = ""
    #: Git clone URL (or local path) that tickets from THIS source provision
    #: into. Empty = fall back to the global ``[repository].url`` — so a
    #: single-repo setup keeps working unchanged; set it per source to run
    #: ingestion across many repos.
    repo_url: str = ""
    #: Agent CLI that tickets from THIS source run — a coding-provider name
    #: (``claude`` | ``codex`` | ``aider`` | ``goose`` | ``opencode`` | … or a
    #: user-defined provider). Empty = fall back to ``[mindflock].agent`` and
    #: then the engine's configured default, so an existing config is unchanged.
    #: Per-source so one flock can route different queues to different CLIs (or
    #: to a local model) — e.g. compliance tickets to an offline Ollama-backed
    #: session while everything else stays on a hosted CLI.
    agent: str = ""
    #: How hard the agent thinks about tickets from THIS source — a neutral rung
    #: from :mod:`backend.providers.effort` (``low``…``ultra``). Empty = whatever
    #: the CLI does on its own.
    #:
    #: Per-source for the same reason ``agent`` is: the amount of thinking a queue
    #: deserves is a property of the queue. A backlog of one-line copy fixes and a
    #: queue of schema migrations want different answers, and neither wants to be
    #: set per ticket forever. The rungs are neutral — whichever CLI runs the
    #: ticket translates and clamps them — so a source may ask for more than its
    #: CLI can give without breaking the launch.
    effort: str = ""


#: Providers the pipeline knows how to ingest from. GitHub Issues leads: it is
#: the zero-config on-ramp (see :mod:`backend.ticket_ingestion.providers`), and
#: the Settings UI defaults a new source to the first entry of the catalog.
SUPPORTED_PROVIDERS = ("github_issues", "shortcut", "jira", "linear", "asana")


def known_agents() -> tuple[str, ...]:
    """Coding-CLI provider names an ``agent`` field may name, or ``()``.

    Read from the live coding-provider registry (bundled CLIs plus any
    user-defined provider TOML), minus the ``generic`` catch-all — that one
    claims every program, so offering it as a *choice* would be meaningless.

    Returns ``()`` when the registry can't be imported at all, which is the one
    genuinely partial install the ingestion half tolerates (see
    :func:`backend.ticket_ingestion.session_runner.engine_bridge_error`). Callers
    read ``()`` as "can't validate" and accept the configured name as-is rather
    than rejecting a config they cannot check.
    """
    try:
        from backend import providers

        return tuple(p.name for p in providers.all_providers() if p.name != "generic")
    except Exception:  # noqa: BLE001 — validation is a convenience, not a gate
        return ()


def _validate_effort(value, label: str, problems: list[str]) -> str:
    """Normalize an ``effort`` field, recording a problem for an unknown rung.

    Mirrors :func:`_validate_agent`: a config that names a rung nothing
    understands is a config the user should be TOLD about, once, at load —
    silently running it at the CLI's default is how somebody concludes the
    setting does nothing.
    """
    level = str(value or "").strip().lower()
    if not level:
        return ""
    try:
        from backend.providers import effort as _effort

        ladder = _effort.EFFORTS
    except Exception:  # noqa: BLE001 — same tolerance as known_agents()
        return level
    if level not in ladder:
        problems.append(f"{label} must be one of {', '.join(ladder)} (got {level!r})")
        return ""
    return level


def _validate_agent(value, label: str, problems: list[str]) -> str:
    """Normalize an ``agent`` field, recording a problem for an unknown CLI.

    A typo here is worth catching at load time: the launch path would otherwise
    fall through to the generic catch-all provider and run the misspelled name as
    a bare program, so the session dies immediately with a shell "command not
    found" that looks like a MindFlock bug.
    """
    agent = str(value or "").strip()
    if not agent:
        return ""
    allowed = known_agents()
    if allowed and agent not in allowed:
        problems.append(f"{label} must be one of {', '.join(allowed)} (got {agent!r})")
    return agent


@dataclass
class EngineConfig:
    """How story sessions are launched: via the MindFlock engine, or standalone.

    When ``enabled`` (**the default**), the story path provisions + launches each
    session as an engine ``Instance`` (provisioned mode) — a real MindFlock
    session with a worktree, a branch and a seeded agent, which the app shows in
    its grid with the stage badge and the commit → push → PR bar. Disabling it
    reverts to the standalone path: a detached tmux session plus an OS terminal
    tab, with no app session at all.

    Enabled is the default because the engine bridge is *in-process*
    (:mod:`backend.ticket_ingestion.session_runner` imports
    :mod:`backend.session` directly): there is no HTTP call and no running
    ``mindflock serve`` it needs to reach, so it works headless as well.

    ``mode`` selects the workspace strategy: ``"worktree"`` (fast worktree off a
    canonical clone) or ``"clone"`` (a full standalone clone).

    ``agent`` is the coding CLI ingested sessions run when a ticketing source
    doesn't name its own (``[[ticketing.source]].agent``). Empty = the engine's
    configured default program, which is what every existing install resolves to,
    so this only ever *widens* the choice. It applies to the standalone launcher
    too — both ingestion paths run the same CLI.
    """

    enabled: bool = True
    mode: str = "worktree"
    agent: str = ""


@dataclass
class PipelineConfig:
    #: Whether the ticket-ingestion half (backfill + source poll loops) runs.
    #: The PR-review half is gated separately by ``github.enabled``; the process
    #: itself runs when either half is on. Layered mode reads this from the
    #: ticket-ingestion toggle (``general.ingestion_autostart``); explicit-path
    #: configs (tests, standalone) default to on.
    tickets_enabled: bool = True
    repo_url: str = ""
    #: Which URL spelling the pipeline builds when it has to turn an
    #: ``owner/repo`` slug into a clone URL: ``"auto"`` (copy ``repo_url``'s own
    #: transport when it names the same repo) | ``"ssh"`` | ``"https"``. Read by
    #: :func:`backend.ticket_ingestion.clone_transport.resolve_transport` as the
    #: config.toml layer of its env -> settings.json -> toml -> ``"auto"`` chain.
    #: Never affects pushing, and never rewrites a URL the user configured.
    git_transport: str = "auto"
    workspace_dir: Path = Path("./workspaces")
    min_description_length: int = 20
    log_file: Path = Path("./logs/pipeline.log")
    # DEBUG by default so Settings → System logs shows detailed ingestion
    # activity (only the backend.ticket_ingestion loggers — propagation is off,
    # so third-party libs don't flood it). Override with logging.log_level.
    log_level: str = "DEBUG"
    poll_interval_seconds: int = 20
    # Generic workspace setup: shell commands run in each fresh workspace
    # (None = auto-detect from workspace contents) and warm cache seeds.
    setup_commands: list[str] | None = None
    caches: list[CacheSeed] = field(default_factory=list)
    github: GithubConfig | None = None
    #: Launch routing. :func:`load_config` always fills this in (engine mode on
    #: by default); ``None`` only happens for a hand-built config (tests) and is
    #: read as "standalone launcher" by the orchestrator.
    engine: EngineConfig | None = None
    # The *primary* (first) ticketing source; ``ticketing_sources`` is the full
    # list the pipeline polls.
    ticketing: TicketProviderConfig | None = None
    #: All configured ticketing sources (>=1). Multiple different providers AND
    #: multiple of the same provider (distinct ``id``/credentials) are allowed.
    ticketing_sources: list[TicketProviderConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ticketing_sources:
            self.ticketing_sources = [self.ticketing or TicketProviderConfig()]
        # The primary source is always the first configured one.
        self.ticketing = self.ticketing_sources[0]
        _assign_source_ids(self.ticketing_sources)

    def agent_for(self, source_id: str = "") -> str:
        """The coding CLI a session from ``source_id`` should run, or ``""``.

        Precedence: that source's own ``agent`` → ``[mindflock].agent`` → ``""``,
        which every launch path reads as "use the engine's configured default
        program". The single place this chain lives, so the engine bridge, the
        standalone tmux launcher, the PR runner and the web force-start paths
        cannot disagree about which CLI a ticket runs.
        """
        for src in self.ticketing_sources or ():
            if source_id and (src.id or src.provider) == source_id and src.agent:
                return src.agent
        return self._pipeline_agent()

    def effort_for(self, source_id: str = "") -> str:
        """The thinking-effort rung a session from ``source_id`` should run at.

        ``""`` = let the CLI decide, which is what every launch path did before
        the setting existed. Deliberately NOT chained to a pipeline-wide default
        the way :meth:`agent_for` is: there is no ``[mindflock].effort``, because
        "how hard to think" is a property of the work rather than of the
        installation, and a flock-wide rung would quietly re-price every queue.
        """
        for src in self.ticketing_sources or ():
            if source_id and (src.id or src.provider) == source_id and src.effort:
                return src.effort
        return ""

    def _pipeline_agent(self) -> str:
        """The ingestion-wide agent (``[mindflock].agent``), or ``""``."""
        return (self.engine.agent if self.engine else "") or ""

    def pr_agent(self, repo: str = "") -> str:
        """The coding CLI a PR-review session should run, or ``""``.

        Precedence: that repo's own card → ``[github].agent`` → ``[mindflock].agent``
        → ``""`` (the resolved default). The per-repo rung mirrors the
        per-source rung in :meth:`agent_for`: a repo watched alongside others
        can route its reviews to a different CLI. PR review has no ticketing
        source, so it never consults the per-source chain.
        """
        if not self.github:
            return self._pipeline_agent()
        return (
            (repo and self.github.agent_for_repo(repo))
            or self.github.agent
            or self._pipeline_agent()
        )

    def issue_agent(self, repo: str = "") -> str:
        """The coding CLI an issue-handling session should run, or ``""``.

        Precedence: that repo's own card → ``[github].issue_agent`` →
        ``[mindflock].agent`` → ``""``. Deliberately does NOT fall back to
        ``[github].agent``: issue handling and PR review are separately
        configured features (separate repo lists, separate toggles), so
        inheriting the review CLI would surprise anyone who set one and not
        the other.
        """
        if not self.github:
            return self._pipeline_agent()
        return (
            (repo and self.github.issue_agent_for_repo(repo))
            or self.github.issue_agent
            or self._pipeline_agent()
        )


def _assign_source_ids(sources: list[TicketProviderConfig]) -> None:
    """Give every source a stable, unique ``id`` (also its slug prefix + state
    key). The first source of a provider keeps the bare provider prefix (so a
    lone Shortcut source stays ``sc-…``); later same-provider sources get a
    numeric suffix (``jira``, ``jira-2``, …). An explicit ``id`` is respected."""
    from backend.ticket_ingestion.providers import provider_slug_prefix

    used: set[str] = set()
    for src in sources:
        base = (src.id or "").strip() or provider_slug_prefix(src.provider)
        candidate = base
        n = 1
        while candidate in used:
            n += 1
            candidate = f"{base}-{n}"
        src.id = candidate
        used.add(candidate)


def load_config(config_path: Path | str | None = None) -> PipelineConfig:
    """Load the pipeline configuration.

    Two modes:

    * **Layered** (the default, ``config_path=None`` — used by
      ``python -m backend.ticket_ingestion``): resolve every field through
      ``env var → settings.json → config.toml → built-in default``. A missing
      ``config.toml`` is fine as long as the user-specific fields (Shortcut
      token / member id / repo url) are supplied via the Settings UI or env.
      This is the productization path — a new user configures everything from
      the web Settings dialog without editing any file.
    * **Explicit path** (``config_path`` given, as the tests do): parse that TOML
      file only — no store/env layering, missing file raises. Preserves the
      original, deterministic ``config.toml``-only contract.
    """
    layered = config_path is None
    path = Path(config_path) if config_path is not None else Path("config.toml")

    if path.exists():
        try:
            with open(path, "rb") as f:
                raw = tomli.load(f)
        except tomli.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in {path}: {e}") from e
    else:
        if not layered:
            raise ConfigError(
                f"Configuration file not found: {path}\n"
                "Create a config.toml file (see config.toml.example for reference)."
            )
        raw = {}

    if layered:
        raw = _merge_layers(raw)

    return _parse_config(raw, path, layered=layered)


def config_for_launch(
    fallback: "PipelineConfig | None" = None,
) -> "PipelineConfig | None":
    """The config to make a LAUNCH decision from — re-read from disk.

    The pipeline calls :func:`load_config` once at startup and holds that
    snapshot for the life of the process, which is right for poll intervals and
    tokens but wrong for "which coding CLI should this session run": switching
    provider in the UI has to apply to the very next ticket / issue / PR, not
    to the next time the pipeline is restarted. Agent choice is therefore read
    fresh at the moment of launch — the same rule
    :func:`backend.config.program.resolve_default_program` follows for the
    app-wide default.

    Best-effort: a config that has become unreadable since startup falls back to
    ``fallback`` (the caller's snapshot) rather than failing the launch.
    """
    try:
        return load_config()
    except Exception:  # noqa: BLE001 — never let a re-read break a launch
        return fallback


def agent_now(pick, fallback: str = "") -> str:
    """``pick`` applied to the config ON DISK RIGHT NOW, empty answer included.

    The difference from :func:`fresh_agent` is the whole point: there, an
    on-disk chain that resolves to ``""`` falls through to the caller's
    snapshot; here ``""`` is an ANSWER ("use the app default"). The pipeline
    loads its config once at boot and hands that snapshot to every runner, so
    deferring to it meant switching a provider in the UI kept launching the old
    CLI until a restart — and *clearing* the field did nothing at all, because
    the snapshot's value came back every time. ``fallback`` is used only when
    the config cannot be read at all.
    """
    fresh = config_for_launch(None)
    if fresh is None:
        return fallback or ""
    try:
        return pick(fresh) or ""
    except Exception:  # noqa: BLE001 — an unreadable config is not a launch error
        return fallback or ""


def source_agent_now(source_key: str, fallback: str = "") -> str:
    """The Agent CLI configured for ticketing source ``source_key`` RIGHT NOW."""
    return agent_now(lambda c: c.agent_for(source_key), fallback)


def source_effort_now(source_key: str, fallback: str = "") -> str:
    """The thinking effort configured for ticketing source ``source_key`` RIGHT
    NOW — the effort twin of :func:`source_agent_now`."""
    return agent_now(lambda c: c.effort_for(source_key), fallback)


def fresh_agent(pick, snapshot: "PipelineConfig | None") -> str:
    """Resolve one surface's coding CLI, preferring the config on disk NOW.

    ``pick`` is the surface's own chain applied to a config — e.g.
    ``lambda c: c.pr_agent()``. It is tried against a freshly-read config first
    so a provider switched in the UI takes effect on the next launch, and falls
    back to ``snapshot`` (the long-lived config the caller was built with) when
    the fresh read has no opinion or is unavailable.

    Keeping the snapshot as the fallback rather than replacing it outright means a
    caller that was handed a config by hand still has it honoured; only an
    explicit on-disk choice overrides it. Every failure mode answers ``""``, which
    every launch path reads as "use the resolved app default".
    """
    fresh = config_for_launch(None)
    if fresh is not None:
        try:
            chosen = pick(fresh)
        except Exception:  # noqa: BLE001
            chosen = ""
        if chosen:
            return chosen
    if snapshot is None:
        return ""
    try:
        return pick(snapshot) or ""
    except Exception:  # noqa: BLE001
        return ""


def _merge_layers(raw: dict) -> dict:
    """Overlay the settings store + env vars onto a parsed ``config.toml`` dict.

    Produces a new raw dict whose values follow ``env → settings.json →
    config.toml → default`` precedence, so :func:`_parse_config` can validate the
    *resolved* values with its existing logic (no duplicated validation). Only
    the genuinely user-specific fields are required; operational fields get sane
    built-in defaults so a brand-new user only has to supply a Shortcut token,
    member id and repo url (via the Settings UI or env).
    """
    from backend.config import settings as _s

    repository = dict(raw.get("repository", {}) or {})
    validation = dict(raw.get("validation", {}) or {})
    logging_section = dict(raw.get("logging", {}) or {})
    github = dict(raw.get("github", {}) or {})
    engine = dict(raw.get("mindflock") or {})

    def _put(section: dict, key: str, value) -> None:
        if value is not None and value != "":
            section[key] = value

    # --- user-specific (no default; unresolved -> _parse_config errors) ------
    _put(
        repository,
        "url",
        _s.resolve_str(
            env="MINDFLOCK_REPO_URL",
            settings_getter=lambda s: s.repository.url,
            toml_value=repository.get("url"),
        ),
    )

    # --- operational (settings/env override, else a sane default) -----------
    _put(
        repository,
        "workspace_dir",
        _s.resolve_str(
            env="MINDFLOCK_WORKSPACE_DIR",
            settings_getter=lambda s: s.repository.workspace_dir,
            toml_value=repository.get("workspace_dir"),
            default="./workspaces",
        ),
    )
    # Which URL spelling the pipeline synthesizes from an ``owner/repo`` slug.
    # Layered here (rather than only in clone_transport) so a value set in
    # config.toml actually reaches the clone path — env and settings.json are
    # read directly by resolve_transport, but the toml layer only exists if it
    # is carried on the config object.
    _put(
        repository,
        "git_transport",
        _s.resolve_str(
            env="MINDFLOCK_GIT_TRANSPORT",
            settings_getter=lambda s: s.repository.git_transport,
            toml_value=repository.get("git_transport"),
            default="auto",
        ),
    )
    _put(
        validation,
        "min_description_length",
        _s.resolve_int(
            settings_getter=lambda s: None,  # not a settings field; toml or default
            toml_value=validation.get("min_description_length"),
            default=20,
        ),
    )
    _put(
        logging_section,
        "log_file",
        logging_section.get("log_file") or "./logs/pipeline.log",
    )
    _put(logging_section, "log_level", logging_section.get("log_level") or "DEBUG")

    # --- github block (only materialize when something is set) --------------
    _put(
        github,
        "base_branch",
        _s.resolve_str(
            env="MINDFLOCK_BASE_BRANCH",
            settings_getter=lambda s: s.github.base_branch or s.repository.base_branch,
            toml_value=github.get("base_branch"),
        ),
    )
    gh_enabled = _s.resolve_bool(
        settings_getter=lambda s: s.github.enabled,
        toml_value=github.get("enabled"),
        default=None,
    )
    if gh_enabled is not None:
        github["enabled"] = gh_enabled
    gh_issues_enabled = _s.resolve_bool(
        settings_getter=lambda s: s.github.issues_enabled,
        toml_value=github.get("issues_enabled"),
        default=None,
    )
    if gh_issues_enabled is not None:
        github["issues_enabled"] = gh_issues_enabled
    _put(
        github,
        "token",
        _s.resolve_str(
            settings_getter=lambda s: s.github.token,
            toml_value=github.get("token"),
        ),
    )
    _put(
        github,
        "min_age_minutes",
        _s.resolve_int(
            settings_getter=lambda s: s.github.min_age_minutes,
            toml_value=github.get("min_age_minutes"),
        ),
    )
    _put(
        github,
        "poll_interval_seconds",
        _s.resolve_int(
            settings_getter=lambda s: s.github.poll_interval_seconds,
            toml_value=github.get("poll_interval_seconds"),
        ),
    )
    _put(
        github,
        "issue_min_age_minutes",
        _s.resolve_int(
            settings_getter=lambda s: s.github.issue_min_age_minutes,
            toml_value=github.get("issue_min_age_minutes"),
        ),
    )
    _put(
        github,
        "issue_poll_interval_seconds",
        _s.resolve_int(
            settings_getter=lambda s: s.github.issue_poll_interval_seconds,
            toml_value=github.get("issue_poll_interval_seconds"),
        ),
    )
    # skip_authors is a list, not a scalar — settings (when non-empty) overrides
    # the toml list; _parse_config defaults it to [] when neither is present.
    _gh = _s.load_settings().github
    if _gh.skip_authors:
        github["skip_authors"] = list(_gh.skip_authors)
    if _gh.issue_skip_authors:
        github["issue_skip_authors"] = list(_gh.issue_skip_authors)
    # repos (multi-repo review): env single-repo override wins; else the
    # settings list.
    import os as _os

    _env_repo = (_os.environ.get("MINDFLOCK_GITHUB_REPO") or "").strip()
    if _env_repo:
        github["repos"] = [_env_repo]
    elif _gh.repos:
        github["repos"] = list(_gh.repos)
    # issue_repos (issue handling): its own list — settings overrides toml.
    if _gh.issue_repos:
        github["issue_repos"] = list(_gh.issue_repos)
    # Per-surface agent CLI: settings override toml, blank falls through to the
    # pipeline-wide chain rather than pinning anything.
    if _gh.agent:
        github["agent"] = _gh.agent
    if _gh.issue_agent:
        github["issue_agent"] = _gh.issue_agent
    # Per-repo override tables: whole-map replacement, like the repo lists above
    # (a per-key merge across layers would make "I cleared that field on the
    # card" indistinguishable from "I never set it", and the card is the only
    # editor either table has).
    if _gh.repo_settings:
        github["repo_settings"] = {k: dict(v) for k, v in _gh.repo_settings.items()}
    if _gh.issue_repo_settings:
        github["issue_repo_settings"] = {
            k: dict(v) for k, v in _gh.issue_repo_settings.items()
        }

    # --- engine block (settings override the [mindflock] section) --------------
    eng_enabled = _s.resolve_bool(
        settings_getter=lambda s: s.engine.enabled,
        toml_value=engine.get("enabled"),
        default=None,
    )
    if eng_enabled is not None:
        engine["enabled"] = eng_enabled
    _put(
        engine,
        "mode",
        _s.resolve_str(
            settings_getter=lambda s: s.engine.mode,
            toml_value=engine.get("mode"),
        ),
    )
    # Which CLI ingested sessions run by default. MINDFLOCK_INGESTION_AGENT is the
    # headless/CI knob (a cron pipeline that must not touch settings.json).
    _put(
        engine,
        "agent",
        _s.resolve_str(
            env="MINDFLOCK_INGESTION_AGENT",
            settings_getter=lambda s: s.engine.agent,
            toml_value=engine.get("agent"),
        ),
    )

    # --- ticketing block (generic provider selection) -----------------------
    # Resolve ticketing sources: an env single-source override wins; else the
    # settings store's sources list; else whatever [ticketing] is already in
    # the TOML.
    ticketing = _resolve_ticketing_layer(raw.get("ticketing", {}) or {}, _s)

    # --- ticket-half gate: the sidebar's Ticket Ingestion toggle -------------
    # None (never toggled — e.g. a standalone run that predates the toggle)
    # means on, matching the pipeline's historical always-poll behaviour.
    tickets_on = _s.resolve_bool(
        settings_getter=lambda s: s.general.ingestion_autostart,
        toml_value=raw.get("tickets_enabled"),
        default=None,
    )

    merged = dict(raw)
    if tickets_on is not None:
        merged["tickets_enabled"] = tickets_on
    merged.pop("shortcut", None)
    merged["repository"] = repository
    merged["validation"] = validation
    merged["logging"] = logging_section
    if github:
        merged["github"] = github
    if engine:
        merged["mindflock"] = engine
    if ticketing:
        merged["ticketing"] = ticketing
    return merged


def _resolve_ticketing_layer(toml_ticketing: dict, _s) -> dict:
    """Build the effective ``[ticketing]`` section for the layered path.

    Precedence: env single-source override → settings-store sources → the TOML
    ``[ticketing]`` as-is. Returns ``{}`` when nothing is configured anywhere."""
    import os

    env_provider = (os.environ.get("MINDFLOCK_TICKET_PROVIDER") or "").strip()
    if env_provider:
        src = {"provider": env_provider}
        for key, env in (
            ("api_token", "MINDFLOCK_TICKET_TOKEN"),
            ("base_url", "MINDFLOCK_TICKET_BASE_URL"),
            ("email", "MINDFLOCK_TICKET_EMAIL"),
            ("member_id", "MINDFLOCK_TICKET_MEMBER_ID"),
            ("project", "MINDFLOCK_TICKET_PROJECT"),
            ("agent", "MINDFLOCK_TICKET_AGENT"),
        ):
            v = (os.environ.get(env) or "").strip()
            if v:
                src[key] = v
        return {"source": [src]}

    settings_sources = _s.load_settings().ticketing.sources
    if settings_sources:
        return {"source": [s.to_dict() for s in settings_sources if s.provider]}

    # Fall back to the TOML section verbatim (single or array), if any.
    if toml_ticketing.get("provider") or toml_ticketing.get("source"):
        return dict(toml_ticketing)
    return {}


# Per-provider required credential fields (ticketing.*), with the human hint
# shown when one is missing.
#
# GitHub Issues is deliberately absent: it is the zero-config on-ramp, so BOTH
# its inputs self-resolve — the token from the shared GitHub auth chain
# (``gh auth token``) and the repo from the source's ``repo_url`` / the global
# ``[repository].url`` / this checkout's ``origin`` (see
# ``GithubIssuesProvider.resolve_repo``). Requiring ``project`` here would reject
# exactly the config that needs no fields filled in.
_PROVIDER_REQUIRED: dict[str, list[tuple[str, str]]] = {
    "shortcut": [
        ("api_token", "Shortcut API token"),
        ("member_id", "your Shortcut member id"),
    ],
    "jira": [
        ("api_token", "Jira API token"),
        ("email", "Jira account email"),
        ("base_url", "Jira site URL (https://your-domain.atlassian.net)"),
    ],
    "linear": [("api_token", "Linear API key")],
    "asana": [
        ("api_token", "Asana personal access token"),
        ("project", "Asana workspace gid"),
    ],
}


def _ticketing_source_dicts(ticketing_section: dict) -> list[dict]:
    """Normalize the ``[ticketing]`` section into a list of source dicts.

    Accepts three shapes:
    * ``[[ticketing.source]]`` array  -> ``ticketing_section["source"]``
    * single ``[ticketing]`` with a ``provider`` key -> a one-element list
    * anything else -> ``[]`` (caller treats as "no ticketing configured")
    """
    src = ticketing_section.get("source")
    if isinstance(src, list) and src:
        return [dict(s) for s in src if isinstance(s, dict)]
    if ticketing_section.get("provider"):
        # A single inline source; drop the nested-array key if present.
        single = {k: v for k, v in ticketing_section.items() if k != "source"}
        return [single]
    return []


def _parse_source(
    src: dict, config_path: Path, idx: int
) -> tuple[TicketProviderConfig, list[str]]:
    """Validate one ticketing source dict; return (config, problems)."""
    # Local: providers.base imports TicketProviderConfig from this module.
    from backend.ticket_ingestion.providers.base import (
        ANY_ASSIGNEE_PROVIDERS,
        STATE_BOUNDED_PROVIDERS,
    )

    label = f"ticketing.source[{idx}]" if idx is not None else "ticketing"
    problems: list[str] = []
    provider = str(src.get("provider") or "shortcut").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"Invalid configuration in {config_path}: {label}.provider must be "
            f"one of {', '.join(SUPPORTED_PROVIDERS)} (got {provider!r})."
        )
    for key, hint in _PROVIDER_REQUIRED.get(provider, []):
        val = src.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            problems.append(f"{label}.{key} ({hint})")
    poll = src.get("poll_interval_seconds", 20)
    if not isinstance(poll, int) or isinstance(poll, bool) or poll <= 0:
        problems.append(f"{label}.poll_interval_seconds must be a positive integer")
        poll = 20
    wsid = src.get("workflow_state_id")
    if wsid is not None and (not isinstance(wsid, int) or isinstance(wsid, bool)):
        problems.append(f"{label}.workflow_state_id must be an integer")
        wsid = None
    scope = str(src.get("assignee_scope", "") or "").strip().lower()
    if scope not in ("", "mine", "anyone"):
        problems.append(f"{label}.assignee_scope must be 'mine' or 'anyone'")
        scope = ""
    if scope == "anyone":
        # Say the narrowing out loud rather than letting a source look wider
        # than it polls. `ingests_any_assignee` enforces the same two rules.
        if provider not in ANY_ASSIGNEE_PROVIDERS:
            problems.append(
                f"{label}.assignee_scope 'anyone' is not supported for "
                f"{provider} — it can only search by assignee"
            )
        elif (
            provider in STATE_BOUNDED_PROVIDERS
            and not str(src.get("workflow_state", "") or "").strip()
        ):
            problems.append(
                f"{label}.assignee_scope 'anyone' needs at least one "
                f"workflow_state — without one it would pull every ticket in "
                f"the tracker, so it stays assigned-to-me"
            )
    cfg = TicketProviderConfig(
        provider=provider,
        api_token=str(src.get("api_token", "") or ""),
        base_url=str(src.get("base_url", "") or "").rstrip("/"),
        email=str(src.get("email", "") or ""),
        member_id=str(src.get("member_id", "") or ""),
        project=str(src.get("project", "") or ""),
        workflow_state=str(src.get("workflow_state", "") or ""),
        workflow_state_id=int(wsid) if wsid is not None else None,
        assignee_scope=scope,
        poll_interval_seconds=int(poll),
        id=str(src.get("id", "") or ""),
        label=str(src.get("label", "") or ""),
        repo_url=str(src.get("repo_url", "") or ""),
        agent=_validate_agent(src.get("agent"), f"{label}.agent", problems),
        effort=_validate_effort(src.get("effort"), f"{label}.effort", problems),
    )
    return cfg, problems


def _parse_generic_config(
    raw: dict,
    config_path: Path,
    ticketing_section: dict,
    allow_no_sources: bool = False,
) -> PipelineConfig:
    """Parse the generic ``[ticketing]`` schema — one or many sources.

    Validates the common fields (repo url, workspace, logging, validation) plus,
    per source, the credentials that source's provider requires.

    ``allow_no_sources`` (layered mode only) permits a config with no ticketing
    sources at all — the PR-review-only path — instead of erroring.
    """
    repository = raw.get("repository", {}) or {}
    validation = raw.get("validation", {}) or {}
    logging_section = raw.get("logging", {}) or {}

    source_dicts = _ticketing_source_dicts(ticketing_section)
    if not source_dicts and not allow_no_sources:
        raise ConfigError(
            f"Invalid configuration in {config_path}: [ticketing] has no provider "
            "(set [ticketing].provider or add [[ticketing.source]] entries)."
        )

    missing: list[str] = []
    type_errors: list[str] = []
    sources: list[TicketProviderConfig] = []
    multi = len(source_dicts) > 1
    for i, src in enumerate(source_dicts):
        cfg, problems = _parse_source(src, config_path, i if multi else None)
        type_errors.extend(problems)
        sources.append(cfg)

    # There's no longer a required global default repo: a source clones the repo
    # named in its own ``repo_url``. Only error when NOTHING can supply a repo —
    # neither a global ``repository.url`` nor any per-source ``repo_url``.
    repo_url = repository.get("url") or ""
    sources_have_repo = any((s.repo_url or "").strip() for s in sources)
    if source_dicts and not repo_url and not sources_have_repo:
        missing.append(
            "repository.url (or a per-source repo_url on each ticketing source)"
        )
    workspace_dir = repository.get("workspace_dir") or "./workspaces"
    # Normalized (not validated) on purpose: a typo here must not refuse to
    # start the pipeline, it degrades to "auto" with a warning.
    git_transport = _normalize_transport(repository.get("git_transport"))
    min_description_length = validation.get("min_description_length", 20)
    if not isinstance(min_description_length, int) or isinstance(
        min_description_length, bool
    ):
        type_errors.append("validation.min_description_length must be an integer")
    log_file = logging_section.get("log_file") or "./logs/pipeline.log"
    log_level = logging_section.get("log_level") or "DEBUG"

    problems = missing + type_errors
    if problems:
        fields = "\n  - ".join(problems)
        raise ConfigError(
            f"Invalid configuration in {config_path}. Missing or invalid fields:\n  - {fields}"
        )

    base_dir = config_path.resolve().parent if config_path.is_file() else Path.cwd()
    try:
        setup_commands = parse_setup_commands(raw)
        caches = parse_caches(raw, base_dir)
    except WorkspaceConfigError as e:
        raise ConfigError(f"Invalid configuration in {config_path}: {e}") from e

    github_cfg = _parse_github(raw, config_path)
    engine_cfg = _parse_engine(raw, config_path)
    # No ticketing sources (PR-review-only layered config): tickets default off
    # and there's no primary source to read a poll interval from.
    primary = sources[0] if sources else None
    tickets_default = bool(source_dicts)
    poll_interval = int(primary.poll_interval_seconds) if primary else 60

    return PipelineConfig(
        tickets_enabled=bool(raw.get("tickets_enabled", tickets_default)),
        repo_url=str(repo_url),
        git_transport=git_transport,
        workspace_dir=Path(workspace_dir),
        min_description_length=int(min_description_length),
        log_file=Path(log_file),
        log_level=str(log_level),
        poll_interval_seconds=poll_interval,
        setup_commands=setup_commands,
        caches=caches,
        github=github_cfg,
        engine=engine_cfg,
        ticketing_sources=sources,
    )


def _parse_github(raw: dict, config_path: Path) -> GithubConfig | None:
    github_section = raw.get("github") or {}
    if not github_section:
        return None
    token_raw = github_section.get("token", "")
    token = token_raw.strip() if isinstance(token_raw, str) else ""
    return GithubConfig(
        base_branch=str(github_section.get("base_branch", "main")),
        min_age_minutes=int(github_section.get("min_age_minutes", 15)),
        poll_interval_seconds=int(github_section.get("poll_interval_seconds", 60)),
        enabled=bool(github_section.get("enabled", True)),
        skip_authors=list(github_section.get("skip_authors", []) or []),
        token=token,
        repos=[str(r) for r in (github_section.get("repos", []) or [])],
        issues_enabled=bool(github_section.get("issues_enabled", False)),
        issue_repos=[str(r) for r in (github_section.get("issue_repos", []) or [])],
        issue_min_age_minutes=int(github_section.get("issue_min_age_minutes", 15)),
        issue_poll_interval_seconds=int(
            github_section.get("issue_poll_interval_seconds", 60)
        ),
        issue_skip_authors=list(github_section.get("issue_skip_authors", []) or []),
        # Per-surface coding CLI. Omitting these left both at the dataclass ""
        # default no matter what the merged config said, so the PR-review tab
        # → Agent CLI (and its issue-handling twin) resolved to nothing and every
        # review fell through to the app-wide default provider.
        agent=str(github_section.get("agent", "") or ""),
        issue_agent=str(github_section.get("issue_agent", "") or ""),
        repo_settings=_repo_override_map(github_section.get("repo_settings")),
        issue_repo_settings=_repo_override_map(
            github_section.get("issue_repo_settings")
        ),
    )


def _repo_override_map(raw) -> dict[str, dict]:
    """Coerce a ``{"owner/name": {…}}`` per-repo override table from TOML/JSON.

    Deliberately re-uses the settings store's normalizer so a value written by
    hand into ``config.toml`` and one saved from the Intake tab are cleaned
    the same way — a second, looser parser here is exactly how the two layers
    would drift.
    """
    from backend.config.settings import _repo_overrides

    return _repo_overrides(raw)


def _parse_engine(raw: dict, config_path: Path) -> EngineConfig:
    """Parse the ``[mindflock]`` block, defaulting to engine mode ON.

    An **absent** section must not mean "standalone": a config written entirely
    from the Settings UI never grows a ``[mindflock]`` block, so returning
    ``None`` here used to silently downgrade every fresh install to the
    tmux + OS-terminal-tab path. A missing section (and a missing ``enabled``
    key inside a present one) therefore yields :class:`EngineConfig` defaults.
    """
    engine_section = raw.get("mindflock") or {}
    engine_mode = str(engine_section.get("mode", "worktree"))
    if engine_mode not in ("worktree", "clone"):
        raise ConfigError(
            f"Invalid configuration in {config_path}: "
            "[mindflock].mode must be 'worktree' or 'clone'"
        )
    problems: list[str] = []
    agent = _validate_agent(engine_section.get("agent"), "[mindflock].agent", problems)
    if problems:
        raise ConfigError(
            f"Invalid configuration in {config_path}: " + "; ".join(problems)
        )
    return EngineConfig(
        enabled=bool(engine_section.get("enabled", True)),
        mode=engine_mode,
        agent=agent,
    )


def _parse_config(
    raw: dict, config_path: Path, layered: bool = False
) -> PipelineConfig:
    ticketing_section = raw.get("ticketing")
    if not ticketing_section:
        if not layered:
            raise ConfigError(
                f"Missing [ticketing] section in {config_path}.\n"
                "Configure a ticketing source ([ticketing].provider or "
                "[[ticketing.source]] entries), or use the web Settings dialog."
            )
        # Layered/productization mode: a PR-review-only user configures GitHub via
        # the Settings dialog and never touches ticketing. Proceed with no
        # ticketing sources — ticket ingestion simply stays off.
        return _parse_generic_config(raw, config_path, {}, allow_no_sources=True)
    return _parse_generic_config(raw, config_path, ticketing_section)
