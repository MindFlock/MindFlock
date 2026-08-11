"""The ntfy push channel: settings, the transport, and rule dispatch.

Covers the settings group, the /api/notify/ntfy read/write/test endpoints (with
their secret handling), the JSON body actually sent to an ntfy server, the rate
cap, the cross-thread dispatch trampoline, and the server-side dispatch that
turns a bus event into a push — including the cases where it must stay silent
(channel off, rule muted, duplicate) and the ones where it must stay quiet about
the topic and the token.

The QR renderer shared with Settings → Mobile is here too: the ntfy view is why
``mobile_access._mobile_svg`` became the public ``qr_svg``, and both callers have
to keep working.
"""

import asyncio
import sys
import threading
import time
import types

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.addons import notify as notify_addon
from backend.web.addons.base import AppContext
from backend.web.core import events, mobile_access, ntfy

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Drop the transport's process-wide state (rate window, last result, the
    registered loop) and any ntfy env overrides, so tests don't leak into each
    other — a loop left behind by one test is a closed loop by the next."""
    ntfy._SENT.clear()
    ntfy._LAST.clear()
    monkeypatch.setattr(ntfy, "_THROTTLE_LOGGED", None, raising=False)
    monkeypatch.setattr(ntfy, "_LOOP", None, raising=False)
    for name in (
        "MINDFLOCK_NTFY_ENABLED",
        "MINDFLOCK_NTFY_SERVER",
        "MINDFLOCK_NTFY_TOPIC",
        "MINDFLOCK_NTFY_TOKEN",
        "MINDFLOCK_NTFY_CLICK_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    ntfy._SENT.clear()
    ntfy._LAST.clear()


# --------------------------------------------------------------------------- #
# A fake aiohttp, so publish() is exercised without a network
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, calls, response, **kwargs):
        self._calls = calls
        self._response = response

    def post(self, url, json=None, headers=None):
        self._calls.append({"url": url, "json": json, "headers": headers or {}})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Calls(list):
    """The recorded POSTs, with a ``state`` dict a test can retarget so the fake
    server answers something other than 200."""

    state: dict


@pytest.fixture
def http(monkeypatch):
    """Install a fake ``aiohttp`` and return the recorded-calls list. Assign
    ``http.state["response"] = _FakeResponse(...)`` to change the answer."""
    calls = _Calls()
    calls.state = {"response": _FakeResponse(200)}

    mod = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    mod.ClientTimeout = ClientTimeout
    # Read the response through `state` at call time, so a test can swap it
    # after the fixture has been built.
    mod.ClientSession = lambda **kw: _FakeSession(calls, calls.state["response"], **kw)
    mod.ClientError = OSError
    monkeypatch.setitem(sys.modules, "aiohttp", mod)
    return calls


def _wait(pred, timeout: float = 5.0) -> bool:
    """Poll ``pred`` until true — for the publishes that run on a thread or loop
    of their own (also the join: the fake aiohttp must be asserted on while the
    monkeypatched sys.modules entry is still installed)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


async def _settle(pred, timeout: float = 5.0) -> bool:
    """:func:`_wait` for a coroutine scheduled on the loop we're running on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


def _ctx() -> AppContext:
    """A minimal AppContext — only ``subscribe`` is exercised by this addon."""
    return AppContext(engine=None, register_task=lambda coro: coro.close())


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #
def test_ntfy_settings_roundtrip():
    ns = S.NotificationSettings(
        ntfy_enabled=True,
        ntfy_server="https://push.example.com",
        ntfy_topic="mindflock-abc",
        ntfy_token="tk_secret",
        ntfy_click_url="http://host/m",
    )
    back = S.NotificationSettings.from_dict(ns.to_dict())
    assert back.ntfy_enabled is True
    assert back.ntfy_server == "https://push.example.com"
    assert back.ntfy_topic == "mindflock-abc"
    assert back.ntfy_token == "tk_secret"
    assert back.ntfy_click_url == "http://host/m"
    # Off/empty stays omitted, so an untouched store serializes as before.
    assert S.NotificationSettings().to_dict() == {}


# --------------------------------------------------------------------------- #
# Transport helpers
# --------------------------------------------------------------------------- #
def test_normalize_server_defaults_and_scheme():
    assert ntfy.normalize_server("") == ntfy.DEFAULT_SERVER
    assert ntfy.normalize_server("  ") == ntfy.DEFAULT_SERVER
    assert ntfy.normalize_server("https://ntfy.sh/") == "https://ntfy.sh"
    # A bare host becomes https, never a relative URL.
    assert ntfy.normalize_server("push.example.com") == "https://push.example.com"
    assert ntfy.normalize_server("http://box.local:8080") == "http://box.local:8080"


def test_validate_rejects_bad_topic_and_scheme():
    assert ntfy.validate("https://ntfy.sh", "mindflock-ok_1") is None
    assert "topic" in (ntfy.validate("https://ntfy.sh", "") or "")
    assert "letters" in (ntfy.validate("https://ntfy.sh", "has space") or "")
    assert "letters" in (ntfy.validate("https://ntfy.sh", "a/b") or "")
    assert "letters" in (ntfy.validate("https://ntfy.sh", "x" * 65) or "")
    assert "http" in (ntfy.validate("ftp://ntfy.sh", "topic") or "")


def test_random_topic_is_unguessable_and_valid():
    a, b = ntfy.random_topic(), ntfy.random_topic()
    assert a != b
    assert a.startswith("mindflock-") and len(a) > 24
    assert ntfy.validate(ntfy.DEFAULT_SERVER, a) is None


def test_strip_token_param_removes_access_token():
    url, stripped = ntfy.strip_token_param("http://host:8080/m?token=SECRET")
    assert stripped is True and "SECRET" not in url and "token" not in url
    # Other params survive; a clean URL is untouched.
    url, stripped = ntfy.strip_token_param("http://h/m?a=1&token=S&b=2")
    assert stripped is True and "a=1" in url and "b=2" in url and "token" not in url
    assert ntfy.strip_token_param("http://h/m?a=1") == ("http://h/m?a=1", False)
    assert ntfy.strip_token_param("") == ("", False)


def test_same_host_compares_host_not_path():
    assert ntfy.same_host("https://ntfy.sh", "https://ntfy.sh/") is True
    assert ntfy.same_host("ntfy.sh", "https://NTFY.sh") is True
    assert ntfy.same_host("https://ntfy.sh", "https://push.example.com") is False


def test_config_active_and_public_flags():
    off = ntfy.NtfyConfig(enabled=True)
    assert off.active is False  # enabled but no topic
    on = ntfy.NtfyConfig(enabled=True, topic="t")
    assert on.active is True and on.subscribe_url == "https://ntfy.sh/t"
    assert on.is_public_server is True
    mine = ntfy.NtfyConfig(enabled=True, topic="t", server="http://box.local:8080")
    assert mine.is_public_server is False and mine.host == "box.local:8080"


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #
def test_load_defaults_to_off():
    cfg = ntfy.load()
    assert cfg.enabled is False and cfg.topic == ""
    assert cfg.server == ntfy.DEFAULT_SERVER and cfg.active is False


def test_load_reads_store():
    S.update_settings(
        notifications={"ntfy_enabled": True, "ntfy_topic": "mindflock-store"}
    )
    cfg = ntfy.load()
    assert cfg.active is True and cfg.topic == "mindflock-store"


def test_env_topic_is_an_implicit_opt_in(monkeypatch):
    """A headless box exports a topic and expects pushes — there is no Settings
    screen there to flip the switch in."""
    monkeypatch.setenv("MINDFLOCK_NTFY_TOPIC", "mindflock-env")
    monkeypatch.setenv("MINDFLOCK_NTFY_SERVER", "http://box.local:8080")
    cfg = ntfy.load()
    assert cfg.topic == "mindflock-env" and cfg.enabled is True and cfg.active is True
    assert cfg.server == "http://box.local:8080"


def test_explicit_env_disable_beats_the_implicit_topic_opt_in(monkeypatch):
    """The implicit opt-in must not trap you: with the topic var still exported,
    MINDFLOCK_NTFY_ENABLED=0 is the way to go quiet."""
    monkeypatch.setenv("MINDFLOCK_NTFY_TOPIC", "mindflock-env")
    monkeypatch.setenv("MINDFLOCK_NTFY_ENABLED", "0")
    cfg = ntfy.load()
    assert cfg.topic == "mindflock-env" and cfg.enabled is False and cfg.active is False
    # And "1" is still on, of course.
    monkeypatch.setenv("MINDFLOCK_NTFY_ENABLED", "1")
    assert ntfy.load().active is True


def test_env_beats_store(monkeypatch):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "from-store"})
    monkeypatch.setenv("MINDFLOCK_NTFY_TOPIC", "from-env")
    assert ntfy.load().topic == "from-env"


# --------------------------------------------------------------------------- #
# publish(): the wire contract
# --------------------------------------------------------------------------- #
async def test_publish_posts_json_to_server_root(http):
    cfg = ntfy.NtfyConfig(
        enabled=True, topic="mindflock-x", token="tk_1", server="https://ntfy.sh"
    )
    ok, err = await ntfy.publish(
        cfg, title="Hi", message="Body", priority=4, tags=["question"]
    )
    assert (ok, err) == (True, "")
    (call,) = http
    # The topic goes in the BODY (the JSON publish API), not the path: a session
    # title is arbitrary UTF-8 and would not survive an X-Title header.
    assert call["url"] == "https://ntfy.sh"
    assert call["json"] == {
        "topic": "mindflock-x",
        "message": "Body",
        "title": "Hi",
        "priority": 4,
        "tags": ["question"],
    }
    assert call["headers"]["Authorization"] == "Bearer tk_1"
    assert ntfy.last_result()["ok"] is True


async def test_publish_sends_unicode_title(http):
    """The reason for the JSON body: non-ASCII titles must survive."""
    cfg = ntfy.NtfyConfig(enabled=True, topic="t")
    await ntfy.publish(cfg, title="refactor-ünïcode ✅", message="")
    assert http[0]["json"]["title"] == "refactor-ünïcode ✅"


async def test_publish_includes_click_url(http):
    cfg = ntfy.NtfyConfig(enabled=True, topic="t", click_url="http://box:8080/m")
    await ntfy.publish(cfg, title="T", message="M")
    assert http[0]["json"]["click"] == "http://box:8080/m"


async def test_publish_omits_empty_optional_fields(http):
    cfg = ntfy.NtfyConfig(enabled=True, topic="t")
    await ntfy.publish(cfg, title="", message="M")
    assert set(http[0]["json"]) == {"topic", "message"}
    assert "Authorization" not in http[0]["headers"]


async def test_publish_surfaces_server_error_sentence(http):
    http.state["response"] = _FakeResponse(
        403, payload={"code": 40301, "error": "topic is reserved"}
    )
    cfg = ntfy.NtfyConfig(enabled=True, topic="t")
    ok, err = await ntfy.publish(cfg, title="T", message="M")
    assert ok is False
    assert "403" in err and "topic is reserved" in err
    assert ntfy.last_result()["ok"] is False


async def test_publish_survives_a_dead_server(http, monkeypatch):
    def _boom(**kw):
        raise OSError("Name or service not known")

    monkeypatch.setattr(sys.modules["aiohttp"], "ClientSession", _boom)
    ok, err = await ntfy.publish(
        ntfy.NtfyConfig(enabled=True, topic="t"), title="T", message="M"
    )
    assert ok is False and "Name or service not known" in err


async def test_publish_without_topic_is_refused(http):
    ok, err = await ntfy.publish(ntfy.NtfyConfig(enabled=True), title="T", message="M")
    assert ok is False and "topic" in err.lower() and not http


async def test_publish_rate_cap_drops_the_flood(http, monkeypatch):
    monkeypatch.setattr(ntfy, "_RATE_LIMIT", 3)
    cfg = ntfy.NtfyConfig(enabled=True, topic="t")
    for _ in range(3):
        assert (await ntfy.publish(cfg, title="T", message="M"))[0] is True
    ok, err = await ntfy.publish(cfg, title="T", message="M")
    assert ok is False and "Rate limit" in err
    assert len(http) == 3
    # The hand-driven test push is exempt — it can't run away.
    ok, _ = await ntfy.publish(cfg, title="T", message="M", budgeted=False)
    assert ok is True and len(http) == 4


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def test_get_ntfy_defaults():
    v = client.get("/api/notify/ntfy").json()
    assert v["enabled"] is False and v["topic"] == "" and v["active"] is False
    assert v["server"] == ntfy.DEFAULT_SERVER == v["server_default"]
    assert v["has_token"] is False and v["public_server"] is True
    assert v["qr_svg"] is None  # nothing to encode yet
    # A random topic is always offered, so the client never invents one.
    assert ntfy.validate(ntfy.DEFAULT_SERVER, v["suggested_topic"]) is None


def test_save_and_enable_ntfy():
    v = client.post(
        "/api/notify/ntfy", json={"enabled": True, "topic": "mindflock-abc123"}
    ).json()
    assert v["enabled"] is True and v["topic"] == "mindflock-abc123"
    assert v["active"] is True
    assert v["subscribe_url"] == "https://ntfy.sh/mindflock-abc123"
    S.invalidate()
    saved = S.load_settings().notifications
    assert saved.ntfy_enabled is True and saved.ntfy_topic == "mindflock-abc123"


def test_save_rejects_a_bad_topic():
    r = client.post("/api/notify/ntfy", json={"enabled": True, "topic": "no spaces"})
    assert r.status_code == 400 and "letters" in r.json()["error"]
    S.invalidate()
    assert S.load_settings().notifications.ntfy_topic == ""  # nothing persisted


def test_enabling_without_a_topic_is_rejected():
    r = client.post("/api/notify/ntfy", json={"enabled": True})
    assert r.status_code == 400 and "topic" in r.json()["error"]


def test_clearing_the_topic_while_off_is_allowed():
    client.post("/api/notify/ntfy", json={"enabled": False, "topic": ""})
    assert client.get("/api/notify/ntfy").json()["topic"] == ""


def test_server_url_is_normalized_on_save():
    v = client.post(
        "/api/notify/ntfy", json={"topic": "t1", "server": "push.example.com/"}
    ).json()
    assert v["server"] == "https://push.example.com"
    assert v["public_server"] is False


def test_token_is_kept_on_empty_and_never_returned():
    client.post("/api/notify/ntfy", json={"topic": "t1", "token": "tk_secret"})
    v = client.get("/api/notify/ntfy").json()
    assert v["has_token"] is True
    assert "tk_secret" not in client.get("/api/notify/ntfy").text
    # An empty submit (and the mask sentinel) keep the saved token.
    client.post("/api/notify/ntfy", json={"topic": "t2", "token": ""})
    assert client.get("/api/notify/ntfy").json()["has_token"] is True
    client.post("/api/notify/ntfy", json={"topic": "t3", "token": "•••set"})
    S.invalidate()
    assert S.load_settings().notifications.ntfy_token == "tk_secret"


def test_token_is_dropped_when_the_server_host_changes():
    """Retargeting the channel must not hand server A's credential to server B."""
    client.post(
        "/api/notify/ntfy",
        json={"topic": "t1", "server": "https://a.example.com", "token": "tk_a"},
    )
    v = client.post("/api/notify/ntfy", json={"server": "https://b.example.com"}).json()
    assert v["has_token"] is False
    assert "previous ntfy server" in v.get("note", "")
    S.invalidate()
    assert S.load_settings().notifications.ntfy_token == ""


def test_clear_token_removes_the_saved_token():
    """The escape hatch from the "empty = keep" convention.

    An ntfy token is optional, and a wrong one is worse than none (ntfy answers
    a bad credential with 401 even on a topic that needs no credential), so
    there has to be a way back to having none at all.
    """
    client.post("/api/notify/ntfy", json={"topic": "t1", "token": "tk_secret"})
    v = client.post("/api/notify/ntfy", json={"clear_token": True}).json()
    assert v["has_token"] is False
    assert "cleared" in v.get("note", "")
    S.invalidate()
    assert S.load_settings().notifications.ntfy_token == ""
    # Same server, same topic: clearing must not disturb the rest of the config.
    assert v["topic"] == "t1"


def test_clear_token_wins_over_a_token_in_the_same_payload():
    client.post("/api/notify/ntfy", json={"topic": "t1", "token": "tk_secret"})
    v = client.post(
        "/api/notify/ntfy", json={"token": "tk_new", "clear_token": True}
    ).json()
    assert v["has_token"] is False
    S.invalidate()
    assert S.load_settings().notifications.ntfy_token == ""


def test_clear_token_on_a_channel_with_no_token_is_a_no_op():
    """No stored token: still fine, and no note claiming something was cleared."""
    client.post("/api/notify/ntfy", json={"topic": "t1"})
    v = client.post("/api/notify/ntfy", json={"clear_token": True}).json()
    assert v["has_token"] is False
    assert "cleared" not in v.get("note", "")


def test_token_survives_an_unrelated_save():
    client.post(
        "/api/notify/ntfy",
        json={"topic": "t1", "server": "https://a.example.com", "token": "tk_a"},
    )
    v = client.post("/api/notify/ntfy", json={"topic": "t2"}).json()
    assert v["has_token"] is True


def test_click_url_access_token_is_stripped():
    v = client.post(
        "/api/notify/ntfy",
        json={"topic": "t1", "click_url": "http://box:8080/m?token=SUPERSECRET"},
    ).json()
    assert "SUPERSECRET" not in v["click_url"] and "token" not in v["click_url"]
    assert "access token was removed" in v.get("note", "")
    S.invalidate()
    assert "SUPERSECRET" not in S.load_settings().notifications.ntfy_click_url


def test_ntfy_token_is_masked_in_the_settings_read():
    """The generic /api/settings must not leak the token either."""
    client.post("/api/notify/ntfy", json={"topic": "t1", "token": "tk_secret"})
    r = client.get("/api/settings")
    assert "tk_secret" not in r.text
    assert r.json()["settings"]["notifications"]["ntfy_token"] == "•••set"


def test_settings_write_keeps_the_token_on_a_masked_submit():
    client.post("/api/notify/ntfy", json={"topic": "t1", "token": "tk_secret"})
    client.post("/api/settings", json={"notifications": {"ntfy_token": "•••set"}})
    S.invalidate()
    assert S.load_settings().notifications.ntfy_token == "tk_secret"


def test_qr_is_offered_once_a_topic_exists():
    client.post("/api/notify/ntfy", json={"topic": "mindflock-qr"})
    qr = client.get("/api/notify/ntfy").json()["qr_svg"]
    # segno is a soft dependency: present -> a bare inline <svg>, absent -> None.
    assert qr is None or qr.lstrip().startswith("<svg")


def test_test_endpoint_uses_the_request_body(http):
    """ "Send a test" must verify a topic the user hasn't saved yet."""
    r = client.post(
        "/api/notify/ntfy/test", json={"topic": "unsaved-topic", "token": "tk_body"}
    ).json()
    assert r == {"ok": True, "error": ""}
    assert http[0]["json"]["topic"] == "unsaved-topic"
    assert http[0]["headers"]["Authorization"] == "Bearer tk_body"
    S.invalidate()
    assert S.load_settings().notifications.ntfy_topic == ""  # a test saves nothing


def test_test_endpoint_falls_back_to_the_saved_token(http):
    client.post("/api/notify/ntfy", json={"topic": "t1", "token": "tk_saved"})
    client.post("/api/notify/ntfy/test", json={"topic": "t1", "token": "•••set"})
    assert http[-1]["headers"]["Authorization"] == "Bearer tk_saved"


def test_test_endpoint_reports_a_bad_config_without_sending(http):
    r = client.post("/api/notify/ntfy/test", json={"topic": ""}).json()
    assert r["ok"] is False and "topic" in r["error"]
    assert not http


def test_test_endpoint_reports_the_server_error(http):
    http.state["response"] = _FakeResponse(401, payload={"error": "unauthorized"})
    r = client.post("/api/notify/ntfy/test", json={"topic": "t1"}).json()
    assert r["ok"] is False and "unauthorized" in r["error"]


# --------------------------------------------------------------------------- #
# Rule dispatch (the server-side channel)
# --------------------------------------------------------------------------- #
@pytest.fixture
def pushes(monkeypatch):
    """Capture what dispatch would push, without touching the transport."""
    sent: list = []
    monkeypatch.setattr(
        ntfy,
        "publish_soon",
        lambda cfg, **kw: sent.append({"cfg": cfg, **kw}),
    )
    return sent


def _addon() -> notify_addon.NotifyAddon:
    return notify_addon.NotifyAddon()


def _clarify(session="alpha") -> dict:
    return {
        "seq": 1,
        "event": "session.activity_changed",
        "session": session,
        "old": "working",
        "new": "clarify",
        "ts": 0.0,
        "data": {},
    }


def test_dispatch_pushes_a_matching_event(pushes):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    _addon()._on_event(_clarify())
    (push,) = pushes
    assert push["title"] == "alpha needs your input"
    assert push["message"] == "The agent is waiting on a clarification."
    # Priority 4 is what rings a phone through most do-not-disturb setups.
    assert push["priority"] == 4 and push["tags"] == ["question"]
    assert push["cfg"].topic == "t1"


def test_dispatch_is_silent_when_the_channel_is_off(pushes):
    _addon()._on_event(_clarify())  # nothing configured
    S.update_settings(notifications={"ntfy_topic": "t1"})  # topic but not enabled
    _addon()._on_event(_clarify())
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": ""})
    _addon()._on_event(_clarify())  # enabled but no topic
    assert pushes == []


def test_dispatch_respects_a_muted_rule(pushes):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    client.post("/api/notify/rules/needs_input", json={"enabled": False})
    _addon()._on_event(_clarify())
    assert pushes == []


def test_dispatch_honours_an_opted_in_rule(pushes):
    """Default-off rules push only once the user turns them on."""
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    idle = {**_clarify(), "new": "idle"}
    _addon()._on_event(idle)
    assert pushes == []
    client.post("/api/notify/rules/session_idle", json={"enabled": True})
    _addon()._on_event(idle)
    assert len(pushes) == 1 and pushes[0]["priority"] == 2  # ambient: no buzz


def test_dispatch_dedupes_a_flapping_transition(pushes):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    addon = _addon()
    addon._on_event(_clarify())
    addon._on_event(_clarify())
    assert len(pushes) == 1
    # A different session is its own stream.
    addon._on_event(_clarify(session="beta"))
    assert len(pushes) == 2


def test_dispatch_ignores_unrelated_events(pushes):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    addon = _addon()
    addon._on_event({**_clarify(), "event": "session.created", "new": None})
    # The stage ladder leaves "pr" on every edit and climbs back on every push.
    # NEITHER direction is a PR closing, and pr_closed no longer keys off it.
    addon._on_event(
        {**_clarify(), "event": "session.stage_changed", "old": "pushed", "new": "pr"}
    )
    addon._on_event(
        {**_clarify(), "event": "session.stage_changed", "old": "pr", "new": "agent"}
    )
    # A PR opening is not a PR closing either.
    addon._on_event(
        {**_clarify(), "event": "session.pr_state_changed", "old": "", "new": "OPEN"}
    )
    assert pushes == []


@pytest.mark.parametrize("ending", ["MERGED", "CLOSED"])
def test_pr_closed_matches_a_real_pr_ending(pushes, ending):
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    _addon()._on_event(
        {
            **_clarify(),
            "event": "session.pr_state_changed",
            "old": "OPEN",
            "new": ending,
        }
    )
    (push,) = pushes
    assert push["title"] == "alpha: PR merged or closed"


def test_dispatch_never_raises_into_the_emitter(monkeypatch):
    """A push failure must not break the event bus for everyone else."""
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})

    def _boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(ntfy, "publish_soon", _boom)
    _addon()._on_event(_clarify())  # no exception


async def test_startup_is_idempotent():
    """A second on_startup must not stack a second subscriber (nothing reclaims
    an unsubscribe callable the addon drops)."""
    addon = _addon()
    await addon.on_startup(_ctx())
    first = addon._unsubscribe
    await addon.on_startup(_ctx())
    assert addon._unsubscribe is first
    await addon.on_shutdown(_ctx())
    assert addon._unsubscribe is None


async def test_startup_registers_the_loop_for_cross_thread_pushes():
    """on_startup runs on the server loop — that's where publish_soon gets the
    loop it trampolines onto from bus callbacks on worker threads."""
    addon = _addon()
    await addon.on_startup(_ctx())
    assert ntfy._LOOP is asyncio.get_running_loop()
    await addon.on_shutdown(_ctx())


async def test_startup_subscribes_to_the_bus(pushes):
    """End to end through the real bus: emit -> push, and unsubscribe on stop."""
    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    addon = _addon()
    ctx = _ctx()
    await addon.on_startup(ctx)
    events.BUS.emit(
        "session.activity_changed", session="gamma", old="working", new="clarify"
    )
    assert len(pushes) == 1 and pushes[0]["title"] == "gamma needs your input"
    await addon.on_shutdown(ctx)
    events.BUS.emit(
        "session.activity_changed", session="delta", old="working", new="clarify"
    )
    assert len(pushes) == 1  # unsubscribed


# --------------------------------------------------------------------------- #
# publish_soon: the cross-thread trampoline
#
# Bus callbacks run synchronously on whatever thread emitted, so this is the
# seam that keeps a 10s HTTP call off the emitter's critical path. Each branch
# (running loop / registered loop from another thread / no loop at all) is a
# different failure if it regresses: a blocked emit, a dropped push, or a
# "coroutine was never awaited" warning.
# --------------------------------------------------------------------------- #
async def test_publish_soon_schedules_on_the_running_loop(http):
    cfg = ntfy.NtfyConfig(enabled=True, topic="t1")
    ntfy.publish_soon(cfg, title="T", message="M")
    assert await _settle(lambda: len(http) == 1)
    assert http[0]["json"]["topic"] == "t1"


async def test_publish_soon_from_a_worker_thread_uses_the_registered_loop(http):
    """The real path: emit() on a FastAPI worker thread, HTTP on the server loop."""
    ntfy.set_loop(asyncio.get_running_loop())
    cfg = ntfy.NtfyConfig(enabled=True, topic="from-thread")
    done = threading.Event()

    def _emit_off_loop():
        ntfy.publish_soon(cfg, title="T", message="M")
        done.set()

    threading.Thread(target=_emit_off_loop, daemon=True).start()
    assert done.wait(5)
    assert await _settle(lambda: len(http) == 1)
    assert http[0]["json"]["topic"] == "from-thread"


def test_publish_soon_without_a_loop_runs_on_a_throwaway_thread(http):
    """A bare sync context (CLI, a unit test) still delivers — no loop needed."""
    assert ntfy._LOOP is None  # the fixture cleared it
    ntfy.publish_soon(
        ntfy.NtfyConfig(enabled=True, topic="no-loop"), title="T", message="M"
    )
    assert _wait(lambda: len(http) == 1)
    assert http[0]["json"]["topic"] == "no-loop"


def test_publish_soon_never_raises_at_the_emitter(monkeypatch, http):
    """Whatever the dispatch does, emit() must survive it — and the coroutine it
    could not schedule must be *closed*, not abandoned to a "never awaited"
    warning at some later GC.

    Asserting on the warning is not enough: the raised OSError's traceback keeps
    the frame (and the coroutine) alive, so the warning never fires during the
    test either way. So hold the coroutine ourselves and check its frame.
    """
    made: list = []
    real_publish = ntfy.publish

    def _tracked(*a, **kw):
        coro = real_publish(*a, **kw)
        made.append(coro)
        return coro

    monkeypatch.setattr(ntfy, "publish", _tracked)
    monkeypatch.setattr(
        ntfy.threading,
        "Thread",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("cannot spawn")),
    )

    ntfy.publish_soon(ntfy.NtfyConfig(enabled=True, topic="t1"), title="T", message="M")

    assert len(made) == 1
    assert made[0].cr_frame is None, "the unscheduled coroutine was left un-closed"
    assert not http  # nothing was sent, and nothing raised at the call site


# --------------------------------------------------------------------------- #
# The QR renderer, shared with Settings → Mobile
# --------------------------------------------------------------------------- #
def test_qr_svg_is_the_shared_renderer():
    """The ntfy view is why ``_mobile_svg`` became the public ``qr_svg``; the old
    name is still what server.py imports, so both have to keep working."""
    assert mobile_access._mobile_svg is mobile_access.qr_svg
    assert server._mobile_svg is mobile_access.qr_svg
    svg = mobile_access.qr_svg("https://ntfy.sh/mindflock-abc")
    if svg is None:
        pytest.skip("segno is not installed — qr_svg degrades to None by design")
    # A bare <svg> (no XML prolog) with a viewBox, so it scales in whatever box
    # the settings CSS gives it — injected via innerHTML at both call sites.
    assert svg.lstrip().startswith("<svg") and "viewBox=" in svg
    assert "<?xml" not in svg


# --------------------------------------------------------------------------- #
# What must never leak
# --------------------------------------------------------------------------- #
def test_nothing_logs_the_topic_or_the_token(monkeypatch, http):
    """mindflock.log is served back out over GET /api/logs, and on a public
    server the topic IS the credential — so log lines carry the host only."""
    lines: list = []
    monkeypatch.setattr(ntfy, "log_error", lambda fmt, *a: lines.append(fmt % a))
    http.state["response"] = _FakeResponse(403, payload={"error": "forbidden"})
    cfg = ntfy.NtfyConfig(
        enabled=True, topic="super-secret-topic", token="tk_secret", server="https://h"
    )
    asyncio.run(ntfy.publish(cfg, title="T", message="M"))
    assert lines, "a failed push should log something"
    blob = "\n".join(lines)
    assert "super-secret-topic" not in blob and "tk_secret" not in blob
    assert "h" in blob  # the host is what identifies the failure


# --------------------------------------------------------------------------- #
# Rule payload + frontend wiring
# --------------------------------------------------------------------------- #
def test_every_rule_carries_push_metadata():
    for rule in notify_addon.NOTIFY_RULES:
        assert rule["priority"] in (2, 3, 4), rule["id"]
        assert rule["tags"] and all(isinstance(t, str) for t in rule["tags"])


def test_push_metadata_is_not_exposed_to_the_client():
    """The client needs the resolved ``enabled``, not our internals."""
    for rule in client.get("/api/notify/config").json()["rules"]:
        assert "priority" not in rule and "tags" not in rule
        assert "default_enabled" not in rule
        assert rule["event"] and "enabled" in rule


def test_frontend_wires_the_ntfy_section():
    js = client.get("/app.js").text
    assert "/api/notify/ntfy" in js
    assert "/api/notify/ntfy/test" in js
    assert "suggested_topic" in js  # Generate uses the server's random topic
