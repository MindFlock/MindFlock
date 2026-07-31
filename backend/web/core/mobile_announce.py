"""Push the phone URL to ntfy whenever this machine becomes reachable.

The ntfy channel (:mod:`backend.web.core.ntfy`) exists so a session alert
reaches you with nothing open. This module closes the other half of that loop:
the phone also has to know *where* MindFlock is. Whenever both halves come
alive — Tailscale is up AND ntfy is configured — one push carries the tailnet
``/m`` URL, so opening the mobile view is a tap on a notification instead of a
QR scan at the desk. Three moments qualify, and they are exactly the three that
change the answer:

* the server starts (:data:`REASON_STARTUP` — from the startup warmups, next to
  the banner that prints the same URL to the console),
* the ntfy channel is switched on (:data:`REASON_NTFY` — before that there was
  nowhere to push it),
* Settings → Mobile turns on tailscale mode (:data:`REASON_MOBILE` — before
  that the URL was not going to work).

The same URL then rides along on *every* push: :func:`click_for` hands the
notify addon a cached, session-deep-linked copy for each session alert, so any
notification is one tap from the thing it is about. Cached because that path
runs on the event-bus thread, where a ``tailscale status`` shell-out has no
business being.

Deliberately *not* in any of it: the access token. These messages travel to —
and are stored on — a third-party ntfy server, so the URL goes bare, the same
call :func:`ntfy.strip_token_param` makes for the tap-to-open URL. The phone
signs in once with the token from Settings → Security and keeps the cookie.
"""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.parse
from typing import Optional, Tuple

from backend.web.core import auth as _auth
from backend.web.core import mobile_access, ntfy

#: Why we're announcing → the opening line of the push. Each is the answer to
#: "why is my phone buzzing", which is the only thing the reason has to carry.
REASON_STARTUP = "startup"
REASON_NTFY = "ntfy"
REASON_MOBILE = "mobile"

_LINES = {
    REASON_STARTUP: "MindFlock just started.",
    REASON_NTFY: "Phone push is on.",
    REASON_MOBILE: "Tailscale mode is on.",
}

#: Don't say the same thing twice inside this window. Turning on ntfy and
#: tailscale mode back to back is one intent, not two notifications — and a
#: server that restart-loops shouldn't narrate every attempt. Keyed by
#: ``(url, live)`` so a URL that becomes *live* still announces: that is the
#: push the user is actually waiting for after "restart to apply".
_DEDUPE_SECONDS = 120.0

_LOCK = threading.Lock()
_SEEN: dict = {}  # (url, live) -> monotonic ts of the last push


def _should_send(key: Tuple[str, bool]) -> bool:
    """Consume the dedupe slot for ``key`` (False = we just said this)."""
    now = time.monotonic()
    with _LOCK:
        for k, ts in list(_SEEN.items()):
            if now - ts > _DEDUPE_SECONDS:
                del _SEEN[k]
        if key in _SEEN:
            return False
        _SEEN[key] = now
        return True


# --------------------------------------------------------------------------- #
# The cached URL every push taps through to
# --------------------------------------------------------------------------- #
#: How long a probed URL is trusted before a refresh is started. A tailnet name
#: changes about never; the point of re-probing at all is noticing that
#: Tailscale came up (or went away) since the last push.
_CACHE_TTL = 300.0

_CACHE_LOCK = threading.Lock()
_CACHED_URL: Optional[str] = None
_CACHED_AT = 0.0  # monotonic; 0.0 = never probed
_REFRESHING = False


def remember_url(url: Optional[str]) -> None:
    """Record a freshly probed phone URL (or its absence) for :func:`click_for`.

    Public because probing is expensive and a couple of callers do it for their
    own reasons — the startup announce, the "Send a test" button — and their
    answer is exactly as good as one this module paid for itself.
    """
    global _CACHED_URL, _CACHED_AT
    with _CACHE_LOCK:
        _CACHED_URL = url
        _CACHED_AT = time.monotonic()


def _refresh_cache_soon() -> None:
    """Probe for the phone URL on a thread of our own, at most one at a time.

    Never on the caller's thread: the only caller is :func:`click_for`, which
    runs inside an event-bus emit — the same critical path ``publish_soon``
    exists to stay off. ``tailscale status`` can take seconds.
    """
    global _REFRESHING
    with _CACHE_LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True

    def _run() -> None:
        global _REFRESHING
        try:
            url, _live = mobile_access.tailnet_url()
            remember_url(url)
        except Exception:  # noqa: BLE001 — a stale cache beats a crashed thread
            pass
        finally:
            with _CACHE_LOCK:
                _REFRESHING = False

    threading.Thread(target=_run, daemon=True).start()


def click_for(session: str = "") -> str:
    """The tap-target for a push: this machine's phone URL, deep-linked to
    ``session`` — or ``""`` when we don't know it.

    Every notification is about something you'd want to *look* at, and a phone
    notification that leaves you hunting for the URL wastes the trip. So each
    push carries this as its ``click``: tapping "alpha needs your input" opens
    the mobile view already showing alpha (``/m?s=…`` — the same query param
    mobile.js honours when picking the session).

    Reads the cache only — never probes on the caller's thread, which is
    whatever thread emitted the event. A stale (or missing) answer starts a
    background refresh and returns what it has, so at worst the *first* push
    after Tailscale comes up has no link and the next one does.
    """
    with _CACHE_LOCK:
        url, at = _CACHED_URL, _CACHED_AT
    if at == 0.0 or time.monotonic() - at > _CACHE_TTL:
        _refresh_cache_soon()
    if not url:
        return ""
    if not session:
        return url
    sep = "&" if "?" in url else "?"
    return "%s%ss=%s" % (url, sep, urllib.parse.quote(session, safe=""))


def _auth_on() -> bool:
    """Whether the phone will meet a sign-in page (advisory, never raises)."""
    try:
        return bool(_auth.auth_enabled())
    except Exception:  # noqa: BLE001
        return False


async def announce(reason: str) -> bool:
    """Push the tailnet phone URL, if there is one and anyone to push it to.

    Returns whether a push was actually sent — False is the ordinary case (no
    Tailscale, no ntfy, or we just said this), not an error. Never raises: this
    runs inside a startup hook and two settings saves, none of which should fail
    because a notification didn't go out.
    """
    try:
        cfg = ntfy.load()
        if not cfg.active:
            return False
        # Both probes shell out to `tailscale` with a timeout — off the loop.
        url, live = await asyncio.to_thread(mobile_access.tailnet_url)
        # We probed anyway; every later push taps through to this (click_for).
        remember_url(url)
        if not url:
            return False
        if not _should_send((url, live)):
            return False
        lines = [_LINES.get(reason, _LINES[REASON_STARTUP]), url]
        if not live:
            lines.append("(restart the server to apply tailscale mode)")
        if _auth_on():
            lines.append("Sign in with the access token from Settings → Security.")
        ok, _err = await ntfy.publish(
            cfg,
            title="MindFlock on your phone",
            message="\n".join(lines),
            priority=3,
            tags=["iphone"],
            # Tap the notification -> the mobile view. Overrides the user's
            # configured click URL for this one push: this notification IS the
            # URL, so opening anything else would be a bug.
            click=url,
        )
        return ok
    except asyncio.CancelledError:  # shutdown — not a delivery failure
        raise
    except Exception as err:  # noqa: BLE001 — never break the caller
        ntfy.log_error("mobile URL announce failed: %s", err)
        return False


def announce_soon(reason: str) -> None:
    """Fire-and-forget :func:`announce` from a sync caller.

    The settings routes are plain ``def`` handlers on a worker thread, so they
    can neither await this nor afford to wait for it (the Tailscale probe alone
    can take seconds). Same trampoline the ntfy channel uses for pushes.
    """
    ntfy.dispatch(announce(reason))
