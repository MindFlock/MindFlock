"""A MindFlock fault in a tool call nudges the agent to OFFER report_issue.

The hint (protocol.FAULT_HINT) goes on a crash or a server-fault 5xx, at most
once per server process per (tool, error kind); never on an ordinary refusal,
on report_issue's own failure, or where report_issue isn't served. The
report_issue description and the server instructions say "offer first, draft
on yes" (docs/mcp.md ``report_issue``).
"""

from __future__ import annotations

import pytest

from backend import client
from backend.mcp import feedback
from backend.mcp.api import Api
from backend.mcp.protocol import FAULT_HINT, Tool, ToolError
from backend.mcp.tools import INSTRUCTIONS
from tests.unit.test_mcp_api import _Clock
from tests.unit.test_mcp_protocol import Harness


def _crash(args, ctx):
    raise RuntimeError("kaput")


def _refuse(args, ctx):
    raise ToolError("session 'x' not found")


def _api_error(status):
    """A tool that translates an ApiError the way most tools do."""

    def handler(args, ctx):
        err = client.ApiError(status, "boom %d" % status)
        if status >= 500 and status not in (502, 503, 504):
            err.fault = True  # what Api.request does on the way out
        try:
            raise err
        except client.ApiError as e:
            raise ToolError("could not do it: %s" % e.message) from None

    return handler


def _faulty(args, ctx):
    raise ToolError("the server lost the session", fault=True)


def _tool(name, handler):
    return Tool(name, name, name, {"type": "object"}, handler, {})


def _harness(*tools, report_issue=True):
    extra = [_tool("report_issue", _crash)] if report_issue else []
    h = Harness([*tools, *extra])
    h.init()
    return h


def _text(h, name):
    result = h.call("tools/call", {"name": name, "arguments": {}})["result"]
    assert result["isError"] is True
    return result["content"][0]["text"]


@pytest.fixture()
def h():
    harness = _harness(
        _tool("crash", _crash),
        _tool("refuse", _refuse),
        _tool("e500", _api_error(500)),
        _tool("e404", _api_error(404)),
        _tool("e503", _api_error(503)),
        _tool("faulty", _faulty),
    )
    yield harness
    harness.close()


class TestFaultHint:
    def test_a_crash_gets_the_hint(self, h):
        text = _text(h, "crash")
        assert "internal error in crash: RuntimeError: kaput" in text
        assert text.endswith("\n" + FAULT_HINT)

    def test_a_plain_refusal_does_not(self, h):
        text = _text(h, "refuse")
        assert text == "session 'x' not found"

    def test_a_server_500_does(self, h):
        text = _text(h, "e500")
        assert text.startswith("could not do it: boom 500")
        assert text.endswith(FAULT_HINT)

    def test_a_4xx_and_an_upstream_5xx_do_not(self, h):
        assert FAULT_HINT not in _text(h, "e404")
        assert FAULT_HINT not in _text(h, "e503")

    def test_an_explicit_fault_does(self, h):
        assert _text(h, "faulty").endswith(FAULT_HINT)

    def test_once_per_tool_and_kind(self, h):
        assert FAULT_HINT in _text(h, "crash")
        again = _text(h, "crash")
        assert FAULT_HINT not in again
        assert "RuntimeError: kaput" in again  # the error itself is unchanged
        # another tool's fault is its own key
        assert FAULT_HINT in _text(h, "e500")
        assert FAULT_HINT not in _text(h, "e500")

    def test_report_issue_s_own_failure_never_gets_it(self, h):
        text = _text(h, "report_issue")
        assert "internal error in report_issue" in text
        assert FAULT_HINT not in text

    def test_not_where_report_issue_is_not_served(self):
        h = _harness(_tool("crash", _crash), report_issue=False)
        try:
            assert FAULT_HINT not in _text(h, "crash")
        finally:
            h.close()

    def test_the_hint_offers_and_waits_for_a_yes(self):
        assert "report_issue" in FAULT_HINT
        assert "offer" in FAULT_HINT and "only if they agree" in FAULT_HINT


class TestApiMarksServerFaults:
    @pytest.fixture()
    def api(self, monkeypatch):
        script = []

        def get(base, path, *a, **kw):
            raise script.pop(0)

        monkeypatch.setattr(client, "discover", lambda *a, **kw: "http://x:1")
        monkeypatch.setattr(client, "get", get)
        clock = _Clock()
        return Api(env={}, sleep=clock.sleep, clock=clock.mono), script

    @pytest.mark.parametrize(
        "status, fault",
        [
            (500, True),
            (501, True),
            (502, False),
            (503, False),
            (504, False),
            (404, False),
            (409, False),
        ],
    )
    def test_only_a_mindflock_5xx_is_a_fault(self, api, status, fault):
        a, script = api
        script.append(client.ApiError(status, "x"))
        with pytest.raises(client.ApiError) as exc:
            a.get("/p")
        assert getattr(exc.value, "fault", False) is fault


class TestOfferFirstText:
    def test_instructions_say_offer_then_draft(self):
        assert len(INSTRUCTIONS) <= 2000  # Claude truncates at 2048
        assert "Offer report_issue once; draft on yes." in INSTRUCTIONS

    def test_the_description_names_the_triggers_and_the_rule(self):
        d = feedback.D_REPORT_ISSUE
        assert len(d) <= 1500
        for phrase in (
            "differently from what its docs or messages say",
            "internal or server error",
            "this is broken",
            "work around a MindFlock limitation",
            "idea for MindFlock",
            "Offer once per problem, in one line",
            "only after the user agrees",
            "Not for problems in the user's own project or in the agent CLI",
            "PUBLIC GitHub issue",
            "nothing is posted by you",
        ):
            assert phrase in d, phrase
