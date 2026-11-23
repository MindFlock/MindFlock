"""Feedback: the draft store, scrubbing, the routes and the MCP's
``report_issue`` (backend/web/core/feedback.py, routes/feedback.py,
backend/mcp/feedback.py).

Nothing here reaches GitHub: ``github_pr.api_token`` and ``_request`` are
stubbed, and the drafts file lives under a scratch ``MINDFLOCK_HOME``.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from backend import client as api_client
from backend.mcp.protocol import ToolError
from backend.mcp.tools import build_tools
from backend.web import server
from backend.web.core import events as E
from backend.web.core import feedback as F
from backend.web.core import github_pr
from backend.web.core import guests
from backend.web.core import remote
from tests.unit._mcp_fakes import make_box, row


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(F, "_path", lambda: str(tmp_path / "home" / F.FILE))
    return tmp_path / "home"


@pytest.fixture()
def github(monkeypatch):
    """A stand-in GitHub: records the calls, answers what the test sets."""

    class G:
        token = "t0ken"
        answer = (201, {"html_url": "https://github.com/x/y/issues/7", "number": 7})
        calls: list = []

    G.calls = []

    async def api_token():
        return G.token

    async def request(method, path, *, token, params=None, body=None):
        G.calls.append((method, path, token, body))
        return G.answer

    monkeypatch.setattr(github_pr, "api_token", api_token)
    monkeypatch.setattr(github_pr, "_request", request)
    return G


@pytest.fixture()
def web(home, github, monkeypatch):
    monkeypatch.setattr(
        "backend.web.routes.feedback._identity",
        lambda: {"signed_in": True, "login": "octo"},
    )
    return TestClient(server.app)


# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #
class TestScrub:
    def test_keys_are_replaced(self):
        fake = "ghp_" + "a" * 36  # pragma: allowlist secret
        text, n = F.scrub("pushed with %s and it failed" % fake)
        assert fake not in text and F.REMOVED in text and n == 1

    def test_a_named_secret_keeps_its_name(self):
        text, n = F.scrub(
            "API_KEY=abcd1234efgh5678 was set"
        )  # pragma: allowlist secret
        assert text == "API_KEY=%s was set" % F.REMOVED and n == 1

    def test_a_private_key_block_goes(self):
        # Built from parts: a literal header would trip the secret scan.
        dashes, name = "-" * 5, "OPENSSH PRIVATE " + "KEY"
        block = "%sBEGIN %s%s\nabc\n%sEND %s%s" % (
            dashes,
            name,
            dashes,
            dashes,
            name,
            dashes,
        )
        text, _ = F.scrub("key:\n%s\nend" % block)
        assert "abc" not in text and text.endswith("end")

    def test_the_home_folder_becomes_a_tilde(self, monkeypatch):
        monkeypatch.setattr(F, "_home", lambda: "/home/dana")
        text, n = F.scrub("crash in /home/dana/work/app.py and /Users/lee/x")
        assert text == "crash in ~/work/app.py and /Users/<user>/x" and n == 2

    def test_paths_are_left_alone_when_asked(self, monkeypatch):
        monkeypatch.setattr(F, "_home", lambda: "/home/dana")
        text, n = F.scrub("see /home/dana/x", paths=False)
        assert text == "see /home/dana/x" and n == 0

    def test_ordinary_text_is_untouched(self):
        src = "The Send button does nothing after I rename a session."
        assert F.scrub(src) == (src, 0)


# --------------------------------------------------------------------------- #
# Composing
# --------------------------------------------------------------------------- #
class TestCompose:
    def test_the_about_line_is_added_and_can_be_left_out(self):
        with_it = F.compose("Title", "Body", "bug")
        assert with_it["body"] == "Body\n\n---\n" + F.about_line()
        assert with_it["labels"] == ["bug"]
        assert F.compose("Title", "Body", "bug", include_about=False)["body"] == "Body"

    def test_the_about_line_says_only_version_and_os(self):
        line = F.about_line()
        assert line.startswith("MindFlock ")
        assert os.path.expanduser("~") not in line
        assert len(line) < 80

    def test_a_message_alone_takes_its_first_line_as_the_title(self):
        out = F.compose("", "Sky view is slow\nwith 40 sessions", "feedback")
        assert out["title"] == "Sky view is slow" and out["labels"] == []

    def test_nothing_to_send_is_refused(self):
        with pytest.raises(F.FeedbackError):
            F.compose("", "   ", "bug")

    def test_an_unknown_kind_is_feedback(self):
        assert F.compose("T", "B", "rant")["kind"] == "feedback"

    def test_a_key_typed_by_the_person_is_still_removed(self):
        fake = "sk-" + "b" * 30  # pragma: allowlist secret
        out = F.compose("T", "my key %s" % fake, "bug", include_about=False)
        assert fake not in out["body"] and out["removed"] == 1

    def test_the_browser_link_is_prefilled_and_bounded(self):
        url = F.issue_url("A title", "x" * 30000, ["bug"])
        assert url.startswith("https://github.com/%s/issues/new?" % F.REPO)
        assert len(url) <= F.MAX_URL
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert q["title"] == ["A title"] and q["labels"] == ["bug"]
        assert "cut to fit a link" in q["body"][0]


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #
class TestDrafts:
    def test_a_draft_is_stored_scrubbed_and_listed_newest_first(
        self, home, monkeypatch
    ):
        monkeypatch.setattr(F, "_home", lambda: "/home/dana")
        F.add_draft("First", "in /home/dana/app", "bug", session="s1")
        second = F.add_draft("Second", "an idea", "idea")
        rows = F.drafts()
        assert [r["title"] for r in rows] == ["Second", "First"]
        assert rows[1]["body"] == "in ~/app" and rows[1]["removed"] == 1
        assert rows[1]["session"] == "s1" and second["kind"] == "idea"

    def test_the_same_title_replaces_the_older_draft(self, home):
        F.add_draft("Same", "one", "bug")
        F.add_draft("Same", "two", "bug")
        rows = F.drafts()
        assert len(rows) == 1 and rows[0]["body"] == "two"

    def test_only_the_newest_are_kept(self, home):
        for i in range(F.MAX_DRAFTS + 5):
            F.add_draft("Draft %d" % i, "body", "bug")
        rows = F.drafts()
        assert len(rows) == F.MAX_DRAFTS
        assert rows[0]["title"] == "Draft %d" % (F.MAX_DRAFTS + 4)

    def test_a_draft_needs_a_title_and_a_body(self, home):
        with pytest.raises(F.FeedbackError):
            F.add_draft("", "body")
        with pytest.raises(F.FeedbackError):
            F.add_draft("Title", "")

    def test_remove(self, home):
        rec = F.add_draft("Gone", "body")
        assert F.remove_draft(rec["id"]) is True
        assert F.remove_draft(rec["id"]) is False
        assert F.drafts() == []

    def test_a_broken_file_reads_as_no_drafts(self, home):
        (home / F.FILE).write_text("{not json", encoding="utf-8")
        assert F.drafts() == []


# --------------------------------------------------------------------------- #
# Send
# --------------------------------------------------------------------------- #
class TestSend:
    def test_github_takes_it_and_the_draft_goes(self, home, github):
        rec = F.add_draft("Crash on rename", "It crashed.", "bug")
        out = asyncio.run(F.send(rec["title"], rec["body"], "bug", draft_id=rec["id"]))
        assert out["via"] == "github" and out["number"] == 7
        assert out["url"] == "https://github.com/x/y/issues/7"
        method, path, token, body = github.calls[0]
        assert (method, path, token) == ("POST", "/repos/%s/issues" % F.REPO, "t0ken")
        assert body["title"] == "Crash on rename" and body["labels"] == ["bug"]
        assert body["body"].endswith(F.about_line())
        assert F.drafts() == []

    def test_no_sign_in_gives_a_browser_link_and_keeps_the_draft(self, home, github):
        github.token = ""
        rec = F.add_draft("Idea", "Make it faster.", "idea")
        out = asyncio.run(F.send(rec["title"], rec["body"], "idea", draft_id=rec["id"]))
        assert out["via"] == "browser" and "issues/new?" in out["url"]
        assert "signed in" in out["why"]
        assert github.calls == [] and len(F.drafts()) == 1

    @pytest.mark.parametrize(
        "status, word",
        [
            (0, "reached"),
            (401, "no longer valid"),
            (403, "can't file"),
            (404, "can't file"),
        ],
    )
    def test_a_refusal_falls_back_to_the_browser(self, home, github, status, word):
        github.answer = (status, {"message": "nope"})
        out = asyncio.run(F.send("Title", "Body", "bug"))
        assert out["via"] == "browser" and word in out["why"]

    def test_feedback_sends_no_labels(self, home, github):
        asyncio.run(F.send("Title", "Body", "feedback"))
        assert "labels" not in github.calls[0][3]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
class TestRoutes:
    def test_get_says_what_the_box_needs(self, web):
        F.add_draft("Waiting", "body", "bug")
        got = web.get("/api/feedback").json()
        assert got["repo"] == F.REPO and got["about"] == F.about_line()
        assert got["signed_in"] is True and got["login"] == "octo"
        assert [d["title"] for d in got["drafts"]] == ["Waiting"]

    def test_a_draft_is_stored_and_announced(self, web):
        seen = []
        unsub = E.BUS.subscribe(lambda env: seen.append(env))
        try:
            r = web.post(
                "/api/feedback/drafts",
                json={"title": "T", "body": "Body text", "session": "s1"},
            )
        finally:
            if callable(unsub):
                unsub()
        assert r.status_code == 201 and r.json()["kind"] == "bug"
        mine = [e for e in seen if e.get("event") == "feedback.draft"]
        assert mine and mine[0]["data"]["title"] == "T"
        assert mine[0].get("session") == "s1"

    def test_a_bad_draft_is_a_400(self, web):
        r = web.post("/api/feedback/drafts", json={"title": "", "body": "x"})
        assert r.status_code == 400 and "title" in r.json()["error"]

    def test_discard(self, web):
        rec = F.add_draft("T", "body")
        assert web.delete("/api/feedback/drafts/" + rec["id"]).status_code == 200
        assert web.delete("/api/feedback/drafts/" + rec["id"]).status_code == 404

    def test_send_files_the_issue(self, web, github):
        r = web.post(
            "/api/feedback/send", json={"title": "T", "body": "B", "kind": "bug"}
        )
        assert r.status_code == 200 and r.json()["via"] == "github"
        assert github.calls[0][3]["body"].endswith(F.about_line())

    def test_send_without_the_about_line(self, web, github):
        web.post(
            "/api/feedback/send",
            json={"title": "T", "body": "B", "include_about": False},
        )
        assert github.calls[0][3]["body"] == "B"

    def test_send_with_nothing_is_a_400(self, web, github):
        r = web.post("/api/feedback/send", json={"title": "", "body": ""})
        assert r.status_code == 400 and github.calls == []

    @pytest.mark.parametrize(
        "method, path",
        [
            ("get", "/api/feedback"),
            ("post", "/api/feedback/drafts"),
            ("post", "/api/feedback/send"),
            ("delete", "/api/feedback/drafts/abc"),
        ],
    )
    def test_a_guest_and_a_relayed_request_get_nothing(
        self, web, github, monkeypatch, method, path
    ):
        monkeypatch.setattr(guests, "scope_guest", lambda request: {"id": "g"})
        assert getattr(web, method)(path).status_code == 403
        monkeypatch.setattr(guests, "scope_guest", lambda request: None)
        monkeypatch.setattr(remote, "from_remote", lambda request: True)
        assert getattr(web, method)(path).status_code == 403
        assert github.calls == []

    def test_a_guest_never_sees_the_draft_event(self):
        env = {"event": "feedback.draft", "session": "", "data": {}}
        assert guests.event_allowed(env, {"id": "g", "sessions": []}) is False


# --------------------------------------------------------------------------- #
# The MCP tool
# --------------------------------------------------------------------------- #
class TestReportIssueTool:
    def _box(self, route=None, me="worker"):
        box, api, _ = make_box([row(me)], me=me)
        if route is not None:
            api.top_routes[("POST", "/api/feedback/drafts")] = route
        return box, api

    def test_it_drafts_and_says_what_to_tell_the_user(self):
        box, api = self._box(
            lambda payload, q: {"id": "ab12", "title": payload["title"], "removed": 0}
        )
        out = box.report_issue({"title": "Send does nothing", "body": "Steps: …"}, None)
        assert out["drafted"] is True and out["id"] == "ab12"
        assert "nothing is posted until you press Send" in out["tell"]
        sent = [c for c in api.calls if c[1] == "/api/feedback/drafts"][0][2]
        assert sent == {
            "title": "Send does nothing",
            "body": "Steps: …",
            "kind": "bug",
            "session": "worker",
        }

    def test_it_reports_what_was_replaced(self):
        box, _ = self._box(lambda p, q: {"id": "x", "title": "T", "removed": 2})
        out = box.report_issue({"title": "Title", "body": "A long enough body"}, None)
        assert out["removed"] == 2 and "replaced" in out["note"]

    def test_an_old_server_points_at_the_issues_page(self):
        box, api = self._box()
        api.errors[("POST", "/api/feedback/drafts")] = api_client.ApiError(
            404, "Not Found"
        )
        with pytest.raises(ToolError) as err:
            box.report_issue({"title": "Title", "body": "A long enough body"}, None)
        assert "issues/new" in str(err.value)

    def test_the_server_s_reason_is_passed_on(self):
        box, api = self._box()
        api.errors[("POST", "/api/feedback/drafts")] = api_client.ApiError(
            400, "a report needs a title"
        )
        with pytest.raises(ToolError) as err:
            box.report_issue({"title": "Title", "body": "A long enough body"}, None)
        assert "needs a title" in str(err.value)

    def test_the_tool_is_served_as_a_draft_only_write(self):
        box, _ = self._box()
        tool = {t.name: t for t in build_tools(box)}["report_issue"]
        assert tool.annotations["openWorldHint"] is False
        assert tool.annotations["destructiveHint"] is False
        assert "PUBLIC" in tool.description and len(tool.description) < 1500
        assert "nothing is posted by you" in tool.description
