"""Peer links you can see, trust and reconnect (roadmap PR 9/10): the peer.*
events are registered and documented, joining again reconnects the same link
with its share, join errors say what to do next, "It matches" persists, and
the snapshot says who a shared session is shared with."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.peer import service as svc_mod
from backend.peer.invite import encode_code
from backend.peer.store import Link, LinkStore
from backend.web.core import events

from ._integration_helpers import SHARE_ID, mk_inst

ROOT = Path(__file__).resolve().parents[3]
PUB = "ee" * 32


def test_every_peer_event_the_service_emits_is_registered_and_documented():
    """An event outside EVENT_NAMES can't be routed by the bell, the notify
    rules or a user's hooks; one missing from docs/extensions.md can't be
    found by hook authors."""
    src = (ROOT / "backend" / "peer" / "service.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'"(peer\.[a-z_]+)"', src))
    assert emitted >= {
        "peer.link_added",
        "peer.link_removed",
        "peer.state",
        "peer.progress",
        "peer.message",
        "peer.relay_changed",
    }
    assert emitted <= set(events.EVENT_NAMES)
    doc = (ROOT / "docs" / "extensions.md").read_text(encoding="utf-8")
    for name in emitted:
        assert "| `%s` |" % name in doc, name


def _service(store, transport):
    return svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: store,
        invites_factory=lambda: SimpleNamespace(active=lambda: []),
        transport_factory=lambda *a: transport,
        settings_getter=lambda: {
            "enabled": True,
            "listen_host": "0.0.0.0",
            "listen_port": 8799,
            "display_name": "alice",
            "relay": "off",
        },
    )


class PairingTransport:
    """``pair`` hands back whatever link the test scripts (the listener's
    choice of link id), stored the way the real transport stores it."""

    def __init__(self, store, link_id, err=None):
        self.store, self.link_id, self.err = store, link_id, err
        self.listening = False
        self.unlinked = []
        self.stages = []

    def start(self):
        pass

    def stop_listener(self):
        self.listening = False

    def is_connected(self, lid):
        return False

    async def pair(self, code, progress=None):
        for s in ("waiting_relay", "connecting", "verifying"):
            progress(s)
        if self.err is not None:
            raise self.err
        if self.store.get(self.link_id) is None:
            self.store.add(
                Link(
                    link_id=self.link_id,
                    peer_name="Bob",
                    peer_pub=PUB,
                    role="dialer",
                    peer_addr="wss://new-name.trycloudflare.com:443/tok",
                    sas="111-222-333-4",
                )
            )
        return self.store.get(self.link_id)

    async def unlink(self, lid):
        self.unlinked.append(lid)
        return self.store.remove(lid)

    def close(self):
        pass


# A real relay code (mfp2, WebSocket carrier): the error wording depends on it.
CODE = encode_code(
    "new-name.trycloudflare.com",
    443,
    b"\x01" * 8,
    b"\x02" * 20,
    b"\x07" * 16,
    relay_path="/tok",
)


def _old_link(store, lid="11" * 16):
    return store.add(
        Link(
            link_id=lid,
            peer_name="Bob",
            peer_pub=PUB,
            role="dialer",
            peer_addr="wss://old-name.trycloudflare.com:443/tok",
            sas="999-888-777-6",
            perms={"messages": True, "diff": False, "read_file": False},
            share_id=SHARE_ID,
            session_title="peer-bob-" + SHARE_ID[:6],
        )
    )


async def test_joining_someone_already_linked_keeps_the_share(tmp_path):
    """Their side lost our link and minted a new one: the share, perms and
    session move onto it, the old link goes, and the join says reconnected."""
    store = LinkStore(str(tmp_path / "links.json"))
    _old_link(store)
    t = PairingTransport(store, "22" * 16)
    svc = _service(store, t)
    events_seen = []
    svc._emit = lambda e, d: events_seen.append((e, d))
    out = await svc.join("here: " + CODE, op_id="j1")
    assert out["link_id"] == "22" * 16 and out["reconnected"] is True
    assert out["shared"] is True and out["share_id"] == SHARE_ID
    assert out["perms"] == {"messages": True, "diff": False, "read_file": False}
    assert [l.link_id for l in store.list()] == ["22" * 16]
    assert t.unlinked == ["11" * 16]
    stages = [d["stage"] for e, d in events_seen if e == "peer.progress"]
    assert stages == ["waiting_relay", "connecting", "verifying", "done"]


async def test_joining_with_the_link_kept_is_a_reconnect(tmp_path):
    store = LinkStore(str(tmp_path / "links.json"))
    _old_link(store)
    t = PairingTransport(store, "11" * 16)  # the listener reused our link
    svc = _service(store, t)
    out = await svc.join(CODE)
    assert out["link_id"] == "11" * 16 and out["reconnected"] is True
    assert out["share_id"] == SHARE_ID
    assert t.unlinked == []


@pytest.mark.parametrize(
    "err,needle",
    [
        ("pairing refused: the code is wrong, used or expired", "ask for a new one"),
        ("pairing timed out", "Their relay isn't answering"),
    ],
)
async def test_join_errors_say_what_to_do_next(tmp_path, err, needle):
    from backend.peer.transport import PairingFailed

    store = LinkStore(str(tmp_path / "links.json"))
    svc = _service(store, PairingTransport(store, "22" * 16, PairingFailed(err)))
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        await svc.join(CODE)
    assert needle in exc.value.message
    assert "pairing" not in exc.value.message  # no raw transport text


def test_direct_join_failure_explains_network_only():
    from backend.peer.transport import PairingFailed

    text = svc_mod.PeerService._join_error(
        PairingFailed("cannot reach 100.64.0.2:8799: ConnectionRefusedError"), False
    )
    assert "only works on their network or tailnet" in text
    assert "ConnectionRefusedError" not in text


def test_it_matches_is_persisted(tmp_path):
    store = LinkStore(str(tmp_path / "links.json"))
    _old_link(store)
    svc = _service(store, PairingTransport(store, "11" * 16))
    assert svc.set_verified("11" * 16, True)["sas_verified"] is True
    assert store.get("11" * 16).sas_verified is True
    with pytest.raises(svc_mod.PeerServiceError):
        svc.set_verified("11" * 16, "yes")


def test_link_view_shows_carrier_and_unread(tmp_path):
    store = LinkStore(str(tmp_path / "links.json"))
    link = _old_link(store)
    svc = _service(store, PairingTransport(store, "11" * 16))
    view = svc.link_view(link)
    assert view["carrier"] == "relay"  # a dialer link: from its address
    assert view["sas_verified"] is False and view["unread"] == 0
    assert view["peer_app"] is None
    assert "peer_pub" not in view


def test_snapshot_says_who_a_shared_session_is_shared_with(tmp_path, monkeypatch):
    from backend.web.core import snapshot

    store = LinkStore(str(tmp_path / "links.json"))
    _old_link(store)
    svc = _service(store, PairingTransport(store, "11" * 16))
    monkeypatch.setattr(svc_mod, "_SERVICE", svc)
    shared = mk_inst("peer-bob-x", str(tmp_path / "w"), peer_share=SHARE_ID)
    plain = mk_inst("plain", str(tmp_path / "p"))
    assert snapshot._peer_with(shared) == {
        "link_id": "11" * 16,
        "name": "Bob",
        "connected": False,
    }
    assert snapshot._peer_with(plain) is None
