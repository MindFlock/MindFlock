"""The MindFlock session host: tmux's job on native Windows
(docs/windows.md).

A per-user daemon (:mod:`.host`) holds every session — a program in a
ConPTY plus a pyte screen model (:mod:`.screen`) — and answers the tmux
subcommands MindFlock uses (:mod:`.commands`) with tmux's output, formats
(:mod:`.formats`) and exit codes; :mod:`.client` is what
``backend.session.mux`` calls; :mod:`.keys` turns ``send-keys`` names into
bytes. The pseudo-terminal is pluggable (:mod:`.ptys`): ConPTY on Windows,
``ptyprocess`` under the Linux tests, so the whole host is tested on Linux.

This ``__init__`` imports nothing: the daemon and the in-session ``tmux``
shim load the package without the session engine (:mod:`._bootstrap`).

Where it differs from tmux, on purpose:

* **Namespaces, not servers** — ``-L`` / ``-S`` select a namespace inside
  the one host (see :mod:`.proto`); ``kill-server`` ends only its own.
* **Clients see the program's own output**, not a tmux redraw: a new
  client gets a full repaint (with up to 1000 lines of history first, for
  its scrollback), then the live bytes, minus terminal queries (the host
  answers those itself) and the sequences tmux never passes on.
* **Key bindings are stored, never run**: they configure tmux's own
  client, which does not exist here; the browser handles the wheel.
  ``mouse``/``status`` are stored only.
* **One window, one pane per session**; ``new-window`` / ``split-window``
  are unknown commands. ``new-session`` without ``-d`` from a program does
  not attach (there is no terminal); the console CLI does attach.
* ``default-terminal`` is ``xterm-256color`` (what ConPTY and pyte speak),
  ``history-limit`` set on a session applies to its existing pane too, and
  ``pane_current_command`` on Windows is the pane's youngest descendant.
"""
