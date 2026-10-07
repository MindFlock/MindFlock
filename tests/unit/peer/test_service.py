"""PeerService: the transport handler (OUR perms on inbound ops, "no shared
folder", messages only into the bound shared session) and the share /
unshare flows (fail closed without a sandbox; rollback on failure)."""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

import backend.peer as peer_pkg
from backend.peer import launch as peer_launch
from backend.peer import paths
from backend.peer import service as svc_mod
from backend.web.core import mailbox as mb

from ._integration_helpers import SHARE_ID, fake_sandbox, make_share, mk_inst
from .test_integration_routes import FakeInvites, FakeStore, FakeTransport, link

LID = "ab" * 16


def run(coro):
    return asyncio.run(coro)


def install(monkeypatch, name, **attrs):
    mod = types.ModuleType("backend.peer." + name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, "backend.peer." + name, mod)
    monkeypatch.setattr(peer_pkg, name, mod, raising=False)
    return mod


@pytest.fixture
def engine(monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    return server.ENGINE.instances


def make_service(*links, enabled=True):
    return svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore(links or [link()]),
        invites_factory=FakeInvites,
        transport_factory=FakeTransport,
        settings_getter=lambda: {
            "enabled": enabled,
            "listen_host": "0.0.0.0",
            "listen_port": 8799,
            "display_name": "alice",
            "advertise_host": "",
            "egress_allow": [],
        },
    )


def op_error(svc, lnk, op, p):
    with pytest.raises(Exception) as exc:
        run(svc.handle_request(lnk, op, p))
    return str(exc.value)


# --------------------------------------------------------------------------- #
# Handler: permissions
# --------------------------------------------------------------------------- #
NO_PERMS = {"messages": False, "diff": False, "read_file": False}


@pytest.mark.parametrize(
    "op,p",
    [
        ("msg", {"msg_id": "a1", "text": "hi", "reply_to": None}),
        ("diff", {"max_chars": 5000}),
        ("read_file", {"path": "README.md"}),
        ("list_files", {}),
    ],
)
def test_refused_ops_say_not_permitted(engine, op, p):
    lnk = link(perms=dict(NO_PERMS), share_id=SHARE_ID, session_title="t")
    svc = make_service(lnk)
    assert op_error(svc, lnk, op, p) == "not permitted"


def test_perms_are_read_fresh_from_the_store(engine):
    # The transport may hand a stale link; a just-revoked perm must hold.
    stale = link(share_id=SHARE_ID)
    svc = make_service(link(perms=dict(NO_PERMS), share_id=SHARE_ID))
    assert op_error(svc, stale, "diff", {"max_chars": 5000}) == "not permitted"


def test_list_files_follows_read_file(engine):
    lnk = link(
        perms={"messages": True, "diff": True, "read_file": False}, share_id=SHARE_ID
    )
    svc = make_service(lnk)
    assert op_error(svc, lnk, "list_files", {}) == "not permitted"


def test_unknown_op_refused(engine):
    svc = make_service()
    assert op_error(svc, link(), "exec", {}) == "not permitted"


def test_status_always_allowed(engine):
    lnk = link(perms=dict(NO_PERMS))
    svc = make_service(lnk)
    assert run(svc.handle_request(lnk, "status", {})) == {
        "shared": False,
        "agent": "none",
        "name": "alice",
    }


@pytest.mark.parametrize(
    "op,p",
    [("diff", {"max_chars": 5000}), ("read_file", {"path": "a"}), ("list_files", {})],
)
def test_share_ops_without_share(engine, op, p):
    svc = make_service()
    assert op_error(svc, link(), op, p) == "no shared folder"


def test_share_ops_read_our_share(engine, monkeypatch):
    make_share()
    seen = []
    Share = lambda **kw: SimpleNamespace(**kw)  # noqa: E731
    install(
        monkeypatch,
        "share",
        Share=Share,
        open_share=lambda sid: SimpleNamespace(share_id=sid, **paths.share_paths(sid)),
        diff=lambda share, mc: seen.append(("diff", share.share_id, mc))
        or {"stat": [], "diff": "", "truncated": False},
        read_file=lambda share, path: {
            "path": path,
            "size": 2,
            "encoding": "utf-8",
            "content": "hi",
            "truncated": False,
        },
        list_files=lambda share: {"files": ["a"], "truncated": False},
    )
    lnk = link(share_id=SHARE_ID)
    svc = make_service(lnk)
    assert (
        run(svc.handle_request(lnk, "diff", {"max_chars": 5000}))["truncated"] is False
    )
    assert seen == [("diff", SHARE_ID, 5000)]
    assert run(svc.handle_request(lnk, "read_file", {"path": "x"}))["content"] == "hi"
    assert run(svc.handle_request(lnk, "list_files", {}))["files"] == ["a"]


def test_read_file_failures_are_one_generic_answer(engine, monkeypatch):
    make_share()

    def boom(share, path):
        raise PermissionError("/home/alice/.ssh/id_rsa: permission denied")

    install(
        monkeypatch,
        "share",
        open_share=lambda sid: SimpleNamespace(share_id=sid),
        read_file=boom,
    )
    lnk = link(share_id=SHARE_ID)
    svc = make_service(lnk)
    assert op_error(svc, lnk, "read_file", {"path": "../../.ssh/id_rsa"}) == "not found"


# --------------------------------------------------------------------------- #
# Handler: inbound messages
# --------------------------------------------------------------------------- #
def test_msg_lands_in_the_shared_session_as_peer(engine, tmp_path):
    make_share()
    engine["peer-bob-abab"] = mk_inst(
        "peer-bob-abab", paths.share_paths(SHARE_ID)["work"], peer_share=SHARE_ID
    )
    lnk = link(share_id=SHARE_ID, session_title="peer-bob-abab", peer_name='Bob"]')
    svc = make_service(lnk)
    res = run(
        svc.handle_request(
            lnk, "msg", {"msg_id": "pm1", "text": "look at foo.py", "reply_to": None}
        )
    )
    assert res == {"accepted": True}
    (m,) = mb.fetch("peer-bob-abab")["messages"]
    assert m["from"] == "peer:Bob"
    assert m["data"] == {"peer_msg_id": "pm1", "link_id": LID}
    assert m["delivery"] == "auto"


def test_msg_never_reaches_an_ordinary_session(engine, tmp_path):
    engine["victim"] = mk_inst("victim", str(tmp_path / "v"))
    lnk = link(session_title="victim")
    svc = make_service(lnk)
    res = run(
        svc.handle_request(
            lnk, "msg", {"msg_id": "pm1", "text": "rm -rf", "reply_to": None}
        )
    )
    assert res == {"accepted": False}
    assert mb.unread_count("victim") == 0


def test_msg_without_bound_session(engine):
    svc = make_service()
    assert run(
        svc.handle_request(
            link(), "msg", {"msg_id": "p", "text": "x", "reply_to": None}
        )
    ) == {"accepted": False}


# --------------------------------------------------------------------------- #
# Share / unshare
# --------------------------------------------------------------------------- #
@pytest.fixture
def share_fakes(monkeypatch, engine):
    rec = SimpleNamespace(created=[], removed=[], egress=[], apis=[], creates=[])

    def create_share(link_id, repo, branch=None):
        p = make_share()
        rec.created.append((link_id, repo, branch))
        return SimpleNamespace(share_id=SHARE_ID, **p)

    def remove_share(share_id, is_running=None):
        rec.removed.append((share_id, bool(is_running())))
        return True

    install(
        monkeypatch,
        "share",
        create_share=create_share,
        remove_share=remove_share,
        open_share=lambda sid: SimpleNamespace(share_id=sid, **paths.share_paths(sid)),
    )

    class Egress:
        def __init__(self, sock, allow):
            self.sock, self.allow, self.running = sock, allow, False
            rec.egress.append(self)

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

    class Api:
        def __init__(self, share, link_id, token, service, title):
            self.token, self.title, self.running = token, title, False
            rec.apis.append(self)

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

    install(monkeypatch, "egress", EgressProxy=Egress)
    install(monkeypatch, "agent_api", AgentApi=Api)
    monkeypatch.setattr(peer_launch, "_TOKENS", {})

    from backend.web import server

    async def create_result(payload, *, peer_share=""):
        rec.creates.append((payload, peer_share, peer_launch.token_for(peer_share)))
        return rec.status, (
            {"title": payload["title"]} if rec.status < 300 else {"error": "nope"}
        )

    rec.status = 202
    monkeypatch.setattr(server._session_create, "create_result", create_result)
    return rec


def test_share_refused_without_sandbox(share_fakes, monkeypatch, tmp_path):
    fake_sandbox(monkeypatch, ok=False)
    svc = make_service()
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.share(LID, str(tmp_path), program="claude"))
    assert exc.value.status == 409
    assert share_fakes.created == [] and share_fakes.creates == []


def test_share_refused_for_unsupported_cli(share_fakes, monkeypatch, tmp_path):
    fake_sandbox(monkeypatch, ok=True)
    svc = make_service()
    with pytest.raises(svc_mod.PeerServiceError, match="claude and codex"):
        run(svc.share(LID, str(tmp_path), program="aider"))
    assert share_fakes.created == []


def test_share_refuses_a_folder_in_the_peer_root(share_fakes, monkeypatch):
    fake_sandbox(monkeypatch, ok=True)
    p = make_share("cd" * 16)
    svc = make_service()
    with pytest.raises(svc_mod.PeerServiceError, match="peer root"):
        run(svc.share(LID, p["work"], program="claude"))


def test_share_starts_runtime_then_sandboxed_session(
    share_fakes, monkeypatch, tmp_path
):
    fake_sandbox(monkeypatch, ok=True)
    svc = make_service()
    out = run(svc.share(LID, str(tmp_path), branch="main", program="claude"))
    assert share_fakes.created == [(LID, str(tmp_path.resolve()), "main")]
    ((payload, peer_share, token_at_create),) = share_fakes.creates
    assert peer_share == SHARE_ID
    assert token_at_create  # the runtime (and its token) is up BEFORE the launch
    assert payload["in_place"] is True
    assert payload["repo_path"] == paths.share_paths(SHARE_ID)["work"]
    assert payload["title"].startswith("peer-bob-")
    assert "untrusted" in payload["prompt"]
    (egress,) = share_fakes.egress
    assert egress.running and egress.sock.endswith("/run/egress.sock")
    (api,) = share_fakes.apis
    assert api.running and api.token == token_at_create and len(api.token) >= 16
    lnk = svc.store.get(LID)
    assert lnk.share_id == SHARE_ID and lnk.session_title == payload["title"]
    assert out["link"]["shared"] is True
    with pytest.raises(svc_mod.PeerServiceError) as again:
        run(svc.share(LID, str(tmp_path), program="claude"))
    assert again.value.status == 409


def test_share_rolls_back_when_the_session_fails(share_fakes, monkeypatch, tmp_path):
    fake_sandbox(monkeypatch, ok=True)
    share_fakes.status = 400
    svc = make_service()
    with pytest.raises(svc_mod.PeerServiceError):
        run(svc.share(LID, str(tmp_path), program="claude"))
    assert peer_launch.token_for(SHARE_ID) == ""
    assert not share_fakes.egress[0].running and not share_fakes.apis[0].running
    assert share_fakes.removed == [(SHARE_ID, False)]
    assert svc.store.get(LID).share_id is None


def test_unshare_stops_runtime_and_optionally_deletes(
    share_fakes, monkeypatch, tmp_path
):
    fake_sandbox(monkeypatch, ok=True)
    svc = make_service()
    run(svc.share(LID, str(tmp_path), program="claude"))
    out = run(svc.unshare(LID, delete_files=True))
    assert out["deleted"] is True
    assert share_fakes.removed == [(SHARE_ID, False)]
    assert peer_launch.token_for(SHARE_ID) == ""
    assert svc.store.get(LID).share_id is None


def test_disabled_service_constructs_nothing(engine):
    svc = make_service(enabled=False)
    run(svc.start())
    assert svc._transport is None and svc._identity is None
    assert svc.status()["links"] == []
