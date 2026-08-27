"""Codex on-disk usage + per-session telemetry (codex_usage_api).

Codex records rate-limit snapshots and cumulative token counts in rollout jsonl
files under ``$CODEX_HOME/sessions/**``. These tests build real rollout files on
disk and lock: auth-mode detection, rate-limit normalization, per-session token
summation with window bounds (including the ``start is None`` exclusion that
keeps an untimestamped rollout out of every sibling's totals), and the
rolling-window delta computation.
"""

from __future__ import annotations

import json

import pytest

from backend.providers import codex_usage_api as api


@pytest.fixture(autouse=True)
def _codex_home(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    # Reset the module caches so tests don't bleed into each other.
    api._cache = {"at": 0.0, "good_at": 0.0, "good": None}
    api._win_cache = {"at": 0.0, "windows": None}
    return home


def _write_rollout(
    home,
    *,
    sid,
    cwd,
    day="2026/07/14",
    ts="2026-07-14T10:00:00Z",
    model="gpt-5-codex",
    turns=(),
    rate_limits=None,
):
    """Write a rollout jsonl. ``turns`` is a list of (cumulative_totals, last,
    ctx_window, turn_ts) tuples; each becomes a token_count event."""
    d = home / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-{sid}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "timestamp": ts}},
        {"type": "turn_context", "payload": {"model": model}},
    ]
    for total, last, cw, tts in turns:
        info = {"total_token_usage": total, "model_context_window": cw}
        if last is not None:
            info["last_token_usage"] = last
        payload = {"type": "token_count", "info": info}
        if rate_limits is not None:
            payload["rate_limits"] = rate_limits
        lines.append({"type": "event_msg", "timestamp": tts, "payload": payload})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def _tot(inp, cached, out):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}


# --------------------------------------------------------------------------- #
# usage_mode
# --------------------------------------------------------------------------- #
def test_usage_mode_chatgpt(_codex_home):
    (_codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    assert api.usage_mode() == "windowed"


def test_usage_mode_apikey(_codex_home):
    (_codex_home / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    assert api.usage_mode() == "metered"


def test_usage_mode_openai_api_key_present(_codex_home):
    (_codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-x"})  # pragma: allowlist secret
    )
    assert api.usage_mode() == "metered"


def test_usage_mode_missing_or_corrupt(_codex_home):
    assert api.usage_mode() is None  # no auth.json
    (_codex_home / "auth.json").write_text("{not json")
    assert api.usage_mode() is None


# --------------------------------------------------------------------------- #
# rate-limit normalization / live_usage
# --------------------------------------------------------------------------- #
def test_window_from_absolute_and_relative_reset():
    assert (
        api._window_from({"used_percent": 20, "resets_at": 1784059500.0})["end"]
        == 1784059500.0
    )
    rel = api._window_from({"used_percent": 5, "resets_in_seconds": 3600})
    assert rel["end"] > 0
    assert api._window_from({}) is None


def test_normalize_rate_limits_primary_secondary_plan():
    out = api._normalize_rate_limits(
        {
            "primary": {"used_percent": 40, "resets_at": 100.0},
            "secondary": {"used_percent": 12, "resets_at": 200.0},
            "plan_type": "pro",
        }
    )
    assert out["percent_used"] == 40.0
    assert out["end"] == 100.0
    assert out["weekly"] == {"percent_used": 12.0, "end": 200.0}
    assert out["plan"] == "pro"


def test_live_usage_reads_newest_snapshot(_codex_home):
    _write_rollout(
        _codex_home,
        sid="s1",
        cwd="/tmp/x",
        turns=[(_tot(100, 0, 50), None, 200000, "2026-07-14T10:01:00Z")],
        rate_limits={"primary": {"used_percent": 33.0, "resets_at": 999.0}},
    )
    out = api.live_usage()
    assert out["percent_used"] == pytest.approx(33.0)
    assert out["end"] == 999.0


def test_live_usage_none_when_no_sessions(_codex_home):
    assert api.live_usage() is None


def test_live_usage_fetch_runs_without_holding_lock(_codex_home, monkeypatch):
    # Regression: the rollout-file scan must NOT run while the module lock is
    # held, or concurrent callers serialize behind one slow disk scan. We assert
    # the lock is released during _fetch by acquiring it from inside the fetch.
    def _f():
        acquired = api._lock.acquire(blocking=False)
        if acquired:
            api._lock.release()
        assert acquired, "lock was held across the rollout scan"
        return {"percent_used": 1.0}

    monkeypatch.setattr(api, "_fetch", _f)
    assert api.live_usage() == {"percent_used": 1.0}


def test_iter_rollouts_sort_survives_vanishing_file(_codex_home, monkeypatch):
    import os

    p_old = _write_rollout(_codex_home, sid="old", cwd="/x")
    p_new = _write_rollout(_codex_home, sid="new", cwd="/x")
    os.utime(p_old, (1000, 1000))
    os.utime(p_new, (2000, 2000))

    class _Vanished:
        # A file that disappears between the glob and the mtime sort (Codex
        # pruning a session mid-scan): stat() raises, is_file() already said yes.
        name = "rollout-ghost.jsonl"

        def is_file(self):
            return True

        def stat(self):
            raise FileNotFoundError(self.name)

    ghost = _Vanished()

    class _Root:
        def rglob(self, pat):
            return [ghost, p_new, p_old]

    monkeypatch.setattr(api, "_sessions_dir", lambda: _Root())
    got = api._iter_rollouts_newest_first()
    # Ordering survives: newest first, the vanished file demoted to the end
    # (mtime 0) — NOT the whole list returned unsorted.
    assert got == [p_new, p_old, ghost]


# --------------------------------------------------------------------------- #
# _session_totals
# --------------------------------------------------------------------------- #
def test_session_totals_real_input_subtracts_cache(_codex_home):
    p = _write_rollout(
        _codex_home,
        sid="s1",
        cwd="/tmp/x",
        model="gpt-5-codex",
        turns=[
            (_tot(1000, 400, 500), _tot(1000, 400, 500), 272000, "2026-07-14T10:01:00Z")
        ],
    )
    tot = api._session_totals(p)
    assert tot["in"] == 600  # 1000 input - 400 cached
    assert tot["cache_read"] == 400
    assert tot["out"] == 500
    assert tot["ctx"] == 1000  # last turn's full input incl. cache
    assert tot["ctx_window"] == 272000
    assert tot["model"] == "gpt-5-codex"


def test_session_totals_none_without_token_count(_codex_home):
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    p = d / "rollout-empty.jsonl"
    p.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "e", "cwd": "/x"}}) + "\n"
    )
    assert api._session_totals(p) is None


# --------------------------------------------------------------------------- #
# session_usage window bounds — including the start-None exclusion regression
# --------------------------------------------------------------------------- #
def test_session_usage_matches_cwd_and_sums(_codex_home):
    _write_rollout(
        _codex_home,
        sid="a",
        cwd="/repo",
        turns=[(_tot(100, 0, 50), None, 200000, "2026-07-14T10:01:00Z")],
    )
    _write_rollout(
        _codex_home,
        sid="b",
        cwd="/other",
        turns=[(_tot(999, 0, 999), None, 200000, "2026-07-14T10:02:00Z")],
    )
    got = api.session_usage("/repo")
    assert got is not None
    assert got["in"] == 100 and got["out"] == 50


def test_session_usage_since_ts_filters_old_sessions(_codex_home):
    # Session started well before since_ts must be excluded.
    _write_rollout(
        _codex_home,
        sid="old",
        cwd="/repo",
        ts="2026-07-14T08:00:00Z",
        turns=[(_tot(100, 0, 50), None, 200000, "2026-07-14T08:00:10Z")],
    )
    import datetime as dt

    since = dt.datetime.fromisoformat("2026-07-14T09:00:00+00:00").timestamp()
    assert api.session_usage("/repo", since_ts=since) is None


def test_session_usage_untimestamped_excluded_when_bounded(_codex_home):
    # A rollout whose session_meta has NO parseable timestamp must not leak into
    # a bounded window (it can't be proven to belong to this session's launch).
    # Regression guard: previously such a file bypassed both bounds and inflated
    # every sibling's totals.
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    p = d / "rollout-notime.jsonl"
    lines = [
        {
            "type": "session_meta",
            "payload": {"id": "nt", "cwd": "/repo"},
        },  # no timestamp
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {
            "type": "event_msg",
            "timestamp": "2026-07-14T10:00:00Z",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": _tot(500, 0, 500),
                    "model_context_window": 200000,
                },
            },
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    # With a since bound set, the untimestamped file is excluded -> None.
    assert api.session_usage("/repo", since_ts=1.0) is None
    # With NO bounds it is still counted (open-ended, single-session case).
    got = api.session_usage("/repo")
    assert got is not None and got["out"] == 500


def test_session_usage_until_ts_excludes_later_sibling(_codex_home):
    import datetime as dt

    _write_rollout(
        _codex_home,
        sid="mine",
        cwd="/repo",
        ts="2026-07-14T10:00:00Z",
        turns=[(_tot(100, 0, 50), None, 200000, "2026-07-14T10:00:10Z")],
    )
    _write_rollout(
        _codex_home,
        sid="next",
        cwd="/repo",
        ts="2026-07-14T11:00:00Z",
        turns=[(_tot(777, 0, 777), None, 200000, "2026-07-14T11:00:10Z")],
    )
    until = dt.datetime.fromisoformat("2026-07-14T10:30:00+00:00").timestamp()
    got = api.session_usage("/repo", until_ts=until)
    assert got is not None
    assert got["out"] == 50  # only "mine", not the later "next" sibling


# --------------------------------------------------------------------------- #
# rolling-window totals (_compute_windows / windows)
# --------------------------------------------------------------------------- #
def test_windows_diffs_cumulative_counters(_codex_home, monkeypatch):
    from backend.providers import pricing

    monkeypatch.setattr(
        pricing,
        "price_per_token",
        lambda m: {"in": 1e-6, "out": 2e-6, "cache_read": 0.0, "cache_write": 0.0},
    )
    # Two cumulative snapshots: deltas are (100 in, 50 out) then (100 in, 50 out).
    ts = "2026-07-14T10:00:00Z"
    _write_rollout(
        _codex_home,
        sid="w",
        cwd="/repo",
        turns=[
            (_tot(100, 0, 50), None, 200000, ts),
            (_tot(200, 0, 100), None, 200000, ts),
        ],
    )
    # A huge window makes the cutoff (now - window) deeply negative, so any real
    # timestamp buckets in — isolates the delta math from wall-clock drift.
    monkeypatch.setattr(api, "_WINDOWS", {"year": 10**12})
    out = api.windows()
    assert out["year"]["in"] == 200  # 100 + 100 incremental
    assert out["year"]["out"] == 100  # 50 + 50 incremental


def test_windows_clamps_cumulative_counter_reset(_codex_home, monkeypatch):
    # A session's cumulative counters can reset mid-file (lower than the prior
    # snapshot). Each per-turn delta is clamped at 0 so the reset contributes
    # nothing negative — the window totals stay the first snapshot's amount.
    from backend.providers import pricing

    monkeypatch.setattr(
        pricing,
        "price_per_token",
        lambda m: {"in": 1e-6, "out": 2e-6, "cache_read": 0.0, "cache_write": 0.0},
    )
    ts = "2026-07-14T10:00:00Z"
    _write_rollout(
        _codex_home,
        sid="reset",
        cwd="/repo",
        turns=[
            (_tot(200, 0, 100), None, 200000, ts),
            (_tot(50, 0, 30), None, 200000, ts),  # counters RESET to lower values
        ],
    )
    monkeypatch.setattr(api, "_WINDOWS", {"year": 10**12})
    out = api.windows()
    # Only the first snapshot counts; the reset delta clamps to 0, never negative.
    assert out["year"]["in"] == 200
    assert out["year"]["out"] == 100
    assert out["year"]["in"] >= 0 and out["year"]["out"] >= 0
    assert out["year"]["cost"] >= 0.0


# --------------------------------------------------------------------------- #
# usage_mode: unknown-auth fall-through
# --------------------------------------------------------------------------- #
def test_usage_mode_none_when_auth_mode_unrecognized(_codex_home):
    # auth.json exists but names neither a chatgpt plan nor an API key -> None,
    # so the provider falls back to its window-kind default.
    (_codex_home / "auth.json").write_text(json.dumps({"auth_mode": "sso"}))
    assert api.usage_mode() is None


# --------------------------------------------------------------------------- #
# _rate_limits_of / _window_from: shape guards + numeric coercion failures
# --------------------------------------------------------------------------- #
def test_rate_limits_of_rejects_wrong_shapes():
    assert api._rate_limits_of("not a dict") is None
    assert api._rate_limits_of({"type": "session_meta"}) is None  # wrong type
    # right type, wrong payload type
    assert (
        api._rate_limits_of(
            {"type": "event_msg", "payload": {"type": "something_else"}}
        )
        is None
    )
    # token_count but rate_limits missing / not a dict
    assert (
        api._rate_limits_of({"type": "event_msg", "payload": {"type": "token_count"}})
        is None
    )
    assert (
        api._rate_limits_of(
            {
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": []},
            }
        )
        is None
    )


def test_window_from_non_numeric_fields_are_dropped():
    # used_percent / resets_at that don't coerce are skipped, not crashed on.
    out = api._window_from({"used_percent": "NaNish", "resets_at": "later"})
    assert out is None  # nothing usable parsed
    # A bad resets_in_seconds also falls through cleanly.
    assert api._window_from({"resets_in_seconds": "soon"}) is None
    assert api._window_from("not a dict") is None


# --------------------------------------------------------------------------- #
# _read_jsonl: torn/blank lines skipped, unreadable file yields nothing
# --------------------------------------------------------------------------- #
def test_read_jsonl_skips_torn_and_blank_lines(_codex_home):
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    p = d / "rollout-x.jsonl"
    p.write_text(
        '{"type": "a"}\n'
        "\n"  # blank line skipped
        "{not valid json\n"  # torn line skipped
        '{"type": "b"}\n'
    )
    got = list(api._read_jsonl(p))
    assert [d["type"] for d in got] == ["a", "b"]


def test_read_jsonl_unreadable_file_yields_nothing(_codex_home):
    # A path that is a directory: open() raises -> the generator yields nothing.
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    assert list(api._read_jsonl(d)) == []


# --------------------------------------------------------------------------- #
# _session_totals: model capture + malformed-field tolerance
# --------------------------------------------------------------------------- #
def test_session_totals_skips_non_token_count_events_and_bad_ctx_window(_codex_home):
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    p = d / "rollout-mixed.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "m", "cwd": "/x"}},
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        # An event_msg that is NOT a token_count -> ignored.
        {"type": "event_msg", "payload": {"type": "agent_message"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": _tot(300, 100, 200),
                    # non-numeric ctx window is tolerated (stays 0)
                    "model_context_window": "huge",
                },
            },
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    tot = api._session_totals(p)
    assert tot["in"] == 200  # 300 - 100 cached
    assert tot["out"] == 200
    assert tot["model"] == "gpt-5-codex"
    assert tot["ctx_window"] == 0  # "huge" didn't coerce


def test_session_totals_i_helper_tolerates_non_numeric_totals(_codex_home):
    # total_token_usage with a non-numeric field: _i() coerces it to 0 rather
    # than raising, so the session still totals (real_in clamps at 0).
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    p = d / "rollout-bad.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "b", "cwd": "/x"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": "oops",
                        "cached_input_tokens": 0,
                        "output_tokens": 5,
                    }
                },
            },
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    tot = api._session_totals(p)
    assert tot["in"] == 0  # "oops" -> 0
    assert tot["out"] == 5


# --------------------------------------------------------------------------- #
# windows(): cache hit + never-raises fallback
# --------------------------------------------------------------------------- #
def test_windows_served_from_cache_within_ttl(_codex_home, monkeypatch):
    sentinel = {"day": api._zero_period()}
    api._win_cache = {"at": __import__("time").time(), "windows": sentinel}
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise AssertionError("must not recompute within TTL")

    monkeypatch.setattr(api, "_compute_windows", _boom)
    assert api.windows() is sentinel
    assert calls["n"] == 0


def test_windows_never_raises_on_compute_error(_codex_home, monkeypatch):
    def _boom():
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(api, "_compute_windows", _boom)
    api._win_cache = {"at": 0.0, "windows": None}
    out = api.windows()
    # Degrades to a zeroed shape, one period per window key.
    assert set(out) == set(api._WINDOWS)
    for k in out:
        assert out[k] == api._zero_period()


# --------------------------------------------------------------------------- #
# _meta_of / find_thread_id / session_usage: further branch coverage
# --------------------------------------------------------------------------- #
def test_meta_of_bails_when_first_typed_record_is_not_meta(_codex_home):
    # session_meta is always first; a file whose first typed record is something
    # else has no usable meta -> (None, None) without scanning further.
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    p = d / "rollout-nometa.jsonl"
    lines = [
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"type": "session_meta", "payload": {"id": "late", "cwd": "/x"}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert api._meta_of(p) == (None, None)


def test_find_thread_id_claimed_meta_yields_empty(_codex_home):
    # The newest rollout for the cwd exists and is in-window, but its session id
    # is already claimed by a sibling -> discovery returns "" (never steals it).
    _write_rollout(
        _codex_home,
        sid="taken",
        cwd="/repo",
        ts="2026-07-14T10:00:00Z",
        turns=[(_tot(10, 0, 5), None, 200000, "2026-07-14T10:00:10Z")],
    )
    assert api.find_thread_id("/repo", None, exclude={"taken"}) == ""


def test_session_usage_skips_matching_file_without_token_count(_codex_home):
    # A rollout in the right cwd but carrying NO token_count event contributes
    # nothing; a sibling with real totals still counts.
    d = _codex_home / "sessions" / "2026/07/14"
    d.mkdir(parents=True)
    (d / "rollout-empty.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "e", "cwd": "/repo"}})
        + "\n"
    )
    _write_rollout(
        _codex_home,
        sid="real",
        cwd="/repo",
        turns=[(_tot(100, 0, 50), None, 200000, "2026-07-14T10:01:00Z")],
    )
    got = api.session_usage("/repo")
    assert got is not None
    assert got["out"] == 50  # only the file with a token_count contributed


# --------------------------------------------------------------------------- #
# Account profiles (auth profiles)
#
# A codex session pinned to an `account` profile runs with CODEX_HOME pointed
# at that profile's isolated dir, so its rollouts land there and NOWHERE else.
# A scanner that reads only the server's own $CODEX_HOME reports such a session
# as having no tokens, no context occupancy and no thread — it looks dead in
# the UI while it is working.
# --------------------------------------------------------------------------- #
def _account_home(tmp_path, monkeypatch, profile_id="work"):
    """Configure a codex account profile and return its config root."""
    from backend.providers import auth_profiles

    home = tmp_path / "acct-home"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        auth_profiles, "codex_account_root_map", lambda: {str(home): profile_id}
    )
    return home


def _one_turn(inp, out, ts="2026-07-14T10:01:00Z"):
    return [(_tot(inp, 0, out), _tot(inp, 0, out), 272000, ts)]


def test_session_usage_finds_a_profiled_sessions_rollouts(
    _codex_home, tmp_path, monkeypatch
):
    home = _account_home(tmp_path, monkeypatch)
    _write_rollout(home, sid="profiled", cwd="/repo/x", turns=_one_turn(111, 22))
    got = api.session_usage("/repo/x")
    assert got is not None, "a codex account profile's session reported no usage"
    assert got["in"] == 111 and got["out"] == 22


def test_find_thread_id_sees_a_profiled_sessions_rollout(
    _codex_home, tmp_path, monkeypatch
):
    home = _account_home(tmp_path, monkeypatch)
    _write_rollout(home, sid="thread-in-account", cwd="/repo/y", turns=_one_turn(1, 1))
    assert api.find_thread_id("/repo/y", None) == "thread-in-account"


def test_both_roots_contribute_to_one_directorys_totals(
    _codex_home, tmp_path, monkeypatch
):
    home = _account_home(tmp_path, monkeypatch)
    _write_rollout(_codex_home, sid="ambient", cwd="/repo/z", turns=_one_turn(5, 0))
    _write_rollout(home, sid="profiled", cwd="/repo/z", turns=_one_turn(7, 0))
    # One machine, one directory, two identities that worked in it.
    assert api.session_usage("/repo/z")["in"] == 12


def test_no_profiles_configured_scans_exactly_the_ambient_root(_codex_home):
    """The pre-feature scan, unchanged."""
    assert api._sessions_dirs() == [api._sessions_dir()]


def test_the_plan_meter_reads_only_the_ambient_root(_codex_home, tmp_path, monkeypatch):
    """A rate-limit snapshot describes ONE subscription. Merging accounts here
    would report whichever identity happened to write last — the same reason
    the queue's limit gate ignores the meter for a profiled session."""
    _account_home(tmp_path, monkeypatch)
    assert api._sessions_dirs(ambient_only=True) == [api._sessions_dir()]
    assert len(api._sessions_dirs()) == 2  # ...while the per-session scan sees both
