"""MCP stdio protocol layer (backend.mcp.protocol), driven over real OS pipes.

The server loop runs on a thread reading one pipe and writing another, exactly
as it would on a process's stdin/stdout; a reader thread collects every line
it writes so tests can assert on order (progress before result, nothing after
a cancelled call) and on silence (no response at all).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time

import pytest

from backend.mcp import protocol
from backend.mcp.protocol import (
    LATEST_VERSION,
    SUPPORTED_VERSIONS,
    McpServer,
    SchemaError,
    Tool,
    ToolError,
    validate,
)


class Harness:
    """An McpServer on a background thread, wired to two OS pipes."""

    def __init__(self, tools, **kw):
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self._to_server = os.fdopen(w_in, "wb", buffering=0)
        self._from_server = os.fdopen(r_out, "rb")
        self.eof = threading.Event()
        self.server = McpServer(
            tools,
            os.fdopen(r_in, "rb"),
            os.fdopen(w_out, "wb"),
            name="mindflock",
            version="9.9.9",
            instructions="be nice",
            on_eof=self.eof.set,
            **kw,
        )
        self.lines: "queue.Queue[dict]" = queue.Queue()
        threading.Thread(target=self.server.run, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        self._next_id = 0

    def _read(self):
        for line in self._from_server:
            self.lines.put(json.loads(line.decode("utf-8")))

    def send(self, obj):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        self._to_server.write(raw + b"\n")

    def recv(self, timeout=3.0):
        return self.lines.get(timeout=timeout)

    def nothing_for(self, seconds):
        try:
            got = self.lines.get(timeout=seconds)
        except queue.Empty:
            return True
        raise AssertionError("unexpected message: %r" % (got,))

    def request(self, method, params=None, msg_id=None, meta=None):
        if msg_id is None:
            self._next_id += 1
            msg_id = self._next_id
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        return msg_id

    def call(self, method, params=None, timeout=3.0):
        msg_id = self.request(method, params)
        resp = self.recv(timeout)
        assert resp.get("id") == msg_id, resp
        return resp

    def init(self, version=LATEST_VERSION):
        resp = self.call("initialize", {"protocolVersion": version, "capabilities": {}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def close(self):
        try:
            self._to_server.close()
        except OSError:
            pass


def _echo(args, ctx):
    return {"echo": args}


def _boom(args, ctx):
    raise ToolError("nope", {"unmerged_commits": ["abc fix"]})


def _crash(args, ctx):
    raise RuntimeError("kaput")


def _text(args, ctx):
    return "plain words"


_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 10},
        "who": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["n"],
    "additionalProperties": False,
}


def _tools(*extra):
    return [
        Tool("echo", "Echo", "echoes", _SCHEMA, _echo, {"readOnlyHint": True}),
        Tool("boom", "Boom", "fails", {"type": "object"}, _boom, {}),
        Tool("crash", "Crash", "crashes", {"type": "object"}, _crash, {}),
        Tool("text", "Text", "prose", {"type": "object"}, _text, {}),
        *extra,
    ]


@pytest.fixture()
def h():
    harness = Harness(_tools())
    yield harness
    harness.close()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
class TestInitialize:
    @pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
    def test_supported_version_is_echoed(self, version):
        h = Harness(_tools())
        try:
            resp = h.init(version)
            result = resp["result"]
            assert result["protocolVersion"] == version
            assert result["serverInfo"]["name"] == "mindflock"
            assert result["serverInfo"]["version"] == "9.9.9"
            assert result["capabilities"] == {"tools": {"listChanged": False}}
            assert result["instructions"] == "be nice"
        finally:
            h.close()

    def test_unknown_version_gets_latest(self, h):
        resp = h.init("1999-01-01")
        assert resp["result"]["protocolVersion"] == LATEST_VERSION

    def test_missing_version_gets_latest(self, h):
        resp = h.call("initialize", {})
        assert resp["result"]["protocolVersion"] == LATEST_VERSION

    def test_server_discover_before_initialize_is_an_immediate_error(self, h):
        # The 2026-07-28 dual-era probe: silence would stall every session
        # start for the probe timeout, so it must be answered at once.
        start = time.monotonic()
        resp = h.call("server/discover", {}, timeout=1.0)
        assert time.monotonic() - start < 1.0
        assert resp["error"]["code"] == protocol.METHOD_NOT_FOUND
        assert "result" not in resp

    def test_tools_list_before_initialize_is_refused(self, h):
        resp = h.call("tools/list")
        assert resp["error"]["code"] == protocol.METHOD_NOT_FOUND

    def test_ping_before_and_after_initialize(self, h):
        assert h.call("ping")["result"] == {}
        h.init()
        assert h.call("ping")["result"] == {}

    def test_stdin_eof_ends_the_loop(self, h):
        h.init()
        assert not h.eof.is_set()
        h.close()
        assert h.eof.wait(3.0)

    def test_eof_while_a_call_is_parked_still_ends_the_loop(self):
        gate = threading.Event()

        def park(args, ctx):
            gate.wait(10)
            return {}

        h = Harness(_tools(Tool("park", "Park", "waits", {"type": "object"}, park)))
        try:
            h.init()
            h.request("tools/call", {"name": "park", "arguments": {}})
            h.close()
            assert h.eof.wait(3.0)
        finally:
            gate.set()


# --------------------------------------------------------------------------- #
# Framing and JSON-RPC errors
# --------------------------------------------------------------------------- #
class TestJsonRpc:
    def test_parse_error_has_null_id(self, h):
        h.send(b"{not json")
        resp = h.recv()
        assert resp["id"] is None
        assert resp["error"]["code"] == protocol.PARSE_ERROR

    def test_invalid_request(self, h):
        h.send({"id": 7, "method": "ping"})  # no "jsonrpc": "2.0"
        resp = h.recv()
        assert resp["id"] == 7
        assert resp["error"]["code"] == protocol.INVALID_REQUEST

    def test_unknown_method(self, h):
        h.init()
        resp = h.call("resources/list")
        assert resp["error"]["code"] == protocol.METHOD_NOT_FOUND

    def test_unknown_notification_is_ignored(self, h):
        h.init()
        h.send({"jsonrpc": "2.0", "method": "notifications/whatever"})
        h.nothing_for(0.3)

    def test_blank_lines_are_skipped(self, h):
        h.send(b"   ")
        assert h.call("ping")["result"] == {}

    def test_batch_is_answered_as_one_array(self, h):
        """JSON-RPC 2.0 §6: a batch's replies come back as ONE array (they used
        to be written as separate lines)."""
        h.init("2025-03-26")
        h.send(
            [
                {"jsonrpc": "2.0", "id": "a", "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/whatever"},
                {"jsonrpc": "2.0", "id": "b", "method": "tools/list"},
            ]
        )
        resp = h.recv()
        assert isinstance(resp, list)
        assert [r["id"] for r in resp] == ["a", "b"]
        h.nothing_for(0.2)

    def test_batch_of_notifications_gets_no_reply(self, h):
        h.init("2025-03-26")
        h.send([{"jsonrpc": "2.0", "method": "notifications/whatever"}])
        h.nothing_for(0.3)

    def test_tools_call_inside_a_batch_is_refused_in_the_array(self, h):
        h.init("2025-03-26")
        h.send(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"n": 1}},
                },
            ]
        )
        resp = h.recv()
        assert [r["id"] for r in resp] == [1, 2]
        assert resp[1]["error"]["code"] == protocol.INVALID_REQUEST
        h.nothing_for(0.3)

    @pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])
    def test_batches_are_refused_where_the_protocol_removed_them(self, h, version):
        h.init(version)
        h.send([{"jsonrpc": "2.0", "id": "a", "method": "ping"}])
        resp = h.recv()
        assert resp["id"] is None
        assert resp["error"]["code"] == protocol.INVALID_REQUEST
        assert "batch" in resp["error"]["message"]

    @pytest.mark.parametrize("bad_id", [[1], {"a": 1}, True])
    def test_an_unhashable_id_is_an_error_not_a_crash(self, h, bad_id):
        """A list/object id used to raise TypeError on ``_calls[id]`` and kill
        the whole stdio server."""
        h.init()
        h.send(
            {
                "jsonrpc": "2.0",
                "id": bad_id,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"n": 1}},
            }
        )
        resp = h.recv()
        assert resp["id"] is None
        assert resp["error"]["code"] == protocol.INVALID_REQUEST
        assert h.call("ping")["result"] == {}  # still serving

    def test_a_cancel_with_an_unhashable_request_id_is_ignored(self, h):
        h.init()
        h.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": {"a": 1}},
            }
        )
        assert h.call("ping")["result"] == {}
        assert not h.eof.is_set()

    def test_a_handler_crash_never_ends_the_serve_loop(self, h, monkeypatch):
        h.init()
        real = h.server.handle_message

        def flaky(message):
            if isinstance(message, dict) and message.get("method") == "explode":
                raise RuntimeError("boom")
            return real(message)

        monkeypatch.setattr(h.server, "handle_message", flaky)
        h.send({"jsonrpc": "2.0", "id": 9, "method": "explode"})
        assert h.call("ping")["result"] == {}

    def test_output_is_one_json_object_per_line_even_with_newlines(self, h):
        h.init()

        def multi(args, ctx):
            return {"text": "line1\nline2"}

        h.server.tools["multi"] = Tool("multi", "M", "m", {"type": "object"}, multi)
        resp = h.call("tools/call", {"name": "multi", "arguments": {}})
        assert json.loads(resp["result"]["content"][0]["text"]) == {
            "text": "line1\nline2"
        }


# --------------------------------------------------------------------------- #
# tools/list + tools/call
# --------------------------------------------------------------------------- #
class TestTools:
    def test_tools_list_shape(self, h):
        h.init("2025-06-18")
        tools = h.call("tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["echo", "boom", "crash", "text"]
        echo = tools[0]
        assert echo["description"] == "echoes"
        assert echo["inputSchema"] == _SCHEMA
        assert echo["annotations"]["readOnlyHint"] is True
        assert echo["annotations"]["title"] == "Echo"
        assert echo["title"] == "Echo"

    def test_tools_list_old_version_has_no_top_level_title(self, h):
        h.init("2024-11-05")
        tools = h.call("tools/list")["result"]["tools"]
        assert "title" not in tools[0]
        assert tools[0]["annotations"]["title"] == "Echo"

    def test_success_with_structured_content(self, h):
        h.init("2025-06-18")
        resp = h.call("tools/call", {"name": "echo", "arguments": {"n": 3}})
        result = resp["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"]) == {"echo": {"n": 3}}
        assert result["structuredContent"] == {"echo": {"n": 3}}

    def test_no_structured_content_before_2025_06_18(self, h):
        h.init("2025-03-26")
        resp = h.call("tools/call", {"name": "echo", "arguments": {"n": 3}})
        assert "structuredContent" not in resp["result"]

    def test_string_result_is_plain_text(self, h):
        h.init()
        resp = h.call("tools/call", {"name": "text"})
        assert resp["result"]["content"] == [{"type": "text", "text": "plain words"}]

    def test_unknown_tool_is_invalid_params(self, h):
        h.init()
        resp = h.call("tools/call", {"name": "nope", "arguments": {}})
        assert resp["error"]["code"] == protocol.INVALID_PARAMS

    def test_non_object_arguments_is_invalid_params(self, h):
        h.init()
        resp = h.call("tools/call", {"name": "echo", "arguments": [1, 2]})
        assert resp["error"]["code"] == protocol.INVALID_PARAMS

    def test_schema_violation_is_a_tool_error_result(self, h):
        h.init()
        resp = h.call("tools/call", {"name": "echo", "arguments": {"n": 99}})
        result = resp["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "invalid arguments for echo" in text and "n must be <= 10" in text

    def test_missing_required_and_unknown_args(self, h):
        h.init()
        r1 = h.call("tools/call", {"name": "echo", "arguments": {}})["result"]
        assert (
            r1["isError"]
            and "missing required argument 'n'" in r1["content"][0]["text"]
        )
        r2 = h.call("tools/call", {"name": "echo", "arguments": {"n": 1, "x": 2}})[
            "result"
        ]
        assert r2["isError"] and "unknown argument(s) 'x'" in r2["content"][0]["text"]

    def test_tool_error_carries_data(self, h):
        h.init()
        result = h.call("tools/call", {"name": "boom", "arguments": {}})["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert text.startswith("nope")
        assert "abc fix" in text

    def test_handler_crash_is_contained(self, h):
        h.init()
        result = h.call("tools/call", {"name": "crash"})["result"]
        assert result["isError"] is True
        assert "RuntimeError: kaput" in result["content"][0]["text"]
        assert h.call("ping")["result"] == {}  # still serving

    def test_ping_answers_while_a_call_is_running(self):
        gate = threading.Event()

        def slow(args, ctx):
            gate.wait(5)
            return {"ok": True}

        h = Harness(_tools(Tool("slow", "Slow", "s", {"type": "object"}, slow)))
        try:
            h.init()
            call_id = h.request("tools/call", {"name": "slow", "arguments": {}})
            assert h.call("ping")["result"] == {}
            gate.set()
            resp = h.recv()
            assert resp["id"] == call_id and resp["result"]["isError"] is False
        finally:
            h.close()

    def test_busy_beyond_the_concurrency_cap(self):
        gate = threading.Event()

        def slow(args, ctx):
            gate.wait(5)
            return {"ok": True}

        h = Harness(
            _tools(Tool("slow", "Slow", "s", {"type": "object"}, slow)),
            max_concurrent=1,
        )
        try:
            h.init()
            first = h.request("tools/call", {"name": "slow", "arguments": {}})
            time.sleep(0.1)
            resp = h.call("tools/call", {"name": "slow", "arguments": {}})
            assert resp["result"]["isError"] is True
            assert "busy" in resp["result"]["content"][0]["text"]
            gate.set()
            assert h.recv()["id"] == first
        finally:
            h.close()


# --------------------------------------------------------------------------- #
# Cancellation and progress
# --------------------------------------------------------------------------- #
class TestCancellationAndProgress:
    def test_cancelled_call_sends_no_response(self):
        entered = threading.Event()
        finished = threading.Event()

        def waiter(args, ctx):
            entered.set()
            try:
                ctx.sleep(10)
            finally:
                finished.set()
            return {"never": True}

        h = Harness(_tools(Tool("wait", "W", "w", {"type": "object"}, waiter)))
        try:
            h.init()
            call_id = h.request("tools/call", {"name": "wait", "arguments": {}})
            assert entered.wait(2)
            h.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": call_id, "reason": "user"},
                }
            )
            assert finished.wait(2)  # the wait stopped promptly
            h.nothing_for(0.4)  # and no response was written
            assert h.call("ping")["result"] == {}
            assert h.server._calls == {}  # its concurrency slot was freed
        finally:
            h.close()

    def test_cancel_for_unknown_request_is_harmless(self, h):
        h.init()
        h.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 12345},
            }
        )
        assert h.call("ping")["result"] == {}

    def test_progress_is_monotonic_and_stops_before_the_result(self):
        def slow(args, ctx):
            ctx.start_progress(5)
            ctx.sleep(0.45)
            return {"done": True}

        h = Harness(
            _tools(Tool("slow", "S", "s", {"type": "object"}, slow)),
            progress_interval=0.05,
        )
        try:
            h.init()
            call_id = h.request(
                "tools/call",
                {"name": "slow", "arguments": {}, "_meta": {"progressToken": "tok"}},
            )
            progress = []
            while True:
                msg = h.recv()
                if msg.get("id") == call_id:
                    break
                assert msg["method"] == "notifications/progress"
                assert msg["params"]["progressToken"] == "tok"
                assert msg["params"]["total"] == 5
                progress.append(msg["params"]["progress"])
            assert len(progress) >= 2
            assert all(b > a for a, b in zip(progress, progress[1:]))
            h.nothing_for(0.3)  # the ticker never writes after the result
        finally:
            h.close()

    def test_no_progress_without_a_token(self):
        def slow(args, ctx):
            ctx.start_progress(5)
            ctx.sleep(0.3)
            return {"done": True}

        h = Harness(
            _tools(Tool("slow", "S", "s", {"type": "object"}, slow)),
            progress_interval=0.05,
        )
        try:
            h.init()
            call_id = h.request("tools/call", {"name": "slow", "arguments": {}})
            assert h.recv()["id"] == call_id
        finally:
            h.close()


# --------------------------------------------------------------------------- #
# Schema validation helper
# --------------------------------------------------------------------------- #
class TestValidate:
    def test_lenient_coercions(self):
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "integer"},
                "f": {"type": "number"},
                "b": {"type": "boolean"},
                "xs": {"type": "array", "items": {"type": "string"}},
            },
        }
        out = validate({"n": "5", "f": "1.5", "b": "true", "xs": '["a","b"]'}, schema)
        assert out == {"n": 5, "f": 1.5, "b": True, "xs": ["a", "b"]}
        assert validate({"n": 4.0}, schema) == {"n": 4}

    def test_a_lone_scalar_is_wrapped_where_an_array_belongs(self):
        schema = {"type": "array", "items": {"type": "string", "enum": ["1", "y"]}}
        assert validate("1", schema) == ["1"]
        with pytest.raises(SchemaError):
            validate("Del", schema)  # still checked against the item schema
        assert validate(3, {"type": "array", "items": {"type": "integer"}}) == [3]
        with pytest.raises(SchemaError):
            validate(True, {"type": "array"})

    def test_an_argv_array_can_opt_out_of_the_scalar_wrap(self):
        schema = {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "x-wrap-scalar": False,
                },
                "keys": {"type": "array", "items": {"type": "string"}},
            },
        }
        with pytest.raises(SchemaError, match="argv must be"):
            validate({"argv": "--model opus"}, schema)
        assert validate({"argv": '["--model","opus"]'}, schema) == {
            "argv": ["--model", "opus"]
        }
        assert validate({"keys": "1"}, schema) == {"keys": ["1"]}  # still wrapped

    def test_published_schemas_drop_private_hints(self):
        from backend.mcp.protocol import public_schema

        schema = {
            "type": "object",
            "properties": {"a": {"type": "array", "x-wrap-scalar": False}},
            "anyOf": [{"x-y": 1, "type": "string"}],
        }
        assert public_schema(schema) == {
            "type": "object",
            "properties": {"a": {"type": "array"}},
            "anyOf": [{"type": "string"}],
        }

    def test_bool_is_not_an_integer(self):
        with pytest.raises(SchemaError):
            validate(True, {"type": "integer"})

    def test_null_optional_means_default(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        assert validate({"x": None}, schema) == {}

    def test_null_required_is_missing(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
        with pytest.raises(SchemaError, match="missing required"):
            validate({"x": None}, schema)

    def test_any_of(self):
        schema = {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            ]
        }
        assert validate("a", schema) == "a"
        assert validate(["a", "b"], schema) == ["a", "b"]
        with pytest.raises(SchemaError):
            validate(["a", "b", "c"], schema)
        with pytest.raises(SchemaError):
            validate(3, schema)

    def test_string_and_array_bounds(self):
        with pytest.raises(SchemaError, match="must not be empty"):
            validate("", {"type": "string", "minLength": 1})
        with pytest.raises(SchemaError, match="limit is 3"):
            validate("abcd", {"type": "string", "maxLength": 3})
        with pytest.raises(SchemaError, match="at least 1"):
            validate([], {"type": "array", "minItems": 1})

    def test_enum_items(self):
        schema = {"type": "array", "items": {"type": "string", "enum": ["Enter"]}}
        with pytest.raises(SchemaError, match=r"\[1\] must be one of"):
            validate(["Enter", "Del"], schema)
