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

Split out of ``backend.web.server`` (which re-imports these names — tests and
the routes reference them through the server namespace).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from backend.web.core import auth as _auth


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
    """``(magicdns_name | None, ipv4 | None)`` for this node if Tailscale is up."""
    if shutil.which("tailscale") is None:
        return None, None
    name = ip = None
    try:
        cp = subprocess.run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if cp.returncode == 0:
            data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
            self_ = data.get("Self") or {}
            dns = (self_.get("DNSName") or "").rstrip(".")
            if dns:
                name = dns
            for a in self_.get("TailscaleIPs") or []:
                if ":" not in a:  # first IPv4
                    ip = a
                    break
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return name, ip


def _tailscale_serves_port(port: int) -> bool:
    """True if ``tailscale serve`` is proxying HTTPS to this server's port."""
    if shutil.which("tailscale") is None:
        return False
    try:
        cp = subprocess.run(
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
        # Direct access — requires uvicorn bound to 0.0.0.0 (not 127.0.0.1).
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

    # Auth: show the token and bake it into the QR (?token=…) so a phone lands
    # signed in from one scan. Only when the gate is actually on.
    token = ""
    try:
        if _auth.auth_enabled():
            token = _auth.get_token()
    except Exception:  # noqa: BLE001
        token = ""
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
            sep = "&" if "?" in qr_url else "?"
            qr_target = "%s%stoken=%s" % (qr_url, sep, token)

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
    return "\n".join(lines)


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


def _mobile_info() -> dict:
    """Mobile (/m) URLs + a scannable QR + access token for Settings → Mobile.

    Mirrors the startup banner (:func:`_mobile_banner`): the QR encodes the best
    tailnet URL a phone can actually reach (with ``?token=`` baked in when the
    auth gate is on), and is omitted in local-only mode where no phone URL works.
    """
    srv = _server()
    port = srv._server_port()
    local_url = "http://127.0.0.1:%d/m" % port
    urls = [{"label": "Local", "url": local_url}]
    note = None
    qr_url = None
    serve_mode = _serve_mode_setting()
    if srv._local_only_mode():
        if serve_mode == "tailscale":
            # The user already flipped the toggle; only the restart is missing.
            note = "Tailscale mode is on — restart the server to apply it."
        else:
            note = "Local mode — turn on tailscale mode for phone access."
    else:
        name, ip = srv._tailscale_info()
        if name and srv._tailscale_serves_port(port):
            qr_url = "https://%s/m" % name
            urls.append({"label": "Tailscale", "url": qr_url})
        elif name or ip:
            if name:
                qr_url = "http://%s:%d/m" % (name, port)
                urls.append({"label": "Tailscale", "url": qr_url})
            if ip:
                ip_url = "http://%s:%d/m" % (ip, port)
                urls.append({"label": "Tailscale IP", "url": ip_url})
                qr_url = qr_url or ip_url
        else:
            note = "Tailscale not detected — run `tailscale up` for phone access."

    token = ""
    try:
        if _auth.auth_enabled():
            token = _auth.get_token()
    except Exception:  # noqa: BLE001
        token = ""

    qr_target = qr_url  # only tailnet URLs are reachable from a phone
    if qr_target and token:
        sep = "&" if "?" in qr_target else "?"
        qr_target = "%s%stoken=%s" % (qr_target, sep, token)

    return {
        "urls": urls,
        "qr_target": qr_target,
        "qr_svg": srv._mobile_svg(qr_target) if qr_target else None,
        "token": token,
        "local_only": srv._local_only_mode(),
        "serve_mode": serve_mode,
        "note": note,
    }
