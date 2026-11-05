"""The native-Windows follow-ups (docs/windows.md), pinned from Linux.

Each test flips ``backend.compat.IS_WINDOWS`` (or the module's own flag)
rather than running on Windows; the real-box checks are in the worker report.
Linux/macOS behaviour is pinned unchanged alongside each one.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest

from backend import compat
from backend.config import config as cfgmod
from backend.config import red_zones
from backend.providers import _tool_hook_src as th
from backend.providers import agent_limits as al
from backend.providers import base, launch_script, mcp_attach
from backend.session.winmux import commands, jobscope

#: Pins of the Linux/macOS behaviour (the CI's Windows job runs this file too).
posix_only = pytest.mark.skipif(os.name == "nt", reason="pins Linux/macOS")
windows_only = pytest.mark.skipif(os.name != "nt", reason="needs real Windows")


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    return monkeypatch


# --------------------------------------------------------------------------- #
# 1. MINDFLOCK_HOME is the config dir
# --------------------------------------------------------------------------- #
def test_mindflock_home_is_the_config_dir(monkeypatch, tmp_path):
    """A process given only a scratch MINDFLOCK_HOME (the session host's rule,
    install.json's) wrote state.json / config.json into $HOME before."""
    real = tmp_path / "real-home"
    scratch = tmp_path / "scratch" / ".mindflock"
    monkeypatch.setenv("HOME", str(real))
    monkeypatch.setenv("MINDFLOCK_HOME", str(scratch))
    assert cfgmod.GetConfigDir() == str(scratch)
    cfgmod.LoadConfig()
    from backend.config import state as statemod

    statemod.LoadState()
    assert sorted(os.listdir(scratch)) == [
        "config.json",
        "state.json",
        "state.json.lock",
    ]
    assert not real.exists()


def test_config_dir_without_mindflock_home_is_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MINDFLOCK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cfgmod.GetConfigDir() == str(tmp_path / ".mindflock")
    monkeypatch.setenv("MINDFLOCK_HOME", "   ")  # blank = unset
    assert cfgmod.GetConfigDir() == str(tmp_path / ".mindflock")


def test_windows_profile_without_home(monkeypatch, tmp_path):
    """Windows has no $HOME by default: the profile is %USERPROFILE%."""
    monkeypatch.delenv("MINDFLOCK_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(cfgmod.os, "name", "nt")
    assert cfgmod._user_home_dir() == str(tmp_path)


def test_real_home_still_refused_under_pytest(monkeypatch):
    from backend.config import home_guard

    monkeypatch.setenv(
        "MINDFLOCK_HOME", os.path.join(home_guard.real_home(), ".mindflock")
    )
    with pytest.raises(home_guard.RealHomeStoreError):
        cfgmod.GetConfigDir()


def test_gitnexus_run_dir_follows_the_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MINDFLOCK_RUN_DIR", raising=False)
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "mf"))
    assert al.shim_dir() == str(tmp_path / "mf" / "run" / "bin")
    assert mcp_attach.run_dir() == str(tmp_path / "mf" / "run")


# --------------------------------------------------------------------------- #
# 2. the seed prompt file
# --------------------------------------------------------------------------- #
@posix_only
def test_seed_prompt_is_lf_and_posix_text_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path))
    expr = base.seed_prompt_expr("s1", "line one\nline two\n")
    path = tmp_path / "s1.md"
    assert path.read_bytes() == b"line one\nline two\n"
    assert expr == '"$(cat %s)"' % shlex.quote(str(path))


def test_seed_prompt_on_windows_is_shell_path(win, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path))
    real_quote = shlex.quote
    seen = []
    monkeypatch.setattr(
        launch_script, "shell_path", lambda p: seen.append(str(p)) or "C:/x y/s.md"
    )
    expr = base.seed_prompt_expr("s", "a\r\nb")
    assert seen and expr == '"$(cat %s)"' % real_quote("C:/x y/s.md")
    # newline="": what was given is what is on disk (no \n → \r\n either way)
    assert (tmp_path / "s.md").read_bytes() == b"a\r\nb"


# --------------------------------------------------------------------------- #
# 3. launch paths with spaces
# --------------------------------------------------------------------------- #
@pytest.fixture
def spaced_exe(tmp_path):
    d = tmp_path / "John Smith" / ".local" / "bin"
    d.mkdir(parents=True)
    exe = d / "claude.EXE"
    exe.write_text("")
    return str(exe)


@posix_only
def test_posix_program_split_unchanged(spaced_exe):
    assert base.split_program("aider --model x") == ("aider", "--model x")
    assert base.program_args("aider --model  x") == ["--model", "x"]
    assert base.program_basename("/usr/bin/claude --x") == "claude"
    # Linux/macOS never treat a spaced path as one word (byte-identical).
    assert base.split_program(spaced_exe)[0] != spaced_exe
    assert launch_script.program_word(spaced_exe) == spaced_exe


def test_windows_program_split_keeps_a_spaced_path(win, spaced_exe):
    assert base.split_program(spaced_exe) == (spaced_exe, "")
    assert base.split_program(spaced_exe + " --flag 'a b'") == (
        spaced_exe,
        "--flag 'a b'",
    )
    assert base.split_program('"C:\\P F\\x.exe" -y') == ("C:\\P F\\x.exe", "-y")
    assert base.program_basename(r"C:\Users\me\.local\bin\claude.EXE") == "claude"
    assert base.program_basename(spaced_exe) == "claude"


def test_windows_claude_path_matches_and_folds(win, spaced_exe):
    from backend import providers

    assert providers.resolve(spaced_exe).name == "claude"
    assert providers.normalize_program(r"C:\Users\emand\.local\bin\claude.EXE") == (
        "claude"
    )
    assert providers.normalize_program(spaced_exe) == "claude"


def test_windows_program_word_quotes_a_path(win, spaced_exe):
    word = launch_script.program_word(spaced_exe + " --x")
    fwd = spaced_exe.replace("\\", "/")
    assert word == shlex.quote(fwd) + " --x"
    assert shlex.split(word)[0] == fwd
    assert launch_script.program_word("claude --x") == "claude --x"
    assert (
        launch_script.program_word(r"C:\tools\codex.exe")
        == "C:/tools/codex.exe"  # shlex leaves C:/… bare
    )


def test_windows_claude_launch_command_quotes_the_binary(win, spaced_exe):
    from backend.providers.claude import claude_launch_command

    out = claude_launch_command(spaced_exe, resume=False, skip_permissions=True)
    fwd = spaced_exe.replace("\\", "/")
    assert out.startswith(shlex.quote(fwd) + " --dangerously-skip-permissions")
    # Linux/macOS: verbatim, as the goldens pin.
    compat.IS_WINDOWS = False
    try:
        assert claude_launch_command("claude", resume=False) == "claude"
    finally:
        compat.IS_WINDOWS = True


def test_windows_binary_override_with_spaces(win, monkeypatch, spaced_exe):
    monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_CLAUDE", spaced_exe)
    out = launch_script.launch_program("claude --x", launch_script.LauncherSpec())
    assert out == shlex.quote(spaced_exe.replace("\\", "/")) + " --x"


def test_mcp_config_run_file_found_inside_quotes(monkeypatch, tmp_path):
    run = tmp_path / "Run Dir"
    run.mkdir()
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(run))
    f = str(run / "mcp-s.json")
    script = "claude %s --x" % shlex.quote("--mcp-config=" + f)
    assert "'" in script
    assert mcp_attach._script_run_files(script) == [f]
    # The bare form is still read (up to whitespace); outside the run dir: none.
    plain = tmp_path / "run2"
    plain.mkdir()
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(plain))
    g = str(plain / "mcp-t.json")
    assert mcp_attach._script_run_files("claude --mcp-config=%s --x" % g) == [g]
    assert mcp_attach._script_run_files("claude --mcp-config=/elsewhere/a.json") == []


# --------------------------------------------------------------------------- #
# 4. the server's interpreter home stays out of sessions (unit; the host
#    end-to-end is in test_winmux_polish)
# --------------------------------------------------------------------------- #
def test_server_python_env_keys():
    env = {
        "PYTHONHOME": "C:/py",
        "UV_INTERNAL__PYTHONHOME": "C:/py",
        "VIRTUAL_ENV": sys.prefix,
        "PATH": "/bin",
    }
    assert sorted(commands._server_python_env(env)) == [
        "PYTHONHOME",
        "UV_INTERNAL__PYTHONHOME",
        "VIRTUAL_ENV",
    ]
    # Another venv (the user's own, activated in their shell) is theirs.
    assert commands._server_python_env({"VIRTUAL_ENV": "/home/u/proj/.venv"}) == []


# --------------------------------------------------------------------------- #
# 6. gitnexus shim under Git Bash
# --------------------------------------------------------------------------- #
def test_bash_path(win):
    assert (
        al.bash_path(r"C:\Users\me\.mindflock\run\bin")
        == "/c/Users/me/.mindflock/run/bin"
    )
    assert al.bash_path("D:/x") == "/d/x"
    assert al.bash_path("C:") == "/c/"
    compat.IS_WINDOWS = False
    try:
        assert al.bash_path(r"C:\x") == r"C:\x"
    finally:
        compat.IS_WINDOWS = True


def test_windows_gitnexus_path_and_shim_forms(win, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", r"C:\Users\me\.mindflock\run")
    text = al.gitnexus_shim_text(
        r"C:\Users\me\venv\Scripts\python.exe", 4096, package_root=r"C:\src\mf"
    )
    assert "shim_dir=/c/Users/me/.mindflock/run/bin\n" in text
    assert "if [ -x C:/Users/me/venv/Scripts/python.exe ]" in text
    assert "PYTHONPATH=C:/src/mf exec" in text
    monkeypatch.delenv("MINDFLOCK_AGENT_SCOPE", raising=False)
    monkeypatch.setattr(al, "config", lambda raw=None: dict(al.DEFAULTS, max="off"))
    monkeypatch.setattr(al, "gitnexus_shim_dir", lambda cfg=None: r"C:\a b\run\bin")
    out = al.wrap_launch("claude", "s")
    assert out == "PATH='/c/a b/run/bin':\"$PATH\"; export PATH; claude"


def test_shim_file_is_lf(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path))
    monkeypatch.delenv("MINDFLOCK_GITNEXUS_GATE", raising=False)
    monkeypatch.setattr(al, "_real_gitnexus_on_path", lambda path=None: "/x/gitnexus")
    d = al.gitnexus_shim_dir(dict(al.DEFAULTS))
    assert d and b"\r\n" not in open(os.path.join(d, "gitnexus"), "rb").read()


# --------------------------------------------------------------------------- #
# 8. the Windows memory scope (Job Object → exit 137)
# --------------------------------------------------------------------------- #
def test_windows_scope_wraps_the_launch(win, monkeypatch):
    monkeypatch.delenv("MINDFLOCK_AGENT_SCOPE", raising=False)
    monkeypatch.setattr(al, "config", lambda raw=None: dict(al.DEFAULTS, max="1G"))
    monkeypatch.setattr(al, "gitnexus_shim_dir", lambda cfg=None: None)
    monkeypatch.setattr(
        compat, "posix_shell", lambda bash=False: r"C:\Git\bin\bash.exe"
    )
    monkeypatch.setattr(
        "backend.providers.activity_markers.hook_python", lambda: "C:/py/python.exe"
    )
    out = al.wrap_launch("claude --x 'a b'", "s")
    argv = shlex.split(out)
    assert argv[:3] == [
        "C:/py/python.exe",
        "-I",
        launch_script.shell_path(al.jobscope_path()),
    ]
    assert argv[3:7] == ["--max", str(1 << 30), "--", "C:/Git/bin/bash.exe"]
    assert argv[7:] == ["-c", "claude --x 'a b'"]
    assert al.jobscope_path().endswith(os.path.join("winmux", "jobscope.py"))
    assert os.path.isfile(al.jobscope_path())
    monkeypatch.setenv("MINDFLOCK_AGENT_SCOPE", "0")
    assert al.wrap_launch("claude", "s") == "claude"


def test_jobscope_args_and_passthrough(tmp_path):
    assert jobscope.parse_args(["--max", "10", "--", "a", "b"]) == (10, ["a", "b"])
    assert jobscope.parse_args(["--max=0", "--", "a"]) == (0, ["a"])
    with pytest.raises(ValueError):
        jobscope.parse_args(["--max", "1"])
    assert jobscope.KILLED_EXIT_CODE == launch_script.KILLED_EXIT_CODE
    # Run by path with -I (as wrap_launch does): stdlib only, the command's
    # own exit status comes back.
    rc = subprocess.run(
        [
            sys.executable,
            "-I",
            al.jobscope_path(),
            "--max",
            "0",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
        timeout=30,
    ).returncode
    assert rc == 7


# --------------------------------------------------------------------------- #
# 9. Setup's install script
# --------------------------------------------------------------------------- #
def test_setup_install_script_is_lf(monkeypatch, tmp_path):
    from backend.web.core import setup_install

    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path))
    path = setup_install._write_script("powershell -EncodedCommand QQA=\necho ok\n")
    assert open(path, "rb").read() == b"powershell -EncodedCommand QQA=\necho ok\n"


# --------------------------------------------------------------------------- #
# 10. Windows path spellings against zones and fences
# --------------------------------------------------------------------------- #
@pytest.fixture
def nt_os(monkeypatch):
    """The hook's ``os`` as native Windows has it (ntpath), for the matcher."""
    import ntpath

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "sep", "\\")
    monkeypatch.setattr(os, "path", ntpath)
    return monkeypatch


def _guard():
    return {
        "root": "C:\\Users\\me\\repo",
        "lroot": "C:\\Users\\me\\repo",
        "ci": True,
        "rules": [{"re": red_zones.compile_pattern("backend/secret/**"), "id": "z"}],
        "protect": ["C:/Users/me/.mindflock-assistant/red-zones"],
        "files": ["backend/secret/a.py"],
        "dirs": [],
        "sym": [],
    }


@pytest.mark.parametrize(
    "target",
    [
        "C:\\Users\\me\\repo\\backend\\secret\\a.py",
        "C:/Users/me/repo/backend/secret/a.py",
        "c:\\users\\ME\\Repo\\backend\\secret\\a.py",
        "/c/Users/me/repo/backend/secret/a.py",
        "backend\\secret\\a.py",
    ],
)
def test_hook_matches_every_windows_spelling(nt_os, target):
    hit = th._mf_zone_hit(target, "C:\\Users\\me\\repo", _guard())
    assert hit and hit.get("rel") == "backend/secret/a.py"


def test_hook_windows_outside_and_protect(nt_os):
    g = _guard()
    assert th._mf_zone_hit("C:/Users/me/repo/backend/ok.py", "", g) is None
    assert th._mf_zone_hit("C:/Users/me/repo-other/backend/secret/a.py", "", g) is None
    hit = th._mf_zone_hit(
        "c:\\users\\me\\.mindflock-assistant\\red-zones\\g.json", "", g
    )
    assert hit and hit.get("protect")


@posix_only
def test_hook_posix_relto_unchanged():
    assert th._mf_relto("/r/a/b", "/r") == "a/b"
    assert th._mf_relto("/R/a/b", "/r") is None  # case matters on Linux
    assert th._mf_under("/x/y/z", "/x/y/") and not th._mf_under("/x/yz", "/x/y")


@posix_only
def test_server_rel_form(monkeypatch):
    assert red_zones.rel_form("backend\\x.py") == "backend\\x.py"  # POSIX: as is
    monkeypatch.setattr(red_zones, "_WINDOWS", True)
    root = "C:\\Users\\me\\repo"
    assert red_zones.rel_form("backend\\x.py") == "backend/x.py"
    assert red_zones.rel_form("./backend/x.py") == "backend/x.py"
    assert (
        red_zones.rel_form("C:\\Users\\me\\repo\\backend\\x.py", root) == "backend/x.py"
    )
    assert red_zones.rel_form("c:/users/me/REPO/backend/x.py", root) == "backend/x.py"
    assert red_zones.rel_form("/c/Users/me/repo/backend/x.py", root) == "backend/x.py"
    assert red_zones.rel_form("D:\\elsewhere\\x.py", root) is None
    doc = {"red": ["backend/secret/**"]}
    assert red_zones.classify("backend\\secret\\a.py", None, doc) == "blocked"
    v = red_zones.verdict(doc, "backend\\secret\\a.py")
    assert v and v["kind"] == "red"
    monkeypatch.setattr(red_zones, "_WINDOWS", False)
    assert red_zones.classify("backend\\secret\\a.py", None, doc) == "ok"


def test_fence_zone_doc_matches_a_windows_rel(monkeypatch):
    """A session fence (``only=`` backend/providers/**) judged on a Windows
    spelling of a path inside it."""
    monkeypatch.setattr(red_zones, "_WINDOWS", True)
    doc = {"green": ["backend/providers/**"]}
    assert red_zones.classify("backend\\providers\\base.py", None, doc) == "ok"
    assert red_zones.classify("backend\\web\\server.py", None, doc) == "outside"


def test_uninstall_stops_the_session_host_on_windows(monkeypatch):
    from backend import uninstall

    import backend.session.winmux.client as wc

    calls = []
    monkeypatch.setattr(wc, "stop_host", lambda timeout=10.0: calls.append(1) or True)
    monkeypatch.setattr(wc, "host_running", lambda: True)
    monkeypatch.setattr(uninstall, "_stop_mcp_bridge", lambda: False)
    monkeypatch.setattr(uninstall, "_mcp_bridge_running", lambda: False)
    monkeypatch.setattr(uninstall.os, "name", "nt")
    rep = uninstall.execute(uninstall.Plan(), dry_run=True)
    monkeypatch.setattr(uninstall.os, "name", "posix")
    assert any("would stop the session host" in a for a in rep.actions)
    assert not calls
    monkeypatch.setattr(uninstall.os, "name", "nt")
    rep = uninstall.execute(uninstall.Plan())
    monkeypatch.setattr(uninstall.os, "name", "posix")
    assert calls == [1]
    assert rep.actions[0].startswith("stopped the session host")


@posix_only
def test_uninstall_leaves_tmux_alone_off_windows(monkeypatch):
    from backend import uninstall

    import backend.session.winmux.client as wc

    monkeypatch.setattr(uninstall, "_stop_mcp_bridge", lambda: False)
    monkeypatch.setattr(wc, "stop_host", lambda timeout=10.0: pytest.fail("called"))
    rep = uninstall.execute(uninstall.Plan(), dry_run=False)
    assert not any("session host" in a for a in rep.actions)


# --------------------------------------------------------------------------- #
# Real Windows only (the CI's windows-latest job, a dev box)
# --------------------------------------------------------------------------- #
@windows_only
def test_real_jobscope_ends_an_agent_over_its_limit_with_137():
    """A child committing past the job's limit: the whole tree exits 137,
    the code the auto-revive reads as "killed for memory"."""
    hog = "x = []\nwhile True:\n    x.append(bytearray(16 << 20))\n"
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            al.jobscope_path(),
            "--max",
            str(256 << 20),
            "--",
            sys.executable,
            "-c",
            hog,
        ],
        capture_output=True,
        timeout=120,
    )
    assert r.returncode == 137, r.stderr
    assert b"memory limit" in r.stderr


@windows_only
def test_real_jobscope_under_the_limit_is_transparent():
    r = subprocess.run(
        [
            sys.executable,
            "-I",
            al.jobscope_path(),
            "--max",
            str(1 << 30),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(5)",
        ],
        capture_output=True,
        timeout=60,
    )
    assert r.returncode == 5


@windows_only
def test_real_windows_relto_and_rel_form(tmp_path):
    root = str(tmp_path)
    target = os.path.join(root, "Backend", "x.py")
    assert th._mf_relto(os.path.normpath(target.upper()), root) == "BACKEND/X.PY"
    assert th._mf_relto(os.path.normpath(target.replace("\\", "/")), root) == (
        "Backend/x.py"
    )
    assert red_zones.rel_form(target.replace("\\", "/"), root) == "Backend/x.py"
