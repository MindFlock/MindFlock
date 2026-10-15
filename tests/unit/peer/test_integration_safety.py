"""The safety rules end to end over two real PeerTransports on 127.0.0.1,
each with its own PeerService, identity and link store: before *It
matches* nothing from the shared folder crosses (messages do); a re-pair
gives a new safety number, un-confirms it on both sides and says so; and
the peer unlinking CLOSES this side's shared session — never deletes it."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from backend.peer import identity, invite, paths, store, transport
from backend.peer import service as svc_mod

from ._integration_helpers import SHARE_ID, make_share, mk_inst
from .conftest import FAST, eventually


@pytest.fixture
def engine(monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    return server.ENGINE.instances


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
    svc.events = []
    svc._emit = lambda event, data: svc.events.append((event, data))
    return svc


async def test_safety_rules_over_real_transports(tmp_path, engine, monkeypatch):
    from backend.web import server

    a, b = _service(tmp_path, "alice"), _service(tmp_path, "bob")
    try:
        await a._ensure_transport()
        await b._ensure_transport()
        _, port = await a.transport.start_listener("127.0.0.1", 0)
        lid = (await b.join(a.invites.create("127.0.0.1", port, 600).code))["link_id"]
        assert await eventually(lambda: a.is_connected(lid) and b.is_connected(lid))
        first_sas = a.store.get(lid).sas

        # 3. Not confirmed on A: A's folder content is refused to B, in words.
        with pytest.raises(svc_mod.PeerServiceError) as exc:
            await b.their_files(lid)
        assert exc.value.status == 403
        assert exc.value.message.startswith(
            "alice hasn't confirmed the safety number yet"
        )
        # ...but they can talk (that's how they compare the number).
        sent = await b.human_message(lid, "mine says %s" % first_sas)
        assert sent["delivered"] is True
        (msg,) = [d for e, d in a.events if e == "peer.message"]
        assert msg["text_head"] == "mine says %s" % first_sas
        assert msg["peer_name"] == "bob" and msg["link_id"] == lid

        # It matches on both sides: the gate opens (no share yet, so that).
        a.set_verified(lid, True)
        b.set_verified(lid, True)
        with pytest.raises(svc_mod.PeerServiceError, match="hasn't shared a folder"):
            await b.their_files(lid)

        # 2. A fresh invite from someone already linked: same link, perms
        # kept, a NEW safety number — un-confirmed on both sides, and said.
        a.set_perms(lid, {"diff": False})
        a.events.clear()
        b.events.clear()
        again = await b.join(a.invites.create("127.0.0.1", port, 600).code)
        assert again["link_id"] == lid and again["reconnected"] is True
        assert await eventually(lambda: a.is_connected(lid) and b.is_connected(lid))
        la, lb = a.store.get(lid), b.store.get(lid)
        assert la.sas == lb.sas != first_sas
        assert la.sas_verified is False and lb.sas_verified is False
        assert la.perms["diff"] is False and la.perms["read_file"] is True
        for side in (a, b):
            changed = [
                d for e, d in side.events if e == "peer.state" and d.get("sas_changed")
            ]
            assert changed and changed[0]["sas"] == la.sas, side.events
        with pytest.raises(svc_mod.PeerServiceError, match="safety number"):
            await b.their_files(lid)
        a.set_verified(lid, True)
        b.set_verified(lid, True)

        # 1. B shares a folder; A unlinks. B's session is CLOSED (folder
        # kept), never deleted, and B hears who did it and where it is.
        p = make_share()
        title = "peer-alice-abab"
        engine[title] = mk_inst(title, p["work"], peer_share=SHARE_ID)
        b.store.update(lid, share_id=SHARE_ID, session_title=title)
        ended = SimpleNamespace(closed=[], deleted=[])

        async def close_instance(t):
            ended.closed.append(t)
            engine.pop(t, None)
            return SimpleNamespace(status_code=200)

        async def delete_instance(t):
            ended.deleted.append(t)
            return SimpleNamespace(status_code=200)

        monkeypatch.setattr(server, "close_instance", close_instance)
        monkeypatch.setattr(server, "delete_instance", delete_instance)
        await a.unlink(lid)
        assert await eventually(
            lambda: any(e == "peer.link_removed" for e, _ in b.events), timeout=5
        )
        assert b.store.get(lid) is None
        assert ended.closed == [title] and ended.deleted == []
        assert os.path.isdir(paths.share_paths(SHARE_ID)["work"])
        (gone,) = [d for e, d in b.events if e == "peer.link_removed"]
        assert gone["by"] == "peer" and gone["peer_name"] == "alice"
        assert gone["kept_folder"] == paths.share_paths(SHARE_ID)["work"]
    finally:
        await a.stop()
        await b.stop()
