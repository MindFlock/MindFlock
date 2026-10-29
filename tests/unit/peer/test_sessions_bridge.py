"""The shared-session terminal bridge (backend.peer.sessions_bridge) with a
fake transport: a real pty pair stands in for the read-only tmux client, and
``send-keys`` is captured — so the tests show the peer's keystrokes go to the
pane and NEVER to the attach client (where tmux's prefix key would hand
them tmux itself)."""

from __future__ import annotations

import asyncio
import base64
import os
import tty

import pytest

from backend.peer import sessions_bridge as sb
from backend.peer import wire
from backend.web.core import guest_terminal

LID = "ab" * 16
OTHER = "cd" * 16
PEER = "peer:" + LID
OWNER = "00000000-0000-4000-8000-00000000000a"


@pytest.fixture(autouse=True)
def _keyboard(monkeypatch, tmp_path):
    """Every shared term takes a seat at the session's one keyboard
    (core.ownership): a fresh book per test, no owner file of the machine's."""
    from backend.web.core import ownership

    ownership._reset_for_tests()
    monkeypatch.setattr(ownership, "_path", lambda: str(tmp_path / "owners.json"))
    yield ownership
    ownership._reset_for_tests()


class FakeProc:
    """What pump_pty drives: ``fd`` (our master end), resize, terminate."""

    def __init__(self):
        self.fd, self.slave = os.openpty()
        tty.setraw(self.slave)  # bytes as they are, both ways
        os.set_blocking(self.slave, False)
        self.sizes = []
        self.terminated = False

    def setwinsize(self, rows, cols):
        self.sizes.append((rows, cols))

    def terminate(self, force=False):
        self.terminated = True
        for fd in (self.fd, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def emit(self, data: bytes):  # "tmux" draws something
        os.write(self.slave, data)

    async def emit_all(self, data: bytes):  # more than the pty buffer holds
        view = memoryview(data)
        while view:
            try:
                view = view[os.write(self.slave, view[:4096]) :]
            except BlockingIOError:
                await asyncio.sleep(0.005)

    def written(self) -> bytes:  # what reached the attach client
        try:
            return os.read(self.slave, 65536)
        except (BlockingIOError, OSError):
            return b""


class Wire:
    def __init__(self):
        self.pushes = []
        self.requests = []
        self.answers = {}

    async def push(self, link_id, op, p):
        wire.validate_push(op, p)  # what we send is strict
        self.pushes.append((link_id, op, p))

    async def request(self, link_id, op, p):
        wire.validate_request(op, p)
        self.requests.append((link_id, op, p))
        ans = self.answers.get(op, {"ok": True})
        if isinstance(ans, Exception):
            raise ans
        return ans

    def out(self, op="term_out"):
        return b"".join(
            base64.b64decode(p["data"]) for _l, o, p in self.pushes if o == op
        )


async def until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def run(coro):
    return asyncio.run(coro)


def sharer(w, procs, keys, **kw):
    bridge = sb.SessionsBridge(w.push, w.request)

    def spawn(name, rows, cols):
        p = FakeProc()
        p.name, p.dims = name, (rows, cols)
        procs.append(p)
        return p

    async def open_(title="api-fix", **more):
        return await bridge.open_mine(
            LID,
            title,
            "mf_api-fix",
            100,
            30,
            spawn=spawn,
            send_keys=lambda name, data: keys.append((name, data)),
            **{**kw, **more},
        )

    return bridge, open_


def test_output_streams_as_term_out_pushes():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        tid = await open_()
        assert procs[0].name == "mf_api-fix" and procs[0].dims == (30, 100)
        procs[0].emit(b"hello ")
        procs[0].emit(b"world")
        await until(lambda: w.out() == b"hello world")
        assert all(p["term_id"] == tid for _l, _o, p in w.pushes)
        assert all(l == LID for l, _o, _p in w.pushes)
        await bridge.close_mine(LID, tid, "closed")
        assert procs[0].terminated

    run(main())


def test_big_output_is_chunked_to_the_wire_cap():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        await open_()
        blob = os.urandom(wire.MAX_TERM_CHUNK * 2 + 100)
        await procs[0].emit_all(blob)
        await until(lambda: len(w.out()) == len(blob))
        assert w.out() == blob
        assert all(
            len(base64.b64decode(p["data"])) <= wire.MAX_TERM_CHUNK
            for _l, o, p in w.pushes
            if o == "term_out"
        )
        await bridge.close_all()

    run(main())


def test_keystrokes_go_to_send_keys_never_to_the_tmux_client():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        tid = await open_()
        # Ctrl+B (tmux's default prefix), then "c": on a writable client that
        # would open an unsandboxed shell window.
        for data in (b"\x02", b"c", b"ls\r"):
            bridge.on_push(
                LID,
                "term_in",
                {"term_id": tid, "data": base64.b64encode(data).decode()},
            )
        await until(lambda: b"".join(d for _n, d in keys) == b"\x02cls\r")
        assert {n for n, _d in keys} == {"mf_api-fix"}
        await asyncio.sleep(0.05)
        assert procs[0].written() == b""
        # The terminal's own answer to tmux's probe DOES go to the client.
        reply = b"\x1b[?1;2c"
        bridge.on_push(
            LID, "term_in", {"term_id": tid, "data": base64.b64encode(reply).decode()}
        )
        await until(lambda: procs[0].written() == reply)
        await bridge.close_all()

    run(main())


def test_budget_lock_drops_keystrokes():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys, may_type=lambda: False)
        tid = await open_()
        bridge.on_push(LID, "term_in", {"term_id": tid, "data": "aGk="})
        await asyncio.sleep(0.1)
        assert keys == []
        await bridge.close_all()

    run(main())


def test_resize_reaches_the_pty():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        tid = await open_()
        bridge.on_push(LID, "term_resize", {"term_id": tid, "cols": 132, "rows": 43})
        await until(lambda: procs[0].sizes == [(43, 132)])
        await bridge.close_all()

    run(main())


def test_close_says_term_end_with_the_reason():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        tid = await open_()
        await bridge.close_title(LID, "api-fix", "withdrawn")
        ends = [p for _l, o, p in w.pushes if o == "term_end"]
        assert ends == [{"term_id": tid, "reason": "withdrawn"}]
        assert bridge.mine_open(LID) == []

    run(main())


def test_one_term_per_link_and_title_a_new_open_replaces():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        first = await open_()
        second = await open_()
        assert first != second
        assert procs[0].terminated and not procs[1].terminated
        assert bridge.mine_open(LID) == ["api-fix"]
        ends = [p for _l, o, p in w.pushes if o == "term_end"]
        assert ends == [{"term_id": first, "reason": "replaced"}]
        await bridge.close_all()

    run(main())


def test_pushes_only_reach_their_own_link_and_direction():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        tid = await open_()
        # Another link naming my term id, or an unknown id: nothing.
        bridge.on_push(OTHER, "term_in", {"term_id": tid, "data": "aGk="})
        bridge.on_push(LID, "term_in", {"term_id": "f" * 16, "data": "aGk="})
        # term_out naming MY sharer term: not an opener term, dropped.
        bridge.on_push(LID, "term_out", {"term_id": tid, "data": "aGk="})
        await asyncio.sleep(0.1)
        assert keys == []
        assert not bridge.on_push(LID, "sessions_changed", {})
        await bridge.close_all()

    run(main())


def test_link_down_closes_everything_quietly():
    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        await open_()
        w.answers["term_open"] = {"ok": True, "term_id": "1" * 16}
        term, q = await bridge.attach_theirs(LID, "theirs", 80, 24)
        await bridge.close_link(LID, "disconnected")
        assert procs[0].terminated
        assert not [o for _l, o, _p in w.pushes if o == "term_end"]
        assert await q.get() is None
        assert term.reason == "disconnected"

    run(main())


# --------------------------------------------------------------------------- #
# The opener
# --------------------------------------------------------------------------- #
def test_opener_fans_out_and_the_last_window_closes():
    async def main():
        w = Wire()
        w.answers["term_open"] = {"ok": True, "term_id": "1" * 16}
        bridge = sb.SessionsBridge(w.push, w.request)
        t1, q1 = await bridge.attach_theirs(LID, "api-fix", 80, 24)
        t2, q2 = await bridge.attach_theirs(LID, "api-fix", 100, 30)
        assert t1 is t2
        assert [op for _l, op, _p in w.requests] == ["term_open"]
        assert w.requests[0][2] == {"title": "api-fix", "cols": 80, "rows": 24}
        bridge.on_push(LID, "term_out", {"term_id": "1" * 16, "data": "aGk="})
        assert q1.get_nowait() == b"hi" and q2.get_nowait() == b"hi"
        await bridge.type_theirs(t1, b"ls\r")
        await bridge.resize_theirs(t1, 120, 40)
        assert [(op, p) for _l, op, p in w.pushes] == [
            ("term_in", {"term_id": "1" * 16, "data": "bHMN"}),
            ("term_resize", {"term_id": "1" * 16, "cols": 120, "rows": 40}),
        ]
        await bridge.detach_theirs(t1, q1)
        assert [op for _l, op, _p in w.requests] == ["term_open"]
        await bridge.detach_theirs(t2, q2)
        assert w.requests[-1][1:] == ("term_close", {"term_id": "1" * 16})

    run(main())


def test_opener_term_end_ends_every_window():
    async def main():
        w = Wire()
        w.answers["term_open"] = {"ok": True, "term_id": "1" * 16}
        bridge = sb.SessionsBridge(w.push, w.request)
        term, q = await bridge.attach_theirs(LID, "api-fix", 80, 24)
        bridge.on_push(LID, "term_end", {"term_id": "1" * 16, "reason": "withdrawn"})
        assert q.get_nowait() is None and term.reason == "withdrawn"
        # A new window opens a new remote term.
        w.answers["term_open"] = {"ok": True, "term_id": "2" * 16}
        term2, _q = await bridge.attach_theirs(LID, "api-fix", 80, 24)
        assert term2 is not term and term2.term_id == "2" * 16

    run(main())


def test_opener_refused_open_leaves_nothing_behind():
    async def main():
        w = Wire()
        w.answers["term_open"] = RuntimeError("not shared")
        bridge = sb.SessionsBridge(w.push, w.request)
        with pytest.raises(RuntimeError):
            await bridge.attach_theirs(LID, "api-fix", 80, 24)
        w.answers["term_open"] = {"ok": True, "term_id": "1" * 16}
        term, _q = await bridge.attach_theirs(LID, "api-fix", 80, 24)
        assert term.term_id == "1" * 16

    run(main())


# --------------------------------------------------------------------------- #
# Presence (the pill on the sharer's rail)
# --------------------------------------------------------------------------- #
class Bus:
    def __init__(self):
        self.events = []

    def __call__(self, name, **kw):
        self.events.append((name, kw))


def test_focus_emits_like_a_guest_and_leaves_with_left_true():
    bus, now = Bus(), [1000.0]
    f = sb.PeerFocus(bus, clock=lambda: now[0])
    f.note(LID, "Bob", "api-fix")
    f.note(LID, "Bob", "api-fix")  # the heartbeat: no event
    f.note(LID, "Bob", "docs")
    f.leave(LID)
    f.leave(LID)  # already gone: nothing
    assert [e[0] for e in bus.events] == ["guest.focus_changed"] * 3
    joined, moved, left = (e[1] for e in bus.events)
    # Exactly guests' shape (core.guests._emit_focus) …
    assert joined == {
        "session": "api-fix",
        "old": "",
        "new": "api-fix",
        "data": {
            "link": "peer:" + LID,
            "label": "Bob",
            "title": "api-fix",
            "joined": True,
        },
    }
    assert moved["data"]["joined"] is False and moved["old"] == "api-fix"
    # … plus left: true (and its last title) when they are gone.
    assert left["data"] == {
        "link": "peer:" + LID,
        "label": "Bob",
        "title": "docs",
        "joined": False,
        "left": True,
    }
    assert left["new"] == "" and left["session"] == "docs"


def test_focus_sweep_keeps_an_open_terminal_and_drops_the_quiet():
    bus, now = Bus(), [1000.0]
    f = sb.PeerFocus(bus, clock=lambda: now[0])
    f.note(LID, "Bob", "api-fix")
    f.note(OTHER, "Ann", "docs")
    now[0] += sb.FOCUS_TTL_S + 1
    f.sweep(lambda lid: "api-fix" if lid == LID else "")
    assert f.of(LID)["title"] == "api-fix"
    assert f.of(OTHER) is None
    assert bus.events[-1][1]["data"]["left"] is True


def test_the_helpers_are_the_shared_guest_terminal_ones():
    """One discipline for every visitor's keystrokes (core.guest_terminal)."""
    term = sb.SharerTerm(link_id=LID, title="t", tmux_name="mf_t", push=None)
    assert term._spawn is guest_terminal.spawn_readonly_attach
    assert term._typist._send is guest_terminal.send_keys


# --------------------------------------------------------------------------- #
# one keyboard (core.ownership)
# --------------------------------------------------------------------------- #
def _type(bridge, tid, data: bytes):
    bridge.on_push(
        LID, "term_in", {"term_id": tid, "data": base64.b64encode(data).decode()}
    )


def test_a_peer_on_a_free_session_drives_under_their_own_name(_keyboard):
    own = _keyboard

    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys, peer_name="Mauricio")
        tid = await open_()
        assert own.driver("api-fix") == PEER
        row = own.row_fields("api-fix")
        assert row["driver_name"] == "Mauricio"
        assert row["viewers"][PEER]["name"] == "Mauricio"
        _type(bridge, tid, b"ls\r")
        await until(lambda: keys == [("mf_api-fix", b"ls\r")])
        assert own.viewers("api-fix")[PEER]["typing"] is True
        await bridge.close_mine(LID, tid)
        # closing the term gives the keyboard back and forgets the name
        assert own.driver("api-fix") == "" and own.viewers("api-fix") == {}
        assert own.name_of(PEER) == ""

    run(main())


def test_a_peer_watches_while_the_owner_drives_and_is_told_why(_keyboard):
    own = _keyboard

    async def main():
        w, procs, keys = Wire(), [], []
        mine = own.attach("api-fix", OWNER, "laptop")
        bridge, open_ = sharer(w, procs, keys, peer_name="Mauricio")
        tid = await open_()
        assert own.driver("api-fix") == OWNER
        assert set(own.viewers("api-fix")) == {OWNER, PEER}
        _type(bridge, tid, b"rm -rf ~\r")
        await until(lambda: b"you can type once they let go" in w.out())
        notice = w.out()
        # drawn on the bottom row with the cursor saved and restored
        assert notice.startswith(b"\x1b7\x1b[999;1H\x1b[2K") and notice.endswith(
            b"\x1b8"
        )
        # the terminal's answer to tmux's probe still reaches the client
        _type(bridge, tid, b"\x1b[?1;2c")
        await until(lambda: procs[0].written() == b"\x1b[?1;2c")
        _type(bridge, tid, b"again")
        await asyncio.sleep(0.1)
        assert keys == []
        assert w.out() == notice  # the notice does not repeat every keystroke
        # the owner closes their terminal: the peer may type (and drives)
        own.detach(mine)
        _type(bridge, tid, b"ok")
        await until(lambda: keys == [("mf_api-fix", b"ok")])
        assert own.driver("api-fix") == PEER
        await bridge.close_all()

    run(main())


def test_a_peer_is_told_when_the_owner_takes_the_keyboard(_keyboard):
    own = _keyboard

    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys, peer_name="Mauricio")
        tid = await open_()
        assert own.driver("api-fix") == PEER
        own.take("api-fix", OWNER, "laptop")
        await until(lambda: b"took the keyboard" in w.out())
        _type(bridge, tid, b"x")
        await asyncio.sleep(0.1)
        assert keys == []
        await bridge.close_all()
        assert own.driver("api-fix") == OWNER  # the peer leaving takes nothing

    run(main())


def test_the_bridge_names_the_peer_as_the_link_does(monkeypatch, _keyboard):
    monkeypatch.setattr(sb, "_peer_name", lambda lid: "Pat" if lid == LID else "")

    async def main():
        w, procs, keys = Wire(), [], []
        bridge, open_ = sharer(w, procs, keys)
        await open_()
        assert _keyboard.row_fields("api-fix")["driver_name"] == "Pat"
        await bridge.close_all()

    run(main())
