"""Hermetic tests for the unified IDE launcher (backend.web.core.ide_launch).

Every external effect (shutil.which, Popen, OS detection, terminal discovery)
is mocked — no real process is ever launched.
"""

from __future__ import annotations

import shlex

import pytest

from backend.config import ide as I
from backend.web.core import ide_launch as L


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    """Shared settings-store isolation (tests/conftest.py) plus clearing the
    ambient MINDFLOCK_IDE / MINDFLOCK_TERMINAL overrides."""
    monkeypatch.delenv("MINDFLOCK_IDE", raising=False)
    monkeypatch.delenv("MINDFLOCK_TERMINAL", raising=False)


@pytest.fixture()
def popen_calls(monkeypatch):
    """Capture detached launches instead of spawning anything."""
    calls: list = []
    monkeypatch.setattr(
        L, "_popen_detached", lambda argv, **kwargs: calls.append(list(argv))
    )
    return calls


@pytest.fixture()
def short_sockdir():
    """A short-pathed dir for AF_UNIX sockets.

    macOS caps ``sun_path`` at ~104 chars and pytest's ``tmp_path`` (under
    ``/private/var/folders/…``) overflows it — for both ``bind()`` here and the
    ``connect()`` liveness probe inside the code under test. ``/tmp`` is short
    and writable on Linux and macOS alike.
    """
    import shutil
    import tempfile
    from pathlib import Path

    d = tempfile.mkdtemp(prefix="mfk-ipc-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _which_only(*names):
    """A shutil.which stand-in resolving only the given basenames."""

    def which(cmd):
        return "/usr/bin/%s" % cmd if cmd in names else None

    return which


# --------------------------------------------------------------------------- #
# detect_ides / ide_installed
# --------------------------------------------------------------------------- #


class TestDetect:
    def test_detects_only_commands_on_path(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", _which_only("cursor", "nvim"))
        assert {s.command for s in L.detect_ides()} == {"cursor", "nvim"}

    def test_nothing_installed(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        assert L.detect_ides() == []

    def test_macos_app_bundle_counts_as_installed(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            L,
            "_macos_app_bundle",
            lambda app: "/Applications/PyCharm.app" if app == "PyCharm" else None,
        )
        assert {s.command for s in L.detect_ides()} == {"pycharm"}

    def test_bundle_probe_skipped_off_macos(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            L,
            "_macos_app_bundle",
            lambda app: pytest.fail("bundle probe must not run on linux"),
        )
        assert L.ide_installed(I.spec_for("cursor")) is False

    def test_wsl_remote_cli_shim_counts_as_installed(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "wsl")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)

        def shim(cmd):
            spec = I.spec_for(cmd)
            assert (
                spec is not None and spec.storage_dirname is not None
            ), "shim probe must only run for VS Code-family editors"
            return (
                "/home/u/.cursor-server/bin/x/bin/remote-cli/cursor"
                if cmd == "cursor"
                else None
            )

        monkeypatch.setattr(L, "_wsl_remote_cli", shim)
        assert {s.command for s in L.detect_ides()} == {"cursor"}

    def test_shim_probe_skipped_off_wsl(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            L,
            "_wsl_remote_cli",
            lambda cmd: pytest.fail("shim probe must not run on linux"),
        )
        assert L.ide_installed(I.spec_for("cursor")) is False

    def test_macos_app_bundle_checks_applications_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            L.os.path,
            "isdir",
            lambda p: p == "/Applications/Zed.app",
        )
        assert L._macos_app_bundle("Zed") == "/Applications/Zed.app"
        monkeypatch.setattr(L.os.path, "isdir", lambda p: False)
        assert L._macos_app_bundle("Zed") is None


# --------------------------------------------------------------------------- #
# launch_ide — GUI kind
# --------------------------------------------------------------------------- #


class TestLaunchGui:
    def test_gui_launch_appends_path(self, monkeypatch, popen_calls):
        # Pin the generic (non-WSL) branch — on a WSL host the family editors
        # route through _launch_gui_wsl_family (TestLaunchGuiWslFamily).
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
        monkeypatch.setattr(L.shutil, "which", _which_only("cursor"))
        L.launch_ide("/x/ws")
        assert popen_calls == [["cursor", "/x/ws"]]

    def test_explicit_argv_override_with_arguments(self, monkeypatch, popen_calls):
        monkeypatch.setattr(L.shutil, "which", _which_only("flatpak"))
        L.launch_ide("/x/ws", argv=["flatpak", "run", "com.visualstudio.code"])
        assert popen_calls == [["flatpak", "run", "com.visualstudio.code", "/x/ws"]]

    def test_missing_gui_cli_raises_with_remediation(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        with pytest.raises(L.IdeLaunchError) as e:
            L.launch_ide("/x/ws")
        assert "cursor" in str(e.value)
        assert "Settings" in str(e.value)
        assert popen_calls == []

    def test_macos_falls_back_to_open_dash_a(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            L,
            "_macos_app_bundle",
            lambda app: "/Applications/Cursor.app" if app == "Cursor" else None,
        )
        L.launch_ide("/x/ws")
        assert popen_calls == [["open", "-a", "Cursor", "/x/ws"]]

    def test_macos_without_bundle_raises(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(L, "_macos_app_bundle", lambda app: None)
        with pytest.raises(L.IdeLaunchError):
            L.launch_ide("/x/ws")
        assert popen_calls == []

    def test_unknown_editor_treated_as_gui(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "myeditor")
        monkeypatch.setattr(L.shutil, "which", _which_only("myeditor"))
        L.launch_ide("/x/ws")
        assert popen_calls == [["myeditor", "/x/ws"]]

    def test_popen_failure_wrapped_in_ide_launch_error(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
        monkeypatch.setattr(L.shutil, "which", _which_only("cursor"))

        def boom(argv):
            raise OSError("spawn failed")

        monkeypatch.setattr(L, "_popen_detached", boom)
        with pytest.raises(L.IdeLaunchError) as e:
            L.launch_ide("/x/ws")
        assert "Cursor" in str(e.value)


# --------------------------------------------------------------------------- #
# launch_ide — terminal kind (per-OS wrapping)
# --------------------------------------------------------------------------- #


class TestLaunchTerminal:
    def test_linux_gnome_terminal(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "nvim")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", _which_only("nvim"))
        monkeypatch.setattr(L, "_linux_terminal", lambda: "/usr/bin/gnome-terminal")
        L.launch_ide("/x/ws")
        assert popen_calls == [
            ["/usr/bin/gnome-terminal", "--title", "ws", "--", "nvim", "/x/ws"]
        ]

    def test_linux_konsole_joins_command(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "hx")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", _which_only("hx"))
        monkeypatch.setattr(L, "_linux_terminal", lambda: "/usr/bin/konsole")
        L.launch_ide("/x/my ws")
        assert popen_calls == [
            ["/usr/bin/konsole", "-e", shlex.join(["hx", "/x/my ws"])]
        ]

    def test_linux_xterm_fallback(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "vim")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", _which_only("vim"))
        monkeypatch.setattr(L, "_linux_terminal", lambda: "/usr/bin/xterm")
        L.launch_ide("/x/ws")
        assert popen_calls == [["/usr/bin/xterm", "-T", "ws", "-e", "vim", "/x/ws"]]

    def test_wsl_wraps_in_windows_terminal(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "nvim")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "wsl")
        monkeypatch.setattr(L.shutil, "which", _which_only("nvim"))
        monkeypatch.setattr(L, "wt_command", lambda: "wt.exe")
        monkeypatch.setattr(L, "wsl_interop_available", lambda: True)
        monkeypatch.setattr(L, "wsl_distro", lambda: "Ubuntu")
        L.launch_ide("/x/ws")
        assert popen_calls == [
            [
                "wt.exe",
                "-w",
                "0",
                "nt",
                "--title",
                "ws",
                "wsl.exe",
                "-d",
                "Ubuntu",
                "--",
                "nvim",
                "/x/ws",
            ]
        ]

    def test_macos_wraps_in_terminal_app(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "nvim")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(L.shutil, "which", _which_only("nvim"))
        L.launch_ide("/x/ws")
        (argv,) = popen_calls
        assert argv[:2] == ["osascript", "-e"]
        assert 'tell application "Terminal" to do script' in argv[2]
        assert "nvim /x/ws" in argv[2]

    def test_linux_no_terminal_emulator_raises(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "nvim")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", _which_only("nvim"))
        monkeypatch.setattr(L, "_linux_terminal", lambda: None)
        with pytest.raises(L.IdeLaunchError) as e:
            L.launch_ide("/x/ws")
        assert "MINDFLOCK_TERMINAL" in str(e.value)
        assert popen_calls == []

    def test_missing_terminal_editor_raises(self, monkeypatch, popen_calls):
        monkeypatch.setenv("MINDFLOCK_IDE", "nvim")
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        with pytest.raises(L.IdeLaunchError) as e:
            L.launch_ide("/x/ws")
        assert "nvim" in str(e.value)
        assert popen_calls == []


class TestLaunchGuiWslFamily:
    """VS Code-family launches under WSL: reach a connected window over a
    verified-live IPC hook, else fall back to the Windows-side launcher."""

    @pytest.fixture()
    def popen_full(self, monkeypatch):
        """Capture argv AND kwargs (the env matters here)."""
        calls: list = []
        monkeypatch.setattr(
            L,
            "_popen_detached",
            lambda argv, **kw: calls.append((list(argv), kw)),
        )
        return calls

    @pytest.fixture(autouse=True)
    def _wsl(self, monkeypatch):
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "wsl")
        monkeypatch.delenv("VSCODE_IPC_HOOK_CLI", raising=False)

    def test_live_hook_uses_path_shim_with_hook_env(self, monkeypatch, popen_full):
        monkeypatch.setattr(L.shutil, "which", _which_only("cursor"))
        monkeypatch.setattr(L, "_live_ipc_hook", lambda *a: "/run/u/live.sock")
        L.launch_ide("/x/ws")
        ((argv, kw),) = popen_full
        assert argv == ["/usr/bin/cursor", "/x/ws"]
        assert kw["env"]["VSCODE_IPC_HOOK_CLI"] == "/run/u/live.sock"

    def test_live_hook_falls_back_to_remote_cli_shim(self, monkeypatch, popen_full):
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(L, "_live_ipc_hook", lambda *a: "/run/u/live.sock")
        monkeypatch.setattr(
            L,
            "_wsl_remote_cli",
            lambda cmd: "/h/.cursor-server/bin/x/bin/remote-cli/cursor",
        )
        L.launch_ide("/x/ws")
        ((argv, kw),) = popen_full
        assert argv == ["/h/.cursor-server/bin/x/bin/remote-cli/cursor", "/x/ws"]
        assert kw["env"]["VSCODE_IPC_HOOK_CLI"] == "/run/u/live.sock"

    def test_no_hook_launches_windows_side_editor(self, monkeypatch, popen_full):
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(L, "_live_ipc_hook", lambda *a: None)
        monkeypatch.setattr(
            L,
            "_windows_editor_cli",
            lambda cmd: "/mnt/c/Users/u/AppData/Local/Programs/cursor/resources/app/bin/cursor",
        )
        L.launch_ide("/x/ws")
        ((argv, kw),) = popen_full
        assert argv[0].startswith("/mnt/c/")
        assert argv[-1] == "/x/ws"
        assert kw.get("env") is None  # inherit: the script needs no hook

    def test_no_hook_path_hit_on_windows_mount_is_the_launcher(
        self, monkeypatch, popen_full
    ):
        monkeypatch.setattr(
            L.shutil,
            "which",
            lambda cmd: (
                "/mnt/c/Program Files/Cursor/resources/app/bin/cursor"
                if cmd == "cursor"
                else None
            ),
        )
        monkeypatch.setattr(L, "_live_ipc_hook", lambda *a: None)
        L.launch_ide("/x/ws")
        ((argv, _),) = popen_full
        assert argv == ["/mnt/c/Program Files/Cursor/resources/app/bin/cursor", "/x/ws"]

    def test_no_hook_linux_side_binary_launches_plain(self, monkeypatch, popen_full):
        monkeypatch.setattr(L.shutil, "which", _which_only("cursor"))
        monkeypatch.setattr(L, "_live_ipc_hook", lambda *a: None)
        monkeypatch.setattr(L, "_windows_editor_cli", lambda cmd: None)
        L.launch_ide("/x/ws")
        ((argv, _),) = popen_full
        assert argv == ["cursor", "/x/ws"]

    def test_nothing_available_raises_with_remediation(self, monkeypatch, popen_full):
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(L, "_live_ipc_hook", lambda *a: None)
        monkeypatch.setattr(L, "_wsl_remote_cli", lambda cmd: None)
        monkeypatch.setattr(L, "_windows_editor_cli", lambda cmd: None)
        with pytest.raises(L.IdeLaunchError) as e:
            L.launch_ide("/x/ws")
        assert "Cursor" in str(e.value)
        assert popen_full == []

    def test_windows_editor_cli_prefers_newest_build(self, tmp_path, monkeypatch):
        old = tmp_path / "old" / "resources" / "app" / "bin" / "cursor"
        new = tmp_path / "new" / "resources" / "app" / "bin" / "cursor"
        for p in (old, new):
            p.parent.mkdir(parents=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        import os as os_mod

        t = os_mod.path.getmtime(new)
        os_mod.utime(old, (t - 100, t - 100))
        monkeypatch.setattr(
            L.glob,
            "glob",
            lambda pat: [str(old), str(new)] if "AppData" in pat else [],
        )
        assert L._windows_editor_cli("cursor") == str(new)


class TestWslIpcHook:
    """_wsl_ipc_hook must return the newest LIVE socket — socket files from
    closed editor windows linger forever, and handing the Remote-WSL CLI a
    dead hook makes 'open in IDE' a silent no-op."""

    def test_skips_newer_dead_socket_for_older_live_one(
        self, short_sockdir, monkeypatch
    ):
        import socket as socket_mod

        live_path = short_sockdir / "vscode-ipc-live.sock"
        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(str(live_path))
        srv.listen(1)
        try:
            # A DEAD socket file with a NEWER mtime (a closed window's leftover).
            dead = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            dead_path = short_sockdir / "vscode-ipc-dead.sock"
            dead.bind(str(dead_path))
            dead.close()  # bound but never listening -> connect refused
            import os as os_mod

            now = os_mod.path.getmtime(live_path)
            os_mod.utime(dead_path, (now + 100, now + 100))

            monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_sockdir))
            assert L._wsl_ipc_hook() == str(live_path)
        finally:
            srv.close()

    def test_none_when_all_sockets_dead(self, short_sockdir, monkeypatch):
        import socket as socket_mod

        dead = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        dead_path = short_sockdir / "vscode-ipc-x.sock"
        dead.bind(str(dead_path))
        dead.close()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_sockdir))
        # /run/user and /tmp may hold REAL live sockets on a dev machine —
        # restrict discovery to the tmp root for a deterministic answer.
        monkeypatch.setattr(
            L.glob,
            "glob",
            lambda pat: [str(dead_path)] if str(short_sockdir) in pat else [],
        )
        assert L._wsl_ipc_hook() is None


class TestSocketOwnershipFilter:
    """Every VS Code-family fork names its IPC sockets vscode-ipc-*.sock, so a
    live socket can belong to the WRONG editor (Kiro/VS Code while Cursor is
    configured) — aiming the configured editor's CLI at it is a silent no-op.
    Sockets provably owned by another editor are skipped; unknown owners are
    accepted so a failed attribution degrades to the old accept-any behavior."""

    @pytest.fixture()
    def live_sock(self, short_sockdir, monkeypatch):
        import socket as socket_mod

        # short_sockdir keeps the path under the macOS AF_UNIX limit for both
        # bind() here and the connect() liveness probe inside _wsl_ipc_hook.
        p = short_sockdir / "vscode-ipc-a.sock"
        srv = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        srv.bind(str(p))
        srv.listen(1)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_sockdir))
        monkeypatch.setattr(
            L.glob,
            "glob",
            lambda pat: [str(p)] if str(short_sockdir) in pat else [],
        )
        yield str(p)
        srv.close()

    def test_foreign_owned_socket_is_skipped(self, live_sock, monkeypatch):
        monkeypatch.setattr(
            L,
            "_sock_owner_cmdline",
            lambda s: "/home/u/.kiro-server/bin/x/node server-main.js",
        )
        assert L._wsl_ipc_hook(".cursor-server") is None

    def test_matching_owner_is_accepted(self, live_sock, monkeypatch):
        monkeypatch.setattr(
            L,
            "_sock_owner_cmdline",
            lambda s: "/home/u/.cursor-server/bin/x/node server-main.js",
        )
        assert L._wsl_ipc_hook(".cursor-server") == live_sock

    def test_unknown_owner_is_accepted(self, live_sock, monkeypatch):
        monkeypatch.setattr(L, "_sock_owner_cmdline", lambda s: None)
        assert L._wsl_ipc_hook(".cursor-server") == live_sock

    def test_no_marker_disables_the_filter(self, live_sock, monkeypatch):
        monkeypatch.setattr(
            L,
            "_sock_owner_cmdline",
            lambda s: "/home/u/.kiro-server/bin/x/node server-main.js",
        )
        assert L._wsl_ipc_hook() == live_sock

    def test_env_hook_owned_by_other_editor_falls_through(self, live_sock, monkeypatch):
        # The inherited VSCODE_IPC_HOOK_CLI points at a live but FOREIGN socket;
        # discovery (same socket here) is filtered too, so the result is None.
        monkeypatch.setenv("VSCODE_IPC_HOOK_CLI", live_sock)
        monkeypatch.setattr(
            L,
            "_sock_owner_cmdline",
            lambda s: "/home/u/.kiro-server/bin/x/node server-main.js",
        )
        assert L._live_ipc_hook(".cursor-server") is None


class TestServerDirMarker:
    def test_shim_path_is_ground_truth(self):
        shim = "/h/.cursor-server/bin/x/bin/remote-cli/cursor"
        assert L._server_dir_marker("cursor", shim) == ".cursor-server"

    def test_known_fork_table_fallback(self):
        assert L._server_dir_marker("code", "/usr/bin/code") == ".vscode-server"
        assert L._server_dir_marker("cursor", None) == ".cursor-server"

    def test_unknown_editor_has_no_marker(self):
        assert L._server_dir_marker("myeditor", "/usr/bin/myeditor") is None


class TestLaunchPassesMarker:
    def test_foreign_socket_falls_back_to_windows_launcher(self, monkeypatch):
        """End to end: cursor configured, the only live socket is another
        editor's -> no hook -> the Windows-side launcher opens Cursor fresh."""
        calls: list = []
        monkeypatch.setattr(
            L, "_popen_detached", lambda argv, **kw: calls.append(list(argv))
        )
        monkeypatch.setattr(L.osenv, "os_kind", lambda: "wsl")
        monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
        monkeypatch.delenv("VSCODE_IPC_HOOK_CLI", raising=False)
        monkeypatch.setattr(L.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            L,
            "_wsl_remote_cli",
            lambda cmd: "/h/.cursor-server/bin/x/bin/remote-cli/cursor",
        )
        seen_markers: list = []

        def fake_hook(marker=None):
            seen_markers.append(marker)
            return None  # the only live socket was foreign -> filtered out

        monkeypatch.setattr(L, "_live_ipc_hook", fake_hook)
        monkeypatch.setattr(
            L,
            "_windows_editor_cli",
            lambda cmd: "/mnt/c/Program Files/cursor/resources/app/bin/cursor",
        )
        L.launch_ide("/x/ws")
        assert seen_markers == [".cursor-server"]
        assert calls == [
            ["/mnt/c/Program Files/cursor/resources/app/bin/cursor", "/x/ws"]
        ]
