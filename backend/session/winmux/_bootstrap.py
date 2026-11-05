"""Import this package without importing the rest of MindFlock.

``backend.session``'s ``__init__`` pulls in the whole session engine (git,
tmux, storage). The host daemon and the in-session ``tmux`` shim need none
of it — and on Windows, until every engine module is POSIX-free, some of it
does not import at all. :data:`PREPARE_CODE` registers ``backend.session`` as a
bare namespace package (its ``__path__`` only) before anything imports it,
so ``backend.session.winmux.*`` loads alone: this package imports only the
standard library, ``backend.compat``, pyte, pywinpty / ptyprocess and
psutil. It is also why the daemon stays small (it outlives the server and
holds every session).
"""

from __future__ import annotations

__all__ = ["PREPARE_CODE", "HOST_CODE", "CLI_CODE"]


#: Source, not a function, for ``python -c`` and the shim file: importing
#: this module to call a function would import ``backend.session`` first —
#: exactly what it exists to avoid. ``sys.argv[1]`` is the root (popped).
PREPARE_CODE = (
    "import os, sys, types\n"
    "_r = sys.argv.pop(1)\n"
    "sys.path.insert(0, _r)\n"
    "import backend\n"
    "if 'backend.session' not in sys.modules:\n"
    "    _m = types.ModuleType('backend.session')\n"
    "    _m.__path__ = [os.path.join(_r, 'backend', 'session')]\n"
    "    _m.__package__ = 'backend.session'\n"
    "    sys.modules['backend.session'] = _m\n"
)

#: ``python -c HOST_CODE <root> <run_dir>`` starts the daemon.
HOST_CODE = (
    PREPARE_CODE
    + "from backend.session.winmux import host\n"
    + "sys.exit(host.main(sys.argv[1:]))\n"
)

#: The in-session ``tmux`` shim: ``python winmux_cli.py <root> <tmux args>``.
CLI_CODE = (
    PREPARE_CODE
    + "from backend.session.winmux.__main__ import main\n"
    + "sys.exit(main(sys.argv[1:]))\n"
)
