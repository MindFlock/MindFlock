"""A shared-folder (PeerShare) session's agent launches sandboxed — or not at
all. Every launch site: the engine's first Start, its Resume (incl. a session
loaded from storage), and the web relaunch; plus the shell pane."""

from __future__ import annotations

import os
import shlex

import pytest

from backend.peer import launch as peer_launch
from backend.session import instance as inst_mod
from backend.session.instance import InstanceOptions, new_instance
from backend.session.storage import Status

from ._integration_helpers import (
    SHARE_ID,
    FakeTmux,
    fake_sandbox,
    make_share,
    mk_inst,
    register_token,
)


@pytest.fixture
def share(monkeypatch):
    p = make_share()
    register_token(monkeypatch)
    return p


def _peer_instance(work: str, program: str = "claude"):
    inst = new_instance(
        InstanceOptions(
            title="peer-bob-abab",
            path=work,
            program=program,
            in_place=True,
            peer_share=SHARE_ID,
        )
    )
    inst.ExtraEnv = {"PORT": "4100", "ANTHROPIC_API_KEY": "host-secret"}
    inst._tmux_session = FakeTmux("mindflock_peer-bob-abab")
    return inst


# --------------------------------------------------------------------------- #
# Engine: first Start
# --------------------------------------------------------------------------- #
def test_start_refused_when_sandbox_unavailable(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=False)
    inst = _peer_instance(share["work"])
    with pytest.raises(RuntimeError, match="sandbox"):
        inst.Start(True)
    assert inst._tmux_session.starts == []  # NEVER an unsandboxed agent
    assert not inst.Started()


def test_start_refused_when_sandbox_module_missing(share, monkeypatch):
    # The probe itself failing (module absent / raising) is "no sandbox".
    import types

    mod = types.ModuleType("backend.peer.sandbox")

    def boom():
        raise OSError("bwrap exploded")

    mod.available = boom
    monkeypatch.setitem(__import__("sys").modules, "backend.peer.sandbox", mod)
    monkeypatch.setattr(__import__("backend.peer").peer, "sandbox", mod, raising=False)
    inst = _peer_instance(share["work"])
    with pytest.raises(RuntimeError):
        inst.Start(True)
    assert inst._tmux_session.starts == []


def test_start_refused_without_agent_token(monkeypatch):
    p = make_share()
    monkeypatch.setattr(peer_launch, "_TOKENS", {})
    fake_sandbox(monkeypatch, ok=True)
    inst = _peer_instance(p["work"])
    with pytest.raises(RuntimeError, match="peer service is not running"):
        inst.Start(True)
    assert inst._tmux_session.starts == []


def test_start_refused_for_provider_without_sandbox_profile(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=True)
    inst = _peer_instance(share["work"], program="aider")
    with pytest.raises(RuntimeError, match="claude and codex"):
        inst.Start(True)
    assert inst._tmux_session.starts == []


def test_start_wraps_launch_in_sandbox_exec(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=True)
    inst = _peer_instance(share["work"])
    inst.Start(True)
    assert len(inst._tmux_session.starts) == 1
    work_dir, cmd, extra_env = inst._tmux_session.starts[0]
    assert work_dir == share["work"]
    argv = shlex.split(cmd)
    assert argv[0] == "env" and argv[1].startswith("PYTHONPATH=")
    i = argv.index("-m")
    assert argv[i + 1] == "backend.peer.sandbox_exec"
    assert argv[argv.index("--share") + 1] == SHARE_ID
    assert argv[argv.index("--provider") + 1] == "claude"
    dd = argv.index("--")
    assert argv[dd + 1 : dd + 3] == ["sh", "-c"]
    inner = argv[dd + 3]
    assert "--mcp-config=" + os.path.join(share["run"], "mcp.json") in inner
    assert "--strict-mcp-config" in inner
    # Nothing of the host per-session env rides along (port block, secrets).
    assert extra_env == {}
    assert "host-secret" not in cmd
    assert inst.PeerShare == SHARE_ID


# --------------------------------------------------------------------------- #
# Engine: Resume (incl. loaded from storage, which has no configured command)
# --------------------------------------------------------------------------- #
def _paused_from_storage(work: str):
    inst = mk_inst("peer-bob-abab", work, peer_share=SHARE_ID)
    inst.Status = Status.Paused
    inst._tmux_session = FakeTmux("mindflock_peer-bob-abab")
    inst._tmux_session.launch_command = None  # what storage gives you
    return inst


def test_resume_refused_when_sandbox_unavailable(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=False)
    inst = _paused_from_storage(share["work"])
    with pytest.raises(RuntimeError):
        inst.Resume()
    assert inst._tmux_session.starts == []


def test_resume_relaunches_sandboxed(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=True)
    inst = _paused_from_storage(share["work"])
    inst.Resume()
    ((_, cmd, _env),) = inst._tmux_session.starts
    assert "backend.peer.sandbox_exec" in cmd


def test_resume_rebuilds_a_tampered_launch_command(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=True)
    inst = _paused_from_storage(share["work"])
    inst._tmux_session.launch_command = "claude --dangerously-skip-permissions"
    inst.Resume()
    ((_, cmd, _env),) = inst._tmux_session.starts
    assert "backend.peer.sandbox_exec" in cmd


def test_peer_share_round_trips_through_storage(share):
    inst = mk_inst("peer-bob-abab", share["work"], peer_share=SHARE_ID)
    data = inst.ToInstanceData()
    assert data.to_dict()["peer_share"] == SHARE_ID
    from backend.session.storage import InstanceData

    assert InstanceData.from_dict(data.to_dict()).peer_share == SHARE_ID


def test_ordinary_instance_json_unchanged():
    from backend.session.storage import InstanceData

    assert "peer_share" not in InstanceData(title="x").to_dict()


# --------------------------------------------------------------------------- #
# Engine: no ordinary session on a path under the peer root
# --------------------------------------------------------------------------- #
def test_new_instance_refuses_peer_root_path(share):
    with pytest.raises(RuntimeError, match="peer-link folder"):
        new_instance(InstanceOptions(title="t", path=share["work"], in_place=True))


def test_new_instance_refuses_symlink_into_peer_root(share, tmp_path):
    link = tmp_path / "innocent"
    os.symlink(share["work"], link)
    with pytest.raises(RuntimeError, match="peer-link folder"):
        new_instance(InstanceOptions(title="t", path=str(link), in_place=True))


def test_new_instance_refuses_dotdot_into_peer_root(share, tmp_path):
    (tmp_path / "x").mkdir()
    rel = os.path.relpath(share["work"], tmp_path / "x")
    sneaky = str(tmp_path / "x" / rel)
    assert ".." in sneaky
    with pytest.raises(RuntimeError, match="peer-link folder"):
        new_instance(InstanceOptions(title="t", path=sneaky, in_place=True))


def test_peer_share_instance_must_be_on_its_own_folder(share, tmp_path):
    with pytest.raises(RuntimeError, match="its share's folder"):
        new_instance(
            InstanceOptions(
                title="t", path=str(tmp_path), in_place=True, peer_share=SHARE_ID
            )
        )
    with pytest.raises(RuntimeError, match="in place"):
        new_instance(
            InstanceOptions(title="t", path=share["work"], peer_share=SHARE_ID)
        )


def test_engine_guard_refuses_ordinary_session_in_peer_root_at_start(
    share, monkeypatch
):
    # An ordinary instance that slipped in (e.g. adopted from storage).
    inst = mk_inst("sneaky", share["work"])
    inst.Status = Status.Paused
    inst._tmux_session = FakeTmux("mindflock_sneaky")
    with pytest.raises(RuntimeError, match="shared"):
        inst.Resume()
    assert inst._tmux_session.starts == []


# --------------------------------------------------------------------------- #
# Web relaunch (agent_sessions._ensure_agent_session) + shell pane
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, rc=0):
        self.returncode = rc
        self.stderr = b""


@pytest.fixture
def tmux_rec(monkeypatch):
    from backend.web import server
    from backend.web.core import agent_sessions

    calls = []

    def rec(args, **kw):
        calls.append(list(args))
        return _Proc(1 if "has-session" in args else 0)

    monkeypatch.setattr(server, "_run_capped", rec)
    monkeypatch.setattr(agent_sessions, "_clear_exit_marker", lambda n: None)
    monkeypatch.setattr(agent_sessions, "_read_exit_marker", lambda n: None)
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: None)
    return calls


class _HookSpy:
    name = "claude"

    def __init__(self, real):
        self._real = real
        self.hooks = []

    def __getattr__(self, k):
        return getattr(self._real, k)

    def install_activity_hooks(self, wt, name):
        self.hooks.append(wt)


def test_web_relaunch_refused_without_sandbox(share, monkeypatch, tmux_rec):
    from backend.web.core import agent_sessions

    fake_sandbox(monkeypatch, ok=False)
    inst = mk_inst("peer-bob-abab", share["work"], peer_share=SHARE_ID)
    name, err = agent_sessions._ensure_agent_session(inst, "peer-bob-abab")
    assert err and "not started" in err
    assert not [c for c in tmux_rec if "new-session" in c]


def test_web_relaunch_is_sandboxed_and_installs_no_hooks(share, monkeypatch, tmux_rec):
    from backend import providers
    from backend.web.core import agent_sessions

    fake_sandbox(monkeypatch, ok=True)
    spy = _HookSpy(providers.resolve("claude"))
    real_resolve = providers.resolve
    monkeypatch.setattr(
        agent_sessions.providers,
        "resolve",
        lambda prog: spy if (prog or "claude") == "claude" else real_resolve(prog),
    )
    inst = mk_inst("peer-bob-abab", share["work"], peer_share=SHARE_ID)
    name, err = agent_sessions._ensure_agent_session(inst, "peer-bob-abab")
    assert err is None
    new = [c for c in tmux_rec if "new-session" in c]
    assert len(new) == 1
    assert "backend.peer.sandbox_exec" in new[0][-1]
    assert spy.hooks == []  # nothing installed into the shared folder


def test_web_relaunch_refuses_ordinary_session_in_peer_root(
    share, monkeypatch, tmux_rec
):
    from backend.web.core import agent_sessions

    inst = mk_inst("sneaky", share["work"])
    name, err = agent_sessions._ensure_agent_session(inst, "sneaky")
    assert err and "unsandboxed" in err
    assert not [c for c in tmux_rec if "new-session" in c]


def test_shell_pane_refused_for_peer_session(share, monkeypatch, tmux_rec):
    from backend.web import server
    from backend.web.core import agent_sessions

    inst = mk_inst("peer-bob-abab", share["work"], peer_share=SHARE_ID)
    monkeypatch.setattr(server.ENGINE, "instances", {"peer-bob-abab": inst})
    name, err = agent_sessions._ensure_shell_session("peer-bob-abab", share["work"])
    assert err == agent_sessions.PEER_SHELL_REFUSED
    assert tmux_rec == []  # not even an attach probe


def test_shell_pane_refused_for_any_path_in_peer_root(share, monkeypatch, tmux_rec):
    from backend.web import server
    from backend.web.core import agent_sessions

    monkeypatch.setattr(server.ENGINE, "instances", {})
    name, err = agent_sessions._ensure_shell_session("x", share["root"])
    assert err == agent_sessions.PEER_SHELL_REFUSED
    assert tmux_rec == []


def test_build_command_never_returns_bare_command(share, monkeypatch):
    fake_sandbox(monkeypatch, ok=False)
    with pytest.raises(peer_launch.PeerLaunchError):
        peer_launch.sandbox_command(SHARE_ID, "claude", "claude")
    with pytest.raises(peer_launch.PeerLaunchError):
        peer_launch.sandbox_command("not-hex", "claude", "claude")


def test_inst_mod_exports_guard():
    assert hasattr(inst_mod.Instance, "_peer_guard_launch")
