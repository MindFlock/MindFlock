"""The per-session "Flock" mode: every prompt asks for MindFlock workers.

The person very often tells a session to split its task across MindFlock
worker sessions; this saves typing the request each time. Two shapes:

* STICKY — ``<flag dir>/<tmux session>.flock``, set by ``POST
  /api/instances/{title}/flock {on}``. The file's CONTENT is what every
  prompt carries (:func:`every_prompt_text`): by default a request to PLACE
  the prompt — do it here, split it across workers, or start it as a separate
  session on its own branch — built around the workers request
  (:func:`request_text`, the ``general.workers_request`` template with
  ``{model}`` filled in from ``general.workers_model``); with
  ``general.workers_decide`` off, the workers request alone. A CLI whose
  prompt-submit hook injects context (Claude, ``voice_prompt_hook``) prints
  it on EVERY submitted prompt — prompts typed straight into the terminal
  included (:func:`backend.providers.activity_markers.hook_command`). Any
  other CLI gets it typed after each ``/send`` text instead.
* ONE-SHOT — ``/send``'s ``flock: true``: the workers request itself (the
  person asked for workers, so nothing is left to decide) typed after that
  one prompt (unless the sticky flag already asks a hook CLI for it).

A file, like voice's flags (:mod:`backend.web.core.voice`, whose
:func:`flag_dir` it shares): the hook reads it at fire time from inside the
agent's process, with no server round trip. Holding the text rather than a
bit keeps the hook free of settings parsing; a settings change re-writes
every existing ``.flock`` file (:func:`rewrite_all`) so the next prompt uses
the new words.

Keyed by the TMUX SESSION NAME the hook resolves, sanitised like the voice
flags. Readers never raise: a flag that cannot be read is a flag that is off.
"""

from __future__ import annotations

from pathlib import Path

from backend.providers import activity_markers as _am
from backend.web.core.voice import flag_dir

#: The flag file's suffix (``<session>.flock``) — the hook's own constant.
SUFFIX = _am.FLOCK_FLAG_SUFFIX


def flag_path(session_name: str) -> Path:
    """``<flag dir>/<session>.flock`` — the sticky workers request."""
    return flag_dir() / (_am.safe_session_name(session_name) + SUFFIX)


def is_on(session_name: str) -> bool:
    """Whether Flock mode is on for the tmux session ``session_name``."""
    if not session_name:
        return False
    try:
        return flag_path(session_name).is_file()
    except Exception:  # noqa: BLE001 — unreadable (or guarded) reads as off
        return False


def request_text() -> str:
    """The workers request as it is typed or injected: the
    ``general.workers_request`` template with ``{model}`` replaced by
    ``general.workers_model`` (a blank setting is its default). Never raises:
    an unreadable settings file reads as the defaults."""
    from backend.config import settings as _settings

    template = _settings.DEFAULT_WORKERS_REQUEST
    model = _settings.DEFAULT_WORKERS_MODEL
    try:
        gen = _settings.load_settings().general
        template = gen.workers_request or template
        model = gen.workers_model or model
    except Exception:  # noqa: BLE001 — the defaults still ask for workers
        pass
    if model == _settings.DEFAULT_WORKERS_MODEL and not _claude_default():
        # The default model is a Claude one: on a machine whose default agent
        # is another CLI (GitHub Copilot, Codex…) the workers it spawns run
        # that CLI, which may not know the name. Leave the model to it.
        template = template.replace(" (model {model})", "")
    # A plain replace, not str.format: a brace the person typed is just text.
    return template.replace("{model}", model).strip()


def _claude_default() -> bool:
    """Whether the default agent is Claude Code (unknowable reads as yes:
    the request then reads as it always did)."""
    try:
        from backend.config.program import default_provider_name

        return default_provider_name() == "claude"
    except Exception:  # noqa: BLE001
        return True


def decides() -> bool:
    """Whether the sticky mode lets the agent place each prompt
    (``general.workers_decide``; unset or unreadable reads as on)."""
    from backend.config import settings as _settings

    try:
        return _settings.load_settings().general.workers_decide is not False
    except Exception:  # noqa: BLE001
        return True


def every_prompt_text() -> str:
    """What the sticky mode adds to every prompt: the request to place it
    (:data:`~backend.config.settings.DEFAULT_WORKERS_DECIDE` around
    :func:`request_text`), or — ``general.workers_decide`` off — the workers
    request alone. One line. Never raises."""
    from backend.config import settings as _settings

    req = request_text()
    if not decides():
        return req
    # A plain replace, like the model's: a brace in the person's words is text.
    text = _settings.DEFAULT_WORKERS_DECIDE.replace("{workers}", req)
    return " ".join(text.split())


def set_on(session_name: str, on: bool, text: str = "") -> bool:
    """Turn Flock mode on (writing ``text``, else :func:`every_prompt_text`) or
    off for ``session_name``; returns the new state, read back.

    Idempotent. An ``OSError`` propagates so the route can say why."""
    path = flag_path(session_name)
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((text or every_prompt_text()).strip() + "\n", encoding="utf-8")
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return is_on(session_name)


def rewrite_all() -> int:
    """Re-write every existing ``.flock`` file with the current
    :func:`every_prompt_text` — after the settings changed, and once at server
    start (an upgrade may have changed the default words) — so each session's
    next prompt carries the new words. Returns how many were re-written.
    Never raises (one unwritable file never blocks the rest)."""
    try:
        files = list(flag_dir().glob("*" + SUFFIX))
    except Exception:  # noqa: BLE001 — no flag dir: nothing is on
        return 0
    text = every_prompt_text() + "\n"
    done = 0
    for path in files:
        try:
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
            done += 1
        except Exception:  # noqa: BLE001
            continue
    return done
