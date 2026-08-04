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

from backend.config.config import DEFAULT_PROGRAM, LoadConfig
from backend.config.settings import load_settings


def resolve_default_program() -> str:
    """The agent CLI to launch when the caller has no more specific choice.

    Precedence: ``coding_cli.default_provider`` (Settings → Coding provider) →
    ``config.json``'s ``default_program`` → :data:`DEFAULT_PROGRAM`. Each layer
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
            return program
    except Exception:  # noqa: BLE001 — config must never break a launch
        pass

    return DEFAULT_PROGRAM
