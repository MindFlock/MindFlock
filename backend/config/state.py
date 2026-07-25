"""Port of the Go ``config`` package's ``state.go``.

Manages persistent application state:

* ``help_screens_seen`` -- a uint32 bitmask of which help screens the user has
  already seen.
* ``instances`` -- the serialized instance data, stored as raw JSON (Go's
  ``json.RawMessage``) embedded inside ``state.json``.

The ``State`` class implements the ``StateManager`` interface (the union of
``InstanceStorage`` and ``AppState``). State is persisted to
``~/.mindflock/state.json`` with the same 2-space-indented byte layout as
Go's ``json.MarshalIndent(state, "", "  ")``.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Optional

from backend import log
from backend.config.config import (
    GetConfigDir,
    _write_file,
)

__all__ = [
    "StateFileName",
    "InstancesFileName",
    "RawMessage",
    "CURRENT_SCHEMA_VERSION",
    "StateSchemaTooNew",
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
]

# const (
#     StateFileName     = "state.json"
#     InstancesFileName = "instances.json"
# )
StateFileName: str = "state.json"
# Declared in Go but unused (instances live inside state.json). Kept for parity.
InstancesFileName: str = "instances.json"


# json.RawMessage analogue: raw JSON bytes preserved verbatim across round-trips.
RawMessage = bytes


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------
# The version of the state.json document layout this build reads and writes.
# Emit-on-deviation: the key is only *emitted* when the version is > 1, so
# every existing (implicitly v1) state file keeps serializing byte-for-byte
# identically. A file without the key parses as v1.
CURRENT_SCHEMA_VERSION: int = 1

# Migration ladder: maps a *from*-version to a function upgrading the parsed
# JSON document one step (v -> v+1). Applied in State.from_bytes for any file
# whose version is below CURRENT_SCHEMA_VERSION. Empty today (v1 is the first
# versioned layout); a future v2 adds ``_MIGRATIONS[1] = _migrate_v1_to_v2``.
_MIGRATIONS: dict = {}


class StateSchemaTooNew(Exception):
    """state.json was written by a newer MindFlock than this one.

    Raised by :meth:`State.from_bytes` when the file's ``schema_version``
    exceeds :data:`CURRENT_SCHEMA_VERSION` (a downgrade scenario). LoadState
    handles it by moving the file aside as a ``.newer-*`` backup and starting
    from the default state — this build must never rewrite (and thereby
    field-strip) a newer document.
    """

    def __init__(self, version: int):
        self.version = version
        super().__init__(
            "state schema v{} is newer than supported v{}".format(
                version, CURRENT_SCHEMA_VERSION
            )
        )


# ---------------------------------------------------------------------------
# Cross-process state-file lock
# ---------------------------------------------------------------------------
# The lock lives on a sidecar file (state.json.lock), NOT on state.json
# itself: saves atomically os.replace() the state file, so a lock taken on
# its inode would not protect against a locker that opened the file after a
# replace. The sidecar is never replaced, only flocked.
_LOCK_MUTEX = threading.RLock()
_LOCK_DEPTH = 0
_LOCK_FD: int | None = None


@contextmanager
def state_file_lock():
    """Hold an exclusive advisory lock on the shared state file.

    Serializes state.json read-modify-write cycles against other threads in
    this process (RLock) and against co-running servers sharing the file
    (``fcntl.flock``, released by the kernel if the holder dies). Reentrant
    within a thread, so callers can wrap a whole load-merge-save section
    while ``LoadState``/``SaveState`` take the lock themselves.
    """
    global _LOCK_DEPTH, _LOCK_FD
    with _LOCK_MUTEX:
        if _LOCK_DEPTH == 0:
            lock_path = os.path.join(GetConfigDir(), StateFileName + ".lock")
            os.makedirs(os.path.dirname(lock_path), mode=0o755, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception:
                os.close(fd)
                raise
            _LOCK_FD = fd
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
            if _LOCK_DEPTH == 0 and _LOCK_FD is not None:
                try:
                    os.close(_LOCK_FD)  # closing the fd releases the flock
                finally:
                    _LOCK_FD = None


# ---------------------------------------------------------------------------
# Interfaces (Go interface types -> ABCs)
# ---------------------------------------------------------------------------
class InstanceStorage(ABC):
    """Handles instance-related operations."""

    @abstractmethod
    def SaveInstances(self, instances_json: RawMessage) -> None:
        """Save the raw instance data."""

    @abstractmethod
    def GetInstances(self) -> RawMessage:
        """Return the raw instance data."""

    @abstractmethod
    def DeleteAllInstances(self) -> None:
        """Remove all stored instances."""


class AppState(ABC):
    """Handles application-level state."""

    @abstractmethod
    def SetHelpScreensSeen(self, seen: int) -> None:
        """Update the bitmask of seen help screens."""


class StateManager(InstanceStorage, AppState, ABC):
    """Combines instance storage and app-state management."""


# ---------------------------------------------------------------------------
# Parsing helpers (used by State.from_bytes)
# ---------------------------------------------------------------------------
def _resolve_schema_version(obj: dict) -> tuple[int, dict]:
    """Validate ``schema_version`` and walk the migration ladder.

    Returns ``(version, obj)`` where ``obj`` has been upgraded one step at a
    time up to :data:`CURRENT_SCHEMA_VERSION`. A missing key means v1. Raises
    :class:`StateSchemaTooNew` for a newer document (a downgrade this build
    must not parse lossily) and ``ValueError`` for a non-integer version or a
    missing migration step.
    """
    version = obj.get("schema_version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("schema_version is not an integer")
    if version > CURRENT_SCHEMA_VERSION:
        raise StateSchemaTooNew(version)
    while version < CURRENT_SCHEMA_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            raise ValueError("no migration from state schema v{}".format(version))
        obj = migrate(obj)
        version += 1
    return version, obj


def _parse_tombstones(obj: dict) -> dict:
    """Extract the ``{title: deleted_at_epoch_seconds}`` tombstone map.

    Dual-read: a ``state.json`` without the key (every pre-L1 file, and the Go
    build's output) yields an empty map; malformed entries — non-string keys or
    non-numeric / boolean values — are dropped rather than failing the whole
    state load.
    """
    tombstones: dict = {}
    raw_tombs = obj.get("tombstones")
    if isinstance(raw_tombs, dict):
        for k, v in raw_tombs.items():
            if (
                isinstance(k, str)
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
            ):
                tombstones[k] = float(v)
    return tombstones


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class State(StateManager):
    """Application state that persists between sessions.

    JSON:
        ``{"help_screens_seen": <uint32>, "instances": <raw JSON>}``

    ``help_screens_seen`` is a bitmask; ``instances`` holds the raw JSON bytes
    (default ``b"[]"``).

    Deletion tombstones (L1, mindflock addition — not part of the Go
    contract): ``tombstones`` maps a deleted session's title to the epoch
    seconds it was deleted at, so co-running servers sharing one state file
    can converge on deletions instead of resurrecting each other's dead
    sessions. The key is only emitted when the map is non-empty, so ordinary
    state files keep serializing byte-for-byte identically to the Go build;
    a state.json without the key parses fine (empty map).

    Schema versioning (upgrade safety, same emit-on-deviation pattern):
    ``schema_version`` is only emitted when > 1, so today's v1 files stay
    byte-identical. Parsing: a missing key means v1;
    older versions are upgraded through the ``_MIGRATIONS`` ladder; a *newer*
    version raises :class:`StateSchemaTooNew` (see ``LoadState`` for the
    non-destructive downgrade handling).
    """

    def __init__(
        self,
        help_screens_seen: int = 0,
        instances_data: RawMessage = b"[]",
        tombstones: Optional[dict] = None,
        schema_version: int = CURRENT_SCHEMA_VERSION,
    ):
        # HelpScreensSeen uint32
        self.help_screens_seen: int = help_screens_seen
        # InstancesData json.RawMessage
        self.instances_data: RawMessage = instances_data
        # {title: deleted_at_epoch_seconds} — see class docstring.
        self.tombstones: dict = dict(tombstones or {})
        # Document layout version — see class docstring.
        self.schema_version: int = schema_version

    # --- JSON ----------------------------------------------------------------
    def _to_serializable(self) -> dict:
        """Build a plain object suitable for :func:`config.marshal_indent`.

        Go marshals ``InstancesData`` (a ``json.RawMessage``) inline, so the
        embedded array is re-indented as part of ``state.json``. We replicate
        that by parsing the raw bytes back into a Python value before dumping.
        """
        import json

        raw = self.instances_data
        if raw is None or len(raw) == 0:
            # Go would emit `null` for a nil RawMessage; default is "[]".
            instances_value = None
        else:
            instances_value = json.loads(raw)
        out = {}
        # Only emitted when it deviates from the implicit default (1), so
        # ordinary state files stay byte-for-byte identical to the Go layout.
        if self.schema_version > 1:
            out["schema_version"] = self.schema_version
        out["help_screens_seen"] = self.help_screens_seen
        out["instances"] = instances_value
        # Only emitted when non-empty so ordinary state files stay
        # byte-for-byte identical to the Go layout (L1 tombstones).
        if self.tombstones:
            out["tombstones"] = dict(self.tombstones)
        return out

    def marshal_indent(self) -> bytes:
        from backend.config.config import marshal_indent

        return marshal_indent(self._to_serializable())

    @classmethod
    def from_bytes(cls, data: bytes) -> "State":
        """Parse a ``state.json`` document into a :class:`State`.

        Mirrors ``json.Unmarshal(data, &state)``: ``instances`` is preserved as
        raw JSON bytes (compact, like Go's ``json.RawMessage``).
        """
        import json

        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise ValueError("state root is not an object")

        # Schema version: missing key -> 1; older versions walk the migration
        # ladder up to CURRENT; newer versions are a downgrade scenario and are
        # refused (LoadState backs the file up and refuses to overwrite it).
        version, obj = _resolve_schema_version(obj)

        seen = obj.get("help_screens_seen", 0)
        if not isinstance(seen, int) or isinstance(seen, bool):
            raise ValueError("help_screens_seen is not an integer")

        if "instances" in obj and obj["instances"] is not None:
            # Re-serialize the parsed value to compact raw JSON (RawMessage).
            instances = json.dumps(
                obj["instances"], separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        else:
            instances = b""

        tombstones = _parse_tombstones(obj)

        return cls(
            help_screens_seen=seen,
            instances_data=instances,
            tombstones=tombstones,
            schema_version=version,
        )

    # --- InstanceStorage -----------------------------------------------------
    def SaveInstances(self, instances_json: RawMessage) -> None:
        """Store the raw instance data and persist the state."""
        self.instances_data = instances_json
        SaveState(self)

    def GetInstances(self) -> RawMessage:
        """Return the in-memory raw instance data."""
        return self.instances_data

    def DeleteAllInstances(self) -> None:
        """Reset instances to ``[]`` and persist."""
        self.instances_data = b"[]"
        SaveState(self)

    # --- AppState ------------------------------------------------------------
    def SetHelpScreensSeen(self, seen: int) -> None:
        """Overwrite the bitmask of seen help screens and persist."""
        self.help_screens_seen = seen
        SaveState(self)

    # snake_case aliases
    save_instances = SaveInstances
    get_instances = GetInstances
    delete_all_instances = DeleteAllInstances
    set_help_screens_seen = SetHelpScreensSeen


# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------
def DefaultState() -> State:
    """Return the default state: ``help_screens_seen=0``, ``instances=[]``."""
    return State(help_screens_seen=0, instances_data=b"[]")


def LoadState() -> State:
    """Load the state from disk, or return the default state on any error.

    * config dir cannot be resolved -> log error, return ``DefaultState()``.
    * file missing -> write a default state (logging a warning on save failure)
      and return it.
    * other read error -> log warning, return ``DefaultState()``.
    * parse error -> log error, return ``DefaultState()`` (no write).
    * schema version newer than this build (downgrade) -> the file is moved
      aside as ``state.json.newer-<ts>`` and ``DefaultState()`` is returned.
      Chosen as the least-destructive behavior: this build refuses to parse a
      newer document lossily or to overwrite it in place (a later SaveState
      would silently strip fields it doesn't know). Sessions disappear from
      the UI until the user upgrades again and restores the backup, but no
      byte of the newer state is ever destroyed.
    """
    try:
        config_dir = GetConfigDir()
    except OSError as err:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("failed to get config directory: %v", err)
        return DefaultState()

    state_path = os.path.join(config_dir, StateFileName)
    try:
        with state_file_lock():
            with open(state_path, "rb") as f:
                data = f.read()
    except FileNotFoundError:
        # Create and save default state if file doesn't exist.
        default_state_value = DefaultState()
        try:
            SaveState(default_state_value)
        except Exception as save_err:  # noqa: BLE001
            if log.WarningLog is not None:
                log.WarningLog.Printf("failed to save default state: %v", save_err)
        return default_state_value
    except OSError as err:
        if log.WarningLog is not None:
            log.WarningLog.Printf("failed to get state file: %v", err)
        return DefaultState()

    try:
        return State.from_bytes(data)
    except StateSchemaTooNew as err:
        # Downgrade scenario: the file was written by a newer MindFlock. Move
        # it aside (same pattern as the corrupt backup, ``.newer-`` prefix) so
        # this build never rewrites — and thereby field-strips — the newer
        # document; start from a fresh default state instead.
        backup_path = "{}.newer-{}".format(state_path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            with state_file_lock():
                os.replace(state_path, backup_path)
        except OSError:
            backup_path = "(could not be backed up)"
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "state file has schema v%v but this build supports v"
                + str(CURRENT_SCHEMA_VERSION)
                + " — written by a newer MindFlock. Preserved at "
                + backup_path
                + "; starting with an empty state. Upgrade MindFlock and "
                + "restore the backup to recover your sessions.",
                err.version,
            )
        return DefaultState()
    except Exception as err:  # noqa: BLE001 - JSON parse failure
        # A corrupt state file must not silently wipe the session list: move
        # the bytes aside for recovery, complain loudly, then start fresh.
        backup_path = "{}.corrupt-{}".format(state_path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            with state_file_lock():
                os.replace(state_path, backup_path)
        except OSError:
            backup_path = "(could not be backed up)"
        if log.ErrorLog is not None:
            log.ErrorLog.Printf(
                "failed to parse state file: %v — corrupt file preserved at "
                + backup_path
                + "; starting with an empty state",
                err,
            )
        return DefaultState()


def SaveState(state: State) -> None:
    """Save the state to disk.

    Creates ``~/.mindflock`` (mode 0755) if needed and writes ``state.json``
    (mode 0644) as 2-space-indented JSON. Raises on error with Go's exact
    wrapping messages.
    """
    try:
        config_dir = GetConfigDir()
    except OSError as err:
        raise OSError("failed to get config directory: {}".format(err)) from err

    try:
        os.makedirs(config_dir, mode=0o755, exist_ok=True)
    except OSError as err:
        raise OSError("failed to create config directory: {}".format(err)) from err

    state_path = os.path.join(config_dir, StateFileName)
    data = state.marshal_indent()
    with state_file_lock():
        _write_file(state_path, data, 0o644)


# snake_case aliases for Pythonic call sites.
default_state = DefaultState
load_state = LoadState
save_state = SaveState
