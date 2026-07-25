"""Port of the Go ``session`` package (mindflock/session).

Re-exports the public surface of ``instance.go`` and ``storage.go`` so the
package namespace mirrors the Go package, e.g.::

    from backend import session

    inst = session.NewInstance(session.InstanceOptions(title="x", path="."))
    storage = session.NewStorage(state)
    storage.SaveInstances([inst])

The ``git`` and ``tmux`` subpackages remain available as ``session.git`` /
``session.tmux``.

Module layout (to avoid the circular import implied by Go's single-package
design):
  * :mod:`.storage` owns the serialization dataclasses (``InstanceData`` /
    ``GitWorktreeData`` / ``DiffStatsData``) and the ``Status`` enum, plus the
    ``Storage`` class.
  * :mod:`.instance` defines ``Instance`` and imports the dataclasses from
    storage; storage imports ``Instance`` / ``FromInstanceData`` lazily.
"""

from __future__ import annotations

from backend.session import git, tmux
from backend.session.instance import (
    FromInstanceData,
    Instance,
    InstanceOptions,
    NewInstance,
    from_instance_data,
    new_instance,
)
from backend.session.storage import (
    DiffStatsData,
    GitWorktreeData,
    InstanceData,
    Loading,
    NewStorage,
    Paused,
    Ready,
    Running,
    Status,
    Storage,
    new_storage,
)

__all__ = [
    # Subpackages.
    "git",
    "tmux",
    # storage.go
    "Status",
    "Running",
    "Ready",
    "Loading",
    "Paused",
    "InstanceData",
    "GitWorktreeData",
    "DiffStatsData",
    "Storage",
    "NewStorage",
    "new_storage",
    # instance.go
    "Instance",
    "InstanceOptions",
    "NewInstance",
    "new_instance",
    "FromInstanceData",
    "from_instance_data",
]
