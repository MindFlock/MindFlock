"""Session chat (docs/people.md, "A small chat"): the people on one session
— its owner, a guest on it, a peer with it open — talking to each other in a
little window; ``/api/instances/{title}/chat``. Filled in by the joint-chat
work; the router is included from ``server.py`` already.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
