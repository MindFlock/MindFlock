"""Byte-contract tests for the engine's on-disk instance serialization.

``~/.mindflock/state.json`` is shared with a co-running pipeline, so the JSON
is a byte-exact wire contract: fixed field order, compact separators, HTML
escaping, and *conditional* emission of the extension keys
(``provisioned``/``workspace_strategy``/``in_place``).

These pin that contract so any accidental reorder, spacing change, or
unconditional new key fails here first.
"""

from __future__ import annotations

import datetime as dt
import json

from backend.session.storage import (
    ZERO_TIME,
    InstanceData,
    _marshal_instances,
    format_rfc3339,
    parse_rfc3339,
)

# The canonical Go field order, exactly as to_dict emits it for a plain session.
_PLAIN_KEYS = [
    "title",
    "path",
    "branch",
    "status",
    "height",
    "width",
    "created_at",
    "updated_at",
    "auto_yes",
    "program",
    "worktree",
    "diff_stats",
]


def test_plain_instance_emits_only_go_keys_in_order():
    d = InstanceData(title="demo", path="/x", program="claude").to_dict()
    assert list(d.keys()) == _PLAIN_KEYS
    # None of the extension keys appear for a plain session.
    for k in ("provisioned", "workspace_strategy", "in_place"):
        assert k not in d


def test_conditional_keys_emitted_only_when_set():
    pv = InstanceData(title="t", provisioned=True, workspace_strategy="clone").to_dict()
    assert pv["provisioned"] is True and pv["workspace_strategy"] == "clone"

    ip = InstanceData(title="t", in_place=True).to_dict()
    assert ip["in_place"] is True

    # Per-session launch flags: emitted (as a list) only when present.
    la = InstanceData(title="t", launch_args=("--yolo", "--foo")).to_dict()
    assert la["launch_args"] == ["--yolo", "--foo"]
    assert "launch_args" not in InstanceData(title="t").to_dict()


def test_launch_args_round_trip():
    orig = InstanceData(title="t", program="claude", launch_args=("--yolo", "--x=1"))
    back = InstanceData.from_dict(orig.to_dict())
    assert back.launch_args == ("--yolo", "--x=1")
    assert back.to_dict() == orig.to_dict()
    # Non-string entries from a hand-edited state.json are dropped on read.
    parsed = InstanceData.from_dict(
        dict(InstanceData(title="t").to_dict(), launch_args=["--ok", 3, None])
    )
    assert parsed.launch_args == ("--ok",)


def test_round_trip_preserves_fields():
    orig = InstanceData(
        title="sc-20005",
        path="/repo",
        branch="feature/x",
        program="claude",
        provisioned=True,
        workspace_strategy="worktree",
    )
    back = InstanceData.from_dict(orig.to_dict())
    assert back.title == orig.title
    assert back.branch == orig.branch
    assert back.provisioned is True
    assert back.workspace_strategy == "worktree"
    # to_dict is stable across a round-trip.
    assert back.to_dict() == orig.to_dict()


def test_marshal_is_compact_and_html_escaped():
    # Compact separators (no spaces) and Go HTML escaping of < > &.
    data = [InstanceData(title="a&b", path="</x>", program="claude")]
    raw = _marshal_instances(data).decode("utf-8")
    assert ", " not in raw and ": " not in raw  # compact
    assert "\\u0026" in raw  # & escaped
    assert "\\u003c" in raw and "\\u003e" in raw  # < > escaped
    # Still valid JSON that parses back to the same logical content.
    parsed = json.loads(raw)
    assert parsed[0]["title"] == "a&b"
    assert parsed[0]["path"] == "</x>"


# ---------------------------------------------------------------------------
# RFC3339 time wire-contract (format_rfc3339 / parse_rfc3339)
#
# ``created_at``/``updated_at`` are hand-formatted to match Go's
# ``json.Marshal(time.Time)`` (RFC3339Nano with trailing-zero trimming) rather
# than via ``datetime.isoformat``, so the interesting branches — fraction
# trimming, offset zones, and over-precision truncation on parse — are pinned
# directly here.
# ---------------------------------------------------------------------------
def test_format_rfc3339_microseconds_utc():
    t = dt.datetime(2025, 6, 18, 14, 30, 45, 123456, tzinfo=dt.timezone.utc)
    assert format_rfc3339(t) == "2025-06-18T14:30:45.123456Z"


def test_format_rfc3339_trailing_zeros_stripped():
    t = dt.datetime(2025, 6, 18, 14, 30, 45, 500000, tzinfo=dt.timezone.utc)
    assert format_rfc3339(t) == "2025-06-18T14:30:45.5Z"


def test_format_rfc3339_no_fraction_when_zero_microseconds():
    t = dt.datetime(2025, 6, 18, 14, 30, 45, tzinfo=dt.timezone.utc)
    assert format_rfc3339(t) == "2025-06-18T14:30:45Z"


def test_format_rfc3339_naive_datetime_rendered_as_utc():
    # A naive datetime (no tzinfo) is treated as UTC and rendered with "Z".
    assert format_rfc3339(dt.datetime(2025, 6, 18, 14, 30, 45)) == (
        "2025-06-18T14:30:45Z"
    )


def test_format_rfc3339_negative_offset_zone():
    tz = dt.timezone(dt.timedelta(hours=-5))
    t = dt.datetime(2026, 1, 2, 9, 5, 0, tzinfo=tz)
    assert format_rfc3339(t) == "2026-01-02T09:05:00-05:00"


def test_format_rfc3339_positive_offset_zone():
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    t = dt.datetime(2026, 1, 2, 9, 5, 0, tzinfo=tz)
    assert format_rfc3339(t) == "2026-01-02T09:05:00+05:30"


def test_format_rfc3339_zero_value():
    assert format_rfc3339(ZERO_TIME) == "0001-01-01T00:00:00Z"


def test_parse_rfc3339_truncates_over_precision_fraction_to_micros():
    parsed = parse_rfc3339("2025-06-18T14:30:45.123456789Z")
    assert parsed == dt.datetime(
        2025, 6, 18, 14, 30, 45, 123456, tzinfo=dt.timezone.utc
    )


def test_parse_rfc3339_offset_round_trips_through_format():
    s = "2026-01-02T09:05:00-05:00"
    parsed = parse_rfc3339(s)
    assert parsed.utcoffset() == dt.timedelta(hours=-5)
    assert format_rfc3339(parsed) == s


def test_parse_rfc3339_zero_value_round_trips():
    assert parse_rfc3339("0001-01-01T00:00:00Z") == ZERO_TIME


def test_save_instances_holds_state_file_lock(monkeypatch):
    """SaveInstances wraps its whole-array overwrite in state_file_lock(),
    like its DeleteInstance/UpdateInstance siblings — otherwise a concurrent
    load-mutate-save can slip in between (lost update)."""
    from contextlib import contextmanager

    from backend.session import storage as storage_mod

    events = []

    @contextmanager
    def fake_lock():
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")

    class FakeState:
        def SaveInstances(self, blob):
            # The backend write must happen while the lock is held.
            events.append("save")

    monkeypatch.setattr(storage_mod, "state_file_lock", fake_lock)
    storage_mod.Storage(FakeState()).SaveInstances([])
    assert events == ["acquire", "save", "release"]
