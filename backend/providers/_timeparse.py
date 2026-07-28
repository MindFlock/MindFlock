"""Shared ISO-timestamp -> epoch-seconds parser for the providers package.

A transcript entry / RPC payload / usage-endpoint timestamp becomes epoch
seconds, or ``None`` for anything unparseable (missing, junk, wrong type). The
five provider modules that turn timestamps into cost/window math all delegate
here so they cannot silently diverge on how a ``Z``/offset form is handled.

Tolerant of both the trailing-``Z`` ("Zulu") form and an explicit ``+00:00``
offset; on Python 3.12 (the project floor) ``datetime.fromisoformat`` parses
both natively, and the ``Z`` replacement keeps older-style inputs working.
Never raises — the None-and-junk-return-None contract is what callers rely on.
"""

from __future__ import annotations

from typing import Optional


def ts_epoch(s) -> Optional[float]:
    """Parse an ISO timestamp to epoch seconds, or ``None`` on any failure."""
    if not s:
        return None
    try:
        import datetime as _dt

        return _dt.datetime.fromisoformat(
            str(s).strip().replace("Z", "+00:00")
        ).timestamp()
    except Exception:  # noqa: BLE001 — missing/junk/wrong-type -> None
        return None
