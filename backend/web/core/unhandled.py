"""The answer to a request whose route raised: a sentence, and a traceback.

Without this an uncaught exception was Starlette's plain-text ``Internal
Server Error``: the dialog could only say "MindFlock hit an error (details in
the log)", and ``mindflock.log`` — the log Settings → System logs shows, and
the one people send — held nothing but ``POST /api/instances -> 500``. The
traceback went to uvicorn's stderr, which on the desktop app is a separate
file nobody is pointed at. A New session that failed on a friend's native
Windows install (2026-11-13) left exactly that: two 500s and no reason.
(The cause, found later: a failed engine update had half-deleted the
install under the running server — see :data:`DAMAGED_INSTALL`.)

So every unhandled exception now leaves its traceback in ``mindflock.log``
(ErrorLog), and the response is JSON whose ``detail`` / ``error`` names the
exception — the text every client already shows for a refused request
(``frontend/src/api/client.ts`` reads ``detail`` / ``error``). An ImportError
of MindFlock's own code says the install is damaged and how to fix it.
"""

from __future__ import annotations

import traceback

from fastapi.responses import JSONResponse

from backend import log

#: How much of the exception's own text the answer carries (the log has all).
MAX_REASON = 300


def reason(exc: BaseException) -> str:
    """``"<ExceptionType>: <its message>"``, one line, at most
    :data:`MAX_REASON` characters of the message."""
    text = " ".join(str(exc).split())
    if len(text) > MAX_REASON:
        text = text[: MAX_REASON - 1] + "…"
    name = type(exc).__name__
    return "%s: %s" % (name, text) if text else name


def log_traceback(method: str, path: str, exc: BaseException) -> None:
    """Write ``exc``'s traceback to ``mindflock.log``. Never raises."""
    try:
        if log.ErrorLog is None:
            return
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        log.ErrorLog.Printf("unhandled error in %s %s:\n%s", method, path, tb.rstrip())
    except Exception:  # noqa: BLE001 — logging must never mask the answer
        pass


#: What a person is told when MindFlock's own code can't be imported: its
#: install is damaged under the running server. That is what the friend's
#: create hit (``ImportError: cannot import name 'preflight' from
#: 'backend.session'``) after an engine update on Windows failed half-way and
#: deleted the tool venv's ``Lib`` — the server kept running from memory until
#: its next lazy import. Retrying cannot help; reinstalling does.
DAMAGED_INSTALL = (
    "MindFlock's engine install is damaged (%s). Quit MindFlock, reinstall "
    "the engine (run the installer again, or `uv tool install --force "
    '"mindflock[web]"`), then start MindFlock again.'
)


def sentence(exc: BaseException) -> str:
    """What the person is told about ``exc``: a damaged install for an
    ImportError (ModuleNotFoundError included) naming MindFlock's own code,
    else the exception itself."""
    if isinstance(exc, ImportError):
        mod = str(getattr(exc, "name", "") or "")
        text = str(exc)
        if mod.startswith("backend") or "'backend" in text:
            return DAMAGED_INSTALL % reason(exc)
    return "MindFlock hit an error: %s (the full error is in the MindFlock log)" % (
        reason(exc)
    )


def answer(request, exc: BaseException) -> JSONResponse:
    """The 500 for ``request`` whose route raised ``exc`` (logged first):
    ``{"error", "detail", "exception"}`` — ``detail`` and ``error`` carry the
    same sentence (clients read ``detail`` first, older ones ``error``)."""
    method = getattr(request, "method", "") or ""
    path = getattr(getattr(request, "url", None), "path", "") or ""
    log_traceback(method, path, exc)
    said = sentence(exc)
    return JSONResponse(
        {"error": said, "detail": said, "exception": type(exc).__name__},
        status_code=500,
    )
