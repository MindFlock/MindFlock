"""Regression pins for the review fixes applied on the streamline branch.

- Resume relaunches with the provider's RESUME command (not the first-start
  seed prompt) once Pause has killed the agent.
- Deleting an in-place session, or one whose folder is already gone, is never
  refused as "folder in use" (nothing would be removed).
- A wiped session is remembered as deleted, not closed, on its parent's Thread.
- The Verify orphan sweep keys on the hidden_by flag, never a title prefix.
"""

from __future__ import annotations

import os

import pytest

from backend.web import server
from backend.web.core import session_end


class _Inst:
    def __init__(self, path, in_place=False, title="t"):
        self.Title = title
        self.Path = str(path)
        self.InPlace = in_place
        self.Parent = ""
        self.PeerShare = ""
        self.killed = False

    def GetWorktreePath(self):
        return self.Path

    def Kill(self):
        self.killed = True

    def ToInstanceData(self):
        class _D:
            def to_dict(_s):
                return {"title": self.Title}

        return _D()


@pytest.fixture
def engine(monkeypatch):
    class _E:
        def __init__(self):
            self.instances = {}

        def save(self, **kw):
            pass

    e = _E()
    monkeypatch.setattr(server, "ENGINE", e)
    monkeypatch.setattr(server, "_record_closed", lambda inst, **kw: None)
    monkeypatch.setattr(session_end, "_server", lambda: server, raising=False)
    return e


class _Reached(Exception):
    """Raised by the stubbed recorder: the folder check let the wipe proceed."""


def test_folder_in_use_is_skipped_for_in_place_and_missing(
    tmp_path, engine, monkeypatch
):
    shared = tmp_path / "wt"
    shared.mkdir()
    engine.instances["origin"] = _Inst(shared, title="origin")
    engine.instances["copy"] = _Inst(shared, in_place=True, title="copy")
    engine.instances["gone"] = _Inst(tmp_path / "missing", title="gone")

    def _reached(inst, **kw):
        raise _Reached(inst.Title)

    monkeypatch.setattr(server, "_record_closed", _reached)
    # The copy shares the folder but owns nothing on disk: no refusal.
    assert session_end.folder_in_use(engine, "copy", str(shared))
    with pytest.raises(_Reached):
        session_end.end_session(engine, "copy", wipe=True)
    # A vanished folder has nothing left to lose either.
    with pytest.raises(_Reached):
        session_end.end_session(engine, "gone", wipe=True)
    # The owning worktree session is still protected while a sibling lives.
    with pytest.raises(session_end.FolderInUse):
        session_end.end_session(engine, "origin", wipe=True)


def test_closed_just_now_ignores_wiped_entries(monkeypatch):
    import datetime as dt

    now = dt.datetime.now().astimezone().isoformat()
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"title": "w", "closed_at": now, "wiped": True}],
    )
    assert server._closed_just_now("w") is False
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"title": "w", "closed_at": now, "wiped": False}],
    )
    assert server._closed_just_now("w") is True


def test_record_closed_stashes_wiped_and_hidden(tmp_path, monkeypatch):
    from backend.web.core import recently_closed as rc

    saved = {}
    monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
    monkeypatch.setattr(
        server, "_save_recently_closed", lambda items: saved.setdefault("items", items)
    )
    monkeypatch.setattr(rc, "_hidden_by_of", lambda title: "verify")
    inst = _Inst(tmp_path, title="verify-x")
    inst.Branch = "b"
    inst.Provisioned = False
    rc._record_closed(inst, wiped=True)
    entry = saved["items"][0]
    assert entry["wiped"] is True and entry["hidden_by"] == "verify"


def test_orphan_sweep_source_keys_on_flag():
    import inspect

    src = inspect.getsource(server._sweep_orphan_verify_sessions)
    assert 'hidden_by(title) != "verify"' in src
    assert 'startswith("verify-")' not in src


def test_resume_relaunch_uses_resume_command():
    import inspect

    from backend.session import instance as inst_mod

    src = inspect.getsource(inst_mod.Instance.Resume)
    # Both current-scheme relaunch branches reconfigure with resume=True.
    assert src.count("_configure_launch_command(resume=True)") == 2
    sig = inspect.signature(inst_mod.Instance._configure_launch_command)
    assert sig.parameters["resume"].default is False


def test_tracker_step_is_derived_from_existing_sources():
    """A veteran with ticket sources must not be asked to 'Connect a tracker'
    again after upgrading: the load derives the step from the store."""
    from backend.config import settings as S

    raw = {
        "general": {"onboarded": True},
        "ticketing": {
            "sources": [
                {"id": "a", "provider": "shortcut", "api_token": "t", "member_id": "m"}
            ]
        },
    }
    s = S.Settings.from_dict(raw)
    assert s.general.setup["repo"]["done"] is True
    assert s.general.setup["tracker"]["done"] is True
    # ...and never un-marks a skipped step or invents one without sources.
    s2 = S.Settings.from_dict({"general": {"setup": {"tracker": {"skipped": True}}}})
    assert s2.general.setup["tracker"].get("done") is not True
    assert S.Settings.from_dict({}).general.setup["tracker"].get("done") is not True


def test_onboarded_is_not_an_open_field():
    from backend.web.addons import settings as addon

    assert "general.onboarded" not in addon._OPEN_FIELDS


def test_revive_skips_an_agent_the_user_quit(tmp_path):
    from backend.web.core import revive

    class _Srv:
        def _read_exit_marker(self, name):
            return 0

        def _is_natural_exit(self, code):
            return code == 0

    wt = tmp_path / "wt"
    wt.mkdir()
    inst = _Inst(wt, title="quit")
    inst.Status = None
    assert revive.skip_reason(_Srv(), inst, "quit", 0.0) == revive.SKIP_QUIT


def test_revive_on_start_env_kill_switch(monkeypatch):
    monkeypatch.setattr(server, "_REVIVE_ON_START_ENABLED", None)
    monkeypatch.setenv("MINDFLOCK_REVIVE_ON_START", "0")
    assert server._revive_on_start_loop_enabled() is False


def test_person_id_switch_rewrites_session_stamps(tmp_path, monkeypatch):
    from backend.web.core import person_sessions as ps

    monkeypatch.setattr(ps, "_path", lambda: str(tmp_path / "people.json"))
    monkeypatch.setattr(ps, "_PERSON", None, raising=False)
    old = "11111111-1111-4111-8111-111111111111"
    new = "22222222-2222-4222-8222-222222222222"
    ps.set_person("a", old)
    ps.set_person("b", old)
    ps.set_person("c", new)
    assert ps.rename(old, new) == 2
    assert ps.person("a") == new and ps.person("b") == new and ps.person("c") == new
    assert ps.rename(new, new) == 0


def test_person_id_switch_moves_the_driver_lock():
    from backend.web.core import ownership as own

    own._reset_for_tests()
    old = "11111111-1111-4111-8111-111111111111"
    new = "22222222-2222-4222-8222-222222222222"
    own.take("t1", old, "laptop", reason="take")
    assert own.driver("t1") == old
    assert own.rename(old, new) >= 1
    assert own.driver("t1") == new
    # The switched person is still the driver: typing is not refused.
    assert own.gate("t1", new, "laptop") is None
    own._reset_for_tests()


def test_wait_for_build_accepts_short_sha_and_branch_refs(monkeypatch):
    from backend import cli_update as cu

    hellos = iter([{"version": "0.7.6", "commit": "abcdef0123456789"}])
    monkeypatch.setattr(cu, "_hello", lambda base: next(hellos, None))
    monkeypatch.setattr(cu, "POLL_S", 0)
    assert cu.wait_for_build("http://x", commit="abcdef0", timeout=1) is not None
    hellos = iter([{"version": "0.7.6", "commit": "zzz"}])
    assert cu.wait_for_build("http://x", version="main", timeout=1) is not None
    hellos = iter([{"version": "0.7.5", "commit": "zzz"}, None, None])
    assert cu.wait_for_build("http://x", version="0.7.6", timeout=0.01) is None


def test_update_rolls_back_when_the_new_build_never_answers(monkeypatch, tmp_path):
    from backend import cli_update as cu
    from backend.web.core import self_update

    ran = []
    monkeypatch.setattr(cu, "_run_installer", lambda argv: ran.append(argv) or 0)
    monkeypatch.setattr(
        self_update, "install_json_path", lambda: tmp_path / "install.json"
    )
    monkeypatch.setattr(
        self_update, "install_command", lambda m, t: ["install", m, t], raising=False
    )
    out = cu._rollback({"method": "uv-tool", "ref": "v0.7.5", "sha": "deadbeef"})
    assert "rolled the engine back to v0.7.5" in out
    assert ran == [["install", "uv-tool", "deadbeef"]]
    assert cu._rollback({}) == ""
    assert cu._rollback({"method": "editable", "ref": "x"}) == ""


def test_asana_get_all_follows_pages():
    import asyncio

    from backend.ticket_ingestion.providers import asana as mod

    pages = {
        "": {"data": [{"gid": "1"}], "next_page": {"offset": "p2"}},
        "p2": {"data": [{"gid": "2"}], "next_page": None},
    }

    class _Resp:
        def __init__(self, payload):
            self.status = 200
            self._p = payload

        async def json(self):
            return self._p

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def get(self, url, params=None, headers=None):
            return _Resp(pages[(params or {}).get("offset", "")])

    prov = mod.AsanaProvider.__new__(mod.AsanaProvider)
    prov._headers = lambda: {}
    rows = asyncio.run(prov._get_all(_Session(), "/tasks", {"limit": "50"}))
    assert [r["gid"] for r in rows] == ["1", "2"]


def test_branches_route_runs_git_only_under_known_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_recent_repo_paths", lambda: [str(tmp_path / "known")])
    monkeypatch.setattr(server.ENGINE, "instances", {})
    assert server._branches_path_allowed(str(tmp_path / "known" / "sub")) is True
    assert server._branches_path_allowed("/srv/elsewhere/repo") is False


def test_override_auto_fields_stay_clearable():
    from backend.ticket_ingestion.providers import provider_meta

    gh = {f["key"]: f for f in provider_meta("github_issues")["fields"]}
    assert gh["project"].get("override") is True
    asana = {f["key"]: f for f in provider_meta("asana")["fields"]}
    assert not asana["member_id"].get("override")


def test_a_model_reaches_the_cli_without_an_auth_profile(monkeypatch):
    from backend.providers import auth_profiles as ap

    monkeypatch.setattr(ap, "get_profile", lambda pid: None)
    assert ap.launch_overlay("claude", "", "claude-opus-5-5") == (
        {"ANTHROPIC_MODEL": "claude-opus-5-5"},
        (),
    )
    assert ap.launch_overlay("codex", "", "o5") == ({}, ("-m", "o5"))
    assert ap.launch_overlay("aider", "", "x") == ({}, ("--model", "x"))
    # no model, no profile: byte-identical to before
    assert ap.launch_overlay("claude", "", "") == ({}, ())
    assert ap.model_route("claude") and not ap.model_route("cline")


# The real line `netstat -anv -p tcp` printed on the Mac mini for the shared
# link's proxy connection (io.tailscale.ipn, a root process).
_MAC_NETSTAT = (
    "Active Internet connections (including servers)\n"
    "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)\n"
    "tcp4       0      0  127.0.0.1.63120        127.0.0.1.8765         ESTABLISHED"
    "         1040          236  408000  146988 io.tailscale.ipn:78019  00182 00000008\n"
    "tcp4       0      0  127.0.0.1.63118        127.0.0.1.8765         ESTABLISHED"
    "        11086          665  402944  146988  Electron Helper:23341  00102 00000008\n"
    "tcp4       0      0  127.0.0.1.63042        127.0.0.1.8765         CLOSE_WAIT"
    "           674          660  408192  146988  Electron Helper:23341  00122 00000008\n"
)


def _fake_mac(monkeypatch, uids):
    import subprocess as sp

    from backend.web.core import tailnet_trust as tt

    class _R:
        def __init__(self, out):
            self.stdout = out

    def run(argv, **kw):
        if argv[0] == "netstat":
            return _R(_MAC_NETSTAT)
        if argv[0] == "ps":
            return _R("  %d\n" % uids[int(argv[-1])])
        raise AssertionError(argv)

    monkeypatch.setattr(tt.subprocess, "run", run)
    monkeypatch.setattr(tt.sys, "platform", "darwin")
    monkeypatch.setattr(tt, "_DARWIN_SEEN", {})
    return tt


def test_macos_vouches_for_tailscales_root_proxy(monkeypatch):
    tt = _fake_mac(monkeypatch, {78019: 0, 23341: 4242})
    assert tt._darwin_socket_uid(63120, 8765) == 0
    assert tt._loopback_peer_vouched(63120, 8765) is True
    assert tt.shared_link_trust_supported() is True


def test_macos_refuses_another_users_socket_and_dead_sockets(monkeypatch):
    tt = _fake_mac(monkeypatch, {78019: 0, 23341: 4242})
    monkeypatch.setattr(tt.os, "getuid", lambda: 501)
    # another local account (uid 4242) forging X-Forwarded-For: refused
    assert tt._loopback_peer_vouched(63118, 8765) is False
    # a socket that is no longer ESTABLISHED is no answer at all
    assert tt._darwin_socket_uid(63042, 8765) is None
    # an unknown port: nothing found
    assert tt._darwin_socket_uid(1, 8765) is None
