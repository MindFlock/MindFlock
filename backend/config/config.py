"""Port of the Go ``config`` package's ``config.go``.

Provides configuration management for mindflock:

* loading / saving ``~/.mindflock/config.json``
* named program profiles (``Profile``)
* discovering the ``claude`` command from shell aliases or ``$PATH``
* generating a default configuration

The JSON wire format is a drop-in contract: field names, field order and the
exact 2-space-indented byte layout (including Go's HTML escaping of ``<``,
``>``, ``&`` and the line/paragraph separators) match Go's
``json.MarshalIndent(v, "", "  ")`` output byte-for-byte.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional

from backend import log

__all__ = [
    "ConfigFileName",
    "DEFAULT_PROGRAM",
    "Profile",
    "Config",
    "GetConfigDir",
    "get_config_dir",
    "GetClaudeCommand",
    "get_claude_command",
    "DefaultConfig",
    "default_config",
    "LoadConfig",
    "load_config",
    "SaveConfig",
    "save_config",
    "marshal_indent",
]

# const (
#     ConfigFileName = "config.json"
#     defaultProgram = "claude"
# )
ConfigFileName: str = "config.json"
DEFAULT_PROGRAM: str = "claude"  # Go's package-private `defaultProgram`.

# Alias-extraction regex (verbatim from Go: regexp.MustCompile).
# Matches "claude: aliased to /p", "claude -> /p", "claude = /p"; capture
# group 1 is the resolved path.
_ALIAS_REGEX = re.compile(r"(?:aliased to|->|=)\s*([^\s]+)")

# Wall-clock ceiling for the alias-resolution subprocess: sourcing a user rc
# file can hang (rc files may prompt or spawn), so the lookup falls through to
# the plain $PATH lookup after this many seconds.
_CLAUDE_LOOKUP_TIMEOUT_SECONDS: int = 15


# ---------------------------------------------------------------------------
# Go-compatible JSON marshaling
# ---------------------------------------------------------------------------
def _go_escape(s: str) -> str:
    """Apply Go's default ``encoding/json`` HTML escaping to an already-encoded
    JSON document.

    Go escapes ``<``, ``>`` and ``&`` (to ``\\u003c``, ``\\u003e``, ``\\u0026``)
    as well as U+2028 / U+2029. These characters only ever appear inside JSON
    string values, so a global replacement over the serialized document is safe.
    """
    s = s.replace("<", "\\u003c")
    s = s.replace(">", "\\u003e")
    s = s.replace("&", "\\u0026")
    s = s.replace(chr(0x2028), "\\u2028")
    s = s.replace(chr(0x2029), "\\u2029")
    return s


def marshal_indent(obj) -> bytes:
    """Equivalent of Go ``json.MarshalIndent(obj, "", "  ")``.

    Returns the indented UTF-8 bytes with no trailing newline, matching Go's
    output byte-for-byte (2-space indent, ``": "`` / ``,`` separators, non-ASCII
    kept literal, and Go's HTML escaping applied).
    """
    import json

    text = json.dumps(obj, indent=2, ensure_ascii=False, separators=(",", ": "))
    return _go_escape(text).encode("utf-8")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@dataclass
class Profile:
    """A named program configuration.

    JSON: ``{"name": <str>, "program": <str>}``.
    """

    name: str = ""
    program: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "program": self.program}

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(name=d.get("name", ""), program=d.get("program", ""))


@dataclass
class Config:
    """Application configuration.

    Field order is a wire contract; serialization preserves it. ``profiles`` is
    omitted from JSON when empty (Go ``omitempty``).
    """

    default_program: str = ""
    auto_yes: bool = False
    daemon_poll_interval: int = 0
    branch_prefix: str = ""
    profiles: List[Profile] = field(default_factory=list)

    # --- Go method: (c *Config) GetProgram() string -----------------------
    def GetProgram(self) -> str:
        """Return the program to run.

        If a profile's ``name`` equals ``default_program``, return that
        profile's ``program``; otherwise return ``default_program`` as-is.
        """
        for p in self.profiles:
            if p.name == self.default_program:
                return p.program
        return self.default_program

    # snake_case aliases
    get_program = GetProgram

    # --- JSON ----------------------------------------------------------------
    def to_dict(self) -> dict:
        d = {
            "default_program": self.default_program,
            "auto_yes": self.auto_yes,
            "daemon_poll_interval": self.daemon_poll_interval,
            "branch_prefix": self.branch_prefix,
        }
        # omitempty: only emit `profiles` when the slice is non-empty.
        if self.profiles:
            d["profiles"] = [p.to_dict() for p in self.profiles]
        return d

    def marshal_indent(self) -> bytes:
        return marshal_indent(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        raw_profiles = d.get("profiles") or []
        return cls(
            default_program=d.get("default_program", ""),
            auto_yes=d.get("auto_yes", False),
            daemon_poll_interval=d.get("daemon_poll_interval", 0),
            branch_prefix=d.get("branch_prefix", ""),
            profiles=[Profile.from_dict(p) for p in raw_profiles],
        )


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------
_CONFIG_DIR_NAME = ".mindflock"


def GetConfigDir() -> str:
    """Return the path to the application's configuration directory (``~/.mindflock``).

    Honors ``$HOME``. Raises ``OSError`` if the home directory cannot be resolved
    (Go returns a wrapped error).
    """
    home_dir = _user_home_dir()
    if home_dir is None:
        raise OSError("failed to get config home directory: $HOME is not defined")
    return os.path.join(home_dir, _CONFIG_DIR_NAME)


def _user_home_dir() -> Optional[str]:
    """Mirror Go's ``os.UserHomeDir()`` on unix: return ``$HOME`` (if set and
    non-empty); ``None`` otherwise.
    """
    home = os.environ.get("HOME", "")
    if home != "":
        return home
    return None


def _claude_lookup_command(shell: str) -> str:
    """Build the ``sh -c`` command string that resolves ``claude``.

    Forces the shell to load the user's profile so shell aliases are visible,
    then runs ``which claude``: for zsh source ``~/.zshrc``, for bash source
    ``~/.bashrc``. An unrecognized shell skips rc sourcing and runs
    ``which claude`` directly.
    """
    if "zsh" in shell:
        return "source ~/.zshrc &>/dev/null || true; which claude"
    elif "bash" in shell:
        return "source ~/.bashrc &>/dev/null || true; which claude"
    else:
        return "which claude"


def GetClaudeCommand() -> str:
    """Find the ``claude`` command in the user's shell.

    Resolution order:
      1. Shell alias resolution via ``which claude`` (sourcing the user's rc
         file first for zsh/bash), parsing any alias line with the alias regex.
      2. ``$PATH`` lookup.

    Raises ``RuntimeError("claude command not found in aliases or PATH")`` if
    both fail (Go returns ``"", error``).
    """
    shell = os.environ.get("SHELL", "")
    if shell == "":
        shell = "/bin/bash"  # Default to bash if SHELL is not set.

    shell_cmd = _claude_lookup_command(shell)

    # exec.Command(shell, "-c", shellCmd); cmd.Output() captures stdout only.
    # A timeout (see the constant) falls through to the plain PATH lookup below.
    try:
        completed = subprocess.run(
            [shell, "-c", shell_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_CLAUDE_LOOKUP_TIMEOUT_SECONDS,
        )
        output = completed.stdout
        # Go's cmd.Output() returns err != nil on non-zero exit; treat that as
        # "no usable output" (fall through to PATH lookup).
        if completed.returncode == 0 and len(output) > 0:
            path = output.decode("utf-8", "replace").strip()
            if path != "":
                # Detect an alias definition and extract the real path.
                matches = _ALIAS_REGEX.search(path)
                if matches is not None:
                    path = matches.group(1)
                return path
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Shell could not be executed (or hung); fall through to PATH lookup.
        pass

    # Otherwise, try to find in PATH directly (exec.LookPath equivalent).
    claude_path = shutil.which("claude")
    if claude_path is not None:
        return claude_path

    raise RuntimeError("claude command not found in aliases or PATH")


def DefaultConfig() -> Config:
    """Return the default configuration.

    ``default_program`` is :func:`GetClaudeCommand`'s find folded back to the
    provider name (it reports an absolute path — ``which`` output — and storing
    that verbatim put a bare ``/opt/homebrew/bin/claude`` in the New Session
    dialog's agent list on a first run), or the literal ``"claude"`` on failure.
    A path no provider recognises is still stored as-is: for a custom agent the
    exact string IS the launch command. ``branch_prefix`` is
    ``"{lower(user)}/"``, or ``"session/"`` if the user cannot be determined.
    """
    try:
        program = GetClaudeCommand()
    except Exception as err:  # noqa: BLE001 - mirror Go's broad error handling
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to get claude command: %v", err)
        program = DEFAULT_PROGRAM
    try:
        from backend import providers

        program = providers.normalize_program(program) or program
    except Exception:  # noqa: BLE001 — config must load without the registry
        pass

    return Config(
        default_program=program,
        auto_yes=False,
        daemon_poll_interval=1000,
        branch_prefix=_default_branch_prefix(),
    )


def _default_branch_prefix() -> str:
    """Replicate the IIFE that builds ``BranchPrefix`` in Go's ``DefaultConfig``.

    Returns ``"{lower(username)}/"``; falls back to ``"session/"`` and logs an
    error if the current user cannot be resolved or has an empty username.
    """
    try:
        username = getpass.getuser()
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to get current user: %v", err)
        return "session/"

    if not username:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to get current user: %v", "empty username")
        return "session/"

    return "{}/".format(username.lower())


def LoadConfig() -> Config:
    """Load configuration from disk, or return defaults.

    * config dir cannot be resolved -> log error, return ``DefaultConfig()``.
    * file missing -> write a default config (logging a warning if the write
      fails) and return it.
    * other read error -> log warning, return ``DefaultConfig()`` (no write).
    * parse error -> log error, return ``DefaultConfig()`` (no write).
    """
    import json

    try:
        config_dir = GetConfigDir()
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to get config directory: %v", err)
        return DefaultConfig()

    config_path = os.path.join(config_dir, ConfigFileName)
    try:
        with open(config_path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        # Create and save default config if file doesn't exist.
        default_cfg = DefaultConfig()
        try:
            _save_config(default_cfg)
        except Exception as save_err:  # noqa: BLE001
            if log.WarningLog is not None:
                log.WarningLog.Printf("failed to save default config: %v", save_err)
        return default_cfg
    except OSError as err:
        if log.WarningLog is not None:
            log.WarningLog.Printf("failed to get config file: %v", err)
        return DefaultConfig()

    try:
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise ValueError("config root is not an object")
    except (ValueError, json.JSONDecodeError) as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to parse config file: %v", err)
        return DefaultConfig()

    return Config.from_dict(parsed)


def _save_config(config: Config) -> None:
    """Save the configuration to disk (Go's package-private ``saveConfig``).

    Creates ``~/.mindflock`` (mode 0755) if needed and writes
    ``config.json`` (mode 0644) as 2-space-indented JSON. Raises on error with
    Go's exact wrapping messages.
    """
    try:
        config_dir = GetConfigDir()
    except OSError as err:
        raise OSError("failed to get config directory: {}".format(err)) from err

    try:
        os.makedirs(config_dir, mode=0o755, exist_ok=True)
    except OSError as err:
        raise OSError("failed to create config directory: {}".format(err)) from err

    config_path = os.path.join(config_dir, ConfigFileName)
    data = config.marshal_indent()

    # os.WriteFile(path, data, 0644)
    _write_file(config_path, data, 0o644)


def SaveConfig(config: Config) -> None:
    """Public export of :func:`_save_config` (Go's ``SaveConfig``)."""
    _save_config(config)


def _write_file(path: str, data: bytes, mode: int) -> None:
    """Atomically replace ``path`` with ``data`` (temp file + ``os.replace``).

    Unlike Go's ``os.WriteFile`` (truncate-in-place), a crash mid-write can
    never leave a truncated file behind: the previous contents survive until
    the fsynced temp file is renamed over them. ``mode`` is applied to the
    new file explicitly (not umask-masked).
    """
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".tmp.", dir=dir_name
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# snake_case aliases for Pythonic call sites.
get_config_dir = GetConfigDir
get_claude_command = GetClaudeCommand
default_config = DefaultConfig
load_config = LoadConfig
save_config = SaveConfig
