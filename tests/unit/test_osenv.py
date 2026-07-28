"""Cross-OS detection + terminal-launcher tests.

MindFlock supports Linux, macOS, and Windows-via-WSL (tmux + Unix PTYs + fcntl).
These tests lock the OS detection and the per-OS terminal-tab argv builder so a
change that breaks one platform's launch path fails here.
"""

from __future__ import annotations

import builtins
import io
from unittest.mock import patch

import pytest

from backend import osenv
from backend.ticket_ingestion import terminal_launch as tl


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    # Clear override env so wsl_distro/wt_command test their defaults.
    for v in ("MINDFLOCK_WSL_DISTRO", "MINDFLOCK_WT_COMMAND", "MINDFLOCK_TERMINAL"):
        monkeypatch.delenv(v, raising=False)
    osenv._detect.cache_clear()
    yield
    osenv._detect.cache_clear()


class TestDetection:
    def test_macos(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        osenv._detect.cache_clear()
        assert osenv.os_kind() == "macos"
        assert osenv.is_macos() and osenv.is_unix_like()
        assert not osenv.is_wsl()

    def test_native_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        osenv._detect.cache_clear()
        assert osenv.os_kind() == "windows"
        assert osenv.is_windows()
        assert not osenv.is_unix_like()

    def test_wsl_via_env(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
        osenv._detect.cache_clear()
        assert osenv.os_kind() == "wsl"
        assert osenv.is_wsl() and osenv.is_unix_like()

    def test_native_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        monkeypatch.delenv("WSL_INTEROP", raising=False)
        osenv._detect.cache_clear()
        with patch.object(osenv, "_looks_like_wsl", return_value=False):
            osenv._detect.cache_clear()
            assert osenv.os_kind() == "linux"
            assert osenv.is_linux() and osenv.is_unix_like()


class TestWslProcFallback:
    """The /proc kernel-string heuristic — the WSL safety net that catches a
    guest shell without the interop env vars set (e.g. a bare `sudo` shell).
    ``_looks_like_wsl`` is probed directly (the env fast-path is covered by
    ``TestDetection.test_wsl_via_env``)."""

    @pytest.fixture(autouse=True)
    def _no_wsl_env(self, monkeypatch):
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        monkeypatch.delenv("WSL_INTEROP", raising=False)

    @staticmethod
    def _patch_proc(monkeypatch, handler):
        """Replace ``open`` so only the /proc probes are intercepted."""
        real_open = builtins.open
        probes = ("/proc/sys/kernel/osrelease", "/proc/version")

        def fake_open(path, *args, **kwargs):
            if path in probes:
                return handler(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fake_open)

    def test_microsoft_kernel_string_detected(self, monkeypatch):
        self._patch_proc(
            monkeypatch,
            lambda path: io.StringIO("5.15.90.1-microsoft-standard-WSL2\n"),
        )
        assert osenv._looks_like_wsl() is True

    def test_plain_linux_kernel_string_is_false(self, monkeypatch):
        self._patch_proc(monkeypatch, lambda path: io.StringIO("5.15.0-91-generic\n"))
        assert osenv._looks_like_wsl() is False

    def test_unreadable_proc_never_raises(self, monkeypatch):
        def _boom(path):
            raise OSError("permission denied")

        self._patch_proc(monkeypatch, _boom)
        assert osenv._looks_like_wsl() is False


class TestTerminalArgv:
    def test_wsl_builds_wt_attach(self):
        with (
            patch.object(osenv, "os_kind", return_value="wsl"),
            patch.object(tl, "wt_command", return_value="wt.exe"),
            patch("shutil.which", return_value="wt.exe"),
            patch.object(tl, "wsl_interop_available", return_value=True),
            patch.object(tl, "wsl_distro", return_value="Ubuntu"),
        ):
            argv = tl.build_terminal_tab_argv("T", "sess")
        assert argv[0] == "wt.exe"
        assert argv[-4:] == ["tmux", "attach", "-t", "sess"]
        assert "Ubuntu" in argv

    def test_wsl_no_wt_degrades(self):
        # Windows Terminal not resolvable in this process's PATH → no-op
        # (matches the Linux branch) instead of an unspawnable argv.
        with (
            patch.object(osenv, "os_kind", return_value="wsl"),
            patch.object(tl, "wt_command", return_value="wt.exe"),
            patch("shutil.which", return_value=None),
        ):
            assert tl.build_terminal_tab_argv("T", "sess") is None

    def test_wsl_builds_when_wt_resolves_absolute(self):
        # Windows Terminal present under a full PATH (shutil.which returns a
        # real absolute path) → argv is still built with the resolved command.
        resolved = "/mnt/c/Users/x/AppData/Local/wt.exe"
        with (
            patch.object(osenv, "os_kind", return_value="wsl"),
            patch.object(tl, "wt_command", return_value=resolved),
            patch("shutil.which", return_value=resolved),
            patch.object(tl, "wsl_interop_available", return_value=True),
            patch.object(tl, "wsl_distro", return_value="Ubuntu"),
        ):
            argv = tl.build_terminal_tab_argv("T", "sess")
        assert argv is not None
        assert argv[0] == resolved
        assert argv[-4:] == ["tmux", "attach", "-t", "sess"]

    def test_wsl_dead_interop_degrades(self):
        # wt.exe on PATH but the WSLInterop binfmt entry was flushed (e.g. by
        # a Docker/qemu binfmt reset) — every .exe exec would raise
        # OSError(ENOEXEC), so the builder degrades to a no-op.
        with (
            patch.object(osenv, "os_kind", return_value="wsl"),
            patch.object(tl, "wt_command", return_value="wt.exe"),
            patch("shutil.which", return_value="wt.exe"),
            patch.object(tl, "wsl_interop_available", return_value=False),
        ):
            assert tl.build_terminal_tab_argv("T", "sess") is None

    def test_wsl_interop_available_reads_binfmt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tl, "_BINFMT_MISC_DIR", str(tmp_path))
        # No MZ handler registered → interop dead.
        (tmp_path / "qemu-aarch64").write_text(
            "enabled\ninterpreter /usr/libexec/qemu-binfmt/aarch64\n"
            "magic 7f454c460201010000000000000000000200b700\n"
        )
        assert tl.wsl_interop_available() is False
        # WSLInterop-style entry (enabled, PE "MZ" magic 4d5a) → alive.
        (tmp_path / "WSLInterop").write_text(
            "enabled\ninterpreter /init\nflags: PF\noffset 0\nmagic 4d5a\n"
        )
        assert tl.wsl_interop_available() is True

    def test_macos_unaffected_by_which_guard(self):
        # Regression guard: the shutil.which no-op guard only gates the WSL
        # branch. macOS builds its osascript argv even when nothing resolves on
        # PATH (it never consults shutil.which).
        with (
            patch.object(osenv, "os_kind", return_value="macos"),
            patch("shutil.which", return_value=None),
        ):
            argv = tl.build_terminal_tab_argv("T", "sess")
        assert argv is not None
        assert argv[0] == "osascript"
        assert "tmux attach -t sess" in argv[-1]

    def test_macos_uses_osascript(self):
        with patch.object(osenv, "os_kind", return_value="macos"):
            argv = tl.build_terminal_tab_argv("T", "sess")
        assert argv[0] == "osascript"
        assert "tmux attach -t sess" in argv[-1]

    def test_linux_prefers_env_terminal(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_TERMINAL", "kitty")
        with (
            patch.object(osenv, "os_kind", return_value="linux"),
            patch(
                "shutil.which",
                side_effect=lambda c: "/usr/bin/" + c if c == "kitty" else None,
            ),
        ):
            argv = tl.build_terminal_tab_argv("T", "sess")
        assert argv[0] == "/usr/bin/kitty"
        assert argv[-3:] == ["attach", "-t", "sess"] or "sess" in argv

    def test_linux_gnome_terminal(self, monkeypatch):
        with (
            patch.object(osenv, "os_kind", return_value="linux"),
            patch(
                "shutil.which",
                side_effect=lambda c: (
                    "/usr/bin/" + c if c == "gnome-terminal" else None
                ),
            ),
        ):
            argv = tl.build_terminal_tab_argv("T", "sess")
        assert argv[0] == "/usr/bin/gnome-terminal"
        assert argv[-4:] == ["tmux", "attach", "-t", "sess"]

    def test_linux_none_available_degrades(self):
        with (
            patch.object(osenv, "os_kind", return_value="linux"),
            patch("shutil.which", return_value=None),
        ):
            assert tl.build_terminal_tab_argv("T", "sess") is None

    def test_native_windows_no_launch(self):
        with patch.object(osenv, "os_kind", return_value="windows"):
            assert tl.build_terminal_tab_argv("T", "sess") is None
