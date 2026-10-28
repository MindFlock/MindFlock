"""The public address a guest link is opened at: a Cloudflare quick tunnel.

A guest (:mod:`backend.web.core.guests`) is usually NOT on the owner's
tailnet or LAN — that is the whole point — so the tailnet/LAN address the
phone QR advertises reaches nobody. Peer links already solve "someone on
another network" with ``cloudflared tunnel --url`` (:mod:`backend.peer.tunnel`,
no account, a random ``https://<label>.trycloudflare.com`` name); this runs
one such tunnel to THIS web server while guest links exist, and the link's
URL is ``https://<label>.trycloudflare.com/?token=…``.

What that exposes, and what keeps it closed:

* The tunnel fronts the whole web server, not a sandboxed ingress — the
  access gate is the only thing between the internet and this machine. So
  links are refused while the gate is off (``routes/guests.py``), and every
  request on the tunnel without a token gets the login page or a 401, the
  owner's token keeps working there like anywhere, and a guest token opens
  only its sessions. The hostname is a random label nobody is told but the
  guest; ``host_ok`` passes it through :func:`backend.web.core.auth.
  allow_fronted_host`, registered while the tunnel is up and forgotten when
  it stops.
* The name is minted per cloudflared process: a server restart gives a new
  one, so a link's URL changes — the owner copies it again from Settings →
  People, which is also where the tunnel's state shows. The token is the
  credential; the hostname is just where it is presented.
* We never download cloudflared (the doctor offers the install), and the
  process gets the same minimal environment and private HOME the peer relay
  tunnel does (:class:`backend.peer.tunnel.QuickTunnel`).

One tunnel per process, started lazily (the first link, or boot with links
on disk) in a background task so a request never waits the ~10–45 s it takes
Cloudflare to issue a name; :func:`status` says ``starting`` meanwhile and
the People screen polls. Stopped when the last link is revoked.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from backend.web.core import auth as _auth

log = logging.getLogger(__name__)

_TUNNEL = None  # the QuickTunnel while one exists
_TASK: Optional[asyncio.Task] = None
_HOST: str = ""
_ERROR: str = ""
_REASON: str = ""
#: The owner asked for the public link from the People screen (so the doctor
#: offers the cloudflared install even before the first link exists).
_WANTED = False

#: Why a start failed, in the peer tunnel's words (``FAILURE_TEXT``) or ours.
_NO_BINARY = "cloudflared is not installed on this computer"


def _binary() -> Optional[str]:
    from backend.peer.service import CLOUDFLARED_ENV
    from backend.peer.tunnel import find_cloudflared

    return find_cloudflared(os.environ.get(CLOUDFLARED_ENV, ""))


def _workdir() -> str:
    from backend import config
    from backend.peer import paths

    return paths.ensure_dir(os.path.join(config.GetConfigDir(), "guest_tunnel"))


def _port() -> int:
    from backend.web.core import mobile_access as _mobile

    return _mobile._server_port()


def _make_tunnel(binary: str):
    """The tunnel object (tests swap this for a fake)."""
    from backend.peer.tunnel import QuickTunnel

    return QuickTunnel(binary, _port(), workdir=_workdir(), on_exit=_exited)


def installed() -> bool:
    try:
        return bool(_binary())
    except Exception:  # noqa: BLE001
        return False


def wanted() -> bool:
    """Whether the doctor should put cloudflared in the install plan: the
    owner pressed for a public link, or links exist on disk."""
    if _WANTED:
        return True
    try:
        from backend.web.core import guests as _guests

        return _guests.any_links()
    except Exception:  # noqa: BLE001
        return False


def want() -> None:
    global _WANTED
    _WANTED = True


def host() -> str:
    """The public hostname while the tunnel is up, else ``""``."""
    return _HOST


def url() -> str:
    """The public root (``https://<host>/``) while up, else ``""``."""
    return "https://%s/" % _HOST if _HOST else ""


def status() -> dict:
    """``{state, url, installed, error}`` — state is ``up``, ``starting``,
    ``failed`` or ``off``."""
    if _HOST:
        state = "up"
    elif _TASK is not None and not _TASK.done():
        state = "starting"
    elif _ERROR:
        state = "failed"
    else:
        state = "off"
    return {
        "state": state,
        "url": url(),
        "installed": installed(),
        "error": _ERROR,
        "reason": _REASON,
    }


def _exited() -> None:
    """cloudflared died on its own: the name is gone for good. The next
    ``ensure`` starts a fresh one (and the People screen says so meanwhile)."""
    global _HOST, _ERROR, _REASON
    if _HOST:
        _auth.forget_fronted_host(_HOST)
    _HOST = ""
    _ERROR = "the public link dropped — reopen Settings → People to start it again"
    _REASON = "exited"


async def _start() -> None:
    global _TUNNEL, _HOST, _ERROR, _REASON
    binary = _binary()
    if not binary:
        _ERROR, _REASON = _NO_BINARY, "not_installed"
        return
    try:
        if _TUNNEL is None:
            _TUNNEL = _make_tunnel(binary)
        host = await _TUNNEL.start()
    except Exception as e:  # noqa: BLE001 — TunnelError or a surprise
        from backend.peer.tunnel import FAILURE_TEXT

        reason = getattr(e, "reason", "failed")
        _ERROR = FAILURE_TEXT.get(reason) or "the public link could not be started"
        _REASON = reason
        log.warning("guest links: tunnel start failed (%s)", reason)
        return
    _HOST = host
    _ERROR = _REASON = ""
    _auth.allow_fronted_host(host)
    log.info("guest links: public link up at https://%s/", host)


def ensure() -> dict:
    """Start the tunnel if it isn't up or starting — in a background task on
    the running loop — and return :func:`status` right away. Call from the
    server loop (a route, the lifespan)."""
    global _TASK, _ERROR, _REASON
    want()
    if not _HOST and (_TASK is None or _TASK.done()):
        if not _binary():
            _ERROR, _REASON = _NO_BINARY, "not_installed"
            return status()
        try:
            _TASK = asyncio.get_running_loop().create_task(
                _start(), name="guest-tunnel"
            )
        except RuntimeError:  # no running loop (a sync caller): nothing to do
            pass
    return status()


async def stop() -> None:
    """Stop the tunnel (the last link was revoked, or shutdown)."""
    global _TUNNEL, _TASK, _HOST, _ERROR, _REASON, _WANTED
    task, _TASK = _TASK, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    tunnel, _TUNNEL = _TUNNEL, None
    if _HOST:
        _auth.forget_fronted_host(_HOST)
    _HOST = _ERROR = _REASON = ""
    _WANTED = False
    if tunnel is not None:
        try:
            await tunnel.stop()
        except Exception:  # noqa: BLE001
            pass


async def stop_if_unused() -> None:
    """Stop when no link is left to open through it."""
    from backend.web.core import guests as _guests

    if not _guests.any_links():
        await stop()
