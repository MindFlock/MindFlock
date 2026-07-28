"""Port of the Go ``config`` package (mindflock/config).

Re-exports the public surface of ``config.go`` and ``state.go`` so the package
namespace mirrors the Go package, e.g.::

    from backend import config
    cfg = config.LoadConfig()
    config.SaveConfig(cfg)
    state = config.LoadState()
    state.SetHelpScreensSeen(1)
"""

from __future__ import annotations

from backend.config.config import (
    Config,
    ConfigFileName,
    DEFAULT_PROGRAM,
    DefaultConfig,
    GetClaudeCommand,
    GetConfigDir,
    LoadConfig,
    Profile,
    SaveConfig,
    default_config,
    get_claude_command,
    get_config_dir,
    load_config,
    marshal_indent,
    save_config,
)
from backend.config.state import (
    AppState,
    DefaultState,
    InstanceStorage,
    InstancesFileName,
    LoadState,
    RawMessage,
    SaveState,
    State,
    StateFileName,
    StateManager,
    default_state,
    load_state,
    save_state,
    state_file_lock,
)
from backend.config.settings import (
    Settings,
    SettingsFileName,
    load_settings,
    resolve,
    resolve_bool,
    resolve_int,
    resolve_path,
    resolve_str,
    save_settings,
    settings_path,
    update_settings,
)

__all__ = [
    # config.go
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
    # state.go
    "StateFileName",
    "InstancesFileName",
    "RawMessage",
    "InstanceStorage",
    "AppState",
    "StateManager",
    "State",
    "DefaultState",
    "default_state",
    "LoadState",
    "load_state",
    "SaveState",
    "save_state",
    "state_file_lock",
    # settings.py (user settings store + resolver)
    "Settings",
    "SettingsFileName",
    "settings_path",
    "load_settings",
    "save_settings",
    "update_settings",
    "resolve",
    "resolve_str",
    "resolve_int",
    "resolve_bool",
    "resolve_path",
]
