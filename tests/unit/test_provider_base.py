"""The BaseProvider default contract (base.py).

BaseProvider is the provider-agnostic default every CLI subclass inherits and
selectively overrides. These lock the defaults themselves — the behaviour an
unconfigured / custom program gets — so a future edit to a subclass can't
silently change what "no override" means. Also covers ``seed_prompt_expr``'s
best-effort file write.
"""

from __future__ import annotations

import pytest

from backend.providers.base import (
    BaseProvider,
    LaunchContext,
    TrustSpec,
    seed_prompt_expr,
)


@pytest.fixture
def base():
    return BaseProvider()


# --------------------------------------------------------------------------- #
# identity + launch
# --------------------------------------------------------------------------- #
def test_matches_empty_program_is_false(base):
    assert base.matches("") is False


def test_matches_unknown_program_is_false(base):
    # BaseProvider claims no aliases, so it matches nothing concrete.
    assert base.matches("some-cli --flag") is False


def test_build_launch_command_bare_and_resume(base):
    assert base.build_launch_command(LaunchContext(program="")) is None
    assert base.build_launch_command(LaunchContext(program="mycli")) == "mycli"
    assert (
        base.build_launch_command(LaunchContext(program="mycli", resume=True))
        == "mycli --continue || mycli"
    )


def test_owns_launcher_false_and_write_launcher_raises(base):
    assert base.owns_launcher(LaunchContext(program="mycli")) is False
    with pytest.raises(NotImplementedError):
        base.write_launcher(LaunchContext(program="mycli"))


def test_natural_exit_codes(base):
    assert base.is_natural_exit(0) is True
    assert base.is_natural_exit(130) is True
    assert base.is_natural_exit(137) is False
    assert base.is_natural_exit(None) is False


# --------------------------------------------------------------------------- #
# terminal classification defaults
# --------------------------------------------------------------------------- #
def test_trust_prompt_default_is_open_documentation_gate(base):
    spec = base.trust_prompt()
    assert isinstance(spec, TrustSpec)
    assert spec.patterns == ("Open documentation url for more info",)
    assert spec.keystroke == b"\x44\x0d"  # 'D' then Enter


def test_no_idle_waiting_working_progress_signals(base):
    assert base.idle_prompt_pattern() is None
    assert base.waiting_prompt_patterns() == ()
    assert base.working_pane_patterns() == ()
    assert base.progress_token_pattern() is None


# --------------------------------------------------------------------------- #
# activity / thread / telemetry defaults
# --------------------------------------------------------------------------- #
def test_activity_and_snippet_defaults_are_none(base):
    assert base.activity_state("s") is None
    assert base.activity_state_age("s") is None
    assert base.install_activity_hooks("/wd", "s") is None
    assert base.last_turn_snippet("s", "/wd") is None
    assert base.record_thread("s", "/wd") is None


def test_resume_thread_id_reads_marker(base, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    from backend.providers import thread_markers

    assert base.resume_thread_id("s") == ""  # nothing recorded
    thread_markers.record("s", "abc-123")
    assert base.resume_thread_id("s") == "abc-123"


def test_session_tokens_default_is_zeroed(base):
    out = base.session_tokens("/wd", None)
    assert out == {
        "in": 0,
        "out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "ctx": 0,
        "model": "",
    }


# --------------------------------------------------------------------------- #
# usage defaults
# --------------------------------------------------------------------------- #
def test_usage_window_default_is_unknown(base):
    assert base.usage_window() == {
        "kind": "",
        "hours": 0.0,
        "weekly_hours": 0.0,
        "note": "",
    }


def test_usage_mode_derives_from_window(base, monkeypatch):
    # No declared window -> metered (pay-per-token).
    assert base.usage_mode() == "metered"
    # A declared window kind -> windowed (subscription plan).
    monkeypatch.setattr(base, "usage_window", lambda: {"kind": "rolling"})
    assert base.usage_mode() == "windowed"


def test_usage_live_periods_panel_defaults(base):
    assert base.usage_live() is None
    assert base.usage_periods() is None
    assert base.usage_panel_visible() is False


def test_usage_limit_patterns_default_is_shared_table(base):
    from backend.providers.usage_limits import DEFAULT_LIMIT_PATTERNS

    assert base.usage_limit_patterns() is DEFAULT_LIMIT_PATTERNS


def test_usage_limit_state_delegates_to_detector(base):
    assert base.usage_limit_state("all good", now=0.0)["limited"] is False
    assert base.usage_limit_state("usage limit reached", now=0.0)["limited"] is True


# --------------------------------------------------------------------------- #
# connection defaults
# --------------------------------------------------------------------------- #
def test_minimal_and_login_fall_back_to_name(base):
    # No aliases -> both fall back to the registry name.
    assert base.minimal_launch_command() == "base"
    assert base.login_command() == "base"


def test_install_hint_and_auth_evidence_empty(base):
    assert base.install_hint() == ""
    assert base.auth_evidence() == ""


# --------------------------------------------------------------------------- #
# seed_prompt_expr: writes a file, degrades to "" on empty/unwritable
# --------------------------------------------------------------------------- #
def test_seed_prompt_expr_writes_and_returns_cat_expr(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path / "seed"))
    expr = seed_prompt_expr("mindflock_s", "fix the bug")
    assert expr.startswith('"$(cat ')
    assert expr.endswith('.md)"')
    # The prompt text was actually written to the seed file.
    written = list((tmp_path / "seed").glob("*.md"))
    assert len(written) == 1
    assert written[0].read_text() == "fix the bug"


def test_seed_prompt_expr_empty_prompt_returns_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path / "seed"))
    assert seed_prompt_expr("mindflock_s", "") == ""


def test_seed_prompt_expr_unwritable_dir_returns_blank(tmp_path, monkeypatch):
    # Parent is a file, so mkdir() raises OSError -> degrade to "" (no seed).
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a dir")
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(blocker / "seed"))
    assert seed_prompt_expr("mindflock_s", "do a thing") == ""


# --------------------------------------------------------------------------- #
# normalize_program — a resolved binary path folds back to the provider name
# --------------------------------------------------------------------------- #
# GetClaudeCommand reports `which` output, so a first run stored an absolute
# path as the default program. Every consumer that shows or matches a program
# then had to cope with an install detail; the New Session dialog didn't, and
# rendered it as a spurious extra agent entry.
def test_normalize_program_folds_a_known_binary_path():
    from backend import providers

    assert providers.normalize_program("/opt/homebrew/bin/claude") == "claude"
    assert providers.normalize_program("/usr/local/bin/codex") == "codex"


def test_normalize_program_passes_through_bare_names_and_customs():
    from backend import providers

    assert providers.normalize_program("claude") == "claude"
    assert providers.normalize_program("codex") == "codex"
    # No provider claims it -> it IS the launch command, keep it exactly.
    assert providers.normalize_program("/opt/bin/my-agent") == "/opt/bin/my-agent"
    # Arguments mean a command line, not a binary to identify.
    assert providers.normalize_program("/opt/homebrew/bin/claude --foo") == (
        "/opt/homebrew/bin/claude --foo"
    )


def test_normalize_program_handles_empty_and_none():
    from backend import providers

    assert providers.normalize_program("") == ""
    assert providers.normalize_program(None) == ""
    assert providers.normalize_program("   ") == ""


def test_normalize_program_never_returns_the_catch_all():
    """The generic fallback claims every program, so it must not be allowed to
    rename an unrecognised path to "generic"."""
    from backend import providers

    assert providers.normalize_program("/opt/bin/whatever") == "/opt/bin/whatever"
