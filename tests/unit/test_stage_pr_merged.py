"""``pr_merged`` on the stage probe: a branch whose PR was SQUASH-merged.

A squash merge leaves the branch's own commits beyond the base for good, so
the git-derived stage reads "pushed" (next: Make PR) about work that already
merged, and the session sat under Needs you as "pushed — ready for PR" for as
long as it stayed open. The stage keeps saying what git says; ``pr_merged``
says the branch is exactly the commit its merged PR was at, and the list
(frontend ``attentionItems``, the phone's ``needsYouKind``) leaves it out.
"""

from backend.web import server
from tests.unit.test_stage_reset import _FakeInst, _git, _head, wt  # noqa: F401


def _pushed(monkeypatch, wt, info):
    monkeypatch.setattr(server, "_origin_branch_sha", lambda *a, **k: _head(wt))
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: info)
    server._PROBE_CACHE.clear()
    server._PR_CACHE.clear()


def test_no_pr_is_plain_pushed(wt, monkeypatch):
    _pushed(monkeypatch, wt, None)
    res = server._session_stage(_FakeInst(str(wt), "m0"))
    assert res["stage"] == "pushed" and res["pr_merged"] is False


def test_a_squash_merged_pr_at_this_commit_is_pr_merged(wt, monkeypatch):
    _pushed(monkeypatch, wt, {"url": "u", "state": "MERGED", "head": _head(wt)})
    res = server._session_stage(_FakeInst(str(wt), "m1"))
    # The stage is still git's answer: Make PR comes back with the next commit.
    assert res["stage"] == "pushed"
    assert res["pr_merged"] is True
    assert res["pr_url"] == "u"


def test_new_commits_after_the_merge_are_ready_for_a_pr_again(wt, monkeypatch):
    merged_at = _head(wt)
    (wt / "more.txt").write_text("after the merge\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "follow-up", cwd=str(wt))
    _pushed(monkeypatch, wt, {"url": "u", "state": "MERGED", "head": merged_at})
    res = server._session_stage(_FakeInst(str(wt), "m2"))
    assert res["stage"] == "pushed" and res["pr_merged"] is False


def test_a_closed_pr_or_one_with_no_head_is_not_merged(wt, monkeypatch):
    _pushed(monkeypatch, wt, {"url": "u", "state": "CLOSED", "head": _head(wt)})
    assert server._session_stage(_FakeInst(str(wt), "m3"))["pr_merged"] is False
    # An older cache entry / a rung that could not say: never guessed.
    _pushed(monkeypatch, wt, {"url": "u", "state": "MERGED"})
    assert server._session_stage(_FakeInst(str(wt), "m4"))["pr_merged"] is False


def test_an_open_pr_is_the_pr_stage_and_not_merged(wt, monkeypatch):
    _pushed(monkeypatch, wt, {"url": "u", "state": "OPEN", "head": _head(wt)})
    res = server._session_stage(_FakeInst(str(wt), "m5"))
    assert res["stage"] == "pr" and res["pr_merged"] is False


def test_the_row_carries_it_and_the_cheap_path_defaults_it():
    assert "pr_merged" in server._SNAPSHOT_PROBE_KEYS
