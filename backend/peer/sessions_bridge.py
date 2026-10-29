"""Shared sessions' terminals over a peer link (docs/peer-link.md, "Shared
sessions — CONTRACT").

Two halves, one per side of a link, both owned by the PeerService:

* **The sharer** (my session, their keyboard): :class:`SharerTerm` attaches a
  per-term tmux client to the shared session's pane and streams it over the
  wire (``term_out`` pushes); the peer's keystrokes (``term_in``) and resizes
  (``term_resize``) come back. The PTY plumbing is the web terminal's own
  (:func:`backend.web.core.terminal.pump_pty`) driven through
  :class:`_WireSocket`, a websocket-shaped adapter over the wire.

  The attach client is **read-only** (``tmux attach-session -r``) and the
  peer's keystrokes go into the pane with ``tmux send-keys -l`` — straight to
  the program in the pane, never through tmux's key table. On a writable
  client the prefix key would hand the peer tmux itself (``prefix c`` opens a
  new window running an UNSANDBOXED shell, ``prefix :`` runs ``run-shell``):
  a gated share's sandbox would be one keystroke deep. Only the terminal's
  own answers to tmux's probes (:func:`terminal.is_terminal_reply`) reach the
  client, as they do for any terminal. Both halves live in
  :mod:`backend.web.core.guest_terminal`, shared with guest links' terminals.

  The peer types under the session's ONE keyboard
  (:mod:`backend.web.core.ownership`), as the person ``peer:<link id>``
  named after the peer: they drive only when nobody else does, and a
  refused keystroke is dropped with a notice drawn on their bottom row.

* **The opener** (their session, my rail): :class:`OpenerTerm` is one remote
  terminal per (link, title), fanned out to every browser socket on this
  side that opened the row; the last one to leave closes it (``term_close``).

:class:`PeerFocus` is the sharer's presence book: which of my shared sessions
each peer has open or selected, emitted as ``guest.focus_changed`` exactly
the way :func:`backend.web.core.guests.note_focus` does (``link`` is
``"peer:<link id>"``), plus ``left: true`` when they are gone.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import time
from typing import Awaitable, Callable, Dict, Optional, Set, Tuple

from backend.peer import wire
from backend.web.core.guest_terminal import Typist, send_keys, spawn_readonly_attach

__all__ = [
    "SessionsBridge",
    "SharerTerm",
    "OpenerTerm",
    "PeerFocus",
    "chunks",
    "FOCUS_TTL_S",
]

_log = logging.getLogger("mindflock.peer")

#: Output is gathered this long before a ``term_out`` goes out: a busy agent
#: redraw is dozens of tiny PTY reads, one frame each would flood the link.
COALESCE_S = 0.02
#: Output held for a slow link before the pump waits for it (backpressure).
MAX_BUFFERED = 1 << 20
#: How long a peer's focus report stands with no terminal open (their page
#: re-reports every 30 s while the row stays selected) — core.guests.FOCUS_TTL_S.
FOCUS_TTL_S = 90.0
#: Local browser sockets on one opened row (the split view, a second tab).
MAX_SINKS = 8

Push = Callable[[str, str, dict], Awaitable[None]]
Request = Callable[[str, str, dict], Awaitable[dict]]


def chunks(data: bytes):
    """``data`` as base64 strings of at most :data:`wire.MAX_TERM_CHUNK` raw
    bytes each — the ``data`` of ``term_out`` / ``term_in``."""
    for i in range(0, len(data), wire.MAX_TERM_CHUNK):
        yield base64.b64encode(data[i : i + wire.MAX_TERM_CHUNK]).decode("ascii")


def _term_id() -> str:
    return secrets.token_hex(8)


def _peer_name(link_id: str) -> str:
    """The link's peer name as the server's PeerService knows it (``""``
    when there is none — tests, a bridge of its own)."""
    try:
        from backend.peer import service

        svc = service._SERVICE
        return svc.peer_name(link_id) if svc is not None else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# The sharer: my session's pane, streamed to them
# --------------------------------------------------------------------------- #
def _notice_bytes(message: str) -> bytes:
    """A notice for the peer, drawn on their terminal's bottom row: the opener
    renders nothing but the byte stream (no text frames cross the link), so
    the notice is terminal output — saved cursor and attributes, the last
    row (``CUP 999`` clamps to it) cleared and written in reverse video, the
    cursor restored, so tmux's next redraw lands where tmux thinks it does.
    The pane's next redraw of that row paints over it."""
    text = "".join(ch for ch in message if ch.isprintable())[:200]
    return ("\x1b7\x1b[999;1H\x1b[2K\x1b[7m %s \x1b[0m\x1b8" % text).encode("utf-8")


class _WireSocket:
    """What :func:`pump_pty` takes for a websocket: ``receive()`` yields the
    peer's input as websocket messages, ``send_bytes()`` gathers output into
    ``term_out`` pushes. Of the text frames, only the read-only notice crosses
    the link, drawn as output (:func:`_notice_bytes`)."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]]) -> None:
        self._send = send
        self._in: "asyncio.Queue[dict]" = asyncio.Queue()
        self._buf = bytearray()
        self._flusher: Optional[asyncio.Task] = None
        self.closed = False
        self.failed = False  # a push failed: the link is gone

    # -- the peer's side -------------------------------------------------
    def feed(self, data: bytes) -> None:
        if not self.closed:
            self._in.put_nowait({"type": "websocket.receive", "bytes": data})

    def resize(self, cols: int, rows: int) -> None:
        if not self.closed:
            frame = json.dumps({"type": "resize", "cols": cols, "rows": rows})
            self._in.put_nowait({"type": "websocket.receive", "text": frame})

    def disconnect(self) -> None:
        self._in.put_nowait({"type": "websocket.disconnect"})

    # -- what pump_pty calls ----------------------------------------------
    async def receive(self) -> dict:
        return await self._in.get()

    async def send_bytes(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionError("term closed")
        self._buf += data
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.ensure_future(self._flush_later())
        if len(self._buf) > MAX_BUFFERED:
            await asyncio.shield(self._flusher)

    async def send_text(self, text: str) -> None:
        """A text frame: the read-only notice is drawn into the stream
        (:func:`_notice_bytes`); the driver frames have no place there."""
        try:
            frame = json.loads(text)
        except (TypeError, ValueError):
            return
        if isinstance(frame, dict) and frame.get("type") == "readonly":
            message = str(frame.get("message") or "")
            if message:
                await self.send_bytes(_notice_bytes(message))

    async def close(self) -> None:
        await self.flush()
        self.closed = True

    async def _flush_later(self) -> None:
        await asyncio.sleep(COALESCE_S)
        await self.flush()

    async def flush(self) -> None:
        while self._buf and not self.closed:
            data = bytes(self._buf[: wire.MAX_TERM_CHUNK])
            del self._buf[: len(data)]
            try:
                await self._send(data)
            except Exception:  # noqa: BLE001 — the link went: end the pump
                self.closed = self.failed = True
                self._buf.clear()
                self.disconnect()


class SharerTerm:
    """One peer's terminal on one of my shared sessions.

    It holds a seat in the session's one keyboard (:mod:`backend.web.core.
    ownership`) as the person ``peer:<link id>``, named ``peer_name``: the
    peer drives only when nobody else does (or the driver let go), the
    owner's rail shows the peer's name as driver or viewer, and the seat
    goes with the term. A refused keystroke is dropped with a notice drawn
    into the peer's stream (:func:`_notice_bytes`)."""

    def __init__(
        self,
        *,
        link_id: str,
        title: str,
        tmux_name: str,
        push: Push,
        may_type: Callable[[], bool] = lambda: True,
        spawn=spawn_readonly_attach,
        send_keys=send_keys,
        peer_name: str = "",
        on_closed: Optional[Callable[["SharerTerm"], None]] = None,
    ) -> None:
        self.link_id = link_id
        self.title = title
        self.tmux_name = tmux_name
        self.term_id = _term_id()
        self.person = "peer:" + link_id
        self.peer_name = peer_name or "peer"
        self._push = push
        self._may_type = may_type
        self._spawn = spawn
        self._typist = Typist(tmux_name, send_keys)
        self._on_closed = on_closed
        self._sock = _WireSocket(self._out)
        self._task: Optional[asyncio.Task] = None
        self._seat: Optional[int] = None
        self._driving = False
        self._reason = "closed"
        self._notify = True
        self.closed = False

    async def _out(self, data: bytes) -> None:
        for b64 in chunks(data):
            await self._push(
                self.link_id, "term_out", {"term_id": self.term_id, "data": b64}
            )

    def start(self, cols: int, rows: int) -> None:
        """Attach (raises when tmux can't), take a seat at the keyboard and
        start pumping."""
        proc = self._spawn(self.tmux_name, rows, cols)
        self._sit()
        self._typist.start()
        self._task = asyncio.ensure_future(self._run(proc))

    # -- the one keyboard ----------------------------------------------------
    def _sit(self) -> None:
        from backend.web.core import ownership

        loop = asyncio.get_running_loop()

        def told(frame: dict) -> None:  # any thread
            with contextlib.suppress(RuntimeError):  # loop closing
                loop.call_soon_threadsafe(self._driver_changed, frame)

        self._seat = ownership.attach(
            self.title, self.person, self.peer_name, notify=told, name=self.peer_name
        )
        self._driving = bool(ownership.mode(self.title, self.person)["driving"])

    def _driver_changed(self, frame: dict) -> None:
        """The keyboard moved: the peer is told when it moves away from them."""
        was, self._driving = self._driving, bool(frame.get("driving"))
        if was and not self._driving and not self.closed:
            self._tell(
                "%s took the keyboard — you are watching"
                % (frame.get("driver_name") or "Someone")
            )

    def _tell(self, message: str) -> None:
        frame = json.dumps({"type": "readonly", "message": message})
        asyncio.ensure_future(self._sock.send_text(frame))

    def _keyboard(self) -> Tuple[bool, str]:
        """Whether the peer may type now (:func:`ownership.may_type`) — and
        the notice for them when not (``""`` between repeats)."""
        from backend.web.core import ownership

        if self._seat is None:
            return True, ""
        ok, notice = ownership.may_type(self._seat)
        if ok:
            self._driving = True
            ownership.note_input(self.title, self.person, self.peer_name)
            return True, ""
        if not notice:
            return False, ""
        who = ownership.mode(self.title, self.person)["driver_name"] or "Someone"
        return False, "%s is driving — you can type once they let go" % who

    def _divert(self, data) -> str:
        """pump_pty's gate: the peer's keystrokes never reach the (read-only)
        attach client — they are queued for ``send-keys``. Dropped while the
        session is budget-locked, like the web terminal's, or while somebody
        else drives (with a notice: pump_pty sends it as a ``readonly`` text
        frame, which :class:`_WireSocket` draws)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data or not self._may_type():
            return ""
        ok, notice = self._keyboard()
        if ok:
            self._typist.type(data)
        return notice

    async def _run(self, proc) -> None:
        from backend.web.core.terminal import pump_pty

        try:
            await pump_pty(self._sock, proc, allow_input=True, gate=self._divert)
        except Exception as err:  # noqa: BLE001
            _log.warning("peer: shared terminal ended: %s", type(err).__name__)
        finally:
            await self._finish()

    async def _finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._typist.close()
        if self._seat is not None:
            from backend.web.core import ownership

            with contextlib.suppress(Exception):
                ownership.detach(self._seat)
            self._seat = None
        if self._notify:
            with contextlib.suppress(Exception):
                await self._sock.flush()
        self._sock.closed = True
        if self._notify and not self._sock.failed:
            with contextlib.suppress(Exception):
                await self._push(
                    self.link_id,
                    "term_end",
                    {
                        "term_id": self.term_id,
                        "reason": wire.clip_text(self._reason, wire.MAX_BYE),
                    },
                )
        if self._on_closed is not None:
            with contextlib.suppress(Exception):
                self._on_closed(self)

    def input(self, data: bytes) -> None:
        self._sock.feed(data)

    def resize(self, cols: int, rows: int) -> None:
        self._sock.resize(cols, rows)

    async def close(self, reason: str = "closed", notify: bool = True) -> None:
        """End the term. ``notify=False`` when the link is gone (nobody to
        tell)."""
        if self.closed:
            return
        self._reason = reason
        self._notify = notify
        self._sock.disconnect()
        task = self._task
        if task is None:
            await self._finish()
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(task), 3.0)
        if not self.closed:
            task.cancel()
            await self._finish()


# --------------------------------------------------------------------------- #
# The opener: their session, on my rail
# --------------------------------------------------------------------------- #
class OpenerTerm:
    """Their session's one terminal on this side, fanned out to every local
    browser socket that has its row open (each sink is a queue of ``bytes``,
    ``None`` = the term ended)."""

    def __init__(self, link_id: str, title: str) -> None:
        self.link_id = link_id
        self.title = title
        self.term_id = ""
        self.sinks: Set["asyncio.Queue"] = set()
        self.reason = ""
        self.ready = asyncio.Event()

    def output(self, data: bytes) -> None:
        for q in list(self.sinks):
            q.put_nowait(data)

    def end(self, reason: str) -> None:
        self.reason = reason or "closed"
        for q in list(self.sinks):
            q.put_nowait(None)
        self.sinks.clear()


# --------------------------------------------------------------------------- #
# Presence: which of my shared sessions each peer is on
# --------------------------------------------------------------------------- #
class PeerFocus:
    """The sharer's side of the pill: ``link_id -> {title, ts, label}``.
    A focus stands while the peer has a terminal open on the link or kept
    reporting within :data:`FOCUS_TTL_S`."""

    def __init__(self, emit: Callable[..., None], clock=time.time) -> None:
        self._emit = emit
        self._clock = clock
        self._focus: Dict[str, dict] = {}

    def of(self, link_id: str) -> Optional[dict]:
        rec = self._focus.get(link_id)
        return dict(rec) if rec else None

    def note(self, link_id: str, label: str, title: str) -> None:
        before = self._focus.get(link_id)
        self._focus[link_id] = {"title": title, "ts": self._clock(), "label": label}
        if before is None or before["title"] != title or before["label"] != label:
            self._send(
                link_id,
                label,
                before["title"] if before else "",
                title,
                joined=before is None,
            )

    def leave(self, link_id: str) -> None:
        rec = self._focus.pop(link_id, None)
        if rec is not None:
            self._send(link_id, rec["label"], rec["title"], "", joined=False)

    def sweep(self, holding: Callable[[str], str]) -> None:
        """Forget stale focus. ``holding(link_id)`` names a title the peer
        still has a terminal open on (``""``: none) — the focus moves there
        instead of going."""
        now = self._clock()
        for lid, rec in list(self._focus.items()):
            if now - rec["ts"] <= FOCUS_TTL_S:
                continue
            held = holding(lid)
            if held:
                self.note(lid, rec["label"], held)
            else:
                self.leave(lid)

    def _send(self, link_id: str, label: str, old: str, new: str, *, joined: bool):
        """``guest.focus_changed`` in guests' own shape; a peer that left
        carries its last title and ``left: true`` (the pill goes)."""
        data = {
            "link": "peer:" + link_id,
            "label": label,
            "title": new or old,
            "joined": joined,
        }
        if not new:
            data["left"] = True
        with contextlib.suppress(Exception):
            self._emit(
                "guest.focus_changed",
                session=new or old,
                old=old,
                new=new,
                data=data,
            )


# --------------------------------------------------------------------------- #
# Both halves, per link
# --------------------------------------------------------------------------- #
class SessionsBridge:
    """Every shared-session terminal on this side, both directions. ``push``
    and ``request`` reach the peer (the service's transport); nothing here
    decides what is shared — the service checks that before calling in."""

    def __init__(self, push: Push, request: Request) -> None:
        self._push = push
        self._request = request
        # sharer side: (link, term_id) -> term; (link, title) -> term_id
        self._mine: Dict[Tuple[str, str], SharerTerm] = {}
        self._mine_by_title: Dict[Tuple[str, str], str] = {}
        # opener side: (link, title) -> term; (link, term_id) -> term
        self._theirs: Dict[Tuple[str, str], OpenerTerm] = {}
        self._theirs_by_id: Dict[Tuple[str, str], OpenerTerm] = {}
        self._lock = asyncio.Lock()

    # -- sharer -----------------------------------------------------------
    async def open_mine(
        self,
        link_id: str,
        title: str,
        tmux_name: str,
        cols: int,
        rows: int,
        **term_kw,
    ) -> str:
        """Open a terminal on my ``title`` for the link's peer → term_id. One
        per (link, title): a new open replaces the old one (their page
        reloaded)."""
        old = self._mine_by_title.get((link_id, title))
        if old:
            term = self._mine.get((link_id, old))
            if term is not None:
                await term.close("replaced")
        term_kw.setdefault("peer_name", _peer_name(link_id))
        term = SharerTerm(
            link_id=link_id,
            title=title,
            tmux_name=tmux_name,
            push=self._push,
            on_closed=self._forget_mine,
            **term_kw,
        )
        term.start(cols, rows)
        self._mine[(link_id, term.term_id)] = term
        self._mine_by_title[(link_id, title)] = term.term_id
        return term.term_id

    def _forget_mine(self, term: SharerTerm) -> None:
        self._mine.pop((term.link_id, term.term_id), None)
        if self._mine_by_title.get((term.link_id, term.title)) == term.term_id:
            self._mine_by_title.pop((term.link_id, term.title), None)

    def mine_open(self, link_id: str) -> list:
        """Titles the link's peer has a terminal open on."""
        return [t for (lid, t) in self._mine_by_title if lid == link_id]

    async def close_mine(self, link_id: str, term_id: str, reason: str = "closed"):
        term = self._mine.get((link_id, term_id))
        if term is not None:
            await term.close(reason)
        return term

    async def close_title(self, link_id: str, title: str, reason: str) -> None:
        """I stopped sharing ``title``: end their terminal on it."""
        tid = self._mine_by_title.get((link_id, title))
        if tid:
            await self.close_mine(link_id, tid, reason)

    # -- opener -----------------------------------------------------------
    async def attach_theirs(
        self, link_id: str, title: str, cols: int, rows: int
    ) -> Tuple[OpenerTerm, "asyncio.Queue"]:
        """A local socket opened their row: join its terminal (opening it on
        their side first) → (term, sink). Raises what the peer refused."""
        q: "asyncio.Queue" = asyncio.Queue()
        async with self._lock:
            term = self._theirs.get((link_id, title))
            if term is not None and len(term.sinks) >= MAX_SINKS:
                raise ValueError("too many windows on this session")
            if term is None:
                term = OpenerTerm(link_id, title)
                self._theirs[(link_id, title)] = term
                term.sinks.add(q)
                try:
                    res = await self._request(
                        link_id,
                        "term_open",
                        {"title": title, "cols": cols, "rows": rows},
                    )
                except BaseException:
                    self._theirs.pop((link_id, title), None)
                    raise
                term.term_id = str(res.get("term_id") or "")
                self._theirs_by_id[(link_id, term.term_id)] = term
                term.ready.set()
                return term, q
            term.sinks.add(q)
            return term, q

    async def detach_theirs(self, term: OpenerTerm, q: "asyncio.Queue") -> None:
        """A local socket left; the last one closes the remote terminal."""
        async with self._lock:
            term.sinks.discard(q)
            if term.sinks or self._theirs.get((term.link_id, term.title)) is not term:
                return
            self._drop_theirs(term)
        if term.term_id:
            with contextlib.suppress(Exception):
                await self._request(
                    term.link_id, "term_close", {"term_id": term.term_id}
                )

    def _drop_theirs(self, term: OpenerTerm) -> None:
        self._theirs.pop((term.link_id, term.title), None)
        self._theirs_by_id.pop((term.link_id, term.term_id), None)

    async def type_theirs(self, term: OpenerTerm, data: bytes) -> None:
        for b64 in chunks(data):
            await self._push(
                term.link_id, "term_in", {"term_id": term.term_id, "data": b64}
            )

    async def resize_theirs(self, term: OpenerTerm, cols: int, rows: int) -> None:
        await self._push(
            term.link_id,
            "term_resize",
            {"term_id": term.term_id, "cols": cols, "rows": rows},
        )

    # -- pushes from the peer ----------------------------------------------
    def on_push(self, link_id: str, op: str, p: dict) -> bool:
        """A terminal push (already schema-checked). Only ever acts on a term
        of THIS link in the right direction; anything else is dropped.
        Returns whether it was a terminal op."""
        tid = str(p.get("term_id") or "")
        if op in ("term_in", "term_resize"):
            term = self._mine.get((link_id, tid))
            if term is not None and not term.closed:
                if op == "term_in":
                    term.input(base64.b64decode(p["data"]))
                else:
                    term.resize(int(p["cols"]), int(p["rows"]))
            return True
        if op in ("term_out", "term_end"):
            theirs = self._theirs_by_id.get((link_id, tid))
            if theirs is not None:
                if op == "term_out":
                    theirs.output(base64.b64decode(p["data"]))
                else:
                    self._drop_theirs(theirs)
                    theirs.end(str(p.get("reason") or "closed"))
            return True
        return False

    # -- the link went -------------------------------------------------------
    async def close_link(
        self, link_id: str, reason: str = "disconnected", notify: bool = False
    ) -> None:
        """End every terminal of the link, both directions. ``notify``: tell
        the peer its terminals on my sessions ended (the connection is still
        up — a safety number to compare again); by default nobody is told
        (the connection is gone)."""
        for (lid, _tid), term in list(self._mine.items()):
            if lid == link_id:
                await term.close(reason, notify=notify)
        for (lid, _title), term in list(self._theirs.items()):
            if lid == link_id:
                self._drop_theirs(term)
                term.end(reason)

    async def close_all(self) -> None:
        for lid in {k[0] for k in self._mine} | {k[0] for k in self._theirs}:
            await self.close_link(lid, "shutdown")
