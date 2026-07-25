"""Hermetic unit tests for :mod:`backend.providers.pricing`.

No network, no real disk cache outside ``tmp_path``. ``urllib.request.urlopen``
is always mocked; the module's disk cache is redirected via
``MINDFLOCK_ASSISTANT_DIR`` (monkeypatched) or, where more direct, by patching
``pricing._cache_path``. The in-process memo (``pricing._mem``) is reset before
each test so TTL/cache behavior is deterministic.
"""

from __future__ import annotations

import json
import time

import pytest

from backend.providers import pricing

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A small fake feed mirroring the real endpoint's shape.
FAKE_FEED = {
    "models": [
        {
            "id": "claude-opus-4.8",
            "context": 200000,
            "pricing": {"inputPerM": 5.0, "cachedInputPerM": 0.5, "outputPerM": 25.0},
        },
        {
            "id": "claude-sonnet-4.5",
            "context": 1000000,
            "pricing": {"inputPerM": 3.0, "cachedInputPerM": 0.3, "outputPerM": 15.0},
        },
        {
            "id": "claude-haiku-4",
            # no "context" key -> ctx should become None
            "pricing": {"inputPerM": 1.0, "cachedInputPerM": 0.1, "outputPerM": 5.0},
        },
    ]
}


@pytest.fixture(autouse=True)
def _reset_memo():
    """Reset the module memo so every test starts cold and deterministic."""
    pricing._mem = {"at": 0.0, "table": None}
    yield
    pricing._mem = {"at": 0.0, "table": None}


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect the disk cache into tmp_path via MINDFLOCK_ASSISTANT_DIR."""
    d = tmp_path / "assistant"
    d.mkdir()
    monkeypatch.setenv("MINDFLOCK_ASSISTANT_DIR", str(d))
    return d


class _FakeResp:
    """Minimal context-manager stand-in for urlopen's return value."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _mock_urlopen(monkeypatch, *, raw=None, exc=None, spy=None):
    """Patch urllib.request.urlopen to return `raw` bytes or raise `exc`."""

    def fake(req, timeout=None):
        if spy is not None:
            spy.append((req, timeout))
        if exc is not None:
            raise exc
        return _FakeResp(raw)

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake)


# ---------------------------------------------------------------------------
# _norm
# ---------------------------------------------------------------------------


def test_norm_strips_and_lowercases():
    assert pricing._norm("Claude-Opus-4.8") == "claudeopus48"


def test_norm_dotted_and_hyphen_converge():
    # Feed dotted id and transcript hyphen id normalize identically.
    assert pricing._norm("claude-opus-4.8") == pricing._norm("Claude Opus 4-8")


def test_norm_none_and_empty():
    assert pricing._norm(None) == ""
    assert pricing._norm("") == ""
    assert pricing._norm("!!!") == ""


def test_norm_non_string_input():
    assert pricing._norm(48) == "48"


# ---------------------------------------------------------------------------
# _parse_feed
# ---------------------------------------------------------------------------


def test_parse_feed_basic_shape():
    table = pricing._parse_feed(FAKE_FEED)
    assert table["claudeopus48"] == {
        "in": 5.0,
        "cr": 0.5,
        "out": 25.0,
        "ctx": 200000,
    }
    assert table["claudesonnet45"]["ctx"] == 1000000


def test_parse_feed_missing_context_becomes_none():
    table = pricing._parse_feed(FAKE_FEED)
    assert table["claudehaiku4"]["ctx"] is None


def test_parse_feed_empty_doc():
    assert pricing._parse_feed({}) == {}
    assert pricing._parse_feed({"models": None}) == {}


def test_parse_feed_skips_entries_without_id():
    doc = {"models": [{"pricing": {"inputPerM": 1}}, {"id": "", "pricing": {}}]}
    assert pricing._parse_feed(doc) == {}


def test_parse_feed_missing_pricing_defaults_zero():
    doc = {"models": [{"id": "x-model", "context": 100}]}
    table = pricing._parse_feed(doc)
    assert table["xmodel"] == {"in": 0.0, "cr": 0.0, "out": 0.0, "ctx": 100}


def test_parse_feed_ctx_zero_becomes_none():
    doc = {"models": [{"id": "z", "context": 0, "pricing": {"inputPerM": 2}}]}
    table = pricing._parse_feed(doc)
    assert table["z"]["ctx"] is None


def test_parse_feed_null_pricing_values_treated_as_zero():
    doc = {"models": [{"id": "n", "pricing": {"inputPerM": None, "outputPerM": None}}]}
    table = pricing._parse_feed(doc)
    assert table["n"]["in"] == 0.0
    assert table["n"]["out"] == 0.0


def test_parse_feed_skips_model_with_uncoercible_field():
    # A per-model coercion failure (a non-numeric context) is skipped, not fatal:
    # the other, well-formed model still lands in the table.
    doc = {
        "models": [
            {"id": "bad", "context": "not-a-number", "pricing": {"inputPerM": 1}},
            {"id": "good", "context": 100, "pricing": {"inputPerM": 2}},
        ]
    }
    table = pricing._parse_feed(doc)
    assert "bad" not in table
    assert table["good"]["ctx"] == 100


# ---------------------------------------------------------------------------
# _lookup (uses _table()); drive it by seeding the memo so no I/O happens.
# ---------------------------------------------------------------------------


def _seed_memo(table):
    pricing._mem = {"at": time.time(), "table": table}


def test_lookup_exact_match():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    hit = pricing._lookup("claude-opus-4.8")
    assert hit["in"] == 5.0 and hit["out"] == 25.0


def test_lookup_longest_prefix_dated_suffix():
    # Transcript id carries a dated suffix; should match the opus family entry.
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    hit = pricing._lookup("claude-opus-4-8-20260101")
    assert hit["in"] == 5.0


def test_lookup_prefers_longest_key():
    # Two feed keys both prefix-compatible; longest must win.
    _seed_memo(
        {
            "claude": {"in": 1, "cr": 0, "out": 2, "ctx": None},
            "claudeopus": {"in": 9, "cr": 0, "out": 3, "ctx": None},
        }
    )
    hit = pricing._lookup("claude-opus-4-8")
    assert hit["in"] == 9


def test_lookup_feed_id_longer_than_query_still_matches():
    # key.startswith(nm) branch: feed id is longer than the queried id.
    _seed_memo({"claudeopus48": {"in": 5, "cr": 0.5, "out": 25, "ctx": 200000}})
    hit = pricing._lookup("claude-opus")
    assert hit["in"] == 5


def test_lookup_no_match_returns_none():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    assert pricing._lookup("gpt-4o") is None


def test_lookup_empty_model_returns_none():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    assert pricing._lookup("") is None
    assert pricing._lookup(None) is None


# ---------------------------------------------------------------------------
# _fallback
# ---------------------------------------------------------------------------


def test_fallback_family_match():
    assert pricing._fallback("claude-opus-4-8") == (5.0, 0.5, 25.0)
    assert pricing._fallback("Claude Sonnet 4.5") == (3.0, 0.3, 15.0)
    assert pricing._fallback("claude-haiku") == (1.0, 0.1, 5.0)
    assert pricing._fallback("claude-fable") == (10.0, 1.0, 50.0)


def test_fallback_default_for_unknown():
    assert pricing._fallback("gpt-4o") == pricing._FALLBACK_DEFAULT
    assert pricing._fallback("") == pricing._FALLBACK_DEFAULT


# ---------------------------------------------------------------------------
# price_per_token
# ---------------------------------------------------------------------------


def test_price_per_token_from_feed():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    p = pricing.price_per_token("claude-opus-4.8")
    assert p["in"] == pytest.approx(5.0 / 1e6)
    assert p["cache_read"] == pytest.approx(0.5 / 1e6)
    assert p["out"] == pytest.approx(25.0 / 1e6)
    # cache_write derived as 1.25x input.
    assert p["cache_write"] == pytest.approx(5.0 * 1.25 / 1e6)


def test_price_per_token_cache_write_multiplier_relationship():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    p = pricing.price_per_token("claude-sonnet-4.5")
    assert p["cache_write"] == pytest.approx(p["in"] * pricing._CACHE_WRITE_MULT)


def test_price_per_token_falls_back_when_no_feed_hit():
    # Empty memo table -> _lookup misses -> _fallback used.
    _seed_memo({})
    p = pricing.price_per_token("claude-opus-4-8")
    assert p["in"] == pytest.approx(5.0 / 1e6)
    assert p["cache_write"] == pytest.approx(5.0 * 1.25 / 1e6)
    assert p["out"] == pytest.approx(25.0 / 1e6)


def test_price_per_token_unknown_uses_default_fallback():
    _seed_memo({})
    p = pricing.price_per_token("some-random-model")
    exp_in, exp_cr, exp_out = pricing._FALLBACK_DEFAULT
    assert p["in"] == pytest.approx(exp_in / 1e6)
    assert p["cache_read"] == pytest.approx(exp_cr / 1e6)
    assert p["out"] == pytest.approx(exp_out / 1e6)


def test_price_per_token_all_zero_hit_falls_back_to_table():
    # A feed that LISTS the model but omits its pricing parses to an all-zero
    # entry (_parse_feed coerces missing prices to 0.0). That must not price the
    # model at $0 — it should fall through to the sane fallback table, exactly
    # as a true miss does. Regression guard for the "Opus shows $0" bug.
    _seed_memo({"claudeopus48": {"in": 0.0, "cr": 0.0, "out": 0.0, "ctx": 200000}})
    p = pricing.price_per_token("claude-opus-4.8")
    exp_in, exp_cr, exp_out = pricing._FALLBACK["claudeopus"]
    assert p["in"] == pytest.approx(exp_in / 1e6)
    assert p["out"] == pytest.approx(exp_out / 1e6)
    assert p["in"] > 0 and p["out"] > 0


def test_context_window_still_honored_for_zero_priced_hit():
    # The all-zero-price fallback is about $ only — a listed model's context
    # window is still real data and must survive (context_window checks ctx
    # independently of the price fields).
    _seed_memo({"claudeopus48": {"in": 0.0, "cr": 0.0, "out": 0.0, "ctx": 321000}})
    assert pricing.context_window("claude-opus-4.8") == 321000


# ---------------------------------------------------------------------------
# context_window
# ---------------------------------------------------------------------------


def test_context_window_from_feed():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    assert pricing.context_window("claude-sonnet-4.5") == 1000000


def test_context_window_defaults_when_ctx_none():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    # haiku entry has ctx=None -> default window.
    assert pricing.context_window("claude-haiku-4") == pricing._DEFAULT_WINDOW


def test_context_window_defaults_when_no_hit():
    _seed_memo({})
    assert pricing.context_window("gpt-4o") == pricing._DEFAULT_WINDOW


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_full_math():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    tok = {
        "in": 1_000_000,
        "out": 1_000_000,
        "cache_read": 1_000_000,
        "cache_write": 1_000_000,
    }
    cost = pricing.estimate_cost(tok, "claude-opus-4.8")
    # 5 (in) + 25 (out) + 0.5 (cache_read) + 5*1.25=6.25 (cache_write)
    assert cost == pytest.approx(5.0 + 25.0 + 0.5 + 6.25)


def test_estimate_cost_missing_keys_treated_as_zero():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    cost = pricing.estimate_cost({"in": 1_000_000}, "claude-opus-4.8")
    assert cost == pytest.approx(5.0)


def test_estimate_cost_none_values_treated_as_zero():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    cost = pricing.estimate_cost({"in": None, "out": None}, "claude-opus-4.8")
    assert cost == pytest.approx(0.0)


def test_estimate_cost_empty_dict():
    _seed_memo(pricing._parse_feed(FAKE_FEED))
    assert pricing.estimate_cost({}, "claude-opus-4.8") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _table(): disk-cache / TTL / fetch path (all network mocked)
# ---------------------------------------------------------------------------


def test_table_fetches_and_writes_disk_cache(cache_dir, monkeypatch):
    raw = json.dumps(FAKE_FEED).encode()
    spy = []
    _mock_urlopen(monkeypatch, raw=raw, spy=spy)

    table = pricing._table()

    assert "claudeopus48" in table
    assert len(spy) == 1  # network was hit once
    # The raw payload was persisted to the redirected cache path.
    cache_file = cache_dir / "pricing.json"
    assert cache_file.exists()
    assert json.loads(cache_file.read_text())["models"][0]["id"] == "claude-opus-4.8"


def test_fetch_disk_cache_written_atomically_no_tmp_litter(cache_dir, monkeypatch):
    # The disk cache is written to a unique tmp file then os.replace'd into
    # place, so a concurrent reader/writer never sees a half-written file —
    # and no tmp litter is left behind.
    raw = json.dumps(FAKE_FEED).encode()
    _mock_urlopen(monkeypatch, raw=raw)
    pricing._table()
    names = [p.name for p in cache_dir.iterdir()]
    assert "pricing.json" in names
    assert not [n for n in names if n.endswith(".tmp")]


def test_table_memoized_within_ttl_no_second_fetch(cache_dir, monkeypatch):
    raw = json.dumps(FAKE_FEED).encode()
    spy = []
    _mock_urlopen(monkeypatch, raw=raw, spy=spy)

    first = pricing._table()
    second = pricing._table()

    assert first is second  # same memoized object
    assert len(spy) == 1  # only one network call despite two _table() calls


def test_table_prefers_fresh_disk_cache_over_network(cache_dir, monkeypatch):
    # Pre-populate a fresh disk cache; network should never be called.
    (cache_dir / "pricing.json").write_text(json.dumps(FAKE_FEED))

    def boom(req, timeout=None):
        raise AssertionError("network must not be hit when disk cache is fresh")

    monkeypatch.setattr(pricing.urllib.request, "urlopen", boom)

    table = pricing._table()
    assert "claudeopus48" in table


def test_table_stale_disk_triggers_fetch(cache_dir, monkeypatch):
    import os

    cache_file = cache_dir / "pricing.json"
    cache_file.write_text(
        json.dumps({"models": [{"id": "old-model", "pricing": {"inputPerM": 1}}]})
    )
    # Backdate mtime beyond TTL so disk is considered stale.
    old = time.time() - pricing._TTL - 100
    os.utime(cache_file, (old, old))

    raw = json.dumps(FAKE_FEED).encode()
    spy = []
    _mock_urlopen(monkeypatch, raw=raw, spy=spy)

    table = pricing._table()
    assert "claudeopus48" in table  # fresh feed, not the stale "oldmodel"
    assert len(spy) == 1


def test_table_fetch_failure_falls_back_to_stale_disk(cache_dir, monkeypatch):
    import os

    cache_file = cache_dir / "pricing.json"
    cache_file.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "old-model", "context": 42, "pricing": {"inputPerM": 1}}
                ]
            }
        )
    )
    old = time.time() - pricing._TTL - 100
    os.utime(cache_file, (old, old))

    # Network fails -> should degrade to the stale disk cache.
    _mock_urlopen(monkeypatch, exc=OSError("no net"))

    table = pricing._table()
    assert "oldmodel" in table
    assert table["oldmodel"]["ctx"] == 42


def test_table_fetch_failure_no_disk_returns_empty(cache_dir, monkeypatch):
    _mock_urlopen(monkeypatch, exc=OSError("no net"))
    table = pricing._table()
    assert table == {}


def test_table_empty_feed_treated_as_failure(cache_dir, monkeypatch):
    # Feed with no usable models -> _fetch returns None -> empty table (no disk).
    _mock_urlopen(monkeypatch, raw=json.dumps({"models": []}).encode())
    assert pricing._table() == {}


def test_table_bad_json_degrades_quietly(cache_dir, monkeypatch):
    _mock_urlopen(monkeypatch, raw=b"not json")
    assert pricing._table() == {}


def test_end_to_end_price_via_fetch(cache_dir, monkeypatch):
    """price_per_token drives _lookup -> _table -> _fetch with mocked network."""
    raw = json.dumps(FAKE_FEED).encode()
    _mock_urlopen(monkeypatch, raw=raw)
    p = pricing.price_per_token("claude-opus-4-8-20260101")
    assert p["in"] == pytest.approx(5.0 / 1e6)
    assert p["cache_write"] == pytest.approx(5.0 * 1.25 / 1e6)


def test_fetch_uses_endpoint_and_timeout(cache_dir, monkeypatch):
    raw = json.dumps(FAKE_FEED).encode()
    spy = []
    _mock_urlopen(monkeypatch, raw=raw, spy=spy)
    pricing._table()
    req, timeout = spy[0]
    assert req.full_url == pricing._ENDPOINT
    assert timeout == pricing._FETCH_TIMEOUT


def test_fetch_returns_table_even_when_disk_replace_fails(cache_dir, monkeypatch):
    # The parsed table is returned regardless of the best-effort disk write: if
    # os.replace fails the tmp file is cleaned up and no litter remains, but the
    # in-memory table is still handed back.
    raw = json.dumps(FAKE_FEED).encode()
    _mock_urlopen(monkeypatch, raw=raw)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(pricing.os, "replace", boom)
    table = pricing._fetch()
    assert "claudeopus48" in table  # table returned despite the failed write
    assert not [p for p in cache_dir.iterdir() if p.name.endswith(".tmp")]


def test_fetch_returns_table_when_makedirs_fails(cache_dir, monkeypatch):
    # An OSError before the write even starts (makedirs) is swallowed; the table
    # is still returned.
    raw = json.dumps(FAKE_FEED).encode()
    _mock_urlopen(monkeypatch, raw=raw)

    def boom(*a, **k):
        raise OSError("cannot mkdir")

    monkeypatch.setattr(pricing.os, "makedirs", boom)
    table = pricing._fetch()
    assert "claudesonnet45" in table


def test_load_disk_empty_models_returns_none(cache_dir):
    # A cached file that parses to an empty table is treated as no cache.
    (cache_dir / "pricing.json").write_text(json.dumps({"models": []}))
    assert pricing._load_disk() is None
