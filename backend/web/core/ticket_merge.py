"""Fold one ticket into another and delete the loser (Intake → Tickets).

Two people file the same task. It happens constantly, and until now MindFlock's
only answer was to start two sessions on it or to remember, forever, that sc-41
is "really" sc-38. The fix has to land in the TRACKER, because the tracker is
where both filers will go back to look — hiding a duplicate in this app would
leave the duplicate in Shortcut, still assigned, still getting ingested.

So: everything ticket A has — description, acceptance criteria, comments, its
attached files — is appended to ticket B, an audit comment is left on B saying
where it came from, and A is deleted for good.

THE ORDER IS THE WHOLE DESIGN. Each step is a separate API call against a
tracker that can refuse any one of them, so the sequence is arranged so that
every partial outcome is a *recoverable* one:

    1. append A's content to B's description   ← load-bearing; nothing else
                                                 runs if this fails, and A is
                                                 untouched
    2. carry A's attachments onto B            ← best effort, per file
    3. comment on B recording the merge        ← best effort
    4. delete A                                ← last, and reported separately

A failure at 1 leaves both tickets exactly as they were. A failure at 4 leaves
a duplicated ticket rather than an erased one — annoying, fixable by hand, and
survivable. The reverse order would have a class of failure that loses work
permanently, which is not a trade worth making for a tidier list. That is also
why :func:`merge_tickets` returns ``deleted`` and ``delete_error`` instead of
raising: "merged, but I could not delete it, here is why" is a true sentence
the UI can show, and a 500 after step 1 would leave the user believing nothing
had happened at all.

Same source only. Merging across trackers would mean deleting a Jira issue into
a Shortcut story, where the attachments cannot follow and the survivor's branch,
repo and agent belong to a different queue — a different feature wearing this
one's name.

The provider writes themselves live in the adapters
(``TicketProvider.append_description`` and friends, gated on ``can_merge``),
because provider-specific behaviour belongs to providers. This module owns the
sequence, the merged-in text, and nothing else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.web.core.ticket_start import _load_config

_logger = logging.getLogger(__name__)


def _resolve_source(source: str):
    """The configured ticketing source ``source`` and its adapter.

    Raises :class:`LookupError` when no such source is configured — the same
    signal ``ticket_start.find_ticket`` gives, so the route maps both to 404.
    """
    from backend.ticket_ingestion.providers import get_provider

    cfg = _load_config()
    for src in cfg.ticketing_sources or []:
        if (src.id or src.provider) == source:
            return src, get_provider(src)
    raise LookupError(
        f"No ticketing source {source!r} is configured — check Intake → Tickets"
    )


def merged_section(story, into_slug: str, *, when: datetime | None = None) -> str:
    """The block appended to the surviving ticket's description.

    Plain markdown, and only four shapes of it — a rule, ``#### `` headings,
    ``- `` bullets and paragraphs — because Jira's description is not markdown
    and :func:`backend.ticket_ingestion.providers.jira.text_to_adf` translates
    exactly those four. Anything richer would render on three providers and
    come out as literal asterisks on the fourth.

    The acceptance-criteria heading says "from <slug>" for a reason that is not
    decoration. ``parse_acceptance_criteria`` enters its criteria section on a
    line matching ``^#+ acceptance criteria$`` *exactly*, and once it finds one
    it stops falling back to mining the description's other bullets. A bare
    "#### Acceptance criteria" in the merged block would therefore quietly
    reach back and change how the SURVIVING ticket's own criteria are read the
    next time it is ingested — the merge rewriting the meaning of text it did
    not touch. Qualifying the heading keeps A's criteria visible, keeps them
    minable by the ordinary bullet fallback, and leaves B's mining alone.
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    lines = [
        "",
        "",
        "---",
        "",
        f"#### Merged from {story.slug} — {story.name or 'untitled'}",
        "",
        f"Folded into {into_slug} on {stamp} and deleted."
        + (f" It was {story.app_url}" if story.app_url else ""),
        "",
    ]
    if (story.description or "").strip():
        lines += [story.description.strip(), ""]
    if story.acceptance_criteria:
        lines += [f"#### Acceptance criteria from {story.slug}", ""]
        lines += [f"- {c}" for c in story.acceptance_criteria]
        lines += [""]
    if story.attachments:
        lines += [f"#### Attachments from {story.slug}", ""]
        lines += [
            f"- {a.name}" + (f" — {a.url}" if a.url else "") for a in story.attachments
        ]
        lines += [""]
    if story.comments:
        n = len(story.comments)
        lines += [
            f"#### Comments from {story.slug} ({n})",
            "",
        ]
        # One bullet per comment, newlines flattened: a multi-paragraph comment
        # pasted raw would break out of the list and read as description text
        # written by whoever merged, rather than as something somebody said on
        # the ticket that no longer exists.
        lines += [
            "- " + " ".join((c or "").split())
            for c in story.comments
            if (c or "").strip()
        ]
        lines += [""]
    return "\n".join(lines)


def audit_comment(story, target, *, when: datetime | None = None) -> str:
    """The permanent record left on the surviving ticket.

    The description carries the CONTENT; this carries the FACT, and it is the
    only place the deleted ticket's id and url survive as a line somebody can
    search for. Deliberately short — it is read months later by someone asking
    "where did this paragraph come from?".
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    origin = f" ({story.app_url})" if story.app_url else ""
    return (
        f"Merged {story.slug} — {story.name or 'untitled'}{origin} into this "
        f"ticket and deleted it, from MindFlock on {stamp}. Its description, "
        "acceptance criteria, comments and files are in the section appended "
        f"to {target.slug}'s description above."
    )


async def merge_tickets(source: str, from_id: str, into_id: str) -> dict:
    """Merge ``from_id`` into ``into_id`` on one source, then delete ``from_id``.

    See the module docstring for why the steps run in this order and why a
    failed delete comes back in the payload instead of as an exception.

    Raises :class:`LookupError` (no such source / ticket) and
    :class:`ValueError` (same ticket twice) for the route to turn into 404 and
    400; a provider refusing the *first* write propagates, because at that
    point nothing has happened and there is nothing to report but the reason.
    """
    from_id = str(from_id or "").strip()
    into_id = str(into_id or "").strip()
    if not from_id or not into_id:
        raise ValueError("source, from and into are required")
    if from_id == into_id:
        raise ValueError("a ticket cannot be merged into itself")

    src_cfg, provider = _resolve_source(source)
    if not getattr(provider, "can_merge", False):
        raise ValueError(
            f"{provider.label or provider.name} tickets cannot be merged from "
            "MindFlock — this provider's adapter is read-only"
        )

    # Both sides are fetched BEFORE anything is written: a typo'd target id
    # should fail here, with both tickets intact, rather than after A's content
    # has been posted somewhere it does not belong.
    story = await provider.fetch(from_id)
    target = await provider.fetch(into_id)

    when = datetime.now(timezone.utc)
    # 1. Load-bearing. Unhandled on purpose — see the module docstring.
    await provider.append_description(
        into_id, merged_section(story, target.slug, when=when)
    )

    # 2. Best effort, per file. A provider whose uploads outlive their ticket
    #    reports nothing moved, which is correct rather than a failure.
    moved: list[str] = []
    failed: list[str] = []
    try:
        moved, failed = await provider.carry_attachments(from_id, into_id)
    except Exception as err:  # noqa: BLE001
        _logger.warning(
            "Carrying attachments from %s to %s failed (continuing): %s",
            story.slug,
            target.slug,
            err,
        )
        failed = [a.name for a in story.attachments]

    # 3. Best effort. Losing the audit line is not worth abandoning a merge
    #    whose content has already landed.
    comment_error = ""
    try:
        await provider.add_comment(into_id, audit_comment(story, target, when=when))
    except Exception as err:  # noqa: BLE001
        comment_error = str(err)
        _logger.warning(
            "Audit comment on %s after merging %s failed: %s",
            target.slug,
            story.slug,
            err,
        )

    # 4. Last. Reported, never raised.
    deleted = False
    delete_error = ""
    try:
        await provider.delete_ticket(from_id)
        deleted = True
    except Exception as err:  # noqa: BLE001
        delete_error = str(err)
        _logger.warning("Deleting %s after merging it failed: %s", story.slug, err)

    return {
        "source": source,
        "source_label": (getattr(src_cfg, "label", "") or "").strip()
        or provider.label
        or source,
        "from": {
            "id": str(story.id),
            "slug": story.slug,
            "name": story.name,
            "url": story.app_url,
        },
        "into": {
            "id": str(target.id),
            "slug": target.slug,
            "name": target.name,
            "url": target.app_url,
        },
        "comments_copied": len(story.comments),
        "attachments_moved": moved,
        "attachments_failed": failed,
        "attachments_linked": (
            # Nothing moved and nothing failed, yet the ticket had files: this
            # provider's uploads live at workspace scope and travel as the links
            # already copied into the description. Named so the UI can say
            # "3 files still reachable" instead of an ominous silent zero.
            [a.name for a in story.attachments]
            if story.attachments and not moved and not failed
            else []
        ),
        "comment_error": comment_error,
        "deleted": deleted,
        "delete_error": delete_error,
    }
