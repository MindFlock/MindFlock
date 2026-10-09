"""A brand-new computer in one line: what "Add a device" hands over.

A join code alone (``mindflock devices join <dev> <code>``) only works on a
computer that already runs MindFlock — and getting it there (uv, the engine,
tmux, the agent CLI, Tailscale) routinely outlasted the code's ten minutes.
:func:`lines` turns a fresh invite into one copyable shell line for a computer
that has nothing yet::

    curl -LsSf https://raw.githubusercontent.com/<repo>/v<ver>/install.sh \\
      | MINDFLOCK_INSTALL_REF=v<ver> sh -s -- --join '<device> <CODE>'

``install.sh --join`` installs, starts the server and joins
(``mindflock devices bootstrap --join``, which falls back to asking for
approval when the code expired meanwhile).

**Pinned to THIS device's version.** ``install.sh`` installs the latest
release by default; a joiner on another version than the device it joins
gets "update MindFlock on X to sync settings". Both the script and the ref
come from this device's own tag. A source checkout (no release version) pins
``main`` and says so.

Desktop users get the same thing as two steps: the app for this version, then
paste ``<device> <CODE>`` into Settings → Devices → Join.
"""

from __future__ import annotations

import re
from typing import Optional

_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")


def install_ref(version: Optional[str] = None) -> str:
    """``v<version>`` for a release build, else ``main``."""
    if version is None:
        from backend import __version__ as version  # noqa: N811
    v = str(version or "").strip().lstrip("vV")
    return "v" + v if _RELEASE.match(v) else "main"


def _repo() -> str:
    from backend.web.core import self_update

    return self_update.UPDATE_REPO


def lines(invite: dict, version: Optional[str] = None) -> dict:
    """The bootstrap line (and its desktop twin) for ``invite`` (a
    ``fleet.create_invite()`` row: ``device``, ``code``, ``expires_at``)."""
    if version is None:
        from backend import __version__ as version  # noqa: N811
    ref = install_ref(version)
    repo = _repo()
    device = str(invite.get("device") or "")
    code = str(invite.get("code") or "")
    pair = "%s %s" % (device, code)
    script = "https://raw.githubusercontent.com/%s/%s/install.sh" % (repo, ref)
    line = "curl -LsSf %s | MINDFLOCK_INSTALL_REF=%s sh -s -- --join '%s'" % (
        script,
        ref,
        pair,
    )
    release = (
        "https://github.com/%s/releases/tag/%s" % (repo, ref)
        if ref != "main"
        else "https://github.com/%s/releases/latest" % repo
    )
    return {
        "line": line,
        "ref": ref,
        "version": str(version or ""),
        "pinned": ref != "main",
        "device": device,
        "code": code,
        "expires_at": invite.get("expires_at"),
        "desktop": {"download": release, "paste": pair},
    }
