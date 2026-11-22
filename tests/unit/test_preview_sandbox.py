"""The UI preview's isolation, piece by piece (no server booted here; the
end-to-end proof is tests/integration/test_preview_isolation.py).

* the ``tmux`` wrapper pins every call to the preview socket and drops
  ``$TMUX`` (which otherwise beats ``TMUX_TMPDIR`` and reaches the real
  server);
* the server env is an allowlist: the caller's secrets, config dirs, ``PORT``
  and ``MINDFLOCK_*`` overrides never reach it, and every path in it is
  inside the scratch dir;
* reserved ports and a scratch dir inside the real home are refused;
* the stub agent answers out-of-band CLI calls at once;
* the scenario tables are self-consistent and the surface list matches
  Settings → Appearance.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from mfpreview import sandbox, scenarios, shots, stub_agent  # noqa: E402
from mfpreview.sandbox import PreviewError  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("tmux") is None,
    reason="the preview runs on Linux with tmux",
)


@pytest.fixture
def outside_home():
    """A temp dir that is not inside any home the test process sees."""
    d = tempfile.mkdtemp(prefix="mfpv-test-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# the tmux wrapper
# --------------------------------------------------------------------------- #
FAKE_TMUX = """#!/bin/sh
printf 'TMUX=%s\\n' "${TMUX-<unset>}"
for a in "$@"; do printf '%s\\n' "$a"; done
"""


@pytest.fixture
def wrapper(outside_home):
    fake = outside_home / "real-tmux"
    fake.write_text(FAKE_TMUX)
    fake.chmod(0o755)
    sock = outside_home / "sock"
    w = outside_home / "tmux"
    w.write_text(
        sandbox.TMUX_WRAPPER.replace("{real}", str(fake)).replace("{sock}", str(sock))
    )
    w.chmod(0o755)

    def run(*args, tmux_env="/tmp/tmux-1000/default,1,0"):
        env = dict(os.environ, TMUX=tmux_env)
        out = subprocess.run(
            [str(w), *args], env=env, capture_output=True, text=True, check=True
        )
        lines = out.stdout.splitlines()
        return lines[0], lines[1:]

    return run, str(sock)


@pytest.mark.parametrize(
    "args, rest",
    [
        (["ls"], ["ls"]),
        (["-L", "real", "kill-server"], ["kill-server"]),
        (["-S", "/tmp/tmux-1000/default", "kill-server"], ["kill-server"]),
        (["-S/tmp/x", "-Lfoo", "ls"], ["ls"]),
        (
            ["-u", "-S", "/x", "new-session", "-d", "-s", "a"],
            ["-u", "new-session", "-d", "-s", "a"],
        ),
        (["-f", "/dev/null", "-L", "x", "ls"], ["-f", "/dev/null", "ls"]),
        # after the command, -S is capture-pane's start line: kept verbatim
        (
            ["capture-pane", "-p", "-S", "-30", "-t", "x"],
            ["capture-pane", "-p", "-S", "-30", "-t", "x"],
        ),
        (
            ["display-message", "-p", "-t", "%1", "#S"],
            ["display-message", "-p", "-t", "%1", "#S"],
        ),
        (["send-keys", "-t", "x", "a b", ""], ["send-keys", "-t", "x", "a b", ""]),
    ],
)
def test_tmux_wrapper_pins_the_preview_socket(wrapper, args, rest):
    run, sock = wrapper
    tmux_var, argv = run(*args)
    assert tmux_var == "TMUX=<unset>"
    assert argv[:2] == ["-S", sock]
    assert argv[2:] == rest


def test_tmux_wrapper_kill_server_never_reaches_the_default_socket(wrapper):
    run, sock = wrapper
    _, argv = run("kill-server", tmux_env="/tmp/tmux-1000/default,6409,23")
    assert argv == ["-S", sock, "kill-server"]


# --------------------------------------------------------------------------- #
# the environment
# --------------------------------------------------------------------------- #
LEAKY = {
    "TMUX": "/tmp/tmux-1000/default,1,0",
    "TMUX_PANE": "%3",
    "CLAUDE_CONFIG_DIR": "/home/someone/.claude",
    "CODEX_HOME": "/home/someone/.codex",
    "GH_TOKEN": "ghp_secret",
    "GITHUB_TOKEN": "ghp_secret",
    "ANTHROPIC_API_KEY": "sk-secret",  # pragma: allowlist secret
    "GH_CONFIG_DIR": "/home/someone/.config/gh",
    "CS_WEB_MODE": "tailscale",
    "PORT": "9000",
    "UVICORN_PORT": "9000",
    "MINDFLOCK_SETTINGS_FILE": "/home/someone/.mindflock/settings.json",
    "MINDFLOCK_REPO_ROOT": "/home/someone/MindFlock/app",
    "MINDFLOCK_BIND_ALL": "1",
    "XDG_CONFIG_HOME": "/home/someone/.config",
    "LANG": "C.UTF-8",
}


@pytest.fixture
def laid_out(outside_home, monkeypatch):
    for k, v in LEAKY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(
        "PATH", "/home/someone/.local/bin:/mnt/c/Windows:" + os.environ["PATH"]
    )
    sb = sandbox.Sandbox(scratch_parent=str(outside_home), run_dir=outside_home / "run")
    sb.port = sandbox.pick_port()
    sb.lay_out()
    env = sb.build_env()
    try:
        yield sb, env
    finally:
        shutil.rmtree(sb.scratch, ignore_errors=True)


def test_env_is_an_allowlist(laid_out):
    sb, env = laid_out
    for key in LEAKY:
        if key in ("LANG",):
            assert env[key] == LEAKY[key]  # locale passes through
        elif key == "PORT":
            assert env["PORT"] == str(sb.port)
        elif key == "CS_WEB_MODE":
            assert env[key] == "local"
        elif key in env:
            assert env[key] != LEAKY[key], key
    for key in (
        "TMUX",
        "TMUX_PANE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "GH_CONFIG_DIR",
        "UVICORN_PORT",
        "MINDFLOCK_BIND_ALL",
    ):
        assert key not in env, key
    for k, v in env.items():
        assert "/home/someone" not in v, (k, v)
    sandbox.assert_isolated(env, sb.scratch, sb.port)  # does not raise


def test_every_path_in_the_env_is_inside_the_scratch_dir(laid_out):
    sb, env = laid_out
    for key in sandbox.PATH_KEYS:
        assert sandbox._inside(env[key], str(sb.scratch)), key
    path = env["PATH"].split(os.pathsep)
    assert path[0] == str(sb.bin_dir)
    assert set(path[1:]) <= set(sandbox.SYSTEM_PATH)


def test_the_safety_switches_are_on(laid_out):
    _, env = laid_out
    for key, want in {
        "MINDFLOCK_HOME_GUARD": "1",
        "MINDFLOCK_AUTH": "0",
        "CS_CURSOR_AUTOADOPT": "0",
        "MINDFLOCK_DISABLE_AGENTS_JSON": "1",
        "MINDFLOCK_AGENT_SCOPE": "0",
        "MINDFLOCK_NO_PATH_ENRICH": "1",
        "MINDFLOCK_REVIVE_ON_START": "0",
    }.items():
        assert env[key] == want, key


def test_settings_turn_off_the_update_check_and_the_pipeline(laid_out):
    sb, _ = laid_out
    s = json.loads(sb.settings_path.read_text())
    assert s["updates"]["mode"] == "off"
    assert s["general"]["onboarded"] is True
    assert s["general"]["ingestion_autostart"] is False
    assert s["ui"]["cursor_autoadopt"] is False


def test_machine_global_tools_are_failing_stubs(laid_out):
    sb, env = laid_out
    for name in ("tailscale", "cloudflared", "gh", "systemctl", "systemd-run"):
        found = shutil.which(name, path=env["PATH"])
        assert found == str(sb.bin_dir / name), name
        r = subprocess.run([found, "status"], capture_output=True, text=True, env=env)
        assert r.returncode == 1 and "preview sandbox" in r.stderr
    assert shutil.which("tmux", path=env["PATH"]) == str(sb.bin_dir / "tmux")
    assert shutil.which("claude", path=env["PATH"]) == str(sb.bin_dir / "claude")


@pytest.mark.parametrize(
    "mutate, words",
    [
        (
            lambda env, sb: env.update(HOME="/home/someone"),
            "HOME=/home/someone is outside",
        ),
        (
            lambda env, sb: env.update(TMUX="/tmp/tmux-1000/default,1,0"),
            "TMUX must not",
        ),
        (
            lambda env, sb: env.update(PATH=env["PATH"] + ":/home/someone/bin"),
            "PATH entry",
        ),
        (
            lambda env, sb: env.pop("MINDFLOCK_SETTINGS_FILE"),
            "MINDFLOCK_SETTINGS_FILE is not set",
        ),
        (lambda env, sb: env.update(MINDFLOCK_HOME_GUARD="0"), "HOME_GUARD"),
    ],
)
def test_assert_isolated_refuses_a_leak(laid_out, mutate, words):
    sb, env = laid_out
    env = dict(env)
    mutate(env, sb)
    with pytest.raises(PreviewError, match=re.escape(words)):
        sandbox.assert_isolated(env, sb.scratch, sb.port)


@pytest.mark.parametrize("port", sorted(sandbox.RESERVED_PORTS))
def test_reserved_ports_are_refused(laid_out, port):
    sb, env = laid_out
    with pytest.raises(PreviewError, match="reserved"):
        sandbox.pick_port(port)
    env = dict(env, PORT=str(port))
    with pytest.raises(PreviewError, match="reserved"):
        sandbox.assert_isolated(env, sb.scratch, port)


def test_a_free_port_is_never_reserved():
    for _ in range(20):
        assert sandbox.pick_port() not in sandbox.RESERVED_PORTS


def test_a_scratch_dir_inside_the_home_is_refused(outside_home, monkeypatch):
    home = outside_home / "fakehome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(PreviewError, match="inside the real home"):
        sandbox.Sandbox(
            scratch_parent=str(home / "scratch"), run_dir=outside_home / "run"
        )


def test_one_preview_at_a_time(outside_home):
    run = outside_home / "run"
    a = sandbox.Sandbox(scratch_parent=str(outside_home), run_dir=run)
    a.acquire()
    try:
        assert sandbox.lock_held(run)
        b = sandbox.Sandbox(scratch_parent=str(outside_home), run_dir=run)
        with pytest.raises(PreviewError, match="already running"):
            b.acquire()
    finally:
        a.release()
    assert not sandbox.lock_held(run)


def test_a_crashed_previews_leftovers_are_cleaned_on_the_next_start(outside_home):
    run = outside_home / "run"
    run.mkdir()
    left = outside_home / "mfpv-crashed"
    (left / "home").mkdir(parents=True)
    (run / "state.json").write_text(
        json.dumps(
            {
                "id": "deadbeef00",
                "scratch": str(left),
                "tmux_socket": str(left / "tmux" / "sock"),
                "server_pid": None,
            }
        )
    )
    sb = sandbox.Sandbox(scratch_parent=str(outside_home), run_dir=run)
    sb.acquire()
    try:
        assert not left.exists()
        assert not (run / "state.json").exists()
    finally:
        sb.release()


# --------------------------------------------------------------------------- #
# home guard switch
# --------------------------------------------------------------------------- #
def test_home_guard_switch_turns_the_guard_on_outside_pytest(monkeypatch):
    from backend.config import home_guard

    monkeypatch.setattr(home_guard, "under_pytest", lambda: False)
    monkeypatch.delenv("MINDFLOCK_HOME_GUARD", raising=False)
    real = os.path.join(home_guard.real_home(), ".mindflock", "state.json")
    assert not home_guard.active()
    assert home_guard.guard(real) == real
    monkeypatch.setenv("MINDFLOCK_HOME_GUARD", "1")
    assert home_guard.active()
    with pytest.raises(home_guard.RealHomeStoreError):
        home_guard.guard(real)


# --------------------------------------------------------------------------- #
# the stub agent
# --------------------------------------------------------------------------- #
@pytest.fixture
def stub(outside_home):
    path = outside_home / "claude"
    path.write_text(
        stub_agent.render(
            sys.executable,
            str(outside_home / "roles.json"),
            str(outside_home / "markers"),
            "/bin/false",
            "/dev/null",
        )
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    "args, code, says",
    [
        (["--version"], 0, "preview stub"),
        (["agents", "--json"], 1, ""),
        (["-p", "describe this"], 0, "no model"),
        (["auth", "status"], 0, ""),
    ],
)
def test_stub_answers_out_of_band_calls_at_once(stub, args, code, says):
    r = subprocess.run([str(stub), *args], capture_output=True, text=True, timeout=10)
    assert r.returncode == code
    assert says in r.stdout


def test_stub_shebang_names_the_interpreter_directly(stub):
    # Not /usr/bin/env: the process must keep the name "claude".
    assert stub.read_text().splitlines()[0] == "#!" + sys.executable


# --------------------------------------------------------------------------- #
# scenarios + shots tables
# --------------------------------------------------------------------------- #
def test_scenarios_are_self_consistent():
    for name, sc in scenarios.SCENARIOS.items():
        for title in sc["sessions"]:
            assert title in scenarios.SESSIONS, (name, title)
            parent = scenarios.SESSIONS[title].get("parent")
            if parent:
                assert parent in sc["sessions"], (name, title)
        for key in sc["routes"]:
            sess = scenarios.ROUTES[key]["session"]
            assert sess is None or sess in sc["sessions"], (name, key)
    for spec in scenarios.SESSIONS.values():
        assert spec["repo"] in scenarios.REPOS
        assert spec["role"]["screen"] in ("working", "idle", "ask", "crash")
    with pytest.raises(PreviewError, match="unknown scenario"):
        scenarios.get("nope")


def test_surfaces_match_settings_appearance():
    src = (ROOT / "frontend/src/components/settings/screens/Appearance.tsx").read_text()
    block = re.split(r"const SURFACES[^=]*= \[", src, maxsplit=1)[1].split("];", 1)[0]
    keys = tuple(re.findall(r'^\s*\["([^"]*)"', block, re.M))
    assert keys == shots.SURFACES


@pytest.mark.parametrize(
    "view, path, hash_",
    [
        ("sky", "/", "#sky"),
        ("sky/api-auth", "/", "#sky/api-auth"),
        ("grid", "/", ""),
        ("settings", "/", "#settings/general"),
        ("settings/appearance", "/", "#settings/appearance"),
        ("phone", "/m", ""),
        ("#intake/prs", "/", "#intake/prs"),
    ],
)
def test_views_resolve(view, path, hash_):
    got = shots.resolve_view(view)
    assert (got["path"], got["hash"]) == (path, hash_)


def test_bad_views_and_themes_are_refused():
    with pytest.raises(PreviewError):
        shots.resolve_view("nowhere")
    with pytest.raises(PreviewError):
        shots.resolve_view("settings/nope")
    with pytest.raises(PreviewError):
        shots.expand("sepia", "")
    with pytest.raises(PreviewError):
        shots.expand("dark", "pigeon")
    assert len(shots.expand("all", "all")) == 2 * len(shots.SURFACES)
    assert shots.out_name(Path("a/s.png"), "dark", "", True) == Path(
        "a/s-dark-default.png"
    )
