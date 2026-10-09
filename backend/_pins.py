"""Pinned third-party installers the engine may run.

The uv installer pin lives in TWO places that must agree: ``install.sh``
(which runs before any Python exists) and here (the doctor's uv fix, see
:func:`backend.doctor.check_uv`). The weekly ``bump-uv-pin`` workflow rewrites
both, and ``tests/unit/test_pins.py`` fails the moment they drift.
"""

# sha256 of https://astral.sh/uv/<UV_PINNED_VERSION>/install.sh
UV_PINNED_VERSION = "0.11.29"
UV_INSTALLER_SHA256 = "504a79fd2ed0dcd47e7f04f0792cfd0871f62e24a7fe40fa8ae0f563a369f2bd"  # pragma: allowlist secret
