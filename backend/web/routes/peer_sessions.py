"""Sessions shared over a peer link (docs/peer-link.md, "Shared sessions"):
the ``/api/peer/links/{id}/sessions*`` routes and the proxied-row plumbing
for a peer's sessions on this rail. Filled in by the shared-sessions work;
the router is included from ``server.py`` already.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
