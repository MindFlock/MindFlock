"""The Windows engine swap (backend/web/core/engine_swap.py).

2026-11-13: a desktop auto-update ran ``uv tool install --force`` while the
server, the session host and the sessions' MCP servers ran from the tool
venv. Windows refused to delete their mapped files, uv gave up halfway and
the engine was left without ``Lib`` ("Failed find package `mindflock` in tool
environment"). The swap builds the new engine beside the running one, moves
the live venv aside (Windows allows renaming what a process runs from) and
puts it back on any failure. These tests pin that contract with a fake
installer; the real-Windows run is in docs/windows.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.web.core import engine_swap


class FakeUv:
    """Stands in for ``uv tool install``: each call takes the next outcome
    from ``plan`` and writes into ``UV_TOOL_DIR`` / ``UV_TOOL_BIN_DIR`` of the
    env it was given. Engine checks (the venv's python, the exe) answer from
    the file they run: "broken" fails, anything else passes."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.installs = []  # the UV_TOOL_DIR of each install
        self.checks = []

    def __call__(self, argv, env=None, **kw):
        if argv[0] == "uv":
            env = env or {}
            tools, bindir = Path(env["UV_TOOL_DIR"]), Path(env["UV_TOOL_BIN_DIR"])
            self.installs.append(str(tools))
            what = self.plan.pop(0)
            venv = tools / "mindflock"
            if what == "fail":
                return subprocess.CompletedProcess(argv, 2)
            if what == "partial":  # what uv did on Windows: half a venv, exit 2
                venv.mkdir(parents=True, exist_ok=True)
                (venv / "Scripts").mkdir(exist_ok=True)
                return subprocess.CompletedProcess(argv, 2)
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text(what, encoding="utf-8")
            bindir.mkdir(parents=True, exist_ok=True)
            (bindir / "mindflock").write_text(what, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0)
        self.checks.append(argv[0])
        try:
            body = Path(argv[0]).read_text(encoding="utf-8")
        except OSError:
            return subprocess.CompletedProcess(argv, 1, stdout=b"missing")
        rc = 1 if body == "broken" else 0
        return subprocess.CompletedProcess(
            argv, rc, stdout=b"boom\nNo module named 'backend'"
        )


@pytest.fixture()
def eng(tmp_path):
    """A live engine: <tmp>/uv/tools/mindflock (marked "old") + its shim."""
    tools = tmp_path / "uv" / "tools"
    bindir = tmp_path / "bin"
    venv = tools / "mindflock"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("old", encoding="utf-8")
    (venv / "Lib").mkdir()
    bindir.mkdir()
    (bindir / "mindflock").write_text("old", encoding="utf-8")
    env = dict(os.environ, UV_TOOL_DIR=str(tools), UV_TOOL_BIN_DIR=str(bindir))
    return {
        "tools": tools,
        "venv": venv,
        "exe": bindir / "mindflock",
        "env": env,
    }


def _swap(eng, fake, logs, **kw):
    return engine_swap.swap_install(
        ["uv", "tool", "install", "--force", "mindflock[web]"],
        venv=str(eng["venv"]),
        exe=str(eng["exe"]),
        run=fake,
        log=logs.append,
        env=eng["env"],
        stamp="20261113101100",
        sleep=lambda s: None,
        **kw,
    )


def _leftovers(eng):
    parent = eng["tools"].parent
    return sorted(p.name for p in parent.iterdir() if p.name != "tools")


def test_a_good_update_stages_first_then_swaps_in_the_new_engine(eng):
    fake, logs = FakeUv(["new", "new"]), []
    assert _swap(eng, fake, logs) == 0
    # The build ran in the stage BESIDE the tools dir (uv errors on strangers
    # inside it), then for real into the live place.
    assert fake.installs == [
        str(eng["tools"]) + "-mindflock-next" + os.sep + "tools",
        str(eng["tools"]),
    ]
    assert (eng["venv"] / "bin" / "python").read_text() == "new"
    assert eng["exe"].read_text() == "new"
    # Stage and the moved-aside copy are gone; nothing new lives in tools/.
    assert _leftovers(eng) == []
    assert sorted(os.listdir(eng["tools"])) == ["mindflock"]
    assert not list(eng["exe"].parent.glob("mindflock.old-*"))


def test_a_failed_build_changes_nothing(eng):
    fake, logs = FakeUv(["fail"]), []
    assert _swap(eng, fake, logs) == 2
    assert len(fake.installs) == 1  # the live install never ran
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert (eng["venv"] / "Lib").is_dir()
    assert eng["exe"].read_text() == "old"
    assert _leftovers(eng) == []
    assert "nothing was changed" in logs[-1]


def test_a_staged_engine_that_does_not_import_changes_nothing(eng):
    fake, logs = FakeUv(["broken"]), []
    assert _swap(eng, fake, logs) == 1
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert "don't import" in logs[-1] and "nothing was changed" in logs[-1]


def test_the_2026_11_13_failure_puts_the_old_engine_back(eng):
    # The stage builds; the live install then leaves half a venv and exits 2
    # (Windows refusing a delete). The previous engine must be back, whole.
    fake, logs = FakeUv(["new", "partial"]), []
    assert _swap(eng, fake, logs) == 2
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert (eng["venv"] / "Lib").is_dir()
    assert not (eng["venv"] / "Scripts").exists()
    assert eng["exe"].read_text() == "old"
    assert _leftovers(eng) == []
    assert "the previous engine is back in place" in logs


def test_an_install_that_says_ok_but_does_not_run_is_rolled_back_too(eng):
    fake, logs = FakeUv(["new", "broken"]), []
    assert _swap(eng, fake, logs) == 1
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert any("putting the previous engine back" in m for m in logs)


def test_an_old_engine_still_in_use_is_kept_and_cleaned_up_next_time(eng, monkeypatch):
    real = engine_swap._rmtree
    old = str(eng["tools"]) + "-mindflock-old-20261113101100"
    # A session still runs from the moved-aside copy: Windows won't delete it.
    monkeypatch.setattr(
        engine_swap, "_rmtree", lambda p: False if p == old else real(p)
    )
    fake, logs = FakeUv(["new", "new"]), []
    assert _swap(eng, fake, logs) == 0
    assert os.path.isdir(old)
    assert any("stays at" in m and old in m for m in logs)
    # The session ended; the next update (or cleanup) clears it.
    monkeypatch.setattr(engine_swap, "_rmtree", real)
    assert engine_swap.cleanup_old(str(eng["venv"]), str(eng["exe"])) == []
    assert not os.path.exists(old)


def test_a_shim_held_by_a_running_mindflock_changes_nothing(eng, monkeypatch):
    # An engine from before the sys.path fix keeps mindflock.exe open while it
    # serves (importlib.metadata caches a ZipFile on it): it can't be renamed,
    # and uv would then fail copying the new one. Nothing may change.
    real = os.rename

    def deny_shim(src, dst):
        if str(src) == str(eng["exe"]):
            raise PermissionError(32, "being used by another process")
        return real(src, dst)

    monkeypatch.setattr(engine_swap.os, "rename", deny_shim)
    fake, logs = FakeUv(["new"]), []
    assert _swap(eng, fake, logs) == engine_swap.EXIT_BUSY
    assert len(fake.installs) == 1  # the stage only
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert eng["exe"].read_text() == "old"
    assert _leftovers(eng) == []
    assert "in use by a running MindFlock" in logs[-1]


def test_a_shim_freed_within_the_wait_is_moved(eng, monkeypatch):
    real, tries = os.rename, []

    def busy_twice(src, dst):
        if str(src) == str(eng["exe"]) and len(tries) < 2:
            tries.append(1)
            raise PermissionError(32, "being used by another process")
        return real(src, dst)

    monkeypatch.setattr(engine_swap.os, "rename", busy_twice)
    assert _swap(eng, FakeUv(["new", "new"]), []) == 0
    assert eng["exe"].read_text() == "new" and len(tries) == 2


def test_a_venv_that_cannot_be_moved_aside_is_left_alone(eng, monkeypatch):
    real = os.rename

    def deny_venv(src, dst):
        if str(src) == str(eng["venv"]):
            raise PermissionError(13, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(engine_swap.os, "rename", deny_venv)
    fake, logs = FakeUv(["new"]), []
    assert _swap(eng, fake, logs) == engine_swap.EXIT_BUSY
    assert len(fake.installs) == 1
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert eng["exe"].read_text() == "old"  # the shim went back too
    assert not list(eng["exe"].parent.glob("mindflock.old-*"))
    assert _leftovers(eng) == []


def test_a_first_install_has_nothing_to_stage_or_move(tmp_path):
    tools, bindir = tmp_path / "tools", tmp_path / "bin"
    env = dict(os.environ, UV_TOOL_DIR=str(tools), UV_TOOL_BIN_DIR=str(bindir))
    fake, logs = FakeUv(["new"]), []
    code = engine_swap.swap_install(
        ["uv", "tool", "install"],
        venv=str(tools / "mindflock"),
        exe=str(bindir / "mindflock"),
        run=fake,
        log=logs.append,
        env=env,
    )
    assert code == 0 and fake.installs == [str(tools)]


def test_check_engine_names_what_is_wrong(eng):
    fake = FakeUv([])
    assert engine_swap.check_engine(str(eng["venv"]), str(eng["exe"]), run=fake) == ""
    (eng["venv"] / "bin" / "python").write_text("broken")
    why = engine_swap.check_engine(str(eng["venv"]), "", run=fake)
    assert "don't import" in why and "No module named 'backend'" in why
    shutil.rmtree(eng["venv"] / "bin")
    assert "missing" in engine_swap.check_engine(str(eng["venv"]), run=fake)


def test_windows_paths_use_scripts_python():
    assert engine_swap.venv_python(r"C:\u\uv\tools\mindflock").endswith(
        os.path.join("Scripts", "python.exe")
    )


def test_the_import_check_imports_for_real():
    # IMPORT_CHECK must stay importable — and side-effect free — in this venv.
    cp = subprocess.run(
        [sys.executable, "-c", engine_swap.IMPORT_CHECK],
        capture_output=True,
        timeout=120,
    )
    assert cp.returncode == 0, cp.stderr


# --------------------------------------------------------------------------- #
# As a standalone script (how the in-app updater runs it on Windows)
# --------------------------------------------------------------------------- #
def _fake_uv_script(bindir: Path, live_fails: bool) -> None:
    """An ``uv`` that installs a runnable fake venv (``bin/python`` exits 0);
    with ``live_fails`` the non-stage install leaves half a venv, exit 2."""
    uv = bindir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$UV_TOOL_DIR" in *mindflock-next*) live=0 ;; *) live=1 ;; esac\n'
        'if [ "$live" = 1 ] && [ "%s" = 1 ]; then\n'
        '  mkdir -p "$UV_TOOL_DIR/mindflock/Scripts"; echo "error: Access is denied"; exit 2\n'
        "fi\n"
        'mkdir -p "$UV_TOOL_DIR/mindflock/bin" "$UV_TOOL_BIN_DIR"\n'
        'printf "#!/bin/sh\\nexit 0\\n" > "$UV_TOOL_DIR/mindflock/bin/python"\n'
        'chmod +x "$UV_TOOL_DIR/mindflock/bin/python"\n'
        'cp "$UV_TOOL_DIR/mindflock/bin/python" "$UV_TOOL_BIN_DIR/mindflock"\n'
        'echo "new" > "$UV_TOOL_DIR/mindflock/NEW"\n'
        "exit 0\n" % ("1" if live_fails else "0"),
        encoding="utf-8",
    )
    uv.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="the fake uv is a POSIX sh script")
@pytest.mark.parametrize("live_fails", [False, True])
def test_the_script_form_swaps_or_restores(eng, tmp_path, live_fails):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _fake_uv_script(fakebin, live_fails)
    script = tmp_path / "copy" / "engine_swap.py"
    script.parent.mkdir()
    shutil.copyfile(engine_swap.__file__, script)  # run OUTSIDE the package
    (eng["venv"] / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (eng["venv"] / "bin" / "python").chmod(0o755)
    env = dict(eng["env"], PATH="%s:/usr/bin:/bin" % fakebin)
    cp = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            "--venv",
            str(eng["venv"]),
            "--exe",
            "",
            "--",
            "uv",
            "tool",
            "install",
            "--force",
            "x",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if live_fails:
        assert cp.returncode == 2, cp.stdout + cp.stderr
        assert (eng["venv"] / "Lib").is_dir() and not (eng["venv"] / "NEW").exists()
        assert "=== the previous engine is back in place ===" in cp.stdout
    else:
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert (eng["venv"] / "NEW").exists() and not (eng["venv"] / "Lib").exists()
    assert _leftovers(eng) == []


def test_the_script_form_needs_an_install_command():
    with pytest.raises(SystemExit):
        engine_swap.main(["--venv", "x"])


# --------------------------------------------------------------------------- #
# Real Windows: the lock the swap exists for
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(os.name != "nt", reason="Windows file locking")
def test_windows_lets_a_running_venv_be_moved_aside(tmp_path):  # pragma: no cover
    # A copy of python.exe running from "the venv" maps it: deleting fails,
    # renaming the folder does not — the whole premise of the swap.
    venv = tmp_path / "tools" / "mindflock" / "Scripts"
    venv.mkdir(parents=True)
    base = Path(
        sys._base_executable if hasattr(sys, "_base_executable") else sys.executable
    )
    for f in base.parent.glob("*.dll"):
        shutil.copy2(f, venv / f.name)
    shutil.copy2(base, venv / "python.exe")
    proc = subprocess.Popen(
        [str(venv / "python.exe"), "-c", "import time; time.sleep(60)"]
    )
    try:
        import time

        time.sleep(1.5)
        with pytest.raises(OSError):
            os.remove(venv / "python.exe")
        moved = tmp_path / "tools-mindflock-old-1"
        os.rename(tmp_path / "tools" / "mindflock", moved)
        assert proc.poll() is None and (moved / "Scripts" / "python.exe").exists()
    finally:
        proc.kill()
        proc.wait()


def test_a_mindflock_process_lets_go_of_its_shim(tmp_path):
    # Under uv's trampoline sys.path[0] is mindflock.exe itself (a zipapp);
    # importlib.metadata kept a ZipFile open on it for the life of the
    # process, so a running server pinned the shim and blocked the swap.
    import backend

    shim = tmp_path / "mindflock.exe"
    shim.write_bytes(b"MZ")
    path = [str(shim), "/site-packages"]
    backend._drop_shim_from_path(path, windows=True)
    assert path == ["/site-packages"]
    # Only on Windows, only a real .exe file: a script's folder stays.
    for first, windows in (
        (str(shim), False),
        (str(tmp_path), True),
        (r"C:\no\x.exe", True),
    ):
        path = [first]
        backend._drop_shim_from_path(path, windows=windows)
        assert path == [first]


def test_messages_are_ascii_and_a_crash_after_the_move_still_restores(eng):
    # The script form prints to whatever code page the console has (cp437
    # has no em dash): a UnicodeEncodeError mid-swap must not be possible,
    # and neither may any other exception strand the moved-aside engine.
    src = Path(engine_swap.__file__).read_text(encoding="utf-8")
    runtime = src[src.index("def find_exe") :]
    assert all(ord(c) < 128 for c in runtime)

    calls = []

    def crashing(argv, env=None, **kw):
        calls.append(argv)
        if len(calls) == 1:
            return FakeUv(["new"])(argv, env=env)
        if argv[0] == "uv":
            raise OSError(2, "uv vanished")
        return FakeUv([])(argv, env=env)

    logs = []
    assert _swap(eng, crashing, logs) == 1
    assert (eng["venv"] / "bin" / "python").read_text() == "old"
    assert eng["exe"].read_text() == "old"
    assert any("could not run" in m for m in logs)
