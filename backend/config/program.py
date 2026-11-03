"""Which agent CLI a launch should use when nothing more specific asked for one.

This exists because the answer lives in **two** stores that used to disagree:

* ``~/.mindflock/config.json``'s ``default_program`` — the engine config, seeded
  once on first run by :func:`GetClaudeCommand`, which only ever hunts for
  ``claude``.
* ``~/.mindflock/settings.json``'s ``coding_cli.default_provider`` — what
  Settings → Coding provider actually writes when you pick a default.

Only the first was ever read at launch time, so picking a default in the UI
changed the Providers screen badge and ``mindflock doctor`` and nothing else:
every session — hand-started, ingested, PR-review, issue — still launched
``claude``. :func:`resolve_default_program` is the single choke point that reads
the user's *chosen* default first and falls back to the engine config, so the
setting means what it says.

Kept in its own module rather than in ``config.py`` because ``settings.py``
imports ``config.py``; putting it there would close an import cycle.
"""

from __future__ import annotations

import os
import shutil

from backend.config.config import DEFAULT_PROGRAM, LoadConfig
from backend.config.settings import load_settings


def _cli_installed(name: str) -> bool:
    """Whether registered provider ``name``'s CLI resolves on this machine
    (an explicit binary-path override, else ``PATH``). Never raises."""
    try:
        from backend import providers
        from backend.providers import config as provider_config

        p = providers.get(name)
        if p is None:
            return False
        binary = provider_config.resolve_provider_binary(name, getattr(p, "cfg", None))
        if not binary:
            return False
        if os.sep in binary:
            return os.path.isfile(binary) and os.access(binary, os.X_OK)
        return bool(shutil.which(binary))
    except Exception:  # noqa: BLE001 — a probe failure is "not installed"
        return False


def installed_fallback(default: str) -> str:
    """The agent to use when nobody CHOSE one and ``default``'s CLI is not on
    this machine: the first registered agent whose CLI is installed (a GitHub
    Copilot-only computer runs Copilot, not a ``claude`` that isn't there).
    ``default`` itself while it is installed, or while no agent is installed
    at all (first-run setup then offers to install it). Never raises."""
    if not default or _cli_installed(default):
        return default
    try:
        from backend import providers

        for p in providers.all_providers():
            name = getattr(p, "name", "")
            if name and name not in ("generic", default) and _cli_installed(name):
                return name
    except Exception:  # noqa: BLE001 — the honest fallback is the default
        pass
    return default


def default_provider_name() -> str:
    """The default agent PROVIDER name every surface shows (Setup, doctor,
    Settings → Agents, connections): ``coding_cli.default_provider`` when one
    was chosen, else the registry default — or, when its CLI is missing, the
    first agent that is installed (:func:`installed_fallback`)."""
    try:
        # Looked up at call time (not this module's import-time binding): the
        # doctor and Settings read through here, and what they patch is the
        # settings module.
        from backend.config import settings as _settings

        chosen = (_settings.load_settings().coding_cli.default_provider or "").strip()
        if chosen:
            return chosen
    except Exception:  # noqa: BLE001 — settings are optional
        pass
    try:
        from backend import providers

        default = providers.DEFAULT_PROVIDER
    except Exception:  # noqa: BLE001
        default = "claude"
    return installed_fallback(default)


def resolve_default_program() -> str:
    """The agent CLI to launch when the caller has no more specific choice.

    Precedence: ``coding_cli.default_provider`` (Settings → Coding provider) →
    ``config.json``'s ``default_program`` → :data:`DEFAULT_PROGRAM`; the
    seeded ``claude`` of the last two yields to an installed agent when its
    CLI is missing (:func:`installed_fallback`). Each layer
    is independently best-effort: a missing or unreadable store falls through
    rather than raising, because every caller is on a launch path where the
    honest fallback is better than an exception.
    """
    try:
        chosen = (load_settings().coding_cli.default_provider or "").strip()
        if chosen:
            return chosen
    except Exception:  # noqa: BLE001 — settings are optional
        pass

    try:
        program = (LoadConfig().GetProgram() or "").strip()
        if program:
            # config.json is seeded with ``claude`` on every first run, so on a
            # machine without it the SEED must not win over an agent that IS
            # installed. Anything else in it was written on purpose and stays.
            if program == DEFAULT_PROGRAM:
                return installed_fallback(program)
            return program
    except Exception:  # noqa: BLE001 — config must never break a launch
        pass

    return installed_fallback(DEFAULT_PROGRAM)
