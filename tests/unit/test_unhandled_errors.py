"""A route that raises answers JSON with the reason and logs its traceback.

A friend's native-Windows New session failed with ``POST /api/instances ->
500`` twice: the dialog said only "MindFlock hit an error (details in the
log)", and ``mindflock.log`` held nothing but those two lines — the traceback
went to uvicorn's stderr. These pin the replacement (core.unhandled).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import log
from backend.web import server
from backend.web.core import session_create as _session_create
from backend.web.core import unhandled


class _Capture:
    def __init__(self) -> None:
        self.lines: list = []

    def Printf(self, fmt, *args):  # noqa: N802 — the log port's Go casing
        self.lines.append(fmt % args if args else fmt)


@pytest.fixture
def captured(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(log, "ErrorLog", cap)
    return cap


def test_create_that_raises_answers_the_reason_and_logs_the_traceback(
    monkeypatch, captured
):
    async def _boom(payload, *, person=""):
        raise RuntimeError("the MindFlock session host is not answering")

    monkeypatch.setattr(_session_create, "create_requested", _boom)
    with TestClient(server.app, raise_server_exceptions=False) as c:
        r = c.post("/api/instances", json={"title": "", "repo_path": "C:\\x"})
    assert r.status_code == 500
    body = r.json()
    # The sentence the dialog shows (api/client.ts reads `error`).
    assert "RuntimeError: the MindFlock session host is not answering" in body["error"]
    assert body["exception"] == "RuntimeError"
    # …and the traceback is in mindflock.log, naming the route and the frame.
    logged = "\n".join(captured.lines)
    assert "unhandled error in POST /api/instances" in logged
    assert "Traceback (most recent call last)" in logged
    assert "_boom" in logged


def test_a_half_deleted_install_says_so(monkeypatch, captured):
    """The friend's actual failure: an engine update on Windows failed half
    way and deleted the tool venv's Lib under the running server, whose next
    lazy import raised. The dialog must say the install is damaged and how to
    fix it — a retry cannot help."""

    async def _boom(payload, *, person=""):
        raise ImportError(
            "cannot import name 'preflight' from 'backend.session'",
            name="backend.session",
        )

    monkeypatch.setattr(_session_create, "create_requested", _boom)
    with TestClient(server.app, raise_server_exceptions=False) as c:
        r = c.post("/api/instances", json={"title": "", "repo_path": "C:\\x"})
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == body["error"]
    assert body["detail"].startswith("MindFlock's engine install is damaged")
    assert "cannot import name 'preflight'" in body["detail"]
    assert "reinstall" in body["detail"]
    assert body["exception"] == "ImportError"
    assert "unhandled error in POST /api/instances" in "\n".join(captured.lines)


def test_only_mindflocks_own_imports_read_as_a_damaged_install():
    gone = ModuleNotFoundError("No module named 'backend'", name="backend")
    assert unhandled.sentence(gone).startswith("MindFlock's engine install is damaged")
    # An optional third-party package missing is not a damaged install.
    other = ModuleNotFoundError(
        "No module named 'faster_whisper'", name="faster_whisper"
    )
    assert unhandled.sentence(other).startswith(
        "MindFlock hit an error: ModuleNotFoundError"
    )


def test_reason_is_one_line_and_bounded():
    assert unhandled.reason(ValueError("a\n  b")) == "ValueError: a b"
    assert unhandled.reason(KeyError()) == "KeyError"
    long = unhandled.reason(OSError("x" * 1000))
    assert (
        long.startswith("OSError: ")
        and len(long) <= len("OSError: ") + unhandled.MAX_REASON
    )


def test_logging_never_masks_the_answer(monkeypatch):
    class _Broken:
        def Printf(self, *a):  # noqa: N802
            raise OSError("disk full")

    monkeypatch.setattr(log, "ErrorLog", _Broken())
    unhandled.log_traceback("POST", "/api/x", RuntimeError("y"))  # must not raise
