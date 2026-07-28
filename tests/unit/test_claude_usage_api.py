"""Live Claude plan-usage (claude_usage_api).

The module reads Anthropic's OAuth usage endpoint READ-ONLY and normalizes it
into the shared ``usage_live()`` shape. These tests lock the normalization, the
credential gate, and the TTL/grace caching — including that the blocking fetch
runs OUTSIDE the module lock so one slow probe can't serialize every caller.
"""

from __future__ import annotations

import json

import pytest

from backend.providers import claude_usage_api as api


@pytest.fixture(autouse=True)
def _reset_cache():
    api._cache = {"at": 0.0, "good_at": 0.0, "good": None}
    yield
    api._cache = {"at": 0.0, "good_at": 0.0, "good": None}


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
# live_usage: TTL throttle, grace on failure, fetch-outside-lock
# --------------------------------------------------------------------------- #
def test_live_usage_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def _f():
        calls["n"] += 1
        return {"percent_used": 10.0}

    monkeypatch.setattr(api, "_fetch", _f)
    assert api.live_usage() == {"percent_used": 10.0}
    assert api.live_usage() == {"percent_used": 10.0}
    assert calls["n"] == 1  # second call served from cache, no re-fetch


def test_live_usage_grace_serves_last_good_on_failure(monkeypatch):
    seq = [{"percent_used": 10.0}, None]

    def _f():
        return seq.pop(0)

    monkeypatch.setattr(api, "_fetch", _f)
    assert api.live_usage() == {"percent_used": 10.0}
    # Force the TTL to expire; next call fetches None but grace reuses last good.
    api._cache["at"] = 0.0
    assert api.live_usage() == {"percent_used": 10.0}


def test_live_usage_grace_expires_to_none(monkeypatch):
    monkeypatch.setattr(api, "_fetch", lambda: None)
    # Prime a stale last-good beyond the grace horizon.
    import time

    api._cache = {
        "at": 0.0,
        "good_at": time.time() - (api._GRACE + 10),
        "good": {"percent_used": 5.0},
    }
    assert api.live_usage() is None


def test_live_usage_fetch_runs_without_holding_lock(monkeypatch):
    # Regression: the ~4s network fetch must NOT run while the module lock is
    # held, or concurrent callers serialize behind one slow probe. We assert the
    # lock is released during _fetch by acquiring it from inside the fetch.
    api._cache = {"at": 0.0, "good_at": 0.0, "good": None}

    def _f():
        acquired = api._lock.acquire(blocking=False)
        if acquired:
            api._lock.release()
        assert acquired, "lock was held across the network fetch"
        return {"percent_used": 1.0}

    monkeypatch.setattr(api, "_fetch", _f)
    assert api.live_usage() == {"percent_used": 1.0}
