"""One phone URL for every device — the shared link, on Tailscale Services.

Every MindFlock server is standalone, so until now the phone URL named a
machine (``http://<device>.<tailnet>.ts.net:8765/m``) and died with it. But
with remote control on, any one device already shows the whole fleet
(:mod:`backend.web.core.remote`), so what the phone needs is not a particular
device — it is *any device that is up*. A `Tailscale Service
<https://tailscale.com/kb/1552/tailscale-services>`_ is exactly that: one
MagicDNS name (``https://<name>.<tailnet>.ts.net``) that several hosts
advertise, routed by Tailscale to an available one.

So, when ``general.shared_link`` names a service, this server:

* **advertises** it at startup and whenever the setting changes
  (:func:`apply` — ``tailscale serve --service=svc:<name> --https=443
  http://127.0.0.1:<port>``), and registers the service hostname with the auth
  middleware so a local-mode server accepts it (:func:`auth.allow_fronted_host`
  — ``tailscale serve`` preserves the ``Host`` header);
* **withdraws** on a clean shutdown (:func:`withdraw` — ``tailscale serve
  clear``), so a machine that is on but not running MindFlock stops drawing
  the phone's traffic;
* **reports** the link (:func:`status`) for Settings → Mobile, the startup
  banner and the ntfy announce, which advertise it in place of this device's
  own URL once it is set up.

Signing in once on the shared origin works on every device because the shared
QR carries every paired device's token and the browser keeps a cookie per
token (see :mod:`backend.web.core.auth`).

What Tailscale requires, and this module cannot do for you: a service host
must be a **tagged** device, the service must be **defined** in the admin
console (or the policy file), and each host must be **approved** for it
(``autoApprovers.services`` does that automatically). :func:`status` says
which of those it can see is missing.

Everything here is best-effort and never raises: no ``tailscale`` binary, no
permission to change serve config, or no tailnet just leave the link off with
the reason in ``error``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from typing import Optional, Tuple

from backend.web.core import auth as _auth

#: The port the service answers on. 443 so the phone URL carries no port.
SERVICE_PORT = 443

#: A Tailscale Service name: one DNS label (it becomes ``<name>.<tailnet>``).
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_TIMEOUT = 15

_LOCK = threading.RLock()  # apply() reports status() while holding it

#: What this process last did: the service name it advertised (``""`` for
#: none), the hostname that resolves to it, and why the last attempt failed.
_STATE = {"name": "", "host": "", "error": ""}


def normalize(name: object) -> str:
    """``name`` as a bare service name (``"svc:MindFlock "`` → ``"mindflock"``),
    or ``""`` when it isn't a valid one."""
    n = str(name or "").strip().lower()
    if n.startswith("svc:"):
        n = n[4:]
    return n if _NAME_RE.match(n) else ""


def service_id(name: str) -> str:
    return "svc:" + name


def configured_name() -> str:
    """The persisted ``general.shared_link``, normalized (``""`` = off)."""
    try:
        from backend.config.settings import load_settings

        return normalize(load_settings().general.shared_link)
    except Exception:  # noqa: BLE001 — advisory only
        return ""


def _run(args: list) -> Tuple[int, str]:
    """``(returncode, combined output)``; ``(-1, reason)`` when it couldn't run."""
    try:
        cp = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -1, "timed out: %s" % " ".join(args)
    except OSError as err:
        return -1, str(err)
    return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()


def _tailscale_status() -> dict:
    if shutil.which("tailscale") is None:
        return {}
    try:
        cp = subprocess.run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if cp.returncode != 0:
            return {}
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _suffix(data: dict) -> str:
    """The tailnet's MagicDNS suffix (``tail1234.ts.net``), or ``""``."""
    suffix = data.get("MagicDNSSuffix") or (data.get("CurrentTailnet") or {}).get(
        "MagicDNSSuffix"
    )
    return str(suffix or "").strip(".").lower()


def _tagged(data: dict) -> bool:
    return bool((data.get("Self") or {}).get("Tags"))


def _approved(data: dict, name: str) -> bool:
    """Whether the control plane has made this node a host of ``svc:<name>``.

    An approved host is told which VIPs to listen on through its node
    capabilities (a ``{"svc:<name>": [ips]}`` map — ``tailcfg.ServiceIPMappings``),
    so the service name showing up there is the approval, seen from this end.
    Searched for as a key rather than under one capability name, so a renamed
    capability reads as "not seen yet" instead of breaking.
    """
    capmap = (data.get("Self") or {}).get("CapMap") or {}
    try:
        blob = json.dumps(capmap)
    except (TypeError, ValueError):
        return False
    return ('"%s"' % service_id(name)) in blob


def _explain(output: str) -> str:
    """Turn ``tailscale serve``'s refusal into the fix, where we know it."""
    low = output.lower()
    if "access denied" in low or "permission" in low or "operator" in low:
        return (
            "Tailscale refused to change serve config for this user. Run "
            "`sudo tailscale set --operator=$USER` once, then save again."
        )
    if "tag" in low and ("service" in low or "host" in low):
        return (
            "Only tagged devices can host a Tailscale Service. Tag this device "
            "in the Tailscale admin console, then save again."
        )
    return output.splitlines()[-1] if output else "tailscale serve failed"


def _withdraw_locked(name: str) -> None:
    _run(["tailscale", "serve", "clear", service_id(name)])
    if _STATE["host"]:
        _auth.forget_fronted_host(_STATE["host"])
    _STATE.update(name="", host="", error="")


def apply(port: int) -> dict:
    """Make this device's advertisement match ``general.shared_link``.

    Clears a previously advertised name that is no longer wanted, then
    (re)configures the wanted one: ``clear`` first so a changed port or a
    drained service comes back as a fresh config, then ``serve`` (which
    configures and advertises in one step). Blocking — call it off the event
    loop. Returns :func:`status`.
    """
    want = configured_name()
    with _LOCK:
        if _STATE["name"] and _STATE["name"] != want:
            _withdraw_locked(_STATE["name"])
        if not want:
            _STATE["error"] = ""
            return status()
        if shutil.which("tailscale") is None:
            _STATE.update(name="", host="", error="Tailscale is not installed.")
            return status()
        svc = service_id(want)
        _run(["tailscale", "serve", "clear", svc])
        rc, out = _run(
            [
                "tailscale",
                "serve",
                "--service=" + svc,
                "--https=%d" % SERVICE_PORT,
                "--bg",
                "--yes",
                "http://127.0.0.1:%d" % port,
            ]
        )
        if rc != 0:
            _STATE.update(name="", host="", error=_explain(out))
            return status()
        suffix = _suffix(_tailscale_status())
        host = "%s.%s" % (want, suffix) if suffix else ""
        if host:
            _auth.allow_fronted_host(host)
        _STATE.update(name=want, host=host, error="")
    return status()


def withdraw() -> None:
    """Stop advertising (server shutdown). A no-op when nothing was advertised
    by this process — never clears a service some other process set up."""
    with _LOCK:
        if _STATE["name"]:
            _withdraw_locked(_STATE["name"])


def advertised_url() -> Optional[str]:
    """The shared ``/m`` URL when THIS process advertises it, else None.

    Cheap (no shell-out): it reads what :func:`apply` recorded. This is the URL
    the QR, banner and ntfy announce hand out in place of the device URL.
    """
    with _LOCK:
        host = _STATE["host"] if _STATE["name"] else ""
    return "https://%s/m" % host if host else None


def status() -> dict:
    """The shared link as Settings → Mobile shows it.

    ``{enabled, name, service, url, advertised, approved, tagged, error}`` —
    ``enabled`` is the setting; ``advertised`` is whether this device's
    ``tailscale serve`` for it is up; ``approved`` is whether Tailscale has
    accepted this device as a host (only visible once it has); ``tagged``
    whether this device has the tag-based identity a host needs.
    """
    name = configured_name()
    if not name:
        return {"enabled": False}
    data = _tailscale_status()
    suffix = _suffix(data)
    with _LOCK:
        advertised = _STATE["name"] == name
        error = _STATE["error"]
    return {
        "enabled": True,
        "name": name,
        "service": service_id(name),
        "url": "https://%s.%s/m" % (name, suffix) if suffix else "",
        "advertised": advertised,
        "approved": _approved(data, name),
        "tagged": _tagged(data),
        "error": error,
    }
