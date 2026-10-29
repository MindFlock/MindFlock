"""The unix-socket API the sandboxed agent's peer-mode MCP talks to.

One :class:`AgentApi` per bound share, listening at ``run/agent.sock`` (0600,
inside a 0700 dir). The sandbox can reach this socket (``run/`` is bound
read-only; connecting needs no write) and nothing else of MindFlock, so this
is the WHOLE surface a prompt-injected agent can drive:

* one request per connection — a JSON line of at most 64 KiB,
  ``{"token": str, "op": str, "args": {...}}`` — and one response line,
  ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": str}``;
* the token is compared with :func:`hmac.compare_digest`, the peer uid must
  be ours (``SO_PEERCRED``), at most :data:`MAX_CONNS` connections at a time,
  a :data:`READ_DEADLINE_S` deadline for the request line;
* a fixed op table with strict per-op argument checks (exact types, bounds,
  no unknown keys). Nothing in a request names a path outside the share, a
  link, or a session of THIS side: ``session_title`` and ``link_id`` are fixed
  at construction, so a request can never reach another session's mailbox.
  The one name a request carries is ``agent_msg``'s ``to``: a session the
  PEER shares on this link (``<peer>::<title>`` or a bare ``<title>``), sent
  as from this session over this link only.

Errors never carry tracebacks: share errors pass their (peer-safe) message,
peer/service errors their sanitized text, anything else is "internal error".

See ``docs/peer-link.md`` ("The sandboxed agent's MCP — CONTRACT").
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import socket
import stat
import struct
import sys
from typing import Any, Callable, Dict, Optional

from backend.peer import paths
from backend.peer import share as share_mod

_log = logging.getLogger(__name__)

__all__ = [
    "AgentApi",
    "ArgError",
    "MAX_LINE",
    "MAX_CONNS",
    "OPS",
    "validate_args",
]

MAX_LINE = 64 * 1024
MAX_CONNS = 16
READ_DEADLINE_S = 10.0
WRITE_DEADLINE_S = 30.0
POLL_S = 0.5
MAX_WAIT_S = 1500
PEER_TIMEOUT_S = 60.0
MAX_TEXT = 20000  # wire.MAX_MSG_TEXT
MAX_TITLE = 200  # wire.MAX_SESSION_TITLE
MAX_TO = 64 + len("::") + MAX_TITLE  # a peer-name prefix, then their title
MAX_PROPOSAL = 10000  # wire.MAX_JOINT_PROPOSAL
MAX_CHECK_STEPS = 25  # wire.MAX_CHECK_STEPS
MAX_CHECK_ANSWER = 60000  # a checklist written as text (a fenced JSON list)
MAX_ERR = 300
_MSG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
# Every control char (newlines included): errors and names are one line.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


class ArgError(ValueError):
    """A request's op or arguments are malformed; the message says how."""


# --------------------------------------------------------------------------- #
# Argument validation — exact types, bounds, no unknown keys
# --------------------------------------------------------------------------- #
def _int(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ArgError("%s must be an integer %d..%d" % (name, lo, hi))
    return v


def _num(v: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ArgError("%s must be a number %s..%s" % (name, lo, hi))
    if not lo <= v <= hi:
        raise ArgError("%s must be a number %s..%s" % (name, lo, hi))
    return float(v)


def _bool(v: Any, name: str) -> bool:
    if not isinstance(v, bool):
        raise ArgError("%s must be a boolean" % name)
    return v


def _str(v: Any, name: str, lo: int, hi: int) -> str:
    if not isinstance(v, str) or not lo <= len(v) <= hi:
        raise ArgError("%s must be a string of %d..%d characters" % (name, lo, hi))
    if "\x00" in v:
        raise ArgError("%s must not contain NUL" % name)
    return v


# op -> {arg: (required, default, checker)}
_SPEC: Dict[str, Dict[str, tuple]] = {
    "whoami": {},
    "send": {
        "text": (True, None, lambda v: _str(v, "text", 1, MAX_TEXT)),
        "reply_to": (False, None, lambda v: _msg_id(v, "reply_to")),
    },
    # Another person's agent in a session THEY share on this link (the
    # service's send_agent_msg, as from this session): ``to`` is
    # ``<peer>::<title>`` or a bare ``<title>`` of theirs.
    "agent_msg": {
        "to": (True, None, lambda v: _str(v, "to", 1, MAX_TO)),
        "text": (True, None, lambda v: _str(v, "text", 1, MAX_TEXT)),
        "reply_to": (False, None, lambda v: _msg_id(v, "reply_to")),
    },
    "inbox": {
        "wait_s": (False, 0.0, lambda v: _num(v, "wait_s", 0, MAX_WAIT_S)),
        "mark_read": (False, True, lambda v: _bool(v, "mark_read")),
        "limit": (False, 20, lambda v: _int(v, "limit", 1, 50)),
    },
    "peer_diff": {
        "max_chars": (False, 50000, lambda v: _int(v, "max_chars", 1000, 200000)),
    },
    "peer_read_file": {
        "path": (True, None, lambda v: _str(v, "path", 1, 1024)),
    },
    "peer_list_files": {},
    "checkpoint": {
        "message": (False, "peer checkpoint", lambda v: _str(v, "message", 0, 2000)),
    },
    # Joint tasks (backend.peer.joint): read the contract or propose a change
    # (a person decides it), and re-read the brief.
    "contract": {
        "action": (True, None, lambda v: _choice(v, "action", ("read", "propose"))),
        "text": (False, None, lambda v: _str(v, "text", 1, MAX_PROPOSAL)),
    },
    "joint": {},
    # The joint check (backend.peer.joint_check): read the checklist, record
    # one of MY agent steps, or write the checklist my user asked me for.
    "check": {
        "action": (
            True,
            None,
            lambda v: _choice(v, "action", ("read", "record", "write")),
        ),
        "step_id": (False, None, lambda v: _str(v, "step_id", 1, 16)),
        "result": (
            False,
            None,
            lambda v: _choice(v, "result", ("pass", "fail", "blocked")),
        ),
        "note": (False, "", lambda v: _str(v, "note", 0, 2000)),
        "steps": (False, None, lambda v: _check_steps(v)),
    },
}
OPS = tuple(_SPEC)


def _check_steps(v: Any):
    """A checklist: a list of step objects (or strings), or text holding a
    JSON list."""
    if isinstance(v, str):
        return _str(v, "steps", 1, MAX_CHECK_ANSWER)
    if not isinstance(v, list) or not 1 <= len(v) <= MAX_CHECK_STEPS:
        raise ArgError("steps must be a list of 1..%d steps" % MAX_CHECK_STEPS)
    if not all(isinstance(x, (dict, str)) for x in v):
        raise ArgError("each step must be an object {text, expect, actor, side}")
    return v


def _choice(v: Any, name: str, allowed: tuple) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise ArgError("%s must be one of %s" % (name, ", ".join(allowed)))
    return v


def _msg_id(v: Any, name: str) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str) or not _MSG_ID_RE.match(v):
        raise ArgError("%s must be a message id ([A-Za-z0-9_-], max 64)" % name)
    return v


def validate_args(op: Any, args: Any) -> dict:
    """``args`` for ``op``, checked and with defaults filled in (ArgError)."""
    if not isinstance(op, str) or op not in _SPEC:
        raise ArgError("unknown op")
    if not isinstance(args, dict):
        raise ArgError("args must be an object")
    spec = _SPEC[op]
    extra = [k for k in args if k not in spec]
    if extra:
        raise ArgError("unknown argument(s) for %s" % op)
    out = {}
    for name, (required, default, check) in spec.items():
        if name not in args:
            if required:
                raise ArgError("missing argument %s" % name)
            out[name] = default
            continue
        out[name] = check(args[name])
    return out


def _no_dupes(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ArgError("duplicate key")
        obj[k] = v
    return obj


def _parse_request(line: bytes) -> dict:
    try:
        text = line.decode("utf-8")
        req = json.loads(text, object_pairs_hook=_no_dupes)
    except ArgError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArgError("malformed request") from None
    if not isinstance(req, dict):
        raise ArgError("malformed request")
    if set(req) - {"token", "op", "args"}:
        raise ArgError("malformed request")
    if not isinstance(req.get("token"), str) or not isinstance(req.get("op"), str):
        raise ArgError("malformed request")
    return req


def _clean_err(err: BaseException) -> str:
    text = str(err) or type(err).__name__
    return _CTRL_RE.sub(" ", text).strip()[:MAX_ERR] or "error"


# --------------------------------------------------------------------------- #
# The server
# --------------------------------------------------------------------------- #
class AgentApi:
    """Serve the agent API for ONE share / link / session.

    ``service`` is duck-typed: ``async request(link_id, op, p, timeout=60) ->
    dict``, ``is_connected(link_id) -> bool``, ``peer_name(link_id) -> str``,
    ``async send_agent_msg(link_id, title, text, from_title, reply_to=None) ->
    {id, ...}`` and optionally ``row_prefixes() -> {prefix: link_id}``; its
    errors are raised as exceptions. ``mailbox`` defaults to
    :mod:`backend.web.core.mailbox` (tests may pass a stand-in)."""

    def __init__(
        self,
        share: "share_mod.Share",
        link_id: str,
        token: str,
        service: Any,
        session_title: str,
        *,
        socket_path: Optional[str] = None,
        max_conns: int = MAX_CONNS,
        read_deadline: float = READ_DEADLINE_S,
        mailbox: Any = None,
        checkpoint: Optional[Callable[..., str]] = None,
    ) -> None:
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("agent token too short")
        try:
            token.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("agent token must be ASCII") from None
        if not isinstance(session_title, str) or not session_title:
            raise ValueError("session title required")
        self.share = share
        self.link_id = link_id
        self._token = token.encode("ascii")
        self.service = service
        self.session_title = session_title
        self.path = socket_path or os.path.join(share.run, "agent.sock")
        self.max_conns = max_conns
        self.read_deadline = read_deadline
        if mailbox is None:
            from backend.web.core import mailbox as _mb

            mailbox = _mb
        self._mailbox = mailbox
        self._checkpoint = checkpoint or share_mod.checkpoint
        self._server: Optional[asyncio.AbstractServer] = None
        self._active = 0
        self._tasks: set = set()
        self.stats = {"accepted": 0, "refused_busy": 0, "bad_token": 0}

    # -- lifecycle ---------------------------------------------------------- #
    def _bind(self) -> socket.socket:
        d = os.path.dirname(self.path)
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError("refusing to replace non-socket %s" % self.path)
            os.unlink(self.path)
        # Bind under a temp name, chmod, then rename into place: the final
        # path never exists with looser permissions than 0600. The temp name
        # is no longer than "agent.sock" itself: sun_path is 104 bytes on
        # macOS, where paths.unix_addr has no /proc escape hatch, and a
        # longer temp name was what tipped a share under an ordinary
        # /Users/<name> past it (the share 500'd before its socket existed).
        tmp = os.path.join(d, ".a%s.s" % secrets.token_hex(3))
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with paths.unix_addr(tmp) as addr:
                sock.bind(addr)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            sock.listen(64)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return sock

    async def start(self) -> None:
        sock = self._bind()
        self._server = await asyncio.start_unix_server(
            self._handle, sock=sock, limit=MAX_LINE + 1
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):
                t.cancel()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._server = None
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except OSError:
            pass

    async def __aenter__(self) -> "AgentApi":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # -- one connection ----------------------------------------------------- #
    async def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n"
        writer.write(data.encode("ascii"))
        await asyncio.wait_for(writer.drain(), WRITE_DEADLINE_S)

    @staticmethod
    def _peer_uid(writer: asyncio.StreamWriter) -> int:
        """The connecting process's uid, or -1 when it can't be read (the
        caller refuses then: fail closed). Linux: SO_PEERCRED; macOS/BSD:
        LOCAL_PEERCRED (struct xucred: u_int version, uid_t uid, ...)."""
        sock = writer.get_extra_info("socket")
        if sock is None:
            return -1
        try:
            if hasattr(socket, "SO_PEERCRED"):
                raw = sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                return struct.unpack("3i", raw)[1]
            if sys.platform == "darwin" or "bsd" in sys.platform:
                sol_local, local_peercred = 0, 0x001
                raw = sock.getsockopt(
                    sol_local, local_peercred, struct.calcsize("IIh2x16I")
                )
                return struct.unpack_from("II", raw)[1]
        except (OSError, struct.error):
            return -1
        return -1

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        busy = self._active >= self.max_conns
        if not busy:
            self._active += 1
        try:
            if busy:
                self.stats["refused_busy"] += 1
                await self._send(writer, {"ok": False, "error": "busy"})
                return
            self.stats["accepted"] += 1
            uid = self._peer_uid(writer)
            if uid != os.getuid():
                await self._send(writer, {"ok": False, "error": "unauthorized"})
                return
            resp = await self._serve_one(reader)
            await self._send(writer, resp)
        except (asyncio.CancelledError, ConnectionError, asyncio.TimeoutError, OSError):
            pass
        except Exception:  # noqa: BLE001 — never kill the server
            _log.exception("agent api: connection failed")
        finally:
            if not busy:
                self._active -= 1
            if task is not None:
                self._tasks.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _serve_one(self, reader: asyncio.StreamReader) -> dict:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), self.read_deadline)
        except asyncio.LimitOverrunError:
            return {"ok": False, "error": "request too large"}
        except asyncio.IncompleteReadError as e:
            if len(e.partial) > MAX_LINE:
                return {"ok": False, "error": "request too large"}
            return {"ok": False, "error": "malformed request"}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout"}
        if len(line) > MAX_LINE + 1:
            return {"ok": False, "error": "request too large"}
        try:
            req = _parse_request(line.rstrip(b"\r\n"))
        except ArgError as e:
            return {"ok": False, "error": str(e)}
        if not hmac.compare_digest(req["token"].encode("utf-8"), self._token):
            self.stats["bad_token"] += 1
            return {"ok": False, "error": "unauthorized"}
        try:
            args = validate_args(req["op"], req.get("args", {}))
        except ArgError as e:
            return {"ok": False, "error": str(e)}
        try:
            result = await getattr(self, "_op_" + req["op"])(args, reader)
        except share_mod.ShareError as e:
            return {"ok": False, "error": _clean_err(e)}
        except _PeerError as e:
            return {"ok": False, "error": e.args[0]}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.exception("agent api: op %s failed", req["op"])
            return {"ok": False, "error": "internal error"}
        return {"ok": True, "result": result}

    # -- peer calls --------------------------------------------------------- #
    async def _peer(self, op: str, p: dict, timeout: float = PEER_TIMEOUT_S) -> dict:
        try:
            if not self.service.is_connected(self.link_id):
                raise _PeerError("peer is not connected")
            res = await self.service.request(self.link_id, op, p, timeout=timeout)
        except _PeerError:
            raise
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise _PeerError("peer did not answer in time") from None
        except Exception as e:  # noqa: BLE001 — peer/service error text only
            raise _PeerError(_clean_err(e)) from None
        if not isinstance(res, dict):
            raise _PeerError("bad response from peer")
        return res

    def _peer_name(self) -> str:
        try:
            return _CTRL_RE.sub("", str(self.service.peer_name(self.link_id)))[:64]
        except Exception:  # noqa: BLE001
            return "peer"

    def _connected(self) -> bool:
        try:
            return bool(self.service.is_connected(self.link_id))
        except Exception:  # noqa: BLE001
            return False

    # -- ops ---------------------------------------------------------------- #
    async def _op_whoami(self, args: dict, reader) -> dict:
        return {
            "share_id": self.share.share_id,
            "folder": self.share.work,
            "peer_name": self._peer_name(),
            "connected": self._connected(),
        }

    async def _op_send(self, args: dict, reader) -> dict:
        msg_id = "p" + secrets.token_hex(12)
        res = await self._peer(
            "msg",
            {"msg_id": msg_id, "text": args["text"], "reply_to": args["reply_to"]},
        )
        return {"msg_id": msg_id, "delivered": res.get("accepted") is True}

    def _row_prefixes(self) -> set:
        """The prefixes a ``<prefix>::<title>`` row of THIS link's peer goes
        by (casefolded): its name, and its rail prefix (numbered when the
        name collides — the sender of a message it sent us)."""
        out = set()
        try:
            out.add(str(self.service.peer_name(self.link_id)).casefold())
        except Exception:  # noqa: BLE001
            pass
        try:
            prefixes = self.service.row_prefixes()
        except Exception:  # noqa: BLE001 — optional on the service
            prefixes = {}
        for prefix, lid in dict(prefixes or {}).items():
            if lid == self.link_id:
                out.add(str(prefix).casefold())
        out.discard("")
        return out

    def _their_title(self, to: str) -> str:
        """``to`` as the title of a session of THIS link's peer; any other
        row is refused (this session talks over its own link only)."""
        prefix, sep, title = to.partition("::")
        if not sep:
            title = to
        elif prefix.casefold() not in self._row_prefixes():
            name = self._peer_name()
            raise _PeerError(
                "%r is not %s's: this session reaches only %s's sessions "
                "(%s::<title>, or just <title>)"
                % (_CTRL_RE.sub("", prefix)[:64], name, name, name)
            )
        if not title or len(title) > MAX_TITLE or "::" in title:
            raise _PeerError(
                "to must be <peer>::<title> or <title> (max %d)" % MAX_TITLE
            )
        if _CTRL_RE.search(title):
            raise _PeerError("to must not contain control characters")
        return title

    async def _op_agent_msg(self, args: dict, reader) -> dict:
        title = self._their_title(args["to"])
        try:
            res = await self.service.send_agent_msg(
                self.link_id,
                title,
                args["text"],
                self.session_title,
                reply_to=args["reply_to"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — PeerServiceError's sentence
            msg = getattr(e, "message", None)
            raise _PeerError(
                _clean_err(Exception(msg) if isinstance(msg, str) and msg else e)
            ) from None
        if not isinstance(res, dict) or not res.get("id"):
            raise _PeerError("bad response from peer")
        return {
            "msg_id": str(res["id"]),
            "to": "%s::%s" % (self._peer_name(), title),
            "delivered": True,
        }

    async def _op_peer_diff(self, args: dict, reader) -> dict:
        return await self._peer("diff", {"max_chars": args["max_chars"]})

    async def _op_peer_read_file(self, args: dict, reader) -> dict:
        return await self._peer("read_file", {"path": args["path"]})

    async def _op_peer_list_files(self, args: dict, reader) -> dict:
        return await self._peer("list_files", {})

    async def _joint(self, fn, *args):
        """A joint-task call on the service; its refusal text, else generic."""
        try:
            out = fn(*args)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "message", None)
            if isinstance(msg, str) and msg:
                raise _PeerError(_clean_err(Exception(msg))) from None
            _log.warning("agent api: joint call failed: %s", type(e).__name__)
            raise _PeerError("internal error") from None

    async def _op_contract(self, args: dict, reader) -> dict:
        joint = self.service.joint
        if args["action"] == "read":
            return await self._joint(joint.agent_contract, self.link_id)
        if args["text"] is None:
            raise _PeerError("propose needs text: the whole new contract")
        res = await self._joint(joint.propose_edit, self.link_id, args["text"], "agent")
        return {"proposal_id": res["proposal_id"]}

    async def _op_joint(self, args: dict, reader) -> dict:
        return await self._joint(self.service.joint.agent_joint, self.link_id)

    async def _op_check(self, args: dict, reader) -> dict:
        checks = self.service.joint.check
        action = args["action"]
        if action == "read":
            return await self._joint(checks.agent_read, self.link_id)
        if action == "record":
            if args["step_id"] is None or args["result"] is None:
                raise _PeerError("record needs step_id and result")
            return await self._joint(
                checks.agent_record,
                self.link_id,
                args["step_id"],
                args["result"],
                args["note"],
            )
        if args["steps"] is None:
            raise _PeerError("write needs steps: the JSON list of the checklist")
        return await self._joint(checks.agent_write, self.link_id, args["steps"])

    async def _op_checkpoint(self, args: dict, reader) -> dict:
        sha = await asyncio.to_thread(self._checkpoint, self.share, args["message"])
        return {"sha": sha}

    async def _op_inbox(self, args: dict, reader) -> dict:
        title = self.session_title  # fixed: no request can name another box
        mb = self._mailbox
        wait = args["wait_s"]

        def fetch():
            return mb.fetch(
                title,
                unread_only=True,
                limit=args["limit"],
                mark_read=args["mark_read"],
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        gone = asyncio.ensure_future(reader.read(1)) if wait > 0 else None
        if wait > 0:
            mb.waiter_begin(title)
        try:
            while True:
                result = await asyncio.to_thread(fetch)
                if result["messages"] or loop.time() >= deadline:
                    break
                seen = result["version"]
                changed = False
                while loop.time() < deadline:
                    await asyncio.sleep(min(POLL_S, max(0.0, deadline - loop.time())))
                    if gone is not None and gone.done():
                        break  # the client hung up: stop waiting
                    if await asyncio.to_thread(mb.version, title) != seen:
                        changed = True
                        break
                if not changed:
                    break
        finally:
            if wait > 0:
                mb.waiter_end(title)
            if gone is not None and not gone.done():
                gone.cancel()
        return {
            "messages": [_public_msg(m, self.link_id) for m in result["messages"]],
            "unread": result.get("unread", 0),
        }


class _PeerError(Exception):
    """A peer-side or service failure; args[0] is already sanitized."""


def _public_msg(m: dict, link_id: str = "") -> dict:
    data = m.get("data") if isinstance(m.get("data"), dict) else {}
    sender = m.get("from") or ""
    # Another person's agent over a peer link (a ``<peer>::<title>`` sender
    # with ``data.peer``: only the service posts one) — mailbox.is_cross_user.
    cross = data.get("peer") is True and "::" in sender
    out = {
        "id": m.get("id"),
        "from": sender,
        "text": m.get("text", ""),
        "ts": m.get("ts"),
        "untrusted": sender.startswith("peer:") or cross,
    }
    if cross:
        # Answered with peer_send(to=..., reply_to=...): reply_to is OUR copy's
        # id (the service maps it to theirs) — only over the link it came on,
        # and only while their session was shared back when it arrived.
        if data.get("link_id") == link_id and data.get("reply_ok") is True:
            out["reply"] = {"to": sender, "reply_to": m.get("id")}
    elif data.get("peer_msg_id"):
        out["peer_msg_id"] = data["peer_msg_id"]  # pass as reply_to to peer_send
    return out
