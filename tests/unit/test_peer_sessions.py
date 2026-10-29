"""Shared sessions over a peer link (docs/peer-link.md "Shared sessions —
CONTRACT"): the service (what I share, what they share, the ops, the
events) and the routes — ``/api/peer/links/{id}/sessions*`` and the
``<peer>::<title>`` rows on this rail, whose terminal rides the link."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.peer import service as svc_mod
from tests.unit.peer._integration_helpers import SHARE_ID, mk_inst
from tests.unit.peer.test_integration_routes import FakeInvites, FakeStore, link

LID = "ab" * 16
TID = "1" * 16


class Transport:
    """The transport as the service sees it, plus what the peer answers."""

    def __init__(self, *a):
        self.listening = False
        self.connected = True
        self.requests = []
        self.pushes = []
        self.answers = {}

    def start(self):
        pass

    def start_listener(self, host, port):
        self.listening = True

    def stop_listener(self):
        self.listening = False

    def is_connected(self, lid):
        return self.connected

    def close(self):
        pass

    def peer_info(self, lid):
        return {}

    async def request(self, lid, op, p, timeout=60.0):
        self.requests.append((lid, op, p))
        ans = self.answers.get(op, {"ok": True})
        if isinstance(ans, Exception):
            raise ans
        return ans

    async def send_push(self, lid, op, p):
        self.pushes.append((lid, op, p))


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))

    def named(self, name):
        return [kw for n, kw in self.events if n == name]


def item(title="api-fix", **kw):
    base = {
        "title": title,
        "name": "API fix",
        "status": "running",
        "activity": "working",
        "stage": "agent",
        "branch": "feature/api",
        "folder": "app",
        "mode": "full",
        "program": "claude",
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def peer_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer-home"))


@pytest.fixture
def bus(monkeypatch):
    from backend.web.core import events

    b = Bus()
    monkeypatch.setattr(events, "BUS", b)
    return b


@pytest.fixture
def engine(monkeypatch, tmp_path):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    wt = str(tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    server.ENGINE.instances["api-fix"] = mk_inst("api-fix", wt)
    server.ENGINE.instances["docs"] = mk_inst("docs", wt)
    server.ENGINE.instances["peer-bob-abab"] = mk_inst(
        "peer-bob-abab", wt, peer_share=SHARE_ID
    )
    return server.ENGINE.instances


def make_svc(monkeypatch, *links):
    s = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore(links or [link()]),
        invites_factory=FakeInvites,
        transport_factory=Transport,
        settings_getter=lambda: {
            "enabled": True,
            "listen_host": "0.0.0.0",
            "listen_port": 8799,
            "display_name": "alice",
            "advertise_host": "100.64.0.1",
            "egress_allow": [],
            "relay": "off",
        },
    )
    monkeypatch.setattr(svc_mod, "_SERVICE", s)
    s.transport  # built: rows are only offered with a transport
    return s


@pytest.fixture
def svc(monkeypatch):
    return make_svc(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def op(svc, lnk, name, p):
    return run(svc.handle_request(lnk, name, p))


def op_error(svc, lnk, name, p):
    with pytest.raises(Exception) as exc:
        op(svc, lnk, name, p)
    return str(exc.value)


# --------------------------------------------------------------------------- #
# The sharer: inbound ops
# --------------------------------------------------------------------------- #
def test_sessions_lists_only_what_is_shared_and_live(svc, engine):
    svc.shared_sessions.add(LID, "api-fix", "full")
    svc.shared_sessions.add(LID, "gone", "full")  # no such session
    # A gated record whose session is not this link's shared folder.
    svc.shared_sessions.add(LID, "docs", "gated")
    out = op(svc, link(), "sessions", {})["sessions"]
    assert [i["title"] for i in out] == ["api-fix"]
    got = out[0]
    assert got["mode"] == "full" and got["branch"] == "main"
    assert got["program"] == "claude"
    # The folder's NAME, never a path.
    assert got["folder"] == "wt" and "/" not in got["folder"]
    from backend.peer import wire

    wire.validate_response("sessions", {"sessions": out})


def test_gated_session_listed_while_it_is_the_links_shared_folder(svc, engine):
    lnk = link(share_id=SHARE_ID, session_title="peer-bob-abab")
    svc = svc  # noqa: PLW0127
    svc.store.links[LID] = lnk
    svc.shared_sessions.add(LID, "peer-bob-abab", "gated")
    svc.shared_sessions.add(LID, "peer-bob-abab-full", "full")
    out = op(svc, lnk, "sessions", {})["sessions"]
    assert [(i["title"], i["mode"]) for i in out] == [("peer-bob-abab", "gated")]


def test_a_gated_share_is_called_what_its_source_session_is(svc, engine):
    # Join trial 2026-10-28: an invite opening "docs (gated)" put a row called
    # "peer-bob-abab" on the joiner's rail — the sandboxed copy's title.
    from backend.web.core import aliases

    lnk = link(share_id=SHARE_ID, session_title="peer-bob-abab")
    svc.store.links[LID] = lnk
    svc.shared_sessions.add(LID, "peer-bob-abab", "gated", "docs")
    (got,) = op(svc, lnk, "sessions", {})["sessions"]
    assert got["title"] == "peer-bob-abab"
    assert got["name"] == aliases.display_name("docs", "main") != "peer-bob-abab"


def test_session_ops_need_the_safety_number_not_a_perm(svc, engine):
    off = {"messages": False, "diff": False, "read_file": False}
    svc.shared_sessions.add(LID, "api-fix", "full")
    lnk = link(perms=off)
    svc.store.links[LID] = lnk
    assert op(svc, lnk, "sessions", {})["sessions"]
    unverified = link(sas_verified=False)
    svc.store.links[LID] = unverified
    for name, p in (
        ("sessions", {}),
        ("term_open", {"title": "api-fix", "cols": 80, "rows": 24}),
        ("session_focus", {"title": "api-fix"}),
    ):
        assert "safety number" in op_error(svc, unverified, name, p)


def test_term_open_refuses_what_is_not_shared(svc, engine):
    assert (
        op_error(svc, link(), "term_open", {"title": "api-fix", "cols": 80, "rows": 24})
        == "not shared"
    )


def test_term_open_attaches_and_shows_the_pill(svc, engine, bus, monkeypatch):
    from backend.web import server

    svc.shared_sessions.add(LID, "api-fix", "full")
    monkeypatch.setattr(
        server, "ensure_terminal_session", lambda inst, t: ("mf_" + t, None)
    )
    opened = []

    async def open_mine(lid, title, name, cols, rows, **kw):
        opened.append((lid, title, name, cols, rows, kw["may_type"]()))
        return TID

    monkeypatch.setattr(svc.bridge, "open_mine", open_mine)
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    res = op(svc, link(), "term_open", {"title": "api-fix", "cols": 90, "rows": 30})
    assert res == {"ok": True, "term_id": TID}
    assert opened == [(LID, "api-fix", "mf_api-fix", 90, 30, True)]
    (focus,) = bus.named("guest.focus_changed")
    assert focus["data"] == {
        "link": "peer:" + LID,
        "label": "Bob",
        "title": "api-fix",
        "joined": True,
    }
    # Their page moved off my sessions (and no terminal of theirs is open).
    op(svc, link(), "session_focus", {"title": ""})
    assert bus.named("guest.focus_changed")[-1]["data"]["left"] is True


def test_term_open_says_why_the_session_cant_start(svc, engine, monkeypatch):
    from backend.web import server

    svc.shared_sessions.add(LID, "api-fix", "full")
    monkeypatch.setattr(
        server, "ensure_terminal_session", lambda inst, t: (None, "paused")
    )
    err = op_error(
        svc, link(), "term_open", {"title": "api-fix", "cols": 80, "rows": 24}
    )
    assert err == "the session can't start: paused"


def test_session_focus_on_an_unshared_title_is_ignored(svc, engine, bus):
    op(svc, link(), "session_focus", {"title": "docs"})
    assert bus.named("guest.focus_changed") == []


# --------------------------------------------------------------------------- #
# Sharing / withdrawing
# --------------------------------------------------------------------------- #
def test_share_full(svc, engine, bus):
    svc._sessions_aware.add(LID)
    view = run(svc.share_session(LID, "api-fix", "full"))
    assert [(m["title"], m["mode"], m["live"]) for m in view["mine"]] == [
        ("api-fix", "full", True)
    ]
    (ev,) = bus.named("peer.session_shared")
    assert ev["data"] == {
        "link_id": LID,
        "peer_name": "Bob",
        "title": "api-fix",
        "mode": "full",
        "by": "you",
        "theirs": False,
    }
    # They speak the ops: told at once.
    assert svc.transport.pushes == [(LID, "sessions_changed", {})]


def test_share_does_not_push_to_a_peer_that_never_asked(svc, engine):
    run(svc.share_session(LID, "api-fix", "full"))
    assert svc.transport.pushes == []  # an older peer would close on it


@pytest.mark.parametrize(
    "title,mode,status",
    [
        ("nope", "full", 404),
        ("Bob::x", "full", 404),
        ("api-fix", "root", 400),
    ],
)
def test_share_refusals(svc, engine, title, mode, status):
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.share_session(LID, title, mode))
    assert exc.value.status == status


def test_share_waits_for_it_matches(monkeypatch, engine):
    svc = make_svc(monkeypatch, link(sas_verified=False))
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.share_session(LID, "api-fix", "full"))
    assert exc.value.status == 409


def test_share_gated_cuts_the_links_shared_folder(svc, engine, monkeypatch):
    calls = []

    async def share(link_id, repo_path, branch=None, program="", **kw):
        calls.append((link_id, repo_path, branch, program))
        svc.store.update(link_id, share_id=SHARE_ID, session_title="peer-bob-abab")
        return {}

    monkeypatch.setattr(svc, "share", share)
    view = run(svc.share_session(LID, "api-fix", "gated"))
    assert calls and calls[0][0] == LID and calls[0][2] == "main"
    assert calls[0][3] == "claude"
    (mine,) = view["mine"]
    assert mine["title"] == "peer-bob-abab" and mine["mode"] == "gated"
    assert mine["source"] == "api-fix" and mine["live"] is True


def test_share_gated_refused_when_the_link_has_a_folder(monkeypatch, engine):
    svc = make_svc(monkeypatch, link(share_id=SHARE_ID, session_title="peer-bob-abab"))
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.share_session(LID, "api-fix", "gated"))
    assert exc.value.status == 409 and "one gated session" in exc.value.message
    # …but the shared-folder session itself is shared gated, whatever was asked.
    view = run(svc.share_session(LID, "peer-bob-abab", "full"))
    assert [(m["title"], m["mode"]) for m in view["mine"]] == [
        ("peer-bob-abab", "gated")
    ]


def test_withdraw_full_leaves_the_session(svc, engine, bus, monkeypatch):
    run(svc.share_session(LID, "api-fix", "full"))
    closed = []

    async def close_title(lid, title, reason):
        closed.append((lid, title, reason))

    monkeypatch.setattr(svc.bridge, "close_title", close_title)
    view = run(svc.unshare_session(LID, "api-fix"))
    assert view["mine"] == []
    assert closed == [(LID, "api-fix", "withdrawn")]
    assert "api-fix" in engine
    (ev,) = bus.named("peer.session_unshared")
    assert ev["data"]["mode"] == "" and ev["data"]["by"] == "you"
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.unshare_session(LID, "api-fix"))
    assert exc.value.status == 404


def test_withdraw_gated_closes_it_like_stop_sharing(monkeypatch, engine):
    svc = make_svc(monkeypatch, link(share_id=SHARE_ID, session_title="peer-bob-abab"))
    svc.shared_sessions.add(LID, "peer-bob-abab", "gated")
    calls = []

    async def unshare(link_id, delete_files=False):
        calls.append((link_id, delete_files))
        return {}

    monkeypatch.setattr(svc, "unshare", unshare)
    run(svc.unshare_session(LID, "peer-bob-abab"))
    assert calls == [(LID, False)]  # folder kept


# --------------------------------------------------------------------------- #
# The opener: what they share with us
# --------------------------------------------------------------------------- #
def test_their_sessions_cached_and_changes_announced(svc, bus):
    t = svc.transport
    t.answers["sessions"] = {"sessions": [item()]}
    run(svc.refresh_their_sessions(LID))
    # The first answer after start is the state, not news.
    assert bus.named("peer.session_shared") == []
    t.answers["sessions"] = {"sessions": [item(title="docs", mode="gated")]}
    run(svc.refresh_their_sessions(LID))
    shared = bus.named("peer.session_shared")
    unshared = bus.named("peer.session_unshared")
    assert [e["data"]["title"] for e in shared] == ["docs"]
    assert shared[0]["data"]["by"] == "peer" and shared[0]["data"]["theirs"] is True
    assert [e["data"]["title"] for e in unshared] == ["api-fix"]
    assert svc.their_sessions(LID)[0]["title"] == "docs"


def test_a_new_links_first_list_is_news(svc, bus):
    # Join trial 2026-10-28: the joiner's first list already held the
    # invite's full session, so only the gated one (shared a moment later)
    # got a peer.session_shared — one toast and bell row of the two.
    svc.on_link_added(link(role="dialer"))
    svc.transport.answers["sessions"] = {"sessions": [item()]}
    run(svc.refresh_their_sessions(LID))
    (ev,) = bus.named("peer.session_shared")
    assert ev["data"]["title"] == "api-fix" and ev["data"]["theirs"] is True


def test_an_older_peer_is_remembered_as_unsupported(svc):
    from backend.peer.transport import PeerOpError

    svc.transport.answers["sessions"] = PeerOpError("unsupported")
    run(svc.refresh_their_sessions(LID))
    assert svc.sessions_view(LID)["their_unsupported"] is True
    n = len(svc.transport.requests)
    run(svc.refresh_their_sessions(LID))
    assert len(svc.transport.requests) == n  # not asked again until reconnect


def test_peer_rows_look_like_device_rows(svc, monkeypatch):
    svc.transport.answers["sessions"] = {"sessions": [item()]}
    run(svc.refresh_their_sessions(LID))
    (row,) = svc.peer_rows()
    assert row["title"] == "Bob::api-fix"
    assert row["device"] == "Bob" and row["device_label"] == "Bob"
    assert row["peer_link"] == LID and row["peer_mode"] == "full"
    assert row["remote_peer"] is True and row["peer_share"] is True
    assert row["display_title"] == "API fix" and row["activity"] == "working"
    # A tailnet device already called "bob" keeps its name; the peer moves.
    from backend.web.core import remote

    monkeypatch.setitem(remote._DEVICES, "bob", {"key": "bob"})
    assert [r["title"] for r in svc.peer_rows()] == ["Bob (2)::api-fix"]
    svc.transport.connected = False
    assert svc.peer_rows() == []


def test_sessions_changed_push_reasks(svc):
    svc.transport.answers["sessions"] = {"sessions": [item()]}

    async def main():
        await svc.handle_push(LID, "sessions_changed", {})
        await asyncio.sleep(0.01)

    run(main())
    assert [op for _l, op, _p in svc.transport.requests] == ["sessions"]
    assert LID in svc._sessions_aware


def test_link_down_drops_their_rows(svc):
    svc.transport.answers["sessions"] = {"sessions": [item()]}
    run(svc.refresh_their_sessions(LID))
    svc.transport.connected = False
    run(svc._sessions_link_down(LID, "disconnected"))
    assert svc.their_sessions(LID) == []


def test_link_gone_forgets_what_i_shared(svc, engine):
    svc.shared_sessions.add(LID, "api-fix", "full")
    run(svc._sessions_link_gone(LID))
    assert svc.shared_sessions.get(LID) == {}
    assert "api-fix" in engine  # the session itself is untouched


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    from backend.web import server

    return TestClient(server.app)


@pytest.fixture
def theirs(svc):
    svc.transport.answers["sessions"] = {"sessions": [item()]}
    run(svc.refresh_their_sessions(LID))
    return svc


REMOTE = {"X-MindFlock-Remote": "other-device-key"}


def test_instances_list_carries_peer_rows(theirs, client, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(server.ENGINE, "instances", {})
    rows = client.get("/api/instances").json()
    peer = [r for r in rows if r.get("remote_peer")]
    assert [r["title"] for r in peer] == ["Bob::api-fix"]
    # Another MindFlock asking gets this device's own sessions only.
    from backend.web.core import remote

    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    rows = client.get("/api/instances", headers=REMOTE).json()
    assert isinstance(rows, list)
    assert not [r for r in rows if r.get("remote_peer")]


def test_row_routes(theirs, client):
    r = client.get("/api/instances/Bob::api-fix")
    assert r.status_code == 200 and r.json()["peer_link"] == LID
    assert client.get("/api/instances/Bob::nope").status_code == 404
    r = client.post("/api/instances/Bob::api-fix/commit", json={})
    assert r.status_code == 409 and "lives on Bob's machine" in r.json()["error"]
    r = client.delete("/api/instances/Bob::api-fix")
    assert r.status_code in (405, 409)


def test_row_routes_refuse_a_relayed_request(theirs, client, monkeypatch):
    from backend.web.core import remote

    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    r = client.get("/api/instances/Bob::api-fix", headers=REMOTE)
    assert r.status_code in (400, 403)


def test_an_unknown_prefix_is_still_a_device(theirs, client):
    r = client.get("/api/instances/stranger::api-fix")
    assert r.status_code == 502 and "not connected" in r.json()["error"]


def test_focus_forwards_session_focus(theirs, client):
    t = theirs.transport
    assert client.post("/api/instances/Bob::api-fix/focus", json={}).json() == {
        "ok": True,
        "peer": True,
    }
    client.post("/api/instances/Bob::api-fix/focus", json={"left": True})
    focus = [p for _l, op, p in t.requests if op == "session_focus"]
    assert focus == [{"title": "api-fix"}, {"title": ""}]


def test_terminal_socket_rides_the_link(theirs, client):
    t = theirs.transport
    t.answers["term_open"] = {"ok": True, "term_id": TID}
    with client.websocket_connect("/api/instances/Bob%3A%3Aapi-fix/terminal") as ws:
        ws.send_text('{"type":"resize","cols":120,"rows":40}')
        ws.send_bytes(b"ls\r")
        # Their output arrives as a push.
        portal = ws.portal if hasattr(ws, "portal") else None
        payload = {"term_id": TID, "data": base64.b64encode(b"hello").decode()}
        if portal is not None:
            portal.call(theirs.handle_push, LID, "term_out", payload)
        else:  # pragma: no cover — older starlette
            ws._loop.run_until_complete(theirs.handle_push(LID, "term_out", payload))
        assert ws.receive_bytes() == b"hello"
    opened = [p for _l, op, p in t.requests if op == "term_open"]
    assert opened == [{"title": "api-fix", "cols": 80, "rows": 24}]
    kinds = [(op, p) for _l, op, p in t.pushes]
    assert ("term_resize", {"term_id": TID, "cols": 120, "rows": 40}) in kinds
    assert (
        "term_in",
        {"term_id": TID, "data": base64.b64encode(b"ls\r").decode()},
    ) in kinds
    closes = [p for _l, op, p in t.requests if op == "term_close"]
    assert closes == [{"term_id": TID}]


def test_terminal_refused_by_the_peer_closes_gone(theirs, client):
    from backend.peer.transport import PeerOpError

    theirs.transport.answers["term_open"] = PeerOpError("not shared")
    with client.websocket_connect("/api/instances/Bob%3A%3Aapi-fix/terminal") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "stopped sharing" in msg["message"]
        with pytest.raises(Exception):
            ws.receive_bytes()


def test_people_routes(svc, engine, client, bus):
    base = "/api/peer/links/" + LID + "/sessions"
    svc.transport.answers["sessions"] = {"sessions": [item(title="theirs")]}
    r = client.post(base, json={"title": "api-fix", "mode": "full"})
    assert r.status_code == 200, r.text
    assert [m["title"] for m in r.json()["mine"]] == ["api-fix"]
    v = client.get(base).json()
    assert [m["title"] for m in v["mine"]] == ["api-fix"]
    assert [x["title"] for x in v["theirs"]] == ["theirs"]
    r = client.request("DELETE", base + "/api-fix")
    assert r.status_code == 200 and r.json()["mine"] == []
    r = client.request("DELETE", base + "/api-fix")
    assert r.status_code == 404


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/sessions", None),
        ("post", "/sessions", {"title": "api-fix", "mode": "full"}),
        ("delete", "/sessions/api-fix", None),
    ],
)
def test_people_routes_refuse_remote_control(
    svc, engine, client, monkeypatch, method, path, body
):
    from backend.web.core import remote

    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    kw = {"headers": REMOTE}
    if body is not None:
        kw["json"] = body
    r = client.request(method.upper(), "/api/peer/links/" + LID + path, **kw)
    assert r.status_code == 403 and "this device" in r.json()["error"]
    assert svc.shared_sessions.get(LID) == {}


def test_people_writes_need_the_owner(svc, engine, client, monkeypatch):
    from backend.web.core import auth

    async def no(scope):
        return False

    monkeypatch.setattr(auth, "may_configure", no)
    r = client.post(
        "/api/peer/links/" + LID + "/sessions",
        json={"title": "api-fix", "mode": "full"},
    )
    assert r.status_code in (401, 403)
    assert svc.shared_sessions.get(LID) == {}
