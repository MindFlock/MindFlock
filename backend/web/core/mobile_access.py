"""Phone access to this server: tailnet URL discovery, QR codes, the banner.

Owns everything behind "how do I reach MindFlock from my phone?":

* sniffing the port the server is bound to (:func:`_server_port`),
* Tailscale node discovery (:func:`_tailscale_info`) and whether
  ``tailscale serve`` fronts this port (:func:`_tailscale_serves_port`),
* QR rendering — terminal half-blocks for the startup banner
  (:func:`_qr_lines`) and inline SVG for Settings → Mobile (:func:`_mobile_svg`),
* the startup banner text (:func:`_mobile_banner`) and the ``/api/mobile``
  payload (:func:`_mobile_info`), which mirror each other: both encode the best
  URL a phone on the tailnet can actually reach, with the access token baked
  into the QR (``?token=…``) when the auth gate is on.

When the shared phone link is up (:mod:`backend.web.core.shared_link`), all
three advertise IT instead of this device's own URL — one URL for the whole
fleet — and the QR carries every paired device's token, not just this one's,
so the scan signs the phone in on whichever device Tailscale routes it to.

Split out of ``backend.web.server`` (which re-imports these names — tests and
the routes reference them through the server namespace).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple

from backend import tailscale_cli as _tailscale_cli
from backend.web.core import auth as _auth
from backend.web.core import shared_link as _shared_link


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _server_port() -> int:
    """Best-effort port this server is bound to (for the banner only).

    uvicorn doesn't hand the app its bound port, so we sniff it from the env
    (``UVICORN_PORT`` / ``PORT``) and the ``--port`` CLI arg, defaulting to the
    8765 used in the module docstring's run command.
    """
    for var in ("UVICORN_PORT", "PORT"):
        v = os.environ.get(var, "")
        if v.isdigit():
            return int(v)
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
        if a.startswith("--port="):
            tail = a.split("=", 1)[1]
            if tail.isdigit():
                return int(tail)
    return 8765


def _tailscale_info() -> tuple:
    """``(magicdns_name | None, ipv4 | None)`` for this node if Tailscale is up.

    No name while the tailnet has MagicDNS switched off: the name wouldn't
    resolve on a phone, so the QR and URLs lead with the IP instead."""
    data = _tailscale_cli.status_json()
    if data is None:
        return None, None
    self_ = data.get("Self") or {}
    name = (self_.get("DNSName") or "").rstrip(".") or None
    if (data.get("CurrentTailnet") or {}).get("MagicDNSEnabled") is False:
        name = None
    return name, _tailscale_cli.self_ipv4(data) or None


def _tailscale_serves_port(port: int) -> bool:
    """True if ``tailscale serve`` is proxying HTTPS to this server's port."""
    if _tailscale_cli.tailscale_bin() is None:
        return False
    try:
        cp = _tailscale_cli.run(
            ["tailscale", "serve", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if cp.returncode != 0:
        return False
    out = cp.stdout.decode("utf-8", "replace")
    return any(
        s in out for s in (":%d" % port, "localhost:%d" % port, "127.0.0.1:%d" % port)
    )


def tailnet_url() -> Tuple[Optional[str], bool]:
    """``(the phone URL for this machine, whether it works right now)``.

    The same "best URL a phone on the tailnet can reach" the banner and the
    Settings → Mobile QR advertise — but deliberately **without** ``?token=``:
    this one is handed to a third party (the ntfy push in
    :mod:`backend.web.core.mobile_announce`), and the access token is not
    theirs to store. Same call ``ntfy.strip_token_param`` makes for the
    tap-to-open URL.

    ``(None, False)`` when Tailscale isn't up — there is no phone URL to give,
    and inventing one would just be wrong. The second element is False when the
    URL is *right but not live yet*: uvicorn is still bound to 127.0.0.1
    (``mindflock serve local``), so the tailnet address only starts answering
    after a restart in tailscale mode.
    """
    shared = _shared_link.advertised_url()
    if shared:
        # One URL for every device, answered by whichever is up — the one to
        # hand out. Live as soon as this device advertises it (`tailscale
        # serve` fronts 127.0.0.1, so local mode doesn't matter).
        return shared, True
    srv = _server()
    port = srv._server_port()
    name, ip = srv._tailscale_info()
    if name and srv._tailscale_serves_port(port):
        # `tailscale serve` fronts localhost with HTTPS, so this URL works even
        # while uvicorn stays bound to 127.0.0.1 — local mode is not a caveat.
        return "https://%s/m" % name, True
    host = name or ip
    if not host:
        return None, False
    return "http://%s:%d/m" % (host, port), not srv._local_only_mode()


def _signin_tokens(shared: bool) -> list:
    """The tokens a phone QR carries: this server's own (when the auth gate is
    on), then the fleet key when this device is one of "Your devices" (every
    member accepts it — but the sign-in cookie belongs to the origin scanned,
    so a per-device QR signs the phone in on THIS device only; the shared
    link, one origin answered by any member, is what reaches all of them),
    plus — for the shared link — every paired device's, so whichever device
    answers the scan finds its own among them. ``[]`` with the gate off.
    """
    try:
        if not _auth.auth_enabled():
            return []
        own = _auth.get_token()
    except Exception:  # noqa: BLE001
        return []
    tokens = [own] if own else []
    try:
        from backend.web.core import fleet as _fleet

        key = _fleet.fleet_key() if _fleet.in_fleet() else ""
        if key and key not in tokens:
            tokens.append(key)
    except Exception:  # noqa: BLE001 — the own token still works here
        pass
    if shared:
        try:
            from backend.web.core import remote as _remote

            for tok in _remote.paired_tokens().values():
                if tok not in tokens:
                    tokens.append(tok)
        except Exception:  # noqa: BLE001 — the own token still works here
            pass
    return tokens


def _with_tokens(url: str, tokens: list) -> str:
    """``url`` with one ``token=`` per entry of ``tokens`` appended."""
    if not tokens:
        return url
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return url + sep + "&".join("token=%s" % quote(t, safe="") for t in tokens)


def _qr_lines(data: str):
    """``data`` as a terminal QR code (str), or None if ``segno`` isn't installed.

    Uses half-block compact rendering so the code is short enough to fit a
    terminal and still scans from a phone camera.
    """
    try:
        import io

        import segno
    except Exception:  # noqa: BLE001 — segno is an optional dep
        return None
    try:
        qr = segno.make(data, error="m")
        buf = io.StringIO()
        try:
            qr.terminal(buf, compact=True, border=2)
        except TypeError:  # older segno without `compact`
            qr.terminal(buf, border=2)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _local_only_mode() -> bool:
    """True when the server was started in local mode (bound to 127.0.0.1).

    ``mindflock serve local`` / run.py export the resolved mode as
    ``CS_WEB_MODE`` so the banner knows tailnet URLs can't work (F7)."""
    return (os.environ.get("CS_WEB_MODE") or "").strip().lower() in (
        "local",
        "localhost",
    )


def _serve_mode_setting() -> str:
    """The persisted Settings → Mobile serve mode (``general.serve_mode``),
    normalized to ``"tailscale"`` / ``"local"`` / ``""``. Never raises."""
    try:
        from backend.config.settings import load_settings

        s = (load_settings().general.serve_mode or "").strip().lower()
        return s if s in ("local", "tailscale") else ""
    except Exception:  # noqa: BLE001 — advisory only
        return ""


def _mobile_banner(*, for_log: bool = False) -> str:
    """The startup banner. ``for_log=True`` builds the copy written to the log
    file: the access token is redacted and the QR is dropped entirely — the QR
    *encodes* ``?token=…``, so block-art in a log file would still be a
    scannable copy of the secret. Secrets belong on the operator's console
    (stdout) and in Settings → Security, never in ``mindflock.log`` (which
    ``GET /api/logs`` serves back out)."""
    srv = _server()
    port = srv._server_port()
    lines = [
        "",
        "  ┌─ MindFlock mobile view ──────────────────────────────────",
        "  │  Local:      http://127.0.0.1:%d/m" % port,
    ]
    shared = _shared_link.advertised_url()
    if shared:
        # The fleet-wide link: the same URL whichever device is up, so it's
        # the only one worth a QR. Works in local mode too (tailscale serve
        # fronts 127.0.0.1).
        lines.append("  │  Shared:     %s   (any of your devices)" % shared)
        lines.append("  └────────────────────────────────────────────────────────")
        return "\n".join(lines + _banner_signin(srv, shared, for_log, shared=True))
    if srv._local_only_mode():
        # Bound to 127.0.0.1: no tailnet URL (or QR) can reach this server —
        # printing them would just be wrong. Point at tailscale mode instead.
        lines.append("  │  (local mode — run `mindflock serve tailscale`")
        lines.append("  │   for phone/tailnet access)")
        lines.append("  └────────────────────────────────────────────────────────")
        lines.append("")
        return "\n".join(lines)
    name, ip = srv._tailscale_info()
    # The URL we encode in the QR — the best one a phone on the tailnet can use.
    qr_url = None
    if name and srv._tailscale_serves_port(port):
        # `tailscale serve` fronts localhost with HTTPS, so this is the only
        # URL that works (uvicorn stays on 127.0.0.1); don't show the others.
        qr_url = "https://%s/m" % name
        lines.append("  │  Tailscale:  %s   (via `tailscale serve`)" % qr_url)
    elif name or ip:
        # Direct access — requires tailscale mode (uvicorn bound to this
        # node's Tailscale addresses too, not just 127.0.0.1).
        if name:
            qr_url = "http://%s:%d/m" % (name, port)
            lines.append("  │  Tailscale:  %s" % qr_url)
        if ip:
            ip_url = "http://%s:%d/m" % (ip, port)
            lines.append("  │  Tailscale:  %s" % ip_url)
            qr_url = qr_url or ip_url
    else:
        lines.append("  │  Tailscale:  not detected — run `tailscale up`, then reach")
        lines.append("  │             this host's 100.x.y.z IP at :%d/m" % port)
    lines.append("  └────────────────────────────────────────────────────────")
    return "\n".join(lines + _banner_signin(srv, qr_url, for_log, shared=False))


def _banner_signin(srv, qr_url, for_log: bool, *, shared: bool) -> list:
    """The banner's sign-in tail: the access token and the QR for ``qr_url``.

    Auth: show the token and bake it into the QR (?token=…) so a phone lands
    signed in from one scan. Only when the gate is actually on. For the shared
    link the QR carries every paired device's token too (:func:`_signin_tokens`).
    """
    lines: list = []
    tokens = _signin_tokens(shared)
    token = tokens[0] if tokens else ""
    if token:
        lines.append("")
        if for_log:
            lines.append(
                "  Access token: <redacted — see the startup console"
                " or Settings → Security>"
            )
        else:
            lines.append("  Access token (enter it on the sign-in page): %s" % token)
    qr_target = qr_url
    if qr_url and token:
        if for_log:
            qr_target = None  # the QR encodes ?token=… — never log it
        else:
            qr_target = _with_tokens(qr_url, tokens)

    if qr_target:
        qr = srv._qr_lines(qr_target)
        if qr:
            lines.append("")
            lines.append(
                "  Scan from a phone on your tailnet"
                + (" (signs in automatically):" if token else ":")
            )
            lines.append(qr)
        else:
            lines.append("  (pip install segno  for a scannable QR code here)")
    lines.append("")
    return lines


def qr_svg(data: str):
    """``data`` as an inline, scannable SVG QR (dark-on-white), or None.

    The one QR renderer any settings screen shares — Settings → Mobile's phone
    URL and the notify addon's ntfy subscribe URL both come through here.
    """
    try:
        import io

        import segno
    except Exception:  # noqa: BLE001 — segno is optional
        return None
    try:
        import re

        qr = segno.make(data, error="m")
        buf = io.BytesIO()
        # xmldecl=False: drop the <?xml?> prolog so the string is a bare <svg>
        # that renders when injected via innerHTML in the settings screen.
        qr.save(
            buf,
            kind="svg",
            scale=4,
            border=2,
            dark="#0f1117",
            light="#ffffff",
            xmldecl=False,
        )
        svg = buf.getvalue().decode("utf-8")
        # segno emits fixed width/height with NO viewBox, so any CSS size larger
        # than that intrinsic size leaves the QR pinned top-left with dead space
        # around it (visible on the Settings → Mobile card, esp. on phones).
        # Replace the fixed dimensions with a viewBox so the QR scales to fill
        # whatever box the CSS gives it. Match on the <svg …> open tag only.
        m = re.match(r"(<svg\b)([^>]*)>", svg)
        if m:
            attrs = m.group(2)
            w = re.search(r'\bwidth="([\d.]+)"', attrs)
            h = re.search(r'\bheight="([\d.]+)"', attrs)
            if w and h:
                attrs = re.sub(r'\s*\b(width|height)="[\d.]+"', "", attrs)
                attrs += f' viewBox="0 0 {w.group(1)} {h.group(1)}"'
                svg = m.group(1) + attrs + ">" + svg[m.end() :]
        return svg
    except Exception:  # noqa: BLE001
        return None


#: Historical name, kept because server.py re-exports it (and _mobile_info calls
#: it back through the server module).
_mobile_svg = qr_svg


def _mobile_info(include_tokens: bool = True) -> dict:
    """Mobile (/m) URLs + a scannable QR + access token for Settings → Mobile.

    Mirrors the startup banner (:func:`_mobile_banner`): the QR encodes the best
    tailnet URL a phone can actually reach (with ``?token=`` baked in when the
    auth gate is on), and is omitted in local-only mode where no phone URL works.
    Without ``include_tokens`` (a caller that may not see this device's own
    token — see :func:`backend.web.core.auth.may_see_own_token`) no token goes
    anywhere: ``token`` is None and the QR is the bare URL.
    """
    srv = _server()
    port = srv._server_port()
    local_url = "http://127.0.0.1:%d/m" % port
    urls = [{"label": "Local", "url": local_url}]
    note = None
    qr_url = None
    serve_mode = _serve_mode_setting()
    # The owner's login goes into the policy snippet only for a caller that
    # may see it (the same rule as the phone-app step's login).
    shared = _shared_link.status(_shared_link.owner_login() if include_tokens else "")
    shared_url = _shared_link.advertised_url()
    if shared.get("enabled"):
        shared["devices"] = _shared_link_devices(shared["name"])
    if shared_url:
        # The fleet-wide link leads, and is what the QR encodes: it keeps
        # working when this device is off. This device's own URLs stay listed
        # below it (labelled as such) for when you want this machine exactly.
        urls.append({"label": "Shared link", "url": shared_url})
        qr_url = shared_url
    if srv._local_only_mode():
        if serve_mode == "tailscale":
            # The user already flipped the toggle; only the restart is missing.
            note = "Tailscale mode is on — restart the server to apply it."
        else:
            note = "Local mode — turn on tailscale mode for phone access."
    else:
        name, ip = srv._tailscale_info()
        own = "This device" if shared_url else "Tailscale"
        if name and srv._tailscale_serves_port(port):
            dev_url = "https://%s/m" % name
            urls.append({"label": own, "url": dev_url})
            qr_url = qr_url or dev_url
        elif name or ip:
            if name:
                dev_url = "http://%s:%d/m" % (name, port)
                urls.append({"label": own, "url": dev_url})
                qr_url = qr_url or dev_url
            if ip:
                ip_url = "http://%s:%d/m" % (ip, port)
                urls.append({"label": own + " (IP)", "url": ip_url})
                qr_url = qr_url or ip_url
        else:
            note = "Tailscale not detected — run `tailscale up` for phone access."
    if shared_url:
        note = None  # the shared link works whatever this server is bound to

    tokens = _signin_tokens(bool(shared_url)) if include_tokens else []
    token = (tokens[0] if tokens else "") if include_tokens else None

    qr_target = qr_url  # only tailnet URLs are reachable from a phone
    if qr_target and tokens:
        qr_target = _with_tokens(qr_target, tokens)

    return {
        "urls": urls,
        "qr_target": qr_target,
        "qr_svg": srv._mobile_svg(qr_target) if qr_target else None,
        "token": token,
        "local_only": srv._local_only_mode(),
        "serve_mode": serve_mode,
        "note": note,
        "shared": shared,
        "phone_app": _phone_app(bool(include_tokens)),
    }


#: Where a phone gets Tailscale (the page sends iOS/Android to their store).
PHONE_APP_URL = "https://tailscale.com/download"


def _tailscale_login() -> str:
    """The Tailscale account the phone should sign in as: this device's own
    login, else — on a tagged device, which belongs to no login — the one
    person owning the tailnet's untagged devices. ``""`` when unclear."""
    try:
        from backend.web.core import tailnet_trust as _trust

        h = _tailscale_cli.health()
        if h.get("user"):
            return h["user"]
        logins = _trust.status().get("logins") or []
        return logins[0] if len(logins) == 1 else ""
    except Exception:  # noqa: BLE001 — copy only
        return ""


_PHONE_APP_QR: list = []


def _phone_app(include_login: bool) -> dict:
    """Step 1 of phone setup: Tailscale on the phone, signed in to the same
    account — without it the MindFlock QR opens a URL that just hangs."""
    if not _PHONE_APP_QR:
        _PHONE_APP_QR.append(_server()._mobile_svg(PHONE_APP_URL) or "")
    return {
        "url": PHONE_APP_URL,
        "qr_svg": _PHONE_APP_QR[0],
        "login": _tailscale_login() if include_login else "",
    }


def _shared_link_devices(name: str) -> list:
    """The other devices that say they answer the same shared link (from the
    remote-control discovery hello) — the failover set, as far as this device
    can see it. ``[{device, host, reachable}]``."""
    try:
        from backend.web.core import remote as _remote

        return [
            {"device": d["device"], "host": d["host"], "reachable": d["reachable"]}
            for d in _remote.devices_json().get("devices", [])
            if d.get("shared_link") == name
        ]
    except Exception:  # noqa: BLE001
        return []
