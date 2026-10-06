"""Move a ticket into its source's "started" state when a session launches.

A board is a claim about what is being worked on, and a ticket that sits in
"Ready for dev" while an agent is three commits into it is a lie the whole team
reads. The source's optional ``start_state`` says where a ticket goes the moment
a session starts for it; this module is the single place that move happens, for
BOTH launch paths:

* the pipeline (``SessionRunner.run``, a separate OS process), and
* the web force-start (Intake → Tickets → Run ticket, inside the server).

Deliberately best-effort. :func:`move_started` never raises: the session is the
work and the board is bookkeeping about it, so a tracker that is down, a Jira
workflow with no transition into the configured status, or a state id left over
from a provider switch costs a warning in the log — never a launch.

The source is resolved from the config **on disk right now**, matching
``source_agent_now`` / ``source_effort_now``: a state picked in the UI applies to
the next ticket rather than the next pipeline restart.

A source can also ask for the move to be announced (``start_comment``): a
"MindFlock (Ethan) is taking this on." comment on the ticket, because the
people watching a ticket read its comments, not its column history. It rides
on the move — posted only after the move succeeds, and only when the move
actually CHANGED the ticket's state, so relaunching a ticket that is already
in development does not stack a second "taking this on" under the first. Same
best-effort contract: a refused comment is a warning, and it never undoes or
un-reports the move.
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

#: Display name per tracker account, so announcing a move costs one identity
#: request per account per process rather than one per ticket. Keyed by what
#: identifies the account (never logged).
_NAMES: dict[tuple[str, str, str], str] = {}


def source_key_of(story) -> str:
    """The ticketing-source key a ticket came from.

    ``Ticket.source_key`` is stamped by whoever produced the ticket (the
    backfill scanner, or the web layer's ``find_ticket``). The provider name is
    the fallback for a ticket built before that field existed or by hand in a
    test — it is what ``PipelineConfig.source_for`` matches on for an unkeyed
    source anyway.
    """
    return str(getattr(story, "source_key", "") or "") or str(
        getattr(story, "provider", "") or ""
    )


def target_for(story, config=None) -> tuple[object | None, str]:
    """``(source config, state id)`` for ``story``'s start-state move.

    ``(None, "")`` when the ticket's source is gone, has no ``start_state``, or
    sits on a provider that cannot move a ticket. ``config`` is a fallback
    snapshot used only when the on-disk config can't be read — the same
    "prefer disk, fall back to what I was built with" shape as
    :func:`~backend.ticket_ingestion.config.fresh_agent`.
    """
    from backend.ticket_ingestion.config import config_for_launch
    from backend.ticket_ingestion.providers.base import start_state_id

    key = source_key_of(story)
    if not key:
        return None, ""
    try:
        on_disk = config_for_launch(None)
    except Exception:  # noqa: BLE001 — an unreadable config is not a launch error
        on_disk = None
    for candidate in (on_disk, config):
        if candidate is None:
            continue
        try:
            src = candidate.source_for(key)
        except Exception:  # noqa: BLE001 — a config we can't read is not a launch error
            continue
        if src is None:
            continue
        return src, start_state_id(src)
    return None, ""


async def move_started(story, config=None) -> str:
    """Move ``story`` into its source's configured start state.

    Returns the state id moved to, or ``""`` when there was nothing to do (no
    ``start_state`` configured, unknown source, unsupported provider) or the
    move failed. Never raises — see the module docstring.
    """
    from backend.ticket_ingestion.providers import get_provider

    try:
        src, target = target_for(story, config)
        if src is None or not target:
            return ""
        provider = get_provider(src)
        await provider.set_state(str(story.id), target)
    except Exception as err:  # noqa: BLE001 — bookkeeping, never the launch
        _logger.warning(
            "Could not move ticket %s into its source's start state: %s",
            getattr(story, "slug", "") or story.id,
            err,
        )
        return ""
    _logger.info(
        "Moved ticket %s into its source's start state (%s) on launch.",
        getattr(story, "slug", "") or story.id,
        target,
    )
    await _announce(story, src, target, provider)
    return target


def _already_in(story, target: str) -> bool:
    """Whether the ticket snapshot says it was sitting in ``target`` already.

    Matches the native state id or, for a hand-edited Jira config that names
    the status, the state name — the same two keys ``ingest_filter_miss`` and
    Jira's ``set_state`` accept. A ticket that reports no state is treated as
    moved: announcing once too often beats never announcing.
    """
    want = target.strip().casefold()
    return want in {
        str(getattr(story, "state_id", "") or "").strip().casefold(),
        str(getattr(story, "state", "") or "").strip().casefold(),
    } - {""}


async def _display_name(provider, src) -> str:
    """The connected account's name, as the tracker reports it. ``""`` when it
    won't say — the comment then reads "MindFlock is taking this on." """
    key = (
        str(getattr(src, "provider", "") or ""),
        str(getattr(src, "base_url", "") or ""),
        str(getattr(src, "api_token", "") or ""),
    )
    if key in _NAMES:
        return _NAMES[key]
    identity, _err = await provider.test_connection()
    identity = identity or {}
    name = str(identity.get("display_name") or identity.get("name") or "").strip()
    if identity:
        # Only a successful lookup is remembered; a flaky one is retried on the
        # next ticket instead of pinning a nameless comment for the process.
        _NAMES[key] = name
    return name


async def _announce(story, src, target: str, provider) -> None:
    """Post the source's "taking this on" comment, if it asked for one.

    Never raises — the move it follows has already happened and been reported.
    """
    from backend.ticket_ingestion.providers.base import (
        comments_on_start,
        start_comment_text,
    )

    if not comments_on_start(src) or _already_in(story, target):
        return
    try:
        name = await _display_name(provider, src)
        await provider.add_comment(str(story.id), start_comment_text(name))
    except Exception as err:  # noqa: BLE001 — bookkeeping, never the launch
        _logger.warning(
            "Moved ticket %s but could not post its start comment: %s",
            getattr(story, "slug", "") or story.id,
            err,
        )
        return
    _logger.info(
        "Commented on ticket %s that MindFlock is taking it on.",
        getattr(story, "slug", "") or story.id,
    )
