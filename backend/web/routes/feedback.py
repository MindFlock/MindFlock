"""Feedback routes (:mod:`backend.web.core.feedback`; docs/web-api.md
"Feedback").

* ``GET /api/feedback`` — ``{repo, about, drafts, signed_in, login}``: what
  the feedback box needs to open (``about`` is the one line MindFlock would
  add; ``login`` is the GitHub account a report would be filed as).
* ``POST /api/feedback/drafts`` — ``{title, body, kind?, session?, agent?}``:
  an agent's draft (the MCP's ``report_issue``). 201 the draft. Emits
  ``feedback.draft`` so the app offers it. Nothing is sent anywhere.
* ``DELETE /api/feedback/drafts/{id}`` — discard one.
* ``POST /api/feedback/send`` — ``{title, body, kind?, include_about?,
  draft_id?}``: file it. ``{ok, via: "github", url, number}`` or ``{ok, via:
  "browser", url, why}`` (a pre-filled page the app opens).

Only the owner, on this device: a guest link and a request another MindFlock
relayed are refused, drafting included.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.web.core import events as _events
from backend.web.core import feedback as _feedback

router = APIRouter()


def _refusal(request: Optional[Request]) -> Optional[JSONResponse]:
    if request is None:
        return None
    from backend.web.core import guests as _guests
    from backend.web.core import remote as _remote

    if _guests.scope_guest(request) is not None:
        return JSONResponse(
            {"error": "feedback is sent by the owner, not from a guest invite"},
            status_code=403,
        )
    if _remote.from_remote(request):
        return JSONResponse(
            {"error": "send feedback from the device you are on"}, status_code=403
        )
    return None


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty or not JSON
        return {}
    return body if isinstance(body, dict) else {}


def _identity() -> dict:
    """``{signed_in, login}`` for the GitHub sign-in a report would use."""
    try:
        from backend.web.core import github_auth as _auth

        token, _src = _auth.resolve_full_chain()
        if not token:
            return {"signed_in": False, "login": ""}
        user = _auth.fetch_user(token)
        return {"signed_in": True, "login": str(user.get("login") or "")}
    except Exception:  # noqa: BLE001 — "not signed in" is a normal state
        return {"signed_in": False, "login": ""}


@router.get("/api/feedback")
async def feedback_get(request: Request) -> JSONResponse:
    no = _refusal(request)
    if no is not None:
        return no
    who = await asyncio.to_thread(_identity)
    rows = await asyncio.to_thread(_feedback.drafts)
    return JSONResponse(
        {
            "repo": _feedback.REPO,
            "about": _feedback.about_line(),
            "drafts": rows,
            **who,
        }
    )


@router.post("/api/feedback/drafts")
async def feedback_draft(request: Request) -> JSONResponse:
    no = _refusal(request)
    if no is not None:
        return no
    body = await _json(request)
    try:
        rec = await asyncio.to_thread(
            lambda: _feedback.add_draft(
                body.get("title"),
                body.get("body"),
                body.get("kind") or "bug",
                session=str(body.get("session") or ""),
                agent=str(body.get("agent") or ""),
            )
        )
    except _feedback.FeedbackError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except OSError as err:
        return JSONResponse(
            {"error": "couldn't save the draft: %s" % err}, status_code=500
        )
    _events.BUS.emit(
        "feedback.draft",
        session=rec["session"],
        data={"id": rec["id"], "title": rec["title"], "kind": rec["kind"]},
    )
    return JSONResponse(rec, status_code=201)


@router.delete("/api/feedback/drafts/{draft_id}")
async def feedback_discard(draft_id: str, request: Request) -> JSONResponse:
    no = _refusal(request)
    if no is not None:
        return no
    gone = await asyncio.to_thread(_feedback.remove_draft, draft_id)
    if not gone:
        return JSONResponse({"error": "that draft is already gone"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/feedback/send")
async def feedback_send(request: Request) -> JSONResponse:
    no = _refusal(request)
    if no is not None:
        return no
    body = await _json(request)
    try:
        out = await _feedback.send(
            body.get("title"),
            body.get("body"),
            body.get("kind") or "feedback",
            include_about=body.get("include_about") is not False,
            draft_id=str(body.get("draft_id") or ""),
        )
    except _feedback.FeedbackError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(out)
