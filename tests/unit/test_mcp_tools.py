"""MCP tool handlers (backend.mcp.tools) against an in-memory MindFlock API.

Lineage/policy, messaging, the wait_for_session done-rules, spawn defaults
and the delete guard. Git-dependent behavior (fork point, dirty warning,
unmerged-commit detection) runs against real throwaway repos.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from backend import client
from backend.mcp import tools as mcp_tools
from backend.mcp.protocol import Cancelled, SchemaError, ToolError
from backend.mcp.tools import INSTRUCTIONS, build_tools, report_footer, split_diff
from tests.unit._mcp_fakes import FakeClock, FakeCtx, make_box, row


def _tree():
    return [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
        row("w1a", parent="w1", spawned=True),
        row("w2", parent="orch", spawned=True),
        row("other"),
        row("dev::far", tmux_name="mindflock_far"),
    ]


def _ctx(clock, **kw):
    return FakeCtx(clock, **kw)


# --------------------------------------------------------------------------- #
# Text budgets and registry
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_twenty_five_tools_with_annotations(self):
        box, _, _ = make_box(_tree())
        tools = build_tools(box)
        assert [t.name for t in tools] == [
            "whoami",
            "list_sessions",
            "get_session",
            "read_output",
            "get_diff",
            "send_message",
            "check_inbox",
            "wait_for_message",
            "report_result",
            "spawn_session",
            "wait_for_session",
            "answer_prompt",
            "kill_session",
            "set_parent",
            "list_tickets",
            "spawn_ticket_session",
            "ship_session",
            "set_autopilot",
            "start_team_run",
            "get_run",
            "list_runs",
            "wait_for_run",
            "control_run",
            "propose_run_plan",
            "report_integrated",
        ]
        by = {t.name: t for t in tools}
        for name in (
            "whoami",
            "list_sessions",
            "get_session",
            "read_output",
            "get_diff",
            "check_inbox",
            "wait_for_message",
            "wait_for_session",
        ):
            assert by[name].annotations["readOnlyHint"] is True
        for name in (
            "send_message",
            "report_result",
            "answer_prompt",
            "set_parent",
            "spawn_session",
        ):
            assert by[name].annotations["destructiveHint"] is False
            assert by[name].annotations["openWorldHint"] is False
        assert by["kill_session"].annotations["destructiveHint"] is True
        # Shipping pushes code, opens PRs and can merge them.
        for name in ("ship_session", "set_autopilot"):
            assert by[name].annotations["destructiveHint"] is True
            assert by[name].annotations["openWorldHint"] is True
            assert by[name].annotations["readOnlyHint"] is False
        assert by["list_tickets"].annotations["readOnlyHint"] is True
        assert by["list_tickets"].annotations["openWorldHint"] is True
        assert by["spawn_ticket_session"].annotations["readOnlyHint"] is False
        assert by["spawn_ticket_session"].annotations["openWorldHint"] is True
        # Team runs: the reads are reads; starting and steering one reach out
        # (its members push code and open PRs) and always prompt.
        for name in ("get_run", "list_runs", "wait_for_run"):
            assert by[name].annotations["readOnlyHint"] is True
        for name in ("start_team_run", "control_run"):
            assert by[name].annotations["readOnlyHint"] is False
            assert by[name].annotations["openWorldHint"] is True

    def test_text_budgets(self):
        box, _, _ = make_box(_tree())
        assert len(INSTRUCTIONS) <= 1900
        for t in build_tools(box):
            assert len(t.description) <= 1500, t.name
            assert t.input_schema["type"] == "object"
            assert t.input_schema.get("additionalProperties") is False

    def test_instructions_name_every_tool_and_the_qualified_send(self):
        box, _, _ = make_box(_tree())
        for t in build_tools(box):
            assert t.name in INSTRUCTIONS
        assert "mcp__mindflock__send_message" in INSTRUCTIONS
        assert "NOT the built-in SendMessage" in INSTRUCTIONS

    def test_footer_names_the_tool_per_provider(self):
        assert "mcp__mindflock__report_result" in report_footer("w", "o", "claude")
        codex = report_footer("w", "o", "codex")
        assert 'report_result tool of the "mindflock" MCP server' in codex
        assert "mcp__" not in codex
        assert '"w", spawned by "o"' in codex


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #
class TestWhoamiAndList:
    def test_whoami(self):
        box, api, clock = make_box(_tree())
        api.deliver("orch", "w1")
        out = box.whoami({}, _ctx(clock))
        assert out["session"]["title"] == "orch"
        assert out["session"]["is_self"] is True
        assert out["external"] is False
        assert out["scope"] == "children"
        assert out["parent"] == "root"
        assert sorted(out["children"]) == ["w1", "w2"]
        assert out["unread"] == 1
        assert out["server"] == "http://127.0.0.1:8765"

    def test_whoami_external(self):
        box, _, clock = make_box(_tree(), me=None)
        out = box.whoami({}, _ctx(clock))
        assert out["session"] is None and out["external"] is True
        assert out["parent"] is None and out["children"] == []

    def test_whoami_managed_marker_unresolved_is_readonly(self):
        box, _, clock = make_box(_tree(), me=None, managed_marker=True)
        out = box.whoami({}, _ctx(clock))
        assert out["scope"] == "readonly"
        assert "cannot tell which session" in out["note"]

    def test_list_defaults_to_children_when_you_have_some(self):
        box, _, clock = make_box(_tree())
        out = box.list_sessions({}, _ctx(clock))
        assert out["filter"] == "children"
        assert sorted(r["title"] for r in out["sessions"]) == ["w1", "w2"]
        assert all(r["managed"] for r in out["sessions"])

    def test_list_defaults_to_all_without_children(self):
        box, _, clock = make_box(_tree(), me="other")
        out = box.list_sessions({}, _ctx(clock))
        assert out["filter"] == "all"
        titles = [r["title"] for r in out["sessions"]]
        assert "dev::far" in titles
        far = next(r for r in out["sessions"] if r["title"] == "dev::far")
        assert far["remote"] is True and far["managed"] is False

    def test_list_managed_first_limit_and_more(self):
        box, _, clock = make_box(_tree())
        out = box.list_sessions({"filter": "all", "limit": 2}, _ctx(clock))
        assert [r["managed"] for r in out["sessions"]] == [True, True]
        assert out["more"] == len(_tree()) - 2

    def test_list_descendants_siblings_repo(self):
        rows = _tree()
        rows[2]["repo"] = "other-repo"
        box, _, clock = make_box(rows)
        desc = box.list_sessions({"filter": "descendants"}, _ctx(clock))
        assert sorted(r["title"] for r in desc["sessions"]) == ["w1", "w1a", "w2"]
        box2, _, _ = make_box(_tree(), me="w1")
        sib = box2.list_sessions({"filter": "siblings"}, _ctx(clock))
        assert [r["title"] for r in sib["sessions"]] == ["w2"]
        repo = box.list_sessions({"filter": "all", "repo": "other-repo"}, _ctx(clock))
        assert [r["title"] for r in repo["sessions"]] == ["w1"]

    def test_list_siblings_needs_identity(self):
        box, _, clock = make_box(_tree(), me=None)
        with pytest.raises(ToolError, match="identity"):
            box.list_sessions({"filter": "siblings"}, _ctx(clock))

    def test_get_session(self):
        box, _, clock = make_box(_tree())
        out = box.get_session({"title": "w1"}, _ctx(clock))["session"]
        assert out["children"] == ["w1a"] and out["managed"] is True
        assert out["queue"]["pending"] == 0  # the full row, not the compact one
        with pytest.raises(ToolError, match="no session named"):
            box.get_session({"title": "ghost"}, _ctx(clock))


class TestReadOutput:
    def test_passes_view_and_max_chars(self):
        box, api, clock = make_box(_tree())
        api.outputs["w1"] = {"view": "screen", "text": "SCREEN", "truncated": False}
        out = box.read_output(
            {"title": "w1", "view": "screen", "max_chars": 500}, _ctx(clock)
        )
        assert out["text"] == "SCREEN"
        assert (
            api.paths("GET")[-1] == "/api/instances/w1/output?view=screen&max_chars=500"
        )

    def test_missing_route_falls_back_to_history_tail(self):
        box, api, clock = make_box(_tree())
        api.route_missing.add("output")
        out = box.read_output({"title": "w1", "max_chars": 200}, _ctx(clock))
        assert out["fallback"] is True and out["view"] == "transcript"
        assert out["text"].endswith("x" * 50)

    def test_unknown_session(self):
        box, _, clock = make_box(_tree())
        with pytest.raises(ToolError, match="instance not found"):
            box.read_output({"title": "ghost"}, _ctx(clock))

    def test_titles_are_url_quoted(self):
        rows = _tree() + [row("has space")]
        box, api, clock = make_box(rows)
        box.read_output({"title": "has space"}, _ctx(clock))
        assert api.paths("GET")[-1].startswith("/api/instances/has%20space/output?")


_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 1..2 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-old\n"
    "+new\n"
    "@@ -10,1 +10,2 @@\n"
    " ctx\n"
    "+++added line that starts with plus\n"
    "diff --git a/docs/b.md b/docs/b.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/docs/b.md\n"
    "@@ -0,0 +1,3 @@\n"
    "+one\n"
    "+two\n"
    "+three\n"
    "diff --git a/gone.txt b/gone.txt\n"
    "deleted file mode 100644\n"
    "--- a/gone.txt\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-bye\n"
    "diff --git a/img.png b/img.png\n"
    "Binary files a/img.png and b/img.png differ\n"
)


class TestDiff:
    def test_split_diff(self):
        files = split_diff(_DIFF)
        assert [(f["path"], f["status"], f["added"], f["removed"]) for f in files] == [
            ("src/a.py", "modified", 2, 1),
            ("docs/b.md", "added", 3, 0),
            ("gone.txt", "deleted", 0, 1),
            ("img.png", "binary", 0, 0),
        ]
        assert len(files[0]["hunks"]) == 2
        assert split_diff("") == []

    def test_get_diff_full(self):
        box, api, clock = make_box(_tree())
        api.diffs["w1"] = {
            "added": 6,
            "removed": 2,
            "content": _DIFF,
            "error": None,
            "base": "fork",
        }
        out = box.get_diff({"title": "w1"}, _ctx(clock))
        assert out["diff"] == _DIFF
        assert out["truncated"] is False
        assert [f["path"] for f in out["files"]] == [
            "src/a.py",
            "docs/b.md",
            "gone.txt",
            "img.png",
        ]
        assert api.calls[-1][1] == "/api/instances/w1/diff?base=fork"

    def test_get_diff_files_filter(self):
        box, api, clock = make_box(_tree())
        api.diffs["w1"] = {
            "added": 6,
            "removed": 2,
            "content": _DIFF,
            "error": None,
            "base": "fork",
        }
        out = box.get_diff({"title": "w1", "files": ["docs/", "nope.py"]}, _ctx(clock))
        assert "docs/b.md" in out["diff"] and "src/a.py" not in out["diff"]
        assert out["files_not_found"] == ["nope.py"]
        assert len(out["files"]) == 4  # the stat always covers everything

    def test_truncation_keeps_whole_hunks(self):
        big_hunk = "@@ -1,1 +1,400 @@\n" + "".join("+line %d\n" % i for i in range(400))
        content = (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n-x\n+y\n"
            + big_hunk
            + "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-p\n+q\n"
            + "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n"
            + big_hunk
        )
        box, api, clock = make_box(_tree())
        api.diffs["w1"] = {
            "added": 1,
            "removed": 1,
            "content": content,
            "error": None,
            "base": "fork",
        }
        out = box.get_diff({"title": "w1", "max_chars": 1000}, _ctx(clock))
        assert out["truncated"] is True
        assert out["partial_files"] == ["a.py"]
        assert out["omitted_files"] == ["c.py"]
        assert "+y\n" in out["diff"] and "+q\n" in out["diff"]
        assert "+line 0" not in out["diff"]  # the big hunk was not cut mid-way
        assert len(out["diff"]) <= 1000

    def test_server_side_error(self):
        box, api, clock = make_box(_tree())
        api.diffs["w1"] = {
            "added": 0,
            "removed": 0,
            "content": "",
            "error": "git diff failed",
            "base": "fork",
        }
        with pytest.raises(ToolError, match="git diff failed"):
            box.get_diff({"title": "w1"}, _ctx(clock))


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
class TestSendMessage:
    def test_auto_to_any_local_session(self):
        box, api, clock = make_box(_tree())
        out = box.send_message({"to": "other", "text": "hello"}, _ctx(clock))
        assert out["sent"][0]["to"] == "other"
        assert out["sent"][0]["delivery"] == "pending"
        sent = api.mail["other"][0]
        assert sent["from"] == "orch" and sent["text"] == "hello"
        assert box.dispatched["other"] == clock.now

    def test_inbox_delivery_is_not_a_dispatch(self):
        box, api, clock = make_box(_tree())
        box.send_message({"to": "w1", "text": "fyi", "delivery": "inbox"}, _ctx(clock))
        assert api.mail["w1"][0]["state"] == "held"
        assert "w1" not in box.dispatched

    def test_now_needs_a_descendant(self):
        box, api, clock = make_box(_tree())
        with pytest.raises(ToolError, match="descendants"):
            box.send_message(
                {"to": "other", "text": "x", "delivery": "now"}, _ctx(clock)
            )
        out = box.send_message(
            {"to": "w1a", "text": "x", "delivery": "now"}, _ctx(clock)
        )
        assert out["sent"][0]["to"] == "w1a"

    def test_parent_children_and_lists(self):
        box, api, clock = make_box(_tree())
        box.send_message({"to": "parent", "text": "up"}, _ctx(clock))
        assert api.mail["root"][0]["text"] == "up"
        out = box.send_message({"to": "children", "text": "down"}, _ctx(clock))
        assert sorted(s["to"] for s in out["sent"]) == ["w1", "w2"]
        out = box.send_message({"to": ["w1", "w1", "other"], "text": "x"}, _ctx(clock))
        assert [s["to"] for s in out["sent"]] == ["w1", "other"]

    def test_list_serialized_as_a_string_is_accepted(self):
        box, api, clock = make_box(_tree())
        out = box.send_message({"to": '["w1", "w2"]', "text": "x"}, _ctx(clock))
        assert [s["to"] for s in out["sent"]] == ["w1", "w2"]

    def test_reply_to_is_forwarded(self):
        box, api, clock = make_box(_tree())
        box.send_message({"to": "w1", "text": "ok", "reply_to": "m9"}, _ctx(clock))
        assert api.calls[-1][2]["reply_to"] == "m9"

    def test_refusals(self):
        box, api, clock = make_box(_tree())
        with pytest.raises(ToolError, match="yourself"):
            box.send_message({"to": "orch", "text": "x"}, _ctx(clock))
        with pytest.raises(ToolError, match="another device"):
            box.send_message({"to": "dev::far", "text": "x"}, _ctx(clock))
        with pytest.raises(ToolError, match="no session named"):
            box.send_message({"to": ["w1", "ghost"], "text": "x"}, _ctx(clock))
        assert "w1" not in api.mail  # validated before sending to anyone

    def test_no_parent_or_children(self):
        box, _, clock = make_box(_tree(), me="root")
        with pytest.raises(ToolError, match="no parent"):
            box.send_message({"to": "parent", "text": "x"}, _ctx(clock))
        box2, _, _ = make_box(_tree(), me="other")
        with pytest.raises(ToolError, match="no live children"):
            box2.send_message({"to": "children", "text": "x"}, _ctx(clock))

    def test_external_sends_from_empty(self):
        box, api, clock = make_box(_tree(), me=None)
        box.send_message({"to": "w1", "text": "hi"}, _ctx(clock))
        assert api.mail["w1"][0]["from"] == ""

    def test_readonly_cannot_message(self):
        box, _, clock = make_box(_tree(), scope="readonly")
        with pytest.raises(ToolError, match="readonly"):
            box.send_message({"to": "w1", "text": "x"}, _ctx(clock))

    def test_partial_failure_is_reported(self):
        box, api, clock = make_box(_tree())
        api.errors[("POST", "/api/instances/w2/messages")] = client.ApiError(
            400, "rate limit"
        )
        out = box.send_message({"to": ["w1", "w2"], "text": "x"}, _ctx(clock))
        assert [s["to"] for s in out["sent"]] == ["w1"]
        assert out["failed"] == [{"to": "w2", "error": "rate limit"}]

    def test_total_failure_is_an_error(self):
        box, api, clock = make_box(_tree())
        api.errors[("POST", "/api/instances/w1/messages")] = client.ApiError(
            400, "too long"
        )
        with pytest.raises(ToolError, match="w1: too long"):
            box.send_message({"to": "w1", "text": "x"}, _ctx(clock))


class TestInbox:
    def test_check_inbox_marks_read_by_default(self):
        box, api, clock = make_box(_tree())
        api.deliver("orch", "w1", "q?")
        out = box.check_inbox({}, _ctx(clock))
        assert [m["text"] for m in out["messages"]] == ["q?"]
        assert api.mail["orch"][0]["state"] == "read"

    def test_check_inbox_peek_and_filters(self):
        box, api, clock = make_box(_tree())
        api.deliver("orch", "w1", "a")
        api.deliver("orch", "w2", "b")
        api.deliver("orch", "w2", "old", state="delivered")
        out = box.check_inbox({"mark_read": False, "from": "w2"}, _ctx(clock))
        assert [m["text"] for m in out["messages"]] == ["b"]
        assert api.mail["orch"][1]["state"] == "held"
        out = box.check_inbox({"include_consumed": True, "from": "w2"}, _ctx(clock))
        assert [m["text"] for m in out["messages"]] == ["b", "old"]

    def test_check_inbox_needs_identity(self):
        box, _, clock = make_box(_tree(), me=None)
        with pytest.raises(ToolError, match="session identity"):
            box.check_inbox({}, _ctx(clock))

    def test_readonly_may_read_its_own_inbox(self):
        box, api, clock = make_box(_tree(), scope="readonly")
        api.deliver("orch", "w1")
        assert box.check_inbox({}, _ctx(clock))["messages"]

    def test_wait_for_message_returns_and_consumes(self):
        clock = FakeClock()
        box, api, _ = make_box(_tree(), clock=clock)
        api.deliver("orch", "w1", "now")
        out = box.wait_for_message({"from": "w1", "kind": "message"}, _ctx(clock))
        assert [m["text"] for m in out["messages"]] == ["now"]
        assert api.mail["orch"][0]["state"] == "read"
        get = [p for p in api.paths("GET") if "/messages?" in p][-1]
        assert "wait=25" in get and "mark_read=0" in get and "from=w1" in get
        assert api.calls[-1] == (
            "POST",
            "/api/instances/orch/messages/read",
            {"ids": [api.mail["orch"][0]["id"]]},
        )

    def test_cancelled_long_poll_does_not_consume(self):
        clock = FakeClock()
        box, api, _ = make_box(_tree(), clock=clock)
        api.deliver("orch", "w1", "arrived as the client gave up")
        ctx = _ctx(clock)
        ctx.cancel_at_check = 2  # loop-top check passes; the post-poll one fires
        with pytest.raises(Cancelled):
            box.wait_for_message({}, ctx)
        assert any("wait=" in p for p in api.paths("GET"))  # the poll did run
        assert api.mail["orch"][0]["state"] == "held"
        assert "/api/instances/orch/messages/read" not in api.paths("POST")

    def test_wait_for_message_times_out_without_spinning(self):
        clock = FakeClock()
        box, api, _ = make_box(_tree(), clock=clock)
        ctx = _ctx(clock)
        out = box.wait_for_message({"timeout_s": 10}, ctx)
        assert out["timed_out"] is True
        # The fake answers ?wait= instantly; the loop must back off, not spin.
        assert ctx.slept and all(s > 0 for s in ctx.slept)
        assert ctx.progress_total == 10

    def test_wait_for_message_cancel(self):
        clock = FakeClock()
        box, _, _ = make_box(_tree(), clock=clock)
        ctx = _ctx(clock)
        ctx.cancel_after = 1
        with pytest.raises(Cancelled):
            box.wait_for_message({"timeout_s": 100}, ctx)


class TestReportResult:
    def test_posts_result_to_parent(self):
        box, api, clock = make_box(_tree(), me="w1")
        out = box.report_result(
            {"status": "done", "summary": "did it", "details": "tests: 3 passed"},
            _ctx(clock),
        )
        assert out["sent_to"] == "orch" and out["status"] == "done"
        msg = api.mail["orch"][0]
        assert msg["kind"] == "result" and msg["from"] == "w1"
        assert msg["text"] == "did it\n\nDetails:\ntests: 3 passed"
        assert msg["data"]["status"] == "done"
        assert msg["data"]["branch"] == "mindflock/w1"
        assert msg["data"]["head_sha"] == ""  # folder not inspectable here
        assert msg["data"]["diff_stat"] == {"files": 1, "additions": 2, "deletions": 0}

    def test_head_sha_from_local_git(self, tmp_path):
        repo = _git_repo(tmp_path / "w")
        rows = _tree()
        rows[2]["folder"] = str(repo)
        box, api, clock = make_box(rows, me="w1")
        box.report_result({"status": "blocked", "summary": "q?"}, _ctx(clock))
        assert api.mail["orch"][0]["data"]["head_sha"] == _git(
            repo, "rev-parse", "HEAD"
        )

    def test_no_parent(self):
        box, _, clock = make_box(_tree(), me="other")
        with pytest.raises(ToolError, match="no parent"):
            box.report_result({"status": "done", "summary": "x"}, _ctx(clock))


# --------------------------------------------------------------------------- #
# Steering
# --------------------------------------------------------------------------- #
class TestSteering:
    def test_answer_prompt(self):
        box, api, clock = make_box(_tree())
        out = box.answer_prompt({"title": "w1", "keys": ["2", "Enter"]}, _ctx(clock))
        assert out["ok"] is True
        assert api.calls[-1] == (
            "POST",
            "/api/instances/w1/answer",
            {"keys": ["2", "Enter"]},
        )
        assert "w1" in box.dispatched

    def test_answer_prompt_needs_input_and_management(self):
        box, api, clock = make_box(_tree())
        with pytest.raises(ToolError, match="text and/or keys"):
            box.answer_prompt({"title": "w1"}, _ctx(clock))
        with pytest.raises(ToolError, match="descendants"):
            box.answer_prompt({"title": "other", "text": "y"}, _ctx(clock))
        api.errors[("POST", "/api/instances/w1/answer")] = client.ApiError(
            409, "session is not waiting on a prompt (activity: working)"
        )
        with pytest.raises(ToolError, match="not waiting on a prompt"):
            box.answer_prompt({"title": "w1", "text": "y"}, _ctx(clock))

    def test_answer_prompt_pins_the_dialog_it_answers(self):
        """The keys carry the id of the dialog up now, so the server refuses
        them when the user's answer buttons already answered it."""
        box, api, clock = make_box(_tree())
        api.dialogs["w1"] = "abc123"
        box.answer_prompt({"title": "w1", "keys": ["1"]}, _ctx(clock))
        assert api.calls[-2][:2] == ("GET", "/api/instances/w1/dialog")
        assert api.calls[-1] == (
            "POST",
            "/api/instances/w1/answer",
            {"keys": ["1"], "dialog_id": "abc123"},
        )

    def test_answer_prompt_pins_the_dialog_the_agent_looked_at(self):
        """A screen read remembers the dialog it showed: an answer chosen from
        it must not land on the NEXT prompt someone else has exposed since."""
        box, api, clock = make_box(_tree())
        api.dialogs["w1"] = "seen1"
        api.outputs["w1"] = {"view": "screen", "text": "…", "activity": "clarify"}
        box.read_output({"title": "w1", "view": "screen"}, _ctx(clock))
        api.dialogs["w1"] = "next2"  # answered meanwhile; the next one is up
        box.answer_prompt({"title": "w1", "keys": ["2"]}, _ctx(clock))
        assert api.calls[-1][2] == {"keys": ["2"], "dialog_id": "seen1"}
        # Used once: the next answer pins what is up then.
        box.answer_prompt({"title": "w1", "keys": ["1"]}, _ctx(clock))
        assert api.calls[-1][2] == {"keys": ["1"], "dialog_id": "next2"}

    @pytest.mark.parametrize(
        "error,msg",
        [
            ("the prompt changed", "read_output"),
            ("that prompt was just answered", "wait_for_session"),
        ],
    )
    def test_answer_prompt_refusals_say_what_to_do(self, error, msg):
        box, api, clock = make_box(_tree())
        api.dialogs["w1"] = "abc123"
        api.errors[("POST", "/api/instances/w1/answer")] = client.ApiError(409, error)
        with pytest.raises(ToolError, match=msg):
            box.answer_prompt({"title": "w1", "keys": ["1"]}, _ctx(clock))

    def test_kill_close(self):
        box, api, clock = make_box(_tree())
        box.policy.spawned_by_me.add("w2")
        out = box.kill_session({"title": "w2"}, _ctx(clock))
        assert out["mode"] == "close" and "kept" in out["worktree"]
        assert api.calls[-1][:2] == ("POST", "/api/instances/w2/close")
        assert "w2" not in box.policy.spawned_by_me

    def test_kill_refusals(self):
        box, _, clock = make_box(_tree())
        with pytest.raises(ToolError, match="own session"):
            box.kill_session({"title": "orch"}, _ctx(clock))
        with pytest.raises(ToolError, match="descendants"):
            box.kill_session({"title": "root"}, _ctx(clock))

    def test_external_kills_only_what_it_spawned(self):
        box, api, clock = make_box(_tree(), me=None)
        with pytest.raises(ToolError, match="spawned"):
            box.kill_session({"title": "w1"}, _ctx(clock))
        box.policy.spawned_by_me.add("w1")
        box.kill_session({"title": "w1a"}, _ctx(clock))  # descendant of its spawn
        assert api.calls[-1][1] == "/api/instances/w1a/close"

    def test_set_parent(self):
        rows = _tree() + [row("orphan", spawned=True)]
        box, api, clock = make_box(rows)
        out = box.set_parent({"title": "orphan"}, _ctx(clock))
        assert out["parent"] == "orch"
        assert api.calls[-1] == (
            "POST",
            "/api/instances/orphan/parent",
            {"parent": "orch"},
        )
        box.set_parent({"title": "w2", "parent": ""}, _ctx(clock))
        assert api.calls[-1][2] == {"parent": ""}

    def test_set_parent_external_needs_parent_arg(self):
        box, _, clock = make_box(_tree(), me=None)
        with pytest.raises(ToolError, match="pass parent"):
            box.set_parent({"title": "w1"}, _ctx(clock))


# --------------------------------------------------------------------------- #
# Real-git helpers
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    cp = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return cp.stdout.strip()


def _git_repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _worktree(repo, path, branch, base="main"):
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
    return path


def _commit(path, name, text="x\n"):
    (path / name).write_text(text)
    _git(path, "add", name)
    _git(path, "commit", "-q", "-m", "add " + name)


@pytest.fixture()
def git_flock(tmp_path):
    """main repo + the orchestrator's worktree + a spawned worker's worktree."""
    repo = _git_repo(tmp_path / "repo")
    orch = _worktree(repo, tmp_path / "wt-orch", "mindflock/orch")
    _commit(orch, "orch.txt")
    worker = _worktree(repo, tmp_path / "wt-w1", "mindflock/w1", base="mindflock/orch")
    rows = [
        row("orch", folder=str(orch), path=str(repo), branch="mindflock/orch"),
        row(
            "w1",
            parent="orch",
            spawned=True,
            folder=str(worker),
            path=str(repo),
            branch="mindflock/w1",
        ),
    ]
    return repo, orch, worker, rows


class TestDiffCommits:
    def test_commits_not_in_my_head(self, git_flock):
        _, orch, worker, rows = git_flock
        _commit(worker, "feature.txt")
        box, api, clock = make_box(rows)
        out = box.get_diff({"title": "w1"}, _ctx(clock))
        assert len(out["commits"]) == 1 and "add feature.txt" in out["commits"][0]
        _git(orch, "merge", "-q", "--no-edit", "mindflock/w1")
        assert box.get_diff({"title": "w1"}, _ctx(clock))["commits"] == []

    def test_no_commits_field_for_my_own_diff(self, git_flock):
        _, _, _, rows = git_flock
        box, _, clock = make_box(rows)
        assert "commits" not in box.get_diff({"title": "orch"}, _ctx(clock))


class TestDeleteGuard:
    def test_clean_merged_worker_is_deleted(self, git_flock):
        repo, orch, worker, rows = git_flock
        box, api, clock = make_box(rows)
        out = box.kill_session({"title": "w1", "mode": "delete"}, _ctx(clock))
        assert out["worktree"] == "removed"
        assert api.calls[-1][:2] == ("DELETE", "/api/instances/w1")

    def test_unmerged_commits_refused_then_merged(self, git_flock):
        repo, orch, worker, rows = git_flock
        _commit(worker, "feature.txt")
        box, api, clock = make_box(rows)
        with pytest.raises(ToolError) as exc:
            box.kill_session({"title": "w1", "mode": "delete"}, _ctx(clock))
        assert "not in your HEAD" in exc.value.message
        assert len(exc.value.data["unmerged_commits"]) == 1
        assert "add feature.txt" in exc.value.data["unmerged_commits"][0]
        assert ("DELETE", "/api/instances/w1") not in [c[:2] for c in api.calls]
        _git(orch, "merge", "-q", "--no-edit", "mindflock/w1")
        box.kill_session({"title": "w1", "mode": "delete"}, _ctx(clock))
        assert api.calls[-1][:2] == ("DELETE", "/api/instances/w1")

    def test_dirty_worktree_refused(self, git_flock):
        _, _, worker, rows = git_flock
        (worker / "scratch.txt").write_text("wip\n")
        box, _, clock = make_box(rows)
        with pytest.raises(ToolError, match="uncommitted changes"):
            box.kill_session({"title": "w1", "mode": "delete"}, _ctx(clock))

    def test_force_overrides(self, git_flock):
        _, _, worker, rows = git_flock
        _commit(worker, "feature.txt")
        (worker / "scratch.txt").write_text("wip\n")
        box, api, clock = make_box(rows)
        box.kill_session({"title": "w1", "mode": "delete", "force": True}, _ctx(clock))
        assert api.calls[-1][:2] == ("DELETE", "/api/instances/w1")

    def test_paused_worker_branch_checked_in_canonical_repo(self, git_flock):
        repo, orch, worker, rows = git_flock
        _commit(worker, "feature.txt")
        _git(repo, "worktree", "remove", "--force", str(worker))  # as pause does
        box, _, clock = make_box(rows)
        with pytest.raises(ToolError) as exc:
            box.kill_session({"title": "w1", "mode": "delete"}, _ctx(clock))
        assert exc.value.data["unmerged_commits"]

    def test_not_inspectable_requires_force(self, git_flock):
        _, _, _, rows = git_flock
        rows[1]["folder"] = "/nonexistent/x"
        rows[1]["path"] = "/nonexistent/repo"
        box, _, clock = make_box(rows)
        with pytest.raises(ToolError, match="force=true"):
            box.kill_session({"title": "w1", "mode": "delete"}, _ctx(clock))

    def test_user_created_and_in_place_never_deleted(self, git_flock):
        _, _, _, rows = git_flock
        rows[1]["spawned"] = False
        box, _, clock = make_box(rows)
        with pytest.raises(ToolError, match="only for sessions an agent spawned"):
            box.kill_session(
                {"title": "w1", "mode": "delete", "force": True}, _ctx(clock)
            )
        rows[1]["spawned"] = True
        rows[1]["in_place"] = True
        with pytest.raises(ToolError, match="in place"):
            box.kill_session(
                {"title": "w1", "mode": "delete", "force": True}, _ctx(clock)
            )


# --------------------------------------------------------------------------- #
# spawn_session
# --------------------------------------------------------------------------- #
class TestSpawn:
    def test_defaults_fork_from_my_head(self, git_flock):
        repo, orch, _, rows = git_flock
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "do x"}, _ctx(clock))
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert payload["repo_path"] == str(repo)
        assert payload["base_ref"] == _git(orch, "rev-parse", "HEAD")
        assert payload["base_branch"] == "mindflock/orch"
        assert payload["parent"] == "orch" and payload["spawned"] is True
        assert payload["program"] == "claude"
        assert payload["title"] == "orch-w1"
        assert payload["prompt"].startswith("do x\n\n---\nYou are MindFlock worker")
        assert "mcp__mindflock__report_result" in payload["prompt"]
        assert out["title"] == "orch-w1" and out["base_sha"] == payload["base_ref"]
        assert out["ready"] is True and out["report_back"] is True
        assert "warnings" not in out
        assert "orch-w1" in box.policy.spawned_by_me
        assert box.dispatched["orch-w1"] == clock.now

    def test_dirty_worktree_warns(self, git_flock):
        _, orch, _, rows = git_flock
        (orch / "wip.txt").write_text("wip\n")
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "do x"}, _ctx(clock))
        assert "NOT visible" in out["warnings"][0]

    def test_title_validation(self):
        box, api, clock = make_box(_tree())
        with pytest.raises(ToolError, match="invalid title") as exc:
            box.spawn_session(
                {"prompt": "x", "title": "feature/thing one"}, _ctx(clock)
            )
        assert "feature-thing-one" in exc.value.message
        with pytest.raises(ToolError, match="already exists"):
            box.spawn_session({"prompt": "x", "title": "w1"}, _ctx(clock))
        # "a.b" maps to the same tmux name as an existing "a_b".
        rows = _tree() + [row("a_b")]
        box2, _, _ = make_box(rows)
        with pytest.raises(ToolError, match="terminal name"):
            box2.spawn_session({"prompt": "x", "title": "a.b"}, _ctx(clock))

    def test_explicit_options_and_no_footer_without_caps(self):
        box, api, clock = make_box(_tree())
        api.config_payload["caps"].pop("agent_mcp")
        api.created_status = "running"
        out = box.spawn_session(
            {
                "prompt": "p",
                "title": "job-1",
                "program": "codex",
                "account": "work",
                "model": "m1",
                "launch_args": ["--x"],
                "in_place": True,
            },
            _ctx(clock),
        )
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert payload["program"] == "codex"
        assert payload["profile_id"] == "work" and payload["profile_model"] == "m1"
        assert payload["extra_launch_args"] == ["--x"] and payload["in_place"] is True
        assert "launch_args" not in payload  # additive, never a replacement
        assert payload["prompt"] == "p"
        assert out["report_back"] is False and "does not attach" in out["reason"]

    def test_codex_footer_and_unsupported_provider(self):
        box, api, clock = make_box(_tree())
        api.created_status = "running"
        box.spawn_session({"prompt": "p", "program": "codex"}, _ctx(clock))
        payload = [c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")][-1]
        assert 'report_result tool of the "mindflock" MCP server' in payload["prompt"]
        out = box.spawn_session({"prompt": "p", "program": "aider"}, _ctx(clock))
        assert out["report_back"] is False and "aider" in out["reason"]

    def test_report_back_false(self):
        box, api, clock = make_box(_tree())
        api.created_status = "running"
        out = box.spawn_session({"prompt": "p", "report_back": False}, _ctx(clock))
        payload = [c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")][-1]
        assert payload["prompt"] == "p"
        assert out["reason"] == "report_back=false"

    def test_external_needs_repo_path_and_records_spawn(self, tmp_path):
        box, api, clock = make_box(_tree(), me=None)
        with pytest.raises(ToolError, match="needs repo_path"):
            box.spawn_session({"prompt": "p"}, _ctx(clock))
        api.created_status = "running"
        out = box.spawn_session(
            {"prompt": "p", "repo_path": str(tmp_path)}, _ctx(clock)
        )
        payload = [c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")][-1]
        assert "parent" not in payload and payload["repo_path"] == str(tmp_path)
        assert out["title"] == "worker-1"
        assert out["report_back"] is False  # nobody to report to
        assert box.policy.is_managed(box.flock(), "worker-1")

    def test_readonly_cannot_spawn(self):
        box, _, clock = make_box(_tree(), scope="readonly")
        with pytest.raises(ToolError, match="readonly"):
            box.spawn_session({"prompt": "p"}, _ctx(clock))

    def test_wait_ready_until_running(self):
        clock = FakeClock()
        box, api, _ = make_box(_tree(), clock=clock)
        polls = {"n": 0}

        def flip(now):
            polls["n"] += 1
            if polls["n"] == 3:
                api.row("orch-w1")["status"] = "running"

        ctx = _ctx(clock, on_sleep=flip)
        out = box.spawn_session({"prompt": "p"}, ctx)
        assert out["ready"] is True and out["status"] == "running"
        assert ctx.progress_total == mcp_tools.READY_WAIT_S

    def test_wait_ready_timeout_is_not_an_error(self):
        clock = FakeClock()
        box, api, _ = make_box(_tree(), clock=clock)
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert out["ready"] is False and out["status"] == "loading"
        assert "still starting" in out["hint"]

    def test_vanished_while_loading_is_an_error(self):
        clock = FakeClock()
        box, api, _ = make_box(_tree(), clock=clock)

        def drop(now):
            api.rows[:] = [r for r in api.rows if r["title"] != "orch-w1"]

        with pytest.raises(ToolError, match="failed to start"):
            box.spawn_session({"prompt": "p"}, _ctx(clock, on_sleep=drop))
        assert "orch-w1" not in box.policy.spawned_by_me

    def test_no_wait_ready(self):
        box, api, clock = make_box(_tree())
        out = box.spawn_session({"prompt": "p", "wait_ready": False}, _ctx(clock))
        assert out["ready"] is False
        assert api.paths("GET").count("/api/instances") == 1  # no readiness polling

    def test_default_title_race_retries_next_number(self):
        box, api, clock = make_box(_tree())
        api.created_status = "running"
        original = api._create

        def racing(payload):
            if payload["title"] == "orch-w1":
                api.rows.append(row("orch-w1"))  # someone else took it first
            return original(payload)

        api._create = racing
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert out["title"] == "orch-w2"
        payload = [c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")][-1]
        assert '"orch-w2"' in payload["prompt"]  # footer names the final title

    def test_base_ref_rejected_falls_back(self, git_flock):
        _, _, _, rows = git_flock
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create

        def picky(payload):
            if "base_ref" in payload:
                raise client.ApiError(
                    400, "base_ref is not supported for provisioned sessions"
                )
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        assert out["base_sha"] is None
        assert "could not fork from your HEAD" in out["warnings"][0]

    def test_create_error_surfaces(self):
        box, api, clock = make_box(_tree())
        api.errors[("POST", "/api/instances")] = client.ApiError(
            409, "orch already has 8 live children (MINDFLOCK_MAX_CHILDREN)"
        )
        with pytest.raises(ToolError, match="MINDFLOCK_MAX_CHILDREN"):
            box.spawn_session({"prompt": "p"}, _ctx(clock))


# --------------------------------------------------------------------------- #
# wait_for_session
# --------------------------------------------------------------------------- #
def _wait_box(rows, **kw):
    clock = FakeClock()
    box, api, _ = make_box(rows, clock=clock, **kw)
    box.wait_poll_s = 3.0
    return box, api, clock


class TestWaitForSession:
    def test_already_finished_returns_immediately(self):
        rows = _tree()
        box, api, clock = _wait_box(rows)
        ctx = _ctx(clock)
        out = box.wait_for_session({"titles": ["w1"]}, ctx)
        assert out["sessions"]["w1"]["reason"] == "idle"
        assert out["sessions"]["w1"]["last_reply"] == "reply of w1"
        assert out["still_running"] == []
        assert ctx.slept == []
        assert ctx.progress_total == 600

    def test_reported_wins_and_is_consumed(self):
        box, api, clock = _wait_box(_tree())
        box.dispatched["w1"] = clock.now - 100
        api.row("w1")["activity"] = "working"
        msg = api.deliver(
            "orch",
            "w1",
            "done!",
            kind="result",
            state="pending",
            data={"status": "done"},
        )
        out = box.wait_for_session({"title": "w1"}, _ctx(clock))
        res = out["sessions"]["w1"]
        assert res["reason"] == "reported"
        assert res["report"]["text"] == "done!"
        assert res["report"]["data"] == {"status": "done"}
        assert msg["state"] == "read"  # never typed in afterwards

    def test_report_older_than_last_dispatch_is_ignored(self):
        box, api, clock = _wait_box(_tree())
        api.row("w1")["activity"] = "working"
        api.deliver("orch", "w1", "old report", kind="result", ts=clock.now - 50)
        box.dispatched["w1"] = clock.now - 10
        out = box.wait_for_session({"title": "w1", "timeout_s": 6}, _ctx(clock))
        assert out["timed_out"] is True and out["still_running"] == ["w1"]

    def test_message_from_me_in_its_mailbox_counts_as_dispatch(self):
        box, api, clock = _wait_box(_tree())
        api.row("w1")["activity"] = "working"
        api.deliver("orch", "w1", "old report", kind="result", ts=clock.now - 50)
        api.deliver("w1", "orch", "follow-up", ts=clock.now - 20, state="delivered")
        out = box.wait_for_session({"title": "w1", "timeout_s": 6}, _ctx(clock))
        assert out["timed_out"] is True

    def test_placeholder_idle_is_not_done(self):
        rows = _tree()
        rows[2].update(activity="idle", activity_since=0)
        box, _, clock = _wait_box(rows)
        out = box.wait_for_session({"title": "w1", "timeout_s": 10}, _ctx(clock))
        assert out["timed_out"] is True

    def test_queued_prompt_blocks_idle(self):
        rows = _tree()
        rows[2]["queue"] = {"pending": 1}
        box, _, clock = _wait_box(rows)
        out = box.wait_for_session({"title": "w1", "timeout_s": 10}, _ctx(clock))
        assert out["timed_out"] is True

    def test_pending_message_to_it_blocks_idle(self):
        box, api, clock = _wait_box(_tree())
        api.deliver("w1", "orch", "please also do y", state="pending")
        out = box.wait_for_session({"title": "w1", "timeout_s": 10}, _ctx(clock))
        assert out["timed_out"] is True

    def test_recent_dispatch_holds_an_unstarted_idle(self):
        box, api, clock = _wait_box(_tree())
        box.dispatched["w1"] = clock.now
        ctx = _ctx(clock)
        out = box.wait_for_session({"title": "w1"}, ctx)
        assert out["sessions"]["w1"]["reason"] == "idle"
        assert sum(ctx.slept) >= mcp_tools.DISPATCH_QUIET_S

    def test_seen_working_then_settles(self):
        rows = _tree()
        rows[2]["activity"] = "working"
        box, api, clock = _wait_box(rows)
        start = clock.now

        def finish(now):
            if now - start >= 9 and api.row("w1")["activity"] == "working":
                api.row("w1").update(activity="idle", activity_since=now)

        ctx = _ctx(clock, on_sleep=finish)
        out = box.wait_for_session({"title": "w1"}, ctx)
        assert out["sessions"]["w1"]["reason"] == "idle"
        # went idle at ~t+9 and needs ~8s of settle for claude
        assert clock.now - start >= 9 + 8 - box.wait_poll_s

    def test_settle_is_longer_for_non_claude(self):
        rows = _tree()
        rows[2].update(activity="working", provider="codex", program="codex")
        box, api, clock = _wait_box(rows)
        start = clock.now

        def finish(now):
            if now - start >= 3 and api.row("w1")["activity"] == "working":
                api.row("w1").update(activity="idle", activity_since=now)

        box.wait_for_session({"title": "w1"}, _ctx(clock, on_sleep=finish))
        assert clock.now - start >= 3 + 30 - box.wait_poll_s

    @pytest.mark.parametrize(
        "change,reason",
        [
            ({"activity": "clarify"}, "needs_input"),
            ({"activity": "limit"}, "usage_limit"),
            ({"status": "paused", "activity": "offline"}, "paused"),
        ],
    )
    def test_terminal_states(self, change, reason):
        rows = _tree()
        rows[2].update(change)
        box, _, clock = _wait_box(rows)
        out = box.wait_for_session({"title": "w1"}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == reason

    def test_gone(self):
        box, api, clock = _wait_box(_tree())
        api.row("w1")["activity"] = "working"

        def vanish(now):
            api.rows[:] = [r for r in api.rows if r["title"] != "w1"]

        out = box.wait_for_session({"title": "w1"}, _ctx(clock, on_sleep=vanish))
        assert out["sessions"]["w1"]["reason"] == "gone"
        assert "last_reply" not in out["sessions"]["w1"]

    def test_offline_must_persist(self):
        rows = _tree()
        rows[2]["activity"] = "offline"
        box, _, clock = _wait_box(rows)
        start = clock.now
        out = box.wait_for_session({"title": "w1"}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == "offline"
        assert clock.now - start >= mcp_tools.OFFLINE_DONE_S

    def test_offline_while_loading_is_not_done(self):
        rows = _tree()
        rows[2].update(activity="offline", status="loading")
        box, _, clock = _wait_box(rows)
        out = box.wait_for_session({"title": "w1", "timeout_s": 60}, _ctx(clock))
        assert out["timed_out"] is True

    def test_message_for_me_ends_the_wait(self):
        rows = _tree()
        rows[2]["activity"] = "working"
        box, api, clock = _wait_box(rows)
        api.deliver("orch", "other", "stale", ts=clock.now - 100)  # predates the call

        def ask(now):
            if not any(m["text"] == "question?" for m in api.mail["orch"]):
                api.deliver("orch", "w1", "question?", state="pending")

        out = box.wait_for_session({"title": "w1"}, _ctx(clock, on_sleep=ask))
        assert out["reason"] == "message"
        assert [m["text"] for m in out["messages"]] == ["question?"]
        assert out["still_running"] == ["w1"]
        q = next(m for m in api.mail["orch"] if m["text"] == "question?")
        assert q["state"] == "read"

    def test_return_on_message_false_keeps_waiting(self):
        rows = _tree()
        rows[2]["activity"] = "working"
        box, api, clock = _wait_box(rows)

        def ask(now):
            if len(api.mail.get("orch", [])) < 1:
                api.deliver("orch", "other", "hey", state="pending")

        out = box.wait_for_session(
            {"title": "w1", "timeout_s": 9, "return_on_message": False},
            _ctx(clock, on_sleep=ask),
        )
        assert out["timed_out"] is True

    def test_mode_any_and_all(self):
        rows = _tree()
        rows[4]["activity"] = "working"  # w2 still busy, w1 finished
        box, _, clock = _wait_box(rows)
        out = box.wait_for_session({"titles": ["w1", "w2"], "mode": "any"}, _ctx(clock))
        assert list(out["sessions"]) == ["w1"] and out["still_running"] == ["w2"]
        out = box.wait_for_session(
            {"titles": ["w1", "w2"], "timeout_s": 7}, _ctx(clock)
        )
        assert out["timed_out"] is True and out["still_running"] == ["w2"]
        assert "w1" in out["sessions"]
        assert "call wait_for_session again" in out["hint"]

    def test_unknown_title_and_no_titles(self):
        box, _, clock = _wait_box(_tree())
        with pytest.raises(ToolError, match="no session named 'ghost'"):
            box.wait_for_session({"titles": ["w1", "ghost"]}, _ctx(clock))
        with pytest.raises(ToolError, match="pass titles"):
            box.wait_for_session({}, _ctx(clock))

    def test_settle_override(self):
        rows = _tree()
        rows[2]["activity_since"] = 10_000.0 - 5
        box, _, clock = _wait_box(rows)
        out = box.wait_for_session({"title": "w1", "settle_s": 2}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == "idle"

    def test_cancel_stops_the_wait(self):
        rows = _tree()
        rows[2]["activity"] = "working"
        box, _, clock = _wait_box(rows)
        ctx = _ctx(clock)
        ctx.cancel_after = 2
        with pytest.raises(Cancelled):
            box.wait_for_session({"title": "w1"}, ctx)

    def test_external_waits_without_an_inbox(self):
        box, api, clock = _wait_box(_tree(), me=None)
        out = box.wait_for_session({"title": "w1"}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == "idle"
        assert not any("/messages" in p and "orch" in p for p in api.paths())


# --------------------------------------------------------------------------- #
# Review fixes                                                                 #
# --------------------------------------------------------------------------- #
class TestSpawnReviewFixes:
    def test_default_title_skips_a_closed_workers_branch(self, git_flock):
        """kill_session(close) keeps orch-w1's branch; the next default spawn
        used to pick orch-w1 again and fail in the background, every time."""
        repo, _, _, rows = git_flock
        _git(repo, "branch", "mindflock/orch-w1", "main")
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "two"}, _ctx(clock))
        assert out["title"] == "orch-w2"

    def test_a_409_branch_exists_moves_to_the_next_title(self, git_flock):
        _, _, _, rows = git_flock
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create

        def taken(payload):
            if payload["title"] == "orch-w1":
                raise client.ApiError(
                    409, "a branch named mindflock/orch-w1 already exists"
                )
            return original(payload)

        api._create = taken
        assert box.spawn_session({"prompt": "p"}, _ctx(clock))["title"] == "orch-w2"

    def test_a_vanished_spawn_names_the_servers_reason(self):
        box, api, clock = make_box(_tree())

        def fail(payload):
            api.rows[:] = [r for r in api.rows if r["title"] != payload["title"]]
            api.create_failures[payload["title"]] = (
                "failed to setup git worktree: branch x already exists"
            )

        api.on_create = fail
        with pytest.raises(ToolError, match="branch x already exists"):
            box.spawn_session({"prompt": "p"}, _ctx(clock))

    def test_provisioned_parent_shares_its_source_repo_without_base_ref(self, tmp_path):
        """canonical_repo_root() of a provisioned worktree is the _base_<slug>
        clone; spawning from it made a SECOND base clone the orchestrator
        could not merge from, and base_ref was always 400'd first."""
        src = _git_repo(tmp_path / "myrepo")
        base = _git_repo(tmp_path / "ws" / "_base_myrepo")
        wt = _worktree(base, tmp_path / "ws" / "orch", "mindflock/orch")
        rows = [
            row(
                "orch",
                folder=str(wt),
                path=str(src),
                provisioned=True,
                workspace_strategy="worktree",
            )
        ]
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert "base_ref" not in payload and "base_branch" not in payload
        assert payload["repo_path"] == str(src)
        assert payload["provisioned"] is True
        assert len([c for c in api.calls if c[:2] == ("POST", "/api/instances")]) == 1
        assert "base branch" in out["warnings"][0]

    def test_config_driven_provisioned_parent_sends_no_repo_path(self, tmp_path):
        base = _git_repo(tmp_path / "ws" / "_base_myrepo")
        wt = _worktree(base, tmp_path / "ws" / "orch", "mindflock/orch")
        for path in (".", str(base)):
            rows = [row("orch", folder=str(wt), path=path, provisioned=True)]
            box, api, clock = make_box(rows)
            api.created_status = "running"
            box.spawn_session({"prompt": "p"}, _ctx(clock))
            payload = next(
                c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")
            )
            assert "repo_path" not in payload and "base_ref" not in payload

    def test_provisioned_parent_skips_a_closed_workers_kept_worktree(self, tmp_path):
        """A closed provisioned worker keeps its worktree, holding
        ``mindflock/orch-w1`` in the shared _base_ clone: the next default spawn
        picked orch-w1 again and its Start failed, every time."""
        base = _git_repo(tmp_path / "ws" / "_base_myrepo")
        wt = _worktree(base, tmp_path / "ws" / "orch", "mindflock/orch")
        _worktree(base, tmp_path / "ws" / "orch-w1", "mindflock/orch-w1")
        rows = [
            row(
                "orch",
                folder=str(wt),
                path=".",
                provisioned=True,
                workspace_strategy="worktree",
            )
        ]
        box, api, clock = make_box(rows)
        api.created_status = "running"
        assert box.spawn_session({"prompt": "p"}, _ctx(clock))["title"] == "orch-w2"

    def test_provisioned_clone_parent_skips_a_closed_workers_clone(self, tmp_path):
        """Clone strategy: the path is deterministic and Setup is idempotent,
        so reusing the title silently ADOPTED the closed worker's clone."""
        ws = tmp_path / "ws"
        orch = _git_repo(ws / "mindflock-orch")
        _git_repo(ws / "mindflock-orch-w1")  # the closed worker's kept clone
        rows = [
            row(
                "orch",
                folder=str(orch),
                path=".",
                provisioned=True,
                workspace_strategy="clone",
            )
        ]
        box, api, clock = make_box(rows)
        api.created_status = "running"
        assert box.spawn_session({"prompt": "p"}, _ctx(clock))["title"] == "orch-w2"

    def test_provisioned_branch_mirrors_the_engines_naming(self):
        from backend.mcp import tools as T
        from backend.session import provisioned

        for t in ("orch-w1", "Orch W1", "a" * 60, "", "x/y.z"):
            assert T._provisioned_branch(t) == provisioned.branch_name_for(None, t)

    def test_a_string_launch_args_is_refused_not_wrapped(self):
        """``launch_args: "--model opus"`` wrapped into ONE token launched
        ``claude '--model opus'``, which the CLI refuses at start."""
        from backend.mcp import tools as T
        from backend.mcp.protocol import validate

        with pytest.raises(SchemaError, match="array"):
            validate({"prompt": "p", "launch_args": "--model opus"}, T._S_SPAWN)
        out = validate(
            {"prompt": "p", "launch_args": '["--model", "opus"]'}, T._S_SPAWN
        )
        assert out["launch_args"] == ["--model", "opus"]
        # The private hint never reaches a client's view of the schema.
        box, _, _ = make_box(_tree())
        spawn = next(t for t in build_tools(box) if t.name == "spawn_session")
        assert "x-wrap-scalar" not in json.dumps(spawn.definition("2025-06-18"))
        assert "x-wrap-scalar" in json.dumps(T._S_SPAWN)  # still validated with

    def test_bare_repo_worktrees_still_fork_from_my_head(self, tmp_path):
        """No canonical root (bare + worktrees): the worker used to start from
        the main checkout's HEAD with no base_ref and no warning."""
        seed = _git_repo(tmp_path / "seed")
        bare = tmp_path / "repo.git"
        _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
        main = tmp_path / "main"
        _git(bare, "worktree", "add", "-q", str(main), "main")
        orch = _worktree(bare, tmp_path / "orch", "mindflock/orch")
        _commit(orch, "plan.txt")
        rows = [row("orch", folder=str(orch), path=str(main))]
        box, api, clock = make_box(rows)
        api.created_status = "running"
        out = box.spawn_session({"prompt": "p"}, _ctx(clock))
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert payload["repo_path"] == str(main)
        assert payload["base_ref"] == _git(orch, "rev-parse", "HEAD")
        assert out["base_sha"] == payload["base_ref"]

    def test_a_queued_prompt_is_reported(self):
        box, api, clock = make_box(_tree())
        api.created_status = "running"
        original = api._create

        def queued(payload):
            out = original(payload)
            out["prompt_delivery"] = "queued"
            return out

        api._create = queued
        out = box.spawn_session({"prompt": "p", "program": "aider"}, _ctx(clock))
        assert out["prompt_delivery"] == "queued"
        assert any("queued" in w for w in out["warnings"])

    def test_texts_do_not_hardcode_the_limits(self):
        from backend.mcp import tools as tools_mod

        assert "8 live children" not in tools_mod.INSTRUCTIONS
        assert "8 live children" not in tools_mod._D_SPAWN
        assert "ADDED" in tools_mod._D_SPAWN


class TestWaitReviewFixes:
    def test_a_deleted_namesakes_report_never_satisfies_a_fresh_process(self):
        """Titles are reused; a fresh MCP process (empty `dispatched`) used to
        return the deleted orch-w1's old report for the new orch-w1."""
        rows = _tree()
        box, api, clock = _wait_box(rows)
        api.deliver(
            "orch", "w1", "OLD WORK", kind="result", state="read", ts=clock.now - 500
        )
        api.row("w1").update(
            created_at=clock.now - 10, activity="working", activity_since=clock.now
        )
        out = box.wait_for_session({"titles": ["w1"], "timeout_s": 9}, _ctx(clock))
        assert out.get("timed_out") is True
        assert "w1" not in out["sessions"]

    def test_an_inbox_note_does_not_hide_an_earlier_report(self):
        box, api, clock = _wait_box(_tree())
        api.row("w1")["activity"] = "working"
        api.deliver(
            "orch",
            "w1",
            "done",
            kind="result",
            state="held",
            ts=clock.now - 5,
            data={"status": "done"},
        )
        box.send_message(
            {"to": "w1", "text": "FYI merging later", "delivery": "inbox"},
            _ctx(clock),
        )
        box.dispatched.clear()  # a fresh process: only the mailbox remembers
        out = box.wait_for_session({"titles": ["w1"], "timeout_s": 9}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == "reported"

    def test_a_clarify_from_before_my_answer_is_not_needs_input(self):
        box, api, clock = _wait_box(_tree())
        api.row("w1").update(activity="clarify", activity_since=clock.now - 30)
        box.answer_prompt({"title": "w1", "keys": ["1"]}, _ctx(clock))
        ctx = _ctx(clock)

        def tick(now):
            # The next tick shows the worker running again after the answer.
            api.row("w1").update(activity="working", activity_since=now)

        ctx.on_sleep = tick
        out = box.wait_for_session({"titles": ["w1"], "timeout_s": 6}, ctx)
        assert "w1" not in out["sessions"]  # never a stale needs_input
        # A NEW prompt raised after the answer is still reported.
        api.row("w1").update(activity="clarify", activity_since=clock.now)
        out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == "needs_input"

    def test_reported_diff_stat_comes_from_the_report(self):
        box, api, clock = _wait_box(_tree())
        box.dispatched["w1"] = clock.now - 100
        fresh = {"files": 3, "additions": 9, "deletions": 1}
        api.deliver(
            "orch",
            "w1",
            "done",
            kind="result",
            state="pending",
            data={"status": "done", "diff_stat": fresh},
        )
        out = box.wait_for_session({"titles": ["w1"]}, _ctx(clock))
        assert out["sessions"]["w1"]["reason"] == "reported"
        assert out["sessions"]["w1"]["diff_stat"] == fresh


class TestSplitDiffPaths:
    """Real git output: a path with a space gets a trailing TAB on ---/+++,
    non-ASCII is C-quoted, and renames name the new path — each used to
    break get_diff(files=[...])."""

    def _diff(self, tmp_path, *config):
        repo = _git_repo(tmp_path / "r")
        (repo / "old name.txt").write_text("a\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "seed")
        (repo / "docs").mkdir()
        (repo / "docs" / "user guide.md").write_text("hi\n")
        (repo / "café.txt").write_text("x\n")
        (repo / "plain.txt").write_text("p\n")
        _git(repo, "mv", "old name.txt", "new name.txt")
        _git(repo, "add", ".")
        return _git(repo, *config, "--no-pager", "diff", "--cached", "-M", "HEAD")

    def test_default_git_output(self, tmp_path):
        files = split_diff(self._diff(tmp_path) + "\n")
        got = {f["path"]: f["status"] for f in files}
        assert got == {
            "café.txt": "added",
            "docs/user guide.md": "added",
            "new name.txt": "renamed",
            "plain.txt": "added",
        }

    def test_the_servers_stable_flags_survive_a_hostile_gitconfig(self, tmp_path):
        from backend.web import server

        hostile = ("-c", "diff.noprefix=true", "-c", "core.quotePath=true")
        self._diff(tmp_path)  # builds the repo + staged changes
        repo = tmp_path / "r"
        # Unpinned, the hostile config leaves nothing to parse a path from.
        bare = _git(repo, *hostile, "--no-pager", "diff", "--cached", "-M", "HEAD")
        assert "docs/user guide.md" not in [f["path"] for f in split_diff(bare)]
        # The route's argv order: config before `diff`, flags after it.
        out = _git(
            repo,
            *hostile,
            *server._DIFF_STABLE_CONFIG,
            "--no-pager",
            "diff",
            *server._DIFF_STABLE_FLAGS,
            "--cached",
            "-M",
            "HEAD",
        )
        paths = sorted(f["path"] for f in split_diff(out + "\n"))
        assert paths == ["café.txt", "docs/user guide.md", "new name.txt", "plain.txt"]
