"""Pinned third-party installers the engine may run.

The uv installer pin lives in TWO places that must agree: ``install.sh``
(which runs before any Python exists) and here (the doctor's uv fix, see
:func:`backend.doctor.check_uv`). The weekly ``bump-uv-pin`` workflow rewrites
both, and ``tests/unit/test_pins.py`` fails the moment they drift.
"""

# sha256 of https://astral.sh/uv/<UV_PINNED_VERSION>/install.sh
UV_PINNED_VERSION = "0.13.6"
UV_INSTALLER_SHA256 = "f7a56e7afaec49a3bcbc3ac3cdbe917e7c6ce30e0d815308a12e531aa5ef23b7"  # pragma: allowlist secret
