"""Unit tests for the Go-wire ``config.json`` byte-format contract.

``backend.config.config`` exists to be byte-for-byte compatible with Go's
``json.MarshalIndent(v, "", "  ")`` output (any Go build is an external reader
of the same file). These tests lock in the escaping, field order, ``omitempty``
and fallback behavior so a well-meaning "cleanup" cannot silently break interop.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from backend.config import config as C


def _raise_oserror(*_a, **_k):
    raise OSError("simulated config-dir failure")


class TestGoEscape:
    def test_escapes_html_chars(self):
        # Each of <, >, & becomes its \\uXXXX escape (literal backslash-u form).
        assert C._go_escape("<") == "\\u003c"
        assert C._go_escape(">") == "\\u003e"
        assert C._go_escape("&") == "\\u0026"

    def test_escapes_line_paragraph_separators(self):
        assert C._go_escape(" ") == "\\u2028"
        assert C._go_escape(" ") == "\\u2029"

    def test_leaves_other_text_untouched(self):
        assert C._go_escape("plain ascii and é ü 漢字") == "plain ascii and é ü 漢字"


class TestMarshalIndent:
    def test_returns_bytes_with_no_trailing_newline(self):
        out = C.marshal_indent({"k": "v"})
        assert isinstance(out, bytes)
        assert not out.endswith(b"\n")

    def test_two_space_indent(self):
        text = C.marshal_indent({"k": "v"}).decode("utf-8")
        # Top-level object opens, then a 2-space-indented member on line 2.
        assert text.split("\n")[0] == "{"
        assert text.split("\n")[1].startswith('  "k"')

    def test_applies_go_html_escaping(self):
        text = C.marshal_indent({"k": "a<b>c&d"}).decode("utf-8")
        assert "\\u003c" in text
        assert "\\u003e" in text
        assert "\\u0026" in text
        # The raw characters must not survive into the document.
        assert "<" not in text and ">" not in text and "&" not in text

    def test_escapes_line_paragraph_separators_in_values(self):
        text = C.marshal_indent({"k": "  "}).decode("utf-8")
        assert "\\u2028" in text
        assert "\\u2029" in text


class TestConfigToDict:
    def test_field_order_and_always_present_fields(self):
        cfg = C.Config(
            default_program="claude",
            auto_yes=True,
            daemon_poll_interval=1000,
            branch_prefix="ethan/",
        )
        d = cfg.to_dict()
        # Field order is a wire contract; profiles omitted when empty.
        assert list(d.keys()) == [
            "default_program",
            "auto_yes",
            "daemon_poll_interval",
            "branch_prefix",
        ]
        assert d == {
            "default_program": "claude",
            "auto_yes": True,
            "daemon_poll_interval": 1000,
            "branch_prefix": "ethan/",
        }

    def test_profiles_omitempty_when_empty(self):
        cfg = C.Config(default_program="claude")
        assert "profiles" not in cfg.to_dict()

    def test_profiles_emitted_when_present(self):
        cfg = C.Config(
            default_program="claude",
            profiles=[C.Profile(name="claude", program="/opt/claude")],
        )
        d = cfg.to_dict()
        # profiles is the last key, preserving field order.
        assert list(d.keys())[-1] == "profiles"
        assert d["profiles"] == [{"name": "claude", "program": "/opt/claude"}]


class TestGetProgram:
    def test_returns_matching_profile_program(self):
        cfg = C.Config(
            default_program="claude",
            profiles=[
                C.Profile(name="other", program="/opt/other"),
                C.Profile(name="claude", program="/opt/claude"),
            ],
        )
        assert cfg.GetProgram() == "/opt/claude"

    def test_falls_back_to_default_program_when_no_profile_matches(self):
        cfg = C.Config(
            default_program="claude",
            profiles=[C.Profile(name="other", program="/opt/other")],
        )
        assert cfg.GetProgram() == "claude"

    def test_falls_back_with_no_profiles(self):
        cfg = C.Config(default_program="mycmd")
        assert cfg.GetProgram() == "mycmd"


class TestDefaultConfig:
    def test_default_program_falls_back_to_literal_claude(self, monkeypatch):
        def _boom():
            raise RuntimeError("claude command not found in aliases or PATH")

        monkeypatch.setattr(C, "GetClaudeCommand", _boom)
        cfg = C.DefaultConfig()
        assert cfg.default_program == C.DEFAULT_PROGRAM == "claude"
        assert cfg.auto_yes is False
        assert cfg.daemon_poll_interval == 1000

    def test_uses_resolved_claude_command(self, monkeypatch):
        monkeypatch.setattr(C, "GetClaudeCommand", lambda: "/usr/local/bin/claude")
        cfg = C.DefaultConfig()
        assert cfg.default_program == "/usr/local/bin/claude"


class TestDefaultBranchPrefix:
    def test_lowercases_username(self, monkeypatch):
        monkeypatch.setattr(C.getpass, "getuser", lambda: "Ethan")
        assert C._default_branch_prefix() == "ethan/"

    def test_session_when_getuser_raises(self, monkeypatch):
        def _boom():
            raise OSError("no user")

        monkeypatch.setattr(C.getpass, "getuser", _boom)
        assert C._default_branch_prefix() == "session/"

    def test_session_when_username_empty(self, monkeypatch):
        monkeypatch.setattr(C.getpass, "getuser", lambda: "")
        assert C._default_branch_prefix() == "session/"


class TestRoundTrip:
    def test_round_trip_with_profiles(self):
        cfg = C.Config(
            default_program="/opt/claude",
            auto_yes=True,
            daemon_poll_interval=1500,
            branch_prefix="ethan/",
            profiles=[
                C.Profile(name="claude", program="/opt/claude"),
                C.Profile(name="x", program="y"),
            ],
        )
        restored = C.Config.from_dict(json.loads(cfg.marshal_indent()))
        assert restored == cfg

    def test_round_trip_without_profiles(self):
        cfg = C.Config(
            default_program="claude",
            auto_yes=False,
            daemon_poll_interval=1000,
            branch_prefix="session/",
        )
        restored = C.Config.from_dict(json.loads(cfg.marshal_indent()))
        assert restored == cfg
        assert restored.profiles == []


class TestGetConfigDir:
    def test_uses_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/xyz")
        assert C.GetConfigDir() == "/home/xyz/.mindflock"

    def test_raises_when_home_unset(self, monkeypatch):
        monkeypatch.setenv("HOME", "")  # Go's os.UserHomeDir returns "" -> error
        with pytest.raises(OSError):
            C.GetConfigDir()

    def test_user_home_dir_none_when_empty(self, monkeypatch):
        monkeypatch.setenv("HOME", "")
        assert C._user_home_dir() is None


class TestClaudeLookupCommand:
    """The extracted ``sh -c`` command builder, tested in isolation (no shell
    is spawned) so each shell's rc-sourcing form is pinned independently of the
    subprocess plumbing in GetClaudeCommand."""

    def test_zsh_sources_zshrc(self):
        assert C._claude_lookup_command("/usr/bin/zsh") == (
            "source ~/.zshrc &>/dev/null || true; which claude"
        )

    def test_bash_sources_bashrc(self):
        assert C._claude_lookup_command("/bin/bash") == (
            "source ~/.bashrc &>/dev/null || true; which claude"
        )

    def test_unknown_shell_bare_which(self):
        # A shell we don't special-case skips rc sourcing entirely.
        assert C._claude_lookup_command("/usr/bin/fish") == "which claude"

    def test_timeout_uses_named_constant(self, monkeypatch):
        # GetClaudeCommand passes the module constant as the subprocess timeout,
        # not a bare literal — assert they stay wired together.
        seen = {}

        def run(argv, **kw):
            seen["timeout"] = kw.get("timeout")
            return subprocess.CompletedProcess(argv, 0, stdout=b"/x/claude\n")

        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(C.subprocess, "run", run)
        C.GetClaudeCommand()
        assert seen["timeout"] == C._CLAUDE_LOOKUP_TIMEOUT_SECONDS == 15


class TestGetClaudeCommand:
    """The alias/PATH resolution — subprocess and PATH lookup are monkeypatched
    so no real shell runs."""

    def _run(self, stdout=b"", returncode=0, exc=None):
        def run(argv, **kw):
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

        return run

    def test_plain_path_returned_verbatim(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(C.subprocess, "run", self._run(stdout=b"/usr/bin/claude\n"))
        assert C.GetClaudeCommand() == "/usr/bin/claude"

    def test_alias_line_is_unwrapped(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        monkeypatch.setattr(
            C.subprocess,
            "run",
            self._run(stdout=b"claude: aliased to /opt/c/claude\n"),
        )
        assert C.GetClaudeCommand() == "/opt/c/claude"

    def test_empty_shell_output_falls_back_to_path(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(C.subprocess, "run", self._run(stdout=b"", returncode=0))
        monkeypatch.setattr(
            C.shutil, "which", lambda n: "/p/claude" if n == "claude" else None
        )
        assert C.GetClaudeCommand() == "/p/claude"

    def test_timeout_falls_back_to_path(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(
            C.subprocess,
            "run",
            self._run(exc=subprocess.TimeoutExpired("sh", 15)),
        )
        monkeypatch.setattr(C.shutil, "which", lambda n: "/p/claude")
        assert C.GetClaudeCommand() == "/p/claude"

    def test_raises_when_not_in_alias_or_path(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(C.subprocess, "run", self._run(stdout=b"", returncode=1))
        monkeypatch.setattr(C.shutil, "which", lambda n: None)
        with pytest.raises(RuntimeError):
            C.GetClaudeCommand()

    def test_shell_specific_rc_sourcing(self, monkeypatch):
        seen = {}

        def run(argv, **kw):
            seen["cmd"] = argv[2]  # argv == [shell, "-c", shell_cmd]
            return subprocess.CompletedProcess(argv, 0, stdout=b"/x/claude\n")

        monkeypatch.setattr(C.subprocess, "run", run)
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        C.GetClaudeCommand()
        assert "~/.zshrc" in seen["cmd"]
        monkeypatch.setenv("SHELL", "/bin/bash")
        C.GetClaudeCommand()
        assert "~/.bashrc" in seen["cmd"]
        monkeypatch.setenv("SHELL", "/usr/bin/fish")  # unknown shell: no sourcing
        C.GetClaudeCommand()
        assert seen["cmd"] == "which claude"

    def test_unset_shell_defaults_to_bash(self, monkeypatch):
        seen = {}

        def run(argv, **kw):
            seen["shell"] = argv[0]
            seen["cmd"] = argv[2]
            return subprocess.CompletedProcess(argv, 0, stdout=b"/x/claude\n")

        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr(C.subprocess, "run", run)
        C.GetClaudeCommand()
        assert seen["shell"] == "/bin/bash"  # SHELL unset -> default bash
        assert "~/.bashrc" in seen["cmd"]


class TestWriteFileCleanup:
    def test_replace_failure_removes_temp_and_raises(self, tmp_path, monkeypatch):
        target = tmp_path / "out.json"

        def boom(_src, _dst):
            raise OSError("replace failed")

        monkeypatch.setattr(C.os, "replace", boom)
        with pytest.raises(OSError):
            C._write_file(str(target), b"data", 0o644)
        # The temp file is unlinked on failure — no litter, no partial target.
        assert list(tmp_path.glob("out.json.tmp.*")) == []
        assert not target.exists()


class TestLoadConfig:
    @pytest.fixture(autouse=True)
    def _stub_claude(self, monkeypatch):
        # DefaultConfig() shells out via GetClaudeCommand; pin it so the error
        # paths stay deterministic and don't hit a real shell.
        monkeypatch.setattr(C, "GetClaudeCommand", lambda: "claude")

    def test_config_dir_error_returns_default(self, monkeypatch):
        monkeypatch.setattr(C, "GetConfigDir", _raise_oserror)
        assert C.LoadConfig().default_program == "claude"

    def test_missing_file_writes_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = C.LoadConfig()
        path = tmp_path / ".mindflock" / "config.json"
        assert path.is_file()  # default was created on disk
        assert C.Config.from_dict(json.loads(path.read_bytes())) == cfg

    def test_read_oserror_returns_default_without_writing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg_dir = tmp_path / ".mindflock"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").mkdir()  # a dir named config.json -> open() OSError
        assert C.LoadConfig().default_program == "claude"

    def test_parse_error_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg_dir = tmp_path / ".mindflock"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text("{not valid json")
        assert C.LoadConfig().default_program == "claude"

    def test_non_object_root_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg_dir = tmp_path / ".mindflock"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text("[1, 2, 3]")
        assert C.LoadConfig().default_program == "claude"

    def test_loads_an_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg_dir = tmp_path / ".mindflock"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_bytes(
            C.Config(default_program="/x", daemon_poll_interval=500).marshal_indent()
        )
        cfg = C.LoadConfig()
        assert cfg.default_program == "/x"
        assert cfg.daemon_poll_interval == 500

    def test_missing_file_save_failure_still_returns_default(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(C, "_save_config", _raise_oserror)
        # The default-write can fail (logged as a warning); the default is still
        # returned so the app boots.
        assert C.LoadConfig().default_program == "claude"


class TestSaveConfig:
    def test_writes_marshaled_bytes_to_config_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = C.Config(
            default_program="claude", daemon_poll_interval=1000, branch_prefix="e/"
        )
        C.SaveConfig(cfg)
        path = tmp_path / ".mindflock" / "config.json"
        assert path.read_bytes() == cfg.marshal_indent()

    def test_config_dir_error_is_wrapped(self, monkeypatch):
        monkeypatch.setattr(C, "GetConfigDir", _raise_oserror)
        with pytest.raises(OSError) as ei:
            C.SaveConfig(C.Config())
        assert "failed to get config directory" in str(ei.value)

    def test_makedirs_error_is_wrapped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(C.os, "makedirs", _raise_oserror)
        with pytest.raises(OSError) as ei:
            C.SaveConfig(C.Config())
        assert "failed to create config directory" in str(ei.value)
