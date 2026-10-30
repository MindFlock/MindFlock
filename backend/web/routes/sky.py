"""The Sky's own routes: ``/api/sky/*`` (docs/flights.md "As built: round 15
objects").

``GET /api/sky/landing`` is the landing strip's data (``core.landing``);
``GET`` / ``PUT /api/sky/notes`` read and replace the sticky notes
(``core.sky_notes``). Both are thin: the shapes live in the core modules.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.web.core import landing as _landing
from backend.web.core import sky_notes as _notes
from backend.web.routes import _server

router = APIRouter()


@router.get("/api/sky/landing")
async def sky_landing() -> JSONResponse:
    """``{"repos": [{root, name, branch, head, checks, last_merge_at,
    landed: [{flight, title, pr_url, at}]}], "at"}`` — cached a minute."""
    return JSONResponse(await _landing.landing(_server().ENGINE.instances))


@router.get("/api/sky/notes")
def sky_notes() -> JSONResponse:
    """``{"notes": [Note]}``."""
    return JSONResponse({"notes": _notes.load_notes()})


@router.put("/api/sky/notes")
def sky_notes_put(payload: Optional[dict] = None) -> JSONResponse:
    """Replace the notes: ``{"notes": [Note]}`` in, the same out as stored;
    400 when ``notes`` is not a list."""
    body = payload if isinstance(payload, dict) else {}
    try:
        notes = _notes.save_notes(body.get("notes"))
    except _notes.NotesError as err:
        return JSONResponse({"error": "bad_notes", "detail": str(err)}, status_code=400)
    return JSONResponse({"notes": notes})
