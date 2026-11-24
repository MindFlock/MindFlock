"""Help routes (:mod:`backend.web.core.help_ask`; docs/web-api.md "Help").

* ``POST /api/help/ask`` — ``{question, places?: [{id, label}]}``: a typed
  question about MindFlock answered from its docs → ``{text, found, source:
  {doc, heading, path, excerpt} | null, model, pointers: [id], limited}``.
  ``places`` are the app's pointable places (the frontend's
  ``lib/helpPlaces``); ``pointers`` are only ever ids from that list. 400
  when there is no question.

The owner only: a guest link is refused (and the guest middleware never
lets it through — the path is not in ``guests.GLOBAL_POST_OK``), so no
question from a guest reaches the model.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

#: Longest question handed to the docs search and the model (characters).
QUESTION_MAX = 400


def _ask(question: str, places) -> dict:
    """``core.help_ask.answer`` — blocking (the model call). Module-level so
    tests replace it."""
    from backend.web.core import help_ask as _help_ask

    return _help_ask.answer(question, places)


@router.post("/api/help/ask")
async def help_ask(request: Request) -> JSONResponse:
    from backend.web.core import guests as _guests

    if _guests.scope_guest(request) is not None:
        return JSONResponse(
            {"error": "help questions are asked by the owner, not from a guest invite"},
            status_code=403,
        )
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — not JSON: a bad body, not a 422
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    q = payload.get("question")
    q = " ".join(q.split())[:QUESTION_MAX] if isinstance(q, str) else ""
    if not q:
        return JSONResponse({"error": "question must be a question"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(_ask, q, payload.get("places")))
