"""The "back to idle" pin: restarting the guided cycle on a finished branch.

Three layers, because the feature's whole safety argument lives in the seams
between them:

* the store, whose release condition is the WORKTREE (a sha and a dirty flag)
  and never the stage label — filing a PR moves the label a beat after the pin is
  set, so a label-keyed release would cancel itself;
* the stage probe, which publishes ``stage_reset`` ALONGSIDE the git-derived
  ``stage`` rather than instead of it, so nothing that reads git truth (the
  autopilot driver, the check kicker) can be fooled by a display pin;
* the route, which also takes down the finished cycle's leftovers — a halted
  fast-track and a STALE check result — while leaving a live chain and a current
  check failure strictly alone.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from backend.web import server
from backend.web.core import agent_state
from backend.web.core import autopilot as ap
from backend.web.core import stage_reset
from backend.web.core import worktree_setup as wts


def _git(*args: str, cwd: str) -> str:
    cp = subprocess.run(
        ["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


@pytest.fixture
def wt(tmp_path):
    """A repo on ``feat`` with one commit beyond ``main`` — the stage probe needs
    commits beyond the base before it will look past "agent"."""
    path = tmp_path / "repo"
    path.mkdir()
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

    def __init__(self, wt_path: str, title: str = "sr"):
        self.Title = title
        self.Branch = "feat"
        self.BaseBranch = "main"
        from backend.session.storage import Status

        self.Status = Status.Running
        self._wt = wt_path

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
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
    stage_reset.prune([])
    yield
    for cache in caches:
        cache.clear()
    stage_reset.prune([])


def _head(wt) -> str:
    return _git("rev-parse", "HEAD", cwd=str(wt))


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def test_pin_holds_only_while_the_worktree_stands_still():
    assert stage_reset.pin("s", "sha1") is True
    assert stage_reset.active("s", "sha1", False) is True
    # A dirty tree means new work exists, so reality is already at the start of
    # the ladder and the pin has nothing left to do.
    assert stage_reset.active("s", "sha1", True) is False
    assert stage_reset.get("s") is None, "release happens in place, on read"

    stage_reset.pin("s", "sha1")
    # A new commit is a real ladder to climb again.
    assert stage_reset.active("s", "sha2", False) is False
    assert stage_reset.titles() == []


def test_pin_refuses_without_a_head_because_the_sha_is_the_release_condition():
    assert stage_reset.pin("s", "") is False
    assert stage_reset.pin("", "sha") is False
    assert stage_reset.titles() == []
    # An unknown head at read time cannot be proved equal, so it releases.
    stage_reset.pin("s", "sha1")
    assert stage_reset.active("s", "", False) is False


def test_prune_drops_pins_for_dead_titles():
    stage_reset.pin("live", "sha")
    stage_reset.pin("gone", "sha")
    assert stage_reset.prune(["live"]) == 1
    assert stage_reset.titles() == ["live"]
    assert stage_reset.active("gone", "sha", False) is False


def test_clear_is_idempotent():
    stage_reset.pin("s", "sha")
    assert stage_reset.clear("s") is True
    assert stage_reset.clear("s") is False


# --------------------------------------------------------------------------- #
# the probe: stage_reset rides ALONGSIDE the stage
# --------------------------------------------------------------------------- #
def test_probe_publishes_the_pin_without_touching_the_git_derived_stage(wt):
    inst = _FakeInst(str(wt), "p1")
    base = server._session_stage(inst)
    assert base["stage"] == "committed" and base["stage_reset"] is False

    stage_reset.pin("p1", _head(wt))
    server._PROBE_CACHE.clear()
    res = server._session_stage(inst)
    # THE POINT: the stage still says "committed". Only the extra flag moves, so
    # the autopilot driver and the check kicker keep reading git truth.
    assert res["stage"] == "committed"
    assert res["stage_reset"] is True


def test_a_new_commit_releases_the_pin(wt):
    inst = _FakeInst(str(wt), "p2")
    stage_reset.pin("p2", _head(wt))
    (wt / "c.txt").write_text("three\n")
    _git("add", "-A", cwd=str(wt))
    _git("commit", "-q", "-m", "more", cwd=str(wt))
    server._PROBE_CACHE.clear()
    res = server._session_stage(inst)
    assert res["stage"] == "committed" and res["stage_reset"] is False
    assert stage_reset.titles() == []


def test_a_dirty_tree_releases_the_pin_on_the_early_return(wt):
    # The dirty branch returns before the pin is ever consulted, so it must clear
    # the pin itself — otherwise a pin could outlive the work that ended it and
    # re-apply after the next commit.
    inst = _FakeInst(str(wt), "p3")
    stage_reset.pin("p3", _head(wt))
    (wt / "a.txt").write_text("edited\n")
    server._PROBE_CACHE.clear()
    res = server._session_stage(inst)
    assert res["stage"] == "agent" and res["stage_reset"] is False
    assert stage_reset.titles() == []


def test_the_pin_survives_the_pushed_to_pr_flip(wt, monkeypatch):
    # The regression that killed the label-keyed version: the reset lands while
    # the stage still reads "pushed", and the PR lookup moves it to "pr" on the
    # very next pass.
    inst = _FakeInst(str(wt), "p4")
    monkeypatch.setattr(server, "_origin_branch_sha", lambda *a, **k: _head(wt))
    monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
    assert server._session_stage(inst)["stage"] == "pushed"

    stage_reset.pin("p4", _head(wt))
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "OPEN"}
    )
    server._PROBE_CACHE.clear()
    server._PR_CACHE.clear()
    res = server._session_stage(inst)
    assert res["stage"] == "pr"
    assert res["stage_reset"] is True


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
def _reset(title):
    resp = asyncio.run(server.instance_reset_stage(title))
    return resp.status_code, json.loads(resp.body)


@pytest.fixture
def routed(wt, monkeypatch):
    """The session in the engine, so the route can find it."""
    inst = _FakeInst(str(wt), "r1")
    monkeypatch.setitem(server.ENGINE.instances, "r1", inst)
    return inst


def test_route_pins_the_window(routed, wt):
    code, body = _reset("r1")
    assert code == 200
    assert body["ok"] is True and body["pinned"] is True and body["dirty"] is False
    assert stage_reset.active("r1", _head(wt), False) is True


def test_route_does_not_pin_a_dirty_tree(routed, wt):
    (wt / "a.txt").write_text("edited\n")
    code, body = _reset("r1")
    assert code == 200
    # Already at the start of the ladder — storing a pin the next stage read
    # would discard would only make "is it pinned?" answerable two ways.
    assert body["dirty"] is True and body["pinned"] is False
    assert stage_reset.titles() == []


def test_route_404s_an_unknown_session():
    code, _ = _reset("no-such-session")
    assert code == 404


def test_pressing_again_is_a_no_op_not_an_error(routed, wt):
    # ↺ is a one-shot action, and the UI hides it once the pin holds — but the
    # route is the contract, and a second press (a stale client, a keybinding)
    # must land on the same pin rather than fail or double up.
    _reset("r1")
    code, body = _reset("r1")
    assert code == 200 and body["pinned"] is True
    assert stage_reset.active("r1", _head(wt), False) is True


def test_route_clears_a_halted_fast_track_but_never_a_live_one(routed):
    ap.arm("r1", "pr", source="session")
    ap.halt("r1", "pre-commit failed")
    _code, body = _reset("r1")
    assert "fast-track" in body["cleared"]
    assert ap.get("r1") is None

    ap.arm("r1", "pr", source="session")  # running
    _code, body = _reset("r1")
    assert body["cleared"] == []
    assert (ap.get("r1") or {}).get("state") == "running", "a live chain is untouched"


def test_route_clears_a_stale_check_but_not_one_that_matches_head(routed, wt):
    # Stale: recorded against a commit that is no longer HEAD.
    wts._write_status(
        str(wt),
        wts.CHECK_STATUS,
        {"state": "ok", "rc": 0, "sha": "0" * 40, "command": "pytest"},
    )
    _code, body = _reset("r1")
    assert "checks" in body["cleared"]
    assert wts.check_status(str(wt)) is None

    # Current failure: the push gate reads this file, so clearing it here would
    # silently un-gate the very push it exists to hold.
    wts._write_status(
        str(wt),
        wts.CHECK_STATUS,
        {"state": "failed", "rc": 1, "sha": _head(wt), "command": "pytest"},
    )
    _code, body = _reset("r1")
    assert body["cleared"] == []
    assert (wts.check_status(str(wt)) or {}).get("state") == "failed"


# --------------------------------------------------------------------------- #
# The docs. The stage/stage_reset split is the whole safety argument, and it is
# now public prose, which makes it contractual: these guard the names a reader
# would go looking for (and the two docs against drifting apart again).
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parents[2]


def _doc(name: str) -> str:
    return (_ROOT / "docs" / name).read_text(encoding="utf-8")


def test_web_ui_docs_name_the_route_the_row_field_and_the_module():
    text = _doc("web-ui.md")
    assert "POST /api/instances/{title}/reset-stage" in text
    assert "stage_reset" in text
    assert "backend/web/core/stage_reset.py" in text
    # The prose cites a path, so the path is part of the contract: splitting the
    # module has to break a test rather than quietly rot the sentence.
    assert (_ROOT / "backend" / "web" / "core" / "stage_reset.py").exists()


def test_web_ui_docs_hold_the_two_invariants_the_pin_is_safe_because_of():
    text = _doc("web-ui.md")
    # (1) the pin is published ALONGSIDE the git-derived stage, never instead of
    # it — folding it in would let an armed fast-track chain commit a clean tree;
    assert "does **not** touch the published `stage`" in text
    # (2) release is keyed on the worktree, because filing a PR flips the label
    # `pushed` -> `pr` a beat after the press.
    assert "never against the stage label" in text
    assert "one-shot action, not a toggle" in text


def test_web_api_docs_carry_the_route_and_the_row_field_too():
    """web-api.md is where an API reader looks; it documented neither."""
    text = _doc("web-api.md")
    assert '"stage_reset"' in text  # in the GET /api/instances row shape
    row = [ln for ln in text.splitlines() if "/api/instances/{title}/reset-stage" in ln]
    assert row, "the guided-workflow table never mentions /reset-stage"
    assert any("`stage` is untouched" in ln for ln in row)
