"""Optional ntfy push channel for session notifications.

The browser channel (``static/addons/notify.js``) can only fire while a tab is
open on a secure origin. This module is its *server-side* counterpart: the
running server POSTs matching events to an `ntfy <https://ntfy.sh>`_ topic, so
"session X needs your input" reaches a phone with nothing open at all — which is
the point of a flock you check on rather than watch.

How the pieces divide up:

* **Config** lives in ``settings.notifications`` (the ``ntfy_*`` fields),
  resolved through the usual env → settings.json → default chain, so a headless
  box can export ``MINDFLOCK_NTFY_TOPIC`` and be done.
* **Which** events push is NOT decided here — the notify addon owns the rules
  (``NOTIFY_RULES``) and both channels share them, so "what notifies me" stays
  one list in one place (Settings → Notifications).
* **Transport** is ntfy's JSON publish API (POST to the server root with the
  topic in the body) rather than the ``POST /<topic>`` + ``X-Title`` header
  form: session titles are arbitrary UTF-8 and HTTP headers are not.

Everything here is best-effort — a failed push is recorded in
:func:`last_result` (for Settings → Notifications to show) and logged, but never
raises into the request path that emitted the event.

Privacy notes, deliberate:

* On a public server the **topic name is the credential** — anyone who knows it
  can read every push and publish fakes. Hence :func:`random_topic` and the
  UI's Generate button; hence also nothing here ever logs the topic (the log is
  served back out over ``GET /api/logs``).
* A push carries the session title and the rule's text to a third-party server.
  That is inherent to the feature, so the UI says so out loud rather than
  burying it.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

from backend import log
from backend.config import settings as settings_store

#: Where pushes go when the user names no server: the public instance.
DEFAULT_SERVER = "https://ntfy.sh"

#: ntfy's topic charset. Anything outside it 404s (or lands on the wrong topic),
#: so it is rejected at the edge instead of failing silently at publish time.
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Total seconds one publish may take. Short: an event-bus subscriber is waiting
#: on nothing, but a wedged server must not keep a task alive for minutes.
_TIMEOUT = 10.0

#: Runaway guard. A pathological session flip-flopping between states could
#: otherwise burn the user's ntfy quota (and their patience) — cap the whole
#: process at this many pushes per rolling window and log once when we start
#: dropping. Well above any sane real rate: the default-on rules are all
#: low-frequency, one-per-transition events.
_RATE_LIMIT = 60
_RATE_WINDOW = 3600.0

_LOCK = threading.Lock()
_SENT: Deque[float] = deque()  # monotonic ts of recent pushes (rate window)
# Monotonic ts of the last "dropping pushes" log line; None = never logged. A
# sentinel rather than 0.0 because monotonic()'s zero point is arbitrary, so
# "now - 0 > window" is not a reliable "long ago".
_THROTTLE_LOGGED: Optional[float] = None
_LAST: dict = {}  # {"ts": epoch, "ok": bool, "error": str} of the last attempt

#: The server's event loop, so a push can be fired from a bus callback running
#: on any worker thread (same trick as ``EventBus.set_loop``).
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Register the loop :func:`publish_soon` schedules onto (the notify addon
    calls this from its startup hook, which runs on the server loop)."""
    global _LOOP
    _LOOP = loop


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NtfyConfig:
    """The resolved ntfy settings. Build with :func:`load` (or by hand in tests
    and in the "test before you save" path, which configures from the request
    body rather than the store)."""

    enabled: bool = False
    server: str = DEFAULT_SERVER
    topic: str = ""
    token: str = ""
    click_url: str = ""

    @property
    def configured(self) -> bool:
        """A topic is the one thing with no sensible default."""
        return bool(self.topic)

    @property
    def active(self) -> bool:
        """Turned on AND pointed somewhere — the check before every push."""
        return bool(self.enabled and self.topic)

    @property
    def host(self) -> str:
        """Host[:port] of the server, for log lines that must not leak the
        topic."""
        return urllib.parse.urlsplit(self.server).netloc or self.server

    @property
    def is_public_server(self) -> bool:
        """True on the shared public instance, where every topic is world-
        readable to anyone who guesses it (the UI warns on this)."""
        return self.host.lower() in ("ntfy.sh", "www.ntfy.sh")

    @property
    def subscribe_url(self) -> str:
        """The URL/deep link to hand a phone (what the settings QR encodes)."""
        return "%s/%s" % (self.server.rstrip("/"), self.topic) if self.topic else ""


def load() -> NtfyConfig:
    """The ntfy config in effect: env → settings.json → defaults.

    The env overrides (``MINDFLOCK_NTFY_*``) exist for headless/provisioned
    boxes that configure by environment and never open Settings.
    """
    try:
        enabled = settings_store.resolve_bool(
            env="MINDFLOCK_NTFY_ENABLED",
            settings_getter=lambda s: s.notifications.ntfy_enabled or None,
            default=False,
        )
        server = settings_store.resolve_str(
            env="MINDFLOCK_NTFY_SERVER",
            settings_getter=lambda s: s.notifications.ntfy_server,
            default=DEFAULT_SERVER,
        )
        topic = settings_store.resolve_str(
            env="MINDFLOCK_NTFY_TOPIC",
            settings_getter=lambda s: s.notifications.ntfy_topic,
            default="",
        )
        token = settings_store.resolve_str(
            env="MINDFLOCK_NTFY_TOKEN",
            settings_getter=lambda s: s.notifications.ntfy_token,
            default="",
        )
        click = settings_store.resolve_str(
            env="MINDFLOCK_NTFY_CLICK_URL",
            settings_getter=lambda s: s.notifications.ntfy_click_url,
            default="",
        )
    except Exception:  # noqa: BLE001 — an unreadable store just means "off"
        return NtfyConfig()
    # An env-supplied topic is an implicit opt-in: nobody exports
    # MINDFLOCK_NTFY_TOPIC on a headless box and then wants silence, and there
    # is no Settings screen there to flip the switch in. But an EXPLICIT
    # MINDFLOCK_NTFY_ENABLED still wins — "=0" has to mean off, or the only way
    # to silence a configured box would be to unset the topic.
    if (
        topic
        and not enabled
        and os.environ.get("MINDFLOCK_NTFY_TOPIC")
        and not os.environ.get("MINDFLOCK_NTFY_ENABLED")
    ):
        enabled = True
    return NtfyConfig(
        enabled=bool(enabled),
        server=normalize_server(server),
        topic=topic.strip(),
        token=token.strip(),
        click_url=click.strip(),
    )


# --------------------------------------------------------------------------- #
# Validation / helpers for the settings edge
# --------------------------------------------------------------------------- #
def normalize_server(raw: str) -> str:
    """A user-typed server into a canonical base URL.

    ``""`` -> the public default; a bare host gets ``https://`` (typing
    "ntfy.example.com" should not silently become a relative URL); the trailing
    slash goes so ``server + "/" + topic`` never doubles up.
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        return DEFAULT_SERVER
    if "://" not in s:
        s = "https://" + s
    return s


def validate(server: str, topic: str) -> Optional[str]:
    """``None`` when the pair is publishable, else a user-facing reason."""
    parts = urllib.parse.urlsplit(normalize_server(server))
    if parts.scheme not in ("http", "https"):
        return "The ntfy server URL must start with http:// or https://"
    if not parts.netloc:
        return "That ntfy server URL has no host"
    if not topic:
        return "Pick a topic — it is the address your phone subscribes to"
    if not _TOPIC_RE.match(topic):
        return (
            "A topic can only use letters, numbers, dashes and underscores "
            "(up to 64 characters)"
        )
    return None


def random_topic() -> str:
    """An unguessable topic name.

    On the public server the topic is the only thing standing between your
    session titles and anyone who tries a word — so the default we offer is
    random, not "mindflock". ``token_urlsafe`` emits ``[A-Za-z0-9_-]``, exactly
    ntfy's charset.
    """
    return "mindflock-" + secrets.token_urlsafe(18)


def strip_token_param(url: str) -> Tuple[str, bool]:
    """``(url without any ``token=`` query param, whether one was removed)``.

    A tapped-notification URL is stored on the ntfy server and travels with
    every push. Users naturally paste the URL from Settings → Mobile, which
    carries ``?token=<access token>`` — that would hand this machine's
    credential to a third party, so it comes out here rather than in a warning
    the user may not read.
    """
    raw = (url or "").strip()
    if not raw:
        return "", False
    parts = urllib.parse.urlsplit(raw)
    if not parts.query:
        return raw, False
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "token"
    ]
    if len(kept) == len(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)):
        return raw, False
    return (
        urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(kept))),
        True,
    )


def same_host(a: str, b: str) -> bool:
    """Whether two server URLs point at the same host[:port].

    Used to decide whether a saved token still belongs to the server being
    saved: retargeting the channel at a different host must not ship the old
    server's token to the new one.
    """
    return (
        urllib.parse.urlsplit(normalize_server(a)).netloc.lower()
        == urllib.parse.urlsplit(normalize_server(b)).netloc.lower()
    )


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
def _allow_one() -> bool:
    """Consume one slot from the rolling rate budget (False = drop this push)."""
    global _THROTTLE_LOGGED
    now = time.monotonic()
    with _LOCK:
        while _SENT and now - _SENT[0] > _RATE_WINDOW:
            _SENT.popleft()
        if len(_SENT) < _RATE_LIMIT:
            _SENT.append(now)
            return True
        # Over budget: log the first drop of each window, then stay quiet
        # (a throttle that spams the log is its own problem).
        should_log = _THROTTLE_LOGGED is None or now - _THROTTLE_LOGGED > _RATE_WINDOW
        if should_log:
            _THROTTLE_LOGGED = now
    if should_log and log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(
                "ntfy: over %d pushes/hour — dropping further pushes this window",
                _RATE_LIMIT,
            )
        except Exception:  # noqa: BLE001
            pass
    return False


def _record(ok: bool, error: str = "") -> None:
    """Remember the outcome of the last attempt for Settings → Notifications."""
    with _LOCK:
        _LAST.clear()
        _LAST.update({"ts": time.time(), "ok": bool(ok), "error": error})


def last_result() -> dict:
    """``{}`` before the first attempt, else ``{ts, ok, error}`` of the last one.

    A push channel is invisible when it works and baffling when it doesn't;
    this is what the settings screen shows so a wrong token/topic is diagnosable
    without reading the log.
    """
    with _LOCK:
        return dict(_LAST)


async def publish(
    cfg: NtfyConfig,
    *,
    title: str,
    message: str,
    priority: Optional[int] = None,
    tags: Optional[List[str]] = None,
    click: Optional[str] = None,
    budgeted: bool = True,
) -> Tuple[bool, str]:
    """POST one notification. Returns ``(ok, error)`` and never raises.

    ``budgeted=False`` skips the rate cap — for the user-initiated "Send a test"
    button, which cannot run away and whose whole job is to report a verdict.
    """
    if not cfg.topic:
        return False, "No ntfy topic configured"
    if budgeted and not _allow_one():
        return False, "Rate limit: too many ntfy pushes this hour"
    body: dict = {"topic": cfg.topic, "message": message or ""}
    if title:
        body["title"] = title
    if priority:
        body["priority"] = int(priority)
    if tags:
        body["tags"] = list(tags)
    target = (click or cfg.click_url or "").strip()
    if target:
        body["click"] = target
    headers = {"Content-Type": "application/json"}
    if cfg.token:
        headers["Authorization"] = "Bearer " + cfg.token
    try:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(cfg.server, json=body, headers=headers) as resp:
                if resp.status >= 400:
                    detail = await _error_detail(resp)
                    err = "ntfy server returned %d%s" % (
                        resp.status,
                        (": " + detail) if detail else "",
                    )
                    _record(False, err)
                    log_error("ntfy publish to %s failed: %s", cfg.host, err)
                    return False, err
        _record(True)
        return True, ""
    except asyncio.CancelledError:  # shutdown — not a delivery failure
        raise
    except Exception as err:  # noqa: BLE001 — DNS, TLS, timeout, aiohttp missing
        msg = "%s: %s" % (type(err).__name__, err) if str(err) else type(err).__name__
        _record(False, msg)
        log_error("ntfy publish to %s failed: %s", cfg.host, msg)
        return False, msg


async def _error_detail(resp) -> str:
    """The ``error`` field of ntfy's JSON error body, best-effort and bounded.

    ntfy answers a bad token/topic with ``{"code":…,"error":"…"}``; surfacing
    that sentence is the difference between "returned 403" and "topic is
    reserved". A non-JSON body (a proxy's HTML error page) is truncated.
    """
    try:
        data = await resp.json(content_type=None)
        if isinstance(data, dict):
            detail = str(data.get("error") or "").strip()
            if detail:
                return detail[:200]
    except Exception:  # noqa: BLE001
        pass
    try:
        return (await resp.text())[:200].strip()
    except Exception:  # noqa: BLE001
        return ""


def publish_soon(
    cfg: NtfyConfig,
    *,
    title: str,
    message: str,
    priority: Optional[int] = None,
    tags: Optional[List[str]] = None,
) -> None:
    """Fire-and-forget :func:`publish` from any thread.

    Event-bus subscribers run synchronously on whichever thread emitted, so this
    must never block: it hands the coroutine to the server loop (or, with no
    loop at all — a bare sync context such as a unit test — a throwaway thread
    with its own), exactly like ``EventBus._dispatch_hooks``.
    """
    coro = publish(cfg, title=title, message=message, priority=priority, tags=tags)
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        target = running or _LOOP
        if target is not None and not target.is_closed():
            if running is target:
                target.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, target)
            return
        threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()
    except Exception as err:  # noqa: BLE001 — a push is never worth a 500
        coro.close()
        log_error("ntfy dispatch failed: %s", err)


def log_error(fmt: str, *args) -> None:
    """Log a line. Callers pass the server *host*, never the topic —
    mindflock.log is served back out over ``GET /api/logs``, and on a public
    server the topic is the credential."""
    if log.ErrorLog is None:
        return
    try:
        log.ErrorLog.Printf(fmt, *args)
    except Exception:  # noqa: BLE001
        pass
