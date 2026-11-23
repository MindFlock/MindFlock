"""Sample hub items for the ``hub`` / ``hub-full`` preview scenarios,
added through the server's own hub routes (``POST /api/hub/items``,
``/api/hub/text``, ``/api/instances/{title}/hub/put``), oldest first so the
panel lists them newest first: a screenshot of an error page, a server log,
an SQL snippet with a note, a PDF, a phone screenshot (``source=phone``) and
a picture an agent put from its worktree.

The pictures are drawn here (a tiny PNG painter: filled rectangles, no
Pillow), so the thumbnails show something that reads as a screenshot.
"""

from __future__ import annotations

import json
import os
import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, List, Sequence, Tuple

from mfpreview.sandbox import Sandbox, call

Rect = Tuple[int, int, int, int, Tuple[int, int, int]]


def png(w: int, h: int, bg: Tuple[int, int, int], rects: Sequence[Rect]) -> bytes:
    """A ``w``×``h`` RGB PNG: ``bg`` with ``rects`` (x, y, w, h, colour)
    painted in order."""
    rows = [bytearray(bytes(bg) * w) for _ in range(h)]
    for x, y, rw, rh, col in rects:
        c = bytes(col)
        for yy in range(max(0, y), min(h, y + rh)):
            x0, x1 = max(0, x), min(w, x + rw)
            if x1 > x0:
                rows[yy][x0 * 3 : x1 * 3] = c * (x1 - x0)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def error_page() -> bytes:
    """A browser window showing a login form under a red 500 banner."""
    return png(
        640,
        400,
        (245, 246, 248),
        [
            (0, 0, 640, 34, (222, 225, 230)),
            (14, 11, 12, 12, (237, 106, 94)),
            (32, 11, 12, 12, (245, 191, 79)),
            (50, 11, 12, 12, (98, 197, 84)),
            (90, 8, 420, 18, (255, 255, 255)),
            (0, 34, 640, 46, (196, 43, 28)),
            (24, 50, 260, 14, (255, 225, 220)),
            (200, 120, 240, 220, (255, 255, 255)),
            (224, 146, 120, 12, (60, 64, 72)),
            (224, 176, 192, 26, (232, 234, 238)),
            (224, 214, 192, 26, (232, 234, 238)),
            (224, 262, 192, 30, (58, 110, 220)),
        ],
    )


def dashboard() -> bytes:
    """A dashboard with a chart — what an agent's UI test saved."""
    bars = [(60 + i * 46, 300 - hgt, 30, hgt, (88, 140, 230)) for i, hgt in enumerate((80, 140, 110, 190, 160, 220, 170, 240))]
    return png(
        640,
        400,
        (22, 26, 33),
        [
            (0, 0, 640, 40, (32, 38, 48)),
            (16, 14, 110, 12, (210, 216, 226)),
            (40, 70, 560, 260, (30, 35, 44)),
            *bars,
            (40, 340, 170, 40, (38, 44, 56)),
            (230, 340, 170, 40, (38, 44, 56)),
            (420, 340, 180, 40, (38, 44, 56)),
        ],
    )


def phone_shot() -> bytes:
    """A phone screenshot: a checkout page with a misplaced button."""
    return png(
        300,
        620,
        (250, 250, 250),
        [
            (0, 0, 300, 44, (20, 20, 20)),
            (16, 70, 180, 18, (40, 40, 40)),
            (16, 110, 268, 120, (226, 230, 236)),
            (16, 250, 268, 16, (200, 204, 210)),
            (16, 276, 200, 16, (200, 204, 210)),
            (120, 330, 260, 46, (34, 160, 90)),
            (16, 560, 268, 40, (230, 232, 236)),
        ],
    )


PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 62>>stream\nBT /F1 24 Tf 72 700 Td (Invoice export: columns) Tj ET\n"
    b"endstream endobj\n5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

LOG = "\n".join(
    [
        "2026-11-22 14:31:58 INFO  POST /api/login 200 41ms",
        "2026-11-22 14:32:03 INFO  POST /api/login 200 38ms",
        "2026-11-22 14:32:05 ERROR POST /api/login 500 1203ms",
        "Traceback (most recent call last):",
        '  File "app/auth.py", line 2, in login',
        "    return user.check(password)",
        "AttributeError: 'NoneType' object has no attribute 'check'",
        "2026-11-22 14:32:09 WARN  rate limiter: 12 failed logins from 10.0.4.7",
    ]
) + "\n"

SQL = (
    "SELECT i.id, i.number, a.name, i.total_cents\n"
    "FROM invoices i JOIN accounts a ON a.id = i.account_id\n"
    "WHERE i.issued_at >= now() - interval '30 days'\n"
    "ORDER BY i.issued_at DESC;\n"
)


def upload(sb: Sandbox, name: str, data: bytes, **q: Any) -> Tuple[int, Dict[str, Any]]:
    qs = urllib.parse.urlencode({"name": name, **q})
    req = urllib.request.Request(
        sb.base_url + "/api/hub/items?" + qs,
        data=data,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def _folder(sb: Sandbox, title: str) -> str:
    status, rows = call(sb.base_url, "GET", "/api/instances")
    for r in rows if isinstance(rows, list) else []:
        if r.get("title") == title:
            return str(r.get("folder") or r.get("worktree") or "")
    return ""


def seed_hub(sb: Sandbox, sessions: List[str], log: List[str], full: bool = False) -> List[str]:
    """Add the sample items; returns their references (oldest first)."""
    refs: List[str] = []

    def done(what: str, st: int, body: Dict[str, Any]) -> None:
        if st >= 400:
            log.append("hub %s refused %s %s" % (what, st, body))
        else:
            refs.append(str(body.get("ref")))

    st, body = upload(sb, "invoice-spec.pdf", PDF)
    done("pdf", st, body)
    st, body = upload(sb, "server.log", LOG.encode())
    done("log", st, body)
    st, body = call(
        sb.base_url,
        "POST",
        "/api/hub/text",
        {"text": SQL, "name": "recent invoices.sql", "note": "the slow query from the billing page"},
    )
    done("snippet", st, body)
    st, body = upload(sb, "IMG_2041.PNG", phone_shot(), source="phone")
    done("phone photo", st, body)
    agent = next((t for t in ("api-auth", "docs-refresh", "billing-export") if t in sessions), "")
    if agent:
        folder = _folder(sb, agent)
        if folder and os.path.isdir(folder):
            os.makedirs(os.path.join(folder, "test-output"), exist_ok=True)
            with open(os.path.join(folder, "test-output", "dashboard-after.png"), "wb") as f:
                f.write(dashboard())
            st, body = call(
                sb.base_url,
                "POST",
                "/api/instances/%s/hub/put" % urllib.parse.quote(agent),
                {"path": "test-output/dashboard-after.png", "note": "after the chart fix"},
            )
            done("agent put", st, body)
        else:
            log.append("hub: no folder for %s yet" % agent)
    st, body = upload(sb, "screenshot 14-32-05.png", error_page())
    done("screenshot", st, body)
    if full:
        for i in range(24):
            st, body = call(
                sb.base_url,
                "POST",
                "/api/hub/text",
                {"text": "Note %d: retry the export with a smaller page size (%d rows).\n" % (i + 1, 500 + i * 50)},
            )
            done("filler %d" % i, st, body)
    return refs
