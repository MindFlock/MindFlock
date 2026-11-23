"""Security review of the cross-device follow-ups (PR #435): one test per
finding, written to fail first.

H1  spawn_session(device=) must not escape the caller's fence, red zones,
    ship lane or program.
M1  … nor reach an UNLINKED tailnet machine.
M2  /api/origin-inbox takes only a VERIFIED device identity.
M3  The detached-checkout attach refuses a branch checked out elsewhere.
M4  On the fleet path the attach happens only at a published release tag.
L1  A `started` record is pinned to the origin session's creation time.
L2  A receiver record binds only to the session its create made.
L3  Device authorization compares exact keys, never folded names.
L4  include_ignored: no ready-made bypass command in what an agent reads;
    the CLI flag asks the person at a terminal.
L5  origin-control's status answers nothing before the provenance check.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from backend.mcp.protocol import ToolError
from backend.web.core import dev_update
from backend.web.core import fleet_update
from backend.web.core import remote
from backend.web.core import remote_origin as ro
from backend.web.core import self_update
from backend.web.core import send_files
from backend.web.core.send_session import Refusal
from tests.unit import test_dev_update as tdu
from tests.unit import test_remote_origin as tro
from tests.unit._mcp_fakes import FakeCtx, make_box, row

link = tro.link
repos = tdu.repos
LAPTOP = tro.LAPTOP

_DEVICES = {
    "devices": [
        {
            "device": "rig",
            "host": "Rig",
            "member": True,
            "same_fleet": True,
            "connected": True,
        },
        {"device": "stranger", "host": "Stranger", "connected": True},
    ]
}


def _box(**me_row):
    rows = [row("orch", **me_row)]
    box, api, clock = make_box(rows, me="orch")
    api.top_routes[("GET", "/api/devices")] = _DEVICES
    seen = []
    for key in ("rig", "stranger"):
        api.top_routes[("POST", "/api/devices/%s/fwd/api/instances" % key)] = (
            lambda payload, q, key=key: seen.append((key, payload))
            or {"title": payload["title"]}
        )
    return box, api, clock, seen


def _spawn(box, clock, **kw):
    args = dict(device="Rig", repo_path="/there", prompt="go", title="t")
    args.update(kw)
    return box.spawn_session(args, FakeCtx(clock))


# --------------------------------------------------------------------------- #
# H1 / M1
# --------------------------------------------------------------------------- #
class TestDeviceSpawn:
    def test_a_fenced_caller_is_refused(self):
        box, api, clock, seen = _box(order={"fence": {"only": ["src/**"]}})
        with pytest.raises(ToolError, match="fence"):
            _spawn(box, clock)
        assert seen == []

    def test_a_caller_with_red_zones_is_refused(self):
        box, api, clock, seen = _box(
            redzone={"zones": [{"path": "infra/**"}], "breaches": []}
        )
        with pytest.raises(ToolError, match="red zone"):
            _spawn(box, clock)
        assert seen == []

    def test_a_caller_whose_lane_stops_before_push_is_refused(self):
        box, api, clock, seen = _box(
            lane={"target": "commit", "ask_first": False, "owner": ""}
        )
        with pytest.raises(ToolError):
            _spawn(box, clock)
        assert seen == []

    def test_only_the_callers_own_program(self):
        box, api, clock, seen = _box(program="claude")
        with pytest.raises(ToolError, match="program"):
            _spawn(box, clock, program="bash")
        _spawn(box, clock, program="claude")
        assert seen[0][1]["program"] == "claude"
        # Default: the caller's own.
        _spawn(box, clock, title="t2")
        assert seen[1][1]["program"] == "claude"

    def test_an_unlinked_machine_is_never_chosen(self):
        box, api, clock, seen = _box()
        with pytest.raises(ToolError, match="not one of the user's devices"):
            _spawn(box, clock, device="Stranger")
        assert seen == []


# --------------------------------------------------------------------------- #
# M2 / L1 / L3 / L5
# --------------------------------------------------------------------------- #
class TestOriginInbox:
    def test_an_unverified_claim_is_refused(self, link, monkeypatch):
        link.instances["orch"] = tro._Inst("orch")
        ro.note_started(LAPTOP, "rw", "orch")
        monkeypatch.setattr(remote, "device_for_ip", lambda ip: "")
        r = tro._inbox()  # only the marker says LAPTOP
        assert r.status_code == 403 and r.json()["code"] == "unverified"

    def test_remote_control_applies_like_everywhere(self, link, monkeypatch):
        link.instances["orch"] = tro._Inst("orch")
        ro.note_started(LAPTOP, "rw", "orch")
        monkeypatch.setattr(remote, "remote_control_enabled", lambda: False)
        assert tro._inbox().status_code == 403

    def test_a_reused_origin_title_inherits_nothing(self, link):
        old = tro._Inst("orch", created=time.time() - 3600)
        link.instances["orch"] = old
        ro.note_started(LAPTOP, "rw", "orch")
        # orch closed; a NEW session takes the name.
        link.instances["orch"] = tro._Inst("orch", created=time.time())
        r = tro._inbox()
        assert r.status_code == 403 and r.json()["code"] == "not_started_here"


def test_device_keys_compare_exactly():
    ro.note_created(
        "rw",
        {"device": "a-b", "device_name": "A-B", "verified": True},
        {"session": "s", "by": "agent"},
    )
    inst = tro._Inst("rw")
    who = {"device": "ab", "device_name": "AB", "verified": True}
    why = ro.control_refusal("rw", inst, who, "s")
    assert why is not None and why[1] == "not_origin"
    who["device"] = "a-b"
    assert ro.control_refusal("rw", inst, who, "s") is None


def test_status_of_a_closed_session_needs_provenance_first(link, monkeypatch):
    from backend.web.core import recently_closed

    ro.note_created(
        "gone",
        {"device": "otherbox", "device_name": "Other", "verified": True},
        {"session": "s", "by": "agent"},
    )
    monkeypatch.setattr(
        recently_closed,
        "_load_recently_closed",
        lambda: [
            {"id": "gone-1", "title": "gone", "closed_at": "2026-11-22T10:00:00+00:00"}
        ],
    )
    r = tro._control("gone", "status", origin_session="s")
    assert r.status_code in (403, 404)
    assert "closed_at" not in r.text


# --------------------------------------------------------------------------- #
# L2: a record binds only to the session its create made
# --------------------------------------------------------------------------- #
def test_a_live_create_is_pinned_at_once(link):
    import asyncio

    from starlette.responses import JSONResponse

    async def inner(payload):
        link.instances["w"] = tro._Inst("w")
        return JSONResponse({"title": "w"}, status_code=202)

    who = {"device": LAPTOP, "device_name": "L", "verified": True}
    asyncio.run(
        ro.create_over_link(
            {"title": "w", "origin": {"session": "s", "by": "agent"}}, who, inner
        )
    )
    rec = ro._load()["here"]["w"]
    assert rec["created_at"] is not None


def test_a_queued_create_that_failed_binds_nothing(link, monkeypatch):
    from backend.web import server

    ro.note_created(
        "q",
        {"device": LAPTOP, "device_name": "L", "verified": True},
        {"session": "s", "by": "agent"},
    )
    monkeypatch.setitem(
        server._CREATE_FAILURES, "q", {"error": "boom", "ts": time.time()}
    )
    later = tro._Inst("q")
    assert ro.record("q", later) is None


def test_a_queued_agent_record_never_binds_a_persons_session(link):
    ro.note_created(
        "q",
        {"device": LAPTOP, "device_name": "L", "verified": True},
        {"session": "s", "by": "agent"},
    )
    mine = tro._Inst("q")
    mine.Spawned = False  # a person made a session of that name
    assert ro.record("q", mine) is None


# --------------------------------------------------------------------------- #
# M3 / M4: the attach
# --------------------------------------------------------------------------- #
def test_attach_refuses_a_branch_checked_out_in_another_worktree(repos, tmp_path):
    other = tmp_path / "other-wt"
    tdu._git(repos.work, "checkout", "-q", "--detach", "HEAD")
    tdu._git(repos.work, "worktree", "add", "-q", str(other), "main")
    repos.publish()
    # main is checked out in other-wt now; this checkout is detached.
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    assert st["can_update"] is False and st["attach"] is None
    assert "checked out in another worktree" in st["why_not"]


def test_the_script_refuses_if_the_branch_got_checked_out_elsewhere(
    repos, tmp_path, monkeypatch
):
    tdu._detach_at(repos, drop_main=True)
    repos.publish()
    st = dev_update.status(str(repos.work), refresh=True, force=True)
    other = tmp_path / "other-wt"
    tdu._git(repos.work, "worktree", "add", "-q", "-b", "main", str(other), st["head"])
    cp, state, refreshes = tdu._run_dev_script(
        tmp_path, monkeypatch, st, curl_bodies=[None]
    )
    assert state["state"] == "failed" and refreshes == []


def _tag(repos, name):
    tdu._git(repos.seed, "tag", name)
    tdu._git(repos.seed, "push", "-q", "origin", name)


def _fleet_dev(monkeypatch, repos):
    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    monkeypatch.setattr(self_update, "install_checkout", lambda: str(repos.work))
    started = []
    monkeypatch.setattr(
        self_update, "start_dev_update", lambda **k: started.append(k) or {"ok": True}
    )

    async def ok(ref):
        return "", 200

    monkeypatch.setattr(self_update, "check_remote_ref", ok)
    return started


def test_fleet_attach_only_at_a_published_release(repos, monkeypatch):
    import asyncio

    started = _fleet_dev(monkeypatch, repos)
    tdu._detach_at(repos, drop_main=True)  # at "one": no tag
    repos.publish()
    out, status = asyncio.run(fleet_update.apply({"tag": "v9.9.9"}))
    assert status == 409 and out["blocked"] is True
    assert "mindflock update" in out["error"] and started == []


def test_fleet_attach_at_a_release_tag_goes(repos, monkeypatch):
    import asyncio

    started = _fleet_dev(monkeypatch, repos)
    _tag(repos, "v0.1.0")  # the commit the checkout sits on
    tdu._git(repos.work, "fetch", "-q", "--tags", "origin")
    tdu._detach_at(repos, drop_main=True)
    repos.publish()
    out, status = asyncio.run(fleet_update.apply({"tag": "v9.9.9"}))
    assert status == 200 and out["dev"] is True and len(started) == 1


# --------------------------------------------------------------------------- #
# L4
# --------------------------------------------------------------------------- #
def test_the_ignored_refusal_hands_no_ready_made_bypass(tmp_path):
    r = tmp_path / "wt"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    (r / ".gitignore").write_text("dist/\n")
    (r / "dist").mkdir()
    (r / "dist" / "Setup.exe").write_bytes(b"MZ")
    with pytest.raises(Refusal) as err:
        send_files.collect(str(r), ["dist/Setup.exe"])
    assert "--include-ignored" not in err.value.message
    assert "mindflock send-files" not in err.value.message


def test_the_cli_flag_asks_the_person_and_needs_a_terminal(monkeypatch, capsys):
    from backend import cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    assert cli._confirm_ignored(["dist/Setup.exe"]) is False
    assert "terminal" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli._confirm_ignored(["dist/Setup.exe"]) is True
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert cli._confirm_ignored(["dist/Setup.exe"]) is False
