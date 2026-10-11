"""macOS peer sandbox: the Seatbelt profile (``backend.peer.seatbelt``) and
``sandbox.build_seatbelt``.

The profile tests are pure and run everywhere. ``TestOnMacOS`` runs a real
``sandbox-exec`` (macOS CI) and checks the escapes the Linux sandbox's
red-team tests check, from inside: the user's files, writes outside the
share, the gitfile, other local ports, ``/tmp``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from backend.peer import paths, sandbox, seatbelt

SHARE_ID = "ab" * 16


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    home = tmp_path / "userhome"
    (home / ".claude").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("CANARY-ssh-key")
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "FAKE-access",
                    "refreshToken": "CANARY-refresh",
                    "expiresAt": int((time.time() + 3600) * 1000),
                }
            }
        )
    )
    (home / ".claude.json").write_text(json.dumps({"userID": "u"}))
    agent = tmp_path / "agentdist" / "bin"
    agent.mkdir(parents=True)
    claude = agent / "claude"
    claude.write_text('#!/bin/sh\necho fake-claude "$@"\n')
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{agent}:/usr/bin:/bin")
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    monkeypatch.setenv("GH_TOKEN", "SECRET-gh-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/SECRET-ssh-agent.sock")
    for key in (
        "CLAUDE_CONFIG_DIR",
        "MINDFLOCK_CLAUDE_JSON",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    paths.ensure_dir(str(tmp_path / "peer"))
    paths.ensure_dir(paths.shares_dir())
    return {"home": home, "tmp": tmp_path}


@pytest.fixture
def share(env):
    src = env["tmp"] / "srcrepo"
    src.mkdir()
    (src / "README.md").write_text("hello\n")
    _git("init", "-q", "-b", "main", str(src))
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)
    sp = paths.share_paths(SHARE_ID)
    for key in ("root", "home", "run"):
        paths.ensure_dir(sp[key])
    _git(
        "clone",
        "-q",
        "--no-local",
        "--depth",
        "1",
        f"--separate-git-dir={sp['gitdir']}",
        f"file://{src}",
        sp["work"],
    )
    os.chmod(sp["work"], 0o700)
    with open(os.path.join(sp["work"], ".git"), "w") as fh:
        fh.write(f"gitdir: {sp['gitdir']}\n")
    return sp


def _dirs(tmp):
    return {
        "root": f"{tmp}/s",
        "work": f"{tmp}/s/work",
        "gitfile": f"{tmp}/s/work/.git",
        "gitdir": f"{tmp}/s/repo.git",
        "home": f"{tmp}/s/home",
        "run": f"{tmp}/s/run",
    }


# --------------------------------------------------------------------------
# the profile, as text


def test_profile_denies_by_default_and_takes_back_procargs():
    prof = seatbelt.build_profile(dirs=_dirs("/x"), runtime=[], bridge_port=40001)
    assert prof.startswith("(version 1)\n(deny default)\n")
    assert '(sysctl-name-prefix "kern.procargs")' in prof
    assert "(deny process-info*)" in prof
    assert "(allow process-info* (target same-sandbox))" in prof


def test_profile_writes_only_work_and_home_never_the_gitfile():
    prof = seatbelt.build_profile(dirs=_dirs("/x"), runtime=[], bridge_port=40001)
    writes = [ln for ln in prof.splitlines() if ln.startswith("(allow file-write*")]
    assert len(writes) == 1
    w = writes[0]
    assert '(subpath "/x/s/work")' in w and '(subpath "/x/s/home")' in w
    assert '(require-not (literal "/x/s/work/.git"))' in w
    assert '(require-not (literal "/x/s/work"))' in w
    assert "repo.git" not in w and "/x/s/run" not in w


def test_profile_network_is_the_bridge_port_and_run_sockets_only():
    prof = seatbelt.build_profile(dirs=_dirs("/x"), runtime=[], bridge_port=40001)
    net = [ln for ln in prof.splitlines() if "network" in ln]
    assert all("localhost:40001" in ln or "unix-socket" in ln for ln in net), net
    assert '(path-literal "/x/s/run/egress.sock")' in prof
    assert '(path-literal "/x/s/run/agent.sock")' in prof
    assert "(allow network*" not in prof and "(allow network-outbound)" not in prof


def test_profile_runtime_reads_are_subpath_or_literal(tmp_path):
    f = tmp_path / "agent"
    f.write_text("x")
    prof = seatbelt.build_profile(
        dirs=_dirs("/x"), runtime=[str(tmp_path), str(f)], bridge_port=40001
    )
    assert f'(subpath "{tmp_path}")' in prof
    assert f'(literal "{f}")' in prof
    assert f'(path-ancestors "{f}")' in prof


@pytest.mark.parametrize(
    "bad", ['/x/a"b', "/x/a\\b", "/x/a\nb", "/x/a\x00b", "/x/\x7f"]
)
def test_profile_refuses_paths_that_would_need_escaping(bad):
    with pytest.raises(seatbelt.SeatbeltError):
        seatbelt.build_profile(dirs=_dirs("/x"), runtime=[bad], bridge_port=40001)


def test_profile_tty_is_validated():
    prof = seatbelt.build_profile(
        dirs=_dirs("/x"), runtime=[], bridge_port=40001, tty="/dev/ttys004"
    )
    assert '(literal "/dev/ttys004")' in prof
    for bad in ("/dev/disk0", "/Users/x/.ssh/id_rsa", "/dev/ttys004/../disk0"):
        with pytest.raises(seatbelt.SeatbeltError):
            seatbelt.build_profile(
                dirs=_dirs("/x"), runtime=[], bridge_port=40001, tty=bad
            )


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, "3128"])
def test_profile_refuses_bad_ports(port):
    with pytest.raises(seatbelt.SeatbeltError):
        seatbelt.build_profile(dirs=_dirs("/x"), runtime=[], bridge_port=port)


def test_homebrew_paths(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    got = seatbelt.homebrew_paths(["/opt/homebrew/Cellar/node/22/bin/node", "/x/y"])
    assert got == ["/opt/homebrew/Cellar", "/opt/homebrew/opt", "/opt/homebrew/lib"]
    assert seatbelt.homebrew_paths(["/Users/me/.local/bin/claude"]) == []


# --------------------------------------------------------------------------
# build_seatbelt (the macOS argv + env), on any OS


def test_build_seatbelt_argv_env_and_links(share, env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    argv, envs, links = sandbox.build_seatbelt(
        share,
        ["sh", "-c", "true"],
        "claude",
        {"MINDFLOCK_MCP_MODE": "peer"},
        bridge_port=40001,
    )
    assert argv[:2] == [seatbelt.SANDBOX_EXEC, "-p"]
    assert argv[3:] == ["sh", "-c", "true"]
    tmpdir = os.path.join(share["home"], sandbox.SEATBELT_TMP)
    assert envs["TMPDIR"] == envs["CLAUDE_CODE_TMPDIR"] == tmpdir
    assert envs["HOME"] == share["home"]
    assert envs["PATH"].split(":")[0] == os.path.join(share["run"], "bin")
    assert envs["HTTPS_PROXY"] == "http://127.0.0.1:40001"
    assert envs["MINDFLOCK_MCP_MODE"] == "peer"
    assert "GH_TOKEN" not in envs and "SSH_AUTH_SOCK" not in envs
    assert "SECRET" not in "\0".join(argv) + "\0".join(envs.values())
    assert os.path.basename(links["claude"]) == "claude"
    assert "localhost:40001" in argv[2]


def test_build_seatbelt_refuses_option_looking_inner_argv(share, env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(sandbox.SandboxError):
        sandbox.build_seatbelt(share, ["-f", "/evil.sb"], "claude")


def test_install_links(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    d = sandbox.install_links(str(run), {"claude": "/opt/x/claude"})
    assert os.readlink(os.path.join(d, "claude")) == "/opt/x/claude"
    # Re-install replaces stale links.
    sandbox.install_links(str(run), {"python3": "/usr/bin/python3"})
    assert os.listdir(d) == ["python3"]
    with pytest.raises(sandbox.SandboxError):
        sandbox.install_links(str(run), {"../evil": "/x"})


def test_install_links_refuses_a_symlinked_bin(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    os.symlink(victim, run / "bin")
    with pytest.raises(sandbox.SandboxError):
        sandbox.install_links(str(run), {"claude": "/opt/x/claude"})
    assert os.listdir(victim) == []


# --------------------------------------------------------------------------
# a real sandbox-exec (macOS only)

PROBE = textwrap.dedent("""
    import json, os, socket, sys
    canary, gitdir = sys.argv[1], sys.argv[2]
    out = {}
    def t(name, f):
        try:
            f(); out[name] = "allow"
        except Exception as e:
            out[name] = "deny"
    def w(p): open(p, "w").write("x")
    t("read_canary", lambda: open(canary).read())
    t("list_user_home", lambda: os.listdir(os.path.dirname(os.path.dirname(canary))))
    t("write_work", lambda: w("probe-out.txt"))
    t("write_home", lambda: w(os.environ["HOME"] + "/x"))
    t("write_tmpdir", lambda: w(os.environ["TMPDIR"] + "/x"))
    t("write_gitfile", lambda: w(".git"))
    t("unlink_gitfile", lambda: os.unlink(".git"))
    t("write_gitdir", lambda: w(gitdir + "/evil"))
    t("write_private_tmp", lambda: w("/tmp/mf-seatbelt-escape-%d" % os.getpid()))
    t("hardlink_canary", lambda: os.link(canary, "hl"))
    t("connect_other_port", lambda: socket.create_connection(("127.0.0.1", 22), 2))
    t("connect_internet", lambda: socket.create_connection(("1.1.1.1", 443), 3))
    t("bind_other_port", lambda: socket.socket().bind(("127.0.0.1", 0)))
    print(json.dumps(out))
    """)


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
class TestOnMacOS:
    def test_available(self):
        ok, why = seatbelt.available()
        assert ok, why

    def test_escapes_are_denied(self, share, env):
        sandbox.prepare_home(share, "claude")
        py = os.path.realpath(sys.executable)
        argv, envs, links = sandbox.build_seatbelt(
            share,
            [
                py,
                "-I",
                "-c",
                PROBE,
                str(env["home"] / ".ssh" / "id_rsa"),
                share["gitdir"],
            ],
            "claude",
            bridge_port=40001,
        )
        sandbox.install_links(share["run"], links)
        os.makedirs(envs["TMPDIR"], exist_ok=True)
        proc = subprocess.run(
            argv,
            env=envs,
            cwd=share["work"],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        assert got == {
            "read_canary": "deny",
            "list_user_home": "deny",
            "write_work": "allow",
            "write_home": "allow",
            "write_tmpdir": "allow",
            "write_gitfile": "deny",
            "unlink_gitfile": "deny",
            "write_gitdir": "deny",
            "write_private_tmp": "deny",
            "hardlink_canary": "deny",
            "connect_other_port": "deny",
            "connect_internet": "deny",
            "bind_other_port": "deny",
        }

    def test_seeded_login_has_no_refresh_token(self, share, env):
        sandbox.prepare_home(share, "claude")
        raw = open(os.path.join(share["home"], ".claude", ".credentials.json")).read()
        assert "FAKE-access" in raw and "CANARY" not in raw
