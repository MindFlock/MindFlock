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
(``autoApprovers.services`` does that automatically). :func:`status` turns
what this device can see of those into a setup checklist (``steps``), each
step ``ok`` / ``fail`` / ``unknown`` with a one-line reason.

Reading approval honestly. Everything comes from ``tailscale status --json``
(``Self``), and the signals are distinct:

* ``CapMap["services/<name>"]`` (and the ``ExtraRecords`` DNS entry for
  ``<name>.<tailnet>``) — the service **exists** and this device can see it.
  It is there while the device advertises and is *not yet* approved, so it is
  NOT approval;
* ``CapMap["service-host"]`` = ``[{"svc:<name>": [VIPs]}]`` — the control
  plane has made this device a **host**, and these are the VIPs to answer on;
* ``AllowedIPs`` / ``PrimaryRoutes`` holding those VIPs — the service's
  traffic is actually **routed** here.

Only the last two together are "approved". When ``status`` can't be read (or
predates ``CapMap``) the answer is ``None`` — unknown — never a guess.

Keeping it applied. Tailscale's auto-approver only looks at an advertisement
when it is made, so one made before the device was tagged stays pending; and
``tailscale serve`` config can be cleared behind our back. :func:`reconcile`
re-applies when the live ``tailscale serve status --json`` no longer carries
our handler or this device's tags changed since the last apply (the server
runs it every :data:`RECHECK_INTERVAL` seconds while the setting is on), and
on demand from Settings → Mobile's Re-check, which also re-advertises a
tagged-but-unapproved host to give the auto-approver another look.

Everything here is best-effort and never raises: no ``tailscale`` binary, no
permission to change serve config, or no tailnet just leave the link off with
the reason in ``error``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import subprocess
import threading
from typing import Callable, Optional, Tuple

from backend import tailscale_cli as _tailscale_cli
from backend.web.core import auth as _auth

#: The port the service answers on. 443 so the phone URL carries no port.
SERVICE_PORT = 443

#: A Tailscale Service name: one DNS label (it becomes ``<name>.<tailnet>``).
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

_TIMEOUT = 15

#: Seconds between background :func:`reconcile` passes while the link is on.
RECHECK_INTERVAL = 60.0

#: Where the checklist points (the admin console pages each fix lives on).
ADMIN_MACHINES = "https://login.tailscale.com/admin/machines"
ADMIN_SERVICES = "https://login.tailscale.com/admin/services"
ADMIN_POLICY = "https://login.tailscale.com/admin/acls/file"

#: The tag the checklist suggests when this device has none yet.
DEFAULT_TAG = "tag:mindflock"

OPERATOR_FIX = "sudo tailscale set --operator=$USER"

_LOCK = threading.RLock()  # apply() reports status() while holding it

#: What this process last did: the service name it advertised (``""`` for
#: none), the hostname that resolves to it, why the last attempt failed (and
#: what kind of failure: ``"operator"`` / ``"tag"`` / ``"missing"`` /
#: ``"other"``), the port it pointed the service at, and this device's tags
#: when it did (a change re-applies — see :func:`reconcile`).
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
    """``(returncode, combined output)``; ``(-1, reason)`` when it couldn't run.
    ``args`` start with ``"tailscale"``, resolved by :mod:`backend.tailscale_cli`."""
    try:
        cp = _tailscale_cli.run(
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


def _json_cmd(args: list) -> Optional[dict]:
    """A ``tailscale … --json`` command's object; None when it couldn't be run,
    failed, or didn't print JSON (so callers can tell "unknown" from "empty")."""
    if _tailscale_cli.tailscale_bin() is None:
        return None
    try:
        cp = _tailscale_cli.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if cp.returncode != 0:
            return None
        data = json.loads(cp.stdout.decode("utf-8", "replace").strip() or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tailscale_status() -> dict:
    """``tailscale status --json`` (``{}`` when unavailable) — the shared,
    briefly cached snapshot."""
    return _tailscale_cli.status_json() or {}


def _serve_status() -> Optional[dict]:
    """``tailscale serve status --json`` (None when it can't be read)."""
    return _json_cmd(["tailscale", "serve", "status", "--json"])


def _suffix(data: dict) -> str:
    """The tailnet's MagicDNS suffix (``tail1234.ts.net``), or ``""``."""
    suffix = data.get("MagicDNSSuffix") or (data.get("CurrentTailnet") or {}).get(
        "MagicDNSSuffix"
    )
    return str(suffix or "").strip(".").lower()


def _self(data: dict) -> dict:
    node = data.get("Self") if isinstance(data, dict) else None
    return node if isinstance(node, dict) else {}


def _tags(data: dict) -> Optional[list]:
    """This device's ACL tags; None when ``status`` couldn't be read."""
    node = _self(data)
    if not node:
        return None
    tags = node.get("Tags") or []
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _tagged(data: dict) -> Optional[bool]:
    tags = _tags(data)
    return None if tags is None else bool(tags)


def _capmap(data: dict) -> Optional[dict]:
    """This node's capability map; None when ``status`` doesn't carry one
    (unreadable, or a client that predates ``CapMap`` in ``status --json``)."""
    node = _self(data)
    if "CapMap" not in node:
        return None
    capmap = node.get("CapMap")
    return capmap if isinstance(capmap, dict) else {}


def _host_vips(data: dict, name: str) -> Optional[list]:
    """The VIPs the control plane told this node to answer ``svc:<name>`` on.

    That is the ``service-host`` capability — ``[{"svc:<name>": [ips]}]``
    (``tailcfg.ServiceIPMappings``) — and it is what approval looks like from
    this end. ``None`` when this node is not (or not yet) a host, or when the
    capability map can't be read; :func:`_approved` tells those apart.
    """
    capmap = _capmap(data) or {}
    entries = capmap.get("service-host")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and service_id(name) in entry:
            vips = entry.get(service_id(name))
            return [str(v) for v in vips] if isinstance(vips, list) else []
    return None


def _approved(data: dict, name: str) -> Optional[bool]:
    """Whether the control plane has made this node a host of ``svc:<name>``.

    Only the ``service-host`` capability counts. ``services/<name>`` — which a
    node gets as soon as it advertises, approved or not — only says the
    service exists (:func:`_defined`); reading it as approval is how this used
    to report ✓ while the phone timed out. ``None`` = can't tell.
    """
    if _capmap(data) is None:
        return None
    return _host_vips(data, name) is not None


def _routed(data: dict, name: str) -> Optional[bool]:
    """Whether the service's VIPs are routed to this node (in its
    ``AllowedIPs``) — the last step before traffic actually arrives here.
    ``None`` when there are no VIPs to check or no route table to check them
    against."""
    vips = _host_vips(data, name)
    if not vips:
        return None
    allowed = _self(data).get("AllowedIPs")
    if not isinstance(allowed, list):
        return None
    nets = []
    for a in allowed:
        try:
            nets.append(ipaddress.ip_network(str(a), strict=False))
        except ValueError:
            continue
    for vip in vips:
        try:
            ip = ipaddress.ip_address(vip)
        except ValueError:
            return None
        if not any(ip.version == n.version and ip in n for n in nets):
            return False
    return True


def _defined(data: dict, name: str) -> Optional[bool]:
    """True when this node can see ``svc:<name>``: its ``services/<name>``
    capability, a host mapping, or the service's MagicDNS record. Never False
    — a device that doesn't advertise a service isn't told about it, so not
    seeing it here is no proof it's undefined."""
    capmap = _capmap(data) or {}
    if ("services/" + name) in capmap or _host_vips(data, name) is not None:
        return True
    suffix = _suffix(data)
    if suffix:
        want = "%s.%s" % (name, suffix)
        for rec in data.get("ExtraRecords") or []:
            if isinstance(rec, dict):
                if str(rec.get("Name") or "").rstrip(".").lower() == want:
                    return True
    return None


def _serve_live(serve: Optional[dict], name: str, port) -> Optional[bool]:
    """Whether the live serve config still carries ``svc:<name>`` on
    :data:`SERVICE_PORT` with HTTPS, proxying to ``port``. None = unreadable."""
    if serve is None or not port:
        return None
    svc = (serve.get("Services") or {}).get(service_id(name))
    if not isinstance(svc, dict):
        return False
    tcp = (svc.get("TCP") or {}).get(str(SERVICE_PORT))
    if not (isinstance(tcp, dict) and tcp.get("HTTPS")):
        return False
    targets = {
        "%s:%d" % (h, int(port)) for h in ("http://127.0.0.1", "http://localhost")
    }
    for hostport, web in (svc.get("Web") or {}).items():
        if not str(hostport).endswith(":%d" % SERVICE_PORT) or not isinstance(
            web, dict
        ):
            continue
        root = (web.get("Handlers") or {}).get("/")
        if isinstance(root, dict) and str(root.get("Proxy") or "").rstrip("/") in (
            targets
        ):
            return True
    return False


def _machine(data: dict) -> dict:
    """How to find this device in the admin console: its MagicDNS name, the
    OS hostname, its Tailscale IPv4, and — when Tailscale de-duplicated the
    name (``box`` → ``box-1``, because another device already has ``box``) —
    the name it is NOT, which is the one people go looking for."""
    node = _self(data)
    dns = str(node.get("DNSName") or "").rstrip(".").lower()
    ip = ""
    for a in node.get("TailscaleIPs") or []:
        if ":" not in str(a):
            ip = str(a)
            break
    label = dns.split(".", 1)[0]
    duplicate_of = ""
    m = re.match(r"^(.+)-\d+$", label)
    if m:
        for peer in (data.get("Peer") or {}).values():
            if not isinstance(peer, dict):
                continue
            if str(peer.get("DNSName") or "").split(".", 1)[0].lower() == m.group(1):
                duplicate_of = m.group(1)
                break
    return {
        "hostname": str(node.get("HostName") or ""),
        "dns": dns,
        "ip": ip,
        "duplicate_of": duplicate_of,
    }


def _host_tag(tags: list) -> str:
    """The tag the policy snippet should name: this device's own (preferring
    :data:`DEFAULT_TAG`), else the default."""
    if DEFAULT_TAG in tags:
        return DEFAULT_TAG
    return tags[0] if tags else DEFAULT_TAG


#: The MindFlock server port the policy block opens between devices when
#: this process can't tell its own (see :func:`_server_port`).
DEFAULT_PORT = 8765


def _server_port() -> int:
    try:
        from backend.web.core import mobile_access

        return int(mobile_access._server_port())
    except Exception:  # noqa: BLE001
        return DEFAULT_PORT


def policy_block(name: str, tag: str, port: int = DEFAULT_PORT) -> str:
    """Everything MindFlock needs in the tailnet policy file, as ONE block
    to copy: who may apply ``tag``, the services auto-approver for
    ``svc:<name>``, grants so tagged devices and your own devices reach each
    other's MindFlock on ``port`` (and 443, the shared link) — which a custom
    policy such as the common ``autogroup:self`` rule otherwise blocks,
    silently — plus a ``tests`` stanza the policy editor checks on save.

    Merge-safe: a policy file can't hold the same key twice, and any tailnet
    with a tagged device already has ``tagOwners``. The block says so, in
    comments (the policy file is HuJSON), and every line inside a key ends in
    a comma so it can be moved into an existing key as-is."""
    svc = service_id(name)
    return (
        "// MindFlock. Policy has none of these keys yet? Paste the whole block.\n"
        '// Already has one (e.g. "tagOwners")? Move the lines inside it into\n'
        "// your existing key instead: a key can't appear twice.\n"
        '"tagOwners": {\n'
        '  "%(tag)s": ["autogroup:admin"],\n'
        "},\n"
        '"autoApprovers": {\n'
        '  "services": {\n'
        '    "%(svc)s": ["%(tag)s"],\n'
        "  },\n"
        "},\n"
        '"grants": [\n'
        "  // Your MindFlock devices reach each other (and the shared link).\n"
        '  {"src": ["%(tag)s"], "dst": ["%(tag)s"], "ip": ["tcp:%(port)d", "tcp:443"]},\n'
        '  {"src": ["%(tag)s"], "dst": ["autogroup:member"], "ip": ["tcp:%(port)d"]},\n'
        "  // Your own phone and computers reach them.\n"
        '  {"src": ["autogroup:member"], "dst": ["%(tag)s"], "ip": ["tcp:%(port)d", "tcp:443"]},\n'
        "  // The shared phone link.\n"
        '  {"src": ["autogroup:member"], "dst": ["%(svc)s"], "ip": ["tcp:443"]},\n'
        "],\n"
        '"tests": [\n'
        '  {"src": "%(tag)s", "accept": ["%(tag)s:%(port)d"]},\n'
        "],"
    ) % {"tag": tag, "svc": svc, "port": port}


def device_grants(tag: str, port: int = DEFAULT_PORT) -> str:
    """Just the grants that let your MindFlock devices reach each other on
    ``port`` — the part of :func:`policy_block` a device that discovery
    "timed out" on needs (a custom policy such as the common
    ``autogroup:self`` rule drops those packets silently). The same lines,
    so pasting both never conflicts."""
    return (
        "// MindFlock: your devices reach each other on tcp:%(port)d.\n"
        '// Policy already has "grants"? Move these lines inside it.\n'
        '"grants": [\n'
        '  {"src": ["%(tag)s"], "dst": ["%(tag)s"], "ip": ["tcp:%(port)d", "tcp:443"]},\n'
        '  {"src": ["%(tag)s"], "dst": ["autogroup:member"], "ip": ["tcp:%(port)d"]},\n'
        '  {"src": ["autogroup:member"], "dst": ["%(tag)s"], "ip": ["tcp:%(port)d", "tcp:443"]},\n'
        '  {"src": ["autogroup:member"], "dst": ["autogroup:member"], "ip": ["tcp:%(port)d"]},\n'
        "],"
    ) % {"tag": tag, "port": port}


def _explain(output: str) -> Tuple[str, str]:
    """``(kind, message)`` for ``tailscale serve``'s refusal — the fix, where
    we know it. ``kind`` is ``"operator"``, ``"tag"`` or ``"other"``."""
    low = output.lower()
    if "access denied" in low or "permission" in low or "operator" in low:
        return "operator", (
            "Tailscale refused to change serve config for this user. Run "
            "`%s` once, then press Re-check." % OPERATOR_FIX
        )
    if "tag" in low and ("service" in low or "host" in low):
        return "tag", (
            "Only tagged devices can host a Tailscale Service. Tag this device "
            "in the Tailscale admin console, then press Re-check."
        )
    return "other", (output.splitlines()[-1] if output else "tailscale serve failed")


def _withdraw_locked(name: str) -> None:
    _run(["tailscale", "serve", "clear", service_id(name)])
    if _STATE["host"]:
        _auth.forget_fronted_host(_STATE["host"])
    _STATE.update(name="", host="", error="", kind="", port=0, tags=None)


def apply(port: int) -> dict:
    """Make this device's advertisement match ``general.shared_link``.

    Clears a previously advertised name that is no longer wanted, then
    (re)configures the wanted one: ``clear`` first so a changed port or a
    drained service comes back as a fresh config, then ``serve`` (which
    configures and advertises in one step — and is what the auto-approver
    reacts to). Unconditional: :func:`reconcile` is the "only if needed" form.
    Blocking — call it off the event loop. Returns :func:`status`.
    """
    want = configured_name()
    with _LOCK:
        if _STATE["name"] and _STATE["name"] != want:
            _withdraw_locked(_STATE["name"])
        if not want:
            _STATE.update(error="", kind="")
            return status()
        if _tailscale_cli.tailscale_bin() is None:
            _STATE.update(
                name="", host="", error="Tailscale is not installed.", kind="missing"
            )
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
            kind, error = _explain(out)
            _STATE.update(name="", host="", error=error, kind=kind)
            return status()
        data = _tailscale_status()
        suffix = _suffix(data)
        host = "%s.%s" % (want, suffix) if suffix else ""
        if host:
            _auth.allow_fronted_host(host)
        _STATE.update(
            name=want, host=host, error="", kind="", port=port, tags=_tags(data)
        )
    return status()


def reconcile(port: int, *, nudge: bool = False) -> dict:
    """:func:`apply`, but only when Tailscale has drifted from what we set up.

    Re-applies when the setting and what this process advertises disagree
    (a changed name, or a last attempt that failed — so fixing the cause and
    waiting is enough), when the live ``tailscale serve status --json`` no
    longer carries our handler (serve config cleared behind our back), or when
    this device's tags changed since the last apply (the auto-approver only
    looks at an advertisement when it is made, so one made before tagging
    stays pending until it is made again). ``nudge`` (the Re-check button,
    a re-save) also re-advertises a tagged host that still isn't approved.
    Otherwise touches nothing. Blocking; returns :func:`status`.
    """
    want = configured_name()
    with _LOCK:
        if not want and not _STATE["name"]:
            return status()
        if _STATE["name"] != want or _STATE.get("port") != port:
            return apply(port)
        if _serve_live(_serve_status(), want, port) is False:
            return apply(port)
        data = _tailscale_status()
        tags = _tags(data)
        if tags is not None and tags != _STATE.get("tags"):
            return apply(port)
        if nudge and tags and _approved(data, want) is not True:
            return apply(port)
    return status()


async def recheck_loop(port_fn: Callable[[], int]) -> None:
    """Run :func:`reconcile` every :data:`RECHECK_INTERVAL` seconds while the
    shared link is on (started by the server lifespan; startup itself already
    applied, so the first pass waits one interval). Never dies."""
    while True:
        await asyncio.sleep(RECHECK_INTERVAL)
        try:
            if configured_name() or _STATE["name"]:
                before = advertised_url()
                await asyncio.to_thread(reconcile, port_fn())
                if advertised_url() != before:
                    # Notification taps point at the phone URL — it changed
                    # (e.g. the link came up once a fixed cause let it).
                    from backend.web.core import mobile_announce

                    await asyncio.to_thread(mobile_announce.refresh_url)
        except Exception:  # noqa: BLE001 — the loop must never die
            pass


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


def _step(sid: str, title: str, state: str, reason: str) -> dict:
    return {"id": sid, "title": title, "state": state, "reason": reason}


def _steps(st: dict) -> list:
    """The setup checklist Settings → Mobile renders: one step per thing a
    host needs, each ``ok`` / ``fail`` / ``unknown`` with a one-line reason.
    Built from :func:`status`'s facts only; the UI adds each step's fix."""
    svc = st["service"]
    m = st["machine"]
    who = m["dns"] or m["hostname"] or "This device"
    kind, error = st["error_kind"], st["error"]
    advertised, tagged = st["advertised"], st["tagged"]
    approved, routed = st["approved"], st["routed"]

    if kind == "missing":
        op = _step("operator", "Tailscale serve access", "fail", error)
    elif kind == "operator":
        op = _step(
            "operator",
            "Tailscale serve access",
            "fail",
            "Tailscale won't let this user change serve config — run the "
            "command below once.",
        )
    elif advertised:
        op = _step(
            "operator",
            "Tailscale serve access",
            "ok",
            "Serve config is set: %s → 127.0.0.1:%s." % (svc, st.get("port") or "?"),
        )
    elif error:
        op = _step("operator", "Tailscale serve access", "unknown", error)
    else:
        op = _step("operator", "Tailscale serve access", "unknown", "Not applied yet.")

    if tagged:
        tag = _step(
            "tag",
            "Tag this device",
            "ok",
            "%s is tagged %s." % (who, ", ".join(st["tags"])),
        )
    elif tagged is False:
        tag = _step(
            "tag",
            "Tag this device",
            "fail",
            "%s has no tag — only tagged devices can host a service." % who,
        )
    else:
        tag = _step(
            "tag",
            "Tag this device",
            "unknown",
            "Couldn't read this device's tags from `tailscale status`.",
        )

    if st["defined"]:
        define = _step(
            "define",
            "Define the service",
            "ok",
            "%s exists — this device can see it." % svc,
        )
    else:
        define = _step(
            "define",
            "Define the service",
            "unknown",
            "This device can't see %s yet; define it with port tcp:%d if you "
            "haven't." % (svc, SERVICE_PORT),
        )

    if approved:
        policy = _step(
            "policy",
            "Approve hosts automatically",
            "ok",
            "This device has been approved for %s." % svc,
        )
    else:
        policy = _step(
            "policy",
            "Approve hosts automatically",
            "unknown",
            "The policy file can't be read from here; add the block below once "
            "per tailnet (or approve each host on the Services page).",
        )

    if approved is None:
        appr = _step(
            "approval",
            "Approved as a host",
            "unknown",
            "Couldn't read this device's capabilities from `tailscale status` "
            "(an older Tailscale?).",
        )
    elif approved and routed:
        appr = _step(
            "approval",
            "Approved as a host",
            "ok",
            "Tailscale made this device a host of %s and routes its address "
            "here." % svc,
        )
    elif approved and routed is False:
        appr = _step(
            "approval",
            "Approved as a host",
            "fail",
            "Approved, but %s's address isn't routed to this device yet — "
            "Re-check in a minute." % svc,
        )
    elif approved:
        appr = _step(
            "approval",
            "Approved as a host",
            "unknown",
            "Approved, but couldn't confirm %s's address routes here." % svc,
        )
    elif not advertised:
        appr = _step(
            "approval",
            "Approved as a host",
            "fail",
            "This device isn't advertising %s (see step 1)." % svc,
        )
    elif not tagged:
        appr = _step(
            "approval",
            "Approved as a host",
            "fail",
            "Advertised, but Tailscale won't approve an untagged host (step 2).",
        )
    else:
        appr = _step(
            "approval",
            "Approved as a host",
            "fail",
            "Advertised, but Tailscale hasn't made this device a host yet — "
            "approve it on the Services page, or add the auto-approver and "
            "press Re-check.",
        )

    if advertised and approved and routed:
        reason = (
            "Everything this device can check passes — scan the QR on a phone "
            "that is on Tailscale."
        )
    else:
        reason = "The link won't answer from here until the steps above pass."
    phone = _step("phone", "Test from your phone", "unknown", reason)
    return [op, tag, define, policy, appr, phone]


def status() -> dict:
    """The shared link as Settings → Mobile shows it.

    ``enabled`` is the setting; ``advertised`` whether this device's
    ``tailscale serve`` for it is up (checked against the live serve config);
    ``tagged`` whether it has the tag a host needs; ``defined`` whether the
    service is visible from here; ``approved`` whether Tailscale made this
    device a host and ``routed`` whether the service's address reaches it.
    The tri-state ones are ``None`` when this device can't tell — shown as
    "unknown", never as ✓. ``steps`` is the setup checklist built from them;
    ``machine``, ``tag`` and ``policy`` (:func:`policy_block`) fill in its
    fixes.
    """
    name = configured_name()
    if not name:
        return {"enabled": False}
    data = _tailscale_status()
    suffix = _suffix(data)
    with _LOCK:
        mine = _STATE["name"] == name
        error = _STATE["error"]
        kind = _STATE.get("kind", "") if error else ""
        port = _STATE.get("port") if mine else None
    advertised = mine and _serve_live(_serve_status(), name, port) is not False
    tags = _tags(data) or []
    tag = _host_tag(tags)
    approved = _approved(data, name)
    st = {
        "enabled": True,
        "name": name,
        "service": service_id(name),
        "url": "https://%s.%s/m" % (name, suffix) if suffix else "",
        "advertised": advertised,
        "port": port,
        "tagged": _tagged(data),
        "tags": tags,
        "tag": tag,
        "defined": _defined(data, name),
        "approved": approved,
        "routed": _routed(data, name) if approved else None,
        "machine": _machine(data),
        "error": error,
        "error_kind": kind,
        "operator_fix": OPERATOR_FIX,
        # One block for the whole policy file, with this server's real port.
        "policy": policy_block(name, tag, _server_port()),
        "admin": {
            "machines": ADMIN_MACHINES,
            "services": ADMIN_SERVICES,
            "policy": ADMIN_POLICY,
        },
    }
    st["steps"] = _steps(st)
    return st
