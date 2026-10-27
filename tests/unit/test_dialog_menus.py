"""Claude Code's OWN command menus (/model, /effort, /config, /permissions,
/usage, the slash autocomplete) are never a dialog: the person opened them,
nobody is asking anything — yet the /model picker draws the same ``❯ 2.``
cursor list a dialog does. Real captures (Claude Code 2.1.311) pin that none
of them parses, trips the waiting patterns or counts as a dialog on screen,
while a real question still does."""

import re

import pytest

from backend.providers import dialogs
from backend.providers.claude import ClaudeProvider

from tests.unit.test_dialog_ask import _ask, _screen

MENUS = (
    "model_picker",
    "slash_autocomplete",
    "permissions",
    "config",
    "usage",
    "effort",
)


@pytest.mark.parametrize("name", MENUS)
def test_a_claude_menu_is_never_a_dialog(name):
    screen = _screen("claude2_menu_%s_w100" % name)
    prov = ClaudeProvider()
    assert dialogs.is_claude_menu(screen) is True
    assert prov.parse_dialog(screen) is None
    assert prov.dialog_on_screen(screen) is False
    for pat in prov.waiting_prompt_patterns():
        assert re.search(pat, screen) is None, (name, pat)


def test_the_model_picker_has_the_cursor_a_dialog_has():
    # The veto is what keeps it out — the cursor row itself is there.
    screen = _screen("claude2_menu_model_picker_w100")
    assert re.search(r"(?m)^\s*❯\s+\d+\.", screen)


@pytest.mark.parametrize("state", ("single", "tabs", "multi", "review"))
def test_a_real_question_still_counts(state):
    screen = _ask(state)
    prov = ClaudeProvider()
    assert dialogs.is_claude_menu(screen) is False
    assert prov.parse_dialog(screen) is not None
    assert prov.dialog_on_screen(screen) is True
    assert any(re.search(p, screen) for p in prov.waiting_prompt_patterns())


def test_a_menu_opened_over_old_dialog_text_is_still_a_menu():
    # The picker's footer sits below everything: the veto looks past the cursor.
    screen = _screen("claude2_menu_model_picker_w100")
    assert (
        dialogs.dialog_on_screen(
            screen, None, ClaudeProvider().waiting_prompt_patterns()
        )
        is False
    )
