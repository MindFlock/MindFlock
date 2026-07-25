"""Parity snapshots that PIN the current agent-launch behaviour.

These exist so launch-path refactors can't silently drift: the launch command
strings, the generated ``.mindflock_launch.sh`` script, the exit-code policy
and the ``sh -c`` wrapper are a behavioural contract (resume, cache env all
depend on the exact bytes). Any drift fails here first.

Pure-Python: no tmux, no git, no network.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
from pathlib import Path

import pytest

from backend import providers
from backend.providers.claude import claude_launch_command
from backend.session import provisioned
from backend.session.instance import _format_rfc822

# --------------------------------------------------------------------------- #
# claude_launch_command — the plain/assistant launch string builder
# --------------------------------------------------------------------------- #
SEED = ' "$(cat .mindflock_prompt.md)"'


@pytest.mark.parametrize(
    "resume,skip,seed,expected",
    [
        (False, False, "", "claude"),
        (False, True, "", "claude --dangerously-skip-permissions"),
        (False, False, SEED, 'claude "$(cat .mindflock_prompt.md)"'),
        (
            True,
            False,
            "",
            "claude --continue || { sleep 3; claude --continue; } || "
            "{ echo '[mindflock] resume failed twice; starting a fresh session"
            " WITHOUT re-sending the task prompt'; claude; }",
        ),
        # A resume NEVER re-sends the seed (a failed --continue can't be told
        # apart from a transient error; re-seeding restarts the whole task) —
        # it retries once, then falls back to a plain unseeded launch.
        (
            True,
            True,
            SEED,
            "claude --dangerously-skip-permissions --continue || "
            "{ sleep 3; claude --dangerously-skip-permissions --continue; } || "
            "{ echo '[mindflock] resume failed twice; starting a fresh session"
            " WITHOUT re-sending the task prompt'; "
            "claude --dangerously-skip-permissions; }",
        ),
    ],
)
def test_claude_launch_command_matrix(resume, skip, seed, expected):
    got = claude_launch_command(
        "claude", resume=resume, skip_permissions=skip, seed=seed
    )
    assert got == expected


def test_claude_launch_command_resume_by_thread_id():
    got = claude_launch_command("claude", resume=True, thread_id="abc-123")
    assert got.startswith("claude --resume abc-123 || ")
    assert "--continue" not in got


# --------------------------------------------------------------------------- #
# claude_launch_command — per-session launch flags (shell interpolation is a
# security boundary: tokens land in a tmux shell command and must be quoted).
# --------------------------------------------------------------------------- #
def test_claude_launch_command_default_is_byte_identical_to_no_args():
    # The launch_args default is (): omitting it must produce the exact same
    # bytes as passing an empty tuple (no drift for the common no-flags case).
    assert claude_launch_command("claude", resume=False) == claude_launch_command(
        "claude", resume=False, launch_args=()
    )
    assert claude_launch_command("claude", resume=False, launch_args=()) == "claude"


def test_claude_launch_command_flags_precede_skip_permissions():
    # launch_args are inserted on the base executable BEFORE the
    # --dangerously-skip-permissions trust flag.
    got = claude_launch_command(
        "claude", resume=False, skip_permissions=True, launch_args=("--verbose",)
    )
    assert got == "claude --verbose --dangerously-skip-permissions"


def test_claude_launch_command_flags_are_shlex_quoted():
    got = claude_launch_command(
        "claude", resume=False, launch_args=("--label=a b", "--x")
    )
    assert got == "claude '--label=a b' --x"


def test_claude_launch_command_flags_present_in_resume_chain():
    # Every relaunch in the resume fallback chain (retry + fresh) carries the
    # flags — a resume must not silently drop them.
    got = claude_launch_command("claude", resume=True, launch_args=("--verbose",))
    assert got.startswith("claude --verbose --continue || ")
    # The base "claude --verbose" appears for the retry and the fresh fallback.
    assert got.count("claude --verbose") == 3


# --------------------------------------------------------------------------- #
# provider matching — a bare/empty program resolves to the claude provider.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "program,is_claude",
    [
        ("", True),
        ("claude", True),
        ("/usr/local/bin/claude", True),
        ("aider", False),
        ("aider --model x", False),
        ("claude-foo", False),  # basename equality, not prefix
        ("codex", False),
    ],
)
def test_claude_provider_matches(program, is_claude):
    provider = providers.get("claude")
    assert provider is not None
    assert provider.matches(program) is is_claude


# --------------------------------------------------------------------------- #
# write_launcher — the full generated launcher script (golden compare)
# --------------------------------------------------------------------------- #
_DATA = Path(__file__).parent / "data"
_WORKDIR_PH = "/WORKDIR"


def _render_launcher(tmp_path, prompt, program, skip) -> str:
    path = provisioned.write_launcher(
        str(tmp_path), prompt, program=program, skip_permissions=skip
    )
    txt = Path(path).read_text(encoding="utf-8")
    abs_dir = os.path.abspath(str(tmp_path))
    # The dir appears shlex-quoted in `cd <dir>` / `exec bash -ilc`.
    txt = txt.replace(shlex.quote(abs_dir), shlex.quote(_WORKDIR_PH))
    txt = txt.replace(abs_dir, _WORKDIR_PH)
    return txt


@pytest.mark.parametrize(
    "golden,prompt,program,skip",
    [
        ("launcher_claude_prompt_skip.golden.sh", "Do the thing", "claude", True),
        ("launcher_claude_noprompt_skip.golden.sh", "", "claude", True),
        ("launcher_claude_prompt_noskip.golden.sh", "Do the thing", "claude", False),
        ("launcher_custom_prog_prompt.golden.sh", "Do the thing", "aider --foo", True),
    ],
)
def test_write_launcher_golden(tmp_path, golden, prompt, program, skip):
    expected = (_DATA / golden).read_text(encoding="utf-8")
    got = _render_launcher(tmp_path, prompt, program, skip)
    assert got == expected


def test_write_launcher_cache_env_generalized(tmp_path):
    """The TESTMON_ENV export is a loop over cache env vars:
    None -> the default, {} -> no export, a dict -> one sorted export
    per key (values shell-quoted)."""
    for d in ("l", "n", "m"):
        (tmp_path / d).mkdir()

    default = Path(
        provisioned.write_launcher(str(tmp_path / "l"), "", cache_env=None)
    ).read_text()
    assert "export TESTMON_ENV=shared\n" in default

    none_env = Path(
        provisioned.write_launcher(str(tmp_path / "n"), "", cache_env={})
    ).read_text()
    assert "export TESTMON_ENV" not in none_env

    multi = Path(
        provisioned.write_launcher(
            str(tmp_path / "m"),
            "",
            cache_env={"ZED": "z", "CACHE_KEY": "a b"},
        )
    ).read_text()
    # (The inner script is shlex-quoted into the outer `bash -ilc` wrapper, so
    # the quoted value's single quotes are escaped in the file bytes; assert on
    # the stable fragments instead of exact quoting.)
    assert "export ZED=z\n" in multi
    assert "export CACHE_KEY=" in multi and "a b" in multi
    assert multi.index("export CACHE_KEY=") < multi.index("export ZED=")


def test_write_launcher_interpolates_launch_args(tmp_path):
    """Per-session launch flags are appended to the launch program (shell-quoted)
    and thus appear in both the fresh and the resume line of the generated
    launcher."""
    (tmp_path / "a").mkdir()
    with_args = Path(
        provisioned.write_launcher(
            str(tmp_path / "a"),
            "",
            program="claude",
            skip_permissions=True,
            launch_args=("--verbose", "--label=a b"),
        )
    ).read_text()
    # The flag and the space-containing value survive into the script bytes.
    assert "--verbose" in with_args
    assert "a b" in with_args
    # It rides on the launch program, before --dangerously-skip-permissions.
    assert with_args.index("--verbose") < with_args.index(
        "--dangerously-skip-permissions"
    )


def test_write_launcher_empty_launch_args_is_byte_identical(tmp_path):
    # The launch_args default is (): omitting it and passing an empty tuple must
    # generate the same launcher bytes (no drift for the no-flags case).
    (tmp_path / "d").mkdir()
    (tmp_path / "e").mkdir()
    default = (
        Path(provisioned.write_launcher(str(tmp_path / "d"), "", program="claude"))
        .read_text()
        .replace(str(tmp_path / "d"), "WD")
    )
    empty = (
        Path(
            provisioned.write_launcher(
                str(tmp_path / "e"), "", program="claude", launch_args=()
            )
        )
        .read_text()
        .replace(str(tmp_path / "e"), "WD")
    )
    assert default == empty


# --------------------------------------------------------------------------- #
# Exit-code policy + the sh -c exit-marker wrapper.
# --------------------------------------------------------------------------- #
def test_exit_policy_and_wrapper():
    from backend.web import server

    assert server._is_natural_exit(0) is True
    assert server._is_natural_exit(130) is True
    for code in (1, 137, 143, None):
        assert server._is_natural_exit(code) is False

    wrapped = server._wrap_launch_cmd("claude", "mindflock_demo")
    # rm stale marker; run cmd; record exit code to the same marker.
    assert wrapped.startswith("rm -f ")
    assert "; claude; echo $? > " in wrapped
    marker = str(server._exit_marker_path("mindflock_demo"))
    assert shlex.quote(marker) in wrapped


# --------------------------------------------------------------------------- #
# _format_rfc822 — pause commit-message time formatting (Go time.RFC822 parity)
#
# Layout "02 Jan 06 15:04 MST": two-digit day, three-letter month, two-digit
# year, HH:MM, then the zone. A named zone renders its abbreviation; a synthetic
# fixed-offset name ("UTC-05:00") renders Go's numeric offset instead. Datetimes
# are constructed with explicit tzinfo so the output is deterministic across the
# host's local zone.
# --------------------------------------------------------------------------- #
def test_format_rfc822_utc_named_zone():
    t = dt.datetime(2026, 1, 2, 9, 5, tzinfo=dt.timezone.utc)
    assert _format_rfc822(t) == "02 Jan 26 09:05 UTC"


def test_format_rfc822_naive_is_treated_as_utc():
    assert _format_rfc822(dt.datetime(2026, 1, 2, 9, 5)) == "02 Jan 26 09:05 UTC"


def test_format_rfc822_fixed_negative_offset_is_numeric():
    tz = dt.timezone(dt.timedelta(hours=-5))
    t = dt.datetime(2026, 1, 2, 9, 5, tzinfo=tz)
    # Not "UTC-05:00" — Go emits the numeric offset for an unnamed zone.
    assert _format_rfc822(t) == "02 Jan 26 09:05 -0500"


def test_format_rfc822_fixed_positive_offset_is_numeric():
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    t = dt.datetime(2026, 1, 2, 9, 5, tzinfo=tz)
    assert _format_rfc822(t) == "02 Jan 26 09:05 +0530"


def test_format_rfc822_two_digit_day_and_year_padding():
    t = dt.datetime(2007, 3, 9, 8, 4, tzinfo=dt.timezone.utc)
    assert _format_rfc822(t) == "09 Mar 07 08:04 UTC"
