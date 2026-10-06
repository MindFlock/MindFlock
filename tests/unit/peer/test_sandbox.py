"""sandbox.py / sandbox_exec.py: argv shape, env whitelist, share and
runtime validation, credential seeding. The real-bwrap escape probes live in
test_sandbox_escape.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from backend.peer import paths, sandbox, sandbox_exec
from backend.peer.sandbox import SandboxError

SHARE_ID = "0123456789abcdef0123456789abcdef"
SECRETS = {
    "MINDFLOCK_AUTH_TOKEN": "SECRET-mf-auth-0001",
    "SSH_AUTH_SOCK": "/tmp/SECRET-ssh-agent.sock",
    "TMUX": "/tmp/tmux-1000/SECRET-default,1,0",
    "TMUX_PANE": "%SECRET-pane",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/SECRET-bus",
    "XDG_RUNTIME_DIR": "/run/user/SECRET-1000",
    "GH_TOKEN": "SECRET-gh-token",
    "GITHUB_TOKEN": "SECRET-github-token",
    "AWS_SECRET_ACCESS_KEY": "SECRET-aws-key",
    "OPENAI_API_KEY": "SECRET-openai-for-claude",
}

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t.invalid",
}


def _git(*args, cwd=None):
    env = {**os.environ, **_GIT_ENV}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def make_share(tmp_path, share_id=SHARE_ID) -> dict:
    """A share laid out like share.create_share makes it."""
    src = tmp_path / "srcrepo"
    src.mkdir(exist_ok=True)
    (src / "README.md").write_text("hello\n")
    _git("init", "-q", "-b", "main", str(src))
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)
    sp = paths.share_paths(share_id)
    for key in ("root", "home", "run"):
        paths.ensure_dir(sp[key])
    _git(
        "clone",
        "-q",
        "--no-local",
        "--depth",
        "1",
        "--single-branch",
        f"--separate-git-dir={sp['gitdir']}",
        f"file://{src}",
        sp["work"],
    )
    os.chmod(sp["work"], 0o700)
    with open(os.path.join(sp["work"], ".git"), "w") as fh:
        fh.write(f"gitdir: {sp['gitdir']}\n")
    return sp


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HOME with fake credentials, a fake agent on PATH, and the
    peer root under tmp_path. Never touches the real user's credentials."""
    ok, bwrap = sandbox.available()
    if ok:
        monkeypatch.setenv("MINDFLOCK_BWRAP", bwrap)
    else:
        # Unit tests below don't run bwrap; point at any executable.
        monkeypatch.setenv("MINDFLOCK_BWRAP", "/bin/true")
    home = tmp_path / "userhome"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("CANARY-ssh-key")
    (home / ".claude" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "FAKE-cred"}}'
    )
    (home / ".claude" / "history.jsonl").write_text("CANARY-history")
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "fake@example.invalid",
                    "accountUuid": "u-1",
                },
                "userID": "fake-user-id",
                "projects": {
                    "/secret/project": {"history": ["CANARY-project-history"]}
                },
                "primaryApiKey": "CANARY-primary-api-key",
                "mcpServers": {"x": {"env": {"TOKEN": "CANARY-mcp-token"}}},
            }
        )
    )
    (home / ".codex" / "auth.json").write_text('{"tokens": "FAKE-codex"}')
    (home / ".codex" / "history.jsonl").write_text("CANARY-codex-history")
    agent = tmp_path / "agentdist" / "bin"
    agent.mkdir(parents=True)
    claude = agent / "claude"
    claude.write_text('#!/bin/sh\necho fake-claude "$@"\n')
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{agent}:/usr/bin:/bin")
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    for key in (
        "CLAUDE_CONFIG_DIR",
        "MINDFLOCK_CLAUDE_JSON",
        "CODEX_HOME",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, val in SECRETS.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LC_CTYPE", "C.UTF-8")
    paths.ensure_dir(str(tmp_path / "peer"))
    paths.ensure_dir(paths.shares_dir())
    return {"home": home, "agent": agent, "tmp": tmp_path}


@pytest.fixture
def share(env, tmp_path):
    return make_share(tmp_path)


def _setenvs(argv):
    out = {}
    for i, a in enumerate(argv):
        if a == "--setenv":
            out[argv[i + 1]] = argv[i + 2]
    return out


def _opt_index(argv, *seq):
    for i in range(len(argv) - len(seq) + 1):
        if tuple(argv[i : i + len(seq)]) == seq:
            return i
    raise AssertionError(f"{seq} not in argv")


def _binds(argv):
    """(kind, src, dest) for every mount option before ``--``."""
    out = []
    end = argv.index("--")
    i = 0
    while i < end:
        a = argv[i]
        if a in (
            "--bind",
            "--ro-bind",
            "--dev-bind",
            "--bind-try",
            "--ro-bind-try",
            "--symlink",
        ):
            out.append((a, argv[i + 1], argv[i + 2]))
            i += 3
            continue
        i += 1
    return out


# --------------------------------------------------------------------------
# availability


def test_available_reports_reason_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert sandbox.available() == (False, "peer sandbox needs Linux + bubblewrap")


def test_available_missing_bwrap(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_BWRAP", str(tmp_path / "nope"))
    ok, reason = sandbox.available()
    assert not ok and "not found" in reason
    monkeypatch.setenv("MINDFLOCK_BWRAP", "relative/bwrap")
    assert sandbox.find_bwrap() is None


def test_available_failing_selftest(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_BWRAP", "/bin/false")
    ok, reason = sandbox.available()
    assert not ok and "self-test failed" in reason


def test_available_real():
    ok, reason = sandbox.available()
    if not ok:
        pytest.skip(f"bubblewrap unavailable here: {reason}")
    assert os.path.isabs(reason)


# --------------------------------------------------------------------------
# argv shape


def test_argv_core_flags_and_order(share, monkeypatch):
    argv = sandbox.build_argv(share, ["claude", "--x"], "claude", {})
    assert argv[0] == os.environ["MINDFLOCK_BWRAP"]
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["claude", "--x"]
    for flag in ("--die-with-parent", "--unshare-all", "--clearenv"):
        assert flag in argv[:sep]
    _opt_index(argv, "--cap-drop", "ALL")
    assert "--share-net" not in argv
    assert _opt_index(argv, "--chdir", share["work"]) < sep

    work_rw = _opt_index(argv, "--bind", share["work"], share["work"])
    gitfile = os.path.join(share["work"], ".git")
    git_ro = _opt_index(argv, "--ro-bind", gitfile, gitfile)
    gitdir_ro = _opt_index(argv, "--ro-bind", share["gitdir"], share["gitdir"])
    assert work_rw < git_ro and work_rw < gitdir_ro
    home_rw = _opt_index(argv, "--bind", share["home"], share["home"])
    run_ro = _opt_index(argv, "--ro-bind", share["run"], share["run"])
    # Every tmpfs precedes every bind it could otherwise cover.
    tmpfs = [i for i, a in enumerate(argv[:sep]) if a == "--tmpfs"]
    assert {argv[i + 1] for i in tmpfs} >= {"/tmp", "/home", "/run"}
    first_bind_outside_usr = min(i for i, a in enumerate(argv) if a == "--bind")
    assert max(tmpfs) < first_bind_outside_usr
    assert max(work_rw, home_rw, run_ro, git_ro, gitdir_ro) < sep
    # clearenv before every setenv
    clr = argv.index("--clearenv")
    assert all(i > clr for i, a in enumerate(argv) if a == "--setenv")


def test_only_expected_paths_are_mounted(share, env):
    argv = sandbox.build_argv(share, ["true"], "claude", {})
    real_home = os.path.realpath(os.path.expanduser("~"))
    allowed_srcs = {
        share["work"],
        share["home"],
        share["run"],
        share["gitdir"],
        os.path.join(share["work"], ".git"),
    }
    for kind, src, dest in _binds(argv):
        if kind == "--symlink":
            continue
        assert src == dest, (src, dest)
        if src in allowed_srcs:
            continue
        if src == "/usr" or src.startswith("/etc/"):
            continue
        # Runtime binds: never a home dir, the tmp root, the peer root, or "/".
        assert src not in (
            "/",
            str(env["home"]),
            real_home,
            str(env["tmp"]),
            paths.peer_root(),
        )
        assert not str(env["home"]).startswith(src + "/")
        assert not paths.peer_root().startswith(src + "/")
        assert not src.startswith(paths.peer_root() + "/")
    srcs = {s for k, s, _ in _binds(argv) if k != "--symlink"}
    assert str(env["tmp"] / "agentdist") in srcs  # the fake agent's install dir
    backend_dir = os.path.dirname(os.path.dirname(os.path.realpath(sandbox.__file__)))
    assert backend_dir in srcs and os.path.dirname(backend_dir) not in srcs
    for forbidden in (
        "/var",
        "/opt",
        "/mnt",
        "/sys",
        "/root",
        "/srv",
        "/snap",
        "/boot",
        "/etc",
    ):
        assert forbidden not in srcs


def test_env_whitelist_drops_secrets(share, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-passthrough")
    argv = sandbox.build_argv(share, ["true"], "claude", {"MINDFLOCK_MCP_MODE": "peer"})
    joined = "\0".join(argv)
    for key, val in SECRETS.items():
        assert val not in joined, key
    envs = _setenvs(argv)
    assert envs["HOME"] == share["home"]
    assert envs["CLAUDE_CONFIG_DIR"] == os.path.join(share["home"], ".claude")
    assert (
        envs["HTTPS_PROXY"]
        == envs["https_proxy"]
        == f"http://127.0.0.1:{sandbox.DEFAULT_BRIDGE_PORT}"
    )
    assert envs["NO_PROXY"] == "localhost,127.0.0.1"
    assert envs["ANTHROPIC_API_KEY"] == "sk-test-passthrough"
    assert envs["MINDFLOCK_MCP_MODE"] == "peer"
    assert envs["TERM"] == "xterm-256color" and envs["LC_CTYPE"] == "C.UTF-8"
    allowed = {
        "HOME",
        "PATH",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "TERM",
        "COLORTERM",
        "LANG",
        "TZ",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "CLAUDE_CONFIG_DIR",
        "DISABLE_AUTOUPDATER",
        "ANTHROPIC_API_KEY",
        "MINDFLOCK_MCP_MODE",
    }
    assert {k for k in envs if not k.startswith("LC_")} <= allowed
    for d in envs["PATH"].split(":"):
        assert d.startswith(
            ("/usr", "/bin", sandbox.SANDBOX_BIN)
        ) or d == os.path.dirname(sys.executable)


def test_codex_env(share, env, monkeypatch, tmp_path):
    codex = env["agent"] / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    envs = _setenvs(sandbox.build_argv(share, ["codex"], "codex", {}))
    assert envs["CODEX_HOME"] == os.path.join(share["home"], ".codex")
    assert envs["OPENAI_API_KEY"] == "sk-openai"
    assert "CLAUDE_CONFIG_DIR" not in envs and "ANTHROPIC_API_KEY" not in envs


@pytest.mark.parametrize(
    "key",
    [
        "TMUX",
        "TMUX_PANE",
        "SSH_AUTH_SOCK",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "MINDFLOCK_AUTH_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_PROFILE",
        "LD_PRELOAD",
        "HOME",
        "PATH",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "CLAUDE_CONFIG_DIR",
        "BAD KEY",
        "X=Y",
        "",
        "1ABC",
        "WSL_INTEROP",
        "DISPLAY",
    ],
)
def test_explicit_env_cannot_smuggle(share, key):
    with pytest.raises(SandboxError):
        sandbox.build_argv(share, ["true"], "claude", {key: "v"})


def test_explicit_env_rejects_nul(share):
    with pytest.raises(SandboxError):
        sandbox.build_argv(share, ["true"], "claude", {"OK_KEY": "a\0b"})


@pytest.mark.parametrize("inner", [[], ["a\0b"], [1]])
def test_bad_inner_argv(share, inner):
    with pytest.raises(SandboxError):
        sandbox.build_argv(share, inner, "claude", {})


def test_unknown_provider_refused(share):
    for fn in (
        lambda: sandbox.build_argv(share, ["x"], "aider", {}),
        lambda: sandbox.prepare_home(share, "aider"),
        lambda: sandbox.egress_allow("aider"),
    ):
        with pytest.raises(SandboxError, match="provider aider has no sandbox profile"):
            fn()


def test_new_session_and_seccomp(share, monkeypatch):
    monkeypatch.setattr(sandbox, "_tiocsti_disabled", lambda: False)
    argv = sandbox.build_argv(share, ["x"], "claude", {})
    assert "--new-session" in argv and "--seccomp" not in argv
    argv = sandbox.build_argv(share, ["x"], "claude", {}, seccomp_fd=9)
    assert "--new-session" not in argv
    _opt_index(argv, "--seccomp", "9")
    monkeypatch.setattr(sandbox, "_tiocsti_disabled", lambda: True)
    assert "--new-session" not in sandbox.build_argv(share, ["x"], "claude", {})


def test_bridge_port_validated(share):
    for port in (0, 80, 70000, "3128"):
        with pytest.raises(SandboxError):
            sandbox.build_argv(share, ["x"], "claude", {}, bridge_port=port)


def test_seccomp_program():
    for machine in ("x86_64", "aarch64"):
        prog = sandbox.seccomp_program(machine)
        assert prog and len(prog) % 8 == 0
    assert sandbox.seccomp_program("riscv64") is None


# --------------------------------------------------------------------------
# share validation


def test_share_outside_shares_dir_refused(env, tmp_path):
    sp = make_share(tmp_path)
    fake = dict(sp)
    other = tmp_path / "elsewhere" / SHARE_ID
    for k in ("work", "home", "run"):
        (other / k).mkdir(parents=True)
    fake = {k: str(other / k) for k in ("work", "home", "run")}
    fake["root"] = str(other)
    fake["gitdir"] = sp["gitdir"]
    with pytest.raises(SandboxError):
        sandbox.share_dirs(fake)


def test_symlinked_share_dirs_refused(env, tmp_path):
    sp = make_share(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.rename(sp["home"], sp["home"] + ".bak")
    os.symlink(victim, sp["home"])
    with pytest.raises(SandboxError):
        sandbox.build_argv(sp, ["x"], "claude", {})
    os.unlink(sp["home"])
    os.rename(sp["home"] + ".bak", sp["home"])
    sandbox.share_dirs(sp)


@pytest.mark.parametrize("kind", ["symlink", "dir", "missing"])
def test_gitfile_must_be_regular(env, tmp_path, kind):
    sp = make_share(tmp_path)
    gitfile = os.path.join(sp["work"], ".git")
    os.unlink(gitfile)
    if kind == "symlink":
        os.symlink(str(env["home"] / ".ssh"), gitfile)
    elif kind == "dir":
        os.mkdir(gitfile)
    with pytest.raises(SandboxError):
        sandbox.build_argv(sp, ["x"], "claude", {})


def test_bad_share_object(env):
    with pytest.raises(SandboxError):
        sandbox.share_dirs({"root": "/tmp"})


# --------------------------------------------------------------------------
# runtime discovery guards


def test_agent_in_generic_bin_binds_file_only(share, env, monkeypatch):
    bindir = env["home"] / ".local" / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    srcs = {
        s
        for k, s, _ in _binds(sandbox.build_argv(share, ["x"], "claude", {}))
        if k != "--symlink"
    }
    assert str(exe) in srcs
    assert str(bindir) not in srcs and str(env["home"] / ".local") not in srcs


def test_agent_whose_dir_holds_the_peer_root_refused(share, env, monkeypatch, tmp_path):
    exe = tmp_path / "claude"  # tmp_path contains peer/ and userhome/
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(SandboxError, match="refusing to expose"):
        sandbox.build_argv(share, ["x"], "claude", {})


def test_agent_inside_peer_root_refused(share, env, monkeypatch):
    d = os.path.join(paths.peer_root(), "evilbin")
    os.mkdir(d)
    exe = os.path.join(d, "claude")
    with open(exe, "w") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    monkeypatch.setenv("PATH", f"{d}:/usr/bin:/bin")
    with pytest.raises(SandboxError):
        sandbox.build_argv(share, ["x"], "claude", {})


def test_missing_agent_refused(share, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(SandboxError, match="not found"):
        sandbox.build_argv(share, ["x"], "claude", {})


def test_node_package_and_env_interpreter(share, env, monkeypatch, tmp_path):
    pkg = tmp_path / "npm" / "lib" / "node_modules" / "@openai" / "codex"
    (pkg / "bin").mkdir(parents=True)
    js = pkg / "bin" / "codex.js"
    nodedist = tmp_path / "nodedist" / "bin"
    nodedist.mkdir(parents=True)
    node = nodedist / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    js.write_text("#!/usr/bin/env node\n")
    js.chmod(0o755)
    npmbin = tmp_path / "npm" / "bin"
    npmbin.mkdir()
    os.symlink(js, npmbin / "codex")
    monkeypatch.setenv("PATH", f"{npmbin}:{nodedist}:/usr/bin:/bin")
    argv = sandbox.build_argv(share, ["codex"], "codex", {})
    srcs = {s for k, s, _ in _binds(argv) if k != "--symlink"}
    assert str(pkg) in srcs  # package root, not the whole global node_modules
    assert str(tmp_path / "npm" / "lib" / "node_modules") not in srcs
    assert str(tmp_path / "nodedist") in srcs
    links = {d: s for k, s, d in _binds(argv) if k == "--symlink"}
    assert links[f"{sandbox.SANDBOX_BIN}/node"] == str(node)
    assert links[f"{sandbox.SANDBOX_BIN}/codex"] == str(js)


# --------------------------------------------------------------------------
# prepare_home


def _mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_prepare_home_claude_copies_only_login(share, env):
    written = sandbox.prepare_home(share, "claude")
    home = share["home"]
    cred = os.path.join(home, ".claude", ".credentials.json")
    assert cred in written and _mode(cred) == 0o600
    assert (
        open(cred).read() == (env["home"] / ".claude" / ".credentials.json").read_text()
    )
    for p in (
        os.path.join(home, ".claude.json"),
        os.path.join(home, ".claude", ".claude.json"),
    ):
        assert _mode(p) == 0o600
        data = json.load(open(p))
        assert set(data) == {
            "hasCompletedOnboarding",
            "oauthAccount",
            "userID",
            "projects",
        }
        assert data["hasCompletedOnboarding"] is True
        assert data["userID"] == "fake-user-id"
        assert data["projects"] == {share["work"]: {"hasTrustDialogAccepted": True}}
    every = ""
    for dp, _dn, fns in os.walk(home):
        for fn in fns:
            every += open(os.path.join(dp, fn)).read()
    assert "CANARY" not in every
    assert _mode(os.path.join(home, ".claude")) == 0o700


def test_prepare_home_claude_json_override_and_garbage(
    share, env, monkeypatch, tmp_path
):
    alt = tmp_path / "alt.json"
    alt.write_text(
        json.dumps({"userID": "alt-id", "oauthAccount": "not-a-dict", "x": 1})
    )
    monkeypatch.setenv("MINDFLOCK_CLAUDE_JSON", str(alt))
    sandbox.prepare_home(share, "claude")
    data = json.load(open(os.path.join(share["home"], ".claude.json")))
    assert data["userID"] == "alt-id" and "oauthAccount" not in data and "x" not in data
    alt.write_text("{not json")
    sandbox.prepare_home(share, "claude")
    data = json.load(open(os.path.join(share["home"], ".claude.json")))
    assert set(data) == {"hasCompletedOnboarding", "projects"}


def test_prepare_home_claude_config_dir(share, env, monkeypatch, tmp_path):
    cfg = tmp_path / "profile"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text("PROFILE-CRED")
    (cfg / ".claude.json").write_text(json.dumps({"userID": "profile-id"}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    sandbox.prepare_home(share, "claude")
    assert (
        open(os.path.join(share["home"], ".claude", ".credentials.json")).read()
        == "PROFILE-CRED"
    )
    assert (
        json.load(open(os.path.join(share["home"], ".claude.json")))["userID"]
        == "profile-id"
    )


def test_prepare_home_without_credentials(share, env):
    os.unlink(env["home"] / ".claude" / ".credentials.json")
    written = sandbox.prepare_home(share, "claude")
    assert not os.path.exists(
        os.path.join(share["home"], ".claude", ".credentials.json")
    )
    assert os.path.join(share["home"], ".claude.json") in written


def test_prepare_home_codex(share, env):
    written = sandbox.prepare_home(share, "codex")
    auth = os.path.join(share["home"], ".codex", "auth.json")
    assert written == [auth] and _mode(auth) == 0o600
    assert os.listdir(os.path.join(share["home"], ".codex")) == ["auth.json"]


def test_prepare_home_does_not_follow_planted_symlinks(share, env, tmp_path):
    """home/ is writable inside the sandbox; a previous (hostile) session may
    have planted symlinks. prepare_home runs on the host, unsandboxed."""
    home = share["home"]
    victim_file = tmp_path / "victim.txt"
    victim_file.write_text("ORIGINAL")
    victim_dir = tmp_path / "victimdir"
    victim_dir.mkdir()
    os.symlink(victim_file, os.path.join(home, ".claude.json"))
    os.mkdir(os.path.join(home, ".claude"))
    os.symlink(victim_file, os.path.join(home, ".claude", ".credentials.json"))
    os.symlink(victim_file, os.path.join(home, ".claude", ".claude.json"))
    sandbox.prepare_home(share, "claude")
    assert victim_file.read_text() == "ORIGINAL"
    for p in (".claude.json", ".claude/.credentials.json", ".claude/.claude.json"):
        assert stat.S_ISREG(os.lstat(os.path.join(home, p)).st_mode)

    # A symlinked directory component is refused outright.
    import shutil

    shutil.rmtree(os.path.join(home, ".claude"))
    os.symlink(victim_dir, os.path.join(home, ".claude"))
    with pytest.raises(SandboxError):
        sandbox.prepare_home(share, "claude")
    assert os.listdir(victim_dir) == []


def test_safe_write_rejects_traversal(share):
    for rel in ("../x", "a/../../x", "", "/", "."):
        with pytest.raises(SandboxError):
            sandbox.safe_write(share["home"], rel, b"x")


def test_safe_write_leaf_directory(share):
    os.mkdir(os.path.join(share["home"], "d"))
    with pytest.raises(SandboxError):
        sandbox.safe_write(share["home"], "d", b"x")
    assert [n for n in os.listdir(share["home"]) if n.startswith(".d.mf-")] == []


# --------------------------------------------------------------------------
# egress allow-list


def test_egress_allow_defaults_and_extra():
    claude = sandbox.egress_allow("claude")
    assert claude == [
        "api.anthropic.com",
        "console.anthropic.com",
        "platform.claude.com",
        "claude.ai",
        "statsig.anthropic.com",
    ]
    assert sandbox.egress_allow("codex") == [
        "api.openai.com",
        "chatgpt.com",
        "auth.openai.com",
    ]
    extra = [
        "pypi.org",
        ".pythonhosted.org",
        "EXAMPLE.com",
        "*",
        ".",
        ".com",
        "1.2.3.4",
        "localhost",
        "evil.com/x",
        "a..b",
        5,
        None,
        "api.anthropic.com",
        "[::1]",
        "x.y:443",
    ]
    got = sandbox.egress_allow("claude", extra)
    assert got == claude + ["pypi.org", ".pythonhosted.org", "example.com"]


# --------------------------------------------------------------------------
# sandbox_exec


def test_exec_fails_closed_when_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "available", lambda: (False, "no bwrap here"))
    rc = sandbox_exec.main(
        ["--share", SHARE_ID, "--provider", "claude", "--", "claude"]
    )
    assert rc == 78
    assert "no bwrap here" in capsys.readouterr().err


def test_exec_usage_errors(env, capsys):
    assert sandbox_exec.main(["--share", SHARE_ID, "--provider", "claude"]) == 64
    assert (
        sandbox_exec.main(
            ["--share", SHARE_ID, "--provider", "claude", "--env", "NOEQ", "--", "x"]
        )
        == 64
    )


def test_exec_refuses_bad_share_and_provider(env, share, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "available", lambda: (True, "/bin/true"))
    assert (
        sandbox_exec.main(["--share", "../../etc", "--provider", "claude", "--", "x"])
        == 78
    )
    assert (
        sandbox_exec.main(["--share", "f" * 32, "--provider", "claude", "--", "x"])
        == 78
    )
    assert (
        sandbox_exec.main(["--share", SHARE_ID, "--provider", "aider", "--", "x"]) == 78
    )
    rc = sandbox_exec.main(
        ["--share", SHARE_ID, "--provider", "claude", "--env", "TMUX=x", "--", "x"]
    )
    assert rc == 78
    assert "may not enter the sandbox" in capsys.readouterr().err


def test_exec_prepare_moves_options_off_cmdline(share, env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-be-on-cmdline")
    exec_argv, keep = sandbox_exec.prepare(
        SHARE_ID, "claude", 4000, ["claude", "--resume"], {}
    )
    try:
        assert exec_argv[1] == "--args" and int(exec_argv[2]) in keep
        sep = exec_argv.index("--")
        inner = exec_argv[sep + 1 :]
        assert inner[:3] == ["sh", "-c", sandbox_exec._LAUNCH]
        assert inner[-2:] == ["claude", "--resume"]
        assert inner[3:8] == [
            "sh",
            os.path.realpath(sys.executable),
            os.path.join(share["run"], "bridge.py"),
            os.path.join(share["run"], "egress.sock"),
            "4000",
        ]
        assert "sk-must-not-be-on-cmdline" not in "\0".join(exec_argv)
        fd = int(exec_argv[2])
        opts = os.pread(fd, 1 << 20, 0).split(b"\0")
        assert b"sk-must-not-be-on-cmdline" in opts
        assert b"--seccomp" in opts or sandbox.seccomp_program() is None
        bridge = os.path.join(share["run"], "bridge.py")
        assert _mode(bridge) == 0o600
        src = os.path.join(os.path.dirname(sandbox_exec.__file__), "bridge.py")
        assert open(bridge, "rb").read() == open(src, "rb").read()
        # home was seeded
        assert os.path.exists(os.path.join(share["home"], ".claude.json"))
    finally:
        for fd in keep:
            os.close(fd)


def test_inner_argv_never_splices_paths_into_script(share):
    inner = sandbox_exec.inner_argv(share, 3128, ["claude", "'; rm -rf ~ #"])
    assert inner[2] == sandbox_exec._LAUNCH
    assert share["run"] not in inner[2]
    assert inner[-1] == "'; rm -rf ~ #"


def test_close_fds_except():
    code = (
        "import json, os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from backend.peer.sandbox_exec import close_fds_except\n"
        "fds = [os.open('/dev/null', os.O_RDONLY) for _ in range(6)]\n"
        "close_fds_except([fds[2], fds[4]])\n"
        "left = [int(x) for x in os.listdir('/proc/self/fd')]\n"
        "print(json.dumps({'left': left, 'keep': [fds[2], fds[4]]}))\n"
    )
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.realpath(sandbox.__file__)))
    )
    out = subprocess.run(
        [sys.executable, "-c", code, root], capture_output=True, text=True, check=True
    )
    res = json.loads(out.stdout)
    extra = set(res["left"]) - {0, 1, 2} - set(res["keep"])
    assert len(extra) <= 1  # the fd listdir itself had open
    assert set(res["keep"]) <= set(res["left"])
