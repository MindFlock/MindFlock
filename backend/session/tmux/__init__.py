"""Port of the Go ``session/tmux`` package.

Re-exports the public surface so the package namespace mirrors Go's ``tmux``
package, e.g.::

    from backend.session import tmux
    s = tmux.NewTmuxSession("my session", "claude")
    tmux.CleanupSessions(cmd.make_executor())

Modules:
- :mod:`.pty`      — ``PtyFactory`` / ``Pty`` / ``MakePtyFactory`` + PTY sizing.
- :mod:`.platform` — merged Unix/Windows window-size monitor (``os.name`` guard).
- :mod:`.tmux`     — ``TmuxSession`` and friends.
"""

from __future__ import annotations

from .platform import monitor_window_size
from .pty import (
    MakePtyFactory,
    Pty,
    PtyFactory,
    PtyFile,
    Setsize,
    Winsize,
    make_pty_factory,
    set_size,
)
from .tmux import (
    CleanupSessions,
    NewTmuxSession,
    NewTmuxSessionWithDeps,
    ProgramAider,
    ProgramClaude,
    TmuxPrefix,
    TmuxSession,
    cleanup_sessions,
    new_tmux_session,
    to_mindflock_tmux_name,
)

__all__ = [
    # Program / prefix constants.
    "ProgramClaude",
    "ProgramAider",
    "TmuxPrefix",
    # Session type + constructors.
    "TmuxSession",
    "NewTmuxSession",
    "NewTmuxSessionWithDeps",
    "new_tmux_session",
    # Name sanitization.
    "to_mindflock_tmux_name",
    # Cleanup.
    "cleanup_sessions",
    "CleanupSessions",
    # PTY factory + sizing.
    "PtyFactory",
    "Pty",
    "PtyFile",
    "MakePtyFactory",
    "make_pty_factory",
    "Winsize",
    "set_size",
    "Setsize",
    # Window-size monitor.
    "monitor_window_size",
]
