"""Configured IDE integration — which editor opens workspaces.

Productization: MindFlock historically hardcoded Cursor everywhere (launching,
window focus/close/maximize, session auto-adopt). Every consumer now resolves
the editor through this module, so linking a different IDE is one Settings
field (Settings → Advanced → IDE) instead of a code change.

Resolution order (the same chain as every other setting)::

    $MINDFLOCK_IDE  →  settings.json platform.ide_command  →  "cursor"

The command may include arguments (it is shlex-split), e.g.
``flatpak run com.visualstudio.code``.

Known editors live in a capability registry (:class:`IdeSpec`): display name,
launch kind (``gui`` opens its own window; ``terminal`` must be wrapped in a
terminal emulator), the window-title needle used for focus/close/maximize, the
VS Code-family storage dir used for open-folder auto-adopt, and the macOS app
bundle name used for the ``open -a`` launch fallback. Unknown commands still
work — they get a synthesized GUI spec with no window/storage capabilities.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import List, Optional

from backend.config import settings as _settings

__all__ = [
    "IdeSpec",
    "ide_command",
    "ide_argv",
    "ide_name",
    "ide_kind",
    "ide_spec",
    "ide_window_needle",
    "ide_storage_dirname",
    "known_ide_specs",
    "spec_for",
]


@dataclass(frozen=True)
class IdeSpec:
    """Capabilities of a known editor, keyed by its launch-command basename."""

    command: str
    """Canonical launch-command basename (``cursor``, ``pycharm``, ``nvim``)."""

    name: str
    """Human display name for UI labels ("Cursor", "PyCharm", "Neovim")."""

    kind: str = "gui"
    """``gui`` (opens its own window) | ``terminal`` (runs inside a terminal)."""

    window_needle: Optional[str] = None
    """Substring expected in the editor's window titles (find/focus/close), or
    ``None`` when title-based window ops don't apply (JetBrains titles carry the
    project name, not the product; terminal editors have no own window)."""

    storage_dirname: Optional[str] = None
    """VS Code-family user-data dir (``~/.config/<name>/User/…``) whose
    ``globalStorage/storage.json`` records open folders, or ``None`` when the
    editor has no such storage (disables open-folder discovery / auto-adopt)."""

    macos_app: Optional[str] = None
    """macOS app-bundle name (``/Applications/<name>.app``) for the
    ``open -a <name>`` launch/focus fallback when the CLI shim is missing."""


# Registry of known editors, in the order the IDE picker should list them.
_SPECS = (
    # VS Code family — full window ops + storage-based auto-adopt.
    IdeSpec("cursor", "Cursor", "gui", "Cursor", "Cursor", "Cursor"),
    IdeSpec(
        "code", "VS Code", "gui", "Visual Studio Code", "Code", "Visual Studio Code"
    ),
    IdeSpec(
        "code-insiders",
        "VS Code Insiders",
        "gui",
        "Visual Studio Code - Insiders",
        "Code - Insiders",
        "Visual Studio Code - Insiders",
    ),
    IdeSpec("codium", "VSCodium", "gui", "VSCodium", "VSCodium", "VSCodium"),
    IdeSpec("windsurf", "Windsurf", "gui", "Windsurf", "Windsurf", "Windsurf"),
    # Other GUI editors — no VS Code-style storage (no auto-adopt).
    IdeSpec("zed", "Zed", "gui", "Zed", None, "Zed"),
    IdeSpec("subl", "Sublime Text", "gui", "Sublime Text", None, "Sublime Text"),
    # JetBrains — window titles carry the project, not the product, so no
    # needle-based window ops; launched via their CLI shims.
    IdeSpec("idea", "IntelliJ IDEA", "gui", None, None, "IntelliJ IDEA"),
    IdeSpec("pycharm", "PyCharm", "gui", None, None, "PyCharm"),
    IdeSpec("webstorm", "WebStorm", "gui", None, None, "WebStorm"),
    IdeSpec("goland", "GoLand", "gui", None, None, "GoLand"),
    IdeSpec("clion", "CLion", "gui", None, None, "CLion"),
    # Terminal editors — launched inside a terminal emulator window.
    IdeSpec("nvim", "Neovim", "terminal"),
    IdeSpec("vim", "Vim", "terminal"),
    IdeSpec("emacs", "Emacs", "terminal"),
    IdeSpec("hx", "Helix", "terminal"),
    IdeSpec("micro", "Micro", "terminal"),
)

_KNOWN = {spec.command: spec for spec in _SPECS}


def known_ide_specs() -> List[IdeSpec]:
    """All registry entries, in display order (installed or not)."""
    return list(_SPECS)


def spec_for(command: str) -> Optional[IdeSpec]:
    """The registry entry for a launch-command basename, or ``None``."""
    return _KNOWN.get(os.path.basename(str(command)).lower())


def ide_command() -> str:
    """The configured editor launch command (default ``cursor``)."""
    v = _settings.resolve_str(
        env="MINDFLOCK_IDE",
        settings_getter=lambda s: s.platform.ide_command,
        default="cursor",
    )
    return str(v).strip() or "cursor"


def ide_argv() -> list:
    """The launch command as an argv list (append the workspace path)."""
    cmd = ide_command()
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = [cmd]
    return argv or ["cursor"]


def _basename() -> str:
    argv = ide_argv()
    return os.path.basename(argv[0]).lower() if argv else "cursor"


def ide_spec() -> IdeSpec:
    """The registry entry for the configured editor. Unknown commands get a
    synthesized GUI spec (capitalized name, no window/storage capabilities)."""
    known = _KNOWN.get(_basename())
    if known:
        return known
    base = _basename()
    name = (base[:1].upper() + base[1:]) if base else "IDE"
    return IdeSpec(command=base or "ide", name=name)


def ide_name() -> str:
    """Human display name for UI labels ("Cursor", "VS Code", …). Unknown
    editors get their capitalized command basename."""
    return ide_spec().name


def ide_kind() -> str:
    """Launch kind of the configured editor: ``gui`` | ``terminal``."""
    return ide_spec().kind


def ide_window_needle() -> str:
    """Substring expected in the editor's window titles, used to find/focus/
    close its windows (best-effort — most editors include their product name).
    Editors without a usable needle fall back to their display name, which
    simply matches nothing (window ops degrade to no-ops)."""
    spec = ide_spec()
    return spec.window_needle if spec.window_needle else spec.name


def ide_storage_dirname() -> Optional[str]:
    """VS Code-family user-data directory name (``~/.config/<name>/User/…``)
    whose ``globalStorage/storage.json`` records open folders, or ``None`` when
    the configured editor has no such storage (auto-adopt then finds nothing)."""
    return ide_spec().storage_dirname
