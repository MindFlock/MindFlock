"""Import this package without importing the rest of MindFlock.

``backend.session``'s ``__init__`` pulls in the whole session engine (git,
tmux, storage). The host daemon and the in-session ``tmux`` shim need none
of it — and on Windows, until every engine module is POSIX-free, some of it
does not import at all. :func:`prepare` registers ``backend.session`` as a
bare namespace package (its ``__path__`` only) before anything imports it,
so ``backend.session.winmux.*`` loads alone: this package imports only the
standard library, ``backend.compat``, pyte, pywinpty / ptyprocess and
psutil. It is also why the daemon stays small (it outlives the server and
holds every session).
"""

from __future__ import annotations

import os
import sys
import types

__all__ = ["prepare", "PREPARE_CODE", "HOST_CODE", "CLI_CODE"]


def prepare(root: str) -> None:
    if root not in sys.path:
        sys.path.insert(0, root)
    if "backend.session" in sys.modules:
        return
    import backend  # noqa: F401 - light: reads the version only

    pkg = types.ModuleType("backend.session")
    pkg.__path__ = [os.path.join(root, "backend", "session")]
    pkg.__package__ = "backend.session"
    sys.modules["backend.session"] = pkg


#: The same as :func:`prepare`, as source for ``python -c`` and the shim
#: file: importing this module itself would import ``backend.session``
#: first — exactly what it exists to avoid. ``sys.argv[1]`` is the root.
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
