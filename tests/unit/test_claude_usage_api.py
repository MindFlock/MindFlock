"""Live Claude plan-usage (claude_usage_api).

The module reads Anthropic's OAuth usage endpoint READ-ONLY and normalizes it
into the shared ``usage_live()`` shape. These tests lock the normalization (both
payload shapes, weekly-only plans), the credential gate, and the shared
per-machine reading — one fetch for every process, the 429 backoff they all
honour, stale serving — including that the blocking fetch runs OUTSIDE the
module lock so one slow probe can't serialize every caller.
"""

from __future__ import annotations

import json

import pytest

from backend.providers import claude_usage_api as api


@pytest.fixture(autouse=True)
def _reset_cache(tmp_path, monkeypatch):
    # conftest points MINDFLOCK_RUN_DIR at a per-test dir; a fresh module
    # state per test keeps the shared reading per-test too.
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "run"))
    api._SHARED = None
    yield
    api._SHARED = None


@pytest.fixture
def _creds(tmp_path, monkeypatch):
    root = tmp_path / ".claude"
    root.mkdir()
    (root / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-abc"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


# --------------------------------------------------------------------------- #
# _iso_epoch / _token
# --------------------------------------------------------------------------- #
def test_iso_epoch_parses_and_rejects_junk():
    assert api._iso_epoch("2026-07-14T20:05:00+00:00") == pytest.approx(1784059500)
    assert api._iso_epoch("not-a-date") is None
    assert api._iso_epoch(None) is None


def test_token_missing_creds_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert api._token() is None


def test_token_reads_access_token(_creds):
    assert api._token() == "tok-abc"


def test_token_none_when_creds_carry_no_access_token(tmp_path, monkeypatch):
    # A credentials file present but with no accessToken -> None (the "no OAuth
    # token" gate), so live usage stays dark rather than sending an empty Bearer.
    root = tmp_path / ".claude"
    root.mkdir()
    (root / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    assert api._token() is None


# --------------------------------------------------------------------------- #
# _fetch normalization
# --------------------------------------------------------------------------- #
def _mock_response(monkeypatch, doc):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(doc).encode()

    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: _Resp())


def test_fetch_normalizes_five_hour_weekly_and_extra(_creds, monkeypatch):
    doc = {
        "five_hour": {"utilization": 42.5, "resets_at": "2026-07-14T20:05:00+00:00"},
        "limits": [
            {
                "group": "weekly",
                "percent": 12.0,
                "resets_at": "2026-07-20T00:00:00+00:00",
            }
        ],
        "extra_usage": {
            "is_enabled": True,
            "decimal_places": 2,
            "used_credits": 6333,
            "monthly_limit": 10000,
            "currency": "USD",
        },
    }
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out["percent_used"] == pytest.approx(42.5)
    assert int(out["end"]) == 1784059500
    assert out["weekly"]["percent_used"] == pytest.approx(12.0)
    assert out["extra"]["used"] == pytest.approx(63.33)
    assert out["extra"]["limit"] == pytest.approx(100.0)


def test_fetch_extra_usage_disabled_is_dropped(_creds, monkeypatch):
    # extra_usage present but is_enabled False: the plan window isn't spent, so
    # the billed-dollars signal must NOT surface (gate at claude_usage_api:135).
    doc = {
        "five_hour": {"utilization": 10.0},
        "extra_usage": {
            "is_enabled": False,
            "decimal_places": 2,
            "used_credits": 6333,
            "monthly_limit": 10000,
        },
    }
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out is not None
    assert "extra" not in out


def test_fetch_extra_usage_no_decimal_places_scale_one(_creds, monkeypatch):
    # No decimal_places -> scale 10**0 == 1, so used_credits are whole dollars.
    doc = {
        "extra_usage": {
            "is_enabled": True,
            "used_credits": 500,
            "monthly_limit": 2000,
            "currency": "USD",
        },
    }
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out["extra"]["used"] == pytest.approx(500.0)
    assert out["extra"]["limit"] == pytest.approx(2000.0)


def test_fetch_extra_usage_non_numeric_credits_dropped(_creds, monkeypatch):
    # Non-numeric used_credits trips the TypeError/ValueError guard (line 143),
    # so the extra block is dropped instead of crashing or showing a bogus spend.
    doc = {
        "five_hour": {"utilization": 10.0},
        "extra_usage": {
            "is_enabled": True,
            "decimal_places": 2,
            "used_credits": "not-a-number",
            "monthly_limit": 10000,
        },
    }
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out is not None
    assert "extra" not in out


def test_fetch_weekly_accepts_utilization_spelling(_creds, monkeypatch):
    # The five-hour window spells the field "utilization" while limits[] entries
    # have been observed with "percent" — the weekly read must accept either.
    doc = {
        "limits": [
            {
                "group": "weekly",
                "utilization": 7.5,
                "resets_at": "2026-07-20T00:00:00+00:00",
            }
        ],
    }
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out["weekly"]["percent_used"] == pytest.approx(7.5)
    # "percent" still wins when present.
    doc["limits"][0]["percent"] = 12.0
    _mock_response(monkeypatch, doc)
    assert api._fetch()["weekly"]["percent_used"] == pytest.approx(12.0)


def test_fetch_non_numeric_utilization_is_dropped(_creds, monkeypatch):
    # A five_hour.utilization that doesn't coerce to float trips the guard
    # (lines 104-105): percent_used is omitted rather than crashing the parse.
    doc = {"five_hour": {"utilization": "lots", "resets_at": "2026-07-14T20:05:00Z"}}
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out is not None  # the reset time still lands
    assert "percent_used" not in out
    assert int(out["end"]) == 1784059500


def test_fetch_weekly_non_numeric_percent_dropped_but_end_kept(_creds, monkeypatch):
    # A weekly entry whose percent doesn't coerce (lines 121-122): percent_used
    # is skipped, but a parseable resets_at still yields a weekly block.
    doc = {
        "limits": [
            {
                "group": "weekly",
                "percent": "n/a",
                "resets_at": "2026-07-20T00:00:00+00:00",
            }
        ],
    }
    _mock_response(monkeypatch, doc)
    out = api._fetch()
    assert out is not None
    assert "percent_used" not in out["weekly"]
    assert out["weekly"]["end"] > 0


def test_fetch_no_token_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert api._fetch() is None


def test_fetch_network_error_returns_none(_creds, monkeypatch):
    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(api.urllib.request, "urlopen", _boom)
    assert api._fetch() is None


def test_fetch_empty_doc_returns_none(_creds, monkeypatch):
    _mock_response(monkeypatch, {})
    assert api._fetch() is None


# --------------------------------------------------------------------------- #
# _normalize: both payload shapes, weekly-only plans
# --------------------------------------------------------------------------- #
_WK = "2026-07-20T00:00:00+00:00"
_FH = "2026-07-14T20:05:00+00:00"


def test_normalize_five_hour_plan_headlines_the_session_window():
    out = api._normalize(
        {
            "five_hour": {"utilization": 42.5, "resets_at": _FH},
            "seven_day": {"utilization": 12.0, "resets_at": _WK},
        }
    )
    assert out["window_kind"] == "session"
    assert out["percent_used"] == pytest.approx(42.5)
    assert int(out["end"]) == 1784059500
    assert out["session"]["percent_used"] == pytest.approx(42.5)
    assert out["weekly"]["percent_used"] == pytest.approx(12.0)


def test_normalize_five_hour_null_is_a_weekly_only_plan():
    # A plan with no session limit ("ultra"): five_hour is null, the weekly
    # window is the only meter — and it becomes the headline.
    out = api._normalize(
        {
            "five_hour": None,
            "seven_day": {"utilization": 35.0, "resets_at": _WK},
            "seven_day_opus": {"utilization": 3.0, "resets_at": _WK},
        }
    )
    assert out["window_kind"] == "weekly"
    assert out["session"] is None
    assert out["percent_used"] == pytest.approx(35.0)
    assert out["end"] == api._iso_epoch(_WK)
    assert out["weekly_opus"]["percent_used"] == pytest.approx(3.0)


def test_normalize_five_hour_absent_with_limits_weekly():
    out = api._normalize(
        {"limits": [{"group": "weekly", "percent": 61.0, "resets_at": _WK}]}
    )
    assert out["window_kind"] == "weekly"
    assert out["session"] is None
    assert out["percent_used"] == pytest.approx(61.0)
    assert out["weekly"]["end"] == api._iso_epoch(_WK)


def test_normalize_limits_weekly_wins_over_seven_day():
    out = api._normalize(
        {
            "five_hour": {"utilization": 1.0, "resets_at": _FH},
            "limits": [{"group": "weekly", "percent": 20.0, "resets_at": _WK}],
            "seven_day": {"utilization": 99.0, "resets_at": _WK},
        }
    )
    assert out["weekly"]["percent_used"] == pytest.approx(20.0)


def test_normalize_fresh_five_hour_window_still_counts_as_session():
    # A present-but-empty five_hour (no use yet, no reset) is a plan WITH a
    # session window, not a weekly-only one.
    out = api._normalize(
        {"five_hour": {}, "seven_day": {"utilization": 5.0, "resets_at": _WK}}
    )
    assert out["window_kind"] == "session"
    assert out["session"] == {}
    assert "percent_used" not in out


def test_normalize_nothing_usable_is_none():
    assert api._normalize({"five_hour": None, "seven_day": None}) is None
    assert api._normalize("junk") is None


# --------------------------------------------------------------------------- #
# _fetch_raw: status + Retry-After for the backoff
# --------------------------------------------------------------------------- #
def _http_error(monkeypatch, code, retry_after=None):
    import email.message
    import urllib.error

    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)

    def _raise(*a, **k):
        raise urllib.error.HTTPError(api._ENDPOINT, code, "x", hdrs, None)

    monkeypatch.setattr(api.urllib.request, "urlopen", _raise)


def test_fetch_raw_reports_429_and_retry_after(_creds, monkeypatch):
    _http_error(monkeypatch, 429, retry_after=0)
    assert api._fetch_raw() == (None, 429, 0.0)
    _http_error(monkeypatch, 429, retry_after=30)
    assert api._fetch_raw() == (None, 429, 30.0)
    _http_error(monkeypatch, 503)
    assert api._fetch_raw() == (None, 503, None)


def test_fetch_raw_success_logs_keys_never_the_token(_creds, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(api, "_shape_logged", False)
    _mock_response(monkeypatch, {"five_hour": None, "seven_day": {"utilization": 1}})
    with caplog.at_level(logging.DEBUG, logger=api.__name__):
        reading, status, _ = api._fetch_raw()
    assert status == 200 and reading["window_kind"] == "weekly"
    text = caplog.text
    assert "five_hour" in text and "seven_day" in text
    assert "tok-abc" not in text


# --------------------------------------------------------------------------- #
# live_usage: ONE reading per machine (shared file), backoff, stale serving
# --------------------------------------------------------------------------- #
from backend.providers import _usage_cache  # noqa: E402

_GOOD = {"window_kind": "session", "percent_used": 10.0, "session": {}}


def _two(tmp_path, fetch, **kw):
    """Two "processes": two SharedReading states over ONE file."""
    path = str(tmp_path / "run" / "claude_usage.json")
    mk = lambda: _usage_cache.SharedReading(  # noqa: E731
        "claude_usage",
        fetch,
        path=path,
        plan_update=api._plan_update,
        **kw,
    )
    return mk(), mk(), path


def test_shared_reading_one_fetch_for_two_processes(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return dict(_GOOD), 200, None

    a, b, path = _two(tmp_path, fetch)
    assert a.get()["percent_used"] == 10.0
    got = b.get()
    assert got["percent_used"] == 10.0 and got["stale"] is False
    assert calls == [1]  # the second process read the file, no fetch
    doc = json.loads(open(path).read())
    assert doc["reading"]["percent_used"] == 10.0 and doc["fetched_at"] > 0
    assert "tok" not in open(path).read()


def test_shared_reading_429_backs_off_for_every_process(tmp_path, monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return None, 429, 0.0  # Retry-After: 0 -> our own backoff

    a, b, path = _two(tmp_path, fetch, ttl=60.0, backoff_base=120.0)
    assert a.get() is None
    doc = json.loads(open(path).read())
    assert doc["failures"] == 1 and doc["last_status"] == 429
    until = doc["backoff_until"]
    assert 0.8 * 120 - 1 <= until - doc["attempt_at"] <= 1.2 * 120 + 1
    # Past the TTL but inside the backoff: neither process fetches.
    import time as _t

    real = _t.time
    monkeypatch.setattr(_usage_cache.time, "time", lambda: real() + 90)
    assert b.get() is None and a.get() is None
    assert calls == [1]
    # After the backoff: one more fetch, and the failure count doubles the wait.
    monkeypatch.setattr(_usage_cache.time, "time", lambda: until + 1)
    b.get()
    doc = json.loads(open(path).read())
    assert calls == [1, 1] and doc["failures"] == 2
    assert doc["backoff_until"] - doc["attempt_at"] >= 0.8 * 240 - 1


def test_shared_reading_retry_after_wins(tmp_path):
    a, _, path = _two(tmp_path, lambda: (None, 429, 1800.0))
    a.get()
    doc = json.loads(open(path).read())
    assert doc["backoff_until"] - doc["attempt_at"] == pytest.approx(1800.0, abs=1)


def test_backoff_delay_doubles_to_the_cap():
    mid = lambda: 0.5  # noqa: E731 — no jitter
    assert [_usage_cache.backoff_delay(n, 120, 900, mid) for n in (1, 2, 3, 4, 9)] == [
        120,
        240,
        480,
        900,
        900,
    ]


def test_shared_reading_serves_last_good_stale_through_backoff(tmp_path, monkeypatch):
    seq = [(dict(_GOOD), 200, None), (None, 429, 0.0)]
    a, b, path = _two(tmp_path, lambda: seq.pop(0), stale_after=150.0)
    assert a.get()["stale"] is False
    import time as _t

    t0 = _t.time()
    monkeypatch.setattr(_usage_cache.time, "time", lambda: t0 + 600)
    got = b.get()  # due -> fetch -> 429 -> still the old reading, now stale
    assert got["percent_used"] == 10.0
    assert got["stale"] is True
    assert got["backoff_until"] > t0 + 600
    assert t0 + 600 - got["fetched_at"] >= 599
    # A deciding caller asks for a recent one and gets nothing.
    assert b.get(max_age=300) is None
    # Past stale_max (6 h) even the display reading is dropped.
    monkeypatch.setattr(_usage_cache.time, "time", lambda: t0 + 7 * 3600)
    assert b.get() is None


def test_shared_reading_remembers_plan_shape(tmp_path):
    weekly_only = {
        "window_kind": "weekly",
        "session": None,
        "weekly": {"percent_used": 35.0, "end": 2e9},
    }
    a, b, _ = _two(tmp_path, lambda: (weekly_only, 200, None))
    a.get()
    plan = b.plan()
    assert plan["weekly_end"] == 2e9 and not plan.get("session_seen_at")


def test_shared_reading_skips_when_another_process_holds_the_lock(tmp_path):
    import fcntl
    import os as _os

    calls = []
    a, _, path = _two(tmp_path, lambda: calls.append(1) or (dict(_GOOD), 200, None))
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    fd = _os.open(path + ".lock", _os.O_RDWR | _os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert a.get() is None
        assert calls == []
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        _os.close(fd)
    assert a.get()["percent_used"] == 10.0


def test_live_usage_is_shared_through_the_run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "run"))
    calls = []
    monkeypatch.setattr(
        api, "_fetch_raw", lambda: calls.append(1) or (dict(_GOOD), 200, None)
    )
    assert api.live_usage()["percent_used"] == 10.0
    api._SHARED = None  # a second process: fresh module state, same file
    assert api.live_usage()["percent_used"] == 10.0
    assert calls == [1]
    assert (tmp_path / "run" / "claude_usage.json").exists()


def test_plan_shape_weekly_only_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "run"))
    assert api.plan_shape() is None  # never read
    reading = {"window_kind": "weekly", "session": None, "weekly": {"end": 2e9}}
    monkeypatch.setattr(api, "_fetch_raw", lambda: (reading, 200, None))
    api.live_usage()
    assert api.plan_shape() == {"session": False, "weekly_end": 2e9}
    # A 5-hour plan: session seen -> remembered as having one.
    api._SHARED = None
    (tmp_path / "run" / "claude_usage.json").unlink()
    monkeypatch.setattr(
        api, "_fetch_raw", lambda: ({**_GOOD, "weekly": {"end": 2e9}}, 200, None)
    )
    api.live_usage()
    assert api.plan_shape()["session"] is True


def test_live_usage_fetch_runs_without_holding_lock(tmp_path, monkeypatch):
    # Regression: the ~4s network fetch must NOT run while a module lock is
    # held, or concurrent callers serialize behind one slow probe.
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "run"))
    shared = api._shared()

    def _f():
        for lk in (api._lock, shared._lock):
            acquired = lk.acquire(blocking=False)
            if acquired:
                lk.release()
            assert acquired, "lock was held across the network fetch"
        return {"percent_used": 1.0}, 200, None

    monkeypatch.setattr(api, "_fetch_raw", _f)
    assert api.live_usage()["percent_used"] == 1.0


# --- macOS credentials (Keychain) ------------------------------------------- #
# Claude Code stores its OAuth credentials in the login Keychain on macOS, not
# in ~/.claude/.credentials.json. Without the fallback below, live plan usage
# was permanently dark on every Mac: no token -> no live reading -> the UI fell
# back to the transcript estimate, which shows a reset countdown but no
# percentage unless a window budget happens to be configured.


def _no_creds_file(monkeypatch, tmp_path):
    """Point the file path at an empty dir so only the keychain can answer."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))


def test_token_falls_back_to_the_macos_keychain(monkeypatch, tmp_path):
    _no_creds_file(monkeypatch, tmp_path)
    monkeypatch.setattr(api.sys, "platform", "darwin")
    blob = json.dumps({"claudeAiOauth": {"accessToken": "kc-tok"}}).encode()

    def _run(argv, **kw):
        assert argv[:2] == ["security", "find-generic-password"]
        assert api._KEYCHAIN_SERVICE in argv
        assert kw.get("timeout") == api._KEYCHAIN_TIMEOUT  # can't hang on a prompt
        return api.subprocess.CompletedProcess(argv, 0, stdout=blob)

    monkeypatch.setattr(api.subprocess, "run", _run)
    assert api._token() == "kc-tok"


def test_keychain_is_not_consulted_off_macos(monkeypatch, tmp_path):
    _no_creds_file(monkeypatch, tmp_path)
    monkeypatch.setattr(api.sys, "platform", "linux")

    def _boom(*a, **k):
        raise AssertionError("must not shell out to `security` off macOS")

    monkeypatch.setattr(api.subprocess, "run", _boom)
    assert api._token() is None


def test_keychain_denial_is_not_an_error(monkeypatch, tmp_path):
    """A denied prompt / missing item / no `security` binary means "no live
    data", exactly as before — never a raise."""
    _no_creds_file(monkeypatch, tmp_path)
    monkeypatch.setattr(api.sys, "platform", "darwin")
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda argv, **kw: api.subprocess.CompletedProcess(argv, 1, stdout=b""),
    )
    assert api._token() is None

    def _timeout(*a, **k):
        raise api.subprocess.TimeoutExpired("security", 5)

    monkeypatch.setattr(api.subprocess, "run", _timeout)
    assert api._token() is None


def test_credentials_file_wins_when_present(monkeypatch, tmp_path):
    """The file is authoritative where it exists — no keychain prompt at all."""
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "file-tok"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(api.sys, "platform", "darwin")
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("keychain consulted")),
    )
    assert api._token() == "file-tok"


def test_provider_decides_on_recent_readings_and_displays_stale_ones(monkeypatch):
    from backend.providers.claude import ClaudeProvider

    seen = []
    monkeypatch.setattr(api, "live_usage", lambda max_age=None: seen.append(max_age))
    p = ClaudeProvider()
    p.usage_live()
    p.usage_live_display()
    assert seen == [api.DECISION_MAX_AGE, None]
