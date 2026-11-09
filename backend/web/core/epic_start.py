"""Epics (docs/epics.md): a whole Shortcut epic ingested as ONE lead session.

The person's ask: "one agent per epic where they are all really working
together" — and, for the PR reviewer, "the work split into their own
tickets". So an epic is never one big branch. It is a LEAD session that:

* reads the epic and the stories in it that are the person's own and not done
  (anyone else's stories are context only — "it should only be my tickets");
* orders them: a story another one "blocks" (Shortcut story links), or that
  builds on another's code, waits for it; the rest run side by side;
* starts one ordinary ticket worker per story (``spawn_ticket_session`` —
  the same start path as Intake, so each story moves to its start state, gets
  its own branch ``feature/sc-<id>/…`` and its own session), going only as far
  as a pushed branch;
* opens one PR per story itself (``ship_session``), STACKED on the
  predecessor's branch when a story builds on another, so each PR shows only
  its own diff;
* keeps the workers in step (the contract between their pieces, questions,
  changes to a shared interface) through the MindFlock mailbox.

Ingested like tickets: a ticket source's ``epic_states`` (epic workflow states)
picks the epics; ``epic_auto`` "on" starts them by themselves
(:func:`run_loop`), otherwise Intake lists them (``GET /api/epics``) to start
by hand (``POST /api/epics/start``). Only epics the source's member owns.

Two servers can read one config dir (an installed one beside a dev one), so an
automatic start is claimed with an exclusive file first — one lead per epic.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backend.config.config import GetConfigDir

#: How often the auto-start loop looks for new epics.
POLL_S = 120.0
#: The listing is cached this long (Intake polls it).
LIST_TTL_S = 60.0
#: At most this many epics get their stories read for the listing.
MAX_DETAIL = 12

_LIST_CACHE: Dict[str, Tuple[float, dict]] = {}


class EpicError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", ""))
        self.status = status
        self.body = body


def _server():
    from backend.web import server

    return server


def _sources() -> list:
    """The ticket sources that ingest epics (``epic_states`` set, and a
    provider that can read epics)."""
    from backend.web.core import ticket_start as _ts
    from backend.ticket_ingestion.providers import get_provider

    out = []
    for src in _ts._load_config().ticketing_sources or []:
        if not str(getattr(src, "epic_states", "") or "").strip():
            continue
        try:
            provider = get_provider(src)
        except Exception:  # noqa: BLE001
            continue
        if getattr(provider, "can_epics", False):
            out.append((src, provider))
    return out


def _source(source: str):
    for src, provider in _sources():
        if (src.id or src.provider) == source:
            return src, provider
    return None, None


def title_for(provider, epic_id: str) -> str:
    """The lead session's title: ``sc-epic-<id>`` — the provider's slug
    prefix, so it never collides with a story's ``sc-<id>``."""
    return provider.make_slug("epic-%s" % epic_id)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return (s[:40].rstrip("-")) or "epic"


def branch_for(provider, epic_id: str, name: str) -> str:
    return "feature/%s/%s" % (title_for(provider, epic_id), _slugify(name))


def open_stories(detail: dict) -> Tuple[List[dict], List[dict]]:
    """``(mine, others)``: the epic's stories that are the person's own and
    not done (what the lead works), and the rest (context only)."""
    mine, others = [], []
    for st in detail.get("stories") or []:
        if st.get("mine") and st.get("state_type") != "done":
            mine.append(st)
        else:
            others.append(st)
    return mine, others


def _has_session(title: str) -> bool:
    srv = _server()
    if title in srv.ENGINE.instances:
        return True
    try:
        return any(r.get("title") == title for r in srv._pending_rows())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# The lead's prompt
# --------------------------------------------------------------------------- #
def _story_line(st: dict, ids: Dict[str, str]) -> str:
    def names(xs):
        return ", ".join(ids.get(x, "#%s" % x) for x in xs)

    bits = ["- **%s** — %s" % (st.get("slug"), st.get("name"))]
    if st.get("state"):
        bits.append("(%s)" % st["state"])
    if st.get("url"):
        bits.append(st["url"])
    line = " ".join(bits)
    deps = []
    if st.get("blocked_by"):
        deps.append("blocked by %s" % names(st["blocked_by"]))
    if st.get("blocks"):
        deps.append("blocks %s" % names(st["blocks"]))
    if st.get("relates"):
        deps.append("relates to %s" % names(st["relates"]))
    if deps:
        line += "\n  - " + "; ".join(deps)
    return line


def build_lead_prompt(
    detail: dict, source: str, base_branch: str, mine: List[dict], others: List[dict]
) -> str:
    """The lead session's first prompt (see the module docstring)."""
    ids = {
        st["id"]: st.get("slug") or "#%s" % st["id"]
        for st in detail.get("stories") or []
    }
    parts = [
        "# Epic: %s" % detail.get("name"),
        detail.get("url") or "",
        "",
        "You are the LEAD for this epic. You do not write the stories' code "
        "yourself: you run one MindFlock worker per story and make them work "
        "together, so each story lands as its OWN small pull request for review.",
        "",
        "## Your stories — owned by you, not done (work exactly these)",
    ]
    parts += [_story_line(st, ids) for st in mine] or ["- (none)"]
    if others:
        parts += [
            "",
            "## The rest of the epic — context only (someone else's, or done): never start these",
        ]
        parts += [_story_line(st, ids) for st in others[:40]]
    desc = str(detail.get("description") or "").strip()
    if desc:
        parts += ["", "## Epic description", desc[:6000]]
    parts += [
        "",
        "## How to run it",
        "1. **Plan.** Read each of your stories (the Shortcut tools, or `list_tickets`). "
        "Decide the order: a story that another one BLOCKS waits for it; so does a "
        "story that builds on another's code or changes the same files (say which). "
        "Everything else runs side by side. Write the plan as a table — story · waits "
        "for · why · its piece of the contract — in your first reply.",
        "2. **Start every story with nothing to wait for**, all at once: "
        '`spawn_ticket_session(ticket="sc-<id>", source="%s", autopilot="push", note=…)`. '
        '`autopilot="push"`: the worker commits and pushes its own branch and stops '
        "there — YOU open the PRs (step 4). In each `note`: what its neighbours are "
        "building, the contract between them (names, interfaces, files each one owns), "
        'and: "Before changing anything another story relies on, message your lead '
        '(send_message to=\\"parent\\")."' % source,
        "3. **Start a dependent story** once every story it waits for has reported "
        'done, with this at the top of its note: "This story builds on '
        "<predecessor's branch>. Before changing anything run `git fetch origin && "
        "git reset --hard origin/<predecessor's branch>`, then work on top of it.\" "
        "(Two predecessors: reset onto one, `git merge` the other.)",
        "4. **Open each story's PR yourself** when its worker reports done: look at "
        '`get_diff` first, then `ship_session(title=<worker>, depth="pr", '
        'base=<"%s", or the predecessor\'s branch when the story builds on one>)` — '
        "a stacked PR shows only its own story's diff. Put the story id in the PR title."
        % (base_branch or "the repo's base branch"),
        "5. **Keep them working together.** Relay any change to a shared contract to "
        "every worker it touches (`send_message`), answer their questions, and keep a "
        "status table (story · worker · state · PR). Wake on the next report with "
        '`wait_for_session(titles=[…], mode="any")` — never poll.',
        "6. **When every story has a PR**, end with the list: story → PR link → its "
        "base, and the merge order for stacked PRs.",
        "",
        "Rules: only the stories listed as yours. Never merge a PR, never push to %s, "
        "never start a story someone else owns. If a story can't be done, say why in "
        "the status table and carry on with the rest."
        % (base_branch or "the base branch"),
    ]
    return "\n".join(p for p in parts if p is not None)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
async def list_epics(force: bool = False) -> dict:
    """``{"epics": [row], "errors": [..]}`` for Intake: each source's epics in
    its ``epic_states`` that the person owns, with their own open stories
    counted. Row: ``{source, source_label, id, title, name, url, state,
    stories_total, mine_open, has_session, auto, eligible, reasons}``."""
    now = time.time()
    hit = _LIST_CACHE.get("all")
    if hit and not force and now - hit[0] < LIST_TTL_S:
        return hit[1]
    rows: List[dict] = []
    errors: List[dict] = []
    for src, provider in _sources():
        source = src.id or src.provider
        states = [x for x in str(src.epic_states).split(",") if x.strip()]
        try:
            names = {s["id"]: s["name"] for s in await provider.list_epic_states()}
            epics = await provider.list_epics(states, mine=True)
        except Exception as err:  # noqa: BLE001
            errors.append({"source": source, "error": str(err)[:300]})
            continue
        for i, e in enumerate(epics):
            title = title_for(provider, e["id"])
            mine_open = None
            if i < MAX_DETAIL:
                try:
                    mine, _o = open_stories(await provider.epic_detail(e["id"]))
                    mine_open = len(mine)
                except Exception:  # noqa: BLE001
                    mine_open = None
            has = _has_session(title)
            reasons = []
            if has:
                reasons.append("a lead session is already working it")
            if mine_open == 0:
                reasons.append("no stories of yours left to do in it")
            rows.append(
                {
                    "source": source,
                    "source_label": str(getattr(src, "label", "") or provider.label),
                    "id": e["id"],
                    "title": title,
                    "name": e["name"],
                    "url": e["url"],
                    "state": names.get(e["state_id"], e["state_id"]),
                    "stories_total": e["stories_total"],
                    "mine_open": mine_open,
                    "has_session": has,
                    "auto": bool(getattr(src, "epic_auto", False)),
                    "eligible": not reasons,
                    "reasons": reasons,
                }
            )
    out = {"epics": rows, "errors": errors}
    _LIST_CACHE["all"] = (now, out)
    return out


# --------------------------------------------------------------------------- #
# Starting a lead
# --------------------------------------------------------------------------- #
def _claim_path(title: str) -> Path:
    return Path(GetConfigDir()) / "epics" / ("%s.claim" % title)


def _claim(title: str, force: bool) -> bool:
    """An exclusive claim on starting ``title`` (two servers on one config dir
    must not both start a lead). ``force`` (a person's Start) takes over a
    stale claim whose session is gone."""
    path = _claim_path(title)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not force:
            return False
        path.write_text(str(time.time()), encoding="utf-8")
        return True
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    return True


async def launch(source: str, epic_id: str, *, auto: bool = False) -> dict:
    """Start the lead session for one epic → ``{"started": True, "title",
    "stories": n}``. Raises :class:`EpicError` (404 unknown source/epic, 409 a
    lead already works it / nothing of yours left / claimed by the other
    server, 400 no repo)."""
    from backend.ticket_ingestion.repo_routing import targets_of

    srv = _server()
    src, provider = _source(source)
    if src is None:
        raise EpicError(404, {"error": "no ticket source %r ingests epics" % source})
    epic_id = str(epic_id).strip().lstrip("#")
    if not epic_id.isdigit():
        raise EpicError(400, {"error": "epic id must be a number"})
    title = title_for(provider, epic_id)
    if _has_session(title):
        raise EpicError(
            409, {"error": "%s is already working this epic" % title, "title": title}
        )
    try:
        detail = await provider.epic_detail(epic_id)
    except Exception as err:  # noqa: BLE001
        raise EpicError(404, {"error": "couldn't read epic %s: %s" % (epic_id, err)})
    mine, others = open_stories(detail)
    if not mine:
        raise EpicError(
            409,
            {"error": "no stories of yours left to do in this epic", "title": title},
        )
    targets = targets_of(src)
    if not targets:
        raise EpicError(400, {"error": "this ticket source has no repository set"})
    if not _claim(title, force=not auto):
        raise EpicError(
            409, {"error": "another MindFlock server is starting this epic"}
        )
    repo_url = targets[0].url
    program = str(getattr(src, "agent", "") or "") or srv.ENGINE.default_program()
    branch = branch_for(provider, epic_id, detail.get("name") or "")
    base = ""
    try:
        from backend.config import settings as _settings

        st = _settings.load_settings()
        base = (st.repository.pr_base_branch or st.repository.base_branch or "").strip()
    except Exception:  # noqa: BLE001
        base = ""
    prompt = build_lead_prompt(detail, source, base, mine, others)
    srv._pending_add(title, "tix", branch=branch)

    async def _bg() -> None:
        try:
            inst = srv.session.NewInstance(
                srv.session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy=_workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=repo_url,
                    launch_args=srv._start_launch_args(program, ""),
                )
            )
            inst.ExtraEnv = srv._ports.env_for(title)
            inst.SetStatus(srv.Loading)
            with srv.ENGINE.lock:
                srv.ENGINE.instances[title] = inst
            srv._seed_event_snapshot(title)
            srv._events.BUS.emit(
                "session.created",
                session=title,
                new="loading",
                data={"program": inst.Program, "provisioned": True, "epic": epic_id},
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                srv.ENGINE.save()
            except Exception:
                srv._drop_failed_start(title, inst)
                raise
            move_to = str(getattr(src, "epic_start_state", "") or "").strip()
            if move_to and move_to != str(detail.get("state_id") or ""):
                try:
                    await provider.set_epic_state(epic_id, move_to)
                except Exception as err:  # noqa: BLE001 — the lead runs anyway
                    if srv.log.ErrorLog is not None:
                        srv.log.ErrorLog.Printf(
                            "epic %s: couldn't move it: %v", epic_id, err
                        )
            if srv.log.InfoLog is not None:
                srv.log.InfoLog.Printf(
                    "epic %s: lead %s started on %d stor%s",
                    epic_id,
                    title,
                    len(mine),
                    "y" if len(mine) == 1 else "ies",
                )
        except Exception as err:  # noqa: BLE001
            try:
                _claim_path(title).unlink()
            except OSError:
                pass
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf(
                    "epic %s: lead failed to start: %v", epic_id, err
                )
            srv._note_create_failure(title, str(err))
            srv._events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
        finally:
            srv._pending_drop(title)
            _LIST_CACHE.clear()

    srv._register_task(_bg())
    _LIST_CACHE.clear()
    return {"started": True, "title": title, "stories": len(mine), "auto": auto}


def _workspace_mode() -> str:
    from backend.web.core import ticket_start as _ts

    return _ts.workspace_mode()


async def auto_start_once() -> List[str]:
    """Start a lead for every eligible epic on a source with ``epic_auto``
    on → the titles started."""
    started = []
    data = await list_epics(force=True)
    for row in data["epics"]:
        if not (row["auto"] and row["eligible"]):
            continue
        if _claim_path(row["title"]).exists():
            continue  # started once already (a finished lead is not restarted)
        try:
            out = await launch(row["source"], row["id"], auto=True)
            started.append(out["title"])
        except EpicError:
            continue
    return started


async def run_loop() -> None:
    """The auto-start loop (started by the ticket-ingestion addon). Never
    raises; idle while no source has ``epic_auto`` on."""
    srv = _server()
    while True:
        try:
            if any(getattr(s, "epic_auto", False) for s, _p in _sources()):
                await auto_start_once()
        except Exception as err:  # noqa: BLE001
            if srv.log.ErrorLog is not None:
                srv.log.ErrorLog.Printf("epics: auto-start pass failed: %v", err)
        await asyncio.sleep(POLL_S)
