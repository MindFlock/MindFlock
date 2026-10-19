"""Pinned third-party installers the engine may run.

The uv installer pin lives in TWO places that must agree: ``install.sh``
(which runs before any Python exists) and here (the doctor's uv fix, see
:func:`backend.doctor.check_uv`). The weekly ``bump-uv-pin`` workflow rewrites
both, and ``tests/unit/test_pins.py`` fails the moment they drift.
"""

# sha256 of https://astral.sh/uv/<UV_PINNED_VERSION>/install.sh
UV_PINNED_VERSION = "0.13.1"
UV_INSTALLER_SHA256 = "7c482d409673a53f4d9bd34ae95c9166be3b8d89ca82f27dd7bca3cab457afff"  # pragma: allowlist secret
