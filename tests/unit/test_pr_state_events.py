"""``session.pr_state_changed`` fires once per REAL pull-request transition.

The regression this locks down: "PR merged or closed" and "PR open" used to be
inferred from the stage pill leaving / re-entering the ``pr`` rung. But
``_session_stage`` returns ``agent`` the moment the tree is dirty — before it
even looks at the PR — so an agent iterating on review feedback walked
``pr -> agent -> committed -> pushed -> pr`` on every single edit, and the user
got "PR merged or closed ✓" followed by "PR open for X", over and over, for a
pull request that never moved.

The event now comes from the PR lookup's own state, so a PR that stays open is
silent no matter what the working tree does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.session.storage import Status
from backend.web import server
from backend.web.addons import notify
from backend.web.core import agent_state
from backend.web.core import events as events_mod


# --------------------------------------------------------------------------- #
# helpers (same shapes as test_stage_branch_drift.py)
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: str) -> str:
    cp = subprocess.run(
        ["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _repo_with_feature_commit(path: Path) -> Path:
    """A repo on ``feat`` with one commit beyond ``main`` — so the stage probe
    has something to push and actually performs the PR lookup."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    exclude = path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".mindflock_*\n")
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-q", "-m", "init", cwd=str(path))
    _git("checkout", "-q", "-b", "feat", cwd=str(path))
    (path / "b.txt").write_text("two\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-q", "-m", "work", cwd=str(path))
    return path


class _FakeInst:
    Program = "bash"
    Path = ""
    InPlace = False

    def __init__(self, wt: str, *, branch: str = "feat", title: str = "t"):
        self.Title = title
        self.Branch = branch
        self.BaseBranch = "main"
        self.Status = Status.Running
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def bus_events():
    """Every envelope emitted during the test, in order."""
    seen: list = []
    unsubscribe = events_mod.BUS.subscribe(seen.append)
    yield seen
    unsubscribe()


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    caches = (
        server._BASE_BRANCH_CACHE,
        server._DIFF_STAT_CACHE,
        server._PR_CACHE,
        server._ORIGIN_SHA_CACHE,
        server._PROBE_CACHE,
        agent_state._LAST_BRANCH,
        agent_state._LAST_PR_STATE,
    )
    for cache in caches:
        cache.clear()
    monkeypatch.setattr(server, "_failed_precommit_step", lambda title: "hook")
    yield
    for cache in caches:
        cache.clear()


def _pr_states(envelopes):
    return [
        (e["old"], e["new"])
        for e in envelopes
        if e["event"] == "session.pr_state_changed"
    ]


# --------------------------------------------------------------------------- #
# The regression: a working tree churning under an OPEN PR is silent
# --------------------------------------------------------------------------- #
def test_dirty_tree_under_open_pr_emits_nothing(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "OPEN"}
    )

    # Poll once with a clean tree: first sighting only SEEDS (a restart must not
    # re-announce a PR that was already open).
    server._session_stage(inst)
    assert _pr_states(bus_events) == []

    # The agent edits a file -> the stage ladder drops to "agent"...
    (wt / "a.txt").write_text("edited\n")
    server._PROBE_CACHE.clear()
    assert server._session_stage(inst)["stage"] == "agent"

    # ...and the agent commits, walking the stage back up toward "pr".
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "fix review", cwd=str(wt))
    server._PROBE_CACHE.clear()
    server._session_stage(inst)

    # The PR was OPEN the whole time, so nothing was announced.
    assert _pr_states(bus_events) == []


# --------------------------------------------------------------------------- #
# Real transitions still fire — exactly once
# --------------------------------------------------------------------------- #
def test_merge_emits_once(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    state = {"state": "OPEN"}
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": state["state"]}
    )

    server._session_stage(inst)  # seed OPEN
    state["state"] = "MERGED"
    server._PROBE_CACHE.clear()
    server._session_stage(inst)
    assert _pr_states(bus_events) == [("OPEN", "MERGED")]

    # Polling again on the same (merged) PR must not re-announce it.
    server._PROBE_CACHE.clear()
    server._session_stage(inst)
    assert _pr_states(bus_events) == [("OPEN", "MERGED")]


def test_pr_appearing_emits_open(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    info = {"value": None}
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: info["value"])

    server._session_stage(inst)  # no PR yet
    info["value"] = {"url": "u", "state": "OPEN"}
    server._PR_CACHE.clear()
    server._PROBE_CACHE.clear()
    server._session_stage(inst)
    # First OPEN observation seeds rather than emitting: there is no prior state
    # to have transitioned FROM, and re-announcing on every server restart is
    # the noise this whole change exists to remove.
    assert _pr_states(bus_events) == []


# --------------------------------------------------------------------------- #
# A missing answer is never a close
# --------------------------------------------------------------------------- #
def test_failed_lookup_does_not_fake_a_close(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    info = {"value": {"url": "u", "state": "OPEN"}}
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: info["value"])

    server._session_stage(inst)  # seed OPEN
    info["value"] = None  # rate limit / no gh / network blip
    server._PROBE_CACHE.clear()
    server._session_stage(inst)

    assert _pr_states(bus_events) == []
    # ...and the remembered state is untouched, so the real merge still fires.
    info["value"] = {"url": "u", "state": "CLOSED"}
    server._PROBE_CACHE.clear()
    server._session_stage(inst)
    assert _pr_states(bus_events) == [("OPEN", "CLOSED")]


def test_branch_switch_reseeds_instead_of_emitting(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "OPEN"}
    )
    server._session_stage(inst)  # seed OPEN on "feat"

    # Same session, second branch, whose PR lookup reports MERGED. That is a
    # DIFFERENT pull request — not this one closing.
    _git("checkout", "-q", "-b", "feat2", cwd=str(wt))
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u2", "state": "MERGED"}
    )
    server._PROBE_CACHE.clear()
    server._session_stage(inst)

    assert _pr_states(bus_events) == []
    assert agent_state._LAST_PR_STATE[inst.Title] == ("feat2", "MERGED")


# --------------------------------------------------------------------------- #
# The notify rule follows the new event, and no longer the stage
# --------------------------------------------------------------------------- #
def test_notify_rule_keys_off_pr_state_not_stage():
    rule = next(r for r in notify.NOTIFY_RULES if r["id"] == "pr_closed")
    assert rule["event"] == "session.pr_state_changed"

    # The transition that used to cause the false alarm no longer matches...
    assert not notify._matches(
        rule, {"event": "session.stage_changed", "old": "pr", "new": "agent"}
    )
    # ...while both genuine endings do.
    for ending in ("MERGED", "CLOSED"):
        assert notify._matches(
            rule,
            {"event": "session.pr_state_changed", "old": "OPEN", "new": ending},
        )
    # A PR opening is not a closing.
    assert not notify._matches(
        rule, {"event": "session.pr_state_changed", "old": "", "new": "OPEN"}
    )
