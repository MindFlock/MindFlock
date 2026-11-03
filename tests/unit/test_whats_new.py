"""A minor or major release ships a What's new entry (docs/vision/whats-new.md).

``frontend/src/whatsNew/releases.ts`` lists one entry per release, newest
first; ``big: true`` pops the What's new window once for everyone who updates.
Cutting a release whose major.minor is newer than the newest entry's would ship
it with nothing to explain — this fails then. Patch releases may skip an entry.
An entry AHEAD of pyproject's version is fine: entries above the running version
are simply not shown yet.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASES_TS = ROOT / "frontend" / "src" / "whatsNew" / "releases.ts"

_VERSION_RE = re.compile(r'^\s*version:\s*"(\d+)\.(\d+)\.(\d+)"', re.M)


def _pyproject_version() -> tuple[int, int, int]:
    raw = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", raw)
    assert m, "pyproject version %r is not major.minor.patch" % raw
    return int(m[1]), int(m[2]), int(m[3])


def _entry_versions() -> list[tuple[int, int, int]]:
    found = [
        tuple(int(x) for x in m) for m in _VERSION_RE.findall(RELEASES_TS.read_text())
    ]
    assert found, 'no `version: "x.y.z"` entries in %s' % RELEASES_TS
    return found  # type: ignore[return-value]


def test_entries_are_newest_first() -> None:
    entries = _entry_versions()
    assert entries == sorted(entries, reverse=True), entries
    assert len(set(entries)) == len(entries), "a version listed twice"


def test_a_minor_or_major_release_has_an_entry() -> None:
    major, minor, _ = _pyproject_version()
    newest = _entry_versions()[0]
    assert (major, minor) <= newest[:2], (
        "pyproject is at %d.%d but the newest What's new entry is %d.%d.%d: add an "
        "entry for this release to frontend/src/whatsNew/releases.ts (big: true "
        "pops it for everyone who updates; see docs/development.md, Cutting a release)"
        % (major, minor, *newest)
    )
