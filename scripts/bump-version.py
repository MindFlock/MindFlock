#!/usr/bin/env python3
"""Single source of truth for the release version across a hybrid repo.

Three manifests carry a version string that must never disagree:

  * ``pyproject.toml``          — the Python package (``backend.__version__``
    reads this back out of installed metadata at runtime).
  * ``electron/package.json``   — the desktop shell.
  * ``frontend/package.json``   — the web UI.

Editing three files by hand is how they drift. This script is the one place
that writes all three, and it doubles as the CI guard that they still agree.

Usage
-----
    scripts/bump-version.py --check
        Assert the three manifests carry the same version. Prints it and
        exits 0, or prints the disagreement and exits 1. This is what CI runs.

    scripts/bump-version.py --check --expect vX.Y.Z
        As above, and additionally assert the shared version equals X.Y.Z
        (a leading ``v`` is tolerated). Used on tagged builds to prove the
        tag matches the code.

    scripts/bump-version.py <version>
    scripts/bump-version.py major|minor|patch
        Write a new version into all three manifests and roll the CHANGELOG
        ``[Unreleased]`` heading into ``[<version>] - <today>``. An explicit
        version is validated as ``MAJOR.MINOR.PATCH`` (optionally with a
        ``-prerelease`` / ``+build`` suffix); the keyword forms bump the
        current shared version.

Edits are done with targeted regex substitutions rather than a JSON/TOML
round-trip so the manifests keep their exact hand-authored formatting.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Each manifest and the regex that isolates its version literal. The pattern
# captures the surrounding text so we can rewrite just the value in group 2.
PYPROJECT = ROOT / "pyproject.toml"
ELECTRON = ROOT / "electron" / "package.json"
FRONTEND = ROOT / "frontend" / "package.json"
UVLOCK = ROOT / "uv.lock"
CHANGELOG = ROOT / "CHANGELOG.md"

# (path, human label, compiled pattern). The pattern must have exactly one
# group around the version value.
MANIFESTS = [
    (
        PYPROJECT,
        "pyproject.toml",
        re.compile(r'(?m)^(version\s*=\s*")([^"]+)(")'),
    ),
    (
        ELECTRON,
        "electron/package.json",
        re.compile(r'(?m)^(\s*"version"\s*:\s*")([^"]+)(",?)'),
    ),
    (
        FRONTEND,
        "frontend/package.json",
        re.compile(r'(?m)^(\s*"version"\s*:\s*")([^"]+)(",?)'),
    ),
    # uv.lock pins the project's OWN version alongside its dependencies, so a
    # bump leaves it disagreeing until something re-locks — and `uv lock` then
    # rewrites it at the least convenient moment (mid-commit, in a hook). It is
    # a manifest like the others; anchored on the [[package]] block for
    # `mindflock` so no dependency's version can match.
    (
        UVLOCK,
        "uv.lock",
        re.compile(
            r'(?ms)(^\[\[package\]\]\nname = "mindflock"\nversion = ")([^"]+)(")'
        ),
    ),
]

# MAJOR.MINOR.PATCH with an optional SemVer pre-release / build suffix.
SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def read_version(path: Path, pattern: re.Pattern[str], label: str) -> str:
    """Return the version string a single manifest currently carries."""
    text = path.read_text(encoding="utf-8")
    match = pattern.search(text)
    if not match:
        sys.exit(f"error: could not find a version string in {label}")
    return match.group(2)


def read_all() -> dict[str, str]:
    """Map each manifest label to the version it currently carries."""
    return {
        label: read_version(path, pattern, label)
        for path, label, pattern in MANIFESTS
    }


def shared_version(versions: dict[str, str]) -> str:
    """Return the one version all manifests agree on, or exit if they don't."""
    distinct = set(versions.values())
    if len(distinct) != 1:
        lines = "\n".join(f"  {label}: {v}" for label, v in versions.items())
        sys.exit(f"error: version drift across manifests:\n{lines}")
    return distinct.pop()


def bump(current: str, part: str) -> str:
    """Return ``current`` with the named part incremented (suffixes dropped)."""
    core = current.split("-", 1)[0].split("+", 1)[0]
    try:
        major, minor, patch = (int(x) for x in core.split("."))
    except ValueError:
        sys.exit(f"error: cannot {part}-bump non-numeric version {current!r}")
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_manifests(new_version: str) -> None:
    """Rewrite the version literal in all three manifests."""
    for path, label, pattern in MANIFESTS:
        text = path.read_text(encoding="utf-8")
        new_text, count = pattern.subn(
            lambda m: f"{m.group(1)}{new_version}{m.group(3)}", text, count=1
        )
        if count != 1:
            sys.exit(f"error: failed to rewrite version in {label}")
        path.write_text(new_text, encoding="utf-8")
        print(f"  {label}: -> {new_version}")


# The Keep-a-Changelog link reference for the running section, e.g.
#   [Unreleased]: https://github.com/owner/repo/compare/v0.1.2...HEAD
# Captures the repo base URL and the previous tag so we can retarget it.
UNRELEASED_LINK_RE = re.compile(
    r"(?m)^\[Unreleased\]:\s*(?P<base>https?://\S+?)/compare/"
    r"v?(?P<prev>[0-9A-Za-z.+-]+)\.\.\.HEAD\s*$"
)


def roll_changelog(new_version: str, date: str) -> None:
    """Turn the ``[Unreleased]`` heading into a dated release section.

    A fresh empty ``[Unreleased]`` is left on top so the next cycle has
    somewhere to accumulate, and the Keep-a-Changelog link references at the
    foot are retargeted: ``[Unreleased]`` now compares from the new tag, and a
    ``[<version>]`` release-tag link is inserted. No-op (with a warning) for
    any piece that isn't present, so a hand-trimmed CHANGELOG still bumps.
    """
    if not CHANGELOG.exists():
        print("  CHANGELOG.md: not found, skipping")
        return
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = "## [Unreleased]"
    if heading not in text:
        print("  CHANGELOG.md: no [Unreleased] heading, skipping")
        return

    text = text.replace(heading, f"{heading}\n\n## [{new_version}] - {date}", 1)

    match = UNRELEASED_LINK_RE.search(text)
    if match:
        base = match.group("base")
        new_links = (
            f"[Unreleased]: {base}/compare/v{new_version}...HEAD\n"
            f"[{new_version}]: {base}/releases/tag/v{new_version}"
        )
        text = UNRELEASED_LINK_RE.sub(new_links, text, count=1)
        print(f"  CHANGELOG.md: opened [{new_version}] - {date}, retargeted links")
    else:
        print(
            f"  CHANGELOG.md: opened [{new_version}] - {date} "
            "(no [Unreleased] link line to retarget)"
        )

    CHANGELOG.write_text(text, encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        help="explicit MAJOR.MINOR.PATCH, or one of: major, minor, patch",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the manifests agree instead of writing (CI guard)",
    )
    parser.add_argument(
        "--expect",
        metavar="vX.Y.Z",
        help="with --check, also assert the shared version equals this",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="CHANGELOG release date (default: today)",
    )
    args = parser.parse_args(argv)

    if args.check:
        current = shared_version(read_all())
        if args.expect:
            expected = args.expect.lstrip("v")
            if current != expected:
                return _fail(
                    f"error: manifests are {current} but expected {expected} "
                    "(tag/code mismatch)"
                )
        print(f"ok: all manifests at {current}")
        return 0

    if not args.target:
        parser.error("a version, a bump keyword, or --check is required")

    if args.target in {"major", "minor", "patch"}:
        current = shared_version(read_all())
        new_version = bump(current, args.target)
        print(f"bumping {args.target}: {current} -> {new_version}")
    else:
        new_version = args.target.lstrip("v")
        if not SEMVER_RE.match(new_version):
            return _fail(
                f"error: {args.target!r} is not MAJOR.MINOR.PATCH[-pre][+build]"
            )
        print(f"setting version -> {new_version}")

    write_manifests(new_version)
    roll_changelog(new_version, args.date or datetime.date.today().isoformat())
    print(
        f"\ndone. review, commit, then tag:\n"
        f"  git commit -am 'Release {new_version}'\n"
        f"  git tag -a v{new_version} -m 'Release {new_version}'"
    )
    return 0


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
