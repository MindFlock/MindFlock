"""The server caps glibc's malloc arenas before it starts any thread."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.web.core import malloc_arenas


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc only")
def test_cap_applies_in_a_fresh_process():
    # In a child: mallopt changes process-wide allocator state, which this
    # test process should not carry around.
    code = "from backend.web.core.malloc_arenas import cap_arenas; print(cap_arenas())"
    env = {"PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(malloc_arenas.ARENA_MAX)


@pytest.mark.parametrize(
    "var,value",
    [("MALLOC_ARENA_MAX", "4"), ("GLIBC_TUNABLES", "glibc.malloc.arena_max=4")],
)
def test_an_explicit_environment_choice_wins(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    called = []
    monkeypatch.setattr("ctypes.CDLL", lambda *a, **k: called.append(a))
    assert malloc_arenas.cap_arenas() is None
    assert called == []


def test_skipped_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert malloc_arenas.cap_arenas() is None


def test_never_raises_without_glibc(monkeypatch):
    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    monkeypatch.delenv("GLIBC_TUNABLES", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    def _no_libc(*a, **k):
        raise OSError("no libc")

    monkeypatch.setattr("ctypes.CDLL", _no_libc)
    assert malloc_arenas.cap_arenas() is None


def test_serve_entry_caps_before_importing_the_server(monkeypatch):
    # main() must apply the cap before anything that could start a thread;
    # pin the call order by stopping main() at its first import afterwards.
    from backend.web import run

    order = []
    monkeypatch.setattr(
        malloc_arenas, "cap_arenas", lambda *a, **k: order.append("cap")
    )
    real_import = __import__

    def _import(name, *a, **k):
        if name == "uvicorn":
            order.append("uvicorn")
            raise RuntimeError("stop here")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _import)
    with pytest.raises(RuntimeError, match="stop here"):
        run.main([])
    assert order == ["cap", "uvicorn"]
