"""Two real PeerTransports on 127.0.0.1, plus raw adversarial clients and a
MITM server. Timeouts are shortened per test; rate limits use fake clocks."""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import ssl
import time

import pytest

from backend.peer import identity, invite, transport, wire
from backend.peer.transport import (
    IdentityMismatch,
    PairingFailed,
    PeerOpError,
    PeerUnavailable,
)
from tests.unit.peer.conftest import (
    authed_raw,
    b64,
    closed_by_peer,
    eventually,
    paired,
    raw_auth,
    raw_hello,
    raw_pair,
    tls_connect,
)

SAS_RE = re.compile(r"^\d{3}-\d{3}-\d{3}-\d$")


def _frame(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


def _forged_code(code: str, *, port=None, invite_id=None, secret=None, fp=None) -> str:
    info = invite.parse_code(code)
    return invite.encode_code(
        info.host,
        port or info.port,
        bytes.fromhex(invite_id or info.invite_id),
        secret or info.secret,
        fp or info.server_fp,
    )


class Attacker:
    """A TLS 1.3 server with its own key that records every application byte
    a client sends it, and baits the client with a plausible hello."""

    def __init__(self, ident):
        self.ctx = transport.server_ssl_context(ident)
        self.accepts = 0
        self.received = bytearray()
        self.server = None

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._cb, "127.0.0.1", port)
        return self.server.sockets[0].getsockname()[1]

    async def _cb(self, reader, writer):
        self.accepts += 1
        try:
            await writer.start_tls(self.ctx)
            writer.write(
                wire.encode(
                    {"t": "hello", "v": 1, "nonce": b64(os.urandom(32)), "name": "evil"}
                )
            )
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.received += data
        except Exception:
            pass
        finally:
            writer.transport.abort()

    def stop(self):
        self.server.close()


# -- pairing -------------------------------------------------------------------------


async def test_pair_happy_path(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    la, lb = a.store.get(link.link_id), b.store.get(link.link_id)
    assert re.match(r"^[0-9a-f]{32}$", link.link_id)
    assert (la.role, lb.role) == ("listener", "dialer")
    assert la.peer_pub == b.ident.pub.hex() and lb.peer_pub == a.ident.pub.hex()
    assert la.peer_addr is None and lb.peer_addr == f"127.0.0.1:{a.port}"
    assert (la.peer_name, lb.peer_name) == ("bob", "alice")
    assert la.sas == lb.sas and SAS_RE.match(la.sas)
    assert [l.link_id for l in a.handler.added] == [link.link_id]
    assert [l.link_id for l in b.handler.added] == [link.link_id]
    assert (link.link_id, True) in a.handler.states and (
        link.link_id,
        True,
    ) in b.handler.states
    assert a.invites.active() == []


async def test_bidirectional_requests(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    assert await b.t.request(link.link_id, "status", {}) == {
        "shared": True,
        "agent": "none",
        "name": "x",
    }
    assert await a.t.request(link.link_id, "msg", {"msg_id": "m1", "text": "hi"}) == {
        "accepted": True
    }
    assert (await b.t.request(link.link_id, "read_file", {"path": "a.txt"}))[
        "content"
    ] == "hi"
    assert await a.t.request(link.link_id, "list_files", {}) == {
        "files": ["a.txt"],
        "truncated": False,
    }
    assert (await b.t.request(link.link_id, "diff", {"max_chars": 1000}))[
        "diff"
    ] == "+hi\n"
    assert [c[1] for c in a.handler.calls] == ["status", "read_file", "diff"]
    assert [c[1:] for c in b.handler.calls] == [
        ("msg", {"msg_id": "m1", "text": "hi"}),
        ("list_files", {}),
    ]
    assert a.handler.links[0].role == "listener" and b.handler.links[0].role == "dialer"


async def test_many_concurrent_requests_multiplex(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)

    async def slow(link_, op, p):
        await asyncio.sleep(0.01 * (int(p["path"]) % 5))
        return {
            "path": p["path"],
            "size": 1,
            "encoding": "utf-8",
            "content": p["path"],
            "truncated": False,
        }

    a.handler.behavior = slow
    results = await asyncio.gather(
        *(b.t.request(link.link_id, "read_file", {"path": str(i)}) for i in range(30))
    )
    assert [r["content"] for r in results] == [str(i) for i in range(30)]


async def test_wrong_secret_denied(make_node):
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    code = a.code()
    with pytest.raises(PairingFailed, match="refused"):
        await b.t.pair(_forged_code(code, secret=b"\x00" * 20))
    assert a.store.list() == [] and b.store.list() == []
    # the failure counted, but the invite is still usable by its holder
    assert len(a.invites.active()) == 1
    await b.t.pair(code)


async def test_code_reuse_denied(make_node):
    a, b, c = make_node("alice"), make_node("bob"), make_node("carol")
    await a.listen()
    code = a.code()
    await b.t.pair(code)
    with pytest.raises(PairingFailed):
        await c.t.pair(code)
    with pytest.raises(PairingFailed):
        await b.t.pair(code)
    assert len(a.store.list()) == 1


async def test_expired_code_denied(make_node):
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    code = a.code(ttl_s=60)
    a.clock.advance(61)
    with pytest.raises(PairingFailed):
        await b.t.pair(code)
    assert a.store.list() == []


async def test_five_failures_destroy_invite(make_node):
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    code = a.code()
    for _ in range(5):
        with pytest.raises(PairingFailed):
            await b.t.pair(_forged_code(code, secret=os.urandom(20)))
    assert a.invites.active() == []
    with pytest.raises(PairingFailed):
        await b.t.pair(code)


async def test_unknown_invite_id_denied(make_node):
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    code = a.code()
    with pytest.raises(PairingFailed):
        await b.t.pair(_forged_code(code, invite_id="11" * 8))
    assert len(a.invites.active()) == 1


async def test_denied_is_generic(make_node, tmp_path):
    a = make_node("alice")
    await a.listen()
    ident = identity.load_or_create(str(tmp_path / "raw"))
    replies = []
    code = a.code()
    for bad in (
        _forged_code(code, secret=b"\x01" * 20),
        _forged_code(code, invite_id="22" * 8),
    ):
        raw, reply = await raw_pair(a, ident, code=bad)
        replies.append(reply)
        assert await closed_by_peer(raw.reader)
    raw, reply = await raw_auth(a, ident, "ab" * 16)  # unknown link
    replies.append(reply)
    assert replies == [{"t": "denied"}] * 3


async def test_bad_signature_on_pair_denied(make_node, tmp_path):
    a = make_node("alice")
    await a.listen()
    ident = identity.load_or_create(str(tmp_path / "raw"))
    other = identity.load_or_create(str(tmp_path / "other"))
    info = invite.parse_code(a.code())
    raw = await raw_hello(a.port)
    tr = transport.pair_transcript(raw.server_pub, raw.nonce, ident.pub)
    await raw.send(
        {
            "t": "pair",
            "v": 1,
            "invite_id": info.invite_id,
            "pub": ident.pub.hex(),
            "name": "x",
            "proof": hmac.new(info.secret, tr, hashlib.sha256).hexdigest(),
            "sig": other.sign(tr).hex(),  # signed by a key other than the claimed pub
        }
    )
    assert await raw.recv() == {"t": "denied"}
    assert a.store.list() == []


async def test_proof_bound_to_server_key_and_nonce(make_node, tmp_path):
    """A proof computed over a different server key (relay) or nonce fails."""
    a = make_node("alice")
    await a.listen()
    ident = identity.load_or_create(str(tmp_path / "raw"))
    info = invite.parse_code(a.code())
    raw = await raw_hello(a.port)
    wrong = transport.pair_transcript(b"\x00" * 32, raw.nonce, ident.pub)
    await raw.send(
        {
            "t": "pair",
            "v": 1,
            "invite_id": info.invite_id,
            "pub": ident.pub.hex(),
            "name": "x",
            "proof": hmac.new(info.secret, wrong, hashlib.sha256).hexdigest(),
            "sig": ident.sign(wrong).hex(),
        }
    )
    assert await raw.recv() == {"t": "denied"}


async def test_concurrent_pairing_on_one_invite(make_node):
    a = make_node("alice")
    joiners = [make_node(f"j{i}") for i in range(4)]
    await a.listen()
    code = a.code()
    results = await asyncio.gather(
        *(j.t.pair(code) for j in joiners), return_exceptions=True
    )
    wins = [r for r in results if not isinstance(r, BaseException)]
    assert len(wins) == 1
    assert all(
        isinstance(r, PairingFailed) for r in results if isinstance(r, BaseException)
    )
    assert [l.link_id for l in a.store.list()] == [wins[0].link_id]


async def test_malformed_code(make_node):
    b = make_node("bob")
    with pytest.raises(ValueError):
        await b.t.pair("mfp1:nope-nope")


async def test_pair_unreachable(make_node):
    a, b = make_node("alice"), make_node("bob")
    port = await a.listen()
    code = a.code()
    await a.t.stop_listener()
    await asyncio.sleep(0.05)
    with pytest.raises(PairingFailed, match="cannot reach"):
        await b.t.pair(code)
    assert port


# -- MITM ------------------------------------------------------------------------------


async def test_mitm_on_pairing_sends_nothing(make_node, tmp_path):
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    code = a.code()  # carries alice's fingerprint
    evil = Attacker(identity.load_or_create(str(tmp_path / "evil")))
    evil_port = await evil.start()
    try:
        with pytest.raises(IdentityMismatch, match="possible MITM"):
            await b.t.pair(_forged_code(code, port=evil_port))
        await asyncio.sleep(0.1)
        assert evil.accepts == 1
        assert bytes(evil.received) == b""  # no proof, no sig, no pub, nothing
    finally:
        evil.stop()
    assert len(a.invites.active()) == 1 and b.store.list() == []


async def test_mitm_on_reconnect_sends_nothing(make_node, tmp_path):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    port = a.port
    await a.t.close()
    assert await eventually(lambda: not b.t.is_connected(link.link_id))
    evil = Attacker(identity.load_or_create(str(tmp_path / "evil")))
    await evil.start(port)
    try:
        assert await eventually(lambda: evil.accepts >= 2, timeout=5)
        assert bytes(evil.received) == b""
        assert not b.t.is_connected(link.link_id)
    finally:
        evil.stop()


# -- authentication ----------------------------------------------------------------------


async def test_reconnect_after_drop(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    b.t._conns[link.link_id].writer.transport.abort()
    assert await eventually(lambda: (link.link_id, False) in b.handler.states)
    assert await eventually(
        lambda: a.t.is_connected(link.link_id) and b.t.is_connected(link.link_id)
    )
    assert await b.t.request(link.link_id, "status", {})
    assert await a.t.request(link.link_id, "status", {})


async def test_reconnect_after_listener_restart(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    port = a.port
    await a.t.close()
    assert await eventually(lambda: not b.t.is_connected(link.link_id))
    a2 = make_node("alice", reuse=a)
    await a2.t.start_listener("127.0.0.1", port)
    assert await eventually(
        lambda: a2.t.is_connected(link.link_id) and b.t.is_connected(link.link_id),
        timeout=5,
    )
    assert await a2.t.request(link.link_id, "status", {})


async def test_dialer_restart_reconnects_from_store(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    await b.t.close()
    b2 = make_node("bob", reuse=b)
    await b2.t.start()
    assert await eventually(
        lambda: b2.t.is_connected(link.link_id) and a.t.is_connected(link.link_id),
        timeout=5,
    )
    assert await a.t.request(link.link_id, "status", {})


async def test_revoked_link_denied(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    await b.t.close()
    assert await a.t.unlink(link.link_id)
    raw, reply = await raw_auth(a, b.ident, link.link_id)
    assert reply == {"t": "denied"}
    assert await closed_by_peer(raw.reader)


async def test_link_removed_from_store_closes_live_connection(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    a.store.remove(link.link_id)  # e.g. CLI unlink in another process
    with pytest.raises((PeerUnavailable, PeerOpError)):
        await b.t.request(link.link_id, "status", {}, timeout=2)
    assert await eventually(lambda: not a.t.is_connected(link.link_id))
    await asyncio.sleep(0.3)  # dialer keeps retrying, but is denied
    assert not a.t.is_connected(link.link_id)
    assert a.handler.calls == []


async def test_valid_link_id_wrong_key_denied(make_node, tmp_path):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    stranger = identity.load_or_create(str(tmp_path / "stranger"))
    raw, reply = await raw_auth(a, stranger, link.link_id)
    assert reply == {"t": "denied"}
    assert b.t.is_connected(link.link_id)  # the real connection is untouched


async def test_dialer_link_cannot_be_used_to_auth_inbound(make_node):
    """A link where we are the dialer is never accepted on our listener."""
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    await b.listen()
    raw, reply = await raw_auth(b, a.ident, link.link_id)
    assert reply == {"t": "denied"}


async def test_replayed_auth_signature_denied(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    await b.t.close()
    raw1 = await raw_hello(a.port)
    old_sig = b.ident.sign(
        transport.auth_transcript(raw1.server_pub, raw1.nonce, link.link_id)
    )
    await raw1.send(
        {"t": "auth", "v": 1, "link_id": link.link_id, "sig": old_sig.hex()}
    )
    assert (await raw1.recv())["t"] == "welcome"
    raw1.close()
    raw2, reply = await raw_auth(a, b.ident, link.link_id, sig=old_sig)
    assert raw2.nonce != raw1.nonce
    assert reply == {"t": "denied"}


async def test_idle_expired_link_denied(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    await b.t.close()
    await asyncio.sleep(0.05)
    a.store.update(link.link_id, last_seen=time.time() - 31 * 86400)
    raw, reply = await raw_auth(a, b.ident, link.link_id)
    assert reply == {"t": "denied"}


async def test_new_connection_replaces_old(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    await b.t.close()
    raw1, r1 = await raw_auth(a, b.ident, link.link_id)
    raw2, r2 = await raw_auth(a, b.ident, link.link_id)
    assert (
        r1["t"] == r2["t"] == "welcome" and r1["sas"] == a.store.get(link.link_id).sas
    )
    assert await closed_by_peer(raw1.reader)
    await raw2.send({"t": "ping"})
    assert await raw2.recv() == {"t": "pong"}
    assert a.t.is_connected(link.link_id)


# -- handshake robustness ---------------------------------------------------------------


async def test_stalled_tcp_dropped_at_deadline(make_node):
    a = make_node("alice", handshake_timeout=0.3)
    await a.listen()
    reader, writer = await asyncio.open_connection("127.0.0.1", a.port)
    t0 = time.monotonic()
    assert await closed_by_peer(reader, timeout=3)
    assert time.monotonic() - t0 < 2
    assert await eventually(lambda: a.t._unauth == 0)
    writer.close()


async def test_stalled_after_hello_dropped_at_deadline(make_node):
    a = make_node("alice", handshake_timeout=0.3)
    await a.listen()
    raw = await raw_hello(a.port)
    assert await closed_by_peer(raw.reader, timeout=3)
    assert await eventually(lambda: a.t._unauth == 0)


async def test_slowloris_partial_frame_dropped(make_node):
    a = make_node("alice", handshake_timeout=0.4)
    await a.listen()
    raw = await raw_hello(a.port)
    await raw.send_bytes(b"\x00\x00\x01")
    assert await closed_by_peer(raw.reader, timeout=3)


async def test_oversized_handshake_frame_closed(make_node):
    a = make_node("alice")
    await a.listen()
    raw = await raw_hello(a.port)
    await raw.send({"t": "pair", "pad": "x" * 5000})
    assert await closed_by_peer(raw.reader)


@pytest.mark.parametrize(
    "first",
    [
        {"t": "req", "id": 1, "op": "status", "p": {}},
        {"t": "welcome", "link_id": "a" * 32, "name": "x", "sas": "000-000-000-0"},
        {"t": "pair"},
        {"t": "auth", "v": 1, "link_id": "A" * 32, "sig": "0" * 128},
    ],
)
async def test_bad_first_frame_denied(make_node, first):
    a = make_node("alice")
    await a.listen()
    raw = await raw_hello(a.port)
    await raw.send(first)
    assert await raw.recv() == {"t": "denied"}
    assert await closed_by_peer(raw.reader)
    assert a.handler.calls == []


async def test_tls12_client_refused(make_node):
    a = make_node("alice")
    await a.listen()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises((ssl.SSLError, ConnectionError)):
        await tls_connect(a.port, ctx)


async def test_tls_contexts_are_13_only(make_node):
    a = make_node("alice")
    for ctx in (transport.server_ssl_context(a.ident), transport.client_ssl_context()):
        assert ctx.minimum_version == ctx.maximum_version == ssl.TLSVersion.TLSv1_3


async def test_plaintext_garbage_refused(make_node):
    a = make_node("alice")
    await a.listen()
    reader, writer = await asyncio.open_connection("127.0.0.1", a.port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" + b"\x00\x00\x00\x02{}")
    await writer.drain()
    data = b""
    try:
        while chunk := await asyncio.wait_for(reader.read(4096), 3):
            data += chunk
    except (ConnectionError, OSError):
        pass
    assert b"hello" not in data and b"nonce" not in data
    writer.close()


# -- frame attacks on an authenticated connection --------------------------------------

ATTACK_FRAMES = {
    "oversized": (wire.MAX_FRAME + 1).to_bytes(4, "big") + b"{}",
    "zero_length": b"\x00\x00\x00\x00",
    "non_object": _frame(b"[1,2]"),
    "deep_nesting": _frame(b'{"t":"ping","x":' + b"[" * 40 + b"]" * 40 + b"}"),
    "duplicate_keys": _frame(b'{"t":"ping","t":"ping"}'),
    "bad_utf8": _frame(b'{"t":"\xff"}'),
    "unsane_op": wire.encode({"t": "req", "id": 1, "op": "Exec Me", "p": {}}),
    "bool_as_int_id": wire.encode({"t": "req", "id": True, "op": "status", "p": {}}),
    "bool_as_int_arg": wire.encode(
        {"t": "req", "id": 1, "op": "diff", "p": {"max_chars": True}}
    ),
    "unknown_type": wire.encode({"t": "hello", "v": 1}),
    "control_chars_in_text": wire.encode(
        {
            "t": "req",
            "id": 1,
            "op": "msg",
            "p": {"msg_id": "a", "text": "\x1b]52;c;x\x07"},
        }
    ),
    "nan": _frame(b'{"t":"req","id":1,"op":"diff","p":{"max_chars":NaN}}'),
}


@pytest.mark.parametrize("name", sorted(ATTACK_FRAMES))
async def test_malformed_frames_close_connection(make_node, tmp_path, name):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    await raw.send_bytes(ATTACK_FRAMES[name])
    assert await closed_by_peer(raw.reader)
    assert a.handler.calls == []
    assert await eventually(lambda: not a.t.is_connected(raw.link_id))


async def test_unknown_op_is_answered_unsupported_and_the_link_stays(
    make_node, tmp_path
):
    """A newer peer asking for an op we don't have gets ``err: unsupported``;
    the connection stays up and the handler never sees it."""
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    await raw.send({"t": "req", "id": 1, "op": "room_join", "p": {"room": "x"}})
    assert await raw.recv() == {"t": "res", "id": 1, "ok": False, "err": "unsupported"}
    await raw.send({"t": "ping"})
    assert await raw.recv() == {"t": "pong"}
    assert a.t.is_connected(raw.link_id)
    assert a.handler.calls == []


async def test_unknown_keys_are_dropped_before_the_handler(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    await raw.send({"t": "req", "id": 3, "op": "status", "p": {"x": 1}, "trace": "abc"})
    res = await raw.recv()
    assert res["ok"] is True and res["id"] == 3
    assert a.handler.calls[-1][1:] == ("status", {})
    assert a.t.is_connected(raw.link_id)


async def test_a_newer_peers_extra_response_keys_are_ignored(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    task = asyncio.create_task(a.t.request(raw.link_id, "status", {}))
    req = await raw.recv()
    await raw.send(
        {
            "t": "res",
            "id": req["id"],
            "ok": True,
            "p": {"shared": False, "agent": "none", "name": "x", "grants": {}},
        }
    )
    assert await task == {"shared": False, "agent": "none", "name": "x"}
    assert a.t.is_connected(raw.link_id)


async def test_unknown_response_ids_ignored(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    await raw.send({"t": "res", "id": 12345, "ok": True, "p": {"anything": 1}})
    await raw.send({"t": "res", "id": 7, "ok": False, "err": "x"})
    await raw.send({"t": "ping"})
    assert await raw.recv() == {"t": "pong"}
    assert a.t.is_connected(raw.link_id)


async def test_invalid_response_from_peer_fails_request_and_closes(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    task = asyncio.create_task(a.t.request(raw.link_id, "status", {}))
    req = await raw.recv()
    assert req["t"] == "req" and req["op"] == "status"
    await raw.send(
        {
            "t": "res",
            "id": req["id"],
            "ok": True,
            "p": {"shared": "yes", "agent": "none", "name": "x"},
        }
    )
    with pytest.raises(PeerOpError, match="invalid response"):
        await task
    assert await closed_by_peer(raw.reader)


async def test_bye_closes(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    await raw.send({"t": "bye", "reason": "done"})
    assert await closed_by_peer(raw.reader)
    assert await eventually(lambda: not a.t.is_connected(raw.link_id))


async def test_duplicate_inflight_id_closes(make_node, tmp_path):
    a = make_node("alice")
    gate = asyncio.Event()

    async def blocked(link, op, p):
        await gate.wait()
        return {"shared": True, "agent": "none", "name": "x"}

    a.handler.behavior = blocked
    raw = await authed_raw(a, tmp_path)
    await raw.send({"t": "req", "id": 1, "op": "status", "p": {}})
    await raw.send({"t": "req", "id": 1, "op": "status", "p": {}})
    assert await closed_by_peer(raw.reader)
    gate.set()


# -- limits ------------------------------------------------------------------------------


async def test_inbound_in_flight_cap(make_node, tmp_path):
    a = make_node("alice")
    gate = asyncio.Event()

    async def blocked(link, op, p):
        await gate.wait()
        return {"shared": True, "agent": "none", "name": "x"}

    a.handler.behavior = blocked
    raw = await authed_raw(a, tmp_path)
    for i in range(1, 10):
        await raw.send({"t": "req", "id": i, "op": "status", "p": {}})
    first = await raw.recv()
    assert first == {
        "t": "res",
        "id": 9,
        "ok": False,
        "err": "too many requests in flight",
    }
    gate.set()
    got = {(await raw.recv())["id"] for _ in range(8)}
    assert got == set(range(1, 9))


async def test_outbound_in_flight_cap(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    tasks = [
        asyncio.create_task(a.t.request(raw.link_id, "status", {})) for _ in range(12)
    ]
    reqs = [await raw.recv() for _ in range(8)]
    with pytest.raises(TimeoutError):
        await raw.recv(timeout=0.2)  # the 9th waits for a slot
    for r in reqs:
        await raw.send(
            {
                "t": "res",
                "id": r["id"],
                "ok": True,
                "p": {"shared": True, "agent": "none", "name": "x"},
            }
        )
    for _ in range(4):
        r = await raw.recv()
        await raw.send(
            {
                "t": "res",
                "id": r["id"],
                "ok": True,
                "p": {"shared": True, "agent": "none", "name": "x"},
            }
        )
    assert len(await asyncio.gather(*tasks)) == 12


async def test_msg_rate_limit(make_node):
    a, b = make_node("alice", msg_rate=3), make_node("bob")
    link = await paired(a, b)
    for i in range(3):
        await b.t.request(link.link_id, "msg", {"msg_id": f"m{i}", "text": "x"})
    with pytest.raises(PeerOpError, match="rate limited"):
        await b.t.request(link.link_id, "msg", {"msg_id": "m9", "text": "x"})
    assert await b.t.request(link.link_id, "status", {})  # other ops unaffected
    a.clock.advance(61)
    assert await b.t.request(link.link_id, "msg", {"msg_id": "ok", "text": "x"}) == {
        "accepted": True
    }
    assert sum(1 for c in a.handler.calls if c[1] == "msg") == 4


async def test_req_rate_limit_survives_reconnect(make_node):
    a, b = make_node("alice", req_rate=4), make_node("bob")
    link = await paired(a, b)
    for _ in range(4):
        await b.t.request(link.link_id, "status", {})
    with pytest.raises(PeerOpError, match="rate limited"):
        await b.t.request(link.link_id, "status", {})
    b.t._conns[link.link_id].writer.transport.abort()
    assert await eventually(lambda: (link.link_id, False) in b.handler.states)
    assert await eventually(
        lambda: b.t.is_connected(link.link_id) and a.t.is_connected(link.link_id),
        timeout=5,
    )
    with pytest.raises(PeerOpError, match="rate limited"):
        await b.t.request(link.link_id, "status", {})


async def test_per_ip_connection_rate(make_node):
    a = make_node("alice", conn_rate_per_ip=3)
    await a.listen()
    for _ in range(3):
        (await tls_connect(a.port)).close()
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        await tls_connect(a.port)
    a.clock.advance(61)
    (await tls_connect(a.port)).close()


async def test_unauthenticated_connection_cap(make_node):
    a = make_node("alice", max_unauth=2, handshake_timeout=5)
    await a.listen()
    stalls = [await asyncio.open_connection("127.0.0.1", a.port) for _ in range(2)]
    assert await eventually(lambda: a.t._unauth == 2)
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        await tls_connect(a.port)
    for _, w in stalls:
        w.transport.abort()
    assert await eventually(lambda: a.t._unauth == 0)
    (await tls_connect(a.port)).close()


async def test_global_pairing_rate_limit(make_node):
    a, b = make_node("alice", invite_kw={"pair_rate": 2}), make_node("bob")
    await a.listen()
    code = a.code()
    for _ in range(2):
        with pytest.raises(PairingFailed):
            await b.t.pair(_forged_code(code, secret=os.urandom(20)))
    with pytest.raises(PairingFailed):
        await b.t.pair(code)  # right code, but over the limit
    a.clock.advance(61)
    assert await b.t.pair(code)


async def test_frame_flood_closes(make_node, tmp_path):
    a = make_node("alice", frame_rate=20)
    raw = await authed_raw(a, tmp_path)
    await raw.send_bytes(wire.encode({"t": "pong"}) * 25)
    assert await closed_by_peer(raw.reader)


async def test_hooks_order_added_before_connected(make_node):
    a, b = make_node("alice"), make_node("bob")
    events = []
    for n in (a, b):
        n.handler.on_link_added = lambda link, n=n: events.append((n.name, "added"))
        n.handler.on_state = lambda lid, up, n=n: events.append((n.name, up))
    await paired(a, b)
    for name in ("alice", "bob"):
        mine = [e[1] for e in events if e[0] == name]
        assert mine[:2] == ["added", True]


async def test_idle_connection_closed(make_node, tmp_path):
    a = make_node("alice", idle_timeout=0.3)
    raw = await authed_raw(a, tmp_path)
    assert await closed_by_peer(raw.reader, timeout=3)


async def test_pings_keep_link_alive(make_node):
    a = make_node("alice", ping_interval=0.1, idle_timeout=0.4)
    b = make_node("bob", ping_interval=0.1, idle_timeout=0.4)
    link = await paired(a, b)
    await asyncio.sleep(1.2)
    assert a.t.is_connected(link.link_id) and b.t.is_connected(link.link_id)
    assert (link.link_id, False) not in a.handler.states


# -- ops, permissions, errors -------------------------------------------------------------


async def test_permissions_enforced_by_receiver(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    a.store.update(link.link_id, perms={"read_file": False, "messages": False})
    for op, p in (
        ("read_file", {"path": "x"}),
        ("list_files", {}),
        ("msg", {"msg_id": "a", "text": "b"}),
    ):
        with pytest.raises(PeerOpError, match="not permitted"):
            await b.t.request(link.link_id, op, p)
    assert await b.t.request(link.link_id, "status", {})
    assert await b.t.request(link.link_id, "diff", {"max_chars": 1000})
    assert [c[1] for c in a.handler.calls] == ["status", "diff"]
    # the handler always sees the current link (with current perms)
    assert a.handler.links[-1].perms["read_file"] is False


async def test_handler_errors(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)

    async def op_error(link_, op, p):
        raise transport.PeerOpError("no shared folder")

    async def crash(link_, op, p):
        raise RuntimeError("/home/alice/.ssh/id_rsa leaked")

    async def bad_shape(link_, op, p):
        return {"shared": True, "agent": "none", "name": "x", "token": "abc"}

    a.handler.behavior = op_error
    with pytest.raises(PeerOpError, match="^no shared folder$"):
        await b.t.request(link.link_id, "status", {})
    for behavior in (crash, bad_shape):
        a.handler.behavior = behavior
        with pytest.raises(PeerOpError) as e:
            await b.t.request(link.link_id, "status", {})
        assert str(e.value) == "internal error"
    assert b.t.is_connected(link.link_id)


async def test_request_timeouts(make_node):
    a, b = make_node("alice", request_timeout=0.2), make_node("bob")
    link = await paired(a, b)

    async def slow(link_, op, p):
        await asyncio.sleep(2)
        return {"shared": True, "agent": "none", "name": "x"}

    a.handler.behavior = slow
    with pytest.raises(PeerOpError, match="timeout"):  # receiver-side deadline
        await b.t.request(link.link_id, "status", {})
    with pytest.raises(PeerUnavailable, match="timed out"):  # requester-side deadline
        await b.t.request(link.link_id, "status", {}, timeout=0.05)
    assert b.t.is_connected(link.link_id)


async def test_request_validation_and_unavailable(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    with pytest.raises(ValueError):
        await b.t.request(link.link_id, "exec", {})
    with pytest.raises(ValueError):
        await b.t.request(link.link_id, "diff", {"max_chars": True})
    with pytest.raises(PeerUnavailable):
        await b.t.request("0" * 32, "status", {})


async def test_unlink_sends_bye_and_stops(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    assert await b.t.unlink(link.link_id)
    assert b.store.get(link.link_id) is None
    assert await eventually(lambda: (link.link_id, False) in a.handler.states)
    assert not a.t.is_connected(link.link_id)
    await asyncio.sleep(0.3)
    assert not a.t.is_connected(link.link_id) and not b.t.is_connected(link.link_id)
    with pytest.raises(PeerUnavailable):
        await b.t.request(link.link_id, "status", {})


async def test_pending_requests_fail_when_connection_drops(make_node):
    a, b = make_node("alice", backoff_initial=5), make_node("bob")
    link = await paired(a, b)
    gate = asyncio.Event()

    async def blocked(link_, op, p):
        await gate.wait()
        return {"shared": True, "agent": "none", "name": "x"}

    a.handler.behavior = blocked
    task = asyncio.create_task(b.t.request(link.link_id, "status", {}))
    await asyncio.sleep(0.1)
    await a.t.close()
    with pytest.raises(PeerUnavailable):
        await task
    gate.set()


async def test_secrets_and_text_never_logged(make_node, caplog):
    caplog.set_level(logging.DEBUG)
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    code = a.code()
    secret = invite.parse_code(code).secret
    link = await b.t.pair(code)
    await eventually(lambda: a.t.is_connected(link.link_id))
    text = "TOP-SECRET-MESSAGE-BODY"
    await b.t.request(link.link_id, "msg", {"msg_id": "m1", "text": text})
    with pytest.raises(PairingFailed):
        await b.t.pair(code)  # failure paths log too
    logged = caplog.text
    for needle in (code, code[5:20], secret.hex(), text):
        assert needle not in logged


# -- pairing the same person again (join plan rank 3) ----------------------------------


async def test_repairing_the_same_key_reuses_the_link_on_both_sides(make_node):
    """B pastes a fresh invite from A while already linked (A's relay address
    changed, say): no duplicate link — the same link id, its perms and shared
    folder, with a new safety number; the invite is consumed as usual."""
    a, b = make_node("alice"), make_node("bob")
    first = await paired(a, b)
    a.store.update(first.link_id, perms={"diff": False}, sas_verified=True)
    b.store.update(first.link_id, share_id="ab" * 16, session_title="peer-alice-abab")
    a.handler.added.clear()
    repaired_a, repaired_b = [], []
    a.handler.on_link_repaired = repaired_a.append
    b.handler.on_link_repaired = repaired_b.append

    again = await b.t.pair(a.code())

    assert again.link_id == first.link_id
    assert [l.link_id for l in a.store.list()] == [first.link_id]
    assert [l.link_id for l in b.store.list()] == [first.link_id]
    a_link, b_link = a.store.get(first.link_id), b.store.get(first.link_id)
    assert a_link.perms["diff"] is False  # kept
    assert a_link.sas_verified is False  # a new number to compare
    assert b_link.share_id == "ab" * 16  # the share survives
    assert a_link.sas == b_link.sas
    assert a.handler.added == []  # not announced as a new person
    assert [l.link_id for l in repaired_a] == [first.link_id]
    assert [l.link_id for l in repaired_b] == [first.link_id]
    assert not a.invites.active()  # single use, as always
    assert await eventually(lambda: b.t.is_connected(first.link_id))


async def test_a_listener_link_records_its_carrier(make_node):
    a, b = make_node("alice"), make_node("bob")
    link = await paired(a, b)
    assert a.store.get(link.link_id).carrier == "tcp"
    assert b.store.get(link.link_id).carrier == "tcp"


async def test_progress_stages_for_a_direct_join(make_node):
    a, b = make_node("alice"), make_node("bob")
    await a.listen()
    stages: list[str] = []
    await b.t.pair(a.code(), progress=stages.append)
    assert stages == ["connecting", "verifying"]
