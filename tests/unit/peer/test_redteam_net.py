"""Red-team regressions for the network-facing peer code (transport, wire,
invite, store, service, agent_api). Each test failed before its fix."""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from backend.peer import identity, invite, wire
from backend.peer import service as svc_mod
from backend.peer import share as share_mod
from backend.peer.store import Link
from backend.peer.transport import PairingFailed, compute_sas, server_ssl_context
from tests.unit.peer.conftest import (
    authed_raw,
    b64,
    closed_by_peer,
    eventually,
    paired,
    raw_auth,
)

from ._integration_helpers import SHARE_ID, mk_inst
from .test_integration_routes import FakeInvites, FakeStore, FakeTransport, link

# --------------------------------------------------------------------------- #
# A hostile inviter: a real TLS 1.3 listener holding the key the code pins,
# which answers a pair with whatever welcome the test wants.
# --------------------------------------------------------------------------- #


class HostileInviter:
    def __init__(self, ident, welcome):
        self.ident = ident
        self.welcome = welcome  # (client_pub, nonce) -> dict
        self.server = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._cb, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _cb(self, reader, writer):
        try:
            await writer.start_tls(server_ssl_context(self.ident))
            nonce = os.urandom(32)
            writer.write(
                wire.encode({"t": "hello", "v": 1, "nonce": b64(nonce), "name": "evil"})
            )
            pair = await wire.read_frame(reader, wire.HANDSHAKE_MAX_FRAME)
            writer.write(wire.encode(self.welcome(bytes.fromhex(pair["pub"]), nonce)))
            await writer.drain()
            await reader.read(65536)
        except Exception:
            pass
        finally:
            writer.transport.abort()

    def code(self, port: int) -> str:
        return invite.encode_code(
            "127.0.0.1", port, os.urandom(8), os.urandom(20), self.ident.fingerprint()
        )


async def _join_hostile(make_node, tmp_path, welcome):
    ident = identity.load_or_create(str(tmp_path / "evil" / "identity"))
    evil = HostileInviter(
        ident, lambda client_pub, nonce: welcome(ident.pub, client_pub, nonce)
    )
    port = await evil.start()
    b = make_node("bob")
    try:
        with pytest.raises(PairingFailed):
            await b.t.pair(evil.code(port))
    finally:
        evil.server.close()
    return b


async def test_inviter_cannot_plant_newline_link_id(make_node, tmp_path):
    # "$" matched before a trailing newline, so a hostile inviter could store a
    # link id the HTTP API (fullmatch) can never address -> un-unlinkable link
    # that redials the attacker forever.
    def welcome(server_pub, client_pub, nonce):
        sas = compute_sas(server_pub, client_pub, nonce)
        return {"t": "welcome", "link_id": "ab" * 16 + "\n", "name": "evil", "sas": sas}

    b = await _join_hostile(make_node, tmp_path, welcome)
    assert b.store.list() == []


async def test_inviter_unicode_digit_sas_is_a_clean_failure(make_node, tmp_path):
    # \d matched Arabic-Indic digits; compare_digest then raised TypeError,
    # skipping every handler (socket left open, raw error text to the user).
    def welcome(server_pub, client_pub, nonce):
        return {
            "t": "welcome",
            "link_id": "ab" * 16,
            "name": "evil",
            "sas": "١٢٣-٤٥٦-٧٨٩-٠",
        }

    b = await _join_hostile(make_node, tmp_path, welcome)
    assert b.store.list() == []


@pytest.mark.parametrize(
    "check,obj",
    [
        (
            wire.validate_auth,
            {"t": "auth", "v": 1, "link_id": "ab" * 16 + "\n", "sig": "cd" * 64},
        ),
        (
            wire.validate_pair,
            {
                "t": "pair",
                "v": 1,
                "invite_id": "ab" * 8,
                "pub": "cd" * 32 + "\n",
                "name": "x",
                "proof": "ef" * 32,
                "sig": "01" * 64,
            },
        ),
        (
            wire.validate_welcome,
            {
                "t": "welcome",
                "link_id": "ab" * 16,
                "name": "x",
                "sas": "123-456-789-0\n",
            },
        ),
    ],
)
def test_wire_refuses_trailing_newline(check, obj):
    with pytest.raises(wire.ProtocolError):
        check(obj)


@pytest.mark.parametrize(
    "field,value",
    [
        ("link_id", "ab" * 16 + "\n"),
        ("peer_pub", "cd" * 32 + "\n"),
        ("sas", "١٢٣-456-789-0"),
    ],
)
def test_store_refuses_trailing_newline_and_unicode_digits(field, value):
    d = dict(
        link_id="ab" * 16,
        peer_name="x",
        peer_pub="cd" * 32,
        role="dialer",
        peer_addr="10.0.0.1:8799",
    )
    d[field] = value
    with pytest.raises(ValueError):
        Link(**d).validate()


def test_non_ascii_checksum_is_a_value_error():
    book = invite.InviteBook(b"\x01" * 16)
    code = book.create("127.0.0.1", 8799).code
    with pytest.raises(ValueError):  # was TypeError from compare_digest
        invite.parse_code(code[:-4] + "é" * 4)


# --------------------------------------------------------------------------- #
# Revocation races
# --------------------------------------------------------------------------- #


async def test_unlink_during_handshake_does_not_adopt(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    raw.close()
    assert await eventually(lambda: not a.t.is_connected(raw.link_id))

    orig = a.t._server_handshake

    async def racing(reader, writer, *rest):
        result = await orig(reader, writer, *rest)
        if result is not None:  # the user unlinks while the welcome drains
            await a.t.unlink(result[0].link_id)
        return result

    a.t._server_handshake = racing
    raw2, reply = await raw_auth(a, raw.ident, raw.link_id)
    assert reply["t"] == "welcome"
    assert await closed_by_peer(raw2.reader)
    assert not a.t.is_connected(raw.link_id)


async def test_removed_link_not_kept_alive_by_pings(make_node):
    a, b = make_node("alice", ping_interval=0.1), make_node("bob", ping_interval=0.1)
    lnk = await paired(a, b)
    a.store.remove(lnk.link_id)  # e.g. unlinked by another process
    # No requests at all: only pings flow. The connection must still go.
    assert await eventually(lambda: not a.t.is_connected(lnk.link_id), timeout=2.0)


# --------------------------------------------------------------------------- #
# Connection rate limit keying
# --------------------------------------------------------------------------- #


async def test_ipv6_rate_limit_covers_the_whole_64(make_node):
    a = make_node("alice", conn_rate_per_ip=2)
    assert a.t._ip_allowed("2001:db8:1:2::1")
    assert a.t._ip_allowed("2001:db8:1:2::2")
    assert not a.t._ip_allowed("2001:db8:1:2:ffff::3")  # same /64, new address
    assert a.t._ip_allowed("2001:db8:1:3::1")  # another /64


async def test_ipv4_mapped_clients_are_not_one_bucket(make_node):
    a = make_node("alice", conn_rate_per_ip=1)
    assert a.t._ip_allowed("::ffff:10.0.0.1")
    assert not a.t._ip_allowed("10.0.0.1")  # the same client either way
    assert a.t._ip_allowed("::ffff:10.0.0.2")


# --------------------------------------------------------------------------- #
# Service: inbound ops
# --------------------------------------------------------------------------- #


def _service(*links):
    return svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore(links),
        invites_factory=FakeInvites,
        transport_factory=FakeTransport,
        settings_getter=lambda: {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "listen_port": 8799,
            "display_name": "alice",
            "advertise_host": "",
            "egress_allow": [],
        },
    )


@pytest.fixture
def engine(monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    return server.ENGINE.instances


async def test_msg_never_reaches_another_links_shared_session(engine, tmp_path):
    from backend.web.core import mailbox as mb

    other_share = "cd" * 16
    # A stale session_title now names a session bound to ANOTHER share.
    engine["peer-carol-cdcd"] = mk_inst(
        "peer-carol-cdcd", str(tmp_path / "w"), peer_share=other_share
    )
    lnk = link(share_id=SHARE_ID, session_title="peer-carol-cdcd")
    svc = _service(lnk)
    res = await svc.handle_request(
        lnk, "msg", {"msg_id": "pm1", "text": "hi", "reply_to": None}
    )
    # Kept on the link for the people, never typed into carol's session.
    assert res == {"accepted": True}
    assert mb.unread_count("peer-carol-cdcd") == 0


async def test_slow_share_ops_cannot_starve_the_server_threads(monkeypatch):
    # The transport's 60 s deadline cancels the await but not the thread; with
    # asyncio.to_thread a peer could fill the server's default executor.
    gate = threading.Event()
    running = []

    def slow_diff(share, max_chars):
        running.append(1)
        gate.wait(10)
        return {"stat": [], "diff": "", "truncated": False}

    monkeypatch.setattr(share_mod, "diff", slow_diff)
    lnk = link(share_id=SHARE_ID)
    svc = _service(lnk)
    monkeypatch.setattr(svc, "_share_obj", lambda sid: object())
    try:
        for _ in range(40):  # 120/min are allowed; each one "times out"
            task = asyncio.ensure_future(
                svc.handle_request(lnk, "diff", {"max_chars": 5000})
            )
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0.2)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 2.0) == 42
        assert len(running) <= svc_mod.PEER_JOBS_PER_LINK
        with pytest.raises(Exception, match="busy"):
            await svc.handle_request(lnk, "diff", {"max_chars": 5000})
    finally:
        gate.set()
        await svc.stop()


# --------------------------------------------------------------------------- #
# A peer's unlink revokes the link on OUR side too
# --------------------------------------------------------------------------- #


async def test_dialer_unlink_revokes_listener_side(make_node):
    a, b = make_node("alice"), make_node("bob")
    removed = []
    a.handler.on_link_removed = removed.append
    lnk = await paired(a, b)
    await b.t.unlink(lnk.link_id)
    assert await eventually(lambda: a.store.get(lnk.link_id) is None)
    assert [l.link_id for l in removed] == [lnk.link_id]
    # The "unlinked" peer can't come back with the same key and link id.
    raw, reply = await raw_auth(a, b.ident, lnk.link_id)
    assert reply == {"t": "denied"}
    raw.close()


async def test_listener_unlink_stops_the_dialer(make_node):
    a, b = make_node("alice"), make_node("bob")
    lnk = await paired(a, b)
    await a.t.unlink(lnk.link_id)
    assert await eventually(lambda: b.store.get(lnk.link_id) is None)
    assert await eventually(lambda: lnk.link_id not in b.t._supervisors)


async def test_bye_unlinked_only_revokes_the_senders_own_link(make_node, tmp_path):
    a, b = make_node("alice"), make_node("bob")
    victim = await paired(a, b)
    mallory = await authed_raw(a, tmp_path, "mallory")
    await mallory.send({"t": "bye", "reason": "unlinked"})
    assert await eventually(lambda: a.store.get(mallory.link_id) is None)
    assert a.store.get(victim.link_id) is not None
    assert a.t.is_connected(victim.link_id)


async def test_unauthenticated_bye_unlinked_is_denied(make_node, tmp_path):
    a = make_node("alice")
    raw = await authed_raw(a, tmp_path)
    raw.close()
    from tests.unit.peer.conftest import raw_hello

    forged = await raw_hello(a.port)
    await forged.send({"t": "bye", "reason": "unlinked"})
    assert await forged.recv() == {"t": "denied"}
    assert a.store.get(raw.link_id) is not None


async def test_service_stops_the_shared_session_when_the_peer_unlinks(
    engine, monkeypatch, tmp_path
):
    from backend.web import server

    engine["peer-bob-abab"] = mk_inst(
        "peer-bob-abab", str(tmp_path / "w"), peer_share=SHARE_ID
    )
    deleted, stopped = [], []

    async def fake_delete(title):
        deleted.append(title)
        engine.pop(title, None)

    monkeypatch.setattr(server, "delete_instance", fake_delete)
    monkeypatch.setattr(
        share_mod, "remove_share", lambda *a, **k: pytest.fail("folder must be kept")
    )
    lnk = link(share_id=SHARE_ID, session_title="peer-bob-abab")
    svc = _service()  # the transport already removed the link
    part = SimpleNamespace(stop=lambda: stopped.append(1))
    svc._runtimes[SHARE_ID] = svc_mod._ShareRuntime(object(), "t" * 32, part, part)
    await svc.on_link_removed(lnk)
    assert deleted == ["peer-bob-abab"]
    assert SHARE_ID not in svc._runtimes and len(stopped) == 2
