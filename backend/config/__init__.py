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
    marshal_indent,
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
    "GetClaudeCommand",
    "DefaultConfig",
    "LoadConfig",
    "SaveConfig",
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
    "LoadState",
    "SaveState",
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
