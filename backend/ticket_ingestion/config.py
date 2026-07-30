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

    def repo_list(self) -> list[str]:
        """The effective ``owner/name`` repos to watch (blanks stripped)."""
        return [r.strip() for r in (self.repos or []) if r and r.strip()]

    def issue_repo_list(self) -> list[str]:
        """The effective ``owner/name`` repos issue handling watches."""
        return [r.strip() for r in (self.issue_repos or []) if r and r.strip()]


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


#: Providers the pipeline knows how to ingest from.
SUPPORTED_PROVIDERS = ("shortcut", "jira", "linear", "github_issues", "asana")


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
    """

    enabled: bool = True
    mode: str = "worktree"


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
# shown when one is missing. GitHub Issues needs no token here (it falls back to
# the shared GitHub auth chain), only a repo scope.
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
    "github_issues": [("project", "owner/repo to ingest issues from")],
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
    cfg = TicketProviderConfig(
        provider=provider,
        api_token=str(src.get("api_token", "") or ""),
        base_url=str(src.get("base_url", "") or "").rstrip("/"),
        email=str(src.get("email", "") or ""),
        member_id=str(src.get("member_id", "") or ""),
        project=str(src.get("project", "") or ""),
        workflow_state=str(src.get("workflow_state", "") or ""),
        workflow_state_id=int(wsid) if wsid is not None else None,
        poll_interval_seconds=int(poll),
        id=str(src.get("id", "") or ""),
        label=str(src.get("label", "") or ""),
        repo_url=str(src.get("repo_url", "") or ""),
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
    )


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
    return EngineConfig(
        enabled=bool(engine_section.get("enabled", True)),
        mode=engine_mode,
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
