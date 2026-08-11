"""Hermetic unit tests for :mod:`backend.providers.claude`.

Covers the pure/filesystem-only surface of the Claude provider:

* :func:`_ts_epoch` ISO timestamp parsing,
* :func:`_claude_transcript_tokens` summing the four /usage figures plus
  latest-turn context and model, reading fake transcripts under a
  monkeypatched ``HOME`` (never the real home dir),
* the terminal-classification patterns (``waiting_prompt_patterns``,
  ``trust_prompt``, ``idle_prompt_pattern``).

No subprocess / tmux / claude / network effects are exercised; every
filesystem read is confined to a pytest ``tmp_path`` used as a fake HOME.
"""

from __future__ import annotations

import datetime as _dt
import json
import re

import pytest

from backend.providers.claude import (
    ClaudeProvider,
    _claude_transcript_tokens,
    _file_in_window,
    _ts_epoch,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _encode(workdir: str) -> str:
    """Mirror the module's cwd->dir encoding (non-alnum -> dash)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", workdir)


def _write_transcript(home, workdir, entries, *, root_name=".claude", fname="s.jsonl"):
    """Write a fake Claude Code transcript .jsonl under a fake HOME.

    ``entries`` is a list of dicts already shaped like transcript lines.
    Returns the encoded project dir path.
    """
    proj = home / root_name / "projects" / _encode(workdir)
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / fname
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return proj


def _usage_entry(*, ts=None, model="claude-sonnet-4", in_=0, out=0, cw=0, cr=0):
    """Build an assistant transcript line with a nested message.usage block."""
    entry = {
        "message": {
            "model": model,
            "usage": {
                "input_tokens": in_,
                "output_tokens": out,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
            },
        }
    }
    if ts is not None:
        entry["timestamp"] = ts
    return entry


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A throwaway HOME with no CLAUDE_CONFIG_DIR leaking the real env."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


# --------------------------------------------------------------------------- #
# _ts_epoch
# --------------------------------------------------------------------------- #
def test_ts_epoch_parses_zulu_iso():
    got = _ts_epoch("2024-01-02T03:04:05Z")
    expected = _dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=_dt.timezone.utc).timestamp()
    assert got == expected


def test_ts_epoch_parses_offset_iso():
    got = _ts_epoch("2024-01-02T03:04:05+00:00")
    expected = _dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=_dt.timezone.utc).timestamp()
    assert got == expected


def test_ts_epoch_none_and_empty():
    assert _ts_epoch(None) is None
    assert _ts_epoch("") is None


def test_ts_epoch_garbage_returns_none():
    assert _ts_epoch("not-a-timestamp") is None
    assert _ts_epoch("2024-13-99T99:99:99Z") is None


def test_ts_epoch_ordering_is_monotonic():
    earlier = _ts_epoch("2024-01-01T00:00:00Z")
    later = _ts_epoch("2024-01-01T00:00:01Z")
    assert earlier is not None and later is not None
    assert later > earlier


# --------------------------------------------------------------------------- #
# _claude_transcript_tokens — summation
# --------------------------------------------------------------------------- #
def test_tokens_empty_workdir_returns_zero():
    out = _claude_transcript_tokens("", None)
    assert out == {
        "in": 0,
        "out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "ctx": 0,
        "model": "",
    }


def test_tokens_no_transcript_dir_returns_zero(fake_home):
    # HOME exists but has no projects dir for this workdir.
    out = _claude_transcript_tokens("/work/proj", None)
    assert out == {
        "in": 0,
        "out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "ctx": 0,
        "model": "",
    }


def test_tokens_sums_all_four_figures(fake_home):
    workdir = "/work/proj"
    _write_transcript(
        fake_home,
        workdir,
        [
            _usage_entry(ts="2024-01-01T00:00:00Z", in_=10, out=1, cw=100, cr=1000),
            _usage_entry(ts="2024-01-01T00:00:01Z", in_=20, out=2, cw=200, cr=2000),
        ],
    )
    out = _claude_transcript_tokens(workdir, None)
    assert out["in"] == 30
    assert out["out"] == 3
    assert out["cache_write"] == 300
    assert out["cache_read"] == 3000


def test_tokens_latest_turn_ctx_and_model(fake_home):
    workdir = "/work/proj"
    # Deliberately write the newest turn first to prove ordering is by timestamp,
    # not file order. latest ctx = in + cache_read + cache_write of newest turn.
    _write_transcript(
        fake_home,
        workdir,
        [
            _usage_entry(
                ts="2024-01-01T00:00:05Z", model="claude-opus-4", in_=5, cw=50, cr=500
            ),
            _usage_entry(
                ts="2024-01-01T00:00:01Z", model="claude-sonnet-4", in_=1, cw=10, cr=100
            ),
        ],
    )
    out = _claude_transcript_tokens(workdir, None)
    assert out["ctx"] == 5 + 500 + 50  # newest turn only
    assert out["model"] == "claude-opus-4"


def test_tokens_since_ts_filters_prior_turns(fake_home):
    workdir = "/work/proj"
    cutoff = _ts_epoch("2024-01-01T00:00:10Z")
    _write_transcript(
        fake_home,
        workdir,
        [
            # before cutoff -> excluded
            _usage_entry(ts="2024-01-01T00:00:05Z", in_=999, out=999, cw=999, cr=999),
            # at/after cutoff -> counted
            _usage_entry(ts="2024-01-01T00:00:10Z", in_=7, out=8, cw=9, cr=10),
        ],
    )
    out = _claude_transcript_tokens(workdir, cutoff)
    assert out["in"] == 7
    assert out["out"] == 8
    assert out["cache_write"] == 9
    assert out["cache_read"] == 10


def test_tokens_reads_top_level_usage_block(fake_home):
    """A line with a top-level ``usage`` (no message wrapper) is also counted."""
    workdir = "/work/proj"
    proj = fake_home / ".claude" / "projects" / _encode(workdir)
    proj.mkdir(parents=True)
    entry = {
        "timestamp": "2024-01-01T00:00:00Z",
        "model": "claude-haiku",
        "usage": {
            "input_tokens": 4,
            "output_tokens": 5,
            "cache_creation_input_tokens": 6,
            "cache_read_input_tokens": 7,
        },
    }
    (proj / "s.jsonl").write_text(json.dumps(entry) + "\n")
    out = _claude_transcript_tokens(workdir, None)
    assert (out["in"], out["out"], out["cache_write"], out["cache_read"]) == (
        4,
        5,
        6,
        7,
    )
    assert out["model"] == "claude-haiku"
    assert out["ctx"] == 4 + 7 + 6


def test_tokens_scans_multiple_claude_roots(fake_home):
    """Every ``~/.claude*`` config root is summed (alternate installs/wrappers)."""
    workdir = "/work/proj"
    _write_transcript(
        fake_home,
        workdir,
        [_usage_entry(ts="2024-01-01T00:00:01Z", in_=10)],
        root_name=".claude",
        fname="a.jsonl",
    )
    _write_transcript(
        fake_home,
        workdir,
        [_usage_entry(ts="2024-01-01T00:00:02Z", in_=100)],
        root_name=".claude-alt",
        fname="b.jsonl",
    )
    out = _claude_transcript_tokens(workdir, None)
    assert out["in"] == 110


def test_tokens_config_dir_env_is_scanned(tmp_path, monkeypatch):
    """A CLAUDE_CONFIG_DIR outside HOME contributes transcripts too."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = tmp_path / "custom-cfg"
    proj = cfg / "projects" / _encode("/work/proj")
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        json.dumps(_usage_entry(ts="2024-01-01T00:00:00Z", in_=42)) + "\n"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    out = _claude_transcript_tokens("/work/proj", None)
    assert out["in"] == 42


def test_tokens_skips_lines_without_usage_and_bad_json(fake_home):
    workdir = "/work/proj"
    proj = fake_home / ".claude" / "projects" / _encode(workdir)
    proj.mkdir(parents=True)
    lines = [
        "not json at all",
        json.dumps({"type": "user", "message": {"content": "hi"}}),  # no usage
        '{broken json but has "usage" token',  # has token, fails json.loads
        json.dumps(_usage_entry(ts="2024-01-01T00:00:00Z", in_=3)),
    ]
    (proj / "s.jsonl").write_text("\n".join(lines) + "\n")
    out = _claude_transcript_tokens(workdir, None)
    assert out["in"] == 3  # only the one valid usage line counted


def test_tokens_ignores_non_jsonl_files(fake_home):
    workdir = "/work/proj"
    proj = _write_transcript(
        fake_home, workdir, [_usage_entry(ts="2024-01-01T00:00:00Z", in_=5)]
    )
    # A .txt file with a usage block must be ignored.
    (proj / "note.txt").write_text(json.dumps(_usage_entry(in_=9999)) + "\n")
    out = _claude_transcript_tokens(workdir, None)
    assert out["in"] == 5


def test_tokens_entry_without_timestamp_still_summed(fake_home):
    workdir = "/work/proj"
    _write_transcript(
        fake_home, workdir, [_usage_entry(model="claude-x", in_=11, cr=22, cw=33)]
    )
    out = _claude_transcript_tokens(workdir, None)
    assert out["in"] == 11
    # With no timestamp anywhere, latest_ctx falls back to the first-seen turn.
    assert out["ctx"] == 11 + 22 + 33
    assert out["model"] == "claude-x"


# --------------------------------------------------------------------------- #
# shared-cwd (copy) attribution — the copy-cost bug
# --------------------------------------------------------------------------- #
def test_file_in_window_birth_time_attribution():
    # Born in [10, 20): belongs here. Before 10 or at/after 20: not here.
    assert _file_in_window(15.0, 10.0, 20.0) is True
    assert _file_in_window(5.0, 10.0, 20.0) is False
    assert _file_in_window(20.0, 10.0, 20.0) is False
    # Open-ended window (no next sibling) takes everything at/after since.
    assert _file_in_window(25.0, 10.0, None) is True
    # Untimestamped file only counts in the open-ended window.
    assert _file_in_window(None, 10.0, None) is True
    assert _file_in_window(None, 10.0, 20.0) is False


def test_shared_cwd_attributes_each_copy_its_own_conversation(fake_home):
    """Two sessions share one workdir (a window + its copy). Each Claude
    conversation is its own .jsonl; shared_cwd attribution must give each
    session ONLY its own file, not the sum of both (the copy-cost bug)."""
    workdir = "/work/proj"
    orig_start = _ts_epoch("2024-01-01T00:00:00Z")
    copy_start = _ts_epoch("2024-01-01T01:00:00Z")
    # Original's conversation: born at orig_start, keeps working AFTER the copy.
    _write_transcript(
        fake_home,
        workdir,
        [
            _usage_entry(ts="2024-01-01T00:00:00Z", in_=100, out=10),
            _usage_entry(
                ts="2024-01-01T02:00:00Z", in_=100, out=10
            ),  # after copy_start
        ],
        fname="orig.jsonl",
    )
    # Copy's conversation: born at copy_start.
    _write_transcript(
        fake_home,
        workdir,
        [
            _usage_entry(ts="2024-01-01T01:00:00Z", in_=5, out=1),
        ],
        fname="copy.jsonl",
    )

    # Original: window [orig_start, copy_start) → its whole file (both turns,
    # including the post-copy one), NOT the copy's.
    orig = _claude_transcript_tokens(workdir, orig_start, copy_start, shared_cwd=True)
    assert orig["in"] == 200 and orig["out"] == 20

    # Copy: window [copy_start, None) → only its own file, NOT the original's.
    copy = _claude_transcript_tokens(workdir, copy_start, None, shared_cwd=True)
    assert copy["in"] == 5 and copy["out"] == 1


# --------------------------------------------------------------------------- #
# waiting_prompt_patterns
# --------------------------------------------------------------------------- #
@pytest.fixture
def provider():
    return ClaudeProvider()


def _any_match(patterns, text):
    return any(re.search(p, text) for p in patterns)


def test_waiting_patterns_match_numbered_selection_cursor(provider):
    pane = (
        "Do you want to proceed?\n"
        "  1. Yes\n"
        "❯ 1. Yes, and don't ask again\n"
        "  2. No\n"
    )
    assert _any_match(provider.waiting_prompt_patterns(), pane)


def test_waiting_patterns_match_cursor_regardless_of_highlighted_option(provider):
    # The cursor can sit on option 2 or 3 — still matched (\d+).
    assert _any_match(provider.waiting_prompt_patterns(), "❯ 3. Some other choice")
    assert _any_match(provider.waiting_prompt_patterns(), "❯   12.  spaced")


def test_waiting_patterns_match_permission_box(provider):
    pane = "  1. Yes\n  2. No, and tell Claude what to do differently\n"
    assert _any_match(provider.waiting_prompt_patterns(), pane)


def test_waiting_patterns_match_askuserquestion_phrases(provider):
    assert _any_match(provider.waiting_prompt_patterns(), "Type something.")
    assert _any_match(provider.waiting_prompt_patterns(), "  Chat about this instead")


def test_waiting_patterns_do_not_match_plain_idle_prompt(provider):
    # A bare idle prompt box with no numbered cursor and none of the phrases.
    idle = (
        "╭──────────────────────────────╮\n"
        "│ >                            │\n"
        "╰──────────────────────────────╯\n"
        "  ? for shortcuts\n"
    )
    assert not _any_match(provider.waiting_prompt_patterns(), idle)


def test_waiting_patterns_plain_prompt_char_not_matched(provider):
    # The plain "> " prompt (no "❯ N.") must not be treated as waiting.
    assert not _any_match(provider.waiting_prompt_patterns(), "> just typing here")
    # A bare heavy-cursor without a number must not match the numbered regex.
    assert not re.search(r"❯\s*\d+\.", "❯ do something")


# --------------------------------------------------------------------------- #
# trust_prompt / idle_prompt_pattern
# --------------------------------------------------------------------------- #
def test_trust_prompt_includes_new_project_wording(provider):
    spec = provider.trust_prompt()
    assert "Is this a project you created or one you trust?" in spec.patterns
    assert "Do you trust the files in this folder?" in spec.patterns
    assert "new MCP server" in spec.patterns
    assert spec.keystroke == b"\r"


def test_trust_prompt_matches_sample_panes(provider):
    spec = provider.trust_prompt()
    old_pane = "Do you trust the files in this folder?\n  1. Yes\n"
    new_pane = "Is this a project you created or one you trust?\n  1. Yes, proceed\n"
    assert any(p in old_pane for p in spec.patterns)
    assert any(p in new_pane for p in spec.patterns)


def test_idle_prompt_pattern(provider):
    assert (
        provider.idle_prompt_pattern() == "No, and tell Claude what to do differently"
    )


def test_waiting_patterns_not_matched_mid_line(provider):
    # The cursor pattern is anchored to line start: a "❯ 1." the agent happens
    # to be PRINTING inside a sentence must not read as a select menu (A3).
    assert not _any_match(
        provider.waiting_prompt_patterns(), "see the ❯ 1. option below"
    )
    assert not _any_match(provider.waiting_prompt_patterns(), "output: ❯ 12. twelve")


def test_waiting_patterns_plain_numbered_list_not_matched(provider):
    # A markdown-style numbered list mid-generation has no cursor glyph.
    pane = "Here is my plan:\n1. First step\n2. Second step\n3. Third step\n"
    assert not _any_match(provider.waiting_prompt_patterns(), pane)


def test_waiting_patterns_match_cursor_inside_dialog_border(provider):
    # Claude's dialogs draw a box: "│ ❯ 1. Yes …" — still matched.
    pane = (
        "╭──────────────────────────────╮\n"
        "│ Do you want to proceed?      │\n"
        "│ ❯ 1. Yes                     │\n"
        "│   2. No                      │\n"
        "╰──────────────────────────────╯\n"
    )
    assert _any_match(provider.waiting_prompt_patterns(), pane)


def test_waiting_patterns_match_dont_ask_again_option(provider):
    pane = "  1. Yes\n  2. Yes, and don't ask again this session\n  3. No\n"
    assert _any_match(provider.waiting_prompt_patterns(), pane)


# --------------------------------------------------------------------------- #
# activity markers (A2): read_activity_marker / activity_state
# --------------------------------------------------------------------------- #
from backend.providers.activity_markers import (  # noqa: E402 — grouped with their tests
    _HOOK_TAG,
    read_activity_marker,
    read_activity_marker_age,
)
from backend.providers.claude import (  # noqa: E402 — grouped with their tests
    install_activity_hooks,
)


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    d = tmp_path / "activity-markers"
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(d))
    return d


def _write_marker(marker_dir, session_name, state, ts):
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / (session_name + ".json")).write_text(
        json.dumps({"state": state, "ts": ts})
    )


def test_activity_marker_fresh_states_returned(marker_dir, provider):
    import time

    for state in ("working", "idle", "clarify"):
        _write_marker(marker_dir, "mindflock_s1", state, time.time())
        assert read_activity_marker("mindflock_s1") == state
        assert provider.activity_state("mindflock_s1") == state


def test_activity_marker_stale_is_ignored(marker_dir):
    import time

    _write_marker(marker_dir, "mindflock_s2", "working", time.time() - 7 * 3600)
    assert read_activity_marker("mindflock_s2") is None


def test_activity_marker_missing_or_garbled_is_none(marker_dir):
    assert read_activity_marker("mindflock_nope") is None
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "mindflock_bad.json").write_text("{not json")
    assert read_activity_marker("mindflock_bad") is None


def test_activity_marker_unknown_state_is_none(marker_dir):
    import time

    _write_marker(marker_dir, "mindflock_s3", "dancing", time.time())
    assert read_activity_marker("mindflock_s3") is None


def test_activity_marker_age_reflects_recent_write(marker_dir, provider):
    import time

    _write_marker(marker_dir, "mindflock_age1", "working", time.time() - 5)
    age = read_activity_marker_age("mindflock_age1")
    assert age is not None and 4 <= age <= 15
    # The provider exposes the same age (used by the web activity classifier).
    assert provider.activity_state_age("mindflock_age1") == pytest.approx(age, abs=2)


def test_activity_marker_age_none_when_absent_or_stale(marker_dir):
    import time

    assert read_activity_marker_age("mindflock_missing") is None
    # A >6h-old marker is treated as no signal (same staleness cap as the state).
    _write_marker(marker_dir, "mindflock_old", "working", time.time() - 7 * 3600)
    assert read_activity_marker_age("mindflock_old") is None


def test_activity_marker_name_is_sanitized(marker_dir):
    import time

    # A session name with shell-hostile chars maps to a safe filename.
    _write_marker(marker_dir, "a_b_c", "idle", time.time())
    assert read_activity_marker("a/b c") == "idle"


# --------------------------------------------------------------------------- #
# install_activity_hooks: settings.local.json merge
# --------------------------------------------------------------------------- #
def _read_settings(workdir):
    return json.loads((workdir / ".claude" / "settings.local.json").read_text())


def test_install_hooks_writes_all_events(tmp_path, marker_dir):
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_sess")
    data = _read_settings(wt)
    for event in (
        "Stop",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Notification",
    ):
        cmds = [h["command"] for e in data["hooks"][event] for h in e["hooks"]]
        assert any(_HOOK_TAG in c for c in cmds), event
    # PostToolUse refreshes the working marker when a tool returns, so the
    # think-before-the-next-tool stretch starts with a fresh trust window
    # instead of aging out (the "thinking reads as idle" gap).
    posttool_cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert '"working"' in posttool_cmd
    # Stop reports idle; UserPromptSubmit reports working; Notification runs
    # the stdin-inspecting command (idle-timeout skipped, otherwise clarify).
    # The marker session is resolved at fire-time ($MINDFLOCK_SESSION_NAME, else
    # tmux #{session_name}) — NOT baked in — so a shared workdir doesn't clobber.
    stop_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert '"idle"' in stop_cmd
    assert "MINDFLOCK_SESSION_NAME" in stop_cmd and "#{session_name}" in stop_cmd
    assert "mindflock_sess.json" not in stop_cmd  # session no longer baked in
    submit_cmd = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert '"working"' in submit_cmd
    notif_cmd = data["hooks"]["Notification"][0]["hooks"][0]["command"]
    assert "clarify" in notif_cmd
    assert "notification_type" in notif_cmd  # inspects the stdin payload (G1)
    assert "#{session_name}" in notif_cmd


def test_install_hooks_merges_without_clobbering_user_content(tmp_path, marker_dir):
    wt = tmp_path / "wt"
    (wt / ".claude").mkdir(parents=True)
    user = {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo user-hook"}]}]
        },
    }
    (wt / ".claude" / "settings.local.json").write_text(json.dumps(user))
    install_activity_hooks(str(wt), "mindflock_sess")
    data = _read_settings(wt)
    # User keys and user hook entries survive; ours is appended.
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "echo user-hook" in stop_cmds
    assert any(_HOOK_TAG in c for c in stop_cmds)


def test_install_hooks_reinstall_replaces_not_duplicates(tmp_path, marker_dir):
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_a")
    install_activity_hooks(str(wt), "mindflock_b")  # e.g. a second session, same dir
    data = _read_settings(wt)
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    ours = [c for c in stop_cmds if _HOOK_TAG in c]
    assert len(ours) == 1
    # Session-agnostic command: no baked session name, resolved at fire-time.
    assert "#{session_name}" in ours[0]
    assert "mindflock_a.json" not in ours[0] and "mindflock_b.json" not in ours[0]


def test_install_hooks_shared_workdir_attributes_per_session(tmp_path, marker_dir):
    # Two sessions sharing ONE workdir (in-place sessions on the same repo) must
    # each record activity under their OWN marker — the bug where a baked path
    # made the last installer's marker the sink for everyone.
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_a")
    install_activity_hooks(str(wt), "mindflock_b")
    stop_cmd = [
        h["command"]
        for e in _read_settings(wt)["hooks"]["Stop"]
        for h in e["hooks"]
        if _HOOK_TAG in h["command"]
    ][0]
    import os
    import subprocess

    for sess in ("mindflock_a", "mindflock_b"):
        subprocess.run(
            ["sh", "-c", stop_cmd],
            check=True,
            env={**os.environ, "MINDFLOCK_SESSION_NAME": sess},
        )
        assert read_activity_marker(sess) == "idle"


def test_install_hooks_tolerates_corrupt_settings(tmp_path, marker_dir):
    wt = tmp_path / "wt"
    (wt / ".claude").mkdir(parents=True)
    (wt / ".claude" / "settings.local.json").write_text("{broken json")
    install_activity_hooks(str(wt), "mindflock_sess")
    data = _read_settings(wt)  # rewritten as valid JSON with our hooks
    assert "hooks" in data


def test_install_hooks_adds_git_exclude(tmp_path, marker_dir):
    import subprocess

    wt = tmp_path / "repo"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    install_activity_hooks(str(wt), "mindflock_sess")
    exclude = (wt / ".git" / "info" / "exclude").read_text()
    assert ".claude/settings.local.json" in exclude.splitlines()
    # Idempotent: a second install doesn't duplicate the line.
    install_activity_hooks(str(wt), "mindflock_sess")
    exclude = (wt / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".claude/settings.local.json") == 1


# --------------------------------------------------------------------------- #
# Notification hook payload inspection (G1): Claude Code fires a Notification
# ~60s after the session goes idle ("Claude is waiting for your input",
# notification_type "idle_prompt"); that must NOT record clarify. Permission /
# plan / question notifications still must. The hook command is executed for
# real (sh -c) with the payload on stdin, exactly as Claude Code runs it.
# --------------------------------------------------------------------------- #
def _installed_notification_cmd(wt):
    data = _read_settings(wt)
    cmds = [
        h["command"]
        for e in data["hooks"]["Notification"]
        for h in e["hooks"]
        if _HOOK_TAG in h["command"]
    ]
    assert len(cmds) == 1
    return cmds[0]


def _run_notification_hook(cmd, payload, session="mindflock_sess"):
    import os
    import subprocess

    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    # The hook resolves its session from $MINDFLOCK_SESSION_NAME (else tmux); set
    # it so the marker is attributed correctly in the no-tmux test environment.
    env = {**os.environ, "MINDFLOCK_SESSION_NAME": session}
    subprocess.run(["sh", "-c", cmd], input=data, check=True, env=env)


def test_notification_hook_idle_timeout_writes_no_marker(tmp_path, marker_dir):
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_sess")
    _run_notification_hook(
        _installed_notification_cmd(wt),
        {
            "hook_event_name": "Notification",
            "message": "Claude is waiting for your input",
            "notification_type": "idle_prompt",
        },
    )
    assert read_activity_marker("mindflock_sess") is None


def test_notification_hook_idle_message_without_type_skipped(tmp_path, marker_dir):
    # Older Claude versions without notification_type: the message substring
    # fallback still recognises the idle-timeout notification.
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_sess")
    _run_notification_hook(
        _installed_notification_cmd(wt),
        {
            "hook_event_name": "Notification",
            "message": "Claude is waiting for your input",
        },
    )
    assert read_activity_marker("mindflock_sess") is None


def test_notification_hook_permission_writes_clarify(tmp_path, marker_dir):
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_sess")
    _run_notification_hook(
        _installed_notification_cmd(wt),
        {
            "hook_event_name": "Notification",
            "message": "Claude needs your permission to use Bash",
            "notification_type": "permission_request",
        },
    )
    assert read_activity_marker("mindflock_sess") == "clarify"


def test_notification_hook_idle_timeout_preserves_prior_clarify(tmp_path, marker_dir):
    # An unanswered permission prompt sits for 60s -> idle-timeout notification
    # must not clobber the recorded clarify (skip, don't write "idle").
    import time

    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_sess")
    _write_marker(marker_dir, "mindflock_sess", "clarify", time.time())
    _run_notification_hook(
        _installed_notification_cmd(wt),
        {
            "message": "Claude is waiting for your input",
            "notification_type": "idle_prompt",
        },
    )
    assert read_activity_marker("mindflock_sess") == "clarify"


# --------------------------------------------------------------------------- #
# Live activity signal: `claude agents --json` preferred over the hook marker.
# The subprocess is always mocked (no real claude binary is ever spawned).
# --------------------------------------------------------------------------- #
from backend.providers import claude as claude_mod  # noqa: E402
from backend.providers import thread_markers  # noqa: E402


@pytest.fixture
def live_agents(tmp_path, monkeypatch, marker_dir):
    """Isolated thread markers + a fresh agents-json cache for each test."""
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))
    monkeypatch.delenv("MINDFLOCK_DISABLE_AGENTS_JSON", raising=False)
    monkeypatch.setattr(
        claude_mod,
        "_agents_cache",
        {"at": float("-inf"), "map": {}, "good": {}, "good_at": float("-inf")},
    )
    return marker_dir


def test_activity_state_prefers_live_signal_over_marker(
    live_agents, provider, monkeypatch
):
    import time

    _write_marker(live_agents, "mindflock_live", "idle", time.time())
    monkeypatch.setattr(claude_mod, "_live_agent_state", lambda s: "working")
    assert provider.activity_state("mindflock_live") == "working"
    # The live signal is real-time, so the reported age is fresh (0) — the web
    # layer then trusts a working/clarify report without pane re-verification.
    assert provider.activity_state_age("mindflock_live") == 0.0


def test_activity_state_falls_back_to_marker_when_live_unavailable(
    live_agents, provider, monkeypatch
):
    import time

    _write_marker(live_agents, "mindflock_fb", "idle", time.time() - 5)
    monkeypatch.setattr(claude_mod, "_live_agent_state", lambda s: None)
    assert provider.activity_state("mindflock_fb") == "idle"
    age = provider.activity_state_age("mindflock_fb")
    assert age is not None and 4 <= age <= 15  # the marker's real age, not 0


def test_live_state_skipped_without_recorded_thread_id(live_agents, monkeypatch):
    # No conversation id recorded yet -> never spawn the binary; no signal.
    def boom(*a, **k):
        raise AssertionError("claude agents --json must not be spawned without an id")

    monkeypatch.setattr("subprocess.run", boom)
    assert claude_mod._live_agent_state("mindflock_noid") is None


def test_live_state_correlates_by_recorded_thread_id(
    live_agents, provider, monkeypatch
):
    import time
    from types import SimpleNamespace

    _write_marker(live_agents, "mindflock_corr", "idle", time.time())
    thread_markers.record("mindflock_corr", "sid-1234")
    payload = json.dumps(
        [
            {"sessionId": "sid-1234", "state": "working"},
            {"sessionId": "other", "state": "done"},
        ]
    ).encode()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload),
    )
    assert provider.activity_state("mindflock_corr") == "working"


@pytest.mark.parametrize(
    "run_behaviour",
    ["oserror", "timeout", "nonzero", "garbage_json", "not_a_list"],
)
def test_live_state_failures_degrade_to_marker(
    live_agents, provider, monkeypatch, run_behaviour
):
    import subprocess as _sp
    import time
    from types import SimpleNamespace

    _write_marker(live_agents, "mindflock_deg", "idle", time.time())
    thread_markers.record("mindflock_deg", "sid-9999")

    def fake_run(*a, **k):
        if run_behaviour == "oserror":
            raise OSError("no claude binary")
        if run_behaviour == "timeout":
            raise _sp.TimeoutExpired(cmd="claude", timeout=8)
        if run_behaviour == "nonzero":
            return SimpleNamespace(returncode=1, stdout=b"")
        if run_behaviour == "garbage_json":
            return SimpleNamespace(returncode=0, stdout=b"{not json")
        return SimpleNamespace(returncode=0, stdout=b'{"a": 1}')  # not a list

    monkeypatch.setattr("subprocess.run", fake_run)
    # Never raises; the marker fallback answers instead.
    assert provider.activity_state("mindflock_deg") == "idle"


def test_live_state_disabled_by_env(live_agents, provider, monkeypatch):
    import time

    monkeypatch.setenv("MINDFLOCK_DISABLE_AGENTS_JSON", "1")
    thread_markers.record("mindflock_off", "sid-0000")

    def boom(*a, **k):
        raise AssertionError("agents --json must not run when disabled")

    monkeypatch.setattr("subprocess.run", boom)
    _write_marker(live_agents, "mindflock_off", "clarify", time.time())
    assert provider.activity_state("mindflock_off") == "clarify"


def test_agents_map_throttles_within_ttl(live_agents, monkeypatch):
    # A successful probe caches; a second call within the TTL must NOT re-spawn.
    from types import SimpleNamespace

    calls = {"n": 0}

    def run(*a, **k):
        calls["n"] += 1
        return SimpleNamespace(
            returncode=0, stdout=b'[{"sessionId":"s","state":"working"}]'
        )

    monkeypatch.setattr("subprocess.run", run)
    assert claude_mod._agents_state_map() == {"s": "working"}
    assert claude_mod._agents_state_map() == {"s": "working"}
    assert calls["n"] == 1


def test_agents_map_grace_serves_last_good_on_failure(live_agents, monkeypatch):
    # A transient probe failure must reuse the last good live map (grace) rather
    # than wiping every session's live signal to marker-fallback on one hiccup.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=b'[{"sessionId":"sid-1","state":"working"}]'
        ),
    )
    assert claude_mod._agents_state_map() == {"sid-1": "working"}

    claude_mod._agents_cache["at"] = float("-inf")  # force a re-fetch

    def boom(*a, **k):
        raise OSError("claude binary hiccup")

    monkeypatch.setattr("subprocess.run", boom)
    assert claude_mod._agents_state_map() == {"sid-1": "working"}  # last good


def test_agents_map_grace_expires_to_empty(live_agents, monkeypatch):
    # Once the last good reading is older than the grace horizon, a failing probe
    # yields {} (full marker fallback).
    claude_mod._agents_cache.update({"good": {"sid-x": "working"}, "good_at": 0.0})
    claude_mod._agents_cache["at"] = float("-inf")

    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr("subprocess.run", boom)
    assert claude_mod._agents_state_map() == {}


def test_agents_map_successful_empty_probe_overrides_stale_good(
    live_agents, monkeypatch
):
    # A clean probe returning [] means genuinely idle — it must win over a stale
    # last-good map, never be "resurrected" by the grace path.
    import time
    from types import SimpleNamespace

    claude_mod._agents_cache.update(
        {"good": {"sid-1": "working"}, "good_at": time.time()}
    )
    claude_mod._agents_cache["at"] = float("-inf")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"[]")
    )
    assert claude_mod._agents_state_map() == {}


def test_agents_map_inflight_probe_blocks_concurrent_spawn(live_agents, monkeypatch):
    # The TTL (2s) is shorter than the probe timeout (8s): while one poller's
    # probe is still outstanding, later pollers land on an expired cache. They
    # must serve the stale cached map, NOT each spawn another concurrent
    # `claude agents --json` (thundering herd).
    claude_mod._agents_cache.update(
        {"map": {"sid-1": "working"}, "at": float("-inf"), "inflight": True}
    )

    def boom(*a, **k):
        raise AssertionError("must not spawn a second probe while one is in flight")

    monkeypatch.setattr("subprocess.run", boom)
    assert claude_mod._agents_state_map() == {"sid-1": "working"}


def test_agents_map_inflight_flag_clears_on_success_and_failure(
    live_agents, monkeypatch
):
    from types import SimpleNamespace

    # Success path releases the in-flight claim...
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"[]")
    )
    claude_mod._agents_state_map()
    assert claude_mod._agents_cache["inflight"] is False
    # ...and so does a failing probe — a stuck flag would disable probing forever.
    claude_mod._agents_cache["at"] = float("-inf")

    def boom(*a, **k):
        raise OSError("claude binary hiccup")

    monkeypatch.setattr("subprocess.run", boom)
    claude_mod._agents_state_map()
    assert claude_mod._agents_cache["inflight"] is False


def test_map_agents_entry_normalizes_states():
    m = claude_mod._map_agents_entry
    # Background sessions carry ``state``.
    assert m({"state": "working"}) == "working"
    assert m({"state": "blocked"}) == "clarify"
    assert m({"state": "done"}) == "idle"
    # Interactive sessions carry ``status``.
    assert m({"status": "busy"}) == "working"
    assert m({"status": "waiting"}) == "clarify"
    assert m({"status": "idle"}) == "idle"
    # A waitingFor reason means blocked on the human.
    assert m({"waitingFor": "permission prompt"}) == "clarify"
    # Anything unrecognized -> None (fall back rather than guess).
    assert m({"state": "mystery"}) is None
    assert m({}) is None


def test_notification_hook_garbled_stdin_falls_back_to_clarify(tmp_path, marker_dir):
    # Unparseable payload -> the pre-G1 behavior (clarify), so a genuine
    # "needs input" from an unknown Claude version is never silently dropped.
    wt = tmp_path / "wt"
    wt.mkdir()
    install_activity_hooks(str(wt), "mindflock_sess")
    _run_notification_hook(_installed_notification_cmd(wt), b"{not json")
    assert read_activity_marker("mindflock_sess") == "clarify"


def test_install_hooks_missing_workdir_is_noop(tmp_path, marker_dir):
    # Never raises for a workdir that doesn't exist.
    install_activity_hooks(str(tmp_path / "nope"), "mindflock_sess")
    install_activity_hooks("", "mindflock_sess")
    install_activity_hooks(str(tmp_path), "")


# --------------------------------------------------------------------------- #
# pre_trust_workdir (F2): seed projects.<path>.hasTrustDialogAccepted in the
# Claude user config so the first run never stalls at the trust gate. Fully
# mocked — conftest points MINDFLOCK_CLAUDE_JSON at a tmp file, so the real
# ~/.claude.json is never read or written.
# --------------------------------------------------------------------------- #
from backend.providers.claude import pre_trust_workdir  # noqa: E402


@pytest.fixture
def trust_env(tmp_path, monkeypatch):
    """A workdir + the tmp config file pre_trust_workdir will write."""
    cfg = tmp_path / "claude-user.json"
    monkeypatch.setenv("MINDFLOCK_CLAUDE_JSON", str(cfg))
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt, cfg


def test_pre_trust_creates_config_and_entry(trust_env):
    import os

    wt, cfg = trust_env
    pre_trust_workdir(str(wt))
    data = json.loads(cfg.read_text())
    entry = data["projects"][os.path.realpath(str(wt))]
    assert entry["hasTrustDialogAccepted"] is True


def test_pre_trust_merges_without_clobbering(trust_env):
    import os

    wt, cfg = trust_env
    cfg.write_text(
        json.dumps(
            {
                "numStartups": 42,
                "projects": {
                    "/some/other": {
                        "hasTrustDialogAccepted": False,
                        "allowedTools": ["x"],
                    },
                },
            }
        )
    )
    pre_trust_workdir(str(wt))
    data = json.loads(cfg.read_text())
    # Every pre-existing key/entry survives verbatim.
    assert data["numStartups"] == 42
    assert data["projects"]["/some/other"] == {
        "hasTrustDialogAccepted": False,
        "allowedTools": ["x"],
    }
    assert data["projects"][os.path.realpath(str(wt))]["hasTrustDialogAccepted"] is True


def test_pre_trust_preserves_other_project_keys(trust_env):
    import os

    wt, cfg = trust_env
    real = os.path.realpath(str(wt))
    cfg.write_text(
        json.dumps(
            {
                "projects": {
                    real: {"hasTrustDialogAccepted": False, "mcpServers": {"a": 1}}
                },
            }
        )
    )
    pre_trust_workdir(str(wt))
    entry = json.loads(cfg.read_text())["projects"][real]
    assert entry == {"hasTrustDialogAccepted": True, "mcpServers": {"a": 1}}


def test_pre_trust_unparseable_config_left_untouched(trust_env):
    wt, cfg = trust_env
    cfg.write_text("{definitely not json")
    pre_trust_workdir(str(wt))  # must not raise
    assert cfg.read_text() == "{definitely not json"


def test_pre_trust_already_trusted_no_rewrite(trust_env):
    import os

    wt, cfg = trust_env
    real = os.path.realpath(str(wt))
    original = json.dumps({"projects": {real: {"hasTrustDialogAccepted": True}}})
    cfg.write_text(original)
    before = cfg.stat().st_mtime_ns
    pre_trust_workdir(str(wt))
    assert cfg.read_text() == original
    assert cfg.stat().st_mtime_ns == before


def test_pre_trust_missing_workdir_is_noop(trust_env, tmp_path):
    _, cfg = trust_env
    pre_trust_workdir(str(tmp_path / "nope"))
    pre_trust_workdir("")
    assert not cfg.exists()


def test_pre_trust_claude_config_dir_also_seeded(tmp_path, monkeypatch):
    # Without the test override, an explicit CLAUDE_CONFIG_DIR is seeded
    # alongside ~/.claude.json (HOME is pointed at a tmp dir — never real).
    import os

    monkeypatch.delenv("MINDFLOCK_CLAUDE_JSON", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg_dir = tmp_path / "cfgdir"
    cfg_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    wt = tmp_path / "wt"
    wt.mkdir()
    pre_trust_workdir(str(wt))
    real = os.path.realpath(str(wt))
    for cfg in (cfg_dir / ".claude.json", home / ".claude.json"):
        data = json.loads(cfg.read_text())
        assert data["projects"][real]["hasTrustDialogAccepted"] is True


# --------------------------------------------------------------------------- #
# remove_trust_entry (G3): GC the projects entry pre_trust_workdir seeded when
# the worktree is deleted. Fully mocked via MINDFLOCK_CLAUDE_JSON (trust_env);
# the managed worktrees root is pointed at tmp_path so ownership checks are
# exercised without touching ~/.mindflock.
# --------------------------------------------------------------------------- #
from backend.providers.claude import remove_trust_entry  # noqa: E402


@pytest.fixture
def owned_root(tmp_path, monkeypatch):
    """Make tmp_path count as the managed ~/.mindflock/worktrees tree."""
    import backend.session.git.worktree as wt_mod

    monkeypatch.setattr(wt_mod, "get_worktree_directory", lambda: str(tmp_path))
    return tmp_path


def test_trust_add_then_remove_roundtrip(trust_env, owned_root):
    import os

    wt, cfg = trust_env
    real = os.path.realpath(str(wt))
    pre_trust_workdir(str(wt))
    assert real in json.loads(cfg.read_text())["projects"]
    remove_trust_entry(str(wt))  # dir still exists but sits under the managed root
    data = json.loads(cfg.read_text())
    assert real not in data.get("projects", {})


def test_trust_remove_preserves_foreign_entries(trust_env, owned_root):
    import os

    wt, cfg = trust_env
    real = os.path.realpath(str(wt))
    cfg.write_text(
        json.dumps(
            {
                "numStartups": 7,
                "projects": {
                    real: {"hasTrustDialogAccepted": True},
                    "/some/other": {
                        "hasTrustDialogAccepted": True,
                        "allowedTools": ["x"],
                    },
                },
            }
        )
    )
    remove_trust_entry(str(wt))
    data = json.loads(cfg.read_text())
    assert real not in data["projects"]
    assert data["numStartups"] == 7
    assert data["projects"]["/some/other"] == {
        "hasTrustDialogAccepted": True,
        "allowedTools": ["x"],
    }


def test_trust_remove_unparseable_config_is_noop(trust_env, owned_root):
    wt, cfg = trust_env
    cfg.write_text("{definitely not json")
    remove_trust_entry(str(wt))  # must not raise
    assert cfg.read_text() == "{definitely not json"


def test_trust_remove_refuses_live_dir_outside_managed_root(trust_env, monkeypatch):
    # A directory that still exists OUTSIDE ~/.mindflock/worktrees (e.g. an
    # in-place session in the user's own repo) is never ours to untrust.
    import os

    import backend.session.git.worktree as wt_mod

    wt, cfg = trust_env
    real = os.path.realpath(str(wt))
    monkeypatch.setattr(
        wt_mod, "get_worktree_directory", lambda: "/nonexistent/worktrees"
    )
    original = json.dumps({"projects": {real: {"hasTrustDialogAccepted": True}}})
    cfg.write_text(original)
    remove_trust_entry(str(wt))
    assert cfg.read_text() == original


def test_trust_remove_deleted_dir_outside_root_is_removed(trust_env, monkeypatch):
    # Once the workdir is gone (worktree rmtree'd), the entry is dead weight
    # and may be GC'd even without the managed-root pedigree.
    import os
    import shutil

    import backend.session.git.worktree as wt_mod

    wt, cfg = trust_env
    real = os.path.realpath(str(wt))
    monkeypatch.setattr(
        wt_mod, "get_worktree_directory", lambda: "/nonexistent/worktrees"
    )
    cfg.write_text(json.dumps({"projects": {real: {"hasTrustDialogAccepted": True}}}))
    shutil.rmtree(str(wt))
    remove_trust_entry(str(wt))
    assert real not in json.loads(cfg.read_text()).get("projects", {})


def test_trust_remove_missing_file_or_entry_is_noop(trust_env, owned_root):
    wt, cfg = trust_env
    remove_trust_entry(str(wt))  # no config file at all -> nothing created
    assert not cfg.exists()
    cfg.write_text(json.dumps({"projects": {"/some/other": {}}}))
    before = cfg.read_text()
    remove_trust_entry(str(wt))  # entry absent -> untouched
    assert cfg.read_text() == before
    remove_trust_entry("")  # empty workdir -> no-op


def test_launch_paths_invoke_pre_trust(tmp_path, monkeypatch):
    # Both the launcher-less launch path and the provider hook-install path
    # (used by the web relaunch) pre-trust the workdir.
    from backend.providers import claude as mod

    seen = []
    monkeypatch.setattr(mod, "pre_trust_workdir", seen.append)
    monkeypatch.setattr(mod, "install_activity_hooks", lambda wt, name: None)
    wt = tmp_path / "wt"
    wt.mkdir()
    provider = ClaudeProvider()
    provider.install_activity_hooks(str(wt), "mindflock_sess")
    assert seen == [str(wt)]


# --------------------------------------------------------------------------- #
# Latest-turn snippet: _snippet_from_text / _entry_text / _claude_last_turn_snippet
# --------------------------------------------------------------------------- #
from backend.providers.claude import (  # noqa: E402
    _claude_last_turn_snippet,
    _entry_text,
    _last_turn_cache_put,
    _snippet_from_text,
)
from backend.providers import claude as _claude_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_snippet_cache():
    _claude_mod._LAST_TURN_CACHE.clear()
    yield
    _claude_mod._LAST_TURN_CACHE.clear()


def test_snippet_from_text_none_and_empty():
    assert _snippet_from_text("") is None
    assert _snippet_from_text(None) is None


def test_snippet_from_text_skips_fence_tag_and_interrupt_lines():
    # Leading fence-delimiter, <tag> and [Request interrupted lines are skipped;
    # the first ordinary line after them is used.
    text = (
        "```\n"
        "<system-reminder>ignore me</system-reminder>\n"
        "[Request interrupted by user]\n"
        "The actual answer is here.\n"
    )
    assert _snippet_from_text(text) == "The actual answer is here."


def test_snippet_from_text_strips_markdown_prefixes_and_emphasis():
    assert _snippet_from_text("## **Heading** text") == "Heading text"
    assert _snippet_from_text("- a list item") == "a list item"
    assert _snippet_from_text("> quoted line") == "quoted line"


def test_snippet_from_text_skips_line_that_is_only_emphasis():
    # A line that reduces to empty after stripping emphasis/backticks is skipped,
    # and the next meaningful line is used.
    assert _snippet_from_text("``\nreal content\n") == "real content"


def test_snippet_from_text_truncates_to_limit_with_ellipsis():
    long = "x" * 200
    out = _snippet_from_text(long, limit=20)
    assert out is not None
    assert len(out) == 20
    assert out.endswith("…")


def test_snippet_from_text_returns_none_when_nothing_readable():
    assert _snippet_from_text("```\n<tag>\n```\n") is None


def test_entry_text_shape_guards():
    assert _entry_text("not a dict") is None
    assert _entry_text({"type": "system"}) is None  # wrong type
    assert _entry_text({"type": "assistant", "isMeta": True}) is None
    assert _entry_text({"type": "user", "message": "not a dict"}) is None


def test_entry_text_string_content():
    obj = {"type": "user", "message": {"content": "hello there"}}
    assert _entry_text(obj) == "hello there"


def test_entry_text_list_content_first_text_block():
    obj = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "x"},
                {"type": "text", "text": "the reply"},
            ]
        },
    }
    assert _entry_text(obj) == "the reply"


def test_entry_text_list_content_without_text_is_none():
    obj = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "x"}]},
    }
    assert _entry_text(obj) is None


def test_last_turn_snippet_no_transcripts_returns_none(fake_home):
    assert _claude_last_turn_snippet("/work/proj") is None


def test_last_turn_snippet_reads_newest_conversational_turn(fake_home):
    workdir = "/work/proj"
    _write_transcript(
        fake_home,
        workdir,
        [
            {"type": "user", "message": {"content": "please fix the parser"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Fixed the parser."}]},
            },
        ],
    )
    assert _claude_last_turn_snippet(workdir) == "Fixed the parser."


def test_last_turn_snippet_provider_method_delegates(fake_home):
    workdir = "/work/proj"
    _write_transcript(
        fake_home,
        workdir,
        [{"type": "assistant", "message": {"content": "done"}}],
    )
    provider = ClaudeProvider()
    assert provider.last_turn_snippet("mindflock_x", workdir) == "done"


def test_last_turn_snippet_skips_bad_json_and_non_conversational(fake_home):
    workdir = "/work/proj"
    proj = fake_home / ".claude" / "projects" / _encode(workdir)
    proj.mkdir(parents=True)
    # The scan reads NEWEST-first (reversed), so the good turn is written FIRST
    # and the noise (blank line, bad JSON, tool-only turn) is written AFTER it,
    # forcing the reversed loop to skip past all the noise to reach the answer.
    with open(proj / "s.jsonl", "w") as f:
        f.write(
            json.dumps({"type": "assistant", "message": {"content": "the answer"}})
            + "\n"
        )
        f.write(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}
            )
            + "\n"
        )
        f.write("{not valid json\n")  # skipped (ValueError)
        f.write("\n")  # blank line skipped
    assert _claude_last_turn_snippet(workdir) == "the answer"


def test_last_turn_snippet_rearms_ttl_when_file_unchanged(fake_home):
    workdir = "/work/proj"
    _write_transcript(
        fake_home, workdir, [{"type": "assistant", "message": {"content": "stable"}}]
    )
    assert _claude_last_turn_snippet(workdir) == "stable"
    # Expire the TTL but leave the file untouched: the sig matches, so the cached
    # snippet is re-served (and its TTL re-armed) without re-parsing.
    _claude_mod._LAST_TURN_CACHE[workdir]["checked"] = 0.0
    assert _claude_last_turn_snippet(workdir) == "stable"
    assert _claude_mod._LAST_TURN_CACHE[workdir]["checked"] > 0.0


def test_last_turn_snippet_skips_unstattable_transcript(fake_home):
    import os

    workdir = "/work/proj"
    proj = fake_home / ".claude" / "projects" / _encode(workdir)
    proj.mkdir(parents=True)
    # A dangling .jsonl symlink is listed but stat() raises -> skipped; the real
    # transcript still provides the snippet.
    os.symlink(str(proj / "gone"), str(proj / "broken.jsonl"))
    with open(proj / "real.jsonl", "w") as f:
        f.write(
            json.dumps({"type": "assistant", "message": {"content": "kept"}}) + "\n"
        )
    assert _claude_last_turn_snippet(workdir) == "kept"


def test_last_turn_snippet_cached_within_ttl(fake_home, monkeypatch):
    workdir = "/work/proj"
    _write_transcript(
        fake_home, workdir, [{"type": "assistant", "message": {"content": "first"}}]
    )
    assert _claude_last_turn_snippet(workdir) == "first"
    # Change the file content, but the ~10s TTL means the cached snippet is
    # returned without a re-read.
    _write_transcript(
        fake_home,
        workdir,
        [{"type": "assistant", "message": {"content": "second"}}],
        fname="s.jsonl",
    )
    assert _claude_last_turn_snippet(workdir) == "first"  # still cached


def test_last_turn_snippet_reads_only_tail_of_large_file(fake_home):
    workdir = "/work/proj"
    proj = fake_home / ".claude" / "projects" / _encode(workdir)
    proj.mkdir(parents=True)
    # A file larger than the tail-read window: only the tail is scanned, so the
    # snippet comes from a turn near the END.
    padding = {"type": "assistant", "message": {"content": "x" * 500}}
    lines = [json.dumps(padding) for _ in range(600)]  # ~ >128KiB total
    lines.append(json.dumps({"type": "assistant", "message": {"content": "the tail"}}))
    with open(proj / "s.jsonl", "w") as f:
        f.write("\n".join(lines) + "\n")
    assert _claude_last_turn_snippet(workdir) == "the tail"


def test_last_turn_cache_put_evicts_when_full():
    _claude_mod._LAST_TURN_CACHE.clear()
    maxn = _claude_mod._LAST_TURN_CACHE_MAX
    for i in range(maxn):
        _last_turn_cache_put(
            "wd-%d" % i, {"checked": 0.0, "sig": None, "snippet": None}
        )
    assert len(_claude_mod._LAST_TURN_CACHE) == maxn
    # One more distinct workdir triggers the half-eviction.
    _last_turn_cache_put("wd-overflow", {"checked": 0.0, "sig": None, "snippet": None})
    assert len(_claude_mod._LAST_TURN_CACHE) <= maxn // 2 + 1
    assert "wd-overflow" in _claude_mod._LAST_TURN_CACHE


# --------------------------------------------------------------------------- #
# Per-WINDOW transcript selection: sibling sessions sharing one directory all
# write into the same projects/<cwd-slug>/ dir, so "newest .jsonl" is whoever
# typed last. The window's thread marker names its own conversation.
# --------------------------------------------------------------------------- #
@pytest.fixture
def two_windows(fake_home, tmp_path, monkeypatch):
    """A workdir holding two conversations, one per window, with the SECOND
    (``other``) written last so mtime alone would always pick it."""
    import os

    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))
    workdir = "/work/shared"
    _write_transcript(
        fake_home,
        workdir,
        [{"type": "user", "message": {"content": "mine: fix the parser"}}],
        fname="aaaa-1111.jsonl",
    )
    _write_transcript(
        fake_home,
        workdir,
        [{"type": "user", "message": {"content": "theirs: bump the deps"}}],
        fname="bbbb-2222.jsonl",
    )
    proj = fake_home / ".claude" / "projects" / _encode(workdir)
    os.utime(proj / "aaaa-1111.jsonl", (1000, 1000))  # older than its sibling
    os.utime(proj / "bbbb-2222.jsonl", (2000, 2000))
    thread_markers.record("mindflock_mine", "aaaa-1111")
    thread_markers.record("mindflock_theirs", "bbbb-2222")
    return workdir, proj


def test_session_transcript_prefers_this_windows_thread_marker(two_windows):
    workdir, proj = two_windows
    # Path-keyed selection always lands on the sibling that wrote last...
    assert _claude_mod._newest_transcript(workdir)[2] == str(proj / "bbbb-2222.jsonl")
    # ...while each window gets its OWN conversation.
    assert _claude_mod._session_transcript(workdir, "mindflock_mine")[2] == str(
        proj / "aaaa-1111.jsonl"
    )
    assert _claude_mod._session_transcript(workdir, "mindflock_theirs")[2] == str(
        proj / "bbbb-2222.jsonl"
    )


def test_session_transcript_falls_back_without_a_usable_marker(two_windows):
    workdir, proj = two_windows
    newest = str(proj / "bbbb-2222.jsonl")
    # No session name, no marker for the name, and a marker naming a file that
    # is gone all fall back to the newest transcript.
    assert _claude_mod._session_transcript(workdir, "")[2] == newest
    assert _claude_mod._session_transcript(workdir, "mindflock_hookless")[2] == newest
    (proj / "aaaa-1111.jsonl").unlink()
    assert _claude_mod._session_transcript(workdir, "mindflock_mine")[2] == newest


def test_last_prompt_snippet_is_per_window_not_per_workdir(two_windows):
    workdir, _ = two_windows
    provider = ClaudeProvider()
    # Both calls happen inside the cache TTL: the session name is part of the
    # cache key, so the first window's answer can't be served to the second.
    assert provider.last_prompt_snippet("mindflock_mine", workdir) == (
        "mine: fix the parser"
    )
    assert provider.last_prompt_snippet("mindflock_theirs", workdir) == (
        "theirs: bump the deps"
    )
    assert provider.last_prompt_full("mindflock_mine", workdir) == (
        "mine: fix the parser"
    )
    assert provider.last_turn_snippet("mindflock_mine", workdir) == (
        "mine: fix the parser"
    )
    assert provider.find_prompt_full("mindflock_mine", workdir, "mine: fix") == (
        "mine: fix the parser"
    )
    # The other window's prompt is not reachable from this window.
    assert provider.find_prompt_full("mindflock_mine", workdir, "theirs: bump") is None


# --------------------------------------------------------------------------- #
# Queued prompts: a message typed while the turn is still running is filed as
# {"type": "queue-operation", "operation": "enqueue", "content": ...} and is
# NEVER re-filed as a "user" entry, so a reader that only knows "user" loses it
# permanently.
# --------------------------------------------------------------------------- #
def _enqueued(text: str) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def test_last_prompt_sees_a_prompt_typed_mid_turn(fake_home):
    workdir = "/work/queued"
    _write_transcript(
        fake_home,
        workdir,
        [
            {"type": "user", "message": {"content": "the first thing I asked"}},
            {"type": "assistant", "message": {"content": "working on it"}},
            _enqueued("actually also make the menu bigger"),
        ],
    )
    provider = ClaudeProvider()
    assert provider.last_prompt_snippet("s", workdir) == (
        "actually also make the menu bigger"
    )
    assert provider.last_prompt_full("s", workdir) == (
        "actually also make the menu bigger"
    )


def test_find_prompt_full_expands_a_queued_prompt(fake_home):
    """The pinned line is width-truncated by the TUI; expanding it looks the
    body up by prefix. Queued prompts used to return null here, so clicking
    the arrow re-showed the same truncated text."""
    workdir = "/work/queued2"
    long_prompt = (
        "I also think i want the new menu to be much larger so i dont have to "
        "scroll. Also make the more options and launch flags open by default"
    )
    _write_transcript(fake_home, workdir, [_enqueued(long_prompt)])
    provider = ClaudeProvider()
    got = provider.find_prompt_full(
        "s", workdir, "I also think i want the new menu to be much larger…"
    )
    assert got == long_prompt


def test_queue_removals_are_not_second_copies_of_the_prompt(fake_home):
    """Each queued message is enqueued and later removed with the SAME text;
    only the enqueue is the human speaking."""
    workdir = "/work/queued3"
    _write_transcript(
        fake_home,
        workdir,
        [
            _enqueued("do the thing"),
            {
                "type": "queue-operation",
                "operation": "remove",
                "content": "do the thing",
            },
            {"type": "assistant", "message": {"content": "done"}},
        ],
    )
    from backend.providers.claude import _entry_text

    assert _entry_text(_enqueued("do the thing")) == "do the thing"
    assert (
        _entry_text(
            {
                "type": "queue-operation",
                "operation": "remove",
                "content": "do the thing",
            }
        )
        is None
    )
    # The newest human prompt is still the enqueue, not the assistant reply.
    assert ClaudeProvider().last_prompt_snippet("s", workdir) == "do the thing"


def test_empty_queue_content_is_not_a_prompt(fake_home):
    from backend.providers.claude import _entry_text

    assert _entry_text(_enqueued("   ")) is None
    assert _entry_text({"type": "queue-operation", "operation": "enqueue"}) is None
