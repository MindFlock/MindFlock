"""The "here's your phone URL" push: which URL, when, and what it must not say.

Two halves have to line up for this feature to exist at all — Tailscale gives
the URL, ntfy gives somewhere to send it — so most of these tests are about
staying *silent* when one half is missing. The rest pin the two things that
would be bugs with consequences rather than annoyances: the access token must
never ride along to a third-party server, and a URL that isn't live yet must
say so instead of pretending.

Covers :func:`mobile_access.tailnet_url`, :mod:`backend.web.core.mobile_announce`
and the three moments that fire it (server startup, the ntfy channel coming on,
Settings → Mobile's tailscale toggle).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.addons import notify as notify_addon
from backend.web.core import mobile_access, mobile_announce, ntfy

client = TestClient(server.app)

#: The real fire-and-forget entry point, captured before tests/conftest.py's
#: suite-wide stub replaces it (this module is the one that tests that path).
_REAL_ANNOUNCE_SOON = mobile_announce.announce_soon
_REAL_REFRESH = mobile_announce._refresh_cache_soon


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


@pytest.fixture
def real_announce_soon(monkeypatch):
    """Undo conftest's stub: these tests are *about* the background push."""
    monkeypatch.setattr(mobile_announce, "announce_soon", _REAL_ANNOUNCE_SOON)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clear the dedupe memory and the ntfy env overrides between tests."""
    mobile_announce._SEEN.clear()
    mobile_announce.remember_url(None)
    monkeypatch.setattr(mobile_announce, "_CACHED_AT", 0.0)
    for name in (
        "MINDFLOCK_NTFY_ENABLED",
        "MINDFLOCK_NTFY_SERVER",
        "MINDFLOCK_NTFY_TOPIC",
        "MINDFLOCK_NTFY_TOKEN",
        "MINDFLOCK_NTFY_CLICK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    mobile_announce._SEEN.clear()


@pytest.fixture
def tailnet(monkeypatch):
    """A tailnet that exists: MagicDNS name + IP, no `tailscale serve`, and a
    non-local bind — i.e. the plain "phone can reach this" case."""
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    # A non-local bind auto-arms the auth gate, which would 401 the API calls
    # below. The gate is not what these tests are about (test_auth.py owns it),
    # and the one test that cares patches auth_enabled directly.
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setenv("UVICORN_PORT", "8765")
    monkeypatch.setattr(
        server, "_tailscale_info", lambda: ("box.tail.net", "100.1.2.3")
    )
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)


@pytest.fixture
def no_tailnet(monkeypatch):
    monkeypatch.setattr(server, "_tailscale_info", lambda: (None, None))
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)


@pytest.fixture
def pushes(monkeypatch):
    """Capture what would be published, without a transport."""
    sent: list = []

    async def _publish(cfg, **kw):
        sent.append({"cfg": cfg, **kw})
        return True, ""

    monkeypatch.setattr(ntfy, "publish", _publish)
    return sent


def _ntfy_on(topic="t1"):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": topic})


def _wait(pred, timeout: float = 5.0) -> bool:
    """Poll until true — announce_soon lands on a thread or a loop of its own."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# --------------------------------------------------------------------------- #
# tailnet_url: which URL a phone gets
# --------------------------------------------------------------------------- #
def test_tailnet_url_prefers_the_serve_proxy(monkeypatch):
    """`tailscale serve` fronts localhost with HTTPS, so that URL works even
    while uvicorn is bound to 127.0.0.1 — local mode is not a caveat here."""
    monkeypatch.setenv("CS_WEB_MODE", "local")
    monkeypatch.setattr(
        server, "_tailscale_info", lambda: ("box.tail.net", "100.1.2.3")
    )
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: True)
    assert mobile_access.tailnet_url() == ("https://box.tail.net/m", True)


def test_tailnet_url_uses_the_magicdns_name_and_port(tailnet):
    assert mobile_access.tailnet_url() == ("http://box.tail.net:8765/m", True)


def test_tailnet_url_falls_back_to_the_ip(monkeypatch, tailnet):
    monkeypatch.setattr(server, "_tailscale_info", lambda: (None, "100.1.2.3"))
    assert mobile_access.tailnet_url() == ("http://100.1.2.3:8765/m", True)


def test_tailnet_url_is_not_live_while_bound_locally(monkeypatch, tailnet):
    """The address is right, but nothing answers on it until the server is
    restarted in tailscale mode — the caller has to be able to tell."""
    monkeypatch.setenv("CS_WEB_MODE", "local")
    assert mobile_access.tailnet_url() == ("http://box.tail.net:8765/m", False)


def test_tailnet_url_is_none_without_tailscale(no_tailnet):
    assert mobile_access.tailnet_url() == (None, False)


# --------------------------------------------------------------------------- #
# announce: when it stays quiet
# --------------------------------------------------------------------------- #
async def test_silent_when_ntfy_is_off(tailnet, pushes):
    assert await mobile_announce.announce("startup") is False
    assert pushes == []


async def test_silent_when_ntfy_has_no_topic(tailnet, pushes):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": ""})
    assert await mobile_announce.announce("startup") is False
    assert pushes == []


async def test_silent_without_tailscale(no_tailnet, pushes):
    """No tailnet, no URL — and a made-up one would be worse than silence."""
    _ntfy_on()
    assert await mobile_announce.announce("startup") is False
    assert pushes == []


async def test_never_raises_when_the_push_blows_up(monkeypatch, tailnet):
    _ntfy_on()

    async def _boom(cfg, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(ntfy, "publish", _boom)
    assert await mobile_announce.announce("startup") is False  # no exception


# --------------------------------------------------------------------------- #
# announce: what it says
# --------------------------------------------------------------------------- #
async def test_push_carries_the_url_and_opens_it_on_tap(tailnet, pushes):
    _ntfy_on()
    assert await mobile_announce.announce(mobile_announce.REASON_STARTUP) is True
    (push,) = pushes
    assert "http://box.tail.net:8765/m" in push["message"]
    assert push["click"] == "http://box.tail.net:8765/m"
    assert push["cfg"].topic == "t1"
    assert "just started" in push["message"]


async def test_each_reason_says_why_the_phone_is_buzzing(tailnet, pushes):
    _ntfy_on()
    for reason in (
        mobile_announce.REASON_STARTUP,
        mobile_announce.REASON_NTFY,
        mobile_announce.REASON_MOBILE,
    ):
        mobile_announce._SEEN.clear()
        assert await mobile_announce.announce(reason) is True
    assert [p["message"].split("\n")[0] for p in pushes] == [
        mobile_announce._LINES[mobile_announce.REASON_STARTUP],
        mobile_announce._LINES[mobile_announce.REASON_NTFY],
        mobile_announce._LINES[mobile_announce.REASON_MOBILE],
    ]


async def test_the_access_token_never_travels_to_ntfy(monkeypatch, tailnet, pushes):
    """The push is stored on a third-party server. The QR may bake ?token= in;
    this must not — same call ntfy.strip_token_param makes for the click URL."""
    monkeypatch.setattr(server._auth, "auth_enabled", lambda: True)
    monkeypatch.setattr(server._auth, "get_token", lambda: "s3cret-token")
    _ntfy_on()
    assert await mobile_announce.announce("startup") is True
    (push,) = pushes
    blob = "%s %s %s" % (push["title"], push["message"], push.get("click"))
    assert "s3cret-token" not in blob
    assert "token=" not in blob
    # It does say a token will be wanted — a sign-in page with no warning is
    # how "the URL doesn't work" reports get written.
    assert "access token" in push["message"].lower()


async def test_a_url_that_is_not_live_yet_says_so(monkeypatch, tailnet, pushes):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    _ntfy_on()
    assert await mobile_announce.announce(mobile_announce.REASON_MOBILE) is True
    (push,) = pushes
    assert "restart" in push["message"].lower()


async def test_a_live_url_carries_no_restart_caveat(tailnet, pushes):
    _ntfy_on()
    await mobile_announce.announce(mobile_announce.REASON_MOBILE)
    assert "restart" not in pushes[0]["message"].lower()


# --------------------------------------------------------------------------- #
# announce: saying it once
# --------------------------------------------------------------------------- #
async def test_the_same_url_is_not_announced_twice(tailnet, pushes):
    """Turning on ntfy and tailscale mode back to back is one intent."""
    _ntfy_on()
    assert await mobile_announce.announce(mobile_announce.REASON_NTFY) is True
    assert await mobile_announce.announce(mobile_announce.REASON_MOBILE) is False
    assert len(pushes) == 1


async def test_a_url_going_live_is_worth_saying_again(monkeypatch, tailnet, pushes):
    """The "restart to apply" push and the "it works now" push are different
    news — the second is the one the user is actually waiting for."""
    monkeypatch.setenv("CS_WEB_MODE", "local")
    _ntfy_on()
    assert await mobile_announce.announce(mobile_announce.REASON_MOBILE) is True
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    assert await mobile_announce.announce(mobile_announce.REASON_STARTUP) is True
    assert len(pushes) == 2


async def test_the_dedupe_window_expires(monkeypatch, tailnet, pushes):
    _ntfy_on()
    assert await mobile_announce.announce("startup") is True
    for key in list(mobile_announce._SEEN):  # age every entry past the window
        mobile_announce._SEEN[key] -= mobile_announce._DEDUPE_SECONDS + 1
    assert await mobile_announce.announce("startup") is True
    assert len(pushes) == 2


# --------------------------------------------------------------------------- #
# The three moments that fire it
# --------------------------------------------------------------------------- #
def test_turning_ntfy_on_announces_the_url(tailnet, pushes, real_announce_soon):
    S.update_settings(notifications={"ntfy_topic": "t1"})
    r = client.post("/api/notify/ntfy", json={"enabled": True, "topic": "t1"})
    assert r.status_code == 200
    assert _wait(lambda: len(pushes) == 1)
    assert "box.tail.net" in pushes[0]["message"]


def test_saving_an_already_on_channel_does_not_re_announce(
    tailnet, pushes, real_announce_soon
):
    _ntfy_on()
    client.post("/api/notify/ntfy", json={"enabled": True, "topic": "t1"})
    time.sleep(0.2)
    assert pushes == []


def test_turning_tailscale_mode_on_announces_the_url(
    tailnet, pushes, real_announce_soon
):
    """No restart is scheduled here (the server is already on the tailnet), so
    the announce is this process's job."""
    _ntfy_on()
    r = client.post("/api/settings", json={"general": {"serve_mode": "tailscale"}})
    assert r.status_code == 200
    assert r.json().get("restarting") is None
    assert _wait(lambda: len(pushes) == 1)


def test_an_unrelated_settings_save_announces_nothing(
    tailnet, pushes, real_announce_soon
):
    _ntfy_on()
    client.post("/api/settings", json={"ui": {"accent": "macaw"}})
    time.sleep(0.2)
    assert pushes == []


def test_server_startup_announces_the_url(tailnet, monkeypatch):
    """The lifespan warmup fires it, so a phone learns the URL from a restart
    it wasn't present for."""
    seen: list = []

    async def _announce(reason):
        seen.append(reason)
        return True

    monkeypatch.setattr(mobile_announce, "announce", _announce)
    with TestClient(server.app):
        assert _wait(lambda: mobile_announce.REASON_STARTUP in seen)


def test_announce_soon_delivers_without_a_running_loop(
    tailnet, pushes, real_announce_soon
):
    """The settings routes are sync handlers on a worker thread — the push has
    to find its own way onto a loop."""
    _ntfy_on()
    mobile_announce.announce_soon(mobile_announce.REASON_NTFY)
    assert _wait(lambda: len(pushes) == 1)


def test_announce_soon_never_raises_at_the_caller(
    monkeypatch, tailnet, real_announce_soon
):
    _ntfy_on()
    monkeypatch.setattr(
        ntfy.threading,
        "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("cannot spawn")),
    )
    mobile_announce.announce_soon("startup")  # no exception


# --------------------------------------------------------------------------- #
# click_for: the URL that rides along on every other push
# --------------------------------------------------------------------------- #
def test_click_for_deep_links_to_the_session():
    mobile_announce.remember_url("http://box.tail.net:8765/m")
    assert mobile_announce.click_for("alpha") == "http://box.tail.net:8765/m?s=alpha"


def test_click_for_escapes_the_session_title():
    """Session titles are free text; a raw one would break the query string."""
    mobile_announce.remember_url("http://box.tail.net:8765/m")
    assert mobile_announce.click_for("fix a&b") == (
        "http://box.tail.net:8765/m?s=fix%20a%26b"
    )


def test_click_for_without_a_session_is_the_bare_url():
    mobile_announce.remember_url("https://box.tail.net/m")
    assert mobile_announce.click_for() == "https://box.tail.net/m"


def test_click_for_is_empty_when_there_is_no_tailnet():
    """No URL is not an error — publish() falls back to the user's own click
    URL, and the push still goes out."""
    mobile_announce.remember_url(None)
    assert mobile_announce.click_for("alpha") == ""


def test_click_for_never_probes_on_the_callers_thread(monkeypatch):
    """It runs inside an event-bus emit; `tailscale status` can take seconds."""
    monkeypatch.setattr(
        mobile_access,
        "tailnet_url",
        lambda: pytest.fail("click_for probed on the calling thread"),
    )
    monkeypatch.setattr(mobile_announce, "_refresh_cache_soon", lambda: None)
    assert mobile_announce.click_for("alpha") == ""


def test_a_stale_cache_refreshes_in_the_background(monkeypatch, tailnet):
    """The first push after Tailscale comes up may have no link; the next one
    does."""
    monkeypatch.setattr(mobile_announce, "_refresh_cache_soon", _REAL_REFRESH)
    assert mobile_announce.click_for("alpha") == ""  # nothing cached yet
    assert _wait(lambda: mobile_announce.click_for("alpha").startswith("http"))
    assert mobile_announce.click_for("alpha") == ("http://box.tail.net:8765/m?s=alpha")


def test_session_pushes_carry_the_url(monkeypatch, tailnet):
    """Every rule push taps through to the session it is about — both as the
    click action and as a line in the message."""
    sent: list = []
    monkeypatch.setattr(ntfy, "publish_soon", lambda cfg, **kw: sent.append(kw))
    mobile_announce.remember_url("http://box.tail.net:8765/m")
    _ntfy_on()
    notify_addon.NotifyAddon()._on_event(
        {
            "event": "session.activity_changed",
            "session": "alpha",
            "old": "working",
            "new": "clarify",
        }
    )
    (push,) = sent
    assert push["click"] == "http://box.tail.net:8765/m?s=alpha"
    assert push["message"].endswith("http://box.tail.net:8765/m?s=alpha")
    # The rule's own text is still the first thing you read.
    assert push["message"].startswith("The agent is waiting on a clarification.")


def test_session_pushes_without_a_tailnet_are_unchanged(monkeypatch):
    """No URL to add, so nothing is added — and publish() still falls back to
    the user's configured click URL."""
    sent: list = []
    monkeypatch.setattr(ntfy, "publish_soon", lambda cfg, **kw: sent.append(kw))
    mobile_announce.remember_url(None)
    _ntfy_on()
    notify_addon.NotifyAddon()._on_event(
        {
            "event": "session.activity_changed",
            "session": "alpha",
            "old": "working",
            "new": "clarify",
        }
    )
    (push,) = sent
    assert push["click"] is None
    assert push["message"] == "The agent is waiting on a clarification."


def test_the_test_push_shows_the_url_too(tailnet, pushes):
    """The first push anyone sees demonstrates what the real ones will do."""
    _ntfy_on()
    r = client.post("/api/notify/ntfy/test", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    (push,) = pushes
    assert push["click"] == "http://box.tail.net:8765/m"
    assert "http://box.tail.net:8765/m" in push["message"]


async def test_publish_soon_forwards_a_click_url(monkeypatch):
    """The click passthrough mobile_announce relies on, checked at the seam."""
    seen: list = []

    async def _publish(cfg, **kw):
        seen.append(kw)
        return True, ""

    monkeypatch.setattr(ntfy, "publish", _publish)
    ntfy.set_loop(asyncio.get_running_loop())
    ntfy.publish_soon(
        ntfy.NtfyConfig(enabled=True, topic="t1"),
        title="T",
        message="M",
        click="https://box.tail.net/m",
    )
    for _ in range(200):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen and seen[0]["click"] == "https://box.tail.net/m"
    ntfy.set_loop(None)
