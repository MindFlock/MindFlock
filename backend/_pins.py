"""Pinned third-party installers the engine may run.

The uv installer pin lives in TWO places that must agree: ``install.sh``
(which runs before any Python exists) and here (the doctor's uv fix, see
:func:`backend.doctor.check_uv`). The weekly ``bump-uv-pin`` workflow rewrites
both, and ``tests/unit/test_pins.py`` fails the moment they drift.
"""

# sha256 of https://astral.sh/uv/<UV_PINNED_VERSION>/install.sh
UV_PINNED_VERSION = "0.13.5"
UV_INSTALLER_SHA256 = "1c6e7f025499664633ac2dce43a210800510e9ef0625c52e034a8afdf8bf177a"  # pragma: allowlist secret
