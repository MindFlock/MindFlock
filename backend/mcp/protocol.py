"""JSON-RPC 2.0 over stdio — the Model Context Protocol transport, stdlib only.

One message per line, UTF-8 JSON, newline-delimited (the MCP stdio framing);
nothing but protocol ever goes to the output stream — logs go to stderr.

Scope (deliberately small — this server only exposes tools):

* lifecycle: ``initialize`` (version negotiation over the "legacy-era"
  revisions in :data:`SUPPORTED_VERSIONS`: the client's version is echoed when
  supported, else the latest is offered), ``notifications/initialized``,
  ``ping``;
* tools: ``tools/list``, ``tools/call``;
* utilities: ``notifications/cancelled`` (the handler's waits stop and NO
  response is sent, as the spec asks) and ``notifications/progress`` (sent
  while a long call waits, when the caller supplied ``_meta.progressToken``).

Anything sent before ``initialize`` other than ``ping`` — notably the
``server/discover`` probe a 2026-07-28-era client sends to detect stateless
servers — is answered IMMEDIATELY with ``-32601``: a dual-era client falls back
to ``initialize`` on any error, but silence would make every session start sit
out the probe timeout. The stateless 2026-07-28 mode itself is a non-goal.

``tools/call`` runs each handler on a daemon thread (at most
:data:`MAX_CONCURRENT_CALLS` in flight; beyond that the call fails fast as a
tool error) so a long ``wait_*`` never blocks ``ping`` or cancellation. Every
write to the output stream is serialized by one lock, and a call's progress
ticker is stopped under that same lock before its result is written, so no
progress notification can ever follow the response it belongs to.

Error mapping (2025-11-25 tools spec): an unknown tool or non-object
``arguments`` is a protocol error (``-32602``); arguments that violate the
tool's input schema are a tool RESULT with ``isError: true`` and a fix-it
message, so the model can correct itself.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

__all__ = [
    "SUPPORTED_VERSIONS",
    "LATEST_VERSION",
    "MAX_CONCURRENT_CALLS",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "Cancelled",
    "ToolError",
    "Tool",
    "ToolContext",
    "McpServer",
    "validate",
    "SchemaError",
]

#: Protocol revisions this server speaks, oldest first (the last is offered
#: when the client asks for anything else).
SUPPORTED_VERSIONS: Tuple[str, ...] = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)
LATEST_VERSION = SUPPORTED_VERSIONS[-1]
#: First revision with ``structuredContent`` / top-level tool ``title``.
_STRUCTURED_SINCE = "2025-06-18"
#: First revision without JSON-RPC batching.
_NO_BATCH_SINCE = "2025-06-18"


def _valid_id(value: Any) -> bool:
    """A JSON-RPC id: a string, a number (not a bool) or null."""
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: Tool calls allowed in flight at once; more fail fast with a "busy" result.
MAX_CONCURRENT_CALLS = 8
#: Seconds between ``notifications/progress`` while a call waits.
PROGRESS_INTERVAL_S = 10.0

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Cancelled(Exception):
    """Raised inside a handler's wait once the client cancelled the call."""


class ToolError(Exception):
    """A tool failed in a way the model should see: becomes a result with
    ``isError: true`` whose text is the message (plus ``data`` as JSON when
    given, so e.g. the unmerged commits behind a refusal stay visible)."""

    def __init__(self, message: str, data: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


@dataclass
class Tool:
    """One registered tool: wire definition + handler.

    ``handler(args, ctx)`` gets schema-validated arguments and returns a
    JSON-serializable dict (sent as text + ``structuredContent``) or a str."""

    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[[dict, "ToolContext"], Any]
    annotations: dict = field(default_factory=dict)

    def definition(self, version: str) -> dict:
        ann = dict(self.annotations)
        ann.setdefault("title", self.title)
        out = {
            "name": self.name,
            "description": self.description,
            "inputSchema": public_schema(self.input_schema),
            "annotations": ann,
        }
        if version >= _STRUCTURED_SINCE:
            out["title"] = self.title
        return out


def public_schema(schema: Any) -> Any:
    """``schema`` without our private ``x-`` validation hints (e.g.
    ``x-wrap-scalar``) — strict clients reject keywords they don't know."""
    if isinstance(schema, dict):
        return {
            k: public_schema(v) for k, v in schema.items() if not k.startswith("x-")
        }
    if isinstance(schema, list):
        return [public_schema(v) for v in schema]
    return schema


# --------------------------------------------------------------------------- #
# Minimal JSON-Schema validation (the subset our tool schemas use)
# --------------------------------------------------------------------------- #
class SchemaError(ValueError):
    """Arguments don't match a tool's input schema; the message says how."""


_TYPE_NAMES = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "array": "an array",
    "object": "an object",
}


def _coerce(value: Any, typ: str, schema: Optional[dict] = None) -> Any:
    """Lenient fixes for what LLM clients commonly send: numbers/booleans as
    strings, integral floats, arrays serialized as a JSON string, and a lone
    scalar where an array belongs (wrapped in a one-item list). Returns
    the value unchanged when no unambiguous coercion applies.

    An array schema with ``"x-wrap-scalar": false`` opts out of the wrap: for
    an argv (``launch_args: "--model opus"``) a wrapped string is ONE token
    with a space in it, which the CLI rejects at launch — the schema error
    that tells the caller to send a list is the better answer."""
    if typ == "integer":
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    elif typ == "number":
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value
    elif typ == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    elif typ == "array":
        if isinstance(value, str) and value.strip().startswith("["):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            if isinstance(parsed, list):
                return parsed
        if (
            isinstance(value, (str, int, float))
            and not isinstance(value, bool)
            and (schema or {}).get("x-wrap-scalar", True)
        ):
            # One scalar where a list belongs (``keys: "1"``): unambiguous —
            # each item is still checked against the item schema.
            return [value]
    return value


def _is_type(value: Any, typ: str) -> bool:
    if typ == "string":
        return isinstance(value, str)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "array":
        return isinstance(value, list)
    if typ == "object":
        return isinstance(value, dict)
    if typ == "null":
        return value is None
    return True


def validate(value: Any, schema: dict, where: str = "arguments") -> Any:
    """Validate (and leniently coerce) ``value`` against ``schema``.

    Supports ``type``, ``enum``, ``anyOf``, ``properties``/``required``/
    ``additionalProperties: false``, ``items``, ``minItems``/``maxItems``,
    ``minLength``/``maxLength``, ``minimum``/``maximum``. Returns the coerced
    value; raises :class:`SchemaError` naming the offending field."""
    if "anyOf" in schema:
        errors = []
        for alt in schema["anyOf"]:
            try:
                return validate(value, alt, where)
            except SchemaError as err:
                errors.append(str(err))
        raise SchemaError(errors[0] if len(errors) == 1 else "; or ".join(errors))
    typ = schema.get("type")
    if isinstance(typ, str):
        value = _coerce(value, typ, schema)
        if not _is_type(value, typ):
            raise SchemaError(
                "%s must be %s, got %s"
                % (where, _TYPE_NAMES.get(typ, typ), json.dumps(value)[:80])
            )
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(
            "%s must be one of %s, got %s"
            % (
                where,
                ", ".join(json.dumps(e) for e in schema["enum"]),
                json.dumps(value)[:80],
            )
        )
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(
                "%s must not be empty" % where
                if schema["minLength"] == 1
                else "%s must be at least %d characters" % (where, schema["minLength"])
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(
                "%s is %d characters; the limit is %d"
                % (where, len(value), schema["maxLength"])
            )
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError("%s must be >= %s" % (where, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError("%s must be <= %s" % (where, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(
                "%s needs at least %d item(s)" % (where, schema["minItems"])
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(
                "%s has %d items; the limit is %d"
                % (where, len(value), schema["maxItems"])
            )
        items = schema.get("items")
        if isinstance(items, dict):
            value = [
                validate(v, items, "%s[%d]" % (where, i)) for i, v in enumerate(value)
            ]
    if isinstance(value, dict) and (
        "properties" in schema or "required" in schema or typ == "object"
    ):
        props = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in value or value[name] is None:
                raise SchemaError("missing required argument %r" % name)
        if schema.get("additionalProperties") is False:
            extra = sorted(k for k in value if k not in props)
            if extra:
                raise SchemaError(
                    "unknown argument(s) %s; accepted: %s"
                    % (", ".join(map(repr, extra)), ", ".join(sorted(props)) or "none")
                )
        out = {}
        for key, val in value.items():
            # An explicit null for an optional argument means "use the default".
            if val is None and key not in (schema.get("required") or ()):
                continue
            sub = props.get(key)
            out[key] = validate(val, sub, key) if isinstance(sub, dict) else val
        value = out
    return value


# --------------------------------------------------------------------------- #
# Per-call context
# --------------------------------------------------------------------------- #
class ToolContext:
    """What a handler can do besides compute: notice cancellation, sleep
    cancellably, and keep the client's idle timer alive with progress."""

    def __init__(
        self,
        server: Optional["McpServer"] = None,
        request_id: Any = None,
        progress_token: Any = None,
    ) -> None:
        self._server = server
        self.request_id = request_id
        self.progress_token = progress_token
        self.cancelled = threading.Event()
        self._ticker: Optional[_ProgressTicker] = None

    def sleep(self, seconds: float) -> None:
        """Sleep up to ``seconds``; raise :class:`Cancelled` if the call is
        cancelled meanwhile (or already was)."""
        if self.cancelled.wait(max(0.0, seconds)):
            raise Cancelled()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()

    def start_progress(self, total: Optional[float] = None) -> None:
        """Begin periodic ``notifications/progress`` (no-op without a progress
        token, or when already started). ``total`` is the wait's timeout."""
        if self._server is None or self.progress_token is None or self._ticker:
            return
        self._ticker = _ProgressTicker(self._server, self.progress_token, total)
        self._ticker.start()

    def _stop_progress_locked(self) -> None:
        if self._ticker is not None:
            self._ticker.stopped = True
            self._ticker.wake.set()


class _ProgressTicker(threading.Thread):
    """Sends ``progress`` = elapsed seconds (strictly increasing) every
    ``server.progress_interval`` until stopped. Stopping happens under the
    server's write lock, and the ticker re-checks ``stopped`` under that same
    lock before each write — so it can never write after the result."""

    def __init__(self, server: "McpServer", token: Any, total: Optional[float]):
        super().__init__(daemon=True, name="mcp-progress")
        self.server = server
        self.token = token
        self.total = total
        self.stopped = False
        self.wake = threading.Event()
        self.started_at = time.monotonic()
        self.last = 0.0

    def run(self) -> None:
        while True:
            self.wake.wait(self.server.progress_interval)
            with self.server._write_lock:
                if self.stopped:
                    return
                elapsed = round(time.monotonic() - self.started_at, 1)
                progress = elapsed if elapsed > self.last else self.last + 0.1
                self.last = round(progress, 1)
                params: Dict[str, Any] = {
                    "progressToken": self.token,
                    "progress": self.last,
                }
                if self.total:
                    params["total"] = self.total
                self.server._write_locked(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": params,
                    }
                )


# --------------------------------------------------------------------------- #
# The server
# --------------------------------------------------------------------------- #
class McpServer:
    """Reads requests from ``stdin`` (binary, line-framed) and writes
    responses/notifications to ``stdout`` (binary) until EOF.

    ``on_eof`` runs when stdin closes; the default hard-exits the process
    (``os._exit(0)``) so a handler thread parked in a long wait can never keep
    an orphaned server alive after the agent CLI went away."""

    def __init__(
        self,
        tools: List[Tool],
        stdin: BinaryIO,
        stdout: BinaryIO,
        *,
        name: str = "mindflock",
        version: str = "0",
        instructions: str = "",
        on_eof: Optional[Callable[[], None]] = None,
        max_concurrent: int = MAX_CONCURRENT_CALLS,
        progress_interval: float = PROGRESS_INTERVAL_S,
    ) -> None:
        self.tools: Dict[str, Tool] = {t.name: t for t in tools}
        self._order = [t.name for t in tools]
        self.stdin = stdin
        self.stdout = stdout
        self.name = name
        self.version = version
        self.instructions = instructions
        self.on_eof = on_eof or self._hard_exit
        self.max_concurrent = max_concurrent
        self.progress_interval = progress_interval
        self.protocol_version: Optional[str] = None
        self._write_lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self._calls: Dict[Any, ToolContext] = {}
        # While the reader thread handles a JSON-RPC batch, its synchronous
        # replies are collected here and written as ONE array (JSON-RPC 2.0
        # §6); worker threads never see it.
        self._batch = threading.local()

    # -- output -------------------------------------------------------------- #
    def _write_locked(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            self.stdout.write(line.encode("utf-8") + b"\n")
            self.stdout.flush()
        except (OSError, ValueError) as err:  # client went away mid-write
            _log.debug("mcp: write failed: %s", err)

    def send(self, message: dict) -> None:
        sink = getattr(self._batch, "sink", None)
        if sink is not None:
            sink.append(message)
            return
        with self._write_lock:
            self._write_locked(message)

    def _result(self, msg_id: Any, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )

    # -- input loop ---------------------------------------------------------- #
    def run(self) -> None:
        """Serve until stdin reaches EOF, then call ``on_eof``."""
        while True:
            try:
                line = self.stdin.readline()
            except (OSError, ValueError):
                line = b""
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                self.handle_line(line)
            except Exception:  # noqa: BLE001 — one bad message never ends the loop
                _log.exception("mcp: failed to handle a message")
        self.on_eof()

    @staticmethod
    def _hard_exit() -> None:  # pragma: no cover — exercised by the e2e test
        os._exit(0)

    def handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(None, PARSE_ERROR, "parse error: not valid JSON")
            return
        if not isinstance(message, list):
            self.handle_message(message)
            return
        self._handle_batch(message)

    def _handle_batch(self, batch: list) -> None:
        """A JSON-RPC batch: only before 2025-06-18 (which removed them); the
        replies go back as ONE array, and nothing at all for a batch of
        notifications. ``tools/call`` answers asynchronously from a worker
        thread, so inside a batch it is refused (in the array) rather than
        answered out of band."""
        if not batch:
            self._error(None, INVALID_REQUEST, "invalid request: empty batch")
            return
        version = self.protocol_version
        if version is not None and version >= _NO_BATCH_SINCE:
            self._error(
                None,
                INVALID_REQUEST,
                "invalid request: JSON-RPC batches are not supported in protocol "
                "version %s" % version,
            )
            return
        sink: List[dict] = []
        self._batch.sink = sink
        try:
            for item in batch:
                if (
                    isinstance(item, dict)
                    and item.get("method") == "tools/call"
                    and "id" in item
                ):
                    msg_id = item.get("id")
                    self._error(
                        msg_id if _valid_id(msg_id) else None,
                        INVALID_REQUEST,
                        "invalid request: tools/call is not allowed in a batch; "
                        "send it on its own",
                    )
                    continue
                self.handle_message(item)
        finally:
            self._batch.sink = None
        if sink:
            with self._write_lock:
                self._write_locked(sink)

    def handle_message(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            msg_id = message.get("id") if isinstance(message, dict) else None
            self._error(msg_id, INVALID_REQUEST, "invalid request: not JSON-RPC 2.0")
            return
        method = message.get("method")
        has_id = "id" in message
        msg_id = message.get("id")
        if method is None:
            # A response to something we sent (we send no requests) — ignore.
            return
        if has_id and not _valid_id(msg_id):
            # JSON-RPC ids are strings, numbers or null; a list/object id is
            # unhashable and used to crash the whole server on `_calls[id]`.
            self._error(
                None,
                INVALID_REQUEST,
                "invalid request: id must be a string, a number or null",
            )
            return
        if not isinstance(method, str):
            if has_id:
                self._error(msg_id, INVALID_REQUEST, "invalid request: bad method")
            return
        params = message.get("params")
        if params is None:
            params = {}
        if not has_id:
            self._notification(method, params)
            return
        if not isinstance(params, dict):
            self._error(msg_id, INVALID_PARAMS, "params must be an object")
            return
        if method == "initialize":
            self._initialize(msg_id, params)
            return
        if method == "ping":
            self._result(msg_id, {})
            return
        if self.protocol_version is None:
            # Pre-initialize requests (incl. the 2026-07-28 `server/discover`
            # probe) get an immediate error — never silence.
            self._error(
                msg_id,
                METHOD_NOT_FOUND,
                "method not found: %s (this server needs initialize first)" % method,
            )
            return
        if method == "tools/list":
            self._result(
                msg_id,
                {
                    "tools": [
                        self.tools[n].definition(self.protocol_version)
                        for n in self._order
                    ]
                },
            )
            return
        if method == "tools/call":
            self._tools_call(msg_id, params)
            return
        self._error(msg_id, METHOD_NOT_FOUND, "method not found: %s" % method)

    def _notification(self, method: str, params: Any) -> None:
        if method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            if not _valid_id(request_id):
                return  # unhashable / malformed: nothing of ours to cancel
            with self._calls_lock:
                ctx = self._calls.get(request_id)
            if ctx is not None:
                ctx.cancelled.set()
        # notifications/initialized and anything unknown: nothing to do.

    def _initialize(self, msg_id: Any, params: dict) -> None:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        self.protocol_version = version
        result: Dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": self.name,
                "title": "MindFlock",
                "version": self.version,
            },
        }
        if self.instructions:
            result["instructions"] = self.instructions
        self._result(msg_id, result)

    # -- tools/call ---------------------------------------------------------- #
    def _tools_call(self, msg_id: Any, params: dict) -> None:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            self._error(msg_id, INVALID_PARAMS, "unknown tool: %s" % (name,))
            return
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            self._error(msg_id, INVALID_PARAMS, "tool arguments must be an object")
            return
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        ctx = ToolContext(self, msg_id, token)
        with self._calls_lock:
            busy = len(self._calls) >= self.max_concurrent
            if not busy:
                self._calls[msg_id] = ctx
        if busy:
            self._result(
                msg_id,
                self._error_result(
                    "busy: %d MindFlock tool calls are already running; wait for "
                    "one to finish (or cancel it) and retry" % self.max_concurrent
                ),
            )
            return
        worker = threading.Thread(
            target=self._run_call,
            args=(tool, args, ctx),
            daemon=True,
            name="mcp-call-%s" % tool.name,
        )
        worker.start()

    def _run_call(self, tool: Tool, args: dict, ctx: ToolContext) -> None:
        result: Optional[dict] = None
        try:
            try:
                clean = validate(args, tool.input_schema)
            except SchemaError as err:
                result = self._error_result(
                    "invalid arguments for %s: %s. Fix the arguments and call it "
                    "again." % (tool.name, err)
                )
            else:
                result = self._success_result(tool.handler(clean, ctx))
        except Cancelled:
            result = None
        except ToolError as err:
            result = self._error_result(err.message, err.data)
        except Exception as err:  # noqa: BLE001 — never kill the server
            _log.exception("mcp: tool %s crashed", tool.name)
            result = self._error_result(
                "internal error in %s: %s: %s" % (tool.name, type(err).__name__, err)
            )
        finally:
            with self._calls_lock:
                self._calls.pop(ctx.request_id, None)
        with self._write_lock:
            ctx._stop_progress_locked()
            if result is None or ctx.cancelled.is_set():
                return  # cancelled: the spec says send no response
            self._write_locked(
                {"jsonrpc": "2.0", "id": ctx.request_id, "result": result}
            )

    def _success_result(self, value: Any) -> dict:
        if isinstance(value, str):
            return {"content": [{"type": "text", "text": value}], "isError": False}
        text = json.dumps(value, ensure_ascii=False, default=str)
        out: Dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
        if (
            isinstance(value, dict)
            and self.protocol_version is not None
            and self.protocol_version >= _STRUCTURED_SINCE
        ):
            out["structuredContent"] = json.loads(text)
        return out

    @staticmethod
    def _error_result(message: str, data: Optional[dict] = None) -> dict:
        text = message
        if data:
            text += "\n" + json.dumps(data, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": True}
