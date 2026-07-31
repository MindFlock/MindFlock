"""User settings store — the productization layer.

A per-user JSON store at ``~/.mindflock/settings.json`` that lets a new user
configure MindFlock entirely from the web Settings UI (API keys, binary paths,
repo + ticketing config, coding-CLI providers) without editing any committed
file or matching the original developer's machine.

This module is **purely additive**: it does NOT touch the Go-wire
``~/.mindflock/config.json`` (:mod:`backend.config.config`) nor the pipeline's
``config.toml`` (:mod:`backend.ticket_ingestion.config`). Instead it sits as a
higher-priority layer above ``config.toml`` in one resolution order::

    env var  →  settings.json  →  config.toml (advanced override)  →  default

The single :func:`resolve` accessor implements that precedence. Callers pass the
value they already parsed out of ``config.toml`` as ``toml_value`` so this module
never has to know anything about TOML.

Every field is optional; an absent field (empty string / ``None``) falls through
to the next layer. A missing or corrupt ``settings.json`` yields an all-empty
:class:`Settings` (never raises), so the store is safe to read unconditionally.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.config.config import GetConfigDir

_logger = logging.getLogger(__name__)

__all__ = [
    "SettingsFileName",
    "SETTINGS_SCHEMA_VERSION",
    "GIT_TRANSPORTS",
    "CodingCliSettings",
    "TicketingSource",
    "TicketingSettings",
    "RepositorySettings",
    "GithubSettings",
    "EngineSettings",
    "UiSettings",
    "PlatformSettings",
    "GeneralSettings",
    "NotificationSettings",
    "Settings",
    "settings_path",
    "load_settings",
    "save_settings",
    "update_settings",
    "set_ticketing_sources",
    "invalidate",
    "resolve",
    "resolve_str",
    "resolve_int",
    "resolve_bool",
    "resolve_path",
]

SettingsFileName: str = "settings.json"

# Version of the settings.json document layout this build reads and writes.
# Same emit-on-deviation pattern as config/state.py: the key is only written
# when > 1, so every existing (implicitly v1) store serializes unchanged.
SETTINGS_SCHEMA_VERSION: int = 1

# Migration ladder: from-version -> function upgrading the parsed document one
# step (v -> v+1). Applied in Settings.from_dict when the file is older than
# SETTINGS_SCHEMA_VERSION. Empty today.
_SETTINGS_MIGRATIONS: dict = {}


# --------------------------------------------------------------------------- #
# Grouped settings dataclasses. Every field is optional; the "unset" sentinel is
# "" for strings, {} for maps, None for optional scalars. to_dict emits only the
# groups/fields that carry a value so the on-disk file stays minimal and a hand
# edit that drops a key simply falls through to the next resolution layer.
# --------------------------------------------------------------------------- #
@dataclass
class CodingCliSettings:
    #: Preferred provider name for new sessions (e.g. "claude", "codex").
    default_provider: str = ""
    #: provider-name -> absolute binary path override.
    binary_paths: Dict[str, str] = field(default_factory=dict)
    #: Default launch flags, keyed by PROVIDER NAME (e.g. "claude", "codex").
    #: The flags for a session's provider are prepended to every new session of
    #: that provider, before any per-session flags. Each value is the raw flags
    #: string the Settings UI persists (e.g. "--dangerously-skip-permissions");
    #: split into argv tokens and validated at session-creation time. Flags are
    #: provider-specific (there is no flag common to every CLI), so a default set
    #: for claude never leaks onto a codex session. Empty map = no defaults.
    default_launch_args: Dict[str, str] = field(default_factory=dict)

    def launch_args_for(self, provider: str) -> str:
        """The raw default-flags string saved for ``provider``, or ``""``."""
        if not provider:
            return ""
        return self.default_launch_args.get(provider, "") or ""

    def to_dict(self) -> dict:
        d: dict = {}
        if self.default_provider:
            d["default_provider"] = self.default_provider
        if self.binary_paths:
            # Drop empty-string overrides so a cleared field falls through.
            paths = {k: v for k, v in self.binary_paths.items() if k and v}
            if paths:
                d["binary_paths"] = paths
        if self.default_launch_args:
            # Drop empty entries so a cleared provider field falls through.
            la = {
                k: v
                for k, v in self.default_launch_args.items()
                if k and isinstance(v, str) and v.strip()
            }
            if la:
                d["default_launch_args"] = la
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CodingCliSettings":
        raw_paths = d.get("binary_paths") or {}
        paths = (
            {
                str(k): str(v)
                for k, v in raw_paths.items()
                if isinstance(k, str) and isinstance(v, str)
            }
            if isinstance(raw_paths, dict)
            else {}
        )
        default_provider = str(d.get("default_provider", "") or "")
        raw_la = d.get("default_launch_args")
        if isinstance(raw_la, dict):
            launch_args = {
                str(k): str(v)
                for k, v in raw_la.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
        else:
            launch_args = {}
        return cls(
            default_provider=default_provider,
            binary_paths=paths,
            default_launch_args=launch_args,
        )


@dataclass
class TicketingSource:
    """One configured ticketing source. Field keys map 1:1 onto
    :class:`~backend.ticket_ingestion.config.TicketProviderConfig`.

    ``id`` is a stable, per-source discriminator (also the slug/branch prefix and
    poll-state key) so several sources — including multiple of the *same*
    provider — never collide. ``api_token`` is a SECRET (masked by the addon)."""

    id: str = ""
    provider: str = ""
    api_token: str = ""  # SECRET
    base_url: str = ""
    email: str = ""
    member_id: str = ""
    project: str = ""
    workflow_state: str = ""
    workflow_state_id: Optional[int] = None
    poll_interval_seconds: Optional[int] = None
    label: str = ""
    # Git clone URL / local path this source's tickets provision into. Empty =
    # fall back to the global repository.url (single-repo behavior).
    repo_url: str = ""

    def to_dict(self) -> dict:
        d: dict = {}
        for k in (
            "id",
            "provider",
            "api_token",
            "base_url",
            "email",
            "member_id",
            "project",
            "workflow_state",
            "label",
            "repo_url",
        ):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.workflow_state_id is not None:
            d["workflow_state_id"] = self.workflow_state_id
        if self.poll_interval_seconds is not None:
            d["poll_interval_seconds"] = self.poll_interval_seconds
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TicketingSource":
        return cls(
            id=str(d.get("id", "") or ""),
            provider=str(d.get("provider", "") or ""),
            api_token=str(d.get("api_token", "") or ""),
            base_url=str(d.get("base_url", "") or ""),
            email=str(d.get("email", "") or ""),
            member_id=str(d.get("member_id", "") or ""),
            project=str(d.get("project", "") or ""),
            workflow_state=str(d.get("workflow_state", "") or ""),
            workflow_state_id=_opt_int(d.get("workflow_state_id")),
            poll_interval_seconds=_opt_int(d.get("poll_interval_seconds")),
            label=str(d.get("label", "") or ""),
            repo_url=str(d.get("repo_url", "") or ""),
        )


@dataclass
class TicketingSettings:
    """All configured ticketing sources.

    Stored as ``{"sources": [ {…}, {…} ]}`` — one or many, mixing providers and
    even multiple of the same provider (distinct ``id``/credentials)."""

    sources: list = field(default_factory=list)  # list[TicketingSource]

    def to_dict(self) -> dict:
        arr = [s.to_dict() for s in self.sources if s.provider]
        return {"sources": arr} if arr else {}

    @classmethod
    def from_dict(cls, d: dict) -> "TicketingSettings":
        raw = d.get("sources")
        if isinstance(raw, list) and raw:
            sources = [TicketingSource.from_dict(s) for s in raw if isinstance(s, dict)]
        else:
            sources = []
        return cls(sources=sources)


#: Accepted ``[repository].git_transport`` values. ``"auto"`` matches the
#: transport of the user's own ``url`` when it names the repo being cloned and
#: falls back to HTTPS otherwise; ``"ssh"``/``"https"`` force one spelling.
GIT_TRANSPORTS = ("auto", "ssh", "https")


@dataclass
class RepositorySettings:
    url: str = ""
    workspace_dir: str = ""
    base_branch: str = ""
    # The branch the "Make PR" button targets. When set it overrides the
    # per-session fork-point (K1) as the PR base, so a plain worktree session
    # PRs into e.g. "staging" instead of whatever branch it happened to be cut
    # from. Blank = use the session's own base (the prior behaviour).
    pr_base_branch: str = ""
    # Which transport the ingestion pipeline clones with when it only knows a
    # repo by its owner/name slug (see
    # :mod:`backend.ticket_ingestion.clone_transport`). One of GIT_TRANSPORTS;
    # blank = unset, which falls through the resolution chain to "auto".
    # NOT a rewrite of `url` — that spelling is always used verbatim.
    git_transport: str = ""

    def to_dict(self) -> dict:
        d: dict = {}
        if self.url:
            d["url"] = self.url
        if self.workspace_dir:
            d["workspace_dir"] = self.workspace_dir
        if self.base_branch:
            d["base_branch"] = self.base_branch
        if self.pr_base_branch:
            d["pr_base_branch"] = self.pr_base_branch
        if self.git_transport:
            d["git_transport"] = self.git_transport
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RepositorySettings":
        return cls(
            url=str(d.get("url", "") or ""),
            workspace_dir=str(d.get("workspace_dir", "") or ""),
            base_branch=str(d.get("base_branch", "") or ""),
            pr_base_branch=str(d.get("pr_base_branch", "") or ""),
            git_transport=_git_transport(d.get("git_transport")),
        )


@dataclass
class GithubSettings:
    """The ``[github]`` block — the automated PR-review + issue-handling features.

    When ``enabled``, the ingestion pipeline polls every repo in ``repos`` for
    the user's own open PRs against ``base_branch`` and auto-runs a coding
    session to handle review comments. ``min_age_minutes`` /
    ``poll_interval_seconds`` / ``skip_authors`` tune that loop; all are optional
    and fall through to the engine defaults (15 min / 60 s / none) when unset.

    ``issues_enabled`` gates the sibling issue-handling loop: newly opened
    issues in ``issue_repos`` (its OWN repo list, independent of PR review's
    ``repos``) are picked up (with their comments) and a coding session
    starts work on each in a fresh branch. Unlike ``enabled`` (unset counts
    as on), issue handling is opt-in — unset counts as OFF. It has its OWN
    tuning knobs too — ``issue_min_age_minutes`` / ``issue_poll_interval_seconds``
    / ``issue_skip_authors`` — independent of PR review's; all optional and
    falling through to the same engine defaults (15 min / 60 s / none).

    ``repos`` is the list of ``owner/name`` repos PR review watches;
    ``issue_repos`` is the list issue handling watches. ``token`` (the GitHub
    credential) is shared by both — it authenticates the same account.
    """

    token: str = ""  # SECRET
    base_branch: str = ""
    enabled: Optional[bool] = None
    issues_enabled: Optional[bool] = None
    min_age_minutes: Optional[int] = None
    poll_interval_seconds: Optional[int] = None
    skip_authors: List[str] = field(default_factory=list)
    repos: List[str] = field(default_factory=list)
    issue_repos: List[str] = field(default_factory=list)
    # Issue-handling tuning, independent of the PR-review knobs above.
    issue_min_age_minutes: Optional[int] = None
    issue_poll_interval_seconds: Optional[int] = None
    issue_skip_authors: List[str] = field(default_factory=list)

    def repo_list(self) -> List[str]:
        """Effective ``owner/name`` repos to watch (blanks stripped)."""
        return [r.strip() for r in (self.repos or []) if r and r.strip()]

    def issue_repo_list(self) -> List[str]:
        """Effective ``owner/name`` repos issue handling watches."""
        return [r.strip() for r in (self.issue_repos or []) if r and r.strip()]

    def to_dict(self) -> dict:
        d: dict = {}
        if self.token:
            d["token"] = self.token
        if self.base_branch:
            d["base_branch"] = self.base_branch
        if self.enabled is not None:
            d["enabled"] = self.enabled
        if self.issues_enabled is not None:
            d["issues_enabled"] = self.issues_enabled
        if self.min_age_minutes is not None:
            d["min_age_minutes"] = self.min_age_minutes
        if self.poll_interval_seconds is not None:
            d["poll_interval_seconds"] = self.poll_interval_seconds
        if self.skip_authors:
            d["skip_authors"] = list(self.skip_authors)
        if self.repos:
            d["repos"] = list(self.repos)
        if self.issue_repos:
            d["issue_repos"] = list(self.issue_repos)
        if self.issue_min_age_minutes is not None:
            d["issue_min_age_minutes"] = self.issue_min_age_minutes
        if self.issue_poll_interval_seconds is not None:
            d["issue_poll_interval_seconds"] = self.issue_poll_interval_seconds
        if self.issue_skip_authors:
            d["issue_skip_authors"] = list(self.issue_skip_authors)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GithubSettings":
        return cls(
            token=str(d.get("token", "") or ""),
            base_branch=str(d.get("base_branch", "") or ""),
            enabled=_opt_bool(d.get("enabled")),
            issues_enabled=_opt_bool(d.get("issues_enabled")),
            min_age_minutes=_opt_int(d.get("min_age_minutes")),
            poll_interval_seconds=_opt_int(d.get("poll_interval_seconds")),
            skip_authors=_str_list(d.get("skip_authors")),
            repos=_str_list(d.get("repos")),
            issue_repos=_str_list(d.get("issue_repos")),
            issue_min_age_minutes=_opt_int(d.get("issue_min_age_minutes")),
            issue_poll_interval_seconds=_opt_int(d.get("issue_poll_interval_seconds")),
            issue_skip_authors=_str_list(d.get("issue_skip_authors")),
        )


@dataclass
class EngineSettings:
    """The optional engine block (config.toml's ``[mindflock]`` section)."""

    enabled: Optional[bool] = None
    mode: str = ""  # worktree | clone
    open_cursor: Optional[bool] = None
    skip_permissions: Optional[bool] = None

    def to_dict(self) -> dict:
        d: dict = {}
        if self.enabled is not None:
            d["enabled"] = self.enabled
        if self.mode:
            d["mode"] = self.mode
        if self.open_cursor is not None:
            d["open_cursor"] = self.open_cursor
        if self.skip_permissions is not None:
            d["skip_permissions"] = self.skip_permissions
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EngineSettings":
        return cls(
            enabled=_opt_bool(d.get("enabled")),
            mode=str(d.get("mode", "") or ""),
            open_cursor=_opt_bool(d.get("open_cursor")),
            skip_permissions=_opt_bool(d.get("skip_permissions")),
        )


@dataclass
class UiSettings:
    scroll_speed: Optional[int] = None
    cursor_autoadopt: Optional[bool] = None
    # Appearance themes (Settings → Appearance): accent preset name and surface
    # (background/panel/border/text) preset name. Empty = built-in defaults.
    # Persisted server-side so desktop and mobile (/m) share the same look; the
    # values are plain preset names — the actual colors live in theme.css.
    accent: str = ""
    surface: str = ""

    def to_dict(self) -> dict:
        d: dict = {}
        if self.scroll_speed is not None:
            d["scroll_speed"] = self.scroll_speed
        if self.cursor_autoadopt is not None:
            d["cursor_autoadopt"] = self.cursor_autoadopt
        if self.accent:
            d["accent"] = self.accent
        if self.surface:
            d["surface"] = self.surface
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "UiSettings":
        return cls(
            scroll_speed=_opt_int(d.get("scroll_speed")),
            cursor_autoadopt=_opt_bool(d.get("cursor_autoadopt")),
            accent=str(d.get("accent") or ""),
            surface=str(d.get("surface") or ""),
        )


@dataclass
class PlatformSettings:
    """OS-integration knobs (IDE / WSL / Windows Terminal). Empty = use
    built-in defaults ("cursor" / "Ubuntu" / "wt.exe")."""

    wsl_distro: str = ""
    wt_command: str = ""
    #: Editor CLI used to open workspaces (e.g. "cursor", "code", "windsurf").
    #: Resolved by :mod:`backend.config.ide`; empty = Cursor.
    ide_command: str = ""

    def to_dict(self) -> dict:
        d: dict = {}
        if self.wsl_distro:
            d["wsl_distro"] = self.wsl_distro
        if self.wt_command:
            d["wt_command"] = self.wt_command
        if self.ide_command:
            d["ide_command"] = self.ide_command
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PlatformSettings":
        return cls(
            wsl_distro=str(d.get("wsl_distro", "") or ""),
            wt_command=str(d.get("wt_command", "") or ""),
            ide_command=str(d.get("ide_command", "") or ""),
        )


@dataclass
class GeneralSettings:
    """Cross-cutting knobs that don't belong to one integration.

    ``session_budget_usd``: per-session cost guardrail (J5). When a session's
    estimated cost crosses this figure the server emits a one-shot
    ``session.budget_exceeded`` event on the bus (toast / notify addon / shell
    hooks). ``None`` or ``0`` = guardrail off.

    ``window_budget_usd``: the user's estimate of their plan's rolling-window
    allowance in API-equivalent dollars — the denominator for the header's
    best-effort "% used" on subscription plans. ``None``/``0`` = unknown (the
    UI shows only the reset countdown, never a made-up percent).

    ``auth_token``: shared bearer token for the web server's auth gate
    (:mod:`backend.web.core.auth`). Auto-generated + persisted here on first
    tailnet start when nothing is configured; treated as a SECRET (masked on
    read, keep-on-empty on write) by the settings addon.

    ``auth_mode``: user's explicit choice for the auth gate — ``"on"`` (always
    require the token), ``"off"`` (never), or ``"auto"``/``""`` (the default:
    on only when the server is exposed beyond localhost). Overrides the
    exposed-mode heuristic; the ``MINDFLOCK_AUTH`` env var still wins over it.

    ``onboarded``: set once the user has ever created a session (or finished the
    first-run checklist). Gates the first-run setup card so it only auto-shows
    for a brand-new install, not every time the grid happens to be empty.

    ``remote_control``: whether OTHER MindFlock devices on the tailnet may
    control this one (:mod:`backend.web.core.remote`) — ``"on"`` allows it,
    ``""``/``"off"`` (the default) refuses remote-flagged requests and
    advertises ``remote_control: false`` in the discovery hello. The
    controller-side device tokens are NOT stored here (they live in
    ``remote_devices.json`` next to the state file, outside the settings
    document, so they never transit the settings GET).

    ``serve_mode``: the server bind mode used when no explicit mode is given
    (CLI arg / ``CS_WEB_MODE``) — ``"tailscale"`` binds 0.0.0.0 for phone
    access, ``""``/``"local"`` binds 127.0.0.1. This is what makes the
    Settings → Mobile toggle stick across restarts: the desktop app's
    auto-start runs a bare ``mindflock serve``, which falls back to this.

    ``ingestion_autostart``: the last state the ticket-ingestion toggle was
    set to (written by ``/api/mindflock/start|stop``). ``True`` makes the
    web server start the pipeline on boot, so a reboot restores the toggle
    to where the user left it; ``None`` (never toggled) / ``False`` = stay
    stopped.
    """

    session_budget_usd: Optional[float] = None
    window_budget_usd: Optional[float] = None
    auth_token: str = ""  # SECRET
    auth_mode: str = ""  # "" / "auto" | "on" | "off"
    onboarded: bool = False
    remote_control: str = ""  # "" / "off" | "on"
    serve_mode: str = ""  # "" / "local" | "tailscale"
    ingestion_autostart: Optional[bool] = None

    def to_dict(self) -> dict:
        d: dict = {}
        if self.session_budget_usd is not None:
            d["session_budget_usd"] = self.session_budget_usd
        if self.window_budget_usd is not None:
            d["window_budget_usd"] = self.window_budget_usd
        if self.auth_token:
            d["auth_token"] = self.auth_token
        if self.auth_mode:
            d["auth_mode"] = self.auth_mode
        if self.onboarded:
            d["onboarded"] = True
        if self.remote_control:
            d["remote_control"] = self.remote_control
        if self.serve_mode:
            d["serve_mode"] = self.serve_mode
        if self.ingestion_autostart is not None:
            d["ingestion_autostart"] = self.ingestion_autostart
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GeneralSettings":
        return cls(
            session_budget_usd=_opt_float(d.get("session_budget_usd")),
            window_budget_usd=_opt_float(d.get("window_budget_usd")),
            auth_token=str(d.get("auth_token", "") or ""),
            auth_mode=str(d.get("auth_mode", "") or "").strip().lower(),
            onboarded=bool(d.get("onboarded", False)),
            remote_control=str(d.get("remote_control", "") or "").strip().lower(),
            serve_mode=str(d.get("serve_mode", "") or "").strip().lower(),
            ingestion_autostart=_opt_bool(d.get("ingestion_autostart")),
        )


@dataclass
class NotificationSettings:
    """Which notification rules the user has toggled off/on, and the optional
    ntfy push channel that delivers them to a phone.

    Rules carry a per-rule default (see ``notify.NOTIFY_RULES``):

    * **default-on** rules fire unless their id is in ``muted_rules`` (opt-out),
      so new default-on rules added later start enabled.
    * **default-off** rules (e.g. "went idle", "pre-commit hooks running" — noisy
      by nature) fire only when their id is in ``enabled_rules`` (opt-in).

    The rule list is shared by every delivery channel — one "what notifies me"
    list, not one per channel. The ``ntfy_*`` fields configure the second
    channel (:mod:`backend.web.core.ntfy`), which is off until the user opts in:

    ``ntfy_enabled``: master switch for server-side pushes.
    ``ntfy_server``: base URL of the ntfy server (``""`` -> the public
    ``https://ntfy.sh``). ``ntfy_topic``: the topic to publish to — on a public
    server the topic name IS the credential, so it should be long and random.
    ``ntfy_token``: access token for a protected topic / self-hosted server, a
    SECRET (masked on read, keep-on-empty on write by the settings addon).
    ``ntfy_click_url``: optional URL a tapped notification opens (your phone
    MindFlock URL); never store an access token in it — it would be handed to
    the ntfy server.
    """

    muted_rules: List[str] = field(default_factory=list)
    enabled_rules: List[str] = field(default_factory=list)
    ntfy_enabled: bool = False
    ntfy_server: str = ""  # "" -> ntfy.ntfy DEFAULT_SERVER (https://ntfy.sh)
    ntfy_topic: str = ""
    ntfy_token: str = ""  # SECRET
    ntfy_click_url: str = ""

    def to_dict(self) -> dict:
        out: dict = {}
        if self.muted_rules:
            out["muted_rules"] = list(self.muted_rules)
        if self.enabled_rules:
            out["enabled_rules"] = list(self.enabled_rules)
        if self.ntfy_enabled:
            out["ntfy_enabled"] = True
        if self.ntfy_server:
            out["ntfy_server"] = self.ntfy_server
        if self.ntfy_topic:
            out["ntfy_topic"] = self.ntfy_topic
        if self.ntfy_token:
            out["ntfy_token"] = self.ntfy_token
        if self.ntfy_click_url:
            out["ntfy_click_url"] = self.ntfy_click_url
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "NotificationSettings":
        return cls(
            muted_rules=_str_list(d.get("muted_rules")),
            enabled_rules=_str_list(d.get("enabled_rules")),
            ntfy_enabled=bool(d.get("ntfy_enabled", False)),
            ntfy_server=str(d.get("ntfy_server", "") or "").strip(),
            ntfy_topic=str(d.get("ntfy_topic", "") or "").strip(),
            ntfy_token=str(d.get("ntfy_token", "") or "").strip(),
            ntfy_click_url=str(d.get("ntfy_click_url", "") or "").strip(),
        )


@dataclass
class Settings:
    """The whole user settings document. All groups default to empty.

    ``schema_version`` is the document layout version (missing key -> 1). It
    is only emitted when > 1 so today's stores serialize unchanged; a store
    written by a *newer* MindFlock is loaded best-effort with a warning
    (least-destructive choice: settings are a fall-through overlay — an
    unknown field simply falls through to config.toml / defaults — and the
    version stamp is preserved on save so the newer build can recognize it).
    """

    coding_cli: CodingCliSettings = field(default_factory=CodingCliSettings)
    ticketing: TicketingSettings = field(default_factory=TicketingSettings)
    repository: RepositorySettings = field(default_factory=RepositorySettings)
    github: GithubSettings = field(default_factory=GithubSettings)
    engine: EngineSettings = field(default_factory=EngineSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    platform: PlatformSettings = field(default_factory=PlatformSettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    schema_version: int = 1

    def to_dict(self) -> dict:
        """Serialize to a minimal dict: omit any group that carries no value."""
        out: dict = {}
        # Emit-on-deviation, like every other field in this store.
        if self.schema_version > 1:
            out["schema_version"] = self.schema_version
        for key, group in self._groups().items():
            gd = group.to_dict()
            if gd:
                out[key] = gd
        return out

    def _groups(self) -> Dict[str, Any]:
        return {
            "coding_cli": self.coding_cli,
            "ticketing": self.ticketing,
            "repository": self.repository,
            "github": self.github,
            "engine": self.engine,
            "ui": self.ui,
            "platform": self.platform,
            "general": self.general,
            "notifications": self.notifications,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Settings":
        """Parse a settings document. Tolerant: a non-dict, missing groups, or
        unknown keys never raise — they just yield empty groups."""
        d = d if isinstance(d, dict) else {}
        # Schema version: missing/invalid -> 1; older versions walk the
        # migration ladder; a newer version loads best-effort (see class
        # docstring) with a warning, keeping its stamp for round-trip.
        version = _opt_int(d.get("schema_version")) or 1
        if version > SETTINGS_SCHEMA_VERSION:
            _logger.warning(
                "settings.json has schema v%d but this build supports v%d "
                "(written by a newer MindFlock); loading best-effort — "
                "unknown fields fall through to config.toml/defaults",
                version,
                SETTINGS_SCHEMA_VERSION,
            )
        else:
            while version < SETTINGS_SCHEMA_VERSION:
                migrate = _SETTINGS_MIGRATIONS.get(version)
                if migrate is None:
                    break  # tolerant store: never raise on load
                migrated = migrate(d)
                d = migrated if isinstance(migrated, dict) else d
                version += 1
        return cls(
            coding_cli=CodingCliSettings.from_dict(_group(d, "coding_cli")),
            ticketing=TicketingSettings.from_dict(_group(d, "ticketing")),
            repository=RepositorySettings.from_dict(_group(d, "repository")),
            github=GithubSettings.from_dict(_group(d, "github")),
            engine=EngineSettings.from_dict(_group(d, "engine")),
            ui=UiSettings.from_dict(_group(d, "ui")),
            platform=PlatformSettings.from_dict(_group(d, "platform")),
            general=GeneralSettings.from_dict(_group(d, "general")),
            notifications=NotificationSettings.from_dict(_group(d, "notifications")),
            schema_version=version,
        )


# --------------------------------------------------------------------------- #
# Coercion helpers (tolerant — a bad value becomes "unset" rather than raising).
# --------------------------------------------------------------------------- #
def _opt_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        s = str(v).strip()
        return int(s) if s else None
    except (TypeError, ValueError):
        return None


def _opt_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def _opt_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


def _git_transport(v: Any) -> str:
    """Normalize a ``git_transport`` value (see :data:`GIT_TRANSPORTS`).

    Tolerant like every other coercion here: a typo ("shh", "SSH2") becomes
    ``"auto"`` — the safe default — rather than raising and taking the whole
    store down with it. Missing/blank stays ``""`` so it falls through to the
    next resolution layer instead of pinning "auto" over a config.toml value.
    """
    s = str(v or "").strip().lower()
    if not s:
        return ""
    return s if s in GIT_TRANSPORTS else "auto"


def _str_list(v: Any) -> List[str]:
    """Normalize a stored value into a clean list of strings.

    Accepts a JSON list (from the store / API) or a comma-separated string
    (what the settings form's text input sends), dropping blanks and stray
    whitespace. Anything else yields an empty list.
    """
    if isinstance(v, (list, tuple)):
        items = v
    elif isinstance(v, str):
        items = v.split(",")
    else:
        return []
    return [s for s in (str(x).strip() for x in items) if s]


def _group(d: dict, key: str) -> dict:
    g = d.get(key)
    return g if isinstance(g, dict) else {}


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #
def settings_path() -> Path:
    """Path to ``settings.json``.

    Honors ``$MINDFLOCK_SETTINGS_FILE`` (used by tests to point at a tmp file);
    otherwise ``<config dir>/settings.json`` (``~/.mindflock/settings.json``).
    """
    env = os.environ.get("MINDFLOCK_SETTINGS_FILE")
    if env:
        return Path(env)
    return Path(GetConfigDir()) / SettingsFileName


# Process-lifetime cache of the parsed store. `resolve` reads it on the hot path
# (once per config field), so we avoid re-reading + re-parsing the file each
# call. Any save invalidates it; tests can call `invalidate()` directly.
_CACHE: Optional[Settings] = None
_CACHE_PATH: Optional[str] = None


def invalidate() -> None:
    """Drop the in-memory settings cache (call after an out-of-band file change
    or when switching ``$MINDFLOCK_SETTINGS_FILE`` mid-process, e.g. in tests)."""
    global _CACHE, _CACHE_PATH
    _CACHE = None
    _CACHE_PATH = None


def load_settings() -> Settings:
    """Load the settings store, or an all-empty :class:`Settings` on any problem.

    Never raises: a missing file, a permission error, or corrupt JSON all yield
    empty settings so callers can read the store unconditionally. Cached for the
    process; the cache keys on the resolved path so a changed
    ``$MINDFLOCK_SETTINGS_FILE`` is picked up.
    """
    global _CACHE, _CACHE_PATH
    path = settings_path()
    key = str(path)
    if _CACHE is not None and _CACHE_PATH == key:
        return _CACHE

    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        parsed = {}

    settings = Settings.from_dict(parsed if isinstance(parsed, dict) else {})
    _CACHE = settings
    _CACHE_PATH = key
    return settings


def save_settings(settings: Settings) -> None:
    """Persist the settings store atomically with owner-only permissions.

    Writes ``settings.json`` (mode 0600, dir 0700 — it holds secrets) via a
    temp file + ``os.replace`` so a concurrent reader never sees a partial file.
    Invalidates the in-memory cache.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort (e.g. a tmp dir we don't own)

    data = json.dumps(settings.to_dict(), indent=2, ensure_ascii=False) + "\n"

    # Atomic write: temp file in the same dir, then rename over the target.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        invalidate()


def update_settings(**group_patches: dict) -> Settings:
    """Read-modify-write one or more groups and persist.

    Each keyword is a group name (``github``, ``coding_cli`` …) mapped to a
    dict of field->value. Only the provided fields are changed; the rest of the
    store is preserved. An empty-string value for a field clears it (falls back
    through the resolution chain). Returns the new state.

    Example::

        update_settings(github={"token": "ghp_…"}, repository={"url": "…"})
    """
    current = load_settings()
    merged = current.to_dict()
    for group, patch in group_patches.items():
        if not isinstance(patch, dict):
            continue
        base = dict(merged.get(group, {}))
        for k, v in patch.items():
            if v is None or v == "":
                base.pop(k, None)  # clear -> fall through to next layer
            else:
                base[k] = v
        if base:
            merged[group] = base
        else:
            merged.pop(group, None)
    new_settings = Settings.from_dict(merged)
    save_settings(new_settings)
    return new_settings


def set_ticketing_sources(sources: list) -> Settings:
    """Replace the whole ticketing sources list and persist.

    ``sources`` is a list of dicts (each a :class:`TicketingSource` shape). The
    field-merge :func:`update_settings` can't express a list replacement, so the
    ticketing CRUD endpoints go through here. Returns the new state.
    """
    current = load_settings()
    merged = current.to_dict()
    clean = [s for s in (sources or []) if isinstance(s, dict) and s.get("provider")]
    if clean:
        merged["ticketing"] = {"sources": clean}
    else:
        merged.pop("ticketing", None)
    new_settings = Settings.from_dict(merged)
    save_settings(new_settings)
    return new_settings


# --------------------------------------------------------------------------- #
# The single resolution accessor: env → settings.json → config.toml → default
# --------------------------------------------------------------------------- #
_SENTINEL = object()


def _is_unset(v: Any) -> bool:
    """A value counts as 'unset' (fall through) when it is None, "", or {}."""
    return v is None or v == "" or v == {}


def resolve(
    *,
    env: Optional[str],
    settings_getter: Callable[[Settings], Any],
    toml_value: Any = _SENTINEL,
    default: Any,
    coerce: Optional[Callable[[str], Any]] = None,
) -> Any:
    """Resolve one value through ``env → settings.json → config.toml → default``.

    * ``env``: environment variable name to check first (or ``None`` to skip).
    * ``settings_getter``: reads the field from a :class:`Settings` (e.g.
      ``lambda s: s.github.token``).
    * ``toml_value``: the value the caller already parsed from ``config.toml``
      (pass :data:`_SENTINEL`/omit when there is none).
    * ``default``: the built-in fallback.
    * ``coerce``: applied to a raw *env-var string* (env values are always str).

    A value is skipped ("unset") when it is ``None``, ``""`` or ``{}``.
    """
    if env:
        ev = os.environ.get(env)
        if ev is not None and ev != "":
            return coerce(ev) if coerce else ev

    sv = settings_getter(load_settings())
    if not _is_unset(sv):
        return sv

    if toml_value is not _SENTINEL and not _is_unset(toml_value):
        return toml_value

    return default


def resolve_str(
    *, env=None, settings_getter, toml_value=_SENTINEL, default: str = ""
) -> str:
    return resolve(
        env=env, settings_getter=settings_getter, toml_value=toml_value, default=default
    )


def resolve_int(
    *, env=None, settings_getter, toml_value=_SENTINEL, default: Optional[int] = None
) -> Optional[int]:
    return resolve(
        env=env,
        settings_getter=settings_getter,
        toml_value=toml_value,
        default=default,
        coerce=_opt_int,
    )


def resolve_bool(
    *, env=None, settings_getter, toml_value=_SENTINEL, default: Optional[bool] = None
) -> Optional[bool]:
    return resolve(
        env=env,
        settings_getter=settings_getter,
        toml_value=toml_value,
        default=default,
        coerce=_opt_bool,
    )


def resolve_path(
    *, env=None, settings_getter, toml_value=_SENTINEL, default: Optional[Path] = None
) -> Optional[Path]:
    v = resolve(
        env=env, settings_getter=settings_getter, toml_value=toml_value, default=default
    )
    if v is None or v == "":
        return None
    return v if isinstance(v, Path) else Path(str(v)).expanduser()
