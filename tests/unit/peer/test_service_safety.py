"""PeerService safety rules (docs/peer-link.md, "Threat model" and "Engine
integration"): an unlink — by either side — CLOSES the shared session and
keeps its folder; a re-pair un-confirms the safety number and says so;
nothing in the shared folder crosses (or comes home) before *It matches*;
turning peer links off keeps every link and share; export answers in one
sentence and says how much came home."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.peer import launch as peer_launch
from backend.peer import paths, transport
from backend.peer import service as svc_mod

from ._integration_helpers import SHARE_ID, fake_sandbox, make_share, mk_inst
from .test_integration_routes import FakeInvites, FakeStore, FakeTransport, link

LID = "ab" * 16
TITLE = "peer-bob-abab"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    return server.ENGINE.instances


@pytest.fixture
def ends(monkeypatch, engine):
    """Records which way a session ended: Close (kept) or Delete."""
    from backend.web import server

    rec = SimpleNamespace(closed=[], deleted=[])

    async def close_instance(title):
        rec.closed.append(title)
        engine.pop(title, None)
        return SimpleNamespace(status_code=200)

    async def delete_instance(title):
        rec.deleted.append(title)
        engine.pop(title, None)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(server, "close_instance", close_instance)
    monkeypatch.setattr(server, "delete_instance", delete_instance)
    return rec


def make_service(*links, enabled=True, settings=None):
    state = {
        "enabled": enabled,
        "listen_host": "0.0.0.0",
        "listen_port": 8799,
        "display_name": "alice",
        "advertise_host": "",
        "egress_allow": [],
        "relay": "off",
        **(settings or {}),
    }
    svc = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore(links or [link()]),
        invites_factory=FakeInvites,
        transport_factory=FakeTransport,
        settings_getter=lambda: dict(state),
        settings_setter=lambda patch: state.update(patch),
    )
    svc.state = state
    svc.events = []
    svc._emit = lambda event, data: svc.events.append((event, data))
    return svc


def shared_link(engine, tmp_path, **kw):
    """A link whose shared session is open (its folder on disk)."""
    p = make_share()
    engine[TITLE] = mk_inst(TITLE, p["work"], peer_share=SHARE_ID)
    return link(share_id=SHARE_ID, session_title=TITLE, **kw)


def op_error(svc, lnk, op, p):
    with pytest.raises(Exception) as exc:
        run(svc.handle_request(lnk, op, p))
    return str(exc.value)


# --------------------------------------------------------------------------- #
# 1. Unlink never destroys the other side's work
# --------------------------------------------------------------------------- #
def test_unlink_by_the_peer_closes_my_shared_session_and_keeps_the_folder(
    engine, ends, tmp_path
):
    lnk = shared_link(engine, tmp_path)
    svc = make_service(lnk)
    svc.store.remove(LID)  # the transport forgot the link before the hook
    run(svc.on_link_removed(lnk))
    assert ends.closed == [TITLE] and ends.deleted == []
    work = paths.share_paths(SHARE_ID)["work"]
    assert os.path.isdir(work)
    ((event, data),) = [e for e in svc.events if e[0] == "peer.link_removed"]
    assert data == {
        "link_id": LID,
        "peer_name": "Bob",
        "by": "peer",
        "kept_folder": work,
    }


def test_unlink_by_the_peer_without_a_share_says_nothing_was_kept(engine, ends):
    svc = make_service()
    run(svc.on_link_removed(link()))
    (data,) = [d for e, d in svc.events if e == "peer.link_removed"]
    assert data["by"] == "peer" and data["kept_folder"] is None
    assert ends.closed == ends.deleted == []


def test_my_unlink_closes_my_shared_session_too(engine, ends, tmp_path):
    svc = make_service(shared_link(engine, tmp_path))
    run(svc.unlink(LID))
    assert ends.closed == [TITLE] and ends.deleted == []
    (data,) = [d for e, d in svc.events if e == "peer.link_removed"]
    assert data["by"] == "you"
    assert data["kept_folder"] == paths.share_paths(SHARE_ID)["work"]
    assert svc.store.get(LID) is None


def test_only_the_delete_tick_deletes(engine, ends, tmp_path, monkeypatch):
    from backend.peer import share as share_mod

    removed = []
    monkeypatch.setattr(
        share_mod, "remove_share", lambda sid, is_running=None: removed.append(sid)
    )
    svc = make_service(shared_link(engine, tmp_path))
    run(svc.unlink(LID, delete_files=True))
    assert ends.deleted == [TITLE] and ends.closed == []
    assert removed == [SHARE_ID]
    (data,) = [d for e, d in svc.events if e == "peer.link_removed"]
    assert data["kept_folder"] is None


def test_stop_sharing_closes_and_names_the_kept_folder(engine, ends, tmp_path):
    svc = make_service(shared_link(engine, tmp_path))
    out = run(svc.unshare(LID))
    assert ends.closed == [TITLE] and ends.deleted == []
    assert out["deleted"] is False
    assert out["folder"] == paths.share_paths(SHARE_ID)["work"]


# --------------------------------------------------------------------------- #
# 2. A re-pair un-confirms the safety number, and says so
# --------------------------------------------------------------------------- #
def test_a_repair_says_the_safety_number_changed(engine):
    svc = make_service()
    lnk = link(sas="111-222-333-4", sas_verified=False)
    svc.on_link_repaired(lnk)
    names = [e for e, _ in svc.events]
    assert names == ["peer.link_added", "peer.state"]
    state = svc.events[1][1]
    assert state == {
        "link_id": LID,
        "peer_name": "Bob",
        "connected": True,
        "sas_changed": True,
        "sas": "111-222-333-4",
    }


# --------------------------------------------------------------------------- #
# 3. Nothing from the shared folder before the safety number is confirmed
# --------------------------------------------------------------------------- #
CONTENT_OPS = [
    ("diff", {"max_chars": 5000}),
    ("read_file", {"path": "README.md"}),
    ("list_files", {}),
    ("screen", {}),
    ("mirror_pull", {"have": "", "offset": 0}),
]


@pytest.mark.parametrize("op,p", CONTENT_OPS)
def test_content_ops_refused_until_it_matches(engine, op, p):
    lnk = link(share_id=SHARE_ID, session_title=TITLE, sas_verified=False)
    svc = make_service(lnk)
    assert op_error(svc, lnk, op, p) == svc_mod.NOT_VERIFIED_ERR
    assert "safety number not confirmed" in svc_mod.NOT_VERIFIED_ERR
    assert svc_mod.NOT_VERIFIED_ERR.startswith("not permitted")  # older peers


def test_messages_and_status_stay_open_before_it_matches(engine):
    lnk = link(sas_verified=False)
    svc = make_service(lnk)
    msg = {"msg_id": "m1", "text": "is it 482-019?", "reply_to": None}
    assert run(svc.handle_request(lnk, "msg", msg)) == {"accepted": True}
    assert run(svc.handle_request(lnk, "status", {}))["shared"] is False


def test_perms_are_still_checked_first(engine):
    lnk = link(
        perms={"messages": True, "diff": False, "read_file": True},
        share_id=SHARE_ID,
        sas_verified=False,
    )
    svc = make_service(lnk)
    assert op_error(svc, lnk, "diff", {"max_chars": 5000}) == "not permitted"


def test_service_and_transport_agree_on_the_rule():
    assert svc_mod._SAS_OPS == transport._SAS_OPS
    assert svc_mod.NOT_VERIFIED_ERR == transport.NOT_VERIFIED_ERR
    assert "msg" not in svc_mod._SAS_OPS and "status" not in svc_mod._SAS_OPS


def test_their_refusal_is_worded_for_the_person(engine):
    svc = make_service()
    err = svc._peer_error(link(), transport.PeerOpError(svc_mod.NOT_VERIFIED_ERR))
    assert err.status == 403
    assert err.message.startswith("Bob hasn't confirmed the safety number yet")
    off = svc._peer_error(link(), transport.PeerOpError("not permitted"), "read_file")
    assert "“read my files”" in off.message and "People" in off.message


def test_share_mirror_export_routes_409_until_it_matches(monkeypatch, engine):
    s = make_service(link(sas_verified=False, share_id=SHARE_ID))
    monkeypatch.setattr(svc_mod, "_SERVICE", s)
    from backend.web import server

    client = TestClient(server.app)
    calls = [
        ("/api/peer/links/%s/share" % LID, {"repo_path": "/tmp"}),
        ("/api/peer/links/%s/mirror" % LID, {"repo_path": "/tmp"}),
        (
            "/api/peer/links/%s/export" % LID,
            {"target_repo": "/tmp", "branch_name": "peer/x"},
        ),
        ("/api/peer/links/%s/share/reopen" % LID, {}),
    ]
    for path, body in calls:
        r = client.post(path, json=body)
        assert r.status_code == 409, (path, r.text)
        assert r.json()["error"] == "Confirm the safety number with Bob first"


# --------------------------------------------------------------------------- #
# 4. Turning peer links off keeps every link and share
# --------------------------------------------------------------------------- #
def test_disable_closes_shares_keeps_links_and_enable_reopens_nothing(
    engine, ends, tmp_path, monkeypatch
):
    lnk = shared_link(engine, tmp_path)
    svc = make_service(lnk)
    run(svc.start())
    out = run(svc.disable())
    assert out == {"enabled": False, "closed": [TITLE], "links": 1}
    assert ends.closed == [TITLE] and ends.deleted == []
    assert svc._transport is None and svc.state["enabled"] is False
    # The link and its share record stay, and are still listed.
    assert svc.store.get(LID).share_id == SHARE_ID
    (view,) = svc.status()["links"]
    assert view["shared"] is True and view["share_open"] is False
    assert view["connected"] is False

    # On again: the list comes back, nothing restarts by itself.
    from backend.web import server

    starts = []

    async def create_result(payload, *, peer_share=""):
        starts.append(payload["title"])
        engine[payload["title"]] = mk_inst(
            payload["title"], payload["repo_path"], peer_share=peer_share
        )
        return 202, {"title": payload["title"]}

    monkeypatch.setattr(server._session_create, "create_result", create_result)
    svc.state["enabled"] = True
    run(svc.reconcile())
    assert starts == [] and TITLE not in engine
    (view,) = svc.status()["links"]
    assert view["shared"] is True and view["share_open"] is False

    # The person reopens it from the card.
    fake_sandbox(monkeypatch, ok=True)
    _fake_runtime(monkeypatch)
    res = run(svc.reopen_share(LID, program="claude"))
    assert starts == [TITLE]
    assert res["link"]["share_open"] is True
    with pytest.raises(svc_mod.PeerServiceError, match="already open") as again:
        run(svc.reopen_share(LID))
    assert again.value.status == 409


def test_reconcile_to_off_closes_shares_the_same_way(engine, ends, tmp_path):
    svc = make_service(shared_link(engine, tmp_path))
    run(svc.start())
    svc.state["enabled"] = False  # the Settings switch, then any /api/peer call
    run(svc.reconcile())
    assert ends.closed == [TITLE] and ends.deleted == []
    assert svc.store.get(LID).share_id == SHARE_ID


def test_server_shutdown_never_closes_shared_sessions(engine, ends, tmp_path):
    svc = make_service(shared_link(engine, tmp_path))
    run(svc.start())
    run(svc.stop())  # lifespan shutdown: the session must survive a restart
    assert ends.closed == ends.deleted == []


def test_off_with_no_links_file_creates_nothing(tmp_path):
    from backend.peer.store import LinkStore

    path = tmp_path / "peer" / "links.json"
    svc = svc_mod.PeerService(
        store_factory=lambda: LinkStore(str(path)),
        settings_getter=lambda: {"enabled": False},
    )
    assert svc.status()["links"] == []
    assert not path.parent.exists()


def _fake_runtime(monkeypatch):
    class Part:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    import sys
    import types

    import backend.peer as peer_pkg

    for name, attr in (("egress", "EgressProxy"), ("agent_api", "AgentApi")):
        mod = types.ModuleType("backend.peer." + name)
        setattr(mod, attr, Part)
        monkeypatch.setitem(sys.modules, "backend.peer." + name, mod)
        monkeypatch.setattr(peer_pkg, name, mod, raising=False)
    monkeypatch.setattr(peer_launch, "_TOKENS", {})


# --------------------------------------------------------------------------- #
# 5. Export: one sentence, and how much came home
# --------------------------------------------------------------------------- #
def test_export_result_names_branch_repo_and_commit_count(
    engine, tmp_path, monkeypatch
):
    from backend.peer import share as share_mod

    target = tmp_path / "widgets"
    target.mkdir()
    monkeypatch.setattr(
        share_mod,
        "export",
        lambda share, t, b: {"branch": b, "sha": "a" * 40, "target": t, "commits": 3},
    )
    svc = make_service(link(share_id=SHARE_ID))
    svc._share_obj = lambda sid: SimpleNamespace(share_id=sid)
    out = run(svc.export(LID, str(target), "peer/ana"))
    assert out["branch"] == "peer/ana"
    assert out["repo_path"] == str(target.resolve())
    assert out["commits"] == 3


@pytest.mark.parametrize(
    "raised,said",
    [
        (
            "export failed: branch not updated",
            "Couldn't bring the work home: the branch didn't move.",
        ),
        (
            "target lacks the shared folder's history",
            "That repo doesn't have the history the shared folder started from",
        ),
        ("target is not a git repository", "That folder isn't a git repository"),
        ("branch peer/ana is checked out in the target", "peer/ana is checked out"),
    ],
)
def test_export_errors_are_one_sentence(engine, tmp_path, monkeypatch, raised, said):
    from backend.peer import share as share_mod

    def boom(*a):
        raise share_mod.ShareError(raised)

    monkeypatch.setattr(share_mod, "export", boom)
    svc = make_service(link(share_id=SHARE_ID))
    svc._share_obj = lambda sid: SimpleNamespace(share_id=sid)
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.export(LID, str(tmp_path), "peer/ana"))
    msg = exc.value.message
    assert msg.startswith(said)
    assert msg.count("export failed") == 0 and msg.endswith(".")


def test_export_input_errors_name_no_field(engine, tmp_path):
    svc = make_service(link(share_id=SHARE_ID))
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.export(LID, str(tmp_path / "nope"), "peer/ana"))
    assert "target_repo" not in exc.value.message
    assert exc.value.message.startswith("That folder doesn't exist")


# --------------------------------------------------------------------------- #
# 6. Every incoming message reaches the bell
# --------------------------------------------------------------------------- #
def test_every_incoming_message_emits_text_head(engine, tmp_path):
    svc = make_service(shared_link(engine, tmp_path))
    lnk = svc.store.get(LID)
    text = "line one\nline two " + "x" * 300
    run(svc.handle_request(lnk, "msg", {"msg_id": "m", "text": text, "reply_to": None}))
    (data,) = [d for e, d in svc.events if e == "peer.message"]
    assert data["link_id"] == LID and data["peer_name"] == "Bob"
    assert data["text_head"].startswith("line one line two")
    assert "\n" not in data["text_head"] and len(data["text_head"]) <= 200
    assert data["stored"] is False and data["delivered_to"] == TITLE


# --------------------------------------------------------------------------- #
# 7. Backend copy
# --------------------------------------------------------------------------- #
def test_a_cut_off_code_is_called_cut_off(engine):
    svc = make_service()
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.join("Join me on MindFlock: mfp1:beytenzogaxdalrrjfxx6nq"))
    assert exc.value.message == "That code is cut off — paste the whole invite message."
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.join("hello there"))
    assert exc.value.message.startswith("That doesn't contain an invite code")


def test_the_invite_message_says_where_to_paste_and_who_it_reaches():
    relay = svc_mod.invite_message("mfp2:abc-defg", 600)
    assert "Settings → People" in relay and 'type "people"' in relay
    assert "Join someone" in relay and "Join a peer" not in relay
    assert "same network" not in relay  # a relay invite reaches anywhere
    direct = svc_mod.invite_message("mfp1:abc-defg", 600, direct=True)
    assert "same network as me (or the same Tailscale network)" in direct
    assert "mfp1:abc-defg" in direct and "expires in 10 min" in direct


def test_a_new_listen_port_rebinds_the_listener(engine):
    class Listening(FakeTransport):
        def __init__(self, *a):
            self.calls, self.bound = [], None

        @property
        def listening(self):
            return self.bound

        def start_listener(self, host, port):
            self.bound = (host, port)

        def stop_listener(self):
            self.bound = None

    svc = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore([link()]),
        invites_factory=FakeInvites,
        transport_factory=Listening,
        settings_getter=lambda: dict(svc_state),
    )
    svc_state = {
        "enabled": True,
        "listen_host": "0.0.0.0",
        "listen_port": 8799,
        "relay": "off",
    }
    run(svc.sync_listener())
    assert svc.transport.bound == ("0.0.0.0", 8799)
    assert svc.status()["listen"]["bound"] == "0.0.0.0:8799"
    svc_state["listen_port"] = 18799
    run(svc.sync_listener())
    assert svc.transport.bound == ("0.0.0.0", 18799)
    run(svc.sync_listener())  # unchanged settings: left alone
    assert svc.transport.bound == ("0.0.0.0", 18799)
