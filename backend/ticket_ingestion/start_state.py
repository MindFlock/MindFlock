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
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)


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
        await get_provider(src).set_state(str(story.id), target)
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
    return target
