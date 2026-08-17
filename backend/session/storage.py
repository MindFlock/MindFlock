"""Port of the Go ``session/storage.go``.

Provides persistent storage for Claude Code instances by serializing/deserial-
izing them to/from JSON via a :class:`config.InstanceStorage` backend.

This module owns the serialization dataclasses (``InstanceData`` /
``GitWorktreeData`` / ``DiffStatsData``) and the ``Status`` enum. They live here
(rather than in ``instance.py``) so that ``instance.py`` can import them without
creating a circular import: ``storage.py`` only imports :class:`Instance` /
``FromInstanceData`` lazily (function-local) for the ``from_instance_data``
round-trip used by :meth:`Storage.LoadInstances`.

Byte-exact JSON contract (see contracts §d):

  ``InstanceData`` fields, in order: ``title, path, branch, status (int),
  height, width, created_at, updated_at (RFC3339), auto_yes, program,
  worktree (object, always present), diff_stats (object, always present)``.

  ``GitWorktreeData``: ``repo_path, worktree_path, session_name, branch_name,
  base_commit_sha, is_existing_branch``.

  ``DiffStatsData``: ``added, removed, content``.

  ``Status``: ``Running=0, Ready=1, Loading=2, Paused=3``.

The inner instances array is serialized with ``json.Marshal`` (compact) like Go;
``config.State`` re-indents it when it embeds the array in ``state.json``.
"""

from __future__ import annotations

import datetime as _datetime
import enum
import json
from dataclasses import dataclass, field
from typing import List, Optional

from backend.config.state import state_file_lock


# ---------------------------------------------------------------------------
# Status enum (iota): Running=0, Ready=1, Loading=2, Paused=3
# ---------------------------------------------------------------------------
class Status(enum.IntEnum):
    """Instance status (Go ``Status`` iota).

    Integer values are a wire contract (serialized to/from JSON as ints):

      * ``Running`` (0): instance running, Claude is working.
      * ``Ready``   (1): ready for user interaction (waiting for input).
      * ``Loading`` (2): loading / starting up.
      * ``Paused``  (3): worktree removed but branch preserved.
    """

    Running = 0
    Ready = 1
    Loading = 2
    Paused = 3


# Module-level aliases mirroring the Go package-level constants so callers can
# write ``session.Running`` etc.
Running = Status.Running
Ready = Status.Ready
Loading = Status.Loading
Paused = Status.Paused


# ---------------------------------------------------------------------------
# RFC3339 (Go time.Time JSON) helpers
# ---------------------------------------------------------------------------
# Go's encoding/json marshals time.Time as RFC3339 with a trimmed nanosecond
# fraction (RFC3339Nano semantics): up to 9 fractional digits, trailing zeros
# stripped, the whole fraction omitted when zero. UTC renders as a "Z" suffix;
# other zones render as a "+HH:MM"/"-HH:MM" offset. The zero value renders as
# "0001-01-01T00:00:00Z".

# The Go zero time (time.Time{}) — used for fields whose Go counterpart is the
# zero value.
ZERO_TIME = _datetime.datetime(1, 1, 1, 0, 0, 0, tzinfo=_datetime.timezone.utc)


def format_rfc3339(t: _datetime.datetime) -> str:
    """Format a ``datetime`` the way Go's ``json.Marshal(time.Time)`` does.

    Replicates RFC3339Nano with trailing-zero trimming:
      * ``2025-06-18T14:30:45.123456789Z`` (full precision, UTC)
      * ``2025-06-18T14:30:45.5Z``         (trailing zeros stripped)
      * ``2025-06-18T14:30:45Z``           (no fraction)
      * ``2026-01-02T09:05:00-05:00``      (offset zone)
      * ``0001-01-01T00:00:00Z``           (zero value)

    A naive ``datetime`` (no tzinfo) is treated as UTC (rendered with ``Z``),
    matching how the Go zero value and any UTC time are emitted.
    """
    # Base "YYYY-MM-DDTHH:MM:SS".
    base = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}".format(
        t.year, t.month, t.day, t.hour, t.minute, t.second
    )

    # Fractional part. datetime resolution is microseconds (6 digits); Go would
    # emit up to 9. We pad the microseconds to 9 nanosecond digits and trim
    # trailing zeros, matching Go's output for any sub-second value reachable
    # from a Python datetime.
    nanos = t.microsecond * 1000
    frac = ""
    if nanos != 0:
        digits = "{:09d}".format(nanos).rstrip("0")
        frac = "." + digits

    # Timezone suffix.
    offset = t.utcoffset()
    if offset is None or offset == _datetime.timedelta(0):
        zone = "Z"
    else:
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        zone = "{}{:02d}:{:02d}".format(sign, total_minutes // 60, total_minutes % 60)

    return base + frac + zone


def parse_rfc3339(s: str) -> _datetime.datetime:
    """Parse an RFC3339 timestamp (as produced by :func:`format_rfc3339`).

    Accepts a trailing ``Z`` (UTC) or a ``+HH:MM``/``-HH:MM`` offset, and an
    optional fractional-seconds component of any length (truncated to microsecond
    precision, like Python's ``datetime``). The Go zero value
    ``0001-01-01T00:00:00Z`` round-trips to :data:`ZERO_TIME`.
    """
    text = s
    # Normalize trailing Z to an explicit +00:00 offset for fromisoformat, and
    # clamp any over-9-digit / 7-9-digit fraction to 6 digits (microseconds).
    if text.endswith("Z") or text.endswith("z"):
        body = text[:-1]
        tz = "+00:00"
    else:
        # Find the offset start: the last '+' or '-' after the time portion.
        # Year is 4 digits then 'T', so search from index 11 onward.
        idx = max(text.rfind("+"), text.rfind("-"))
        # Guard: an offset sign must come after the 'T' (date also has '-').
        if idx > 10:
            body = text[:idx]
            tz = text[idx:]
        else:
            body = text
            tz = ""

    # Truncate fractional seconds to 6 digits (microseconds).
    if "." in body:
        head, frac = body.split(".", 1)
        frac = frac[:6]
        body = head + ("." + frac if frac else "")

    iso = body + tz
    try:
        return _datetime.datetime.fromisoformat(iso)
    except ValueError:
        # Fallback: strptime without fraction.
        fmt = "%Y-%m-%dT%H:%M:%S"
        if tz in ("", "+00:00"):
            dt = _datetime.datetime.strptime(body.split(".")[0], fmt)
            return dt.replace(tzinfo=_datetime.timezone.utc)
        raise


# ---------------------------------------------------------------------------
# Serialization dataclasses
# ---------------------------------------------------------------------------
@dataclass
class DiffStatsData:
    """Serializable git diff statistics (Go ``DiffStatsData``).

    JSON keys: ``added``, ``removed``, ``content``.
    """

    added: int = 0
    removed: int = 0
    content: str = ""

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "removed": self.removed,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "DiffStatsData":
        if d is None:
            return cls()
        return cls(
            added=d.get("added", 0),
            removed=d.get("removed", 0),
            content=d.get("content", ""),
        )


@dataclass
class GitWorktreeData:
    """Serializable git worktree metadata (Go ``GitWorktreeData``).

    JSON keys: ``repo_path``, ``worktree_path``, ``session_name``,
    ``branch_name``, ``base_commit_sha``, ``is_existing_branch``.
    """

    repo_path: str = ""
    worktree_path: str = ""
    session_name: str = ""
    branch_name: str = ""
    base_commit_sha: str = ""
    is_existing_branch: bool = False

    def to_dict(self) -> dict:
        return {
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "session_name": self.session_name,
            "branch_name": self.branch_name,
            "base_commit_sha": self.base_commit_sha,
            "is_existing_branch": self.is_existing_branch,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "GitWorktreeData":
        if d is None:
            return cls()
        return cls(
            repo_path=d.get("repo_path", ""),
            worktree_path=d.get("worktree_path", ""),
            session_name=d.get("session_name", ""),
            branch_name=d.get("branch_name", ""),
            base_commit_sha=d.get("base_commit_sha", ""),
            is_existing_branch=d.get("is_existing_branch", False),
        )


@dataclass
class InstanceData:
    """Serializable form of an :class:`Instance` (Go ``InstanceData``).

    Field/JSON-key order is a wire contract (contracts §d):
    ``title, path, branch, status, height, width, created_at, updated_at,
    auto_yes, program, worktree, diff_stats``.

    ``worktree`` and ``diff_stats`` are *always present* objects (zero-value if
    the source instance had no worktree / diff stats).
    """

    title: str = ""
    path: str = ""
    branch: str = ""
    status: Status = Status.Running
    height: int = 0
    width: int = 0
    created_at: _datetime.datetime = field(default_factory=lambda: ZERO_TIME)
    updated_at: _datetime.datetime = field(default_factory=lambda: ZERO_TIME)
    auto_yes: bool = False
    program: str = ""
    launch_args: tuple[str, ...] = ()
    worktree: GitWorktreeData = field(default_factory=GitWorktreeData)
    diff_stats: DiffStatsData = field(default_factory=DiffStatsData)
    # Provisioned-workspace extension. Only emitted to JSON when ``provisioned``
    # is True so ordinary sessions serialize to the minimal shape.
    provisioned: bool = False
    workspace_strategy: str = "worktree"
    # In-place mode: session runs directly in an existing repo (no worktree).
    # Only emitted to JSON when True so ordinary sessions stay byte-compatible.
    in_place: bool = False
    # Branch the session's worktree was cut from (per-session diff/stage base,
    # K1). Only emitted when set; absent in pre-K1 state.json entries — readers
    # fall back to origin/HEAD -> main/master -> configured base.
    base_branch: str = ""
    # Auth profile the session's agent runs under ("" = inherit the global
    # default; "default" = explicitly the CLI's own login). Only emitted when
    # set, so pre-feature entries serialize unchanged.
    profile_id: str = ""

    def to_dict(self) -> dict:
        """Build the JSON-ready dict, preserving Go field order exactly."""
        d = {
            "title": self.title,
            "path": self.path,
            "branch": self.branch,
            "status": int(self.status),
            "height": self.height,
            "width": self.width,
            "created_at": format_rfc3339(self.created_at),
            "updated_at": format_rfc3339(self.updated_at),
            "auto_yes": self.auto_yes,
            "program": self.program,
            "worktree": self.worktree.to_dict(),
            "diff_stats": self.diff_stats.to_dict(),
        }
        if self.provisioned:
            d["provisioned"] = True
            d["workspace_strategy"] = self.workspace_strategy
        if self.launch_args:
            d["launch_args"] = list(self.launch_args)
        if self.in_place:
            d["in_place"] = True
        if self.base_branch:
            d["base_branch"] = self.base_branch
        if self.profile_id:
            d["profile_id"] = self.profile_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "InstanceData":
        return cls(
            title=d.get("title", ""),
            path=d.get("path", ""),
            branch=d.get("branch", ""),
            status=Status(d.get("status", 0)),
            height=d.get("height", 0),
            width=d.get("width", 0),
            created_at=(
                parse_rfc3339(d["created_at"]) if d.get("created_at") else ZERO_TIME
            ),
            updated_at=(
                parse_rfc3339(d["updated_at"]) if d.get("updated_at") else ZERO_TIME
            ),
            auto_yes=d.get("auto_yes", False),
            program=d.get("program", ""),
            launch_args=tuple(
                a for a in d.get("launch_args", ()) if isinstance(a, str)
            ),
            worktree=GitWorktreeData.from_dict(d.get("worktree")),
            diff_stats=DiffStatsData.from_dict(d.get("diff_stats")),
            provisioned=bool(d.get("provisioned", False)),
            workspace_strategy=d.get("workspace_strategy") or "worktree",
            in_place=bool(d.get("in_place", False)),
            # Absent when the session has no recorded cut-point -> "" (readers
            # resolve a base via the fallback chain).
            base_branch=d.get("base_branch", "") or "",
            profile_id=d.get("profile_id", "") or "",
        )


def _marshal_instances(data: List[InstanceData]) -> bytes:
    """Marshal a list of ``InstanceData`` the way Go's ``json.Marshal`` does.

    Compact separators (no spaces), non-ASCII kept literal, and Go's HTML
    escaping (``<``, ``>``, ``&``) applied so the bytes match Go byte-for-byte.
    """
    from backend.config.config import _go_escape

    text = json.dumps(
        [d.to_dict() for d in data],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _go_escape(text).encode("utf-8")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class Storage:
    """Saves and loads instances using a :class:`config.InstanceStorage` backend.

    Wraps a state backend (typically ``config.State``) that knows how to persist
    the raw instances JSON. Mirrors the Go ``Storage`` struct.
    """

    def __init__(self, state) -> None:
        self.state = state

    # --- SaveInstances ------------------------------------------------------
    def SaveInstances(self, instances: List) -> None:
        """Persist the started instances (Go ``SaveInstances``).

        Filters out instances where ``Started()`` is False, converts the rest to
        ``InstanceData`` (which sets ``updated_at`` to now), marshals to compact
        JSON, and hands the bytes to the backend.

        Raises ``RuntimeError('failed to marshal instances: ...')`` on a marshal
        failure; backend errors propagate unchanged.

        The whole-array overwrite runs under the cross-process state lock so a
        concurrent Delete/Update (which lock their load-mutate-save) can't slip
        in between (lost update). The lock is reentrant, so nesting with the
        backend's own locking is safe.
        """
        with state_file_lock():
            data: List[InstanceData] = []
            for instance in instances:
                if instance.Started():
                    data.append(instance.ToInstanceData())

            try:
                json_data = _marshal_instances(data)
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(
                    "failed to marshal instances: {}".format(err)
                ) from err

            self.state.SaveInstances(json_data)

    # --- LoadInstances ------------------------------------------------------
    def LoadInstances(self) -> List:
        """Load and reconstruct instances from the backend (Go ``LoadInstances``).

        Raises ``RuntimeError('failed to unmarshal instances: ...')`` on a JSON
        parse error, and ``RuntimeError('failed to create instance <title>: ...')``
        if any instance fails to reconstruct.
        """
        # Lazy import to avoid a circular import with instance.py.
        from backend.session.instance import FromInstanceData

        json_data = self.state.GetInstances()

        try:
            raw = json_data
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8")
            parsed = json.loads(raw) if raw else []
            if parsed is None:
                parsed = []
            instances_data = [InstanceData.from_dict(item) for item in parsed]
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("failed to unmarshal instances: {}".format(err)) from err

        instances = []
        for data in instances_data:
            try:
                instance = FromInstanceData(data)
            except Exception as err:  # noqa: BLE001
                raise RuntimeError(
                    "failed to create instance {}: {}".format(data.title, err)
                ) from err
            instances.append(instance)

        return instances

    # --- raw persisted array (shared by Delete/Update) -----------------------
    def _load_raw_instances(self) -> list:
        """The persisted instances as raw JSON dicts (no reconstruction).

        Delete/Update only need to splice ONE entry by title; reconstructing
        every stored instance via ``FromInstanceData`` (the old path) also
        re-attached their tmux sessions as a side effect — a whole-fleet PTY
        restore to change a single field.
        """
        raw = self.state.GetInstances()
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        parsed = json.loads(raw) if raw else []
        return parsed if isinstance(parsed, list) else []

    def _save_raw_instances(self, items: list) -> None:
        """Persist raw JSON dicts with the same byte format as SaveInstances."""
        from backend.config.config import _go_escape

        text = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
        self.state.SaveInstances(_go_escape(text).encode("utf-8"))

    # --- DeleteInstance -----------------------------------------------------
    def DeleteInstance(self, title: str) -> None:
        """Remove the instance matching ``title`` (Go ``DeleteInstance``).

        Raises ``RuntimeError('failed to load instances: ...')`` if the load
        fails, or ``RuntimeError('instance not found: <title>')`` if no instance
        matches.

        The whole load-mutate-save runs under the cross-process state lock so
        a concurrent writer can't slip in between (lost update). Operates on
        the raw JSON array — the other entries are passed through untouched.
        """
        with state_file_lock():
            try:
                items = self._load_raw_instances()
            except Exception as err:  # noqa: BLE001
                raise RuntimeError("failed to load instances: {}".format(err)) from err

            kept = [x for x in items if (x or {}).get("title") != title]
            if len(kept) == len(items):
                raise RuntimeError("instance not found: {}".format(title))

            self._save_raw_instances(kept)

    # --- UpdateInstance -----------------------------------------------------
    def UpdateInstance(self, instance) -> None:
        """Replace the stored instance matching ``instance`` by title.

        Raises ``RuntimeError('failed to load instances: ...')`` if the load
        fails, or ``RuntimeError('instance not found: <title>')`` if no instance
        matches.

        The whole load-mutate-save runs under the cross-process state lock so
        a concurrent writer can't slip in between (lost update). Operates on
        the raw JSON array — the other entries are passed through untouched.
        """
        with state_file_lock():
            try:
                items = self._load_raw_instances()
            except Exception as err:  # noqa: BLE001
                raise RuntimeError("failed to load instances: {}".format(err)) from err

            data = instance.ToInstanceData()
            found = False
            for i, existing in enumerate(items):
                if (existing or {}).get("title") == data.title:
                    items[i] = data.to_dict()
                    found = True
                    break

            if not found:
                raise RuntimeError("instance not found: {}".format(data.title))

            self._save_raw_instances(items)

    # --- DeleteAllInstances -------------------------------------------------
    def DeleteAllInstances(self) -> None:
        """Remove all stored instances (Go ``DeleteAllInstances``)."""
        self.state.DeleteAllInstances()

    # snake_case aliases
    save_instances = SaveInstances
    load_instances = LoadInstances
    delete_instance = DeleteInstance
    update_instance = UpdateInstance
    delete_all_instances = DeleteAllInstances


def new_storage(state) -> Storage:
    """Create a new :class:`Storage` (Go ``NewStorage``). Never fails."""
    return Storage(state)


# Go-name alias.
NewStorage = new_storage
