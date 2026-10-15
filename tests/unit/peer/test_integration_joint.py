"""Joint tasks end to end over two real PeerTransports on 127.0.0.1 (TLS,
the pinned-key handshake, the frame validators both ways), each with its own
PeerService, identity, link store and joint file: propose → accept → a
contract revision → an agent's proposal accepted by the other person → a
dropped connection, edits on both sides while it's down, and the
``joint_state`` resync when it comes back."""

from __future__ import annotations

import asyncio
import pytest

from backend.peer import identity, invite, joint, store, transport
from backend.peer import service as svc_mod

from .conftest import FAST, eventually


@pytest.fixture
def engine(monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)


def _service(tmp_path, name: str) -> svc_mod.PeerService:
    base = tmp_path / name
    ident = identity.load_or_create(str(base / "identity"))

    def make_transport(ident_, st, invites, display, handler):
        return transport.PeerTransport(ident_, st, invites, display, handler, **FAST)

    svc = svc_mod.PeerService(
        identity_factory=lambda: ident,
        store_factory=lambda: store.LinkStore(str(base / "links.json")),
        invites_factory=lambda: invite.InviteBook(ident.fingerprint()),
        transport_factory=make_transport,
        settings_getter=lambda: {
            "enabled": True,
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "display_name": name,
            "advertise_host": "127.0.0.1",
            "egress_allow": [],
            "relay": "off",
        },
    )
    svc._joint = joint.JointTasks(svc, joint.JointStore(str(base / "joint")))
    return svc


async def _settle():
    """Wait for the joint tasks' own background work (share starts,
    resyncs, push-backs) — never the transports' endless read loops."""
    for _ in range(50):
        mine = [
            t
            for t in asyncio.all_tasks()
            if not t.done() and "JointTasks" in t.get_coro().__qualname__
        ]
        if not mine:
            return
        await asyncio.wait(mine, timeout=2)


async def test_joint_task_over_real_transports(tmp_path, engine):
    a, b = _service(tmp_path, "alice"), _service(tmp_path, "bob")
    try:
        await a._ensure_transport()
        await b._ensure_transport()
        _, port = await a.transport.start_listener("127.0.0.1", 0)
        code = a.invites.create("127.0.0.1", port, 600).code
        lnk = await b.transport.pair(code)
        lid = lnk.link_id
        assert await eventually(lambda: a.is_connected(lid) and b.is_connected(lid))
        await _settle()  # the reconnect resyncs find no joint yet

        # Not before the safety number matches.
        with pytest.raises(svc_mod.PeerServiceError, match="safety number"):
            await a.joint.propose(lid, "Add GET /items")
        a.set_verified(lid, True)
        b.set_verified(lid, True)

        # 1. Propose → accept.
        await a.joint.propose(
            lid,
            "Add GET /items to A; B's client prints it",
            contract_text="## Interfaces\nGET /items -> {items: [str]}",
        )
        jb = b.joint.store.get(lid)
        assert jb["state"] == "proposed" and jb["proposed_by"] == "peer"
        assert b.link_view(b.store.get(lid))["joint"]["state"] == "proposed"
        await b.joint.accept(lid)
        await _settle()
        ja, jb = a.joint.store.get(lid), b.joint.store.get(lid)
        assert ja["id"] == jb["id"] and ja["state"] == jb["state"] == "active"

        # 2. A contract revision from A reaches B.
        out = await a.joint.set_contract(
            lid, "## Interfaces\nGET /items -> {items: []}"
        )
        assert out["synced"] is True
        assert b.joint.store.get(lid)["contract"]["rev"] == 2

        # 3. B's agent proposes a change; A's person accepts it.
        res = await b.joint.propose_edit(
            lid, "## v3: {items: [{id, name}]}", by="agent"
        )
        pid = res["proposal_id"]
        (pa,) = a.joint.store.get(lid)["proposals"]
        assert pa["id"] == pid and pa["by"] == "peer" and pa["state"] == "open"
        assert a.link_view(a.store.get(lid))["joint"]["open_proposals"] == 1
        await a.joint.decide(lid, pid, True)
        for side in (a, b):
            j = side.joint.store.get(lid)
            assert j["contract"]["rev"] == 3
            assert j["contract"]["text"] == "## v3: {items: [{id, name}]}"
            assert j["proposals"][0]["state"] == "accepted"

        # 4. The link drops (and can't come back yet: A stops listening);
        # both edit, and B's agent proposes, while it's down.
        await a.transport.stop_listener()
        a.transport._conns[lid].writer.transport.abort()
        assert await eventually(
            lambda: not a.is_connected(lid) and not b.is_connected(lid)
        )
        r1 = await a.joint.set_contract(lid, "## v4 from alice (the proposer)")
        r2 = await b.joint.set_contract(lid, "## v4 from bob")
        offline = await b.joint.propose_edit(lid, "## while offline")
        assert r1["synced"] is r2["synced"] is offline["synced"] is False
        assert len(a.joint.store.get(lid)["proposals"]) == 1
        await a.transport.start_listener("127.0.0.1", port)

        # 5. It comes back: on_state(connected) resyncs both sides.
        assert await eventually(
            lambda: a.is_connected(lid) and b.is_connected(lid), timeout=10
        )
        ok = await eventually(
            lambda: len(a.joint.store.get(lid)["proposals"]) == 2
            and b.joint.store.get(lid)["contract"]["text"]
            == "## v4 from alice (the proposer)",
            timeout=10,
        )
        assert ok, (a.joint.store.get(lid), b.joint.store.get(lid))
        await _settle()
        ja, jb = a.joint.store.get(lid), b.joint.store.get(lid)
        assert ja["contract"]["text"] == jb["contract"]["text"]
        assert ja["contract"]["rev"] == jb["contract"]["rev"] == 4
        assert {p["id"]: p["state"] for p in ja["proposals"]} == {
            p["id"]: p["state"] for p in jb["proposals"]
        }
        assert offline["proposal_id"] in {p["id"] for p in ja["proposals"]}

        # 6. Done from B ends it on both sides.
        await b.joint.cancel(lid, "done")
        assert a.joint.store.get(lid)["state"] == "done"
    finally:
        await a.stop()
        await b.stop()


async def test_an_old_peer_without_joint_ops(tmp_path, engine, monkeypatch):
    """A peer whose wire has no joint_* ops answers ``unsupported`` (the
    link stays up) and the route says to ask them to update."""
    a, b = _service(tmp_path, "alice"), _service(tmp_path, "bob")
    try:
        await a._ensure_transport()
        await b._ensure_transport()
        _, port = await a.transport.start_listener("127.0.0.1", 0)
        lnk = await b.transport.pair(a.invites.create("127.0.0.1", port, 600).code)
        lid = lnk.link_id
        assert await eventually(lambda: a.is_connected(lid) and b.is_connected(lid))
        a.set_verified(lid, True)
        from backend.peer import wire

        # B is the old release: it has never heard of joint_* ops.
        real = wire.validate_message

        def old_validate(obj):
            if obj.get("t") == "req" and str(obj.get("op", "")).startswith("joint_"):
                raise wire.UnsupportedOp(obj["id"], obj["op"])
            return real(obj)

        monkeypatch.setattr(wire, "validate_message", old_validate)
        with pytest.raises(svc_mod.PeerServiceError) as exc:
            await a.joint.propose(lid, "x")
        assert exc.value.status == 409
        assert exc.value.message == "bob's MindFlock is older — ask them to update"
        assert a.joint.store.get(lid) is None
        assert a.is_connected(lid)  # the link stays up
        st = await a.request(lid, "status", {})
        assert st["name"] == "bob"
    finally:
        await a.stop()
        await b.stop()
