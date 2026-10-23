"""The folder-trust gate that the voice QA sandbox died at — twice.

Claude Code 2.1.3xx draws its per-folder trust gate with the cursor resting
on ``No, exit`` (older builds rested it on ``1. Yes, I trust this folder``),
so the bare Enter the dismissers used to send EXITS the agent. ``TrustSpec``
now names the accepting option (``accept_words``) and reads the screen to
say which arrow keys reach it first (:meth:`TrustSpec.nav_keys_for`); the
Claude provider's spec carries ``("yes",)``. These tests pin both layouts,
the MCP-server gate and the fall-backs that keep the pre-existing behaviour
(``()`` = press the keystroke as before).
"""

from __future__ import annotations

import pytest

from backend.providers.base import TrustSpec
from backend.providers.claude import ClaudeProvider

NEW_GATE = """
 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""

OLD_GATE = """
 Quick safety check: Is this a project you created or one you trust?

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

OLDEST_GATE = (
    "Do you trust the files in this folder?\n" " ❯ 1. Yes, proceed\n" "   2. No, exit\n"
)

MCP_GATE = (
    "This project contains a new MCP server\n"
    " ❯ 1. Yes, use this one\n"
    "   2. Yes, use all\n"
    "   3. No\n"
)


@pytest.fixture
def spec():
    return ClaudeProvider().trust_prompt()


def test_claude_spec_names_the_accepting_option(spec):
    assert spec.keystroke == b"\r"  # the consumers still press Enter last
    assert tuple(w.lower() for w in spec.accept_words) == ("yes",)
    assert "Is this a project you created or one you trust?" in spec.patterns


def test_new_layout_steps_down_onto_yes(spec):
    # Cursor on "No, exit", "Yes…" one line below: one Down, then Enter.
    assert spec.nav_keys_for(NEW_GATE) == ("Down",)


def test_old_layouts_need_no_steering(spec):
    assert spec.nav_keys_for(OLD_GATE) == ()
    assert spec.nav_keys_for(OLDEST_GATE) == ()


def test_ascii_cursor_fixture_shape(spec):
    # tests/unit/data golden screens use ">" for the cursor glyph.
    pane = (
        " Quick safety check: Is this a project you created or one you trust?\n"
        " > 1. Yes, I trust this folder\n"
        "   2. No, exit\n"
        " Enter to confirm - Esc to cancel\n"
    )
    assert spec.nav_keys_for(pane) == ()
    flipped = (
        " Quick safety check: Is this a project you created or one you trust?\n"
        " > No, exit\n"
        "   Yes, I trust this folder\n"
    )
    assert spec.nav_keys_for(flipped) == ("Down",)


def test_mcp_gate_cursor_already_on_yes(spec):
    assert spec.nav_keys_for(MCP_GATE) == ()


def test_yes_above_the_cursor_steps_up(spec):
    pane = "   Yes, I trust this folder\n   Maybe\n ❯ No, exit\n"
    assert spec.nav_keys_for(pane) == ("Up", "Up")


def test_no_cursor_or_no_yes_option_falls_back(spec):
    assert spec.nav_keys_for("Do you trust the files in this folder?\n  1. Yes\n") == ()
    assert spec.nav_keys_for(" ❯ No, exit\n   Quit\n") == ()
    assert spec.nav_keys_for("") == ()


def test_option_block_stops_at_blank_lines(spec):
    # The hint line below the gap is not an option: no "Yes" there counts.
    pane = " ❯ No, exit\n   Quit\n\n Say yes to continue\n"
    assert spec.nav_keys_for(pane) == ()


def test_boxed_dialog_borders_are_ignored(spec):
    pane = "│ ❯ No, exit                 │\n│   Yes, I trust this folder │\n"
    assert spec.nav_keys_for(pane) == ("Down",)


def test_spec_without_accept_words_keeps_old_behaviour():
    plain = TrustSpec(patterns=("Open documentation url",), keystroke=b"D\r")
    assert plain.accept_words == ()
    assert plain.nav_keys_for(NEW_GATE) == ()
    assert plain.keystroke == b"D\r"


def test_word_match_is_whole_word(spec):
    # "eyes" must not read as "yes".
    pane = " ❯ No, exit\n   Close your eyes\n   Yes\n"
    assert spec.nav_keys_for(pane) == ("Down", "Down")
