"""``core.session_create`` — the create route's body, callable without HTTP.

``POST /api/instances`` is a one-line wrapper around ``session_create.create``
(its behaviour is pinned by the route's own tests, which pass unchanged); a
team run creates its task sessions through ``create_result``. These pin that
the two are the same code and that ``create_result`` speaks plain
``(status, body)``.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.web import server
from backend.web.core import session_create
from tests.unit.test_webui import _stub_create


@pytest.fixture
def reg(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    return instances


def test_the_route_is_a_thin_wrapper():
    src = inspect.getsource(server.create_instance)
    assert "return await _session_create.create(payload)" in src


def test_create_result_claims_the_title_and_answers_202(reg, monkeypatch):
    captured: list = []
    _stub_create(monkeypatch, captured, git_enabled=True)
    status, body = asyncio.run(
        session_create.create_result(
            {"title": "q4-ratelimit", "program": "bash", "repo_path": "/tmp/x"}
        )
    )
    assert status == 202
    assert body["title"] == "q4-ratelimit"
    assert "q4-ratelimit" in reg
    (opts,) = captured
    assert opts.title == "q4-ratelimit" and opts.in_place is False


def test_create_result_reports_a_refusal_as_status_and_error(reg, monkeypatch):
    _stub_create(monkeypatch, git_enabled=True)
    reg["taken"] = object()
    status, body = asyncio.run(
        session_create.create_result({"title": "taken", "program": "bash"})
    )
    assert status == 409
    assert "already exists" in body["error"]
