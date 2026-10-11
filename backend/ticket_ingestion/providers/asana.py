"""Asana provider.

Ingests tasks assigned to you (``assignee=me``) within a workspace
(``project`` = workspace gid), changed since the last run. Auth: a personal
access token (Bearer). Task notes are plain text; comments come from the task's
"stories" (type=comment) and attachments from the attachments endpoint.

Asana has no workflow states; its **sections** play that part here. A task sits
in one section per project it belongs to, so ``list_states`` offers every
section of every (unarchived) project in the workspace, qualified by its
project the way Shortcut qualifies a state by its workflow, ``workflow_state``
narrows the search to tasks in those sections (``GET /tasks?section=``, the one
Asana listing that takes no assignee — which is what lets ``assignee_scope =
"anyone"`` stand on it), and ``set_state`` is ``POST /sections/{gid}/addTask``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from backend.ticket_ingestion.models import Attachment, Ticket
from backend.ticket_ingestion.providers.base import (
    HTTP_TIMEOUT,
    ProviderError,
    TicketNotFound,
    TicketProvider,
    ingests_any_assignee,
    parse_acceptance_criteria,
    parse_iso8601,
    workflow_state_list,
)

_logger = logging.getLogger(__name__)
# Shared request budget (defined once in providers/base.py).
_HTTP_TIMEOUT = HTTP_TIMEOUT
_API = "https://app.asana.com/api/1.0"
_MAX_TASKS = 50
#: Projects whose sections ``list_states`` offers — one request per project, so
#: a workspace with hundreds of them is cut off rather than enumerated.
_MAX_PROJECTS = 50
#: What every task read asks for: the panel buckets by ``memberships`` (the
#: task's section per project), which the plain listing never returned before.
_TASK_FIELDS = (
    "name,notes,permalink_url,assignee.gid,assignee.name,created_at,completed,"
    "memberships.section.gid,memberships.section.name,memberships.project.name,"
    # The claim mirror reads the claim label (an Asana tag) off the task.
    "tags.name"
)


def _section_label(project: str, section: str) -> str:
    """How a section is named everywhere it is shown: ``Project · Section`` —
    the panel buckets by this exact string, so :meth:`list_states` and the
    ticket's ``state`` have to spell it the same way."""
    return f"{project} · {section}" if project else section


class AsanaProvider(TicketProvider):
    name = "asana"
    label = "Asana"
    slug_prefix = "asana"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_token}",
            "Accept": "application/json",
        }

    async def _get(
        self, session: aiohttp.ClientSession, path: str, params: dict | None = None
    ) -> Any:
        async with session.get(
            f"{_API}{path}", params=params or {}, headers=self._headers()
        ) as resp:
            if resp.status in (401, 403):
                raise ProviderError(f"Asana rejected the token (HTTP {resp.status})")
            if resp.status != 200:
                text = await resp.text()
                raise aiohttp.ClientError(
                    f"Asana API returned {resp.status}: {text[:200]}"
                )
            payload = await resp.json()
        return payload.get("data")

    async def _get_all(
        self,
        session: aiohttp.ClientSession,
        path: str,
        params: dict | None = None,
        max_pages: int = 20,
    ) -> list:
        """Every page of a list endpoint — Asana caps a page at 100 and hands
        back ``next_page.offset`` for the rest; a section with more tasks than
        one page must not silently drop the person's own work. ``max_pages``
        bounds a runaway listing."""
        out: list = []
        params = dict(params or {})
        for _ in range(max_pages):
            async with session.get(
                f"{_API}{path}", params=params, headers=self._headers()
            ) as resp:
                if resp.status in (401, 403):
                    raise ProviderError(
                        f"Asana rejected the token (HTTP {resp.status})"
                    )
                if resp.status != 200:
                    text = await resp.text()
                    raise aiohttp.ClientError(
                        f"Asana API returned {resp.status}: {text[:200]}"
                    )
                payload = await resp.json()
            out.extend(payload.get("data") or [])
            nxt = (
                (payload.get("next_page") or {}) if isinstance(payload, dict) else {}
            ).get("offset")
            if not nxt:
                break
            params["offset"] = nxt
        return out

    async def _comments(self, session: aiohttp.ClientSession, gid: str) -> list[str]:
        try:
            stories = await self._get(
                session,
                f"/tasks/{gid}/stories",
                {"opt_fields": "type,text,created_at,created_by.name"},
            )
        except (ProviderError, aiohttp.ClientError):
            return []
        out = []
        for s in stories or []:
            if s.get("type") != "comment":
                continue
            text = (s.get("text") or "").strip()
            if not text:
                continue
            author = (s.get("created_by") or {}).get("name") or "unknown"
            out.append(f"[{s.get('created_at') or ''} by {author}] {text}")
        return out

    async def _attachments(
        self, session: aiohttp.ClientSession, gid: str
    ) -> list[Attachment]:
        try:
            atts = await self._get(
                session,
                "/attachments",
                {"parent": gid, "opt_fields": "name,download_url,view_url"},
            )
        except (ProviderError, aiohttp.ClientError):
            return []
        out = []
        for a in atts or []:
            url = a.get("download_url") or a.get("view_url")
            if not url:
                continue
            out.append(Attachment(name=a.get("name") or "attachment", url=url))
        return out

    async def _task_to_ticket(
        self, session: aiohttp.ClientSession, task: dict[str, Any]
    ) -> Ticket:
        gid = str(task.get("gid") or "")
        notes = task.get("notes") or ""
        assignee = task.get("assignee") or {}
        state, state_id = self._membership_state(task)
        return Ticket(
            id=gid,
            name=str(task.get("name") or ""),
            description=notes,
            acceptance_criteria=parse_acceptance_criteria(notes),
            owner_ids=[str(assignee.get("gid"))] if assignee.get("gid") else [],
            owner_names=[str(assignee.get("name"))] if assignee.get("name") else [],
            app_url=task.get("permalink_url") or "",
            created_at=parse_iso8601(task.get("created_at")),
            comments=await self._comments(session, gid),
            attachments=await self._attachments(session, gid),
            provider="asana",
            slug=self.make_slug(gid),
            source_label=self.label,
            state=state,
            state_id=state_id,
            labels=[
                str(t.get("name"))
                for t in task.get("tags") or []
                if isinstance(t, dict) and t.get("name")
            ],
        )

    def _membership_state(self, task: dict[str, Any]) -> tuple[str, str]:
        """``(state, state_id)`` for a task: the section it sits in. A task in
        several projects has several sections; the one this source ingests
        from wins, else the first — the ingest filter and the pending-ticket
        re-check both match on ``state_id``, so the configured one has to be
        the one reported."""
        wanted = set(workflow_state_list(self.cfg))
        memberships = [m for m in task.get("memberships") or [] if isinstance(m, dict)]
        chosen = next(
            (
                m
                for m in memberships
                if str((m.get("section") or {}).get("gid") or "") in wanted
            ),
            memberships[0] if memberships else None,
        )
        if not chosen:
            return "", ""
        section = chosen.get("section") or {}
        project = chosen.get("project") or {}
        sid = str(section.get("gid") or "")
        return (
            _section_label(
                str(project.get("name") or ""), str(section.get("name") or sid)
            ),
            sid,
        )

    async def _me_gid(self, session: aiohttp.ClientSession) -> str:
        """The token owner's user gid, for filtering a section listing down to
        "mine" when the source names no ``member_id`` (the assignee listing
        takes the literal ``me``; a section listing takes no assignee at all)."""
        me = await self._get(session, "/users/me", {"opt_fields": "gid"})
        return str((me or {}).get("gid") or "")

    async def _search(self, since: datetime, *, with_state: bool) -> list[Ticket]:
        """Tasks changed since ``since``: by assignee within the workspace, or —
        when the source ingests from sections and ``with_state`` keeps that
        filter — one listing per section, narrowed to the assignee client-side
        unless the source takes anyone's. Shared by the poll and the panel."""
        sections = workflow_state_list(self.cfg) if with_state else []
        any_assignee = ingests_any_assignee(self.cfg)
        common = {
            "modified_since": since.isoformat(),
            "completed_since": "now",  # exclude tasks completed before now
            "limit": str(_MAX_TASKS),
            "opt_fields": _TASK_FIELDS,
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            if not sections:
                if not self.cfg.project:
                    raise ProviderError(
                        "asana requires ticketing.project = the workspace gid"
                    )
                tasks = await self._get_all(
                    session,
                    "/tasks",
                    {
                        "assignee": self.cfg.member_id or "me",
                        "workspace": self.cfg.project,
                        **common,
                    },
                )
                rows = list(tasks or [])
            else:
                mine = "" if any_assignee else (self.cfg.member_id or "")
                if not any_assignee and not mine:
                    mine = await self._me_gid(session)
                rows, seen = [], set()
                for sid in sections:
                    tasks = await self._get_all(
                        session, "/tasks", {"section": sid, **common}
                    )
                    for t in tasks or []:
                        gid = str(t.get("gid") or "")
                        if not gid or gid in seen:
                            continue
                        if (
                            mine
                            and str((t.get("assignee") or {}).get("gid") or "") != mine
                        ):
                            continue
                        seen.add(gid)
                        rows.append(t)
            out = []
            for t in rows:
                if t.get("completed"):
                    continue
                out.append(await self._task_to_ticket(session, t))
        return out

    async def search_assigned(self, since: datetime) -> list[Ticket]:
        return await self._search(since, with_state=True)

    async def search_assigned_all(self) -> list[Ticket]:
        """Every open task assigned to you, in ANY section and with no age
        cutoff, each annotated with its section so the panel can bucket it.
        An any-assignee source keeps its section filter — it is the only bound
        on a search that names no assignee (same rule as Linear)."""
        return await self._search(
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            with_state=ingests_any_assignee(self.cfg),
        )

    async def fetch(self, ticket_id: str) -> Ticket:
        params = {"opt_fields": _TASK_FIELDS}
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            task = await self._get(session, f"/tasks/{ticket_id}", params)
            if not task:
                raise TicketNotFound(f"Asana task {ticket_id} not found")
            return await self._task_to_ticket(session, task)

    async def test_connection(self) -> tuple[dict | None, str]:
        """Identity, plus the workspace when the source names none: a token
        sees its workspaces (``GET /workspaces``), so nobody should have to
        copy a gid out of a URL. One workspace is the answer (``project``);
        several come back as ``workspaces`` for the form to offer a pick."""
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                me = await self._get(session, "/users/me", {"opt_fields": "name,gid"})
                spaces: list[dict] = []
                if not (self.cfg.project or "").strip():
                    rows = await self._get(
                        session,
                        "/workspaces",
                        {"opt_fields": "name,gid", "limit": "50"},
                    )
                    spaces = [
                        {
                            "gid": str(w.get("gid")),
                            "name": str(w.get("name") or w.get("gid")),
                        }
                        for w in rows or []
                        if isinstance(w, dict) and w.get("gid")
                    ]
        except ProviderError as e:
            return None, str(e)
        except aiohttp.ClientError as e:
            return None, f"network error reaching Asana: {e}"
        identity: dict = {
            "member_id": str((me or {}).get("gid", "")),
            "name": (me or {}).get("name"),
        }
        if len(spaces) == 1:
            identity["project"] = spaces[0]["gid"]
            identity["project_name"] = spaces[0]["name"]
        elif spaces:
            identity["workspaces"] = spaces
        return identity, ""

    async def list_states(self) -> list[dict]:
        """Every section of every unarchived project in the workspace — the
        closest thing Asana has to a workflow state. ``id`` is the section gid
        (what ``workflow_state`` / ``start_state`` store), ``name`` the
        ``Project · Section`` label the panel buckets by, ``group``/``label``
        the two halves. No ``type``: Asana says nothing about whether a section
        means done."""
        if not (self.cfg.project or "").strip():
            raise ProviderError("no Asana workspace configured — run Test to pick one")
        out: list[dict] = []
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            projects = await self._get_all(
                session,
                "/projects",
                {
                    "workspace": self.cfg.project.strip(),
                    "archived": "false",
                    "limit": str(_MAX_PROJECTS),
                    "opt_fields": "name",
                },
            )
            for p in (projects or [])[:_MAX_PROJECTS]:
                pgid = str(p.get("gid") or "")
                pname = str(p.get("name") or pgid)
                if not pgid:
                    continue
                sections = await self._get_all(
                    session, f"/projects/{pgid}/sections", {"opt_fields": "name"}
                )
                for sec in sections or []:
                    sid = str(sec.get("gid") or "")
                    if not sid:
                        continue
                    sname = str(sec.get("name") or sid)
                    out.append(
                        {
                            "id": sid,
                            "name": _section_label(pname, sname),
                            "group": pname,
                            "label": sname,
                            "type": "",
                        }
                    )
        return out

    async def set_state(self, ticket_id: str, state_id: str) -> None:
        """Move a task into a section (``POST /sections/{gid}/addTask``). A task
        already in another section of the same project is moved; one not in
        that project is added to it."""
        target = (state_id or "").strip()
        if not target.isdigit():
            raise ProviderError(f"Asana section gid must be numeric, got {state_id!r}")
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{_API}/sections/{target}/addTask",
                json={"data": {"task": str(ticket_id)}},
                headers=self._headers(),
            ) as resp:
                if resp.status in (401, 403):
                    raise ProviderError(
                        f"Asana rejected the token (HTTP {resp.status})"
                    )
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise ProviderError(
                        f"Asana could not move task {ticket_id} into section "
                        f"{target} (HTTP {resp.status}): {text[:200]}"
                    )

    # --- claim mirror (tracker_claims) -------------------------------- #

    can_claim = True

    async def _write(
        self, session: aiohttp.ClientSession, method: str, path: str, data: dict
    ) -> Any:
        call = session.put if method == "PUT" else session.post
        async with call(
            f"{_API}{path}", json={"data": data}, headers=self._headers()
        ) as resp:
            if resp.status in (401, 403):
                raise ProviderError(f"Asana rejected the token (HTTP {resp.status})")
            if resp.status not in (200, 201):
                text = await resp.text()
                raise ProviderError(
                    f"Asana refused {method} {path} (HTTP {resp.status}): {text[:200]}"
                )
            payload = await resp.json()
        return (payload or {}).get("data")

    async def assign_to(self, ticket_id: str, member: str) -> None:
        """Asana tasks have ONE assignee: set it to ``member`` (a user gid)."""
        member = (member or "").strip()
        if not member:
            raise ProviderError("no Asana user to assign")
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            await self._write(
                session, "PUT", f"/tasks/{ticket_id}", {"assignee": member}
            )

    async def _task_tags(self, session: aiohttp.ClientSession, gid: str) -> list:
        task = await self._get(session, f"/tasks/{gid}", {"opt_fields": "tags.name"})
        return [t for t in (task or {}).get("tags") or [] if isinstance(t, dict)]

    async def add_label(self, ticket_id: str, label: str) -> None:
        """The label is an Asana TAG in the source's workspace (``project``):
        reused by name, created when the workspace has none."""
        low = label.casefold()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            if any(
                str(t.get("name") or "").casefold() == low
                for t in await self._task_tags(session, ticket_id)
            ):
                return
            ws = (self.cfg.project or "").strip()
            if not ws:
                raise ProviderError("this Asana source has no workspace for tags")
            tags = await self._get(
                session,
                f"/workspaces/{ws}/tags",
                {"opt_fields": "name", "limit": "100"},
            )
            gid = next(
                (
                    str(t.get("gid"))
                    for t in tags or []
                    if str(t.get("name") or "").casefold() == low and t.get("gid")
                ),
                "",
            )
            if not gid:
                made = await self._write(
                    session, "POST", "/tags", {"name": label, "workspace": ws}
                )
                gid = str((made or {}).get("gid") or "")
            if not gid:
                raise ProviderError(f"Asana would not create the tag {label!r}")
            await self._write(
                session, "POST", f"/tasks/{ticket_id}/addTag", {"tag": gid}
            )

    async def remove_label(self, ticket_id: str, label: str) -> None:
        low = label.casefold()
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            for t in await self._task_tags(session, ticket_id):
                if str(t.get("name") or "").casefold() == low and t.get("gid"):
                    await self._write(
                        session,
                        "POST",
                        f"/tasks/{ticket_id}/removeTag",
                        {"tag": str(t["gid"])},
                    )

    async def add_comment(self, ticket_id: str, body: str) -> None:
        """Post a comment on a task (``POST /tasks/{gid}/stories``) — what the
        source's ``start_comment`` says when it moves the task into its
        ``start_state`` section."""
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{_API}/tasks/{ticket_id}/stories",
                json={"data": {"text": body}},
                headers=self._headers(),
            ) as resp:
                if resp.status in (401, 403):
                    raise ProviderError(
                        f"Asana rejected the token (HTTP {resp.status})"
                    )
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise ProviderError(
                        f"Asana would not comment on task {ticket_id} "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )

    # --- filing a new ticket (New → Ticket) -------------------------- #

    can_create = True

    def create_blocker(self) -> str:
        """Asana's ``project`` field is the workspace gid, and a task cannot be
        created outside one. Same question :meth:`search_assigned` already
        refuses on, asked before the draft rather than after it."""
        if not (self.cfg.project or "").strip():
            return (
                "This Asana source has no workspace to file into — run Test on "
                "it to pick one (or set its Workspace field to the gid) first."
            )
        return ""

    async def create_ticket(self, name: str, description: str) -> Ticket:
        """File a new task (``POST /tasks``) and return it, hydrated.

        ``notes`` rather than ``html_notes``: the description is markdown, and
        Asana's rich-text field would render the ``##`` and ``-`` markers as
        literal characters inside a paragraph. Plain notes keep the markdown
        intact, which is what :func:`parse_acceptance_criteria` reads back when
        the task is later ingested — the round trip matters more here than the
        rendering does.

        ``assignee`` falls back to the literal ``"me"``, the same token
        :meth:`search_assigned` uses for its default: the task has to come back
        from that search, and "me" is how Asana spells the token's owner.
        """
        blocker = self.create_blocker()
        if blocker:
            raise ProviderError(blocker)
        body = {
            "data": {
                "workspace": (self.cfg.project or "").strip(),
                "name": name,
                "notes": description,
                "assignee": self.cfg.member_id or "me",
            }
        }
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(
                f"{_API}/tasks", json=body, headers=self._headers()
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise ProviderError(
                        f"Asana could not create the task "
                        f"(HTTP {resp.status}): {text[:200]}"
                    )
                created = (await resp.json()).get("data") or {}
            gid = str(created.get("gid") or "")
            if not gid:
                raise ProviderError("Asana created a task but did not return its gid")
            # Re-read rather than converting the create response: _task_to_ticket
            # wants opt_fields this POST did not ask for (permalink_url above
            # all, which IS the link this whole feature exists to hand back).
            return await self.fetch(gid)
