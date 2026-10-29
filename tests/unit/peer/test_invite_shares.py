"""An invite that carries sessions (docs/peer-link.md "Invite with
sessions"): the owner picks which sessions and how when making the invite,
and the moment the other person joins, those are shared over the new link —
no second step, no It matches first. The book keeps them on the invite, the
transport's pairing tells the service which invite a link came from, and a
session that can't be shared never fails the join."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.peer import service as svc_mod
from backend.peer.invite import InviteBook, clean_sessions
from tests.unit.peer._integration_helpers import SHARE_ID, mk_inst
from tests.unit.peer.test_integration_routes import FakeStore, link

from .conftest import FakeClock, eventually

LID = "ab" * 16
PUB = "ee" * 32
FP = b"\x07" * 16


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #
def test_invite_with_sessions_round_trips():
    book = InviteBook(FP, clock=FakeClock())
    inv = book.create(
        "127.0.0.1",
        8799,
        sessions=[
            {"title": "fix-login", "mode": "full"},
            {"title": "api", "mode": "gated"},
        ],
    )
    want = [{"title": "fix-login", "mode": "full"}, {"title": "api", "mode": "gated"}]
    assert list(inv.sessions) == want
    (row,) = book.active()
    assert row["sessions"] == want
    assert inv.code not in repr(row)
    # No sessions: an invite that just connects.
    assert book.create("127.0.0.1", 8799).sessions == ()


@pytest.mark.parametrize(
    "sessions,words",
    [
        ("api", "list"),
        ([{"title": "a", "mode": "root"}], "mode"),
        ([{"title": "", "mode": "full"}], "title"),
        ([{"title": "a\x07", "mode": "full"}], "title"),
        ([{"title": "a", "mode": "full"}, {"title": "a", "mode": "gated"}], "twice"),
        (
            [{"title": "a", "mode": "gated"}, {"title": "b", "mode": "gated"}],
            "one gated",
        ),
        ([{"title": "s%d" % i, "mode": "full"} for i in range(51)], "at most 50"),
    ],
)
def test_bad_sessions_are_refused_in_words(sessions, words):
    with pytest.raises(ValueError) as exc:
        clean_sessions(sessions)
    assert words in str(exc.value)
    with pytest.raises(ValueError):
        InviteBook(FP).create("127.0.0.1", 8799, sessions=sessions)


def test_the_mode_defaults_to_gated():
    assert clean_sessions([{"title": " api "}]) == ({"title": "api", "mode": "gated"},)


async def test_pairing_tells_which_invite_and_its_sessions(make_node):
    """The real handshake: the inviter's book remembers the consumed invite
    under the joiner's key, once."""
    a, b = make_node("alice"), make_node("bob")
    port = await a.listen()
    inv = a.invites.create(
        "127.0.0.1", port, sessions=[{"title": "fix-login", "mode": "full"}]
    )
    lk = await b.t.pair(inv.code)
    assert await eventually(lambda: a.t.is_connected(lk.link_id))
    pub = a.store.get(lk.link_id).peer_pub
    assert a.invites.take_consumed(pub) == {
        "invite_id": inv.invite_id,
        "sessions": [{"title": "fix-login", "mode": "full"}],
    }
    assert a.invites.take_consumed(pub) is None  # once
    assert b.invites.take_consumed(a.ident.pub.hex()) is None  # the joiner's book


def test_an_unclaimed_consumed_invite_is_forgotten():
    clock = FakeClock()
    book = InviteBook(FP, clock=clock)
    inv = book.create("127.0.0.1", 8799, sessions=[{"title": "a", "mode": "full"}])
    book._consumed[PUB] = (inv.invite_id, inv.sessions, clock())
    clock.advance(121)
    assert book.take_consumed(PUB) is None


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class Transport:
    def __init__(self, *a):
        self.listening = False
        self.pushes = []

    def start(self):
        pass

    def start_listener(self, host, port):
        self.listening = True

    def stop_listener(self):
        self.listening = False

    def is_connected(self, lid):
        return True

    def close(self):
        pass

    def peer_info(self, lid):
        return {}

    async def send_push(self, lid, op, p):
        self.pushes.append((lid, op, p))


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, name, **kw):
        self.events.append((name, kw))

    def named(self, name):
        return [kw["data"] for n, kw in self.events if n == name]


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
    for title in ("fix-login", "api", "docs"):
        server.ENGINE.instances[title] = mk_inst(title, wt)
    server.ENGINE.instances["peer-bob-abab"] = mk_inst(
        "peer-bob-abab", wt, peer_share=SHARE_ID
    )
    return server.ENGINE.instances


def make_svc(monkeypatch, *links):
    book = InviteBook(FP)
    s = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: FP),
        store_factory=lambda: FakeStore(links or [link(sas_verified=False)]),
        invites_factory=lambda: book,
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
    s.transport
    s.invites
    return s


@pytest.fixture
def svc(monkeypatch):
    return make_svc(monkeypatch)


def consumed(svc, *sessions, iid="00000000000000aa"):
    """As if the joiner with key PUB just used an invite listing these."""
    svc.invites._consumed[PUB] = (iid, tuple(sessions), svc.invites._clock())


async def settle():
    me = asyncio.current_task()
    while True:
        rest = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not rest:
            return
        await asyncio.gather(*rest, return_exceptions=True)


def joined(svc, repaired=False):
    """The transport's hook for a new (or re-paired) link → the link's view."""

    async def go():
        lnk = svc.store.get(LID)
        (svc.on_link_repaired if repaired else svc.on_link_added)(lnk)
        await settle()

    asyncio.run(go())
    return svc.link_view(svc.store.get(LID))


def op(svc, name, p=None):
    return asyncio.run(svc.handle_request(svc.store.get(LID), name, p or {}))


def test_create_invite_carries_the_sessions(svc, engine):
    out = asyncio.run(
        svc.create_invite(
            sessions=[
                {"title": "fix-login", "mode": "full"},
                {"title": "api", "mode": "full"},
            ]
        )
    )
    want = [{"title": "fix-login", "mode": "full"}, {"title": "api", "mode": "full"}]
    assert out["sessions"] == want
    (row,) = svc.status()["invites"]
    assert row["sessions"] == want and row["invite_id"] == out["invite_id"]
    assert out["code"] not in str(svc.status())


@pytest.mark.parametrize(
    "sessions,words",
    [
        ([{"title": "nope", "mode": "full"}], "no session called nope"),
        ([{"title": "Bob::x", "mode": "full"}], "no session called"),
        ([{"title": "peer-bob-abab", "mode": "full"}], "another link's shared folder"),
        ([{"title": "api", "mode": "root"}], "mode"),
        (
            [{"title": "api", "mode": "gated"}, {"title": "docs", "mode": "gated"}],
            "one gated",
        ),
    ],
)
def test_a_bad_session_is_refused_at_create(svc, engine, sessions, words):
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        asyncio.run(svc.create_invite(sessions=sessions))
    assert exc.value.status == 400 and words in exc.value.message
    assert svc.status()["invites"] == []  # nothing minted


def test_a_sub_session_is_refused_at_create(svc, engine):
    engine["docs"].Parent = "api"
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        asyncio.run(svc.create_invite(sessions=[{"title": "docs", "mode": "full"}]))
    assert exc.value.status == 400 and "sub-session" in exc.value.message


def test_gated_on_an_invite_needs_the_sandbox(svc, engine, monkeypatch):
    def nope():
        raise svc_mod.peer_launch.PeerLaunchError("no bubblewrap")

    monkeypatch.setattr(svc_mod.peer_launch, "check_sandbox", nope)
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        asyncio.run(svc.create_invite(sessions=[{"title": "api", "mode": "gated"}]))
    assert exc.value.status == 400 and "share it in full" in exc.value.message


def test_join_shares_the_invites_sessions_at_once(svc, engine, bus):
    consumed(
        svc, {"title": "fix-login", "mode": "full"}, {"title": "api", "mode": "full"}
    )
    lnk = joined(svc)
    # Recorded for the link, for the card and the allowance.
    assert lnk["from_invite"] == "00000000000000aa"
    assert sorted(lnk["invite_shares"]) == ["api", "fix-login"]
    assert lnk["sas_verified"] is False  # the number is still theirs to compare
    assert sorted(svc.shared_sessions.get(LID)) == ["api", "fix-login"]
    (added,) = bus.named("peer.link_added")
    assert added["sessions"] == [
        {"title": "fix-login", "mode": "full"},
        {"title": "api", "mode": "full"},
    ]
    assert sorted(e["title"] for e in bus.named("peer.session_shared")) == [
        "api",
        "fix-login",
    ]
    assert lnk["share_errors"] == []
    # …and the joiner reaches them before It matches.
    items = op(svc, "sessions")["sessions"]
    assert sorted(i["title"] for i in items) == ["api", "fix-login"]


def test_join_shares_a_gated_session_as_share_session_does(svc, engine, monkeypatch):
    calls = []

    async def share(link_id, repo_path, branch=None, program="", **kw):
        calls.append(kw)
        svc.store.update(link_id, share_id=SHARE_ID, session_title="peer-bob-abab")
        return {}

    monkeypatch.setattr(svc, "share", share)
    consumed(svc, {"title": "api", "mode": "gated"})
    lnk = joined(svc)
    assert calls == [{"from_invite": True}]  # no It matches first
    assert lnk["invite_shares"] == ["peer-bob-abab"]
    (mine,) = svc.sessions_view(LID)["mine"]
    assert (mine["title"], mine["mode"], mine["source"]) == (
        "peer-bob-abab",
        "gated",
        "api",
    )
    assert [i["title"] for i in op(svc, "sessions")["sessions"]] == ["peer-bob-abab"]


def test_a_share_failure_does_not_fail_the_join(svc, engine, bus):
    consumed(svc, {"title": "gone", "mode": "full"}, {"title": "api", "mode": "full"})
    lnk = joined(svc)
    assert lnk["invite_shares"] == ["api"]
    (err,) = lnk["share_errors"]
    assert err["title"] == "gone" and err["mode"] == "full"
    assert "no session called gone" in err["error"]
    assert [e["title"] for e in bus.named("peer.session_shared")] == ["api"]
    (state,) = [d for d in bus.named("peer.state") if "share_errors" in d]
    assert state["share_errors"] == lnk["share_errors"] and state["link_id"] == LID


def test_an_invite_without_sessions_just_connects(svc, engine, bus):
    consumed(svc)
    lnk = joined(svc)
    assert lnk["from_invite"] == "00000000000000aa" and lnk["invite_shares"] == []
    assert svc.shared_sessions.get(LID) == {}
    assert bus.named("peer.link_added")[0]["sessions"] == []
    assert op(svc, "sessions") == {"sessions": []}


def test_a_link_from_no_invite_still_waits_for_it_matches(svc, engine):
    lnk = joined(svc)  # nothing consumed: an older pairing path
    assert lnk["from_invite"] == ""
    with pytest.raises(Exception) as exc:
        op(svc, "sessions")
    assert svc_mod.NOT_VERIFIED_ERR in str(exc.value)


def test_before_it_matches_only_the_invites_sessions_are_reachable(svc, engine):
    consumed(svc, {"title": "api", "mode": "full"})
    joined(svc)
    # Something else shared on the link (say, before a re-pair): not reachable
    # until It matches.
    svc.shared_sessions.add(LID, "docs", "full")
    assert [i["title"] for i in op(svc, "sessions")["sessions"]] == ["api"]
    with pytest.raises(Exception) as exc:
        op(svc, "term_open", {"title": "docs", "cols": 80, "rows": 24})
    assert "not shared" in str(exc.value)
    # The folder's content still waits for It matches.
    with pytest.raises(Exception) as exc:
        op(svc, "diff", {})
    assert svc_mod.NOT_VERIFIED_ERR in str(exc.value)
    svc.store.update(LID, sas_verified=True)
    assert sorted(i["title"] for i in op(svc, "sessions")["sessions"]) == [
        "api",
        "docs",
    ]


def test_a_re_pair_starts_the_allowance_over(svc, engine):
    consumed(svc, {"title": "api", "mode": "full"})
    joined(svc)
    # Paired again with a fresh invite that opens docs only.
    consumed(svc, {"title": "docs", "mode": "full"}, iid="00000000000000bb")
    lnk = joined(svc, repaired=True)
    assert lnk["from_invite"] == "00000000000000bb" and lnk["invite_shares"] == ["docs"]
    assert [i["title"] for i in op(svc, "sessions")["sessions"]] == ["docs"]
    # …and a re-pair from an invite with nothing on it opens nothing early.
    lnk = joined(svc, repaired=True)
    assert lnk["from_invite"] == "" and lnk["invite_shares"] == []


def test_the_joiner_side_claims_nothing(monkeypatch, engine):
    svc = make_svc(monkeypatch, link(role="dialer", sas_verified=False))
    consumed(svc, {"title": "api", "mode": "full"})
    joined(svc)
    assert svc.shared_sessions.get(LID) == {}
    assert PUB in svc.invites._consumed


def test_withdrawing_an_invite_share_closes_it_early_too(svc, engine):
    consumed(svc, {"title": "api", "mode": "full"})
    joined(svc)
    asyncio.run(svc.unshare_session(LID, "api"))
    assert svc.link_view(svc.store.get(LID))["invite_shares"] == []


def test_the_invite_record_lives_beside_the_links_sessions():
    """Not a Link field: an older release's validator would move the whole
    links file aside over a value it doesn't know."""
    from backend.peer import paths
    from backend.peer.store import Link, SharedSessions

    assert not {"from_invite", "invite_shares", "share_errors"} & set(
        Link.__dataclass_fields__
    )
    ss = SharedSessions()
    assert ss.invite(LID) == {"invite_id": "", "shares": [], "errors": []}
    ss.set_invite(LID, "00000000000000aa")  # an invite with nothing on it
    assert ss.invite(LID)["invite_id"] == "00000000000000aa"
    ss.add(LID, "api", "full")
    ss.add_invite_share(LID, "api")
    ss.set_invite_errors(
        LID,
        [{"title": "gone", "mode": "full", "error": "x" * 400}, {"title": "bad"}],
    )
    got = ss.invite(LID)
    assert got["shares"] == ["api"]
    assert got["errors"] == [{"title": "gone", "mode": "full", "error": "x" * 300}]
    assert list(ss.get(LID)) == ["api"]  # the sessions are untouched
    with pytest.raises(ValueError):
        ss.set_invite(LID, "nope")
    ss.set_invite(LID, "")  # a re-pair from no invite: the record goes
    assert ss.invite(LID)["invite_id"] == "" and list(ss.get(LID)) == ["api"]
    ss.remove(LID, "api")
    import os

    assert not os.path.exists(paths.shared_sessions_file(LID))
