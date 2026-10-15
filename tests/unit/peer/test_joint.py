"""Joint tasks (backend.peer.joint): the per-link file, the pure rules (the
contract's rev rule, proposals), the six ``joint_*`` wire validators, the state
machine between two services wired back to back, the share a joint starts
(hand-off prompt + CONTRACT.md before the agent runs), the HTTP routes and the
shared agent's ``peer_contract`` / ``peer_joint``."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.peer import agent_api as aa
from backend.peer import joint as jm
from backend.peer import paths, transport, wire
from backend.peer import service as svc_mod
from backend.web.core import events
from backend.web.core import mailbox as mb

from ._integration_helpers import SHARE_ID, fake_sandbox, mk_inst
from .test_integration_routes import FakeInvites, FakeStore, link
from .test_service import engine, share_fakes  # noqa: F401 — fixtures

LID = "ab" * 16
JID = "jt_0123456789ab"
PID = "cp_01234567"


def run(coro):
    return asyncio.run(coro)


async def settle():
    """Let the tasks a handler scheduled (share start, push-backs) finish."""
    me = asyncio.current_task()
    for _ in range(20):
        pending = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def emitted(monkeypatch):
    got = []
    monkeypatch.setattr(
        events.BUS, "emit", lambda name, data=None, **kw: got.append((name, data))
    )
    return got


# --------------------------------------------------------------------------- #
# Two services, back to back (strict wire checks both ways)
# --------------------------------------------------------------------------- #
class Wire:
    """A transport that hands each request straight to the other service's
    handler — with the real transport's checks: strict outbound schema, the
    receiver's ``messages`` permission, strict response validation."""

    def __init__(self):
        self.other = None
        self.up = True
        self.sent = []
        self.override = {}

    def start(self):
        pass

    def is_connected(self, lid):
        return self.up

    def peer_info(self, lid):
        return {}

    def close(self):
        pass

    async def request(self, lid, op, p, timeout):
        wire.validate_request(op, p)  # strict: an older peer refuses extras
        self.sent.append((op, copy.deepcopy(p)))
        if op in self.override:
            return self.override[op](p)
        if not self.up:
            raise transport.PeerUnavailable("peer not connected")
        other = self.other
        lnk = other.store.get(lid)
        perm = transport._OP_PERM[op]
        if perm and not lnk.perms.get(perm):
            raise transport.PeerOpError("not permitted")
        res = await other.handle_request(lnk, op, copy.deepcopy(p))
        wire.validate_response(op, res, p)
        return copy.deepcopy(res)


def _service(name, peer_name, wire_, tmp_path, **link_kw):
    lnk = link(LID, peer_name=peer_name, sas_verified=True, **link_kw)
    s = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore([lnk]),
        invites_factory=FakeInvites,
        transport_factory=lambda *a: wire_,
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
    s._joint = jm.JointTasks(s, jm.JointStore(str(tmp_path / ("joint-" + name))))
    return s


@pytest.fixture
def pair(engine, tmp_path):  # noqa: F811
    wa, wb = Wire(), Wire()
    a = _service("alice", "Bob", wa, tmp_path)
    b = _service("bob", "Alice", wb, tmp_path)
    wa.other, wb.other = b, a
    return SimpleNamespace(a=a, b=b, wa=wa, wb=wb)


def joint_of(svc):
    return svc.joint.store.get(LID)


async def propose_and_accept(pair, contract="## API\nGET /items"):
    await pair.a.joint.propose(
        LID, "Add GET /items; B prints it", contract_text=contract
    )
    await pair.b.joint.accept(LID)
    await settle()


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def test_store_roundtrip_is_0600_and_keeps_unknown_keys(tmp_path):
    st = jm.JointStore(str(tmp_path / "j"))
    j = jm.new_joint(JID, "brief", "me", "text")
    j["check"] = {"steps": [{"id": "s1"}]}  # a later wave's key
    st.put(LID, j)
    path = tmp_path / "j" / (LID + ".json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "j").st_mode) == 0o700
    doc = json.loads(path.read_text())
    assert doc["version"] == 1 and doc["joint"]["id"] == JID
    got = st.get(LID)
    assert got["check"] == {"steps": [{"id": "s1"}]}
    st.update(LID, lambda cur: dict(cur, state="active"))
    assert st.get(LID)["state"] == "active"
    assert st.get(LID)["check"] == {"steps": [{"id": "s1"}]}
    st.drop(LID)
    assert st.get(LID) is None


def test_store_corrupt_or_odd_file_is_tolerated(tmp_path):
    st = jm.JointStore(str(tmp_path / "j"))
    os.makedirs(tmp_path / "j", exist_ok=True)
    (tmp_path / "j" / (LID + ".json")).write_text("{not json")
    assert st.get(LID) is None
    (tmp_path / "j" / (LID + ".json")).write_text(
        json.dumps({"joint": {"id": JID, "state": "weird", "contract": {"rev": True}}})
    )
    j = st.get(LID)
    assert j["state"] == "cancelled" and j["contract"]["rev"] == 0
    assert j["mine"]["repo_path"] == "" and j["theirs"]["shared"] is False


def test_store_refuses_a_bad_link_id(tmp_path):
    st = jm.JointStore(str(tmp_path / "j"))
    for bad in ("../x", "AB" * 16, "ab"):
        with pytest.raises(ValueError):
            st.put(bad, jm.new_joint(JID, "b", "me", ""))


def test_store_defaults_under_the_peer_root(peer_home):
    jm.JointStore().put(LID, jm.new_joint(JID, "b", "me", ""))
    assert (peer_home / "joint" / (LID + ".json")).is_file()


# --------------------------------------------------------------------------- #
# Pure rules
# --------------------------------------------------------------------------- #
def _j(proposed_by="me", rev=3, text="ours"):
    j = jm.new_joint(JID, "b", proposed_by, text)
    j["contract"]["rev"] = rev
    return j


def test_contract_rev_rule():
    j = _j(rev=3)
    assert jm.apply_contract(j, 3, "ours", "peer") == "same"
    assert jm.apply_contract(j, 2, "older", "peer") == "kept"
    assert jm.apply_contract(j, 4, "newer", "peer") == "taken"
    assert j["contract"]["text"] == "newer" and j["contract"]["by"] == "peer"


def test_contract_tie_goes_to_the_proposers_copy():
    mine = _j(proposed_by="me")  # I proposed: my copy wins a tie
    assert jm.apply_contract(mine, 3, "theirs", "peer") == "kept"
    theirs = _j(proposed_by="peer")  # they proposed: theirs wins
    assert jm.apply_contract(theirs, 3, "theirs", "peer") == "taken"


def test_merge_proposal_states():
    j = _j()
    p = {"id": PID, "by": "peer", "text": "x", "at": jm.now_iso(), "state": "open"}
    assert jm.merge_proposal(j, dict(p)) == "new_open"
    assert jm.merge_proposal(j, dict(p)) == "same"
    assert jm.merge_proposal(j, dict(p, state="accepted")) == "decided"
    # A decision is final: an old "open" copy never reopens it.
    assert jm.merge_proposal(j, dict(p)) == "same"
    assert j["proposals"][0]["state"] == "accepted"


def test_open_proposals_are_capped_and_never_trimmed():
    j = _j()
    for i in range(jm.MAX_PROPOSALS):
        jm.merge_proposal(
            j,
            {
                "id": "cp_%08x" % i,
                "by": "me",
                "text": "t",
                "at": "2026",
                "state": "open",
            },
        )
    with pytest.raises(jm.JointError, match="too many open"):
        jm.merge_proposal(
            j,
            {
                "id": "cp_ffffffff",
                "by": "me",
                "text": "t",
                "at": "2026",
                "state": "open",
            },
        )
    j["proposals"][0]["state"] = "rejected"
    for i in range(5):  # decided ones make room by dropping the oldest decided
        j["proposals"][-1 - i]["state"] = "accepted"
    jm.merge_proposal(
        j, {"id": "cp_eeeeeeee", "by": "me", "text": "t", "at": "2026", "state": "open"}
    )
    assert len(j["proposals"]) == jm.MAX_PROPOSALS
    assert jm.open_count(j) == 5


def test_wire_view_carries_no_local_path():
    j = _j()
    j["mine"].update(
        repo_path="/home/me/secret/api",
        repo_name="api",
        share_id=SHARE_ID,
        session_title="peer-bob-abab",
    )
    v = jm.wire_view(j)
    wire.validate_response("joint_state", {"joint": v}, strict=True)
    blob = json.dumps(v)
    for leak in ("/home/me", SHARE_ID, "peer-bob-abab", "repo_path", "share_id"):
        assert leak not in blob
    assert v["mine"] == {
        "repo_name": "api",
        "branch": "",
        "program": "",
        "shared": True,
    }


def test_summary_shape():
    j = _j()
    j["brief"] = "First line of the brief\nsecond"
    jm.merge_proposal(
        j, {"id": PID, "by": "peer", "text": "x", "at": "2026", "state": "open"}
    )
    assert jm.summary(j, theirs_shared=True, mine_shared=False) == {
        "id": JID,
        "state": "proposed",
        "brief_head": "First line of the brief",
        "contract_rev": 3,
        "open_proposals": 1,
        "mine_shared": False,
        "theirs_shared": True,
    }
    assert jm.summary(None) is None


def test_handoff_prompt_names_the_contract_and_who_wrote_the_brief():
    j = jm.new_joint(JID, "Add GET /items", "peer", "c")
    j["theirs"]["repo_name"] = "svc-a"
    text = jm.handoff_prompt(j, "Alice")
    assert text.startswith("[MindFlock joint task %s] " % JID)
    assert "(brief written by Alice, accepted by your user) Add GET /items" in text
    assert "Alice's half runs in their repo svc-a" in text
    assert "CONTRACT.md" in text and "peer_contract" in text
    assert "peer_send / peer_inbox" in text and "Checkpoint" in text
    mine = jm.handoff_prompt(jm.new_joint(JID, "Do it", "me", "c"), "Bob")
    assert "written by" not in mine and "their own repo" in mine


def test_contract_file_never_writes_through_a_planted_symlink(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me")
    os.symlink(outside, work / "CONTRACT.md")
    jm.write_contract_file(str(work), "## the contract")
    assert outside.read_text() == "keep me"
    assert not os.path.islink(work / "CONTRACT.md")
    assert (work / "CONTRACT.md").read_text() == "## the contract"
    assert [p.name for p in work.iterdir()] == ["CONTRACT.md"]  # no temp left


def test_contract_file_refuses_a_symlinked_work_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "work")
    with pytest.raises(OSError):
        jm.write_contract_file(str(tmp_path / "work"), "x")
    assert not (real / "CONTRACT.md").exists()


# --------------------------------------------------------------------------- #
# Wire validators
# --------------------------------------------------------------------------- #
HALF = {"repo_name": "api", "branch": "main", "program": "claude"}
PROP = {
    "id": PID,
    "by": "me",
    "text": "new contract",
    "at": "2026-10-15T10:00:00Z",
    "state": "open",
}
GOOD = {
    "joint_propose": {"id": JID, "brief": "b", "contract_text": "", "mine": HALF},
    "joint_accept": {"id": JID, "mine": HALF},
    "joint_cancel": {"id": JID, "reason": "done"},
    "joint_contract": {"id": JID, "rev": 2, "text": "t\n", "by": "me"},
    "joint_proposal": {"id": JID, "proposal": PROP},
    "joint_state": {"id": JID},
}


def test_joint_ops_are_wire_ops():
    for op in GOOD:
        assert op in wire.OPS and wire._OP_NAME_RE.match(op)
        assert transport._OP_PERM[op] == "messages"
        assert svc_mod._OP_PERM[op] == "messages"


@pytest.mark.parametrize("op", sorted(GOOD))
def test_good_requests_pass_strict(op):
    assert wire.validate_request(op, copy.deepcopy(GOOD[op])) == GOOD[op]


@pytest.mark.parametrize(
    "op,patch",
    [
        ("joint_propose", {"id": "jt_XYZ"}),
        ("joint_propose", {"id": "jt_0123456789ab\n"}),
        ("joint_propose", {"brief": ""}),
        ("joint_propose", {"brief": "x" * (wire.MAX_JOINT_BRIEF + 1)}),
        ("joint_propose", {"brief": "bell\x07"}),
        ("joint_propose", {"contract_text": "x" * (wire.MAX_JOINT_CONTRACT + 1)}),
        ("joint_propose", {"mine": dict(HALF, repo_name="a\nb")}),
        ("joint_propose", {"mine": dict(HALF, shared=True)}),
        ("joint_propose", {"mine": {"repo_name": "a"}}),
        ("joint_accept", {"mine": "api"}),
        ("joint_cancel", {"reason": "x" * 201}),
        ("joint_cancel", {"reason": "two\nlines"}),
        ("joint_contract", {"rev": 0}),
        ("joint_contract", {"rev": True}),
        ("joint_contract", {"rev": 2**31 + 1}),
        ("joint_contract", {"by": "agent"}),
        ("joint_proposal", {"proposal": dict(PROP, id="cp_1")}),
        ("joint_proposal", {"proposal": dict(PROP, state="maybe")}),
        ("joint_proposal", {"proposal": dict(PROP, text="")}),
        ("joint_proposal", {"proposal": dict(PROP, at="yesterday")}),
        ("joint_state", {"id": None}),
    ],
)
def test_bad_requests_are_refused(op, patch):
    with pytest.raises(wire.ProtocolError):
        wire.validate_request(op, dict(copy.deepcopy(GOOD[op]), **patch))


def test_inbound_extra_keys_are_dropped_outbound_refused():
    p = dict(copy.deepcopy(GOOD["joint_propose"]), later=1)
    p["mine"]["later"] = 2
    with pytest.raises(wire.ProtocolError):
        wire.validate_request("joint_propose", copy.deepcopy(p))
    got = wire.validate_request("joint_propose", p, strict=False)
    assert "later" not in got and "later" not in got["mine"]


def test_responses():
    for op in ("joint_propose", "joint_accept", "joint_cancel", "joint_proposal"):
        assert wire.validate_response(op, {"ok": True}) == {"ok": True}
        with pytest.raises(wire.ProtocolError):
            wire.validate_response(op, {"ok": 1})
    assert wire.validate_response("joint_contract", {"ok": True, "rev": 3})
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("joint_contract", {"ok": True, "rev": -1})
    assert wire.validate_response("joint_state", {"joint": None}) == {"joint": None}
    view = jm.wire_view(_j())
    assert wire.validate_response("joint_state", {"joint": copy.deepcopy(view)})
    leaky = copy.deepcopy(view)
    leaky["mine"]["repo_path"] = "/home/x"
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("joint_state", {"joint": leaky})
    got = wire.validate_response("joint_state", {"joint": leaky}, strict=False)
    assert "repo_path" not in got["joint"]["mine"]  # a newer peer's extra: dropped
    too_many = copy.deepcopy(view)
    too_many["proposals"] = [dict(PROP, id="cp_%08x" % i) for i in range(11)]
    with pytest.raises(wire.ProtocolError):
        wire.validate_response("joint_state", {"joint": too_many})


def test_a_full_joint_state_fits_one_frame():
    j = jm.new_joint(
        JID, "\U0001f600" * jm.MAX_BRIEF, "me", "\U0001f600" * jm.MAX_CONTRACT
    )
    for i in range(jm.MAX_PROPOSALS):
        jm.merge_proposal(
            j,
            {
                "id": "cp_%08x" % i,
                "by": "me",
                "text": "\U0001f600" * jm.MAX_PROPOSAL,
                "at": jm.now_iso(),
                "state": "open",
            },
        )
    frame = {"t": "res", "id": 1, "ok": True, "p": {"joint": jm.wire_view(j)}}
    assert len(wire.encode(frame)) <= wire.MAX_FRAME


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
def test_propose_then_accept_makes_both_active(pair, emitted):
    async def go():
        out = await pair.a.joint.propose(
            LID, "Add GET /items", contract_text="## API", branch=None
        )
        assert out["joint"]["state"] == "proposed"
        assert out["joint"]["proposed_by"] == "me"
        b = joint_of(pair.b)
        assert b["state"] == "proposed" and b["proposed_by"] == "peer"
        assert b["brief"] == "Add GET /items" and b["contract"]["text"] == "## API"
        assert b["contract"]["by"] == "peer" and b["contract"]["rev"] == 1
        assert (
            "peer.joint_proposed",
            {
                "link_id": LID,
                "peer_name": "Alice",
                "joint_id": b["id"],
                "brief": "Add GET /items",
            },
        ) in emitted
        await pair.b.joint.accept(LID)
        await settle()

    run(go())
    assert joint_of(pair.a)["state"] == joint_of(pair.b)["state"] == "active"
    assert joint_of(pair.a)["id"] == joint_of(pair.b)["id"]
    states = [d for n, d in emitted if n == "peer.joint_state"]
    assert {(d["peer_name"], d["state"], d["by"]) for d in states} >= {
        ("Bob", "proposed", "you"),
        ("Alice", "active", "you"),
        ("Bob", "active", "peer"),
    }


def test_starter_contract_when_none_written(pair):
    run(pair.a.joint.propose(LID, "x"))
    assert joint_of(pair.b)["contract"]["text"] == jm.STARTER_CONTRACT


def test_propose_needs_a_matched_safety_number(pair):
    pair.a.store.get(LID).sas_verified = False
    with pytest.raises(svc_mod.PeerServiceError, match="safety number") as exc:
        run(pair.a.joint.propose(LID, "x"))
    assert exc.value.status == 409
    assert pair.wa.sent == [] and joint_of(pair.a) is None


def test_propose_validation(pair):
    for kw, status in (
        ({"brief": ""}, 400),
        ({"brief": "x" * 4001}, 400),
        ({"brief": "a\x1bb"}, 400),
        ({"brief": "x", "repo_path": "/no/such/folder/anywhere"}, 400),
        ({"brief": "x", "branch": "-bad"}, 400),
        ({"brief": "x", "program": "claude"}, 400),  # an agent with no folder
    ):
        with pytest.raises(svc_mod.PeerServiceError) as exc:
            run(pair.a.joint.propose(LID, **kw))
        assert exc.value.status == status, kw
    assert pair.wa.sent == []


def test_one_open_joint_per_link(pair):
    run(pair.a.joint.propose(LID, "first"))
    with pytest.raises(svc_mod.PeerServiceError, match="already has a joint") as exc:
        run(pair.a.joint.propose(LID, "second"))
    assert exc.value.status == 409
    run(pair.a.joint.cancel(LID, "changed my mind"))
    run(pair.a.joint.propose(LID, "third"))  # a closed one is replaced
    assert joint_of(pair.b)["brief"] == "third"


def test_a_refused_proposal_is_rolled_back(pair):
    # Both propose at once: b's arrived first; a's own attempt never landed.
    run(pair.b.joint.propose(LID, "theirs first"))
    pair.a.joint.store.drop(LID)
    with pytest.raises(svc_mod.PeerServiceError, match="Bob already has a joint"):
        run(pair.a.joint.propose(LID, "mine"))
    assert joint_of(pair.a) is None  # rolled back
    assert joint_of(pair.b)["brief"] == "theirs first"
    # Locally, a received proposal blocks a new one too.
    run(pair.b.joint.cancel(LID))
    run(pair.b.joint.propose(LID, "again"))
    with pytest.raises(svc_mod.PeerServiceError, match="this link already has"):
        run(pair.a.joint.propose(LID, "mine"))


def test_an_older_peer_gets_a_409_that_says_update(pair):
    def unsupported(p):
        raise transport.PeerOpError("unsupported")

    pair.wa.override["joint_propose"] = unsupported
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.propose(LID, "x"))
    assert exc.value.status == 409
    assert exc.value.message == "Bob's MindFlock is older — ask them to update"
    assert joint_of(pair.a) is None


def test_offline_peer_502_and_nothing_kept(pair):
    pair.wa.up = False
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.propose(LID, "x"))
    assert exc.value.status == 502 and joint_of(pair.a) is None


def test_joint_ops_need_the_messages_permission(pair):
    pair.b.store.get(LID).perms["messages"] = False
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.propose(LID, "x"))
    assert exc.value.status == 403
    lnk = pair.b.store.get(LID)
    with pytest.raises(Exception, match="not permitted"):
        run(pair.b.handle_request(lnk, "joint_state", {"id": JID}))


def test_accept_needs_a_proposal_from_them(pair):
    with pytest.raises(svc_mod.PeerServiceError, match="no joint task from Alice"):
        run(pair.b.joint.accept(LID))
    run(pair.a.joint.propose(LID, "x"))
    with pytest.raises(svc_mod.PeerServiceError, match="no joint task from Bob"):
        run(pair.a.joint.accept(LID))  # my own proposal


def test_inbound_ops_without_a_joint_say_so(pair):
    lnk = pair.b.store.get(LID)
    for op in ("joint_accept", "joint_cancel", "joint_contract", "joint_proposal"):
        with pytest.raises(transport.PeerOpError, match="no joint task"):
            run(pair.b.handle_request(lnk, op, copy.deepcopy(GOOD[op])))
    assert run(pair.b.handle_request(lnk, "joint_state", {"id": JID})) == {
        "joint": None
    }


def test_decline_and_done(pair, emitted):
    run(pair.a.joint.propose(LID, "x"))
    out = run(pair.b.joint.cancel(LID))
    assert out["synced"] is True
    assert joint_of(pair.a)["state"] == joint_of(pair.b)["state"] == "cancelled"
    run(pair.a.joint.propose(LID, "y"))
    run(pair.b.joint.accept(LID))
    run(pair.a.joint.cancel(LID, "done"))
    assert joint_of(pair.a)["state"] == joint_of(pair.b)["state"] == "done"
    assert joint_of(pair.b)["reason"] == "done"
    with pytest.raises(svc_mod.PeerServiceError, match="already done"):
        run(pair.b.joint.cancel(LID))


def test_cancel_offline_is_local_then_resynced(pair):
    run(propose_and_accept(pair))
    pair.wa.up = pair.wb.up = False
    out = run(pair.b.joint.cancel(LID, "no time"))
    assert out["synced"] is False and joint_of(pair.a)["state"] == "active"
    pair.wa.up = pair.wb.up = True
    run(pair.a.joint.resync(LID))  # a asks b, sees cancelled
    assert joint_of(pair.a)["state"] == "cancelled"
    assert joint_of(pair.a)["reason"] == ""  # b's reason travels with the cancel only
    j = joint_of(pair.b)
    run(pair.b.joint.resync(LID))  # nothing left to do
    assert joint_of(pair.b) == j


def test_contract_revisions_sync_both_ways(pair, emitted):
    run(propose_and_accept(pair))
    out = run(pair.a.joint.set_contract(LID, "## v2"))
    assert out["synced"] is True and out["joint"]["contract"]["rev"] == 2
    assert joint_of(pair.b)["contract"] == dict(
        joint_of(pair.b)["contract"], text="## v2", rev=2, by="peer"
    )
    run(pair.b.joint.set_contract(LID, "## v3"))
    assert joint_of(pair.a)["contract"]["text"] == "## v3"
    assert joint_of(pair.a)["contract"]["rev"] == 3
    revs = [
        (d["peer_name"], d["rev"], d["by"])
        for n, d in emitted
        if n == "peer.joint_contract" and d["kind"] == "revision"
    ]
    assert revs == [
        ("Bob", 2, "you"),
        ("Alice", 2, "peer"),
        ("Alice", 3, "you"),
        ("Bob", 3, "peer"),
    ]


def test_concurrent_edits_converge_on_the_proposers_copy(pair):
    run(propose_and_accept(pair))
    pair.wa.up = pair.wb.up = False
    run(pair.a.joint.set_contract(LID, "alice's"))  # a proposed the joint
    run(pair.b.joint.set_contract(LID, "bob's"))
    pair.wa.up = pair.wb.up = True

    async def both():
        await pair.b.joint.resync(LID)
        await pair.a.joint.resync(LID)
        await settle()

    run(both())
    assert joint_of(pair.a)["contract"]["text"] == "alice's"
    assert joint_of(pair.b)["contract"]["text"] == "alice's"
    assert (
        joint_of(pair.a)["contract"]["rev"] == joint_of(pair.b)["contract"]["rev"] == 2
    )


def test_a_stale_revision_is_answered_with_ours(pair):
    run(propose_and_accept(pair))
    pair.wa.up = pair.wb.up = False
    run(pair.a.joint.set_contract(LID, "a2"))
    run(pair.a.joint.set_contract(LID, "a3"))
    run(pair.b.joint.set_contract(LID, "b2"))
    pair.wa.up = pair.wb.up = True

    async def push():
        await pair.b.joint._push_contract(pair.b.store.get(LID), joint_of(pair.b))
        await settle()  # a keeps rev 3 and sends it back

    run(push())
    assert joint_of(pair.b)["contract"]["text"] == "a3"
    assert (
        "joint_contract",
        {"id": joint_of(pair.a)["id"], "rev": 3, "text": "a3", "by": "me"},
    ) in pair.wa.sent


def test_contract_edits_need_an_open_joint(pair):
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.set_contract(LID, "x"))
    assert exc.value.status == 404
    run(pair.a.joint.propose(LID, "x"))
    run(pair.a.joint.cancel(LID))
    with pytest.raises(svc_mod.PeerServiceError, match="is cancelled"):
        run(pair.a.joint.set_contract(LID, "x"))


def test_proposal_accept_replaces_the_contract_on_both_sides(pair, emitted):
    run(propose_and_accept(pair))
    out = run(pair.b.joint.propose_edit(LID, "## v2 by bob's agent", by="agent"))
    pid = out["proposal_id"]
    assert out["synced"] is True and pid.startswith("cp_")
    (pa,) = joint_of(pair.a)["proposals"]
    assert pa == dict(pa, id=pid, by="peer", state="open", text="## v2 by bob's agent")
    bell = [
        d
        for n, d in emitted
        if n == "peer.joint_contract" and d["kind"] == "proposal" and d["open"]
    ]
    assert [(d["peer_name"], d["by"]) for d in bell] == [
        ("Alice", "agent"),
        ("Bob", "peer"),
    ]
    run(pair.a.joint.decide(LID, pid, True))
    for side in (pair.a, pair.b):
        j = joint_of(side)
        assert j["contract"]["text"] == "## v2 by bob's agent"
        assert j["contract"]["rev"] == 2
        assert j["proposals"][0]["state"] == "accepted"
    with pytest.raises(svc_mod.PeerServiceError, match="already accepted"):
        run(pair.b.joint.decide(LID, pid, False))


def test_proposal_reject_keeps_the_contract(pair):
    run(propose_and_accept(pair, contract="v1"))
    pid = run(pair.a.joint.propose_edit(LID, "v2"))["proposal_id"]
    run(pair.b.joint.decide(LID, pid, False))
    for side in (pair.a, pair.b):
        j = joint_of(side)
        assert j["contract"] == dict(j["contract"], text="v1", rev=1)
        assert j["proposals"][0]["state"] == "rejected"
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.decide(LID, "cp_ffffffff", True))
    assert exc.value.status == 404
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.decide(LID, "../x", True))
    assert exc.value.status == 404


def test_a_proposal_made_offline_reaches_them_on_resync(pair):
    run(propose_and_accept(pair))
    pair.wa.up = pair.wb.up = False
    pid = run(pair.b.joint.propose_edit(LID, "while offline"))["proposal_id"]
    pair.wa.up = pair.wb.up = True
    run(pair.b.joint.resync(LID))
    assert [p["id"] for p in joint_of(pair.a)["proposals"]] == [pid]
    assert joint_of(pair.a)["proposals"][0]["by"] == "peer"


def test_resync_when_they_lost_the_joint(pair):
    run(propose_and_accept(pair))
    pair.b.joint.store.drop(LID)
    run(pair.a.joint.resync(LID))
    j = joint_of(pair.a)
    assert j["state"] == "cancelled" and j["reason"] == "gone on their side"


def test_resync_with_an_older_peer_changes_nothing(pair):
    run(propose_and_accept(pair))
    before = joint_of(pair.a)

    def unsupported(p):
        raise transport.PeerOpError("unsupported")

    pair.wa.override["joint_state"] = unsupported
    run(pair.a.joint.resync(LID))
    assert joint_of(pair.a) == before


def test_reconnect_schedules_a_resync(pair):
    seen = []

    async def fake(lid):
        seen.append(lid)

    pair.a.joint.resync = fake

    async def go():
        pair.a.on_state(LID, True)
        pair.a.on_state(LID, False)
        await settle()

    run(go())
    assert seen == [LID]


def test_a_proposal_reaches_the_shared_agent_as_untrusted_peer_input(
    pair, engine
):  # noqa: F811
    from ._integration_helpers import make_share

    make_share()
    engine["peer-bob-abab"] = mk_inst(
        "peer-bob-abab", paths.share_paths(SHARE_ID)["work"], peer_share=SHARE_ID
    )
    lnk = pair.a.store.get(LID)
    lnk.share_id, lnk.session_title = SHARE_ID, "peer-bob-abab"
    run(propose_and_accept(pair))  # a adopts the folder it already shares
    assert joint_of(pair.a)["mine"]["share_id"] == SHARE_ID
    work = paths.share_paths(SHARE_ID)["work"]
    assert "GET /items" in open(os.path.join(work, "CONTRACT.md")).read()
    pid = run(pair.b.joint.propose_edit(LID, "rename to /things", by="agent"))[
        "proposal_id"
    ]
    msgs = mb.fetch("peer-bob-abab")["messages"]
    note = [m for m in msgs if m["kind"] == "note"]
    assert note and "[MindFlock joint task" in note[0]["text"]
    (prop,) = [m for m in msgs if m["from"].startswith("peer:")]
    assert prop["from"] == "peer:Bob"
    assert prop["data"]["peer_kind"] == "contract_proposal"
    assert prop["data"]["proposal_id"] == pid
    line, _full = mb.render_delivery(prop, "claude")
    assert line.startswith("[MindFlock PEER contract proposal %s" % pid)
    assert "untrusted" in line and "you cannot" in line and "rename to /things" in line
    # An accepted revision rewrites the file in the shared folder.
    run(pair.a.joint.decide(LID, pid, True))
    assert "rename to /things" in open(os.path.join(work, "CONTRACT.md")).read()


# --------------------------------------------------------------------------- #
# The share a joint starts
# --------------------------------------------------------------------------- #
def test_accept_with_a_folder_starts_the_share(
    pair, share_fakes, monkeypatch, tmp_path
):  # noqa: F811
    fake_sandbox(monkeypatch, ok=True)
    from backend.web import server

    seen_contract = []
    real_create = server._session_create.create_result

    async def create_result(payload, *, peer_share=""):
        work = paths.share_paths(peer_share)["work"]
        seen_contract.append(open(os.path.join(work, "CONTRACT.md")).read())
        return await real_create(payload, peer_share=peer_share)

    monkeypatch.setattr(server._session_create, "create_result", create_result)
    repo = tmp_path / "client-b"
    repo.mkdir()
    run(
        pair.a.joint.propose(
            LID, "Add GET /items", contract_text="## shape\n{items: []}"
        )
    )
    out = run(pair.b.joint.accept(LID, str(repo), "main", "claude"))
    assert out["session"] == {"title": share_fakes.creates[0][0]["title"]}
    (created,) = share_fakes.created
    assert created == (LID, str(repo.resolve()), "main")
    payload = share_fakes.creates[0][0]
    assert "[MindFlock joint task %s]" % joint_of(pair.b)["id"] in payload["prompt"]
    assert "(brief written by Alice, accepted by your user)" in payload["prompt"]
    assert "untrusted" in payload["prompt"]  # the share's own intro stays
    assert seen_contract and "{items: []}" in seen_contract[0]  # before the agent
    j = joint_of(pair.b)
    assert j["mine"]["share_id"] == SHARE_ID and j["mine"]["repo_name"] == "client-b"
    assert pair.b.link_view(pair.b.store.get(LID))["joint"]["mine_shared"] is True
    # What a sent about b's half: names only.
    accept = [p for op, p in pair.wb.sent if op == "joint_accept"][0]
    assert accept["mine"] == {
        "repo_name": "client-b",
        "branch": "main",
        "program": "claude",
    }
    assert joint_of(pair.a)["theirs"]["repo_name"] == "client-b"
    run(pair.a.joint.set_contract(LID, "## v2 shape"))
    work = paths.share_paths(SHARE_ID)["work"]
    assert open(os.path.join(work, "CONTRACT.md")).read().endswith("## v2 shape")


def test_the_proposers_share_starts_when_they_accept(
    pair, share_fakes, monkeypatch, tmp_path
):  # noqa: F811
    fake_sandbox(monkeypatch, ok=True)
    repo = tmp_path / "service-a"
    repo.mkdir()
    run(pair.a.joint.propose(LID, "Add GET /items", str(repo), None, "claude"))
    assert share_fakes.created == []  # not before they accept
    run(propose_accept_only(pair))
    assert share_fakes.created == [(LID, str(repo.resolve()), None)]
    assert joint_of(pair.a)["mine"]["share_id"] == SHARE_ID


async def _accept_and_settle(pair):
    await pair.b.joint.accept(LID)
    await settle()


def propose_accept_only(pair):
    return _accept_and_settle(pair)


def test_a_share_that_fails_is_kept_on_the_card(
    pair, share_fakes, monkeypatch, tmp_path, emitted
):  # noqa: F811
    fake_sandbox(monkeypatch, ok=True)
    share_fakes.status = 400
    repo = tmp_path / "b"
    repo.mkdir()
    run(pair.a.joint.propose(LID, "x"))
    out = run(pair.b.joint.accept(LID, str(repo), None, "claude"))
    assert out["joint"]["state"] == "active"
    assert "could not start" in out["joint"]["mine"]["error"]
    assert any(n == "peer.joint_state" and d.get("error") for n, d in emitted)


def test_a_folder_needs_a_sandbox(pair, monkeypatch, tmp_path):
    fake_sandbox(monkeypatch, ok=False)
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(pair.a.joint.propose(LID, "x", str(tmp_path), None, "claude"))
    assert exc.value.status == 409 and pair.wa.sent == []


def test_a_folder_while_the_link_already_shares_is_refused(pair, tmp_path):
    pair.a.store.get(LID).share_id = SHARE_ID
    with pytest.raises(svc_mod.PeerServiceError, match="leave the folder empty"):
        run(pair.a.joint.propose(LID, "x", str(tmp_path)))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(pair, monkeypatch):
    from backend.web import server

    monkeypatch.setattr(svc_mod, "_SERVICE", pair.a)
    return TestClient(server.app)


BASE = "/api/peer/links/" + LID


def test_route_family(pair, client):
    assert client.get(BASE + "/joint").json() == {"joint": None}
    r = client.post(
        BASE + "/joint", json={"brief": "Add GET /items", "contract_text": "## v1"}
    )
    assert r.status_code == 201, r.text
    j = r.json()["joint"]
    assert j["state"] == "proposed" and j["contract"]["rev"] == 1
    assert client.post(BASE + "/joint", json={"brief": "again"}).status_code == 409
    view = client.get("/api/peer").json()["links"][0]["joint"]
    assert view == {
        "id": j["id"],
        "state": "proposed",
        "brief_head": "Add GET /items",
        "contract_rev": 1,
        "open_proposals": 0,
        "mine_shared": False,
        "theirs_shared": False,
    }
    run(pair.b.joint.accept(LID))
    assert client.get(BASE + "/joint").json()["joint"]["state"] == "active"
    r = client.put(BASE + "/joint/contract", json={"text": "## v2"})
    assert r.status_code == 200 and r.json()["joint"]["contract"]["rev"] == 2
    assert joint_of(pair.b)["contract"]["text"] == "## v2"
    r = client.post(BASE + "/joint/proposals", json={"text": "## v3"})
    assert r.status_code == 201
    pid = r.json()["proposal_id"]
    assert client.get("/api/peer").json()["links"][0]["joint"]["open_proposals"] == 1
    r = client.post(BASE + "/joint/proposals/%s/accept" % pid)
    assert r.status_code == 200 and r.json()["joint"]["contract"]["rev"] == 3
    assert client.post(BASE + "/joint/proposals/%s/reject" % pid).status_code == 409
    pid2 = client.post(BASE + "/joint/proposals", json={"text": "## v4"}).json()[
        "proposal_id"
    ]
    assert client.post(BASE + "/joint/proposals/%s/reject" % pid2).status_code == 200
    assert client.post(BASE + "/joint/proposals/cp_ffffffff/accept").status_code == 404
    r = client.post(BASE + "/joint/cancel", json={"reason": "done"})
    assert r.status_code == 200 and r.json()["joint"]["state"] == "done"
    assert joint_of(pair.b)["state"] == "done"


def test_route_errors(pair, client):
    assert client.post(BASE + "/joint", json={}).status_code == 400
    assert (
        client.post(
            "/api/peer/links/" + "cd" * 16 + "/joint", json={"brief": "x"}
        ).status_code
        == 404
    )
    assert client.post(BASE + "/joint/accept", json={}).status_code == 409
    assert client.post(BASE + "/joint/cancel", json={}).status_code == 404
    assert client.put(BASE + "/joint/contract", json={"text": "x"}).status_code == 404

    def unsupported(p):
        raise transport.PeerOpError("unsupported")

    pair.wa.override["joint_propose"] = unsupported
    r = client.post(BASE + "/joint", json={"brief": "x"})
    assert r.status_code == 409
    assert r.json() == {"error": "Bob's MindFlock is older — ask them to update"}


JOINT_ROUTES = [
    ("get", BASE + "/joint", None),
    ("post", BASE + "/joint", {"brief": "x"}),
    ("post", BASE + "/joint/accept", {}),
    ("post", BASE + "/joint/cancel", {}),
    ("put", BASE + "/joint/contract", {"text": "x"}),
    ("post", BASE + "/joint/proposals", {"text": "x"}),
    ("post", BASE + "/joint/proposals/%s/accept" % PID, None),
    ("post", BASE + "/joint/proposals/%s/reject" % PID, None),
]


@pytest.mark.parametrize("method,path,body", JOINT_ROUTES)
def test_joint_routes_refuse_remote_control(
    pair, client, monkeypatch, method, path, body
):
    from backend.web.core import remote

    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    kw = {"headers": {"X-MindFlock-Remote": "other-device-key"}}
    if body is not None:
        kw["json"] = body
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403 and "this device" in r.json()["error"]
    assert pair.wa.sent == [] and joint_of(pair.a) is None


# --------------------------------------------------------------------------- #
# The shared agent's tools
# --------------------------------------------------------------------------- #
def test_agent_args():
    assert aa.validate_args("contract", {"action": "read"}) == {
        "action": "read",
        "text": None,
    }
    assert (
        aa.validate_args("contract", {"action": "propose", "text": "x"})["text"] == "x"
    )
    assert aa.validate_args("joint", {}) == {}
    for op, args in (
        ("contract", {}),
        ("contract", {"action": "accept"}),
        ("contract", {"action": "read", "text": ""}),
        ("contract", {"action": "propose", "text": "x" * 10001}),
        ("contract", {"action": "read", "rev": 2}),
        ("joint", {"id": JID}),
    ):
        with pytest.raises(aa.ArgError):
            aa.validate_args(op, args)


def _api(svc, tmp_path):
    share = SimpleNamespace(share_id=SHARE_ID, run=str(tmp_path), work=str(tmp_path))
    return aa.AgentApi(share, LID, "t" * 32, svc, "peer-bob-abab", mailbox=mb)


def test_agent_tools_read_and_propose(pair, tmp_path):
    api = _api(pair.b, tmp_path)

    async def go():
        with pytest.raises(aa._PeerError, match="no joint task"):
            await api._op_joint({}, None)
        await propose_and_accept(pair, contract="## v1")
        info = await api._op_joint({}, None)
        assert set(info) == {"id", "brief", "state", "mine", "theirs"}
        assert info["state"] == "active" and info["brief"].startswith("Add GET")
        got = await api._op_contract({"action": "read", "text": None}, None)
        assert got == {"rev": 1, "text": "## v1", "open_proposals": []}
        with pytest.raises(aa._PeerError, match="propose needs text"):
            await api._op_contract({"action": "propose", "text": None}, None)
        res = await api._op_contract({"action": "propose", "text": "## v2"}, None)
        assert set(res) == {"proposal_id"}
        got = await api._op_contract({"action": "read", "text": None}, None)
        assert [p["id"] for p in got["open_proposals"]] == [res["proposal_id"]]
        # Agents never accept: the contract is unchanged until a person does.
        assert got["rev"] == 1 and got["text"] == "## v1"
        return res["proposal_id"]

    pid = run(go())
    assert joint_of(pair.a)["proposals"][0]["id"] == pid


def test_peer_tools_map_to_the_agent_api():
    from backend.mcp import peer_tools as pt
    from backend.mcp.protocol import ToolError

    calls = []

    class Client:
        def call(self, op, args, timeout=None):
            calls.append((op, args))
            return {"ok": True}

    tools = {t.name: t for t in pt.build_peer_tools(Client())}
    ctx = SimpleNamespace()
    tools["peer_contract"].handler({"action": "read"}, ctx)
    tools["peer_contract"].handler({"action": "propose", "text": "## new"}, ctx)
    tools["peer_joint"].handler({}, ctx)
    assert calls == [
        ("contract", {"action": "read"}),
        ("contract", {"action": "propose", "text": "## new"}),
        ("joint", {}),
    ]
    with pytest.raises(ToolError, match="propose needs text"):
        tools["peer_contract"].handler({"action": "propose"}, ctx)
    from backend.providers import mcp_attach

    assert {"peer_contract", "peer_joint"} <= set(mcp_attach._PEER_TOOLS_FALLBACK)
    assert mcp_attach._PEER_TOOLS_FALLBACK == pt.PEER_TOOL_NAMES


def test_intro_prompt_names_the_joint_tools():
    text = svc_mod.PeerService._intro_prompt("Bob")
    assert "peer_contract" in text and "peer_joint" in text
