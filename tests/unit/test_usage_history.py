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
    """Days present in the transcripts are authoritative over the ledger."""
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
    """Ledger days >= earliest scanned day are ignored to avoid double counting."""
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
