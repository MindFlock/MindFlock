"""File a ticket on a configured source from one sentence — New → Ticket.

Two things live here: which sources can be filed into
(:func:`creatable_sources`), and the one operation the feature has
(:func:`compose`) — draft the ticket, file it, hand back the link.

**Drafting and filing are one call on purpose.** There is no route anywhere
that takes ticket FIELDS, because MindFlock does not have a ticket form and is
not going to grow a worse copy of the one the tracker already has. The only way
a ticket gets filed from here is by describing it, which is also the only way
worth having: the tracker is one click away for anything else.

**The link is the deliverable.** Every adapter's ``create_ticket`` returns a
hydrated :class:`~backend.ticket_ingestion.models.Ticket` carrying a real
``app_url``, and this module refuses to report success without one — a create
that cannot say where the ticket went leaves the user hunting through a board
for something they are not sure got filed.

**A failed file still returns the draft.** The model turn is the slow part
(~10-25s), and losing it because a token expired would make the retry cost the
whole wait again. :class:`ComposeError` carries the draft when there is one, and
the route puts it in the error payload.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.web.core import ticket_draft as _ticket_draft
from backend.web.core import ticket_start as _ticket_start

_logger = logging.getLogger(__name__)


class ComposeError(RuntimeError):
    """Nothing was filed. ``draft`` carries the work already done, if any."""

    def __init__(self, message: str, draft: Optional[_ticket_draft.Draft] = None):
        super().__init__(message)
        self.draft = draft


def _sources():
    """The configured ticketing sources, through the pipeline's own config
    ladder. Deliberately ``ticket_start``'s loader rather than a second call to
    ``load_config``: that one re-anchors a relative ``workspace_dir`` at the
    repo root, and two resolutions of the same config in one process is how the
    Intake panel and this dialog would start disagreeing about which sources
    exist."""
    cfg = _ticket_start._load_config()
    return cfg, (cfg.ticketing_sources or [])


def creatable_sources() -> dict:
    """Every configured source, each with whether a ticket can be filed into it.

    Sources that CAN'T are listed too, with the reason, rather than omitted:
    "Jira isn't in this list" is indistinguishable from "MindFlock doesn't
    support Jira", while "this Jira source has no project set" names a field
    and a place to set it. The one thing never returned is a source that looks
    available and fails ~20 seconds later, after a draft.

    ``ingest_on`` says whether ticket ingestion is running for the flock at all,
    which is what decides whether filing a ticket also, eventually, starts a
    session for it. The dialog says so before the button is pressed — a ticket
    that quietly turns into a running agent is a surprise worth one sentence.
    """
    from backend.ticket_ingestion.providers import get_provider

    cfg, sources = _sources()
    rows: list[dict] = []
    for src in sources:
        key = src.id or src.provider
        label = (getattr(src, "label", "") or "").strip() or key
        try:
            provider = get_provider(src)
        except Exception as err:  # noqa: BLE001 — unknown provider / bad config
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "provider": src.provider,
                    "can_create": False,
                    "blocker": str(err),
                }
            )
            continue
        # The user's own label wins, the adapter's is the fallback — the same
        # order the Intake panel resolves in, and only reachable once the
        # adapter has constructed.
        label = (
            (getattr(src, "label", "") or "").strip()
            or getattr(provider, "label", "")
            or key
        )
        # create_blocker is contracted to be cheap and offline (see base.py); a
        # blocker that needed the network would put a request per source on a
        # dialog that has not been asked to do anything yet.
        try:
            blocker = provider.create_blocker()
        except Exception as err:  # noqa: BLE001 — a misconfigured source
            blocker = str(err)
        rows.append(
            {
                "key": key,
                "label": label,
                "provider": src.provider,
                "can_create": not blocker,
                "blocker": blocker,
            }
        )
    return {
        "sources": rows,
        "ingest_on": bool(getattr(cfg, "tickets_enabled", False)),
    }


def _find_source(source: str):
    """The configured source named ``source``, or a :class:`LookupError`."""
    _cfg, sources = _sources()
    for src in sources:
        if (src.id or src.provider) == source:
            return src
    raise LookupError(
        f"No ticketing source {source!r} is configured — check Intake → Tickets"
    )


def _row(story, src, draft: _ticket_draft.Draft) -> dict:
    """The created ticket, as the dialog renders it.

    ``source``/``id`` are the pair every other ticket route is keyed on
    (``POST /api/tickets/start`` takes exactly those two), so the dialog can
    hand the row straight to Intake without the user finding it again.
    """
    return {
        "source": src.id or src.provider,
        "source_label": (getattr(src, "label", "") or "").strip()
        or getattr(story, "source_label", "")
        or src.provider,
        "provider": src.provider,
        "id": str(getattr(story, "id", "")),
        "slug": getattr(story, "slug", ""),
        "name": getattr(story, "name", "") or draft.name,
        "url": getattr(story, "app_url", ""),
        "description": getattr(story, "description", "") or draft.description,
        "criteria": list(getattr(story, "acceptance_criteria", None) or draft.criteria),
    }


async def compose(source: str, text: str, *, program: str = "") -> dict:
    """Draft a ticket from ``text`` and file it on ``source``. Returns the row.

    The blocker is checked BEFORE the model runs, so a source that was never
    going to accept a ticket costs a round trip rather than a draft. Everything
    after that point can still fail — a token expires, a project is archived,
    Jira refuses the issue type — and when it does, the draft goes back with the
    error so the retry is one button and not another 25 seconds.
    """
    import asyncio

    from backend.ticket_ingestion.providers import get_provider

    src = _find_source(source)
    provider = get_provider(src)
    # One question, asked once: create_blocker's default already answers for an
    # adapter with can_create False, and adapters that override it answer for a
    # source that is merely unconfigured. Checking can_create separately here
    # would be a second gate that can disagree with the sentence shown in the
    # picker.
    blocker = provider.create_blocker()
    if blocker:
        raise ComposeError(blocker)

    def _draft() -> _ticket_draft.Draft:
        return _ticket_draft.draft(text, program=program)

    try:
        # Threaded: the one-shot blocks for the whole model turn, and this is an
        # async route on the same loop that serves the grid's websockets.
        drafted = await asyncio.to_thread(_draft)
    except _ticket_draft.TicketDraftError as err:
        raise ComposeError(str(err))

    try:
        story = await provider.create_ticket(drafted.name, drafted.description)
    except Exception as err:  # noqa: BLE001 — provider refusal / network
        raise ComposeError(str(err), draft=drafted)

    if not getattr(story, "app_url", ""):
        # Filed, but unfindable. Reported as a failure with the draft attached
        # because the user cannot act on a ticket they cannot open — and said
        # plainly rather than dressed up, since a retry here WILL file a second
        # copy and they need to know that before they press it.
        raise ComposeError(
            "The ticket was filed on %s but the tracker did not return a link to "
            "it — check the board before filing it again."
            % ((getattr(src, "label", "") or "").strip() or src.provider),
            draft=drafted,
        )
    row = _row(story, src, drafted)
    _logger.info(
        "Filed %s on %s: %s", row["slug"] or row["id"], row["source"], row["url"]
    )
    return row
