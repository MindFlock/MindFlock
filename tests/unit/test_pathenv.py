"""PATH enrichment for GUI-launched backends (:mod:`backend.pathenv`).

A backend started from a desktop launcher inherits a minimal ``PATH`` that never
sourced the user's shell profile, so ``shutil.which`` misses CLIs that work in
the terminal. ``pathenv`` probes the login+interactive shell and unions in
well-known per-user bin dirs. These tests lock the parse, the guards, the union
ordering, idempotency, and the never-raises contract — and that the server
lifespan calls it without letting a failure abort startup.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from backend import log, osenv, pathenv


@pytest.fixture(autouse=True)
def _reset_pathenv(monkeypatch):
    """Every function in ``pathenv`` is ``lru_cache``d — clear them around each
    test so a prior run's probe/PATH never leaks in, and neutralize the two
    control env vars so the ambient shell can't flip a test."""
    for var in ("MINDFLOCK_NO_PATH_ENRICH", "MINDFLOCK_PATH_PROBE"):
        monkeypatch.delenv(var, raising=False)
    # Capture the real cached callables now — a test may monkeypatch the module
    # attribute with a plain lambda, so re-reading it at teardown would miss the
    # ``cache_clear`` the true objects still carry.
    cached = [
        pathenv.login_shell_dirs,
        pathenv.well_known_dirs,
        pathenv.ensure_enriched,
    ]
    for fn in cached:
        fn.cache_clear()
    yield
    for fn in cached:
        fn.cache_clear()


def _framed(*env_lines: str, prefix: str = "", suffix: str = "") -> str:
    """Build a fake ``$SHELL -ilc`` stdout: rc banner noise, then the delimiter-
    framed ``env`` dump, then trailing noise."""
    block = "\n".join(env_lines)
    d = pathenv._DELIM
    return "%s%s%s%s%s" % (prefix, d, block, d, suffix)


class _Proc:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.returncode = 0


# --------------------------------------------------------------------------- #
# login_shell_dirs
# --------------------------------------------------------------------------- #
def test_login_shell_dirs_parses_path_amid_banner_noise(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    out = _framed(
        "HOME=/home/me",
        "PATH=/home/me/.local/bin:/usr/bin",
        "TERM=xterm",
        prefix="Welcome to your shell!\nnvm loaded\n",
        suffix="\nlast login banner\n",
    )
    monkeypatch.setattr(pathenv.subprocess, "run", lambda *a, **k: _Proc(out))
    assert pathenv.login_shell_dirs() == ("/home/me/.local/bin", "/usr/bin")


def test_login_shell_dirs_no_path_line_returns_empty(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        pathenv.subprocess, "run", lambda *a, **k: _Proc(_framed("HOME=/h", "TERM=x"))
    )
    assert pathenv.login_shell_dirs() == ()


def test_login_shell_dirs_no_delim_returns_empty(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        pathenv.subprocess, "run", lambda *a, **k: _Proc("PATH=/usr/bin\n")
    )
    assert pathenv.login_shell_dirs() == ()


def test_login_shell_dirs_no_shell_returns_empty(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.delenv("SHELL", raising=False)
    # No subprocess should even be attempted.
    monkeypatch.setattr(
        pathenv.subprocess,
        "run",
        lambda *a, **k: pytest.fail("probe ran without $SHELL"),
    )
    assert pathenv.login_shell_dirs() == ()


def test_login_shell_dirs_non_unix_returns_empty(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: False)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(
        pathenv.subprocess,
        "run",
        lambda *a, **k: pytest.fail("probe ran on a non-unix host"),
    )
    assert pathenv.login_shell_dirs() == ()


def test_login_shell_dirs_reentrancy_guard(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("MINDFLOCK_PATH_PROBE", "1")  # we are inside a probe
    monkeypatch.setattr(
        pathenv.subprocess,
        "run",
        lambda *a, **k: pytest.fail("probe recursed"),
    )
    assert pathenv.login_shell_dirs() == ()


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(["sh"], 4.0),
        OSError("no such shell"),
        subprocess.SubprocessError("boom"),
    ],
)
def test_login_shell_dirs_swallows_subprocess_errors(monkeypatch, exc):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/zsh")

    def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(pathenv.subprocess, "run", _boom)
    assert pathenv.login_shell_dirs() == ()  # never raises


def test_login_shell_dirs_invocation_and_probe_env(monkeypatch):
    monkeypatch.setattr(osenv, "is_unix_like", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    seen = {}

    def _capture(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _Proc(_framed("PATH=/usr/bin"))

    monkeypatch.setattr(pathenv.subprocess, "run", _capture)
    pathenv.login_shell_dirs()
    assert seen["argv"][:2] == ["/bin/zsh", "-ilc"]
    assert seen["kw"]["timeout"] == pathenv._PROBE_TIMEOUT
    # The child carries the reentrancy guard so a rc file that imports MindFlock
    # can't recurse into another probe.
    assert seen["kw"]["env"]["MINDFLOCK_PATH_PROBE"] == "1"


# --------------------------------------------------------------------------- #
# well_known_dirs
# --------------------------------------------------------------------------- #
def test_well_known_dirs_only_existing_in_priority_order(monkeypatch, tmp_path):
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    cargo = home / ".cargo" / "bin"
    local_bin.mkdir(parents=True)
    cargo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Only the two dirs we created "exist"; everything else (/usr/local/bin, …)
    # reports absent, so ordering/existence is deterministic across machines.
    existing = {str(local_bin), str(cargo)}
    monkeypatch.setattr(pathenv.os.path, "isdir", lambda p: p in existing)
    dirs = pathenv.well_known_dirs()
    assert dirs == (str(local_bin), str(cargo))  # ~ expanded, priority preserved


def test_well_known_dirs_dedupes(monkeypatch, tmp_path):
    # If two candidates expand to the same path, it appears once.
    monkeypatch.setattr(pathenv.os.path, "isdir", lambda p: True)
    dirs = pathenv.well_known_dirs()
    assert len(dirs) == len(set(dirs))


# --------------------------------------------------------------------------- #
# enriched_path
# --------------------------------------------------------------------------- #
def test_enriched_path_unions_base_first_deduped(monkeypatch):
    monkeypatch.setattr(
        pathenv, "login_shell_dirs", lambda: ("/usr/bin", "/opt/tool/bin")
    )
    monkeypatch.setattr(
        pathenv, "well_known_dirs", lambda: ("/opt/tool/bin", "/home/me/.local/bin")
    )
    merged = pathenv.enriched_path("/usr/bin::/usr/local/bin")
    parts = merged.split(os.pathsep)
    # Base entries keep their position first (empty segment dropped)...
    assert parts[:2] == ["/usr/bin", "/usr/local/bin"]
    # ...then the new dirs, each exactly once across all three groups.
    assert parts == [
        "/usr/bin",
        "/usr/local/bin",
        "/opt/tool/bin",
        "/home/me/.local/bin",
    ]
    assert len(parts) == len(set(parts))


def test_enriched_path_defaults_to_current_environ(monkeypatch):
    monkeypatch.setattr(pathenv, "login_shell_dirs", lambda: ())
    monkeypatch.setattr(pathenv, "well_known_dirs", lambda: ("/new/bin",))
    monkeypatch.setenv("PATH", "/base/bin")
    merged = pathenv.enriched_path()
    assert merged.split(os.pathsep) == ["/base/bin", "/new/bin"]


# --------------------------------------------------------------------------- #
# ensure_enriched
# --------------------------------------------------------------------------- #
def test_ensure_enriched_adds_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/base/bin")
    monkeypatch.setattr(
        pathenv,
        "enriched_path",
        lambda before=None: "/base/bin" + os.pathsep + "/new/bin",
    )
    added = pathenv.ensure_enriched()
    assert added == ("/new/bin",)
    assert os.environ["PATH"] == "/base/bin" + os.pathsep + "/new/bin"
    # Second call is cached (lru_cache) — a no-op that returns the same result
    # and does not append again.
    assert pathenv.ensure_enriched() == ("/new/bin",)
    assert os.environ["PATH"].count("/new/bin") == 1


def test_ensure_enriched_noop_when_unchanged(monkeypatch):
    monkeypatch.setenv("PATH", "/base/bin")
    monkeypatch.setattr(pathenv, "enriched_path", lambda before=None: "/base/bin")
    assert pathenv.ensure_enriched() == ()
    assert os.environ["PATH"] == "/base/bin"


def test_ensure_enriched_honors_disable_flag(monkeypatch):
    monkeypatch.setenv("PATH", "/base/bin")
    monkeypatch.setenv("MINDFLOCK_NO_PATH_ENRICH", "1")
    monkeypatch.setattr(
        pathenv,
        "enriched_path",
        lambda before=None: pytest.fail("enrichment ran while disabled"),
    )
    assert pathenv.ensure_enriched() == ()
    assert os.environ["PATH"] == "/base/bin"


def test_ensure_enriched_swallows_exceptions(monkeypatch):
    monkeypatch.setenv("PATH", "/base/bin")

    def _boom(before=None):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(pathenv, "enriched_path", _boom)
    assert pathenv.ensure_enriched() == ()  # never raises
    assert os.environ["PATH"] == "/base/bin"  # left untouched


def test_ensure_enriched_logs_when_dirs_added(monkeypatch):
    monkeypatch.setenv("PATH", "/base/bin")
    monkeypatch.setattr(
        pathenv,
        "enriched_path",
        lambda before=None: "/base/bin" + os.pathsep + "/new/bin",
    )
    lines = []

    class _Cap:
        def Printf(self, fmt, *args):
            lines.append(fmt % args if args else fmt)

    monkeypatch.setattr(log, "InfoLog", _Cap())
    pathenv.ensure_enriched()
    assert len(lines) == 1
    assert "/new/bin" in lines[0] and "pathenv" in lines[0]


def test_ensure_enriched_does_not_crash_when_infolog_none(monkeypatch):
    monkeypatch.setenv("PATH", "/base/bin")
    monkeypatch.setattr(
        pathenv,
        "enriched_path",
        lambda before=None: "/base/bin" + os.pathsep + "/new/bin",
    )
    monkeypatch.setattr(log, "InfoLog", None)
    assert pathenv.ensure_enriched() == ("/new/bin",)  # no AttributeError


# --------------------------------------------------------------------------- #
# server lifespan wiring
# --------------------------------------------------------------------------- #
def test_lifespan_calls_ensure_enriched(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import pathenv as _pathenv
    from backend.web.server import app

    calls = []
    monkeypatch.setattr(_pathenv, "ensure_enriched", lambda: calls.append(True) or ())
    with TestClient(app):  # entering the context runs the lifespan startup
        pass
    assert calls, "lifespan did not call pathenv.ensure_enriched at startup"


def test_lifespan_survives_ensure_enriched_raising(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import pathenv as _pathenv
    from backend.web.server import app

    def _boom():
        raise RuntimeError("PATH probe blew up at startup")

    monkeypatch.setattr(_pathenv, "ensure_enriched", _boom)
    # A raising probe must not abort startup — the server still comes up and
    # answers requests.
    with TestClient(app) as c:
        assert c.get("/api/addons").status_code == 200
