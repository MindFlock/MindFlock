"""What the voice assistant may do: the typed catalog it picks from.

The assistant (:mod:`backend.web.core.voice_assistant`, docs/voice-assistant.md)
never improvises: the model names an action from this catalog and the server
validates it here before anything runs — the way Android AppFunctions /
Apple App Intents expose an app. Three kinds of entries:

* **Settings** (:func:`settings`): every plain ``group.field`` of
  :class:`backend.config.settings.Settings` that is a bool, a string or an
  int, labelled from ``settings_sync.LABELS`` (else from the field's name),
  with ``options`` where the field takes a fixed set. Secrets
  (``*_token``, ``*_api_key``, the settings addon's ``_SECRET_FIELDS``,
  ``settings_sync.SECRET_PATHS``) and the fields that open the machine or
  weaken a gate (:data:`EXCLUDED`: auth, binding, tailnet, peer links,
  permission skipping, binaries) are never in it — a request for one is
  refused, whatever the model says.
* **Client surfaces** mirrored from the web UI: :data:`DIALOGS`
  (frontend/src/state/store.ts ``DialogName``), :data:`SETTINGS_SCREENS`
  (frontend/src/components/settings/SettingsDialog.tsx ``SCREENS``) and
  :data:`SESSION_ACTIONS` (frontend/src/lib/sessionActionTable.ts
  ``SESSION_ACTIONS``). tests/unit/test_voice_assistant_catalog.py parses
  those TypeScript files and fails when either side drifts.
* **Destructive** markers: the session actions and click labels that always
  go through a spoken confirmation.

Pure but for :func:`settings` reading the live settings for each value.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from typing import Any, Dict, List, Optional

__all__ = [
    "DESTRUCTIVE_SESSION_ACTIONS",
    "DIALOGS",
    "EXCLUDED",
    "OPEN_DIALOGS",
    "SESSION_ACTIONS",
    "SETTINGS_SCREENS",
    "catalog",
    "check_setting",
    "is_destructive_click",
    "open_targets",
    "settings",
]

#: Every ``DialogName`` (frontend/src/state/store.ts), in its order.
DIALOGS = (
    "new-session",
    "settings",
    "intake",
    "customize",
    "verify",
    "commit",
    "make-pr",
    "rename",
    "move",
    "recent",
    "prompts",
    "setup",
    "todo",
    "assistant-agent",
    "palette",
    "shortcuts",
    "red-zones",
    "schedule",
    "templates",
    "flights",
    "extension",
    "peer-session",
    "ask",
)
#: The dialogs ``{"do": "open"}`` may name: those that open without a
#: target. The per-session ones (commit, make-pr, rename, move) are
#: ``session_action``s; "templates" / "flights" are legacy names of
#: "schedule" and the sky; extension / peer-session / ask need a payload.
OPEN_DIALOGS = tuple(
    d
    for d in DIALOGS
    if d
    not in (
        "commit",
        "make-pr",
        "rename",
        "move",
        "templates",
        "flights",
        "extension",
        "peer-session",
        "ask",
    )
)
#: Friendly names a person says for a dialog (the instant path and the
#: prompt both use them).
DIALOG_LABELS: Dict[str, str] = {
    "new-session": "New session",
    "settings": "Settings",
    "intake": "Intake (tickets, PRs, issues)",
    "customize": "Customize (sidebar, layout)",
    "verify": "Verify (checklists)",
    "recent": "Recently closed and workspaces",
    "prompts": "Saved prompts",
    "setup": "Setup",
    "todo": "To-do list",
    "assistant-agent": "Assistant agent chat",
    "palette": "Command palette",
    "shortcuts": "Keyboard shortcuts",
    "red-zones": "Red zones",
    "schedule": "Schedule and templates",
}

#: Settings screens (``SCREENS`` key → label), in SettingsDialog.tsx order.
SETTINGS_SCREENS: Dict[str, str] = {
    "general": "General",
    "connections": "Connections",
    "notifications": "Notifications",
    "coding": "Agents",
    "providers": "Custom agents",
    "accounts": "Accounts",
    "localmodel": "Local model",
    "orchestration": "Agent orchestration",
    "workspace": "Workspace",
    "ide": "IDE",
    "security": "Security",
    "appearance": "Appearance",
    "devices": "Your devices",
    "mobile": "Your phone",
    "voice": "Voice",
    "peer": "People",
    "extensions": "Extensions",
    "doctor": "Doctor",
    "logs": "System logs",
    "advanced": "Engine & updates",
    "traffic": "Site traffic",
}
#: Screens the assistant never opens ("traffic" is a maintainer-only screen).
_HIDDEN_SCREENS = frozenset({"traffic"})

#: ``SESSION_ACTIONS`` ids (sessionActionTable.ts) → what they do.
SESSION_ACTIONS: Dict[str, str] = {
    "commit": "Commit",
    "push": "Push",
    "pr": "Make a pull request",
    "merge": "Merge the pull request",
    "viewPr": "Open the pull request",
    "rename": "Rename",
    "move": "Move to a folder",
    "message": "Message another session",
    "queue": "Queue a prompt",
    "fastTrack": "Goes as far as (autopilot)",
    "workers": "Workers",
    "split": "Split into workers",
    "askWorkers": "Ask for workers",
    "flock": "Flock mode",
    "thread": "Thread",
    "ide": "Open in the IDE",
    "duplicate": "Duplicate",
    "branch": "Branch the session",
    "hide": "Hide",
    "map": "Code map",
    "pause": "Pause",
    "resume": "Resume",
    "close": "Close",
    "delete": "Delete",
}
#: Session actions that always ask "Are you sure?" first.
DESTRUCTIVE_SESSION_ACTIONS = frozenset({"delete", "close", "merge"})
#: A click on a control so labelled always asks first.
_DESTRUCTIVE_CLICK_RE = re.compile(
    r"\b(?:delete|remove|merge|discard|reset)\b", re.IGNORECASE
)

#: Settings the assistant never reads out or writes, beside every secret:
#: what opens this machine or weakens a gate (auth, binding, tailnet, phone
#: links, peers, permission skipping, the agent's scope), what runs a
#: program (binaries, IDE / terminal commands, hooks), endpoints a voice or
#: model is sent to, and bookkeeping no person sets by hand.
EXCLUDED = frozenset(
    {
        "general.auth_mode",
        "general.auth_token",
        "general.remote_control",
        "general.serve_mode",
        "general.shared_link",
        "general.tailnet_trusted_logins",
        "general.agent_mcp_scope",
        "general.setup",
        "general.setup_devices",
        "general.onboarded",
        "general.last_repo_path",
        "general.intake_paused",
        "general.default_folder",
        "engine.skip_permissions",
        "repository.workspace_dir",
        "repository.precommit_retry_hooks",
        "github.oauth_client_id",
        "github.automation_device",
        "local_model.base_url",
        "voice.stt_url",
        "voice.tts_url",
        "notifications.ntfy_server",
        "notifications.ntfy_topic",  # whoever knows it reads the pushes
        "repository.url",
        "notifications.ntfy_click_url",
        "prefs.whats_new_seen",
        "prefs.map_tour",
        "prefs.welcome_tour",
        "person.id",
    }
)
#: Whole groups the assistant leaves alone.
_EXCLUDED_GROUPS = frozenset(
    {"peer", "platform", "auth_profiles", "ticketing", "schema_version"}
)
_SECRET_NAME_RE = re.compile(r"(?:^|_)(?:token|api_key|secret|password)$")

#: Fixed option sets (the first is what "" means where blank is the default).
OPTIONS: Dict[str, List[str]] = {
    "voice.assistant_talk": ["quiet", "brief", "chatty"],
    "voice.stt": ["phone", "server"],
    "voice.tts": ["phone", "server"],
    "voice.stt_backend": ["", "system", "openai", "local"],
    "voice.tts_backend": ["", "kokoro", "system", "openai", "local"],
    "prefs.theme": ["", "light", "dark"],
    "engine.mode": ["worktree", "clone"],
    "repository.git_transport": ["auto", "ssh", "https"],
    "repository.fasttrack_depth": ["off", "agent", "commit", "push", "pr", "merge"],
}


#: Labels for fields ``settings_sync.LABELS`` doesn't name (it labels only
#: what syncs).
EXTRA_LABELS: Dict[str, str] = {
    "voice.stt": "Phone speech recognition (phone or server)",
    "voice.tts": "Phone voice (phone or server)",
    "voice.stt_backend": "Speech recognition engine",
    "voice.tts_backend": "Speech voice engine",
    "voice.stt_model": "Speech recognition model",
    "voice.stt_stream_model": "Live transcript model",
    "voice.stt_final": "Accurate pass model",
    "voice.tts_model": "Voice model",
    "voice.tts_voice": "Voice name",
    "voice.assistant_model": "Assistant model",
    "voice.assistant_talk": "Assistant talk",
    "voice.assistant_voice": "Assistant voice",
    "engine.enabled": "Ticket sessions on/off",
    "engine.mode": "Ticket sessions: worktree or clone",
    "engine.open_cursor": "Open ticket sessions in Cursor",
    "engine.max_sessions": "Ticket sessions: most at once",
    "local_model.enabled": "Local model on/off",
    "local_model.runtime": "Local model runtime",
    "local_model.model": "Local model name",
    "ui.cursor_autoadopt": "Adopt Cursor windows",
    "ui.surface": "Surface colour",
    "general.ingestion_autostart": "Start ticket intake with the server",
    "general.revive_on_start": "Revive sessions on start",
    "general.sleep_idle_minutes": "Sleep idle agents after (minutes)",
    "general.memory_sleep_minutes": "Sleep agents when memory is low after (minutes)",
    "general.memory_low_pct": "Low memory (percent free)",
    "general.memory_critical_pct": "Critical memory (percent free)",
    "general.max_live_agents": "Most live agents",
    "general.agent_warmup_mb": "Memory per agent start (MB)",
    "general.heavy_slots": "Heavy jobs at once",
}


def _secret_paths() -> frozenset:
    out = set()
    try:
        from backend.web.addons import settings as _addon

        out |= {"%s.%s" % gf for gf in getattr(_addon, "_SECRET_FIELDS", ())}
    except Exception:  # noqa: BLE001 — the name rule still holds
        pass
    try:
        from backend.web.core import settings_sync as _sync

        out |= set(_sync.SECRET_PATHS)
    except Exception:  # noqa: BLE001
        pass
    return frozenset(out)


def _labels() -> Dict[str, str]:
    try:
        from backend.web.core import settings_sync as _sync

        return dict(_sync.LABELS)
    except Exception:  # noqa: BLE001
        return {}


def _kind_of(tp: Any) -> Optional[str]:
    """``bool`` / ``str`` / ``int`` for a field's (Optional) type; None for
    anything else (lists, dicts, floats, nested settings)."""
    args = typing.get_args(tp)
    if typing.get_origin(tp) is typing.Union and len(args) == 2 and type(None) in args:
        tp = args[0] if args[1] is type(None) else args[1]
    if tp is bool:
        return "bool"
    if tp is int:
        return "int"
    if tp is str:
        return "str"
    return None


def _provider_names() -> List[str]:
    try:
        from backend import providers

        return sorted({p.name for p in providers.all_providers() if p.name})
    except Exception:  # noqa: BLE001
        return []


def _human(field: str) -> str:
    s = field.replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def _excluded(path: str, secrets: frozenset) -> bool:
    group, _, field = path.partition(".")
    return (
        group in _EXCLUDED_GROUPS
        or path in EXCLUDED
        or path in secrets
        or bool(_SECRET_NAME_RE.search(field))
    )


def _shape() -> List[dict]:
    """``[{path, label, type, options?}]`` — the settable fields, no values."""
    from backend.config import settings as store

    secrets = _secret_paths()
    labels = _labels()
    providers_list = None
    out: List[dict] = []
    groups = typing.get_type_hints(store.Settings)
    for gf in dataclasses.fields(store.Settings):
        group = gf.name
        cls = groups.get(group)
        if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
            continue
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            path = "%s.%s" % (group, f.name)
            if _excluded(path, secrets):
                continue
            kind = _kind_of(hints.get(f.name))
            if kind is None:
                continue
            label = labels.get(path) or EXTRA_LABELS.get(path) or _human(f.name)
            row = {"path": path, "label": label, "type": kind}
            opts = OPTIONS.get(path)
            if opts is None and path in (
                "coding_cli.default_provider",
                "coding_cli.assistant_provider",
                "github.agent",
                "github.issue_agent",
                "engine.agent",
            ):
                if providers_list is None:
                    providers_list = _provider_names()
                opts = [""] + providers_list if providers_list else None
            if opts is not None and kind == "str":
                row["type"] = "select"
                row["options"] = list(opts)
            out.append(row)
    return out


def settings(current: Any = None) -> List[dict]:
    """The settings catalog: ``[{path, label, type, value, options?}]``
    (``type``: bool | str | int | select). ``current``: a
    :class:`~backend.config.settings.Settings` (None = load it)."""
    if current is None:
        from backend.config.settings import load_settings

        current = load_settings()
    out = []
    for row in _shape():
        group, _, field = row["path"].partition(".")
        value = getattr(getattr(current, group, None), field, None)
        out.append(dict(row, value=value))
    return out


def check_setting(path: Any, value: Any, rows: Optional[List[dict]] = None):
    """``(group, field, value, None)`` when ``value`` may be written to
    ``path``, else ``(…, error)`` with a sentence the assistant says.
    Coerces the obvious spoken forms: "on"/"off" for a bool, "5" for an
    int, an option said in another case. ``rows``: :func:`settings` (or
    its shape) to check against; None = :func:`_shape`."""
    path = str(path or "").strip()
    group, _, field = path.partition(".")
    if not group or not field:
        return group, field, value, "That isn't a setting."
    if _excluded(path, _secret_paths()):
        return (
            group,
            field,
            value,
            "I can't change that one by voice; it's in Settings.",
        )
    row = next(
        (r for r in (rows if rows is not None else _shape()) if r["path"] == path), None
    )
    if row is None:
        return group, field, value, "There's no setting called %s." % path
    kind = row["type"]
    if kind == "bool":
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "on", "yes", "1", "enabled"):
                value = True
            elif v in ("false", "off", "no", "0", "disabled"):
                value = False
        if not isinstance(value, bool):
            return group, field, value, "%s takes on or off." % row["label"]
    elif kind == "int":
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            value = int(value.strip())
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return group, field, value, "%s takes a whole number." % row["label"]
    elif kind == "select":
        opts = row.get("options") or []
        said = str(value if value is not None else "").strip()
        hit = next((o for o in opts if o.lower() == said.lower()), None)
        if hit is None:
            shown = [o for o in opts if o]
            return (
                group,
                field,
                value,
                "%s is one of: %s." % (row["label"], ", ".join(shown)),
            )
        value = hit
    else:
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return group, field, value, "%s takes text." % row["label"]
        value = str(value).strip()
        if len(value) > 200 or any(ord(c) < 32 for c in value):
            return group, field, value, "That value is too long for %s." % row["label"]
    return group, field, value, None


def is_destructive_click(label: Any) -> bool:
    """Whether clicking a control named ``label`` asks first."""
    return bool(_DESTRUCTIVE_CLICK_RE.search(str(label or "")))


def open_targets() -> List[str]:
    """Every ``{"do": "open"}`` target: the openable dialogs, then
    ``settings:<screen>`` for each visible Settings screen."""
    return list(OPEN_DIALOGS) + [
        "settings:" + k for k in SETTINGS_SCREENS if k not in _HIDDEN_SCREENS
    ]


def catalog(current: Any = None) -> dict:
    """``GET /api/voice/assistant/catalog``'s body."""
    return {
        "settings": settings(current),
        "dialogs": list(OPEN_DIALOGS),
        "settings_screens": [k for k in SETTINGS_SCREENS if k not in _HIDDEN_SCREENS],
        "session_actions": list(SESSION_ACTIONS),
    }
