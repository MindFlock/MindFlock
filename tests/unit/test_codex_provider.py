"""The CodexProvider class (codex.py): its live-usage / telemetry / resume-thread
delegation to ``codex_usage_api``, and its degrade-to-generic fallbacks.

CodexProvider is a thin GenericProvider subclass whose extra methods each defer
to ``codex_usage_api`` and swallow every failure so the bundled Codex CLI keeps
working (as the plain generic provider) when the on-disk rollout probe can't
help. These lock both halves: the happy delegation AND the never-raises fallback
that the coverage of codex.py otherwise leaves untested.
"""

from __future__ import annotations

import pytest

from backend import providers
from backend.providers import codex as codex_mod
from backend.providers import codex_usage_api
from backend.providers import thread_markers
from backend.providers.base import LaunchContext


@pytest.fixture
def provider():
    p = providers.resolve("codex")
    assert isinstance(p, codex_mod.CodexProvider)
    return p


# --------------------------------------------------------------------------- #
# usage_mode: auth-derived mode wins; unknown falls back to the config default.
# --------------------------------------------------------------------------- #
def test_usage_mode_prefers_auth_probe(provider, monkeypatch):
    monkeypatch.setattr(codex_usage_api, "usage_mode", lambda: "metered")
    assert provider.usage_mode() == "metered"


def test_usage_mode_falls_back_to_window_default_when_unknown(provider, monkeypatch):
    # auth unknown (None) -> the generic/base default derived from the codex
    # config's usage_window (rolling -> "windowed").
    monkeypatch.setattr(codex_usage_api, "usage_mode", lambda: None)
    assert provider.usage_mode() == "windowed"


def test_usage_mode_falls_back_when_probe_raises(provider, monkeypatch):
    def _boom():
        raise RuntimeError("auth.json exploded")

    monkeypatch.setattr(codex_usage_api, "usage_mode", _boom)
    # The exception is swallowed and the window-default is used instead.
    assert provider.usage_mode() == "windowed"


# --------------------------------------------------------------------------- #
# usage_live / usage_periods: pass the api result through, None on failure.
# --------------------------------------------------------------------------- #
def test_usage_live_passes_through(provider, monkeypatch):
    reading = {"percent_used": 33.0, "end": 999.0}
    monkeypatch.setattr(codex_usage_api, "live_usage", lambda: reading)
    assert provider.usage_live() == reading


def test_usage_live_none_on_failure(provider, monkeypatch):
    def _boom():
        raise RuntimeError("no rollout")

    monkeypatch.setattr(codex_usage_api, "live_usage", _boom)
    assert provider.usage_live() is None


def test_usage_periods_passes_through(provider, monkeypatch):
    periods = {"day": {"in": 1, "out": 2, "cache_read": 0, "cache_write": 0}}
    monkeypatch.setattr(codex_usage_api, "windows", lambda: periods)
    assert provider.usage_periods() == periods


def test_usage_periods_none_on_failure(provider, monkeypatch):
    def _boom():
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(codex_usage_api, "windows", _boom)
    assert provider.usage_periods() is None


# --------------------------------------------------------------------------- #
# session_tokens: real telemetry wins; empty/None/exception -> generic zeros.
# --------------------------------------------------------------------------- #
def test_session_tokens_returns_api_reading(provider, monkeypatch):
    got = {
        "in": 600,
        "out": 500,
        "cache_read": 400,
        "cache_write": 0,
        "ctx": 1000,
        "ctx_window": 272000,
        "model": "gpt-5-codex",
    }
    monkeypatch.setattr(codex_usage_api, "session_usage", lambda cwd, s, u: dict(got))
    out = provider.session_tokens("/repo", 100.0, 200.0)
    assert out == got


def test_session_tokens_falls_back_to_zeros_when_no_data(provider, monkeypatch):
    # session_usage returns None (nothing on disk) -> the generic empty telemetry.
    monkeypatch.setattr(codex_usage_api, "session_usage", lambda cwd, s, u: None)
    out = provider.session_tokens("/repo", None)
    assert out["in"] == 0 and out["out"] == 0
    assert out["model"] == ""


def test_session_tokens_empty_dict_treated_as_no_data(provider, monkeypatch):
    # A falsy (empty) reading is not returned as-is — the ``if got`` guard makes
    # it fall through to the generic zeros rather than surfacing {}.
    monkeypatch.setattr(codex_usage_api, "session_usage", lambda cwd, s, u: {})
    out = provider.session_tokens("/repo", None)
    assert out["in"] == 0 and "model" in out


def test_session_tokens_falls_back_when_probe_raises(provider, monkeypatch):
    def _boom(cwd, s, u):
        raise RuntimeError("torn rollout")

    monkeypatch.setattr(codex_usage_api, "session_usage", _boom)
    out = provider.session_tokens("/repo", None)
    assert out["in"] == 0 and out["out"] == 0


# --------------------------------------------------------------------------- #
# record_thread: binds this window's own session id, idempotent, never raises.
# --------------------------------------------------------------------------- #
def test_record_thread_binds_discovered_id(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    monkeypatch.setattr(
        codex_usage_api, "find_thread_id", lambda wd, since, exclude: "sess-42"
    )
    provider.record_thread("mindflock_c1", "/repo", since_ts=100.0)
    assert thread_markers.read("mindflock_c1") == "sess-42"


def test_record_thread_no_id_leaves_marker_untouched(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    monkeypatch.setattr(codex_usage_api, "find_thread_id", lambda wd, s, exclude: "")
    provider.record_thread("mindflock_c2", "/repo", since_ts=100.0)
    assert thread_markers.read("mindflock_c2") == ""


def test_record_thread_idempotent_no_rewrite(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    monkeypatch.setattr(
        codex_usage_api, "find_thread_id", lambda wd, s, exclude: "sess-9"
    )
    thread_markers.record("mindflock_c3", "sess-9")
    writes = {"n": 0}
    real_record = thread_markers.record

    def _spy(name, tid):
        writes["n"] += 1
        return real_record(name, tid)

    monkeypatch.setattr(thread_markers, "record", _spy)
    provider.record_thread("mindflock_c3", "/repo", since_ts=None)
    # The recorded id already matches, so no rewrite happens.
    assert writes["n"] == 0
    assert thread_markers.read("mindflock_c3") == "sess-9"


def test_record_thread_excludes_sibling_claims(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    # A sibling window already claimed "taken"; discovery must be asked to skip it.
    thread_markers.record("mindflock_sib", "taken")
    seen = {}

    def _find(wd, since, exclude):
        seen["exclude"] = exclude
        return ""

    monkeypatch.setattr(codex_usage_api, "find_thread_id", _find)
    provider.record_thread("mindflock_me", "/repo", since_ts=None)
    assert "taken" in seen["exclude"]


def test_record_thread_never_raises(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))

    def _boom(wd, s, exclude):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(codex_usage_api, "find_thread_id", _boom)
    # Must not propagate — thread binding is enrichment only.
    provider.record_thread("mindflock_c4", "/repo", since_ts=None)
    assert thread_markers.read("mindflock_c4") == ""


# --------------------------------------------------------------------------- #
# Generic launch path picked up by codex: activity hooks + activity markers.
# --------------------------------------------------------------------------- #
def test_build_launch_installs_activity_hooks(provider, tmp_path, monkeypatch):
    # Codex declares an activity_hooks_file, so a launch with a real workdir +
    # session name installs the marker hooks (generic.install_activity_hooks).
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path / "seed"))
    wd = tmp_path / "repo"
    wd.mkdir()
    cmd = provider.build_launch_command(
        LaunchContext(program="codex", workdir=str(wd), session_name="mindflock_h")
    )
    assert cmd.startswith("codex")
    # The provider config points at .codex/hooks.json; the install writes it.
    assert (wd / ".codex" / "hooks.json").exists()


def test_activity_state_reads_marker(provider, tmp_path, monkeypatch):
    # Codex has a hooks file declared, so activity_state consults the marker
    # (returns None when none has been written yet).
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "act"))
    assert provider.activity_state("mindflock_none") is None
    assert provider.activity_state_age("mindflock_none") is None
