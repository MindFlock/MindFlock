"""Removal guards for the old "needs rebase" / update-from-base feature, plus
the still-live wedged-session signal it used to ship alongside.

The needs-rebase surface (the ``↓N behind base`` pill decoration, the per-session
"Update from base" action, the bulk "Update all" toolbar button, the
``behind_base``/``base_branch`` payload, ``_commits_behind_base`` /
``_maybe_fetch_base``, and the ``POST /update-from-base`` endpoint) was removed:
it relied on knowing each repo's base branch, which a stale global config could
stamp wrongly (e.g. "staging" on a repo whose origin only has "main"), so the
action fetched a nonexistent ref and failed. These tests pin that it stays gone.

``activity_since`` (attention ordering + the wedged-session watchdog) rode the
same instance payload but is an independent feature — kept and still covered.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Status
from backend.web import server
from backend.web.core import git_ops


class _FakeInst:
    Program = "bash"
    Path = ""

    def __init__(self, wt: str, *, title: str = "t"):
        self.Title = title
        self.Branch = ""
        self.BaseBranch = ""
        self.Status = Status.Running
        self.InPlace = False
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    for cache in (server._ACTIVITY_CACHE, git_ops._HAS_ORIGIN_CACHE):
        cache.clear()
    yield


# --------------------------------------------------------------------------- #
# the feature is gone (backend)
# --------------------------------------------------------------------------- #
def test_behind_base_helpers_removed():
    # The behind-base counter and its background fetch were deleted with the
    # feature; only the "beyond base" counter (stage detection) remains.
    assert not hasattr(git_ops, "_commits_behind_base")
    assert not hasattr(git_ops, "_maybe_fetch_base")
    assert hasattr(git_ops, "_commits_beyond_base")


def test_update_from_base_endpoint_removed():
    assert not hasattr(server, "instance_update_from_base")
    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/api/instances/{title}/update-from-base" not in paths


def test_stage_payload_omits_behind_base(tmp_path):
    # A session's stage payload no longer carries behind_base / base_branch.
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "add",
            "-A",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    inst = _FakeInst(str(repo))
    res = server._session_stage(inst)
    assert "behind_base" not in res
    assert "base_branch" not in res


# --------------------------------------------------------------------------- #
# activity_since in the payload (wedge watchdog input) — still live
# --------------------------------------------------------------------------- #
def test_snapshot_carries_activity_since(monkeypatch):
    inst = _FakeInst("", title="act-t")
    monkeypatch.setitem(server.ENGINE.instances, inst.Title, inst)
    # Stub the live probe so it can't overwrite the seeded cache record.
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    # ``state_since`` is the key every layer of _agent_activity writes through
    # agent_state._verdict. This test used to seed ``changed_epoch``, a key
    # nothing has ever written — which is exactly how the field stayed dead in
    # production while its test went on passing.
    monkeypatch.setitem(
        server._ACTIVITY_CACHE,
        inst.Title,
        {"hash": "", "state_since": 1234.5, "reported": "idle", "streak": 0},
    )
    snap = server._build_instances_snapshot()
    mine = [d for d in snap if d["title"] == inst.Title]
    assert mine and mine[0]["activity_since"] == 1234.5


def test_activity_since_is_zero_for_a_session_with_no_reading(monkeypatch):
    """0 is the "unknown" the frontend gates on — an offline session, or one the
    poll has not classified yet, must not look like it changed state at the
    epoch. The wedge rule reads ``Number(activity_since) > 0`` for exactly this,
    and would otherwise call every unseen session stuck since 1970."""
    inst = _FakeInst("", title="act-none")
    monkeypatch.setitem(server.ENGINE.instances, inst.Title, inst)
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "offline")
    server._ACTIVITY_CACHE.pop(inst.Title, None)
    snap = server._build_instances_snapshot()
    mine = [d for d in snap if d["title"] == inst.Title]
    assert mine and mine[0]["activity_since"] == 0.0


def test_the_cheap_snapshot_publishes_activity_since_too(monkeypatch):
    """The two snapshot builders are one payload as far as a client is
    concerned. The cheap path (a session too new/paused to probe) carried the
    same dead ``changed_epoch`` read, so a row could lose the field simply by
    being served from the other branch."""
    inst = _FakeInst("", title="act-cheap")
    monkeypatch.setitem(
        server._ACTIVITY_CACHE,
        inst.Title,
        {"hash": "", "state_since": 4321.0, "reported": "idle"},
    )
    d = server._session_snapshot_cheap(inst, {})
    assert d["activity_since"] == 4321.0


# --------------------------------------------------------------------------- #
# frontend wiring (house-style markup assertions)
# --------------------------------------------------------------------------- #
client = TestClient(server.app)


def test_app_js_has_no_update_from_base_wiring():
    js = client.get("/app.js").text
    assert 'data-act="updatebase"' not in js
    assert "/update-from-base" not in js
    assert "_withBehind" not in js
    assert "behind_base" not in js


def test_index_has_no_bulk_update_button():
    html = client.get("/").text
    assert 'id="update-all-btn"' not in html


def test_app_js_has_wedge_watchdog_rule():
    js = client.get("/app.js").text
    assert "WEDGE_IDLE_S" in js
    assert "activity_since" in js
    assert "possibly stuck" in js


def test_a_committed_and_walked_away_session_is_not_called_stuck():
    """This branch had never once rendered — ``activity_since`` read a key
    nothing wrote, so it was 0 for every session. Now that it is live, what it
    calls "unfinished work" has to be true: a COMMITTED branch is finished as
    far as git is concerned (the header is asking for a push, not reporting a
    problem), and counting it would put every session anyone committed and
    walked away from on the bell's attention badge, claiming to be wedged.
    """
    js = client.get("/app.js").text
    at = js.index("const unfinished = ")
    rule = js[at : js.index(";", at)]
    assert "un.additions" in rule and "un.deletions" in rule
    assert "committed" not in rule
