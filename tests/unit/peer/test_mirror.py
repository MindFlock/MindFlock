"""backend.peer.mirror — the standing export mirror: the per-link file and
its validation, when their ``status`` head starts an update, the fetch-only
update against real throwaway repos (their shallow share → a bundle over
``mirror_pull`` → ``share.fetch_into`` into our repo), the wire shapes, the
service glue, the routes and the events."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from backend.peer import mirror, wire
from backend.peer import service as svc_mod
from backend.peer import share as sh

from .test_integration_routes import FakeInvites, FakeStore, FakeTransport, link

LID = "ab" * 16
HEAD1 = "1" * 40
HEAD2 = "2" * 40


def run(coro):
    return asyncio.run(coro)


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def make_repo(path, files=None, msg="first"):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    for name, text in (files or {"README.md": "hello\n"}).items():
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-qm", msg)
    return path


@pytest.fixture
def target(tmp_path):
    """OUR repo the mirror fetches into (unrelated to their history)."""
    return make_repo(tmp_path / "mine", {"mine.txt": "mine\n"}, "my work")


@pytest.fixture
def theirs(tmp_path):
    """THEIR repo, with history: the share is a depth-1 clone of it."""
    r = make_repo(tmp_path / "theirs", {"a.txt": "alpha\n"})
    (r / "a.txt").write_text("alpha\nbeta\n")
    git(r, "commit", "-qam", "second")
    return r


@pytest.fixture
def share(theirs):
    return sh.create_share(LID, str(theirs))


# --------------------------------------------------------------------------- #
# The per-link file and its validation
# --------------------------------------------------------------------------- #
def test_enable_stores_a_private_file_with_the_contract_shape(target, peer_home):
    m = mirror.enable(LID, str(target), None, "Bob Smith")
    assert m == {
        "repo_path": str(target.resolve()),
        "branch": "peer/bob-smith",
        "enabled": True,
        "last_head": "",
        "last_at": "",
        "last_error": "",
    }
    path = peer_home / "mirror" / (LID + ".json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert mirror.load(LID) == m
    assert mirror.view(LID) == dict(m, updating=False)
    assert mirror.enabled_links() == [LID]


def test_default_branch_is_a_safe_slug():
    assert mirror.default_branch("Bob") == "peer/bob"
    assert mirror.default_branch("  ..éé  ") == "peer/peer"
    assert mirror.default_branch("a b/c") == "peer/a-b-c"


@pytest.mark.parametrize(
    "setup,branch,needle",
    [
        ("missing", None, "doesn't exist"),
        ("plain", None, "not a git repository"),
        ("repo", "main", 'starting with "peer/"'),
        ("repo", "peer/..bad", 'starting with "peer/"'),
        ("repo", "peer/a b", 'starting with "peer/"'),
        ("checked_out", "peer/live", "checked out in that repository"),
        ("empty", None, "pick the repository"),
    ],
)
def test_enable_refusals_say_why(tmp_path, target, setup, branch, needle):
    repo = {
        "missing": str(tmp_path / "nope"),
        "plain": str(tmp_path),
        "repo": str(target),
        "checked_out": str(target),
        "empty": "  ",
    }[setup]
    if setup == "checked_out":
        git(target, "checkout", "-q", "-b", "peer/live")
    with pytest.raises(mirror.MirrorError, match=needle):
        mirror.enable(LID, repo, branch, "bob")
    assert mirror.load(LID) is None


def test_enable_refuses_the_peer_folder(share):
    with pytest.raises(mirror.MirrorError, match="inside the peer folder"):
        mirror.enable(LID, share.work, "peer/x", "bob")


def test_re_enabling_elsewhere_starts_over_but_the_same_keeps_progress(
    tmp_path, target
):
    mirror.enable(LID, str(target), "peer/x", "bob")
    mirror._patch(LID, last_head=HEAD1, last_at="t", last_error="boom")
    again = mirror.enable(LID, str(target), "peer/x", "bob")
    assert (again["last_head"], again["last_error"]) == (HEAD1, "")
    other = mirror.enable(LID, str(target), "peer/y", "bob")
    assert other["last_head"] == "" and other["branch"] == "peer/y"


def test_drop_move_and_unreadable_files(target, peer_home):
    mirror.enable(LID, str(target), "peer/x", "bob")
    new = "cd" * 16
    mirror.move(LID, new)
    assert mirror.load(LID) is None and mirror.load(new)["branch"] == "peer/x"
    mirror.drop(new)
    assert mirror.load(new) is None and mirror.enabled_links() == []
    (peer_home / "mirror" / (LID + ".json")).write_text("{not json")
    assert mirror.load(LID) is None
    assert mirror.load("../../etc") is None


# --------------------------------------------------------------------------- #
# The trigger rule
# --------------------------------------------------------------------------- #
ON = {"enabled": True, "last_head": HEAD1}


@pytest.mark.parametrize(
    "m,head,expect",
    [
        (ON, HEAD2, True),  # their head moved
        (ON, HEAD1, False),  # unchanged
        (dict(ON, enabled=False), HEAD2, False),  # mirror disabled
        (None, HEAD2, False),  # no mirror
        ({"enabled": True, "last_head": ""}, HEAD1, True),  # first enable
        (ON, None, False),  # an older peer sends no head
        (ON, "not-a-sha", False),
    ],
)
def test_wants_update(m, head, expect):
    assert mirror.wants_update(m, head) is expect


def test_a_failed_head_waits_before_it_is_retried():
    failed = (HEAD2, 100.0)
    assert not mirror.wants_update(ON, HEAD2, failed, now=100.0 + 5)
    assert mirror.wants_update(ON, HEAD2, failed, now=100.0 + mirror.RETRY_S + 1)
    assert mirror.wants_update(ON, "3" * 40, failed, now=101.0)  # a newer head


# --------------------------------------------------------------------------- #
# The fetch-only update, against real repos
# --------------------------------------------------------------------------- #
def puller(share, calls=None):
    """``mirror_pull`` answered by their side in-process, through the wire
    validators both ends run."""

    async def pull(p):
        wire.validate_request("mirror_pull", p)
        res = mirror.serve_pull(share, LID, p["have"], p["offset"])
        wire.validate_response("mirror_pull", res, p)
        if calls is not None:
            calls.append((p, res["size"]))
        return res

    return pull


def tip(repo, branch):
    return git(repo, "rev-parse", branch).stdout.strip()


def test_update_fetches_their_checkpoints_and_never_touches_the_work_tree(
    share, target
):
    mirror.enable(LID, str(target), "peer/bob", "bob")
    head_before = tip(target, "HEAD")
    calls = []

    out = run(mirror.update(LID, pull=puller(share, calls), peer_name="bob"))
    kind, data = out
    assert kind == "updated"
    assert data["sha"] == tip(target, "peer/bob")
    assert data["head"] == sh.head_sha(share)
    assert set(data) == {"link_id", "peer_name", "repo_path", "branch", "sha", "head"}
    shown = git(target, "show", "peer/bob:a.txt").stdout
    assert shown == "alpha\nbeta\n"
    # their shallow base became a root: the branch is self-contained
    assert git(target, "rev-list", "--count", "peer/bob").stdout.strip() == "1"
    assert git(target, "fsck", "--no-dangling").returncode == 0
    # only a ref moved
    assert tip(target, "HEAD") == head_before
    assert git(target, "status", "--porcelain").stdout == ""
    assert not (target / "a.txt").exists()
    m = mirror.load(LID)
    assert m["last_head"] == sh.head_sha(share) and m["last_at"] and not m["last_error"]
    assert calls[0][0] == {"have": "", "offset": 0}

    # a checkpoint on their side → an incremental bundle on top of ours
    first = tip(target, "peer/bob")
    with open(os.path.join(share.work, "b.txt"), "w") as f:
        f.write("from bob\n")
    sh.checkpoint(share, "add b")
    calls.clear()
    kind, data = run(mirror.update(LID, pull=puller(share, calls), peer_name="bob"))
    assert kind == "updated"
    assert git(target, "show", "peer/bob:b.txt").stdout == "from bob\n"
    assert git(target, "rev-parse", "peer/bob~1").stdout.strip() == first
    assert git(target, "log", "-1", "--format=%s", "peer/bob").stdout.strip() == "add b"
    assert calls[0][0] == {"have": first, "offset": 0}

    # nothing new: no event, no fetch, and their side left no bundle behind
    assert run(mirror.update(LID, pull=puller(share), peer_name="bob")) is None
    assert not os.path.exists(os.path.join(share.root, "mirror-out.bundle"))
    assert os.listdir(mirror._dir()) == [LID + ".json"]


def test_a_big_bundle_crosses_in_chunks(share, target, monkeypatch):
    monkeypatch.setattr(mirror, "CHUNK", 64)
    monkeypatch.setattr(mirror, "PULL_PAUSE_S", 0)
    mirror.enable(LID, str(target), "peer/bob", "bob")
    calls = []
    kind, _ = run(mirror.update(LID, pull=puller(share, calls), peer_name="bob"))
    assert kind == "updated" and len(calls) > 2
    assert [c[0]["offset"] for c in calls] == list(range(0, 64 * len(calls), 64))


def test_the_update_refuses_a_branch_checked_out_since(share, target):
    mirror.enable(LID, str(target), "peer/bob", "bob")
    git(target, "checkout", "-q", "-b", "peer/bob")
    kind, data = run(mirror.update(LID, pull=puller(share), peer_name="bob"))
    assert kind == "failed" and "checked out" in data["error"]
    assert "checked out" in mirror.load(LID)["last_error"]


def test_a_failure_is_reported_once_per_distinct_error(share, target):
    mirror.enable(LID, str(target), "peer/bob", "bob")

    async def refused(p):
        raise RuntimeError("not permitted")

    def go(pull):
        return run(
            mirror.update(LID, pull=pull, peer_name="bob", describe=lambda e: str(e))
        )

    assert go(refused) == (
        "failed",
        {
            "link_id": LID,
            "peer_name": "bob",
            "repo_path": str(target.resolve()),
            "branch": "peer/bob",
            "error": "not permitted",
        },
    )
    assert go(refused) is None  # the same error again: said already
    git(target, "checkout", "-q", "-b", "peer/bob")
    assert go(puller(share))[0] == "failed"  # a different one: said
    git(target, "checkout", "-q", "main")
    assert go(puller(share))[0] == "updated"
    assert mirror.load(LID)["last_error"] == ""
    assert go(refused)[0] == "failed"  # after a success, said again


def test_nothing_shared_yet_is_not_a_failure(target):
    mirror.enable(LID, str(target), "peer/bob", "bob")

    async def none(p):
        raise RuntimeError("no shared folder")

    assert run(mirror.update(LID, pull=none, peer_name="bob")) is None
    assert mirror.load(LID)["last_error"] == ""


def test_a_garbled_bundle_is_refused_and_fetches_nothing(share, target):
    mirror.enable(LID, str(target), "peer/bob", "bob")
    real = puller(share)

    async def lying(p):
        res = await real(p)
        return dict(res, sha256="0" * 64)

    kind, data = run(mirror.update(LID, pull=lying, peer_name="bob"))
    assert kind == "failed" and "garbled" in data["error"]
    assert (
        git(target, "rev-parse", "-q", "--verify", "peer/bob", check=False).stdout == ""
    )


def test_a_disabled_mirror_does_nothing(share, target):
    assert run(mirror.update(LID, pull=puller(share), peer_name="bob")) is None


# --------------------------------------------------------------------------- #
# The wire
# --------------------------------------------------------------------------- #
def test_status_head_is_optional_and_checked():
    base = {"shared": True, "agent": "none", "name": "bob"}
    assert wire.validate_response("status", base) == base  # an older peer
    assert wire.validate_response("status", dict(base, head=HEAD1))["head"] == HEAD1
    assert wire.validate_response("status", dict(base, head="a" * 64))
    for bad in ("", "xyz", 5, "A" * 40):
        with pytest.raises(wire.ProtocolError):
            wire.validate_response("status", dict(base, head=bad))
    # an older peer's tolerant inbound just drops it
    assert "head" not in wire._keys(
        dict(base, head=HEAD1), ("shared", "agent", "name"), (), strict=False
    )


def test_mirror_pull_shapes():
    assert "mirror_pull" in wire.OPS
    assert wire.validate_request("mirror_pull", {"have": "", "offset": 0})
    for p in ({"have": "x", "offset": 0}, {"have": "", "offset": -1}, {"have": ""}):
        with pytest.raises(wire.ProtocolError):
            wire.validate_request("mirror_pull", p)
    ok = {
        "head": HEAD1,
        "tip": HEAD2,
        "size": 3,
        "sha256": "f" * 64,
        "offset": 0,
        "data": "YWJj",
    }
    assert wire.validate_response("mirror_pull", ok) == ok
    assert wire.validate_response("mirror_pull", dict(ok, size=0, sha256="", data=""))
    for bad in (
        dict(ok, data="!!!!"),
        dict(ok, data="A" * (wire.MAX_MIRROR_CHUNK + 4)),
        dict(ok, sha256=""),
        dict(ok, size=wire.MAX_MIRROR_BYTES + 1),
        dict(ok, tip="nope"),
    ):
        with pytest.raises(wire.ProtocolError):
            wire.validate_response("mirror_pull", bad)
    # a full chunk fits a frame
    big = dict(ok, data="A" * wire.MAX_MIRROR_CHUNK)
    assert (
        len(wire.encode({"t": "res", "id": 1, "ok": True, "p": big})) < wire.MAX_FRAME
    )


# --------------------------------------------------------------------------- #
# The service: status head, the trigger, mirror_pull, link_view, events
# --------------------------------------------------------------------------- #
def make_service(*links):
    s = svc_mod.PeerService(
        identity_factory=lambda: SimpleNamespace(fingerprint=lambda: b"\x07" * 16),
        store_factory=lambda: FakeStore(links or [link()]),
        invites_factory=FakeInvites,
        transport_factory=FakeTransport,
        settings_getter=lambda: {"enabled": True, "display_name": "alice"},
    )
    s._server_mod = SimpleNamespace(ENGINE=SimpleNamespace(instances={}))
    return s


def with_answers(svc, answer):
    class T(FakeTransport):
        async def request(self, lid, op, p, timeout=60.0):
            return await answer(op, p)

    svc._transport = T()
    svc._transport_started = True


def test_status_answers_the_share_head(share):
    svc = make_service(link(share_id=share.share_id))
    lnk = svc.store.get(LID)
    out = run(svc.handle_request(lnk, "status", {}))
    assert out["head"] == sh.head_sha(share)
    assert wire.validate_response("status", out) == out
    svc.store.update(LID, share_id=None)
    assert "head" not in run(svc.handle_request(lnk, "status", {}))


def test_mirror_pull_needs_read_file_and_a_share(share):
    svc = make_service(link(share_id=share.share_id))
    lnk = svc.store.get(LID)
    res = run(svc.handle_request(lnk, "mirror_pull", {"have": "", "offset": 0}))
    assert res["tip"] and res["head"] == sh.head_sha(share)
    svc.store.update(LID, perms={"messages": True, "diff": True, "read_file": False})
    with pytest.raises(Exception, match="not permitted"):
        run(svc.handle_request(lnk, "mirror_pull", {"have": "", "offset": 0}))
    svc.store.update(LID, perms={"read_file": True}, share_id=None)
    with pytest.raises(Exception, match="no shared folder"):
        run(svc.handle_request(lnk, "mirror_pull", {"have": "", "offset": 0}))


def _capture(svc):
    seen = []
    svc._emit = lambda event, data: seen.append((event, data))
    return seen


def test_a_moved_head_in_their_status_updates_the_mirror(share, target):
    """The whole receiving side: status → trigger → pulls → event."""
    svc = make_service()
    seen = _capture(svc)
    heads = [sh.head_sha(share)]

    async def answer(op, p):
        if op == "status":
            return {
                "shared": True,
                "agent": "running",
                "name": "bob",
                "head": heads[-1],
            }
        return await puller(share)(p)

    with_answers(svc, answer)

    async def go():
        out = await svc.enable_mirror(LID, str(target), "peer/bob")
        assert out["link"]["mirror"]["updating"] is False  # not started yet
        await asyncio.sleep(0.5)  # the first-enable update
        first = list(seen)
        await svc._refresh_their_status(LID)  # head unchanged: nothing
        await asyncio.sleep(0.2)
        same = list(seen)
        with open(os.path.join(share.work, "c.txt"), "w") as f:
            f.write("c\n")
        heads.append(sh.checkpoint(share, "add c"))
        await svc._refresh_their_status(LID)  # one status poll
        for _ in range(50):
            if len(seen) > len(same):
                break
            await asyncio.sleep(0.05)
        if svc._mirror_task is not None:
            svc._mirror_task.cancel()
        return first, same, list(seen)

    first, same, later = run(go())
    assert [e for e, _ in first] == ["peer.mirror_updated"]
    assert same == first
    assert [e for e, _ in later] == ["peer.mirror_updated"] * 2
    assert git(target, "show", "peer/bob:c.txt").stdout == "c\n"
    view = svc.link_view(svc.store.get(LID))
    assert view["mirror"]["last_head"] == heads[-1]
    assert view["their_share"]["head"] == heads[-1]


def test_an_older_peer_never_triggers(target):
    svc = make_service()
    pulls = []

    async def answer(op, p):
        if op == "status":
            return {"shared": True, "agent": "running", "name": "bob"}
        pulls.append(p)
        raise RuntimeError("unsupported")

    with_answers(svc, answer)
    mirror.enable(LID, str(target), "peer/bob", "bob")
    run(svc._refresh_their_status(LID))
    assert pulls == [] and "head" not in svc._their_status[LID][1]


def test_failed_events_say_what_to_do(target):
    svc = make_service()
    seen = _capture(svc)

    async def answer(op, p):
        from backend.peer.transport import PeerOpError

        raise PeerOpError("unsupported")

    with_answers(svc, answer)
    mirror.enable(LID, str(target), "peer/bob", "bob")
    run(svc._mirror_update(LID, HEAD1))
    run(svc._mirror_update(LID, HEAD1))
    assert [e for e, _ in seen] == ["peer.mirror_failed"]
    assert "need to update MindFlock" in seen[0][1]["error"]
    assert svc._mirror_failed[LID][0] == HEAD1  # backed off
    assert not mirror.wants_update(mirror.load(LID), HEAD1, svc._mirror_failed[LID])


def test_disable_and_unlink_forget_the_mirror(target):
    svc = make_service()
    mirror.enable(LID, str(target), "peer/bob", "bob")
    assert svc.link_view(svc.store.get(LID))["mirror"]["branch"] == "peer/bob"
    assert svc.disable_mirror(LID)["link"]["mirror"] is None
    mirror.enable(LID, str(target), "peer/bob", "bob")
    run(svc.unlink(LID))
    assert mirror.load(LID) is None


def test_enable_refusal_is_a_400(tmp_path):
    svc = make_service()
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.enable_mirror(LID, str(tmp_path), None))
    assert exc.value.status == 400 and "not a git repository" in exc.value.message
    with pytest.raises(svc_mod.PeerServiceError) as exc:
        run(svc.enable_mirror("ff" * 16, str(tmp_path), None))
    assert exc.value.status == 404


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_mirror_routes(monkeypatch, target):
    from fastapi.testclient import TestClient

    from backend.web import server

    svc = make_service()
    monkeypatch.setattr(svc_mod, "_SERVICE", svc)
    client = TestClient(server.app)
    url = "/api/peer/links/%s/mirror" % LID
    r = client.post(url, json={"repo_path": str(target)})
    assert r.status_code == 200, r.text
    m = r.json()["link"]["mirror"]
    assert m["branch"] == "peer/bob" and m["enabled"] is True
    assert set(m) == {
        "repo_path",
        "branch",
        "enabled",
        "last_head",
        "last_at",
        "last_error",
        "updating",
    }
    r = client.post(url, json={"repo_path": str(target), "branch": "main"})
    assert r.status_code == 400 and "peer/" in r.json()["error"]
    r = client.delete(url)
    assert r.status_code == 200 and r.json()["link"]["mirror"] is None
    assert client.delete("/api/peer/links/%s/mirror" % ("ff" * 16)).status_code == 404
