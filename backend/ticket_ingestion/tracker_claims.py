"""Claims a teammate can see: the ticket itself says who is on it.

Fleet claims (:mod:`backend.web.core.fleet_claims`) stop one PERSON's devices
from starting the same ticket twice, over device pairing. A teammate's
MindFlock was never paired with yours, so the only place both of you look is
the tracker. This module puts the claim there and reads it back:

* **Mirror** (:func:`mirror_started`). When MindFlock starts work on a ticket
  it assigns the ticket to the person's tracker identity — the source's
  ``member_id`` (Shortcut member, Jira accountId, Linear user, Asana user
  gid) or the GitHub login — and adds the source's claim label
  (:func:`claim_label`: ``<claim_label>:working``, ``claim_label`` defaulting
  to ``"mindflock"``). A source that ingests ANYONE's tickets (a QA queue,
  ``assignee_scope = "anyone"``) is never reassigned — those tickets belong
  to whoever wrote the code — so there only the label is written. What was
  mirrored is remembered per session (``tracker_claims.json`` in the config
  dir) so the label comes off when that session closes or ships
  (:func:`release`); the assignment is NEVER undone.
* **Check** (:func:`tracker_holder`). Before a ticket starts, the ticket as the
  tracker has it right now counts as claimed when it is assigned to someone
  else — owners present, the person's own identity not among them — AND it
  carries the claim label, or sits in a "started"-type workflow state. On an
  any-assignee source only the label counts: every ticket there is someone
  else's and many sit in a started-type state on purpose.

Best-effort both ways: a tracker that is down, a token without write scope, or
a source whose "me" can't be resolved costs a log line — never a launch, and
never a refusal (an unknown identity can't tell a teammate's ticket from
yours, so it claims nothing). The check is not a lock: two unpaired people
pressing Begin work in the same second can both pass it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Dict, Optional

_logger = logging.getLogger(__name__)

#: ``claim_label`` when a source doesn't set one.
DEFAULT_CLAIM_LABEL = "mindflock"
#: What the written label says about the claim.
WORKING_SUFFIX = ":working"

#: Seconds a source's started-state ids / tracker identity are reused.
STATES_TTL = 300.0
ME_TTL = 3600.0

_states: Dict[str, tuple] = {}  # source key -> (fetched_at, frozenset)
_me: Dict[str, tuple] = {}  # source key -> (fetched_at, identity)
_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Pure rules
# --------------------------------------------------------------------------- #
def claim_label(src) -> str:
    """The label a claim writes for this source: ``<claim_label>:working``
    (``mindflock:working`` by default). Whitespace becomes ``-`` (Jira labels
    can't hold a space); a value that already ends in ``:working`` is used
    as is."""
    base = (getattr(src, "claim_label", "") or "").strip() or DEFAULT_CLAIM_LABEL
    base = re.sub(r"\s+", "-", base)
    return base if base.endswith(WORKING_SUFFIX) else base + WORKING_SUFFIX


def source_key(src) -> str:
    return str(getattr(src, "id", "") or getattr(src, "provider", "") or "")


def _any_assignee(src) -> bool:
    from backend.ticket_ingestion.providers.base import ingests_any_assignee

    try:
        return bool(ingests_any_assignee(src))
    except Exception:  # noqa: BLE001
        return False


def has_label(story, label: str) -> bool:
    low = (label or "").casefold()
    return bool(low) and any(
        str(n or "").casefold() == low for n in getattr(story, "labels", None) or []
    )


def claimed_by_other(story, src, *, me: str, started_ids=()) -> Optional[dict]:
    """The claim ``story`` shows for someone other than ``me``, or None.

    ``{"kind": "tracker", "name", "label", "why": "label"|"started",
    "source", "device": "", "since": 0.0, "person": ""}`` — shaped like a
    fleet claim so :func:`backend.web.core.fleet_claims.describe` and the
    Intake row read both kinds alike (``label`` is the display name)."""
    me = str(me or "").strip()
    owners = [str(o) for o in getattr(story, "owner_ids", None) or [] if str(o)]
    if not me or me in owners:
        return None
    any_assignee = _any_assignee(src)
    labelled = has_label(story, claim_label(src))
    # On an any-assignee (QA queue) source the label alone is the claim: an
    # UNASSIGNED ticket a teammate's MindFlock labelled is theirs. Elsewhere
    # a ticket nobody owns is nobody's claim.
    if not owners and not (any_assignee and labelled):
        return None
    why = ""
    if labelled:
        why = "label"
    elif not any_assignee:
        sid = str(getattr(story, "state_id", "") or "")
        if sid and sid in set(started_ids or ()):
            why = "started"
    if not why:
        return None
    names = [str(n) for n in getattr(story, "owner_names", None) or [] if str(n)]
    # On an any-assignee source the assignee wrote the code; whoever put the
    # label on is a teammate we can't name.
    name = "A teammate" if any_assignee else (", ".join(names) or "Someone else")
    return {
        "kind": "tracker",
        "name": name,
        "label": name,
        "why": why,
        "source": source_key(src),
        "device": "",
        "since": 0.0,
        "person": "",
    }


def describe(claim: dict) -> str:
    """``"<name> is on this"`` — the Intake chip and the 409 wording."""
    return "%s is on this" % (claim.get("name") or claim.get("label") or "Someone")


# --------------------------------------------------------------------------- #
# Tracker reads (cached)
# --------------------------------------------------------------------------- #
async def started_ids(provider, key: str) -> frozenset:
    """The source's ``started``-type workflow state ids (from
    ``list_states()``); empty for a provider without states. Cached
    :data:`STATES_TTL` s; a failed read is empty and not cached."""
    with _LOCK:
        hit = _states.get(key)
    if hit and time.monotonic() - hit[0] < STATES_TTL:
        return hit[1]
    try:
        states = await provider.list_states()
    except Exception as err:  # noqa: BLE001
        _logger.info("Claim check: could not list states of %s: %s", key, err)
        return frozenset()
    ids = frozenset(
        str(s.get("id"))
        for s in states or []
        if isinstance(s, dict) and str(s.get("type") or "") == "started"
    )
    with _LOCK:
        _states[key] = (time.monotonic(), ids)
    return ids


async def me_for(provider, key: str) -> str:
    """The person's identity on the source (:meth:`claim_identity`), cached
    :data:`ME_TTL` s once known."""
    with _LOCK:
        hit = _me.get(key)
    if hit and time.monotonic() - hit[0] < ME_TTL:
        return hit[1]
    try:
        raw = await provider.claim_identity()
        me = raw.strip() if isinstance(raw, str) else ""
    except Exception as err:  # noqa: BLE001
        _logger.info("Claim check: could not resolve who you are on %s: %s", key, err)
        return ""
    if me:
        with _LOCK:
            _me[key] = (time.monotonic(), me)
    return me


def clear_cache() -> None:
    with _LOCK:
        _states.clear()
        _me.clear()


def _source_of(story, config=None):
    """The configured source ``story`` came from (on-disk config first)."""
    from backend.ticket_ingestion.start_state import source_key_of

    key = source_key_of(story)
    if not key:
        return None
    try:
        from backend.ticket_ingestion.config import config_for_launch

        on_disk = config_for_launch(None)
    except Exception:  # noqa: BLE001
        on_disk = None
    for candidate in (on_disk, config):
        if candidate is None:
            continue
        try:
            src = candidate.source_for(key)
        except Exception:  # noqa: BLE001
            continue
        if src is not None:
            return src
    return None


async def tracker_holder(
    story, config=None, *, src=None, provider=None
) -> Optional[dict]:
    """:func:`claimed_by_other` for ``story`` against the tracker's answers
    (who "me" is, which states are started). ``story`` should be the ticket
    as the tracker has it NOW (a fresh fetch). Fails open: None on any error.
    A ticket this device mirrored itself is never someone else's."""
    try:
        src = src or _source_of(story, config)
        if src is None:
            return None
        if provider is None:
            from backend.ticket_ingestion.providers import get_provider

            provider = get_provider(src)
        key = source_key(src)
        me = await me_for(provider, key)
        if not me:
            return None
        ids = frozenset()
        if not _any_assignee(src) and getattr(story, "state_id", ""):
            ids = await started_ids(provider, key)
        claim = claimed_by_other(story, src, me=me, started_ids=ids)
        if claim and claim["why"] == "label" and _mirrored_here(key, story):
            return None  # our own label (a session here, or one that crashed)
        return claim
    except Exception as err:  # noqa: BLE001 — the check fails open
        _logger.warning(
            "Claim check for %s failed (starting anyway): %s",
            getattr(story, "slug", "") or getattr(story, "id", ""),
            err,
        )
        return None


# --------------------------------------------------------------------------- #
# The mirror record (what to take off when a session ends)
# --------------------------------------------------------------------------- #
def store_path() -> str:
    from backend.config.home_guard import guard

    env = (os.environ.get("MINDFLOCK_TRACKER_CLAIMS_FILE") or "").strip()
    if env:
        return guard(env, "tracker-claims store")
    from backend.config.config import GetConfigDir

    return guard(
        os.path.join(GetConfigDir(), "tracker_claims.json"), "tracker-claims store"
    )


def _load() -> Dict[str, dict]:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 — missing/corrupt = nothing mirrored
        return {}
    claims = data.get("claims") if isinstance(data, dict) else None
    return {
        str(k): v
        for k, v in (claims or {}).items()
        if isinstance(v, dict) and v.get("id") and v.get("source")
    }


def _save(claims: Dict[str, dict]) -> None:
    from backend.fsutil import atomic_write_json

    path = store_path()
    try:
        atomic_write_json(
            path,
            {"v": 1, "claims": claims},
            sort_keys=True,
            newline=False,
            ensure_ascii=True,
            prefix="tmp",
        )
    except OSError:
        pass


def mirrored() -> Dict[str, dict]:
    """``{session title: {"source", "id", "label", "assigned", "at"}}``."""
    with _LOCK:
        return _load()


def _mirrored_here(key: str, story) -> bool:
    tid = str(getattr(story, "id", "") or "")
    return any(
        r.get("source") == key and str(r.get("id")) == tid for r in mirrored().values()
    )


def _record(title: str, entry: dict) -> None:
    with _LOCK:
        claims = _load()
        claims[title] = entry
        _save(claims)


async def mirror_started(story, config=None, *, title: str = "") -> dict:
    """Say on the ticket that MindFlock is working on it — see the module
    docstring. ``{"assigned": bool, "labelled": bool, "label": str}``; never
    raises. ``title`` is the session the claim belongs to (default: the
    ticket's slug, which is a ticket session's title)."""
    out = {"assigned": False, "labelled": False, "label": ""}
    try:
        src = _source_of(story, config)
        if src is None:
            return out
        from backend.ticket_ingestion.providers import get_provider

        provider = get_provider(src)
        if not getattr(provider, "can_claim", False):
            return out
        key = source_key(src)
        tid = str(story.id)
        label = claim_label(src)
        out["label"] = label
        if not _any_assignee(src):
            me = await me_for(provider, key)
            owners = [str(o) for o in getattr(story, "owner_ids", None) or []]
            # Never take a ticket away from a teammate: only an unassigned
            # ticket (or one already ours) is assigned. One someone else holds
            # was refused before the launch unless the owner overrode — and an
            # override is not a reassignment.
            if me and not owners:
                try:
                    await provider.assign_to(tid, me)
                    out["assigned"] = True
                except Exception as err:  # noqa: BLE001
                    _logger.warning("Could not assign ticket %s: %s", story.slug, err)
        if has_label(story, label):
            # Already labelled (a teammate's claim, an override, a human's own
            # tag): not ours to add — and never ours to take off later.
            _logger.info(
                "Ticket %s already carries %r; not recording it.", story.slug, label
            )
            return out
        try:
            await provider.add_label(tid, label)
            out["labelled"] = True
        except Exception as err:  # noqa: BLE001
            _logger.warning("Could not label ticket %s %r: %s", story.slug, label, err)
        if out["labelled"]:
            _record(
                title or story.slug,
                {
                    "source": key,
                    "id": tid,
                    "label": label,
                    "assigned": out["assigned"],
                    "at": int(time.time()),
                },
            )
            _logger.info("Marked ticket %s as %s on its tracker.", story.slug, label)
    except Exception as err:  # noqa: BLE001 — bookkeeping, never the launch
        _logger.warning(
            "Could not mirror the claim on ticket %s: %s",
            getattr(story, "slug", "") or getattr(story, "id", ""),
            err,
        )
    return out


async def release(title: str, config=None) -> bool:
    """Take the claim label off the ticket ``title``'s session mirrored (the
    assignment stays). True when a label was removed. Another live record for
    the same ticket (a retry session) keeps it on. The record is forgotten
    only once the tracker write succeeded: a transient failure keeps it
    (flagged ``release_failed``) so the next release attempt retries instead
    of leaving the label on the ticket forever. Never raises."""
    with _LOCK:
        claims = _load()
        rec = claims.get(title)
        if rec is None:
            return False
        shared = any(
            t != title
            and r.get("source") == rec.get("source")
            and str(r.get("id")) == str(rec["id"])
            for t, r in claims.items()
        )
        if shared:
            claims.pop(title, None)
            _save(claims)
            return False
    try:
        from backend.ticket_ingestion.config import config_for_launch
        from backend.ticket_ingestion.providers import get_provider

        cfg = None
        try:
            cfg = config_for_launch(None)
        except Exception:  # noqa: BLE001
            cfg = None
        cfg = cfg or config
        src = cfg.source_for(rec["source"]) if cfg is not None else None
        if src is None:
            return False
        await get_provider(src).remove_label(
            str(rec["id"]), rec.get("label") or claim_label(src)
        )
        _logger.info("Took %s off ticket %s.", rec.get("label"), rec["id"])
        with _LOCK:
            claims = _load()
            claims.pop(title, None)
            _save(claims)
        return True
    except Exception as err:  # noqa: BLE001
        _logger.warning(
            "Could not take the claim label off ticket %s: %s", rec.get("id"), err
        )
        with _LOCK:
            claims = _load()
            if title in claims:
                claims[title]["release_failed"] = int(time.time())
                _save(claims)
        return False


def release_soon(title: str) -> bool:
    """:func:`release` on a daemon thread — for synchronous callers (the
    session-removed hook). Returns at once; False when ``title`` mirrored
    nothing (no thread is started)."""
    try:
        if title not in mirrored():
            return False
    except Exception:  # noqa: BLE001
        return False

    def _run() -> None:
        import asyncio

        try:
            asyncio.run(release(title))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, name="tracker-claim-release", daemon=True).start()
    return True
