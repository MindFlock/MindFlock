"""Antigravity live plan-quota (antigravity_usage_api).

agy keeps quota only in memory — MindFlock asks the running CLI's local
language server (RetrieveUserQuotaSummary over ConnectRPC/JSON) and normalizes
the per-model-group weekly buckets into the shared usage_live() shape. These
lock the log-line port discovery, the remainingFraction -> percent_used
normalization (the field is REMAINING, not used), and the provider wiring.
"""

from __future__ import annotations

from backend.providers import antigravity_usage_api as api

# A verbatim (values rounded) RetrieveUserQuotaSummary response from agy.
_DOC = {
    "response": {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit",
                        "window": "weekly",
                        "remainingFraction": 0.9056,
                        "resetTime": "2026-07-14T20:05:00Z",
                    }
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "displayName": "Weekly Limit",
                        "window": "weekly",
                        "remainingFraction": 1,
                        "resetTime": "2026-07-15T14:12:39Z",
                    }
                ],
            },
        ],
    }
}


def test_normalize_reads_remaining_not_used():
    out = api._normalize(_DOC)
    gem = next(g for g in out["groups"] if g["label"] == "Gemini Models")
    # remainingFraction 0.9056 -> ~9.4% USED (the TUI bar shows remaining).
    assert abs(gem["percent_used"] - 9.4) < 0.11
    tp = next(g for g in out["groups"] if g["label"] == "Claude and GPT models")
    assert tp["percent_used"] == 0.0


def test_normalize_headline_is_most_used_group():
    out = api._normalize(_DOC)
    assert out["percent_used"] == max(g["percent_used"] for g in out["groups"])
    gem = next(g for g in out["groups"] if g["label"] == "Gemini Models")
    assert out["end"] == gem["end"]


def test_normalize_parses_reset_time():
    out = api._normalize(_DOC)
    # 2026-07-14T20:05:00Z
    assert int(out["end"]) == 1784059500


def test_normalize_missing_remaining_fraction_is_unknown_not_exhausted():
    doc = {
        "response": {
            "groups": [
                {
                    "displayName": "G",
                    "buckets": [{"resetTime": "2026-07-14T20:05:00Z"}],
                }
            ]
        }
    }
    out = api._normalize(doc)
    assert "percent_used" not in out["groups"][0]
    assert out["groups"][0]["end"]


def test_normalize_rejects_junk():
    assert api._normalize({}) is None
    assert api._normalize({"response": {"groups": []}}) is None
    assert api._normalize({"response": {"groups": [{"buckets": []}]}}) is None


def test_normalize_skips_non_dict_group():
    doc = {
        "response": {
            "groups": [
                "not-a-dict",  # skipped
                {
                    "displayName": "G",
                    "buckets": [{"remainingFraction": 0.5, "resetTime": "x"}],
                },
            ]
        }
    }
    out = api._normalize(doc)
    assert [g["label"] for g in out["groups"]] == ["G"]
    assert out["groups"][0]["percent_used"] == 50.0


def test_bucket_window_shape_and_coercion_guards():
    # buckets not a list -> None
    assert api._bucket_window({"buckets": "nope"}) is None
    # bucket not a dict -> None
    assert api._bucket_window({"buckets": ["nope"]}) is None
    # non-numeric remainingFraction -> pct None, but a resetTime still qualifies
    win = api._bucket_window(
        {"buckets": [{"remainingFraction": "x", "resetTime": "2026-07-14T20:05:00Z"}]}
    )
    assert win == (None, 1784059500.0)
    # neither a usable fraction nor a reset time -> None
    assert api._bucket_window({"buckets": [{}]}) is None


def test_fetch_none_when_ports_live_but_rpc_unreachable(monkeypatch):
    # A live agy process whose language server isn't answering yet: every port
    # is probed, each urlopen raises, and _fetch falls through to None (the
    # quiet mode-only fallback) rather than crashing or inventing a reading.
    ports = [46031, 46032]
    monkeypatch.setattr(api, "_live_ports", lambda: list(ports))
    probed = []

    def _boom(req, timeout=None):
        probed.append(req.full_url)
        raise OSError("connection refused")

    monkeypatch.setattr(api.urllib.request, "urlopen", _boom)
    assert api._fetch() is None
    assert len(probed) == len(ports)  # every live port probed before giving up


def test_fetch_none_when_rpc_returns_non_dict_json(monkeypatch):
    # The server answers but with valid JSON that isn't an object (a list) —
    # _normalize is skipped and _fetch keeps looking, ending at None.
    monkeypatch.setattr(api, "_live_ports", lambda: [46031])

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[1, 2, 3]"  # valid JSON, but a list rather than a dict

    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert api._fetch() is None


def test_fetch_none_when_rpc_returns_dict_without_groups(monkeypatch):
    # A live server that answers with a dict that _normalize rejects (no groups)
    # -> _fetch keeps looking and ends at None (line 182 fall-through).
    monkeypatch.setattr(api, "_live_ports", lambda: [46031])

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"response": {"groups": []}}'  # valid dict, nothing usable

    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert api._fetch() is None


def test_fetch_returns_normalized_reading_from_live_server(monkeypatch):
    # A live port that answers with a real quota doc -> _fetch returns the
    # normalized reading (the success path).
    monkeypatch.setattr(api, "_live_ports", lambda: [46031])

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json as _json

            return _json.dumps(_DOC).encode()

    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = api._fetch()
    assert out is not None
    assert out["percent_used"] == max(g["percent_used"] for g in out["groups"])


def test_live_ports_no_log_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CLI_DIR", str(tmp_path / "absent"))
    assert api._live_ports() == []


def test_live_ports_ignores_listen_line_beyond_startup_window(tmp_path, monkeypatch):
    import os

    logdir = tmp_path / "log"
    logdir.mkdir()
    # 250 junk lines push the listen line past the 200-line startup scan cap, so
    # the port is never discovered.
    body = "".join("noise line %d\n" % i for i in range(250))
    body += (
        "I0708 10:07:20.073070 %d server.go:527] "
        "Language server listening on random port at 46031 for HTTP\n" % os.getpid()
    )
    (logdir / "cli-late.log").write_text(body)
    monkeypatch.setenv("ANTIGRAVITY_CLI_DIR", str(tmp_path))
    assert api._live_ports() == []


def test_live_ports_skips_unreadable_log(tmp_path, monkeypatch):
    import os

    logdir = tmp_path / "log"
    logdir.mkdir()
    # A "log" that is actually a directory: open() raises -> that entry is
    # skipped, and the real log alongside it still yields its port.
    (logdir / "cli-adir.log").mkdir()
    (logdir / "cli-good.log").write_text(
        "I0708 10:07:20.073070 %d server.go:527] "
        "Language server listening on random port at 46031 for HTTP\n" % os.getpid()
    )
    monkeypatch.setenv("ANTIGRAVITY_CLI_DIR", str(tmp_path))
    assert api._live_ports() == [46031]


def test_pid_alive_permission_error_counts_as_alive(monkeypatch):
    # os.kill raising a non-ProcessLookupError (e.g. EPERM: the process exists
    # but we can't signal it) still means the process is alive.
    def _perm(pid, sig):
        raise PermissionError("EPERM")

    monkeypatch.setattr(api.os, "kill", _perm)
    assert api._pid_alive(4242) is True

    def _gone(pid, sig):
        raise ProcessLookupError("no such pid")

    monkeypatch.setattr(api.os, "kill", _gone)
    assert api._pid_alive(4242) is False


def test_live_ports_parses_listen_line_and_checks_pid(tmp_path, monkeypatch):
    logdir = tmp_path / "log"
    logdir.mkdir()
    import os

    (logdir / "cli-20260708_100720.log").write_text(
        "I0708 10:07:20.072985 %d server.go:519] Language server listening on random port at 45707 for HTTPS (gRPC)\n"
        "I0708 10:07:20.073070 %d server.go:527] Language server listening on random port at 46031 for HTTP\n"
        % (os.getpid(), os.getpid())
    )
    # A dead process's log must be skipped.
    (logdir / "cli-20260701_000000.log").write_text(
        "I0701 00:00:00.000000 99999999 server.go:527] Language server listening on random port at 12345 for HTTP\n"
    )
    monkeypatch.setenv("ANTIGRAVITY_CLI_DIR", str(tmp_path))
    assert api._live_ports() == [46031]


def test_live_usage_fetch_runs_without_holding_lock(monkeypatch):
    # Regression: _fetch (up to ~8 ports x 3s of blocking urlopen) must NOT run
    # while the module lock is held, or concurrent callers serialize behind one
    # slow probe. We assert the lock is released during _fetch by acquiring it
    # from inside the fetch (mirrors the claude_usage_api test).
    monkeypatch.setattr(api, "_cache", {"at": 0.0, "good_at": 0.0, "good": None})

    def _f():
        acquired = api._lock.acquire(blocking=False)
        if acquired:
            api._lock.release()
        assert acquired, "lock was held across the port probes"
        return {"percent_used": 1.0}

    monkeypatch.setattr(api, "_fetch", _f)
    assert api.live_usage() == {"percent_used": 1.0}


def test_provider_usage_live_is_wired(monkeypatch):
    from backend import providers

    p = providers.resolve("agy")
    monkeypatch.setattr(api, "live_usage", lambda: {"percent_used": 9.4, "end": 1.0})
    assert p.usage_live() == {"percent_used": 9.4, "end": 1.0}


def test_provider_usage_live_never_raises(monkeypatch):
    from backend import providers

    p = providers.resolve("agy")

    def _boom():
        raise RuntimeError("no server")

    monkeypatch.setattr(api, "live_usage", _boom)
    assert p.usage_live() is None


# --------------------------------------------------------------------------- #
# find_thread_id / record_thread (per-window resume-thread discovery)
# --------------------------------------------------------------------------- #
from backend.providers import antigravity as agy  # noqa: E402


def _conv(tmp_path, monkeypatch):
    d = tmp_path / "conv"
    (d / "conversations").mkdir(parents=True)
    monkeypatch.setenv("ANTIGRAVITY_CLI_DIR", str(d))
    return d / "conversations"


def _mk(convdir, stem, mtime):
    import os

    p = convdir / f"{stem}.db"
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))
    return p


def test_find_thread_id_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CLI_DIR", str(tmp_path / "nope"))
    assert agy.find_thread_id(since_ts=None) == ""


def test_find_thread_id_picks_newest(tmp_path, monkeypatch):
    convdir = _conv(tmp_path, monkeypatch)
    _mk(convdir, "older", 1000.0)
    _mk(convdir, "newer", 2000.0)
    assert agy.find_thread_id(since_ts=None) == "newer"


def test_find_thread_id_respects_since_ts_with_slack(tmp_path, monkeypatch):
    convdir = _conv(tmp_path, monkeypatch)
    _mk(convdir, "stale", 500.0)
    # Nothing at/after the launch bound (with 5s slack) -> no match.
    assert agy.find_thread_id(since_ts=1000.0) == ""
    _mk(convdir, "fresh", 1004.0)  # within the 5s slack below 1000
    assert agy.find_thread_id(since_ts=1000.0) == "fresh"


def test_find_thread_id_skips_claimed(tmp_path, monkeypatch):
    convdir = _conv(tmp_path, monkeypatch)
    _mk(convdir, "claimed", 2000.0)
    _mk(convdir, "mine", 1500.0)
    assert agy.find_thread_id(since_ts=None, exclude={"claimed"}) == "mine"


def test_find_thread_id_skips_file_whose_stat_fails(tmp_path, monkeypatch):
    import os

    convdir = _conv(tmp_path, monkeypatch)
    _mk(convdir, "real", 1500.0)
    # A dangling symlink with a .db suffix is yielded by glob but its stat()
    # raises OSError -> it is skipped, and the real conversation still wins.
    os.symlink(str(convdir / "nonexistent-target"), str(convdir / "broken.db"))
    assert agy.find_thread_id(since_ts=None) == "real"


def test_find_thread_id_never_raises(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(agy, "_conversations_dir", _boom)
    assert agy.find_thread_id(since_ts=None) == ""


def test_record_thread_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))

    def _boom(*a, **k):
        raise RuntimeError("discovery blew up")

    monkeypatch.setattr(agy, "find_thread_id", _boom)
    p = agy.AntigravityProvider(_agy_cfg())
    # Must not propagate — thread binding is enrichment only.
    p.record_thread("sess-z", "/wd", since_ts=None)
    from backend.providers import thread_markers

    assert thread_markers.read("sess-z") == ""


def test_record_thread_binds_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))
    convdir = _conv(tmp_path, monkeypatch)
    _mk(convdir, "conv-1", 2000.0)
    from backend.providers import thread_markers

    p = agy.AntigravityProvider(_agy_cfg())
    p.record_thread("sess-a", "/wd", since_ts=None)
    assert thread_markers.read("sess-a") == "conv-1"
    # Re-running without a new conversation keeps the same binding.
    p.record_thread("sess-a", "/wd", since_ts=None)
    assert thread_markers.read("sess-a") == "conv-1"


def _agy_cfg():
    from backend.providers.config import BUILTIN_CONFIGS

    return next(c for c in BUILTIN_CONFIGS if c.name == "antigravity")
