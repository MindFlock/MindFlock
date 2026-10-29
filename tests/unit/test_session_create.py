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
    assert "return await _session_create.create(" in src
    # …stamping the person the request is for, never one named in the payload.
    assert "person=_auth.request_person(request)" in src


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


# --------------------------------------------------------------------------- #
# A blank Name is named from the task
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "prompt,title",
    [
        ("Add a README section about setup", "add-readme-section-about-setup"),
        ("  \n\nFix the login bug in acme-api!\nmore detail", "fix-login-bug-acme-api"),
        ("please update the CHANGELOG for 0.9", "update-changelog-0-9"),
        ("a the to", "a-the-to"),  # all filler: keep the words rather than nothing
        ("🙂 ✨", ""),
        ("", ""),
        ("x" * 50, "x" * session_create.TITLE_FROM_PROMPT_MAX),
        (
            "refactor authentication middleware session handling cleanup tests",
            "refactor-authentication",
        ),
    ],
)
def test_title_from_prompt(prompt, title):
    assert session_create.title_from_prompt(prompt) == title


def test_free_title_numbers_past_taken_names(reg, monkeypatch):
    monkeypatch.setattr(server, "_session_branches", lambda: {})
    reg["fix-login"] = object()
    reg["fix-login-2"] = object()
    assert session_create.free_title("fix-login") == "fix-login-3"
    assert session_create.free_title("other") == "other"


def test_blank_title_with_a_task_is_named_from_it(reg, monkeypatch):
    _stub_create(monkeypatch, git_enabled=True)
    monkeypatch.setattr(server._thread, "note_seed", lambda *a, **k: None)
    monkeypatch.setattr(server._prompt_queue, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(server._prompt_queue, "set_flags", lambda *a, **k: None)
    body = {"title": "", "program": "bash", "repo_path": "/tmp/x"}
    s1, b1 = asyncio.run(
        session_create.create_result({**body, "prompt": "Fix the login bug"})
    )
    s2, b2 = asyncio.run(
        session_create.create_result({**body, "prompt": "fix the login bug"})
    )
    s3, b3 = asyncio.run(session_create.create_result(dict(body)))
    assert (s1, b1["title"]) == (202, "fix-login-bug")
    assert (s2, b2["title"]) == (202, "fix-login-bug-2")
    # No task either: named for the folder it works in (/tmp/x → "x").
    assert (s3, b3["title"]) == (202, "x")


def test_title_from_place():
    assert (
        session_create.title_from_place("/home/me/Allure/sitecheck-bot7")
        == "sitecheck-bot7"
    )
    assert session_create.title_from_place("/home/me/My Repo.git/") == "my-repo-git"
    assert session_create.title_from_place("C:\\\\work\\\\App") == "app"
    # No folder, or nothing nameable in it: the caller falls back to untitled.
    assert session_create.title_from_place("") == ""
    assert session_create.title_from_place("/tmp/---") == ""
    assert (
        len(session_create.title_from_place("/x/" + "a" * 80))
        == session_create.TITLE_FROM_PROMPT_MAX
    )
