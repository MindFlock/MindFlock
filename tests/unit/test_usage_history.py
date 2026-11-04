"""Hermetic tests for ``backend.providers.usage_history``.

These exercise the rolling-window scan over Claude Code transcripts plus the
durable on-disk ledger and pruned-day tail-fill. Everything is confined to
``tmp_path``: HOME / CLAUDE_CONFIG_DIR / MINDFLOCK_ASSISTANT_DIR are monkeypatched,
``pricing.price_per_token`` is replaced with a fixed rate table, and no network
or subprocess is ever touched.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

from backend.providers import pricing
from backend.providers import usage_history as uh

# Fixed, easy-to-multiply per-token rates so cost assertions are exact.
_RATES = {
    "in": 1.0,
    "out": 10.0,
    "cache_read": 0.1,
    "cache_write": 2.0,
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point every root/ledger dir at tmp_path, pin pricing, and clear the cache."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assistant = tmp_path / "assistant"
    monkeypatch.setenv("MINDFLOCK_ASSISTANT_DIR", str(assistant))

    # Fixed rates so cost == in*1 + out*10 + cache_read*0.1 + cache_write*2.
    monkeypatch.setattr(pricing, "price_per_token", lambda model: dict(_RATES))

    # The module memoizes results for ~60s; reset before and after each test so
    # each test recomputes from its own fixture files.
    monkeypatch.setattr(uh, "_cache", {"at": 0.0, "windows": None})

    yield home


def _reset_cache(monkeypatch):
    monkeypatch.setattr(uh, "_cache", {"at": 0.0, "windows": None})


def _write_transcript(home, project, filename, entries):
    """Write ``entries`` as JSONL lines under ``<home>/.claude/projects/<project>/``."""
    pdir = home / ".claude" / "projects" / project
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / filename
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _iso(epoch):
    """UTC ISO timestamp with trailing Z, matching Claude Code transcript style."""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _entry(epoch, *, model="claude-sonnet-4-5-20250101", inp=0, out=0, cr=0, cw=0):
    return {
        "timestamp": _iso(epoch),
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
        },
    }


# --------------------------------------------------------------------------- #
# _ts_epoch / _roots basics
# --------------------------------------------------------------------------- #


def test_ts_epoch_parses_z_suffix():
    epoch = 1_700_000_000
    parsed = uh._ts_epoch(_iso(epoch))
    assert parsed == pytest.approx(epoch, abs=1)


def test_ts_epoch_bad_input_returns_none():
    assert uh._ts_epoch("") is None
    assert uh._ts_epoch(None) is None
    assert uh._ts_epoch("not-a-timestamp") is None


def test_roots_finds_dotclaude_dirs(_isolate):
    home = _isolate
    (home / ".claude").mkdir()
    (home / ".claude-alt").mkdir()
    (home / ".config").mkdir()  # not a .claude dir
    roots = uh._roots()
    assert str(home / ".claude") in roots
    assert str(home / ".claude-alt") in roots
    assert str(home / ".config") not in roots


def test_roots_includes_claude_config_dir(_isolate, monkeypatch, tmp_path):
    cfg = tmp_path / "explicit-cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert str(cfg) in uh._roots()


# --------------------------------------------------------------------------- #
# windows(): shape + rolling-window bucketing
# --------------------------------------------------------------------------- #


def test_windows_empty_returns_zeroed_shape(_isolate):
    (_isolate / ".claude").mkdir()
    w = uh.windows()
    assert set(w) == {"day", "week", "month", "year"}
    for k in w:
        assert w[k] == {
            "in": 0,
            "out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cost": 0.0,
        }


def test_windows_single_recent_turn_all_windows(_isolate):
    home = _isolate
    now = time.time()
    _write_transcript(
        home,
        "proj",
        "a.jsonl",
        [_entry(now - 60, inp=100, out=20, cr=1000, cw=50)],
    )
    w = uh.windows()
    # cost = 100*1 + 20*10 + 1000*0.1 + 50*2 = 100 + 200 + 100 + 100 = 500
    expected = {
        "in": 100,
        "out": 20,
        "cache_read": 1000,
        "cache_write": 50,
        "cost": 500.0,
    }
    for k in ("day", "week", "month", "year"):
        assert w[k] == expected


def test_windows_bucket_by_timestamp(_isolate, monkeypatch):
    """A turn 3 days old lands in week/month/year but NOT in the 24h window."""
    home = _isolate
    now = time.time()
    _write_transcript(
        home,
        "proj",
        "recent.jsonl",
        [_entry(now - 3600, inp=10, out=1)],  # 1h ago -> all windows
    )
    _write_transcript(
        home,
        "proj",
        "threedays.jsonl",
        [_entry(now - 3 * 86400, inp=100, out=5)],  # 3d ago -> week+ only
    )
    _write_transcript(
        home,
        "proj",
        "old.jsonl",
        [_entry(now - 100 * 86400, inp=1000, out=7)],  # 100d ago -> year only
    )
    w = uh.windows()

    # day window: only the 1h-ago turn
    assert w["day"]["in"] == 10
    assert w["day"]["out"] == 1
    # week window: 1h + 3d turns
    assert w["week"]["in"] == 110
    assert w["week"]["out"] == 6
    # month window: same as week (3d and 1h both < 30d, 100d excluded)
    assert w["month"]["in"] == 110
    # year window: all three
    assert w["year"]["in"] == 1110
    assert w["year"]["out"] == 13


def test_windows_cost_uses_fixed_rates(_isolate):
    home = _isolate
    now = time.time()
    _write_transcript(
        home,
        "proj",
        "a.jsonl",
        [_entry(now - 10, inp=3, out=4, cr=5, cw=6)],
    )
    w = uh.windows()
    # 3*1 + 4*10 + 5*0.1 + 6*2 = 3 + 40 + 0.5 + 12 = 55.5
    assert w["day"]["cost"] == pytest.approx(55.5)


def test_windows_skips_zero_token_and_no_usage_lines(_isolate):
    home = _isolate
    now = time.time()
    entries = [
        {
            "timestamp": _iso(now - 10),
            "message": {
                "model": "m",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
        {"timestamp": _iso(now - 10), "type": "user", "message": {"content": "hi"}},
        _entry(now - 10, inp=5),
    ]
    _write_transcript(home, "proj", "a.jsonl", entries)
    w = uh.windows()
    assert w["day"]["in"] == 5


def test_windows_ignores_entries_without_timestamp(_isolate):
    home = _isolate
    e = _entry(time.time() - 10, inp=42)
    del e["timestamp"]
    _write_transcript(home, "proj", "a.jsonl", [e])
    w = uh.windows()
    assert w["day"]["in"] == 0


def test_windows_reads_top_level_usage_and_model(_isolate):
    """Some transcript lines carry usage/model at top level, not under message."""
    home = _isolate
    now = time.time()
    e = {
        "timestamp": _iso(now - 10),
        "model": "claude-opus-4-8",
        "usage": {
            "input_tokens": 7,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    _write_transcript(home, "proj", "a.jsonl", [e])
    w = uh.windows()
    assert w["day"]["in"] == 7


def test_windows_survives_malformed_json_lines(_isolate):
    home = _isolate
    now = time.time()
    pdir = home / ".claude" / "projects" / "proj"
    pdir.mkdir(parents=True)
    with open(pdir / "a.jsonl", "w") as f:
        f.write('{"usage": broken json\n')  # invalid but contains "usage"
        f.write("not even close\n")
        f.write(json.dumps(_entry(now - 10, inp=9)) + "\n")
    w = uh.windows()
    assert w["day"]["in"] == 9


def test_windows_scans_multiple_roots_and_projects(_isolate):
    home = _isolate
    now = time.time()
    _write_transcript(home, "projA", "a.jsonl", [_entry(now - 10, inp=1)])
    _write_transcript(home, "projB", "b.jsonl", [_entry(now - 10, inp=2)])
    # a second .claude* root
    alt = home / ".claude-work" / "projects" / "projC"
    alt.mkdir(parents=True)
    with open(alt / "c.jsonl", "w") as f:
        f.write(json.dumps(_entry(now - 10, inp=4)) + "\n")
    w = uh.windows()
    assert w["day"]["in"] == 7


# --------------------------------------------------------------------------- #
# Ledger: written + merged
# --------------------------------------------------------------------------- #


def test_ledger_written_after_scan(_isolate):
    home = _isolate
    now = time.time()
    _write_transcript(home, "proj", "a.jsonl", [_entry(now - 10, inp=11, out=2)])
    uh.windows()

    ledger_path = uh._ledger_path()
    assert os.path.isfile(ledger_path)
    with open(ledger_path) as f:
        doc = json.load(f)
    assert isinstance(doc["days"], dict)
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    assert doc["days"][today]["in"] == 11
    assert doc["days"][today]["out"] == 2
    assert "updated" in doc


def test_ledger_preexisting_days_are_preserved_on_merge(_isolate, monkeypatch):
    """An in-horizon ledger day the scan doesn't touch survives; days beyond
    the longest rolling window (+ margin) are pruned so the ledger stays
    bounded; scanned days are overwritten."""
    home = _isolate
    now = time.time()

    # Seed a ledger with an untouched-but-recent day (must survive) and an
    # ancient day beyond every window (must be pruned).
    recent_untouched = time.strftime("%Y-%m-%d", time.localtime(now - 100 * 86400))
    ancient = "2000-01-01"
    day = {"in": 1, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 3.0}
    seed = {"days": {recent_untouched: dict(day), ancient: dict(day)}}
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(seed, f)

    _write_transcript(home, "proj", "a.jsonl", [_entry(now - 10, inp=5)])
    uh.windows()

    with open(uh._ledger_path()) as f:
        doc = json.load(f)
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    assert doc["days"][recent_untouched]["in"] == 1  # preserved (in horizon)
    assert ancient not in doc["days"]  # pruned (beyond horizon)
    assert doc["days"][today]["in"] == 5  # freshly added


def test_ledger_scanned_day_overwrites_stale_ledger_value(_isolate):
    """A v1 (pre-dedupe) ledger day the transcripts still cover is recomputed
    from the scan, not kept (see the v2 migration tests below)."""
    home = _isolate
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))

    seed = {
        "days": {
            today: {
                "in": 999,
                "out": 999,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 9999.0,
            }
        }
    }
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(seed, f)

    _write_transcript(home, "proj", "a.jsonl", [_entry(now - 10, inp=5, out=1)])
    uh.windows()

    with open(uh._ledger_path()) as f:
        doc = json.load(f)
    assert doc["days"][today]["in"] == 5  # overwritten, not 999
    assert doc["days"][today]["out"] == 1


def test_save_ledger_uses_unique_tmp_per_write(_isolate, monkeypatch):
    """Two writers (engine + web server) must never share a tmp name — a fixed
    ``path + ".tmp"`` lets their writes interleave and rename a corrupt file
    into place. Each write gets its own tmp, atomically replaced, no litter."""
    tmps = []
    real_replace = os.replace

    def spy_replace(src, dst):
        tmps.append(src)
        real_replace(src, dst)

    monkeypatch.setattr(uh.os, "replace", spy_replace)
    uh._save_ledger({"days": {"2026-07-20": {"in": 1}}})
    uh._save_ledger({"days": {"2026-07-20": {"in": 2}}})

    assert len(tmps) == 2
    assert tmps[0] != tmps[1]  # unique per write, not a shared fixed name
    for t in tmps:
        assert t.startswith(uh._ledger_path() + ".") and t.endswith(".tmp")
    with open(uh._ledger_path()) as f:
        assert json.load(f)["days"]["2026-07-20"]["in"] == 2
    d = os.path.dirname(uh._ledger_path())
    assert not [n for n in os.listdir(d) if n.endswith(".tmp")]  # no litter


def test_save_ledger_cleans_up_tmp_on_failure(_isolate, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(uh.os, "replace", boom)
    uh._save_ledger({"days": {}})  # best-effort: must not raise
    d = os.path.dirname(uh._ledger_path())
    assert not [n for n in os.listdir(d) if n.endswith(".tmp")]


def test_load_ledger_tolerates_corrupt_file(_isolate):
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        f.write("{ this is not valid json")
    assert uh._load_ledger() == {"days": {}}


def test_load_ledger_missing_file(_isolate):
    assert uh._load_ledger() == {"days": {}}


# --------------------------------------------------------------------------- #
# Pruned-day tail-fill
# --------------------------------------------------------------------------- #


def test_pruned_day_tail_fill(_isolate, monkeypatch):
    """A ledger day older than the earliest scanned day back-fills the windows.

    Simulates Claude having pruned an old transcript: the day only lives in the
    ledger. Since it's older than the earliest scanned day, ``_compute`` folds it
    into any window it still falls inside (by day granularity).
    """
    home = _isolate
    now = time.time()

    # Scanned transcript: a turn from "yesterday" so earliest_day is recent.
    _write_transcript(home, "proj", "a.jsonl", [_entry(now - 86400, inp=10, out=0)])

    # Ledger-only pruned day 100 days ago (older than earliest scanned day).
    pruned_epoch = now - 100 * 86400
    pruned_day = time.strftime("%Y-%m-%d", time.localtime(pruned_epoch))
    seed = {
        "days": {
            pruned_day: {
                "in": 500,
                "out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 42.0,
            }
        }
    }
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(seed, f)

    w = uh.windows()
    # Pruned day is 100d old -> only inside the year window, back-filled.
    # The scanned turn (1d old) is in month/week/year for sure; the pruned day
    # adds its ledger totals only to the year window.
    assert w["year"]["in"] == 10 + 500
    assert w["month"]["in"] == 10  # pruned day (100d) excluded from month
    assert w["week"]["in"] == 10
    # year cost = scanned turn cost (10*1) + pruned ledger cost (42).
    assert w["year"]["cost"] == pytest.approx(10 * _RATES["in"] + 42.0)
    # The pruned day's tokens never appear in month/week (only the scanned turn).
    assert w["month"]["cost"] == pytest.approx(10 * _RATES["in"])


def test_tail_fill_skips_days_not_older_than_earliest(_isolate):
    """v1 ledger days >= the earliest scanned day are dropped by the migration
    and recomputed — never added on top of the scan."""
    home = _isolate
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))

    _write_transcript(home, "proj", "a.jsonl", [_entry(now - 10, inp=10)])

    # Ledger has today with a big number; because today >= earliest_day, tail-fill
    # must NOT add it again on top of the scanned value.
    seed = {
        "days": {
            today: {"in": 777, "out": 0, "cache_read": 0, "cache_write": 0, "cost": 1.0}
        }
    }
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(seed, f)

    w = uh.windows()
    assert w["day"]["in"] == 10  # scanned value only, not 10+777


def test_tail_fill_when_no_transcripts_scanned(_isolate):
    """With no transcripts, earliest_day is None so all ledger days back-fill."""
    home = _isolate
    (home / ".claude").mkdir()
    now = time.time()

    recent_day = time.strftime("%Y-%m-%d", time.localtime(now - 2 * 86400))
    old_day = time.strftime("%Y-%m-%d", time.localtime(now - 200 * 86400))
    seed = {
        "days": {
            recent_day: {
                "in": 3,
                "out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 1.0,
            },
            old_day: {
                "in": 9,
                "out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 2.0,
            },
        }
    }
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(seed, f)

    w = uh.windows()
    # recent_day (2d) is in week/month/year; old_day (200d) only in year.
    assert w["week"]["in"] == 3
    assert w["month"]["in"] == 3
    assert w["year"]["in"] == 12
    assert w["day"]["in"] == 0  # 2d ago is outside the 24h window


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


def test_windows_result_is_cached(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    _write_transcript(home, "proj", "a.jsonl", [_entry(now - 10, inp=5)])
    first = uh.windows()
    assert first["day"]["in"] == 5

    # Add more usage; without a cache reset the same cached object comes back.
    _write_transcript(home, "proj", "b.jsonl", [_entry(now - 10, inp=100)])
    second = uh.windows()
    assert second is first
    assert second["day"]["in"] == 5  # stale, from cache

    # After clearing the cache we see the new total.
    _reset_cache(monkeypatch)
    third = uh.windows()
    assert third["day"]["in"] == 105


def test_windows_never_raises_on_compute_error(_isolate, monkeypatch):
    """If _compute blows up, windows() degrades to a zeroed result, not an exception."""

    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(uh, "_compute", boom)
    _reset_cache(monkeypatch)
    w = uh.windows()
    assert set(w) == {"day", "week", "month", "year"}
    for k in w:
        assert w[k] == {
            "in": 0,
            "out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cost": 0.0,
        }


# --------------------------------------------------------------------------- #
# Defensive scan branches
# --------------------------------------------------------------------------- #
def test_iter_transcripts_skips_non_dir_project_entry(_isolate):
    """A stray file (not a dir) sitting in projects/ is skipped, not opened."""
    home = _isolate
    base = home / ".claude" / "projects"
    base.mkdir(parents=True)
    (base / "loose-file").write_text("not a project dir")  # skipped
    # A real project alongside it still yields.
    _write_transcript(home, "realproj", "a.jsonl", [_entry(time.time() - 10, inp=3)])
    w = uh.windows()
    assert w["day"]["in"] == 3


def test_compute_survives_unreadable_transcript(_isolate):
    """A path that ends in .jsonl but is a directory raises on open() and is
    skipped (the OSError guard), while sibling transcripts still count."""
    home = _isolate
    pdir = home / ".claude" / "projects" / "proj"
    pdir.mkdir(parents=True)
    (pdir / "a-dir.jsonl").mkdir()  # yielded by the scan, but open() fails
    with open(pdir / "good.jsonl", "w") as f:
        f.write(json.dumps(_entry(time.time() - 10, inp=8)) + "\n")
    w = uh.windows()
    assert w["day"]["in"] == 8


def test_tail_fill_ignores_unparseable_ledger_day(_isolate):
    """A malformed day key in the ledger (unparseable by strptime) is skipped
    during tail-fill rather than raising."""
    home = _isolate
    now = time.time()
    (home / ".claude").mkdir()  # no transcripts -> earliest_day is None
    # A current-year but impossible month/day: recent enough to survive the
    # horizon prune, yet strptime rejects it -> the tail-fill parse guard fires.
    bad_day = time.strftime("%Y", time.localtime(now)) + "-99-99"
    seed = {
        "days": {
            bad_day: {
                "in": 500,
                "out": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 9.0,
            }
        }
    }
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(seed, f)
    w = uh.windows()  # must not raise
    for k in w:
        assert w[k]["in"] == 0  # the bad day contributed nothing


# --------------------------------------------------------------------------- #
# current_window: rolling-window anchoring from the recent-turn list
# --------------------------------------------------------------------------- #
def _prime_recent(monkeypatch, recent):
    """Seed the module cache with a fresh recent list so current_window reads it
    without recomputing (windows non-None + at=now keeps _refresh a no-op)."""
    monkeypatch.setattr(
        uh,
        "_cache",
        {"at": time.time(), "windows": {}, "recent": list(recent)},
    )


def _tok(total):
    # A tok dict whose values sum to `total` (all in "in").
    return {"in": total, "out": 0, "cache_read": 0, "cache_write": 0}


def test_current_window_zero_hours_returns_none(_isolate):
    assert uh.current_window(0) is None
    assert uh.current_window(-1) is None


def test_current_window_none_when_no_recent_turns(_isolate, monkeypatch):
    _prime_recent(monkeypatch, [])
    assert uh.current_window(5.0) is None


def test_current_window_anchors_and_sums(_isolate, monkeypatch):
    now = time.time()
    # Two turns 10 min apart, both inside a 5h window anchored on the first.
    recent = [
        (now - 600, 1.5, _tok(100)),
        (now - 60, 2.5, _tok(40)),
    ]
    _prime_recent(monkeypatch, recent)
    win = uh.current_window(5.0)
    assert win is not None
    assert win["anchor"] == now - 600
    assert win["end"] == pytest.approx(now - 600 + 5 * 3600)
    assert win["cost"] == pytest.approx(4.0)  # 1.5 + 2.5
    assert win["tokens"] == 140  # 100 + 40


def test_current_window_idle_past_window_returns_none(_isolate, monkeypatch):
    now = time.time()
    # The only turn is 6h old; a 5h window anchored there has already closed.
    _prime_recent(monkeypatch, [(now - 6 * 3600, 1.0, _tok(10))])
    assert uh.current_window(5.0) is None


def test_current_window_reanchors_after_a_gap(_isolate, monkeypatch):
    now = time.time()
    # An old turn, then a gap longer than the window, then a recent turn: the
    # window must re-anchor on the recent turn and count ONLY it.
    recent = [
        (now - 10 * 3600, 9.0, _tok(999)),  # before the gap; excluded
        (now - 120, 2.0, _tok(30)),  # re-anchors here
        (now - 30, 3.0, _tok(20)),
    ]
    _prime_recent(monkeypatch, recent)
    win = uh.current_window(1.0)  # 1h window
    assert win["anchor"] == now - 120
    assert win["tokens"] == 50  # 30 + 20 only, the 999 turn is pre-gap
    assert win["cost"] == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# Duplicate lines: one API response, several transcript lines
# --------------------------------------------------------------------------- #
def _api_line(epoch, mid, rid, *, inp=0, out=0, cr=0, cw=0, model="m"):
    """One transcript line of an API response: Claude Code writes one per
    content block, each repeating the response's ``message.usage``."""
    e = _entry(epoch, model=model, inp=inp, out=out, cr=cr, cw=cw)
    e["message"]["id"] = mid
    e["requestId"] = rid
    return e


def test_content_block_lines_of_one_response_count_once(_isolate):
    """thinking/text/tool_use lines share (message.id, requestId): one turn.
    When the copies differ, the one with the most output (the final block)
    wins."""
    home = _isolate
    now = time.time()
    _write_transcript(
        home,
        "proj",
        "a.jsonl",
        [
            _api_line(now - 30, "msg_1", "req_1", inp=10, out=1, cr=100),
            _api_line(now - 30, "msg_1", "req_1", inp=10, out=1, cr=100),
            _api_line(now - 29, "msg_1", "req_1", inp=10, out=7, cr=100),
            _api_line(now - 20, "msg_2", "req_2", inp=5, out=2),
        ],
    )
    w = uh.windows()
    assert w["day"]["in"] == 15
    assert w["day"]["out"] == 9  # 7 (final block of msg_1) + 2
    assert w["day"]["cache_read"] == 100
    assert w["day"]["cost"] == pytest.approx(15 + 9 * 10 + 100 * 0.1)


def test_turns_copied_into_a_resumed_conversation_count_once(_isolate):
    """A resumed/forked conversation copies earlier lines into a NEW file;
    the copies keep their (message.id, requestId) and must not double up."""
    home = _isolate
    now = time.time()
    orig = [_api_line(now - 300, "msg_1", "req_1", inp=10, out=4)]
    _write_transcript(home, "proj", "orig.jsonl", orig)
    _write_transcript(
        home,
        "proj",
        "fork.jsonl",
        orig + [_api_line(now - 10, "msg_2", "req_2", inp=3, out=1)],
    )
    w = uh.windows()
    assert w["day"]["in"] == 13
    assert w["day"]["out"] == 5


def test_identical_lines_without_message_id_fall_back_to_the_raw_line(_isolate):
    home = _isolate
    now = time.time()
    line = _entry(now - 10, inp=4)
    _write_transcript(home, "proj", "a.jsonl", [line, line, _entry(now - 5, inp=1)])
    assert uh.windows()["day"]["in"] == 5


def test_subagent_transcripts_are_counted(_isolate):
    """Agent-tool subagents write <proj>/<conversation>/subagents/**.jsonl —
    the old scan only listed <proj>/*.jsonl and missed them all."""
    home = _isolate
    now = time.time()
    _write_transcript(
        home, "proj", "conv1.jsonl", [_api_line(now - 50, "m1", "r1", inp=1)]
    )
    _write_transcript(
        home,
        "proj/conv1/subagents",
        "agent-a.jsonl",
        [_api_line(now - 40, "m2", "r2", inp=20)],
    )
    _write_transcript(
        home,
        "proj/conv1/subagents/workflows/wf_1",
        "agent-b.jsonl",
        [_api_line(now - 30, "m3", "r3", inp=300)],
    )
    assert uh.windows()["day"]["in"] == 321


def test_unchanged_files_are_not_reparsed(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    _write_transcript(home, "proj", "a.jsonl", [_api_line(now - 10, "m1", "r1", inp=2)])
    calls = []
    real = uh._parse_turns
    monkeypatch.setattr(uh, "_parse_turns", lambda p: calls.append(p) or real(p))
    assert uh.windows()["day"]["in"] == 2
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 2
    assert len(calls) == 1  # second scan served from the (mtime, size) memo


def _append(path, entries, *, newline=True):
    with open(path, "a") as f:
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            f.write(json.dumps(e) + ("\n" if newline or not last else ""))


def _forget_process_memo(monkeypatch):
    """What a server restart loses: the in-memory memo and the 60s cache."""
    monkeypatch.setattr(uh, "_FILE_MEMO", {})
    monkeypatch.setattr(
        uh, "_memo_state", {"path": None, "dirty": False, "saved_at": 0.0}
    )
    _reset_cache(monkeypatch)


def test_a_grown_transcript_is_read_from_where_the_last_read_stopped(
    _isolate, monkeypatch
):
    home = _isolate
    now = time.time()
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(now - 50, "m1", "r1", inp=2)]
    )
    full = []
    real = uh._parse_turns
    monkeypatch.setattr(uh, "_parse_turns", lambda p: full.append(p) or real(p))
    starts = []
    real_scan = uh._scan
    monkeypatch.setattr(
        uh, "_scan", lambda p, s, t, f: starts.append(s) or real_scan(p, s, t, f)
    )
    assert uh.windows()["day"]["in"] == 2
    size = a.stat().st_size
    # A later content block of the SAME response plus a new response.
    _append(
        a,
        [
            _api_line(now - 49, "m1", "r1", inp=2, out=5),
            _api_line(now - 40, "m2", "r2", inp=3),
        ],
    )
    _reset_cache(monkeypatch)
    w = uh.windows()
    assert w["day"]["in"] == 5  # m1 counted once, m2 added
    assert w["day"]["out"] == 5  # m1's final block won
    assert len(full) == 1  # the growth was not a full re-read
    assert starts[-1] == size


def test_a_line_still_being_written_is_read_again_when_it_completes(
    _isolate, monkeypatch
):
    home = _isolate
    now = time.time()
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(now - 50, "m1", "r1", inp=2)]
    )
    line = json.dumps(_api_line(now - 40, "m2", "r2", inp=3))
    with open(a, "a") as f:
        f.write(line[:20])  # half a line: not JSON yet
    assert uh.windows()["day"]["in"] == 2
    with open(a, "a") as f:
        f.write(line[20:] + "\n")
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 5


def test_a_restart_reads_only_what_changed_since_the_saved_memo(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    _write_transcript(
        home, "proj", "old.jsonl", [_api_line(now - 90, "m1", "r1", inp=2)]
    )
    b = _write_transcript(
        home, "proj", "live.jsonl", [_api_line(now - 80, "m2", "r2", inp=3)]
    )
    assert uh.windows()["day"]["in"] == 5
    assert os.path.isfile(uh._memo_path())  # the first scan saves at once
    _forget_process_memo(monkeypatch)
    size = b.stat().st_size
    _append(b, [_api_line(now - 70, "m3", "r3", inp=4)])
    full = []
    real = uh._parse_turns
    monkeypatch.setattr(uh, "_parse_turns", lambda p: full.append(p) or real(p))
    starts = []
    real_scan = uh._scan
    monkeypatch.setattr(
        uh, "_scan", lambda p, s, t, f: starts.append(s) or real_scan(p, s, t, f)
    )
    assert uh.windows()["day"]["in"] == 9
    assert full == []  # neither file was read from the top
    assert starts == [size]  # only the live file's new bytes


def test_a_rewritten_transcript_is_read_again_in_full(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    a = _write_transcript(
        home,
        "proj",
        "a.jsonl",
        [
            _api_line(now - 50, "m1", "r1", inp=2),
            _api_line(now - 40, "m2", "r2", inp=3),
        ],
    )
    assert uh.windows()["day"]["in"] == 5
    # Shorter content in place (same inode, smaller): never an append.
    with open(a, "w") as f:
        f.write(json.dumps(_api_line(now - 30, "m9", "r9", inp=7)) + "\n")
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 7


def test_a_corrupt_saved_memo_is_ignored(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    _write_transcript(home, "proj", "a.jsonl", [_api_line(now - 50, "m1", "r1", inp=2)])
    assert uh.windows()["day"]["in"] == 2
    with open(uh._memo_path(), "w") as f:
        f.write('{"version": 1, "files": {"x": [1, 2]}')  # truncated
    _forget_process_memo(monkeypatch)
    assert uh.windows()["day"]["in"] == 2


def test_lines_without_a_message_id_dedupe_across_processes(_isolate, monkeypatch):
    """The fallback key outlives the process in the saved memo, so it must not
    be Python's per-process salted ``hash()``."""
    line = json.dumps(_entry(time.time() - 10, inp=1))
    assert uh._line_key(line) == uh._line_key(line + "\n")
    assert (
        uh._line_key(line)
        == "\x01"
        + __import__("hashlib").blake2b(line.encode(), digest_size=8).hexdigest()
    )


def test_line_key_ignores_surrounding_whitespace_and_tells_lines_apart():
    line = json.dumps(_entry(1_700_000_000, inp=1))
    key = uh._line_key(line)
    for variant in (line + "\r\n", line + "  \n", "\t" + line + " ", line):
        assert uh._line_key(variant) == key
    other = json.dumps(_entry(1_700_000_000, inp=2))
    assert uh._line_key(other) != key
    assert key.startswith("\x01") and len(key) == 17  # \x01 + 16 hex digits


class _Clock:
    """``uh.time`` with a settable ``time()`` (strftime/localtime pass through)."""

    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


def _clock(monkeypatch):
    clock = _Clock(time.time())
    monkeypatch.setattr(uh, "time", clock)
    return clock


def _saved_memo():
    with open(uh._memo_path()) as f:
        return json.load(f)


def _record_scan_starts(monkeypatch):
    starts = []
    real_scan = uh._scan
    monkeypatch.setattr(
        uh, "_scan", lambda p, s, t, f: starts.append(s) or real_scan(p, s, t, f)
    )
    return starts


def test_a_transcript_truncated_in_place_is_read_again_from_the_top(
    _isolate, monkeypatch
):
    """Same inode, fewer bytes: not an append — the old turns must go."""
    home = _isolate
    now = time.time()
    a = _write_transcript(
        home,
        "proj",
        "a.jsonl",
        [
            _api_line(now - 50, "m1", "r1", inp=2),
            _api_line(now - 40, "m2", "r2", inp=3),
            _api_line(now - 30, "m3", "r3", inp=4),
        ],
    )
    ino = a.stat().st_ino
    assert uh.windows()["day"]["in"] == 9
    starts = _record_scan_starts(monkeypatch)
    with open(a, "r+") as f:
        f.truncate(0)
        f.write(json.dumps(_api_line(now - 20, "m8", "r8", inp=1)) + "\n")
    assert a.stat().st_ino == ino
    # The ledger keeps today's 9 by design (a day never shrinks); without it
    # the scan alone must see only what the file now holds.
    os.unlink(uh._ledger_path())
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 1  # m1..m3 are not kept
    assert starts == [0]
    turns, _first = uh.read_turns(str(a))
    assert [t[0] for t in turns] == ["m8\x00r8"]


def test_a_saved_memo_of_another_version_is_ignored_and_rewritten(
    _isolate, monkeypatch
):
    home = _isolate
    now = time.time()
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(now - 50, "m1", "r1", inp=2)]
    )
    st = a.stat()
    # Matches the file's signature exactly, but claims far more usage: were it
    # trusted, the day would read 1000.
    bogus = [
        st.st_mtime_ns,
        st.st_size,
        st.st_ino,
        st.st_size,
        now - 50,
        [["m1\x00r1", now - 50, 1000, 0, 0, 0, "m"]],
    ]
    os.makedirs(os.path.dirname(uh._memo_path()), exist_ok=True)
    with open(uh._memo_path(), "w") as f:
        json.dump({"version": uh.MEMO_VERSION + 1, "files": {str(a): bogus}}, f)
    _forget_process_memo(monkeypatch)
    assert uh.windows()["day"]["in"] == 2
    doc = _saved_memo()
    assert doc["version"] == uh.MEMO_VERSION
    assert doc["files"][str(a)][5][0][2] == 2


def test_the_saved_memo_is_rewritten_at_most_every_interval(_isolate, monkeypatch):
    home = _isolate
    clock = _clock(monkeypatch)
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(clock.t - 50, "m1", "r1", inp=2)]
    )
    writes = []
    real = uh.atomic_write_json

    def counting(path, *args, **kw):
        if os.path.basename(path) == "usage-parse-cache.json":
            writes.append(path)
        return real(path, *args, **kw)

    monkeypatch.setattr(uh, "atomic_write_json", counting)
    uh._compute()
    assert len(writes) == 1  # the first scan of a process saves at once
    _append(a, [_api_line(clock.t - 40, "m2", "r2", inp=3)])
    clock.t += 10
    uh._compute()
    assert len(writes) == 1  # changed, but inside the interval
    assert uh._memo_state["dirty"] is True
    clock.t += uh._MEMO_SAVE_EVERY
    uh._compute()  # nothing new read, but the last change is still unsaved
    assert len(writes) == 2
    assert uh._memo_state["dirty"] is False
    assert len(_saved_memo()["files"][str(a)][5]) == 2  # m1 and m2
    clock.t += uh._MEMO_SAVE_EVERY * 2
    uh._compute()
    assert len(writes) == 2  # nothing changed: not rewritten


def test_a_memo_from_another_assistant_dir_is_not_reused(
    _isolate, monkeypatch, tmp_path
):
    home = _isolate
    now = time.time()
    _write_transcript(home, "proj", "a.jsonl", [_api_line(now - 50, "m1", "r1", inp=2)])
    assert uh.windows()["day"]["in"] == 2
    first = uh._memo_path()
    assert uh._memo_state["path"] == first
    # The other dir's memo has its own entry for a file this process has not
    # read yet: its (deliberately different) turns prove it was the one loaded.
    c = _write_transcript(
        home, "proj", "c.jsonl", [_api_line(now - 40, "m3", "r3", inp=1)]
    )
    st = c.stat()
    other = tmp_path / "assistant-2"
    other.mkdir()
    entry = [
        st.st_mtime_ns,
        st.st_size,
        st.st_ino,
        st.st_size,
        now - 40,
        [["m3\x00r3", now - 40, 42, 0, 0, 0, "m"]],
    ]
    (other / "usage-parse-cache.json").write_text(
        json.dumps({"version": uh.MEMO_VERSION, "files": {str(c): entry}})
    )
    monkeypatch.setenv("MINDFLOCK_ASSISTANT_DIR", str(other))
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 2 + 42
    assert uh._memo_state["path"] == str(other / "usage-parse-cache.json")
    assert uh._memo_state["path"] != first
    with open(first) as f:
        assert str(c) not in json.load(f)["files"]  # the first dir's memo untouched


def test_entries_of_deleted_transcripts_are_pruned_from_memory_and_disk(
    _isolate, monkeypatch
):
    home = _isolate
    clock = _clock(monkeypatch)
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(clock.t - 50, "m1", "r1", inp=2)]
    )
    b = _write_transcript(
        home, "proj", "b.jsonl", [_api_line(clock.t - 40, "m2", "r2", inp=3)]
    )
    assert uh.windows()["day"]["in"] == 5
    assert set(_saved_memo()["files"]) == {str(a), str(b)}
    b.unlink()
    clock.t += uh._MEMO_SAVE_EVERY + 1
    _reset_cache(monkeypatch)
    uh.windows()
    assert str(b) not in uh._FILE_MEMO and str(a) in uh._FILE_MEMO
    assert set(_saved_memo()["files"]) == {str(a)}


def test_first_ts_survives_an_incremental_read(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(now - 500, "m1", "r1", inp=2)]
    )
    _turns, first = uh.read_turns(str(a))
    assert first == pytest.approx(now - 500, abs=1)
    starts = _record_scan_starts(monkeypatch)
    size = a.stat().st_size
    _append(a, [_api_line(now - 10, "m2", "r2", inp=3)])
    turns, first = uh.read_turns(str(a))
    assert starts == [size]  # an incremental read
    assert first == pytest.approx(now - 500, abs=1)  # not the appended block's
    full_turns, full_first, _off = uh._parse_turns(str(a))
    assert sorted(turns) == sorted(full_turns) and first == full_first
    # An appended line that is (oddly) OLDER still moves the birth back.
    _append(a, [_api_line(now - 900, "m3", "r3", inp=1)])
    _turns, first = uh.read_turns(str(a))
    assert first == pytest.approx(now - 900, abs=1)


def test_a_failed_memo_save_keeps_the_previous_file_and_retries(_isolate, monkeypatch):
    home = _isolate
    clock = _clock(monkeypatch)
    a = _write_transcript(
        home, "proj", "a.jsonl", [_api_line(clock.t - 50, "m1", "r1", inp=2)]
    )
    assert uh.windows()["day"]["in"] == 2
    with open(uh._memo_path(), "rb") as f:
        before = f.read()
    real_replace = os.replace

    def failing(src, dst):
        if os.path.basename(dst) == "usage-parse-cache.json":
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing)
    _append(a, [_api_line(clock.t - 40, "m2", "r2", inp=3)])
    clock.t += uh._MEMO_SAVE_EVERY + 1
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 5  # no raise, correct totals
    with open(uh._memo_path(), "rb") as f:
        assert f.read() == before  # the old memo is intact
    leftovers = [
        n
        for n in os.listdir(os.path.dirname(uh._memo_path()))
        if n.startswith("usage-parse-cache.json.")
    ]
    assert leftovers == []  # the tmp file was cleaned up
    assert uh._memo_state["dirty"] is True  # still owed a save
    monkeypatch.setattr(os, "replace", real_replace)
    uh._compute()
    assert len(_saved_memo()["files"][str(a)][5]) == 2


# --------------------------------------------------------------------------- #
# Ledger v2: a day never shrinks because transcripts were pruned
# --------------------------------------------------------------------------- #
def _day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _midday(days_ago):
    """Noon, ``days_ago`` local days back — safely inside one calendar day."""
    t = time.localtime(time.time() - days_ago * 86400)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))


def test_partially_pruned_day_keeps_its_ledger_total(_isolate, monkeypatch):
    home = _isolate
    when = _midday(10)
    a = _write_transcript(home, "proj", "a.jsonl", [_api_line(when, "m1", "r1", inp=5)])
    b = _write_transcript(
        home, "proj", "b.jsonl", [_api_line(when + 60, "m2", "r2", inp=7)]
    )
    assert uh.windows()["month"]["in"] == 12
    b.unlink()  # Claude prunes one of the day's conversations
    _reset_cache(monkeypatch)
    w = uh.windows()
    assert w["month"]["in"] == 12  # was 5: the ledger day was overwritten downward
    assert w["month"]["cost"] == pytest.approx(12.0)
    with open(uh._ledger_path()) as f:
        assert json.load(f)["days"][_day(when)]["in"] == 12
    a.unlink()
    _reset_cache(monkeypatch)
    assert uh.windows()["month"]["in"] == 12  # fully pruned: ledger only


def test_pruned_day_newer_than_a_surviving_old_file_still_counts(_isolate, monkeypatch):
    """One surviving OLD transcript used to block back-fill for every newer
    day (back-fill only looked before the earliest scanned day)."""
    home = _isolate
    _write_transcript(
        home, "proj", "old.jsonl", [_api_line(_midday(60), "m0", "r0", inp=1)]
    )
    mid = _write_transcript(
        home, "proj", "mid.jsonl", [_api_line(_midday(10), "m1", "r1", inp=40)]
    )
    assert uh.windows()["year"]["in"] == 41
    mid.unlink()
    _reset_cache(monkeypatch)
    w = uh.windows()
    assert w["year"]["in"] == 41
    assert w["month"]["in"] == 40
    assert w["week"]["in"] == 0


def test_ledger_day_grows_with_new_turns(_isolate, monkeypatch):
    home = _isolate
    now = time.time()
    _write_transcript(
        home, "proj", "a.jsonl", [_api_line(now - 100, "m1", "r1", inp=5)]
    )
    uh.windows()
    _write_transcript(
        home,
        "proj",
        "a.jsonl",
        [
            _api_line(now - 100, "m1", "r1", inp=5),
            _api_line(now - 50, "m2", "r2", inp=6),
        ],
    )
    _reset_cache(monkeypatch)
    assert uh.windows()["day"]["in"] == 11  # no ledger delta on top of a full scan
    with open(uh._ledger_path()) as f:
        assert sum(t["in"] for t in json.load(f)["days"].values()) == 11


def test_account_split_still_sums_to_total_after_pruning(_isolate, monkeypatch):
    home = _isolate
    when = _midday(3)
    work = home / "work-root"
    monkeypatch.setattr(
        uh, "_roots", lambda: {str(home / ".claude"): "default", str(work): "work"}
    )
    _write_transcript(home, "proj", "a.jsonl", [_api_line(when, "m1", "r1", inp=5)])
    wp = work / "projects" / "proj"
    wp.mkdir(parents=True)
    (wp / "b.jsonl").write_text(json.dumps(_api_line(when, "m2", "r2", inp=9)) + "\n")
    uh.windows()
    (wp / "b.jsonl").unlink()
    _reset_cache(monkeypatch)
    w = uh.windows()
    per = uh.windows_by_account()
    assert w["week"]["in"] == 14
    assert per["work"]["week"]["in"] == 9
    assert per["default"]["week"]["in"] + per["work"]["week"]["in"] == w["week"]["in"]


# --------------------------------------------------------------------------- #
# Ledger v1 -> v2 migration
# --------------------------------------------------------------------------- #
def _seed_ledger(doc):
    os.makedirs(os.path.dirname(uh._ledger_path()), exist_ok=True)
    with open(uh._ledger_path(), "w") as f:
        json.dump(doc, f)


def _day_tot(n, cost):
    return {"in": n, "out": 0, "cache_read": 0, "cache_write": 0, "cost": cost}


def test_v1_ledger_migration(_isolate, monkeypatch):
    """v1 days the scan still covers are dropped and recomputed (they were
    inflated by duplicates / missing subagents); older v1 days are kept as
    estimates; the v1 file is backed up once."""
    home = _isolate
    scan_day = _midday(5)
    old_day = _day(_midday(100))
    covered_old = _day(_midday(40))
    _write_transcript(
        home, "proj", "old.jsonl", [_api_line(_midday(40), "m0", "r0", inp=1)]
    )
    _write_transcript(home, "proj", "a.jsonl", [_api_line(scan_day, "m1", "r1", inp=3)])
    v1 = {
        "days": {
            old_day: _day_tot(500, 50.0),
            covered_old: _day_tot(999, 99.0),
            _day(scan_day): _day_tot(888, 88.0),
        },
        "accounts": {
            "default": {
                old_day: _day_tot(500, 50.0),
                _day(scan_day): _day_tot(888, 88.0),
            }
        },
    }
    _seed_ledger(v1)

    w = uh.windows()
    assert w["month"]["in"] == 3  # the inflated v1 value is gone, not maxed in
    assert w["year"]["in"] == 3 + 1 + 500
    assert uh.windows_by_account()["default"]["year"]["in"] == 504

    with open(uh._ledger_path()) as f:
        doc = json.load(f)
    assert doc["version"] == 2
    assert doc["approx_days"] == [old_day]
    assert doc["days"][covered_old]["in"] == 1
    assert doc["days"][_day(scan_day)]["in"] == 3
    assert doc["accounts"]["default"][old_day]["in"] == 500
    backup = os.path.join(os.path.dirname(uh._ledger_path()), "usage-history.v1.json")
    with open(backup) as f:
        assert json.load(f) == v1
    expected = time.strftime("%Y-%m-%d", time.localtime(_midday(99)))
    assert uh.estimated_before() == expected

    # A second refresh neither re-migrates nor touches the backup.
    with open(backup, "w") as f:
        f.write("sentinel")
    _reset_cache(monkeypatch)
    assert uh.windows()["year"]["in"] == 504
    with open(backup) as f:
        assert f.read() == "sentinel"


def test_v1_ledger_left_alone_when_the_scan_is_empty(_isolate):
    """An empty scan can't say which v1 days are recomputable."""
    (_isolate / ".claude").mkdir()
    v1 = {"days": {_day(_midday(2)): _day_tot(7, 1.0)}}
    _seed_ledger(v1)
    assert uh.windows()["week"]["in"] == 7
    with open(uh._ledger_path()) as f:
        assert json.load(f) == v1


def test_no_estimate_note_without_v1_days(_isolate):
    home = _isolate
    _write_transcript(
        home, "proj", "a.jsonl", [_api_line(time.time() - 5, "m", "r", inp=1)]
    )
    uh.windows()
    assert uh.estimated_before() is None
    with open(uh._ledger_path()) as f:
        doc = json.load(f)
    assert doc["version"] == 2 and doc["approx_days"] == []
