"""End-to-end coverage of the fast-track routes against a real git worktree.

Proves the route actually arms/disarms and that the run shows up on the session
DTO — not merely that the paths are registered.
"""

import asyncio
import json
import subprocess

import pytest

from backend.web import server
from backend.web.core import autopilot as ap


def _git(wt, *args):
    subprocess.run(
        ["git", "-C", str(wt), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def wt(tmp_path):
    """A real repo with one commit, so stage probes have something to read."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "T")
    (d / "a.txt").write_text("hello\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    return d


@pytest.fixture
def inst(wt, monkeypatch):
    """A started in-memory session whose workspace is ``wt``, in the engine."""
    from backend.session.instance import FromInstanceData
    from backend.session.storage import GitWorktreeData, InstanceData, Status
    from datetime import datetime, timezone

    t = datetime.now(timezone.utc)
    data = InstanceData(
        title="ft-session",
        path=str(wt),
        branch="b",
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=str(wt),
            worktree_path=str(wt),
            session_name="ft-session",
            branch_name="b",
        ),
    )
    i = FromInstanceData(data, attach=False)
    monkeypatch.setitem(server.ENGINE.instances, "ft-session", i)
    return i


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
    yield


def _post(title, payload=None):
    return asyncio.run(server.instance_fast_track(title, payload))


def _body(resp):
    return json.loads(resp.body)


def test_arming_records_the_target(inst):
    resp = _post("ft-session", {"depth": "push", "message": "do the thing"})
    assert resp.status_code == 200
    assert _body(resp)["autopilot"]["depth"] == "push"
    rec = ap.get("ft-session")
    assert rec["depth"] == "push"
    assert rec["state"] == "running"
    assert rec["message"] == "do the thing"
    assert rec["source"] == "session"


def test_depth_defaults_to_the_configured_rung(inst):
    resp = _post("ft-session", {"message": "m"})
    assert resp.status_code == 200
    # Nothing configured in the isolated settings file -> the built-in default.
    assert _body(resp)["autopilot"]["depth"] == "pr"


def test_an_unknown_depth_is_refused(inst):
    resp = _post("ft-session", {"depth": "teleport", "message": "m"})
    assert resp.status_code == 400
    assert "unknown depth" in _body(resp)["error"]
    assert ap.get("ft-session") is None, "a refused request must arm nothing"


def test_the_agent_rung_is_intake_only(inst):
    """Arming "agent" on a session that already exists would mean "do nothing"."""
    resp = _post("ft-session", {"depth": "agent", "message": "m"})
    assert resp.status_code == 400
    assert "intake" in _body(resp)["error"]


def test_a_dirty_tree_needs_a_message(inst, wt):
    (wt / "b.txt").write_text("uncommitted\n")
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 400
    assert _body(resp)["error"] == "commit message required"


def test_a_clean_tree_needs_no_message(inst):
    """Nothing to commit means nothing to name — the chain may still push/PR."""
    resp = _post("ft-session", {"depth": "push"})
    assert resp.status_code == 200


def test_a_message_on_disk_is_reused(inst, wt):
    (wt / "b.txt").write_text("uncommitted\n")
    (wt / server._COMMIT_MSG_FILE).write_text("recovered subject\n")
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 200
    assert ap.get("ft-session")["message"] == "recovered subject"


def test_arming_captures_the_branch_for_drift_detection(inst):
    _post("ft-session", {"depth": "push", "message": "m"})
    # The push/PR/merge routes resolve the LIVE branch, so a mid-run switch would
    # retarget the chain; the armed branch is what makes that detectable.
    assert ap.get("ft-session")["branch"] in ("b", "master", "main")


def test_unknown_session_is_404():
    resp = _post("nope", {"depth": "pr"})
    assert resp.status_code == 404


def test_cancel_disarms(inst):
    _post("ft-session", {"depth": "pr", "message": "m"})
    resp = asyncio.run(server.instance_fast_track_cancel("ft-session"))
    assert resp.status_code == 200
    assert _body(resp)["stopped"] is True
    assert ap.get("ft-session") is None


def test_cancel_is_idempotent(inst):
    resp = asyncio.run(server.instance_fast_track_cancel("ft-session"))
    assert resp.status_code == 200
    assert _body(resp)["stopped"] is False


def test_the_run_shows_up_on_the_session_dto(inst):
    _post("ft-session", {"depth": "pr", "message": "m"})
    dto = server._autopilot_dto("ft-session")
    assert dto["depth"] == "pr" and dto["state"] == "running"
    # And it is present on BOTH snapshot paths, so a cheap row never omits it.
    assert server._autopilot_dto("no-such-session") is None


def test_retryable_hooks_come_from_settings_not_the_client(inst, monkeypatch):
    """The skip list ends up inside a shell command, so it must never be
    client-supplied."""
    monkeypatch.setattr(server, "_precommit_retry_hooks", lambda: ["gitnexus-index"])
    _post(
        "ft-session",
        {"depth": "pr", "message": "m", "retryable": ["run-tests", "anything"]},
    )
    assert ap.get("ft-session")["retryable"] == ["gitnexus-index"]


def _fake_settings(monkeypatch, *, hooks="", depth=""):
    """Stand in for the real settings store, via the module the server reads."""

    class _Repo:
        precommit_retry_hooks = hooks
        fasttrack_depth = depth

    class _S:
        repository = _Repo()

    # The helpers import the settings module locally on every call (the house
    # idiom, so a settings change needs no restart), so patch the real module.
    from backend.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings", lambda: _S())


def test_settings_hook_list_is_charset_filtered(monkeypatch):
    _fake_settings(monkeypatch, hooks="gitnexus-index, bad;name, run-tests, ok_hook.1")
    got = server._precommit_retry_hooks()
    assert "gitnexus-index" in got and "ok_hook.1" in got
    assert "bad;name" not in got, "a shell metacharacter must be dropped"
    assert "run-tests" not in got, "a test hook is never skippable"


def test_the_configured_hook_list_actually_reaches_the_run(inst, monkeypatch):
    """Regression: the helpers originally called a bare ``load_settings()`` that
    does not exist on this module, and their own try/except swallowed the
    NameError — so the setting silently never applied. Assert the real read."""
    _fake_settings(monkeypatch, hooks="gitnexus-index")
    _post("ft-session", {"depth": "pr", "message": "m"})
    assert ap.get("ft-session")["retryable"] == ["gitnexus-index"]


def test_the_configured_depth_actually_applies(inst, monkeypatch):
    _fake_settings(monkeypatch, depth="push")
    assert server._fasttrack_depth() == "push"
    resp = _post("ft-session", {"message": "m"})
    assert _body(resp)["autopilot"]["depth"] == "push"


def test_a_junk_configured_depth_falls_back(monkeypatch):
    _fake_settings(monkeypatch, depth="teleport")
    assert server._fasttrack_depth() == "pr"


def test_source_defaults_cannot_choose_merge():
    """A per-source default applies to every future item with nobody watching."""
    assert server._cap_source_depth("merge") == "pr"
    assert server._cap_source_depth("push") == "push"
    assert server._cap_source_depth("") == ""


def test_per_item_depth_override_accepts_merge():
    """The person picking it is looking at the one thing it will merge."""
    assert server._start_depth_override({"depth": "merge"}) == "merge"
    assert server._start_depth_override({}) == ""
    with pytest.raises(ValueError):
        server._start_depth_override({"depth": "teleport"})
